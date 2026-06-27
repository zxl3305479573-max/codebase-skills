"""
Python AST Chunker

使用 Python 标准库 `ast` 进行零额外依赖的代码切分。
切分单元：函数、异步函数、类、方法、模块级变量赋值。
"""

import ast
from pathlib import Path

from .base import BaseChunker, Chunk, ChunkType


class PythonChunker(BaseChunker):
    """Python 代码切分器 — 基于 ast 标准库"""

    language = "python"

    def chunk(self, file_path: str, source_code: str) -> list[Chunk]:
        """切分 Python 源码文件"""
        try:
            tree = ast.parse(source_code, filename=file_path)
        except SyntaxError:
            # 语法错误的文件降级为空 chunk 列表
            return []

        chunks: list[Chunk] = []
        lines = source_code.split("\n")
        module_name = Path(file_path).stem

        # 遍历顶层节点
        for node in ast.iter_child_nodes(tree):
            result = self._process_top_level(
                node, file_path, source_code, lines, module_name
            )
            if result:
                if isinstance(result, list):
                    chunks.extend(result)
                else:
                    chunks.append(result)

        return sorted(chunks, key=lambda c: c.line_start)

    # ──── top-level dispatcher ────

    def _process_top_level(self, node: ast.AST, file_path: str,
                           source_code: str, lines: list[str],
                           module_name: str) -> Chunk | list[Chunk] | None:
        """处理顶层 AST 节点"""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return self._make_function_chunk(node, file_path, source_code,
                                             lines, parent=None)
        elif isinstance(node, ast.ClassDef):
            return self._make_class_chunks(node, file_path, source_code, lines)
        elif isinstance(node, ast.Assign):
            return self._make_variable_chunk(node, file_path, source_code,
                                             lines, module_name)
        elif isinstance(node, ast.AnnAssign):
            return self._make_ann_assign_chunk(node, file_path, source_code,
                                               lines, module_name)
        return None

    # ──── function / method ────

    def _make_function_chunk(self, node: ast.FunctionDef | ast.AsyncFunctionDef,
                             file_path: str, source_code: str,
                             lines: list[str],
                             parent: str | None) -> Chunk | None:
        """为函数/方法创建 chunk"""
        line_start = node.lineno
        line_end = node.end_lineno or line_start

        source = "\n".join(lines[line_start - 1:line_end])

        # 提取签名
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        args = self._format_args(node.args)
        returns = ""
        if node.returns:
            returns = f" -> {ast.unparse(node.returns)}"
        signature = f"{prefix} {node.name}({args}){returns}"

        # 提取依赖（被调用的函数名）
        deps = self._extract_calls(node)

        chunk_id = self._make_chunk_id(file_path, node.name, line_start, line_end)
        chunk_type = ChunkType.METHOD if parent else ChunkType.FUNCTION

        return Chunk(
            chunk_id=chunk_id,
            file_path=file_path,
            language="python",
            name=node.name,
            type=chunk_type,
            line_start=line_start,
            line_end=line_end,
            source_code=source,
            parent=parent,
            dependencies=deps,
            docstring=ast.get_docstring(node),
            signature=signature,
        )

    # ──── class ────

    def _make_class_chunks(self, node: ast.ClassDef, file_path: str,
                           source_code: str, lines: list[str]) -> list[Chunk]:
        """为类及其所有方法创建 chunk"""
        chunks: list[Chunk] = []

        # 类的 chunk
        line_start = node.lineno
        line_end = node.end_lineno or line_start
        source = "\n".join(lines[line_start - 1:line_end])

        bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
        signature = f"class {node.name}" + (f"({bases})" if bases else "")

        class_chunk = Chunk(
            chunk_id=self._make_chunk_id(file_path, node.name, line_start, line_end),
            file_path=file_path,
            language="python",
            name=node.name,
            type=ChunkType.CLASS,
            line_start=line_start,
            line_end=line_end,
            source_code=source,
            parent=None,
            docstring=ast.get_docstring(node),
            signature=signature,
        )
        chunks.append(class_chunk)

        # 类中每个方法
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_chunk = self._make_function_chunk(
                    child, file_path, source_code, lines, parent=node.name
                )
                if func_chunk:
                    chunks.append(func_chunk)

        return chunks

    # ──── module-level variable ────

    def _make_variable_chunk(self, node: ast.Assign, file_path: str,
                             source_code: str, lines: list[str],
                             module_name: str) -> Chunk | None:
        """为模块级变量赋值创建 chunk"""
        # 只处理有名字的目标（跳过 tuple unpacking 等复杂情况）
        names = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id)

        if not names:
            return None

        name = names[0]
        line_start = node.lineno
        line_end = node.end_lineno or line_start
        source = "\n".join(lines[line_start - 1:line_end])

        return Chunk(
            chunk_id=self._make_chunk_id(file_path, name, line_start, line_end),
            file_path=file_path,
            language="python",
            name=name,
            type=ChunkType.VARIABLE,
            line_start=line_start,
            line_end=line_end,
            source_code=source,
            parent=module_name,
            signature=f"{name} = ...",
        )

    # ──── annotated assignment ────

    def _make_ann_assign_chunk(self, node: ast.AnnAssign, file_path: str,
                               source_code: str, lines: list[str],
                               module_name: str) -> Chunk | None:
        """为带类型注解的模块级变量创建 chunk (e.g. x: int = 1)"""
        target = node.target
        if not isinstance(target, ast.Name):
            return None

        name = target.id
        line_start = node.lineno
        line_end = node.end_lineno or line_start
        source = "\n".join(lines[line_start - 1:line_end])

        annotation = ast.unparse(node.annotation) if node.annotation else ""
        sig = f"{name}: {annotation}" if annotation else f"{name}: ..."

        return Chunk(
            chunk_id=self._make_chunk_id(file_path, name, line_start, line_end),
            file_path=file_path,
            language="python",
            name=name,
            type=ChunkType.VARIABLE,
            line_start=line_start,
            line_end=line_end,
            source_code=source,
            parent=module_name,
            signature=sig,
        )

    # ──── helpers ────

    def _format_args(self, args: ast.arguments) -> str:
        """格式化函数参数列表"""
        parts = []

        # 位置参数
        for arg in args.args:
            annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
            parts.append(f"{arg.arg}{annotation}")

        # *args
        if args.vararg:
            annotation = (f": {ast.unparse(args.vararg.annotation)}"
                         if args.vararg.annotation else "")
            parts.append(f"*{args.vararg.arg}{annotation}")

        # 关键字参数
        for arg in args.kwonlyargs:
            annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
            parts.append(f"{arg.arg}{annotation}")

        # **kwargs
        if args.kwarg:
            annotation = (f": {ast.unparse(args.kwarg.annotation)}"
                         if args.kwarg.annotation else "")
            parts.append(f"**{args.kwarg.arg}{annotation}")

        return ", ".join(parts)

    def _extract_calls(self, node: ast.AST) -> list[str]:
        """从函数体提取所有被调用的函数名"""
        calls: set[str] = set()

        class CallVisitor(ast.NodeVisitor):
            def visit_Call(self, call_node: ast.Call):
                name = PythonChunker._get_call_name(call_node.func)
                if name:
                    calls.add(name)
                self.generic_visit(call_node)

        CallVisitor().visit(node)
        return sorted(calls)

    @staticmethod
    def _get_call_name(func_node: ast.AST) -> str | None:
        """从 Call.func 节点提取函数名"""
        if isinstance(func_node, ast.Name):
            return func_node.id
        if isinstance(func_node, ast.Attribute):
            return func_node.attr
        return None
