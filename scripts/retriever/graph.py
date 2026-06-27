"""
依赖图遍历检索

基于静态调用关系（import / call）构建有向依赖图。
当其他检索命中某个函数时，沿图扩展上行（caller）和下行（callee）上下文。
"""

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .fusion import SearchResult


class DependencyGraph:
    """
    代码依赖图

    节点：函数/方法（由 chunk_id 标识）
    边：A → B 表示 A 调用了 B

    属性：
    - callers[node] → 谁调用了 node
    - callees[node] → node 调用了谁
    - file_nodes[file_path] → 该文件包含的所有节点
    """

    def __init__(self):
        # caller: node → [nodes that call this node]
        self.callers: dict[str, set[str]] = defaultdict(set)
        # callee: node → [nodes this node calls]
        self.callees: dict[str, set[str]] = defaultdict(set)
        # 节点元数据
        self.nodes: dict[str, dict] = {}
        # 文件 → 节点列表
        self.file_nodes: dict[str, list[str]] = defaultdict(list)

    # ──── build ────

    def build_from_chunks(self, chunks: list[dict],
                         merge: bool = False) -> None:
        """
        从 chunk 列表构建依赖图

        每个 chunk 的 'dependencies' 字段包含它调用的函数名列表。
        我们通过函数名匹配来建立边。

        Args:
            chunks: chunk dict 列表
            merge: 若为 True，新节点还会尝试与已有节点建立边
                   （用于增量更新场景）
        """
        # 先建立 name → chunk_id 的索引
        name_to_ids: dict[str, list[str]] = defaultdict(list)
        for c in chunks:
            chunk_id = c["chunk_id"]
            name = c.get("name", "")
            self.nodes[chunk_id] = c
            self.file_nodes[c.get("file_path", "")].append(chunk_id)

            if name:
                name_to_ids[name].append(chunk_id)

        # 构建已有节点的 name 索引（用于增量合并）
        existing_by_name: dict[str, list[str]] = defaultdict(list)
        if merge:
            for existing_id, node in self.nodes.items():
                existing_name = node.get("name", "")
                if existing_name:
                    existing_by_name[existing_name].append(existing_id)

        # 构建边
        for c in chunks:
            chunk_id = c["chunk_id"]
            deps = c.get("dependencies", [])
            for dep_name in deps:
                # 先在当前批次内查找
                target_ids = list(name_to_ids.get(dep_name, []))
                # 合并模式下也在已有节点中查找
                if merge:
                    target_ids.extend(existing_by_name.get(dep_name, []))
                for target_id in target_ids:
                    if target_id != chunk_id:
                        self.callees[chunk_id].add(target_id)
                        self.callers[target_id].add(chunk_id)

    # ──── query ────

    def get_callers(self, chunk_id: str, max_depth: int = 1) -> list[str]:
        """
        获取调用者列表

        Args:
            chunk_id: 目标节点 ID
            max_depth: 最大遍历深度（1 = 直接调用者）

        Returns:
            caller chunk_id 列表
        """
        if max_depth <= 0:
            return []
        seen: set[str] = set()
        queue = [chunk_id]
        callers: list[str] = []

        for _ in range(max_depth):
            next_queue: list[str] = []
            for node in queue:
                for caller in self.callers.get(node, set()):
                    if caller not in seen:
                        seen.add(caller)
                        callers.append(caller)
                        next_queue.append(caller)
            queue = next_queue

        return callers

    def get_callees(self, chunk_id: str, max_depth: int = 1) -> list[str]:
        """
        获取被调用者列表

        Args:
            chunk_id: 目标节点 ID
            max_depth: 最大遍历深度

        Returns:
            callee chunk_id 列表
        """
        if max_depth <= 0:
            return []
        seen: set[str] = set()
        queue = [chunk_id]
        callees: list[str] = []

        for _ in range(max_depth):
            next_queue: list[str] = []
            for node in queue:
                for callee in self.callees.get(node, set()):
                    if callee not in seen:
                        seen.add(callee)
                        callees.append(callee)
                        next_queue.append(callee)
            queue = next_queue

        return callees

    def get_siblings(self, chunk_id: str,
                     max_siblings: int = 10) -> list[str]:
        """
        获取同文件内的兄弟节点

        Args:
            chunk_id: 目标节点 ID
            max_siblings: 最大返回数量

        Returns:
            同文件内的其他 chunk_id 列表
        """
        node = self.nodes.get(chunk_id)
        if not node:
            return []

        file_path = node.get("file_path", "")
        siblings = self.file_nodes.get(file_path, [])
        # 排除自身，按行号排序
        others = [s for s in siblings if s != chunk_id]
        # 取相邻的节点（按行号距离排序）
        target_line = node.get("line_start", 0)
        others.sort(key=lambda sid: abs(
            self.nodes.get(sid, {}).get("line_start", 0) - target_line
        ))
        return others[:max_siblings]

    def get_all_related(self, chunk_id: str,
                        max_callers: int = 3,
                        max_callees: int = 5,
                        max_siblings: int = 10) -> dict:
        """
        获取节点的完整上下文

        Returns:
            {
                "callers": [...],
                "callees": [...],
                "siblings": [...]
            }
        """
        return {
            "callers": self.get_callers(chunk_id)[:max_callers],
            "callees": self.get_callees(chunk_id)[:max_callees],
            "siblings": self.get_siblings(chunk_id, max_siblings),
        }

    # ──── persistence ────

    def save(self, file_path: str) -> None:
        """持久化依赖图到 JSON"""
        data = {
            "nodes": self.nodes,
            "callers": {k: list(v) for k, v in self.callers.items()},
            "callees": {k: list(v) for k, v in self.callees.items()},
            "file_nodes": self.file_nodes,
        }
        # 转换为可序列化格式
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, file_path: str) -> "DependencyGraph":
        """从 JSON 文件加载依赖图，损坏时返回空图"""
        graph = cls()
        if not os.path.exists(file_path):
            return graph

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError, OSError) as e:
            import warnings
            warnings.warn(f"Failed to load dependency graph from {file_path}: {e}. "
                          "Starting with empty graph. Run /codebase index to rebuild.")
            return graph

        graph.nodes = data.get("nodes", {})
        graph.callers = defaultdict(
            set, {k: set(v) for k, v in data.get("callers", {}).items()}
        )
        graph.callees = defaultdict(
            set, {k: set(v) for k, v in data.get("callees", {}).items()}
        )
        graph.file_nodes = defaultdict(
            list, data.get("file_nodes", {})
        )
        return graph

    def node_count(self) -> int:
        """节点总数"""
        return len(self.nodes)

    def edge_count(self) -> int:
        """边总数"""
        return sum(len(v) for v in self.callees.values())


class GraphRetriever:
    """
    依赖图遍历检索器

    从多路召回结果出发，沿依赖图扩展上下文。
    输入：其他检索器命中的 chunk_id 列表
    输出：扩展后的 SearchResult 列表（包含 caller/callee/sibling）
    """

    def __init__(self, graph: DependencyGraph):
        self.graph = graph

    def expand(self, hits: list[SearchResult],
               max_callers: int = 3,
               max_callees: int = 5,
               max_siblings: int = 10) -> list[SearchResult]:
        """
        对检索命中结果进行图扩展

        Args:
            hits: 其他检索器返回的 SearchResult 列表
            max_callers: 每个 hit 扩展的最大 caller 数
            max_callees: 每个 hit 扩展的最大 callee 数
            max_siblings: 每个 hit 扩展的最大兄弟节点数

        Returns:
            扩展后的 SearchResult 列表（原始 + 扩展）
        """
        expanded: dict[str, SearchResult] = {}
        # 先保留原始结果
        for hit in hits:
            expanded[hit.chunk_id] = hit

        # 扩展每个命中
        for hit in hits:
            context = self.graph.get_all_related(
                hit.chunk_id,
                max_callers=max_callers,
                max_callees=max_callees,
                max_siblings=max_siblings,
            )

            for relation_type, chunk_ids in [
                ("callers", context["callers"]),
                ("callees", context["callees"]),
                ("siblings", context["siblings"]),
            ]:
                for cid in chunk_ids:
                    if cid in expanded:
                        continue
                    node = self.graph.nodes.get(cid, {})
                    expanded[cid] = SearchResult(
                        chunk_id=cid,
                        file_path=node.get("file_path", ""),
                        name=node.get("name", ""),
                        chunk_type=node.get("type", ""),
                        line_start=node.get("line_start", 0),
                        line_end=node.get("line_end", 0),
                        score=hit.score * 0.7,  # 图扩展结果略降权重
                        source=f"graph:{relation_type}",
                        snippet=node.get("source_code", "")[:200],
                        metadata={
                            "parent": node.get("parent", ""),
                            "dependencies": node.get("dependencies", []),
                            "callers": node.get("callers", []),
                            "related_to": hit.chunk_id,
                            "relation": relation_type,
                        }
                    )

        return list(expanded.values())
