"""
BM25 关键词检索

基于 rank-bm25 库，在 chunk 的：
- 函数名 / 类名 / 变量名
- 文件路径
- docstring
- 源代码前 N 行
上进行关键词匹配。

权重策略：精确匹配函数名 > 部分匹配 > docstring 匹配
"""

import re
from dataclasses import dataclass
from typing import Sequence

from .fusion import SearchResult


@dataclass
class BM25Config:
    """BM25 检索配置"""
    # 字段权重
    weight_name: float = 3.0        # 函数名/类名精确匹配
    weight_path: float = 1.5        # 文件路径
    weight_docstring: float = 1.0   # docstring
    weight_code: float = 2.0        # 源代码
    # BM25 参数
    k1: float = 1.2
    b: float = 0.75


class BM25Retriever:
    """
    BM25 关键词检索引擎

    使用 rank-bm25 库实现。对每个 chunk 构造多字段加权文档，
    检索时合并各字段分数。
    """

    def __init__(self, config: BM25Config | None = None):
        self.config = config or BM25Config()
        self._chunks: list[dict] = []
        self._bm25 = None
        self._doc_texts: list[str] = []

    def index(self, chunks: list[dict]) -> None:
        """
        构建 BM25 索引

        Args:
            chunks: chunk dict 列表（来自 chunker 输出）
        """
        self._chunks = chunks
        self._doc_texts = []

        for c in chunks:
            # 构造加权文档文本：重复关键字段以提升权重
            parts = []

            # 名称字段（高权重）
            name = c.get("name", "")
            if name:
                parts.extend([name] * int(self.config.weight_name))

            # 文件路径（中权重）
            file_path = c.get("file_path", "")
            if file_path:
                # 路径分段都加入
                path_parts = re.split(r'[/\\]', file_path)
                parts.extend(path_parts * int(self.config.weight_path))

            # 源代码（中高权重）
            code = c.get("source_code", "")
            if code:
                parts.extend([code] * int(self.config.weight_code))

            # docstring（基准权重）
            doc = c.get("docstring", "")
            if doc:
                parts.extend([doc] * int(self.config.weight_docstring))

            self._doc_texts.append(" ".join(parts))

        # 构建 BM25 索引
        if self._doc_texts:
            try:
                from rank_bm25 import BM25Okapi
                tokenized = [text.lower().split() for text in self._doc_texts]
                self._bm25 = BM25Okapi(
                    tokenized,
                    k1=self.config.k1,
                    b=self.config.b,
                )
            except ImportError:
                # rank-bm25 不可用时降级为简单 TF-IDF
                self._bm25 = None

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        """
        BM25 检索

        Args:
            query: 查询字符串
            top_k: 返回结果数

        Returns:
            SearchResult 列表，按分数降序
        """
        if not self._doc_texts or not query.strip():
            return []

        query_tokens = query.lower().split()

        if self._bm25 is not None:
            scores = self._bm25.get_scores(query_tokens)
        else:
            scores = self._fallback_tfidf(query_tokens)

        # 选出 top_k
        indexed_scores = list(enumerate(scores))
        indexed_scores.sort(key=lambda x: x[1], reverse=True)

        results: list[SearchResult] = []
        for idx, score in indexed_scores[:top_k]:
            if score <= 0:
                continue
            chunk = self._chunks[idx]
            results.append(SearchResult(
                chunk_id=chunk["chunk_id"],
                file_path=chunk["file_path"],
                name=chunk["name"],
                chunk_type=chunk.get("type", ""),
                line_start=chunk["line_start"],
                line_end=chunk["line_end"],
                score=float(score),
                source="bm25",
                snippet=chunk.get("source_code", "")[:200],
                metadata={
                    "parent": chunk.get("parent", ""),
                    "dependencies": chunk.get("dependencies", []),
                    "callers": chunk.get("callers", []),
                }
            ))

        return results

    def _fallback_tfidf(self, query_tokens: list[str]) -> list[float]:
        """
        简单 TF-IDF 降级方案（无需 rank-bm25）

        TF = 词在文档中出现次数 / 文档总词数
        IDF = log(总文档数 / 包含该词的文档数)
        """
        import math

        n_docs = len(self._doc_texts)
        doc_tokens_list = [text.lower().split() for text in self._doc_texts]

        # 计算 IDF
        idf: dict[str, float] = {}
        for token in set(query_tokens):
            doc_count = sum(1 for tokens in doc_tokens_list if token in tokens)
            idf[token] = math.log((n_docs + 1) / (doc_count + 1)) + 1

        # 计算每个文档的 TF-IDF 分数
        scores: list[float] = []
        for tokens in doc_tokens_list:
            score = 0.0
            doc_len = len(tokens) or 1
            for token in query_tokens:
                tf = tokens.count(token) / doc_len
                score += tf * idf.get(token, 0)
            scores.append(score)

        return scores
