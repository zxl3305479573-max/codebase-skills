"""
Embedder — 本地嵌入模块

使用 BAAI/bge-small-en-v1.5（384维）通过 ONNX Runtime 本地推理。
零外部 API 调用，完全离线运行。
"""

from .local import LocalEmbedder

__all__ = ["LocalEmbedder"]
