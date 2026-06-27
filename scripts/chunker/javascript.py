"""
JavaScript / TypeScript Chunker

使用 tree-sitter 进行 AST 级代码切分。
切分单元：函数声明、箭头函数赋值、类声明、方法、export。
"""

from pathlib import Path

from .base import BaseChunker, Chunk, ChunkType


class JavaScriptChunker(BaseChunker):
    """JavaScript/TypeScript 代码切分器 — 基于 tree-sitter"""

    language = "javascript"
    _file_ext = ".js"

    # tree-sitter 语言对象（惰性加载）
    _parser = None
    _language = None

    def __init__(self):
        self._ensure_parser()

    @classmethod
    def _ensure_parser(cls):
        """惰性初始化 tree-sitter parser"""
        if cls._parser is not None:
            return
        try:
            import tree_sitter
        except ImportError:
            raise ImportError(
                "tree-sitter is required for JS/TS chunking. "
                "Install: pip install tree-sitter"
            )

        cls._language = tree_sitter.Language(
            # 使用 tree-sitter-javascript 的预编译 .so/.dll
            # 实际部署需检查路径；此处为默认查找路径
            str(cls._get_language_lib_path()),
            "javascript"
        )
        cls._parser = tree_sitter.Parser()
        cls._parser.set_language(cls._language)

    @staticmethod
    def _get_language_lib_path():
        """获取 tree-sitter 语言的编译库路径"""
        import sys
        # tree-sitter 0.21+ 使用 Language(path, name) 构造函数
        # 实际路径取决于 tree-sitter-javascript wheel 的安装位置
        # 此处提供一个查找逻辑
        for p in sys.path:
            candidate = Path(p) / "tree_sitter_javascript"
            if candidate.exists():
                return candidate
        # 回退：让 tree-sitter 自行查找
        raise RuntimeError(
            "tree-sitter-javascript language library not found. "
            "Install: pip install tree-sitter-javascript"
        )

    def chunk(self, file_path: str, source_code: str) -> list[Chunk]:
        """切分 JS/TS 源码文件"""
        try:
            tree = self._parser.parse(bytes(source_code, "utf-8"))
        except Exception:
            return []

        chunks: list[Chunk] = []
        lines = source_code.split("\n")
        module_name = Path(file_path).stem

        root = tree.root_node
        # 遍历顶层节点（函数声明、类声明、变量声明、export 等）
        for child in root.children:
            if child.type == "ERROR":
                continue

            results = self._process_top_level(
                child, file_path, source_code, lines, module_name
            )
            if results:
                chunks.extend(results)

        return sorted(chunks, key=lambda c: c.line_start)

    def _process_top_level(self, node, file_path: str, source_code: str,
                           lines: list[str], module_name: str) -> list[Chunk]:
        """处理顶层语法节点"""
        node_type = node.type

        if node_type == "function_declaration":
            chunk = self._make_function_chunk(node, file_path, lines, parent=None)
            return [chunk] if chunk else []

        elif node_type == "class_declaration":
            return self._make_class_chunks(node, file_path, lines)

        elif node_type == "lexical_declaration":
            return self._extract_lexical_chunks(
                node, file_path, lines, module_name
            )

        elif node_type == "variable_declaration":
            return self._extract_var_chunks(
                node, file_path, lines, module_name
            )

        elif node_type == "export_statement":
            return self._process_export(node, file_path, source_code,
                                        lines, module_name)

        return []

    # ──── function / arrow ────

    def _make_function_chunk(self, node, file_path: str,
                             lines: list[str],
                             parent: str | None) -> Chunk | None:
        """为函数声明或箭头函数创建 chunk"""
        line_start = node.start_point[0] + 1
        line_end = node.end_point[0] + 1
        source = "\n".join(lines[line_start - 1:line_end])

        name = self._extract_function_name(node)
        if not name:
            return None

        deps = self._extract_calls(node)

        chunk_id = self._make_chunk_id(file_path, name, line_start, line_end)
        chunk_type = ChunkType.METHOD if parent else ChunkType.FUNCTION

        # 提取参数列表作为签名
        params = self._extract_params(node)
        sig = f"function {name}({params})" if parent is None else f"{parent}.{name}({params})"

        return Chunk(
            chunk_id=chunk_id,
            file_path=file_path,
            language=self.language,
            name=name,
            type=chunk_type,
            line_start=line_start,
            line_end=line_end,
            source_code=source,
            parent=parent,
            dependencies=deps,
            signature=sig,
        )

    def _extract_function_name(self, node) -> str | None:
        """从函数节点提取名称"""
        for child in node.children:
            if child.type == "identifier":
                # 跳过 async/function 关键字后的第一个 identifier 是函数名
                # 需要确认它不是在参数位置
                text = child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
                if text not in ("async", "function", "get", "set"):
                    return text
        return "anonymous"

    # ──── class ────

    def _make_class_chunks(self, node, file_path: str,
                           lines: list[str]) -> list[Chunk]:
        """为类及其方法创建 chunk"""
        chunks: list[Chunk] = []

        line_start = node.start_point[0] + 1
        line_end = node.end_point[0] + 1
        source = "\n".join(lines[line_start - 1:line_end])

        class_name = self._extract_class_name(node)
        if not class_name:
            return chunks

        class_chunk = Chunk(
            chunk_id=self._make_chunk_id(file_path, class_name, line_start, line_end),
            file_path=file_path,
            language=self.language,
            name=class_name,
            type=ChunkType.CLASS,
            line_start=line_start,
            line_end=line_end,
            source_code=source,
            parent=None,
            signature=f"class {class_name}",
        )
        chunks.append(class_chunk)

        # 遍历类体中的方法
        class_body = self._find_child(node, "class_body")
        if class_body:
            for member in class_body.children:
                if member.type == "method_definition":
                    func_chunk = self._make_function_chunk(
                        member, file_path, lines, parent=class_name
                    )
                    if func_chunk:
                        chunks.append(func_chunk)

        return chunks

    def _extract_class_name(self, node) -> str | None:
        for child in node.children:
            if child.type == "identifier":
                text = child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
                return text
        return None

    # ──── variable / lexical declarations ────

    def _extract_lexical_chunks(self, node, file_path: str,
                                lines: list[str],
                                module_name: str) -> list[Chunk]:
        """处理 let/const 声明（含箭头函数）"""
        return self._extract_var_chunks(node, file_path, lines, module_name)

    def _extract_var_chunks(self, node, file_path: str,
                            lines: list[str],
                            module_name: str) -> list[Chunk]:
        """处理 var/let/const 声明，检测箭头函数赋值"""
        chunks: list[Chunk] = []

        # 遍历声明中的每个变量
        for child in self._traverse(node):
            if child.type == "variable_declarator":
                name_node = self._find_child(child, "identifier")
                value_node = self._find_child(child, "arrow_function")
                if name_node and value_node:
                    chunk = self._make_arrow_chunk(
                        name_node, value_node, file_path, lines
                    )
                    if chunk:
                        chunks.append(chunk)

        return chunks

    def _make_arrow_chunk(self, name_node, arrow_node, file_path: str,
                          lines: list[str]) -> Chunk | None:
        """为箭头函数赋值创建 chunk"""
        name_text = name_node.text.decode("utf-8") if isinstance(name_node.text, bytes) else name_node.text
        line_start = arrow_node.start_point[0] + 1
        line_end = arrow_node.end_point[0] + 1

        if line_end < line_start:
            return None

        source = "\n".join(lines[line_start - 1:line_end])
        deps = self._extract_calls(arrow_node)

        return Chunk(
            chunk_id=self._make_chunk_id(file_path, name_text, line_start, line_end),
            file_path=file_path,
            language=self.language,
            name=name_text,
            type=ChunkType.FUNCTION,
            line_start=line_start,
            line_end=line_end,
            source_code=source,
            parent=None,
            dependencies=deps,
            signature=f"const {name_text} = (...) => {{ ... }}",
        )

    # ──── export ────

    def _process_export(self, node, file_path: str, source_code: str,
                        lines: list[str], module_name: str) -> list[Chunk]:
        """处理 export 语句 — 递归处理 export 包裹的所有声明类型"""
        chunks: list[Chunk] = []
        exportable_types = (
            "function_declaration", "class_declaration",
            "lexical_declaration", "variable_declaration",
        )
        for child in node.children:
            if child.type in exportable_types:
                results = self._process_top_level(
                    child, file_path, source_code, lines, module_name
                )
                if results:
                    chunks.extend(results)
        return chunks

    # ──── tree-sitter helpers ────

    @staticmethod
    def _find_child(node, child_type: str):
        """查找直接子节点中第一个匹配类型的节点"""
        for child in node.children:
            if child.type == child_type:
                return child
        return None

    @staticmethod
    def _traverse(node):
        """递归遍历所有子节点（生成器）"""
        yield node
        if hasattr(node, 'children'):
            for child in node.children:
                yield from JavaScriptChunker._traverse(child)

    def _extract_params(self, node) -> str:
        """提取函数参数"""
        params_node = self._find_child(node, "formal_parameters")
        if not params_node:
            return ""
        text = params_node.text.decode("utf-8") if isinstance(params_node.text, bytes) else params_node.text
        return text.strip("()").strip()

    def _extract_calls(self, node) -> list[str]:
        """提取函数体内的所有函数调用名"""
        calls: set[str] = set()
        for child in self._traverse(node):
            if child.type == "call_expression":
                func_node = child.child_by_field_name("function")
                if func_node:
                    text = func_node.text.decode("utf-8") if isinstance(func_node.text, bytes) else func_node.text
                    # 只取最后一截（obj.method → method）
                    if "." in text:
                        text = text.rsplit(".", 1)[-1]
                    calls.add(text)
        return sorted(calls)


class TypeScriptChunker(JavaScriptChunker):
    """TypeScript 切分器 — 继承 JS 切分器，使用 typescript 语法"""

    language = "typescript"
    _file_ext = ".ts"
    _ts_parser = None
    _ts_language = None

    def __init__(self):
        self._ensure_ts_parser()

    @classmethod
    def _ensure_ts_parser(cls):
        """惰性初始化 TypeScript parser"""
        if cls._ts_parser is not None:
            cls._parser = cls._ts_parser
            return
        try:
            import tree_sitter
        except ImportError:
            raise ImportError(
                "tree-sitter is required for TypeScript chunking. "
                "Install: pip install tree-sitter"
            )

        cls._ts_language = tree_sitter.Language(
            str(cls._get_ts_language_lib_path()),
            "typescript"
        )
        cls._ts_parser = tree_sitter.Parser()
        cls._ts_parser.set_language(cls._ts_language)
        # 更新父类 parser
        cls._parser = cls._ts_parser

    @staticmethod
    def _get_ts_language_lib_path():
        import sys
        for p in sys.path:
            candidate = Path(p) / "tree_sitter_typescript"
            if candidate.exists():
                return candidate
        raise RuntimeError(
            "tree-sitter-typescript language library not found. "
            "Install: pip install tree-sitter-typescript"
        )
