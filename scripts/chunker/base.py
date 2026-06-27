"""
Base Chunker — 抽象基类

定义 chunk 数据结构和切分器接口。
所有语言的切分器必须继承此类并实现 chunk() 方法。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
import hashlib


class ChunkType(Enum):
    """Chunk 类型枚举"""
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    VARIABLE = "variable"          # 模块级变量
    INTERFACE = "interface"        # TS/Java
    STRUCT = "struct"              # Go/Rust
    IMPL = "impl"                  # Rust impl 块
    EXPORT = "export"              # 模块级导出
    UNKNOWN = "unknown"


@dataclass
class Chunk:
    """
    代码块元数据

    每个 chunk 代表一个完整的语法单元，包含：
    - 位置信息（文件路径 + 行范围）
    - 语义信息（名称、类型、父级）
    - 关系信息（依赖、调用者）
    - 内容哈希（用于增量更新检测）
    """
    chunk_id: str
    file_path: str
    language: str
    name: str
    type: ChunkType
    line_start: int
    line_end: int
    source_code: str                          # 原始源代码
    parent: str | None = None                 # 父级名称（类名/模块名）
    dependencies: list[str] = field(default_factory=list)   # 它调用了谁 (callee)
    callers: list[str] = field(default_factory=list)        # 谁调用了它
    docstring: str | None = None              # 文档字符串
    content_hash: str = ""                    # 源代码 SHA256
    signature: str = ""                       # 函数/方法签名

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = hashlib.sha256(
                self.source_code.encode("utf-8")
            ).hexdigest()[:16]

    @property
    def line_range(self) -> tuple[int, int]:
        """返回行范围 (start, end)"""
        return (self.line_start, self.line_end)

    @property
    def qualified_name(self) -> str:
        """返回完全限定名：Parent.name"""
        if self.parent:
            return f"{self.parent}.{self.name}"
        return self.name

    def to_dict(self) -> dict:
        """序列化为 dict（用于 JSON / LanceDB 存储）"""
        return {
            "chunk_id": self.chunk_id,
            "file_path": self.file_path,
            "language": self.language,
            "type": self.type.value,
            "name": self.name,
            "parent": self.parent or "",
            "line_start": self.line_start,
            "line_end": self.line_end,
            "dependencies": self.dependencies,
            "callers": self.callers,
            "docstring": self.docstring or "",
            "content_hash": self.content_hash,
            "signature": self.signature,
            "source_code": self.source_code,
        }

    @property
    def searchable_text(self) -> str:
        """返回用于向量嵌入的文本：签名 + docstring + 代码前10行"""
        lines = self.source_code.split("\n")
        head = "\n".join(lines[:10])
        parts = [self.signature, self.docstring or "", head]
        return "\n".join(p for p in parts if p)


class BaseChunker(ABC):
    """切分器抽象基类"""

    # 子类必须定义语言名
    language: str = "unknown"

    @abstractmethod
    def chunk(self, file_path: str, source_code: str) -> list[Chunk]:
        """
        将源码文件切分为 chunk 列表

        Args:
            file_path: 文件路径（用于填充 chunk.file_path）
            source_code: 文件完整源代码

        Returns:
            Chunk 列表，按行号升序排列
        """
        ...

    def _make_chunk_id(self, file_path: str, name: str,
                       line_start: int, line_end: int) -> str:
        """生成 chunk 唯一 ID"""
        return f"{file_path}:{name}:{line_start}-{line_end}"

    def _hash_content(self, source_code: str) -> str:
        return hashlib.sha256(source_code.encode("utf-8")).hexdigest()[:16]

    def _extract_docstring(self, node_source: str) -> str | None:
        """
        尝试从函数/类源码中提取 docstring
        简单实现：查找第一个三引号字符串
        """
        import re
        # 匹配 """...""" 或 '''...'''
        match = re.search(r'(?:"{3}[\s\S]*?"{3}|\'{3}[\s\S]*?\'{3})', node_source)
        if match:
            doc = match.group(0)[3:-3].strip()
            # 只取第一行作为摘要
            first_line = doc.split("\n")[0].strip()
            return first_line[:200] if first_line else None
        return None
