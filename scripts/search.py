"""
Search Engine — 检索引擎入口

完整检索管线：
1. Query Rewriter — 大白话 → 精确查询（含确认 Gate）
2. 多路召回 — BM25 + 语义向量 + 依赖图
3. RRF 融合 + 去重
4. 上下文扩展

用法:
    python search.py --query "支付回调偶发空指针" --project /path/to/project
    python search.py --query "用户登录逻辑在哪" --project . --top-k 5
    python search.py --query "重构订单模块" --project . --skip-rewrite
"""

import argparse
import json
import os
import sys
from pathlib import Path

# 添加 scripts 目录到 path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from rewrite import QueryRewriter, RewriteResult
from embedder import LocalEmbedder
from retriever.bm25 import BM25Retriever
from retriever.vector import VectorRetriever
from retriever.graph import DependencyGraph, GraphRetriever
from retriever.fusion import RRFusion, SearchResult


def load_config(project_dir: str) -> dict:
    """加载 config.yaml"""
    config_paths = [
        SCRIPT_DIR.parent / "config.yaml",
        Path(project_dir) / ".codebase-skill.yaml",
    ]
    for p in config_paths:
        if p.exists():
            import yaml
            with open(p, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)

    return {
        "retrieval": {
            "top_k": 5,
            "candidate_multiplier": 2,
            "rrf_k": 60,
            "context": {
                "max_callers": 3,
                "max_callees": 5,
                "max_siblings": 10,
            }
        },
        "storage": {
            "db_path": ".code-kb/lancedb",
            "snapshot_file": ".code-kb/snapshot.json",
            "graph_file": ".code-kb/graph.json",
        },
        "rewrite": {
            "model": "claude-haiku-4-5-20251001",
            "confirmation": {
                "small_distance_threshold": 0.3,
                "large_distance_threshold": 0.7,
            }
        }
    }


def load_chunks_from_index(project_dir: str, config: dict) -> list[dict]:
    """
    从现有索引中加载所有 chunk 元数据

    这是为了初始化 BM25Retriever。优先从 LanceDB 加载，
    LanceDB 不可用时降级为 JSON 索引。
    """
    storage = config.get("storage", {})
    db_path = os.path.join(project_dir, storage.get("db_path", ".code-kb/lancedb"))

    # 优先尝试 LanceDB（主存储）
    try:
        import lancedb
        db = lancedb.connect(db_path)
        table = db.open_table("code_chunks")
        records = table.to_pandas().to_dict("records")
        result = [{k: v for k, v in r.items() if k != "vector"} for r in records]
        if result:
            return result
    except Exception:
        pass

    # 降级为 JSON 索引
    json_index = Path(db_path) / "json_index" / "index.json"
    if json_index.exists():
        try:
            with open(json_index, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 移除向量列（不需要）
                result = [{k: v for k, v in item.items() if k != "vector"} for item in data]
                if result:
                    return result
        except (json.JSONDecodeError, IOError) as e:
            print(f"  [warn] JSON index corrupted: {e}", file=sys.stderr)

    return []


def search(query: str, project_dir: str, config: dict,
           skip_rewrite: bool = False,
           confirm_callback=None) -> dict:
    """
    执行完整检索管线

    Args:
        query: 用户查询
        project_dir: 项目目录
        config: 配置
        skip_rewrite: 跳过 Query Rewrite 步骤
        confirm_callback: 确认回调函数 (result: RewriteResult) → bool
                         返回 True 表示用户确认，False 表示取消

    Returns:
        检索结果 dict
    """
    retrieval_config = config.get("retrieval", {})
    storage = config.get("storage", {})
    rewrite_config = config.get("rewrite", {})

    top_k = retrieval_config.get("top_k", 5)
    candidate_multiplier = retrieval_config.get("candidate_multiplier", 2)
    rrf_k = retrieval_config.get("rrf_k", 60)
    ctx_config = retrieval_config.get("context", {})
    max_callers = ctx_config.get("max_callers", 3)
    max_callees = ctx_config.get("max_callees", 5)
    max_siblings = ctx_config.get("max_siblings", 10)

    db_path = os.path.join(project_dir, storage.get("db_path", ".code-kb/lancedb"))
    graph_file = os.path.join(project_dir, storage.get("graph_file", ".code-kb/graph.json"))
    snapshot_file = os.path.join(project_dir, storage.get("snapshot_file", ".code-kb/snapshot.json"))

    # ──── Step 0: 检查索引是否存在 ────
    if not os.path.exists(snapshot_file):
        return {
            "error": "no_index",
            "message": "索引不存在。请先运行: /codebase index",
            "results": [],
        }

    rewritten = query
    needs_confirmation = False

    # ──── Step 1: Query Rewrite ────
    if not skip_rewrite:
        try:
            rewriter = QueryRewriter(
                model=rewrite_config.get("model", "claude-haiku-4-5-20251001"),
                small_threshold=rewrite_config.get("confirmation", {}).get(
                    "small_distance_threshold", 0.3),
                large_threshold=rewrite_config.get("confirmation", {}).get(
                    "large_distance_threshold", 0.7),
            )
            rewrite_result = rewriter.rewrite(query)

            if rewrite_result.needs_confirmation:
                needs_confirmation = True
                if confirm_callback:
                    if not confirm_callback(rewrite_result):
                        return {
                            "query": query,
                            "rewritten": rewrite_result.rewritten,
                            "cancelled": True,
                            "message": "用户取消了检索",
                            "results": [],
                        }
                else:
                    # 非交互模式：返回改写结果并要求确认
                    return {
                        "query": query,
                        "rewritten": rewrite_result.rewritten,
                        "intent": rewrite_result.intent.value,
                        "entities": rewrite_result.entities,
                        "uncertainties": rewrite_result.uncertainties,
                        "needs_confirmation": True,
                        "semantic_distance": rewrite_result.semantic_distance,
                        "results": [],
                    }
            rewritten = rewrite_result.rewritten
        except Exception as e:
            # Query Rewrite 不可用（如没有 Anthropic SDK），原样使用查询
            print(f"[search] Query Rewrite skipped: {e}", file=sys.stderr)

    # ──── Step 2: 加载索引 ────
    all_chunks = load_chunks_from_index(project_dir, config)

    if not all_chunks:
        return {
            "error": "empty_index",
            "message": "索引为空。请运行: /codebase reindex",
            "results": [],
        }

    # ──── Step 3: 多路检索 ────

    # 3a. BM25
    bm25 = BM25Retriever()
    bm25.index(all_chunks)
    bm25_results = bm25.search(rewritten, top_k=top_k * candidate_multiplier)

    # 3b. 向量检索
    embed_cache = config.get("embedding", {}).get("cache_dir", ".code-kb/models")
    embedder = LocalEmbedder(
        cache_dir=os.path.join(project_dir, embed_cache),
    )
    vector = VectorRetriever(db_path=db_path)
    vector_results = vector.search(rewritten, embedder,
                                   top_k=top_k * candidate_multiplier)

    # 3c. 依赖图
    graph = DependencyGraph.load(graph_file)
    graph_retriever = GraphRetriever(graph)
    # 先用前两路的命中结果进行图扩展
    preliminary_hits = bm25_results[:top_k] + vector_results[:top_k]
    graph_results = graph_retriever.expand(
        preliminary_hits,
        max_callers=max_callers,
        max_callees=max_callees,
        max_siblings=max_siblings,
    )

    # ──── Step 4: RRF 融合 ────
    fusion = RRFusion(k=rrf_k)
    fused = fusion.fuse_with_dedup(
        [bm25_results, vector_results, graph_results],
        top_k=top_k,
    )

    # ──── Step 5: 添加上下文 ────
    for result in fused:
        context = graph.get_all_related(
            result.chunk_id,
            max_callers=max_callers,
            max_callees=max_callees,
            max_siblings=max_siblings,
        )
        result.metadata["context"] = {
            "callers": [
                _make_context_ref(cid, graph) for cid in context["callers"]
            ],
            "callees": [
                _make_context_ref(cid, graph) for cid in context["callees"]
            ],
            "sibling_functions": [
                _make_context_ref(cid, graph) for cid in context["siblings"]
            ],
        }

    return {
        "query": query,
        "rewritten": rewritten,
        "results": [r.to_dict() for r in fused],
        "count": len(fused),
    }


def _make_context_ref(chunk_id: str, graph: DependencyGraph) -> dict:
    """为上下文节点创建简短引用"""
    node = graph.nodes.get(chunk_id, {})
    return {
        "name": node.get("name", chunk_id),
        "file_path": node.get("file_path", ""),
        "line_start": node.get("line_start", 0),
        "line_end": node.get("line_end", 0),
        "type": node.get("type", ""),
    }


# ──── CLI ────

def main():
    parser = argparse.ArgumentParser(description="Codebase Skill — Search Engine")
    parser.add_argument("--query", "-q", required=True, nargs="+",
                       help="查询文本")
    parser.add_argument("--project", "-p", default=os.getcwd(),
                       help="项目根目录 (默认: 当前目录)")
    parser.add_argument("--top-k", "-k", type=int, default=5,
                       help="返回结果数 (默认: 5)")
    parser.add_argument("--skip-rewrite", action="store_true",
                       help="跳过 Query Rewrite 步骤")
    parser.add_argument("--json", action="store_true",
                       help="以 JSON 格式输出")
    parser.add_argument("--interactive", "-i", action="store_true",
                       help="交互模式（含确认提示）")
    args = parser.parse_args()

    query = " ".join(args.query)
    config = load_config(args.project)

    # 覆盖 top_k
    if "retrieval" not in config:
        config["retrieval"] = {}
    config["retrieval"]["top_k"] = args.top_k

    # 确认回调（交互模式）
    confirm_callback = None
    if args.interactive:
        def _confirm(result: RewriteResult) -> bool:
            print("\n" + "=" * 60)
            print("Query Rewrite 确认")
            print("=" * 60)
            print(f"原始查询: {result.original}")
            print(f"改写查询: {result.rewritten}")
            print(f"意图: {result.intent.value}")
            if result.entities:
                print(f"实体: {', '.join(result.entities)}")
            if result.uncertainties:
                print(f"不确定: {', '.join(result.uncertainties)}")
            print(f"语义距离: {result.semantic_distance:.2f}")
            print("-" * 60)
            resp = input("确认检索? [Y/n] ").strip().lower()
            return resp in ("", "y", "yes")
        confirm_callback = _confirm

    result = search(
        query=query,
        project_dir=args.project,
        config=config,
        skip_rewrite=args.skip_rewrite,
        confirm_callback=confirm_callback,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_pretty(result)


def _print_pretty(result: dict) -> None:
    """美化输出检索结果"""
    print()
    if "error" in result:
        print(f"[ERROR] {result.get('message', result['error'])}")
        return

    if result.get("cancelled"):
        print(f"[CANCELLED] {result.get('message', '')}")
        return

    if result.get("needs_confirmation"):
        print("⚠ 需要确认查询改写：")
        print(f"  原始: {result['query']}")
        print(f"  改写: {result['rewritten']}")
        print(f"  意图: {result.get('intent', 'unknown')}")
        print(f"  语义距离: {result.get('semantic_distance', 'N/A')}")
        if result.get("uncertainties"):
            print(f"  不确定: {', '.join(result['uncertainties'])}")
        print()
        print("  使用 --interactive 进入交互确认模式")
        return

    print(f"查询: {result.get('query', '')}")
    rewritten = result.get('rewritten')
    if rewritten and rewritten != result.get('query'):
        print(f"改写: {rewritten}")
    print(f"结果: {result.get('count', 0)} 条")
    print()

    for i, r in enumerate(result.get("results", []), 1):
        print(f"{'─' * 60}")
        print(f"[{i}] {r['name']} ({r['chunk_type']}) — score: {r['score']}")
        print(f"    位置: {r['file_path']}:{r['line_start']}-{r['line_end']}")
        print(f"    来源: {r['source']}")
        if r.get('snippet'):
            snippet = r['snippet'].replace('\n', '\n    ')
            print(f"    代码:\n    {snippet[:300]}")

        ctx = r.get('metadata', {}).get('context', {})
        if ctx.get('callers'):
            names = [c['name'] for c in ctx['callers']]
            print(f"    调用者: {', '.join(names)}")
        if ctx.get('callees'):
            names = [c['name'] for c in ctx['callees']]
            print(f"    被调用: {', '.join(names)}")
    print(f"{'─' * 60}")


if __name__ == "__main__":
    main()
