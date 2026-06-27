"""
AST Chunker — 代码切分模块

将源码文件按 AST 语法单元切分为可索引的 chunk。
每个 chunk 是完整的语法单元（函数/类/方法），禁止拦腰截断。

支持语言：Python (ast stdlib), JavaScript/TypeScript (tree-sitter)
"""

from .base import BaseChunker, Chunk, ChunkType
from .python import PythonChunker
from .javascript import JavaScriptChunker, TypeScriptChunker

# 语言 → 切分器映射
CHUNKER_REGISTRY: dict[str, type[BaseChunker]] = {
    "python": PythonChunker,
    "javascript": JavaScriptChunker,
    "typescript": TypeScriptChunker,
}

# 文件扩展名 → 语言映射
EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
}


def get_chunker(language: str) -> BaseChunker:
    """工厂方法：根据语言名获取切分器实例"""
    chunker_cls = CHUNKER_REGISTRY.get(language)
    if chunker_cls is None:
        raise ValueError(
            f"Unsupported language: {language}. "
            f"Supported: {list(CHUNKER_REGISTRY.keys())}"
        )
    return chunker_cls()


def detect_language(file_path: str) -> str | None:
    """根据文件扩展名检测语言"""
    import os
    ext = os.path.splitext(file_path)[1].lower()
    return EXTENSION_MAP.get(ext)
