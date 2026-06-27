"""
向量语义检索

基于 LanceDB 嵌入式向量数据库，使用 BGE-small 嵌入模型
对查询和 chunk 进行语义相似度搜索。
"""

import json
import os
import warnings
from pathlib import Path
from typing import Sequence

from .fusion import SearchResult


class VectorRetriever:
    """
    向量语义检索引擎

    封装 LanceDB 的读写和 ANN 搜索。

    使用方法:
        retriever = VectorRetriever(db_path=".code-kb/lancedb")
        retriever.index(chunks, embedder)
        results = retriever.search("支付回调异常", embedder, top_k=10)
    """

    # LanceDB 表名
    TABLE_NAME = "code_chunks"
    # 向量列名
    VECTOR_COL = "vector"

    def __init__(self, db_path: str = ".code-kb/lancedb"):
        """
        Args:
            db_path: LanceDB 数据库目录
        """
        self.db_path = db_path
        self._db = None
        self._table = None

    @property
    def db(self):
        """惰性连接 LanceDB"""
        if self._db is None:
            try:
                import lancedb
                os.makedirs(self.db_path, exist_ok=True)
                self._db = lancedb.connect(self.db_path)
            except ImportError:
                raise ImportError(
                    "lancedb is required for vector search. "
                    "Install: pip install lancedb>=0.12"
                )
        return self._db

    @property
    def table(self):
        """获取或创建代码块表"""
        if self._table is None:
            try:
                self._table = self.db.open_table(self.TABLE_NAME)
            except Exception:
                self._table = None
        return self._table

    # ──── write ────

    def index(self, chunks: list[dict],
              embedder=None,
              batch_size: int = 32,
              append: bool = False) -> int:
        """
        将 chunk 列表向量化并写入 LanceDB

        Args:
            chunks: chunk dict 列表
            embedder: LocalEmbedder 实例
            batch_size: 嵌入批大小
            append: 若为 True，追加到现有表而非全量替换
                    （用于增量更新；调用前需先 delete_chunks 清理旧数据）

        Returns:
            写入的 chunk 数量
        """
        if not chunks:
            return 0

        # 如果没有提供 embedder，使用哈希编码（降级模式）
        if embedder is None:
            from embedder.local import LocalEmbedder
            embedder = LocalEmbedder()
            warnings.warn("No embedder provided, using fallback hash encoding")

        # 准备嵌入文本
        texts = [self._chunk_to_text(c) for c in chunks]

        # 批量嵌入
        vectors = embedder.embed(texts)

        # 构造写入数据
        records: list[dict] = []
        for chunk, vector in zip(chunks, vectors):
            record = {
                "chunk_id": chunk["chunk_id"],
                "file_path": chunk["file_path"],
                "name": chunk["name"],
                "chunk_type": chunk.get("type", ""),
                "line_start": int(chunk.get("line_start", 0)),
                "line_end": int(chunk.get("line_end", 0)),
                "source_code": chunk.get("source_code", ""),
                "parent": chunk.get("parent", ""),
                "docstring": chunk.get("docstring", ""),
                "dependencies": json.dumps(chunk.get("dependencies", [])),
                "callers": json.dumps(chunk.get("callers", [])),
                "signature": chunk.get("signature", ""),
                "content_hash": chunk.get("content_hash", ""),
                "vector": vector,
            }
            records.append(record)

        # 写入数据
        if append:
            self._append_records(records, chunks, vectors)
        else:
            self._replace_records(records, chunks, vectors)

        return len(chunks)

    def _replace_records(self, records: list[dict],
                        chunks: list[dict],
                        vectors: "list[list[float]]") -> None:
        """全量替换：删除旧表 → 建新表（全量索引）"""
        try:
            self.db.drop_table(self.TABLE_NAME)
        except Exception:
            pass

        try:
            self._table = self.db.create_table(self.TABLE_NAME, records)
        except Exception as e:
            warnings.warn(f"LanceDB write failed: {e}. Falling back to JSON index.")
            self._save_json_index(chunks, vectors)

    def _append_records(self, records: list[dict],
                       chunks: list[dict],
                       vectors: "list[list[float]]") -> None:
        """追加写入：保留已有数据，仅添加/更新新记录（增量更新）"""
        # 尝试 LanceDB 追加
        if self.table is not None:
            try:
                self._table.add(records)
                return  # LanceDB 追加成功
            except Exception as e:
                warnings.warn(f"LanceDB append failed: {e}. Falling back to JSON merge.")

        # JSON 降级：合并而非覆盖
        self._merge_json_index(chunks, vectors)

    def _save_json_index(self, chunks: list[dict],
                         vectors: "list[list[float]]") -> None:
        """将索引保存为 JSON 文件（全量替换，LanceDB 不可用时的降级方案）"""
        index_dir = Path(self.db_path) / "json_index"
        index_dir.mkdir(parents=True, exist_ok=True)

        index_data = []
        for chunk, vec in zip(chunks, vectors):
            index_data.append({
                **{k: v for k, v in chunk.items() if k != "source_code"},
                "source_code": chunk.get("source_code", "")[:500],
                "vector": vec,
            })

        with open(index_dir / "index.json", "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False)

    def _merge_json_index(self, chunks: list[dict],
                         vectors: "list[list[float]]") -> None:
        """合并 chunk 到 JSON 索引（增量更新降级方案）"""
        index_file = Path(self.db_path) / "json_index" / "index.json"
        index_dir = index_file.parent
        index_dir.mkdir(parents=True, exist_ok=True)

        # 加载已有数据
        existing: dict[str, dict] = {}
        if index_file.exists():
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                for item in existing_data:
                    existing[item.get("chunk_id", "")] = item
            except Exception:
                pass

        # 合并新数据（按 chunk_id 去重，新数据覆盖旧数据）
        for chunk, vec in zip(chunks, vectors):
            merged = {
                **{k: v for k, v in chunk.items() if k != "source_code"},
                "source_code": chunk.get("source_code", "")[:500],
                "vector": vec,
            }
            existing[merged["chunk_id"]] = merged

        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(list(existing.values()), f, ensure_ascii=False)

    # ──── search ────

    def search(self, query: str, embedder=None,
               top_k: int = 10) -> list[SearchResult]:
        """
        向量语义检索

        Args:
            query: 查询文本
            embedder: LocalEmbedder 实例
            top_k: 返回结果数

        Returns:
            SearchResult 列表，按余弦相似度降序
        """
        if not query.strip():
            return []

        # 嵌入查询
        if embedder is None:
            from embedder.local import LocalEmbedder
            embedder = LocalEmbedder()

        query_vec = embedder.embed_single(query)
        if not query_vec:
            return []

        # 尝试 LanceDB ANN 搜索
        if self.table is not None:
            try:
                return self._lancedb_search(query_vec, top_k)
            except Exception as e:
                warnings.warn(f"LanceDB search failed: {e}. Falling back to brute-force.")

        # 降级为暴力搜索 JSON 索引
        return self._json_search(query_vec, top_k)

    def _lancedb_search(self, query_vec: list[float],
                        top_k: int) -> list[SearchResult]:
        """使用 LanceDB 进行 ANN 搜索"""
        results = self.table.search(query_vec).limit(top_k).to_list()

        search_results: list[SearchResult] = []
        for r in results:
            # LanceDB _distance is a distance metric (lower = better).
            # Convert to similarity score (higher = better) for RRF fusion.
            raw_distance = float(r.get("_distance", 0))
            score = 1.0 / (1.0 + raw_distance)
            search_results.append(SearchResult(
                chunk_id=r.get("chunk_id", ""),
                file_path=r.get("file_path", ""),
                name=r.get("name", ""),
                chunk_type=r.get("chunk_type", ""),
                line_start=r.get("line_start", 0),
                line_end=r.get("line_end", 0),
                score=score,
                source="vector",
                snippet=r.get("source_code", "")[:200],
                metadata={
                    "parent": r.get("parent", ""),
                    "dependencies": json.loads(r.get("dependencies", "[]")),
                    "callers": json.loads(r.get("callers", "[]")),
                }
            ))

        return search_results

    def _json_search(self, query_vec: list[float],
                     top_k: int) -> list[SearchResult]:
        """暴力搜索 JSON 索引（LanceDB 不可用时的降级方案）"""
        import math

        index_file = Path(self.db_path) / "json_index" / "index.json"
        if not index_file.exists():
            return []

        try:
            with open(index_file, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            warnings.warn(f"JSON index corrupted: {e}")
            return []

        # 计算余弦相似度
        scored: list[tuple[dict, float]] = []
        for item in index_data:
            vec = item.get("vector", [])
            if not vec:
                continue
            sim = self._cosine_similarity(query_vec, vec)
            scored.append((item, sim))

        # 按相似度降序排序
        scored.sort(key=lambda x: x[1], reverse=True)

        results: list[SearchResult] = []
        for item, score in scored[:top_k]:
            results.append(SearchResult(
                chunk_id=item.get("chunk_id", ""),
                file_path=item.get("file_path", ""),
                name=item.get("name", ""),
                chunk_type=item.get("type", ""),
                line_start=item.get("line_start", 0),
                line_end=item.get("line_end", 0),
                score=score,
                source="vector",
                snippet=item.get("source_code", "")[:200],
                metadata={
                    "parent": item.get("parent", ""),
                    "dependencies": item.get("dependencies", []),
                    "callers": item.get("callers", []),
                }
            ))

        return results

    # ──── helpers ────

    @staticmethod
    def _chunk_to_text(chunk: dict) -> str:
        """将 chunk 转为嵌入文本：签名 + docstring + 代码前10行"""
        parts = [
            chunk.get("signature", ""),
            chunk.get("docstring", ""),
            "\n".join(chunk.get("source_code", "").split("\n")[:10]),
        ]
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算余弦相似度"""
        import math
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def count(self) -> int:
        """返回已索引的 chunk 数量"""
        if self.table is not None:
            try:
                return self.table.count_rows()
            except Exception:
                pass
        # 检查 JSON 索引
        index_file = Path(self.db_path) / "json_index" / "index.json"
        if index_file.exists():
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    return len(json.load(f))
            except Exception:
                pass
        return 0

    def delete_chunks(self, file_path: str) -> int:
        """删除指定文件的所有 chunk，返回删除数量"""
        removed = 0

        # Try LanceDB first
        if self.table is not None:
            try:
                before = self.table.count_rows()
                # Use LanceDB filter with escaped file path to prevent injection
                safe_path = file_path.replace("'", "''")
                self.table.delete(f"file_path = '{safe_path}'")
                after = self.table.count_rows()
                removed = before - after
            except Exception as e:
                warnings.warn(f"LanceDB delete failed: {e}")

        # JSON index fallback: remove matching entries
        index_file = Path(self.db_path) / "json_index" / "index.json"
        if index_file.exists():
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    index_data = json.load(f)
                before = len(index_data)
                index_data = [item for item in index_data
                             if item.get("file_path") != file_path]
                after = len(index_data)
                with open(index_file, "w", encoding="utf-8") as f:
                    json.dump(index_data, f, ensure_ascii=False)
                json_removed = before - after
                removed = max(removed, json_removed)
            except Exception as e:
                warnings.warn(f"JSON index delete failed: {e}")

        return removed
