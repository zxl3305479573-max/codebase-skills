"""
RRF (Reciprocal Rank Fusion) 多路融合

将 BM25、向量、依赖图三路检索结果进行归一化融合。

RRF 公式：
    RRF_score(doc) = Σ 1 / (k + rank_i(doc))
    其中 k=60, rank_i 是 doc 在第 i 路检索中的排名

复用了 RAG_AGENT 项目中已验证过的 RRF 实现。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchResult:
    """单条检索结果"""
    chunk_id: str
    file_path: str
    name: str
    chunk_type: str
    line_start: int
    line_end: int
    score: float
    source: str               # "bm25" | "vector" | "graph:callers" | ...
    snippet: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def line_range(self) -> tuple[int, int]:
        return (self.line_start, self.line_end)

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "file_path": self.file_path,
            "name": self.name,
            "chunk_type": self.chunk_type,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "score": round(self.score, 4),
            "source": self.source,
            "snippet": self.snippet,
            "metadata": self.metadata,
        }


class RRFusion:
    """
    Reciprocal Rank Fusion 融合器

    对多路检索结果按排名进行分数融合，消除各路分数尺度不一致的问题。

    使用方法:
        fusion = RRFusion(k=60)
        results = bm25.search(query)
        results.extend(vector.search(query))
        merged = fusion.fuse([bm25_results, vector_results, graph_results])
    """

    def __init__(self, k: int = 60):
        """
        Args:
            k: RRF 常数，默认 60（经典取值）
        """
        self.k = k

    def fuse(self, result_lists: list[list[SearchResult]],
             top_k: int = 5) -> list[SearchResult]:
        """
        融合多路检索结果

        Args:
            result_lists: 各路检索引擎返回的 SearchResult 列表
            top_k: 最终返回的结果数

        Returns:
            融合后的 Top-K SearchResult 列表
        """
        if not result_lists:
            return []

        # chunk_id → 累计 RRF 分数
        rrf_scores: dict[str, float] = {}
        # chunk_id → 最佳 SearchResult（取分数最高的来源）
        best_results: dict[str, SearchResult] = {}

        for result_list in result_lists:
            # 每路结果按分数排序（降序）
            sorted_list = sorted(result_list, key=lambda r: r.score, reverse=True)

            for rank, result in enumerate(sorted_list, start=1):
                cid = result.chunk_id

                # RRF 分数累加
                rrf_score = 1.0 / (self.k + rank)
                rrf_scores[cid] = rrf_scores.get(cid, 0.0) + rrf_score

                # 保留分数最高的来源
                if cid not in best_results or result.score > best_results[cid].score:
                    best_results[cid] = result

        # 按 RRF 分数排序
        sorted_ids = sorted(rrf_scores, key=lambda cid: rrf_scores[cid], reverse=True)

        # 取 Top-K
        final_results: list[SearchResult] = []
        for cid in sorted_ids[:top_k]:
            result = best_results[cid]
            # 更新分数为 RRF 分数
            result.score = round(rrf_scores[cid], 4)
            final_results.append(result)

        return final_results

    def fuse_with_dedup(self, result_lists: list[list[SearchResult]],
                        top_k: int = 5) -> list[SearchResult]:
        """
        融合 + 去重（同名但不同 chunk 的保留高分者）

        当同一函数名出现在多个位置（如重载），只保留得分最高的 chunk。
        """
        fused = self.fuse(result_lists, top_k=top_k * 2)

        # 按 name 去重
        seen_names: set[str] = set()
        deduped: list[SearchResult] = []

        for result in fused:
            name_key = f"{result.file_path}:{result.name}"
            if name_key not in seen_names:
                seen_names.add(name_key)
                deduped.append(result)
                if len(deduped) >= top_k:
                    break

        return deduped

    def format_results(self, results: list[SearchResult],
                       query: str | None = None,
                       rewritten: str | None = None) -> dict:
        """
        将检索结果格式化为 JSON 输出

        输出格式符合 SKILL.md 中定义的接口规范。
        """
        output: dict[str, Any] = {
            "results": [r.to_dict() for r in results],
            "count": len(results),
        }
        if query:
            output["query"] = query
        if rewritten:
            output["rewritten"] = rewritten

        return output
