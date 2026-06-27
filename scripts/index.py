"""
Index Engine — 代码索引入口

功能：
- 全量索引构建（首次使用 / 手动重建）
- 增量索引更新（hook 触发，仅更新变化的文件）
- 索引状态查询

用法:
    # 全量构建
    python index.py --project /path/to/project

    # 增量更新（hook 触发）
    python index.py --incremental --file src/services/order.py --project /path/to/project

    # 查看状态
    python index.py --status --project /path/to/project
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# 添加 scripts 目录到 path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from chunker import get_chunker, detect_language
from embedder import LocalEmbedder
from retriever.vector import VectorRetriever
from retriever.graph import DependencyGraph


def load_config(project_dir: str) -> dict:
    """加载 config.yaml"""
    config_paths = [
        Path(SCRIPT_DIR).parent / "config.yaml",
        Path(project_dir) / ".codebase-skill.yaml",
    ]
    for p in config_paths:
        if p.exists():
            import yaml
            with open(p, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)

    # 默认配置
    return {
        "index": {
            "extensions": [".py", ".js", ".ts", ".jsx", ".tsx"],
            "exclude_dirs": ["__pycache__", "node_modules", ".git", ".venv",
                            "venv", ".env", "dist", "build", ".code-kb"],
            "exclude_patterns": ["*.min.js", "*.test.*", "*.spec.*"],
            "max_file_size": 1_048_576,
        },
        "storage": {
            "db_path": ".code-kb/lancedb",
            "snapshot_file": ".code-kb/snapshot.json",
            "graph_file": ".code-kb/graph.json",
        },
        "embedding": {
            "batch_size": 32,
        },
    }


def collect_files(project_dir: str, config: dict) -> list[str]:
    """收集项目中所有需要索引的源码文件"""
    idx_config = config.get("index", {})
    extensions = set(idx_config.get("extensions", [".py"]))
    exclude_dirs = set(idx_config.get("exclude_dirs", []))
    exclude_patterns = idx_config.get("exclude_patterns", [])
    max_file_size = idx_config.get("max_file_size", 1_048_576)

    import fnmatch

    files: list[str] = []
    project_path = Path(project_dir).resolve()

    for root, dirs, filenames in os.walk(project_path):
        # 排除目录
        # Skip hidden dirs and explicitly excluded dirs
        dirs[:] = [d for d in dirs if d not in exclude_dirs
                   and not d.startswith(".")]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in extensions:
                continue

            file_path = os.path.join(root, fname)
            rel_path = os.path.relpath(file_path, project_dir)

            # 排除模式检查
            skip = False
            for pattern in exclude_patterns:
                if fnmatch.fnmatch(fname, pattern) or fnmatch.fnmatch(rel_path, pattern):
                    skip = True
                    break
            if skip:
                continue

            # 文件大小检查
            try:
                if os.path.getsize(file_path) > max_file_size:
                    print(f"  [skip] {rel_path} (too large)", file=sys.stderr)
                    continue
            except OSError:
                continue

            files.append(file_path)

    return files


def chunk_file(file_path: str) -> tuple[list[dict], str]:
    """切分单个文件，返回 (chunks, file_hash)

    file_hash 基于原始字节计算，与 incremental_update 的哈希算法一致。
    """
    import hashlib

    language = detect_language(file_path)
    if not language:
        return [], ""

    try:
        chunker = get_chunker(language)
    except ValueError:
        return [], ""

    # 先读原始字节（用于哈希），再解码为文本（用于 AST）
    try:
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
    except Exception:
        return [], ""

    file_hash = hashlib.sha256(raw_bytes).hexdigest()[:16]

    try:
        source_code = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return [], ""

    if not source_code.strip():
        return [], ""

    chunks = chunker.chunk(file_path, source_code)
    return [c.to_dict() for c in chunks], file_hash


def build_index(project_dir: str, config: dict) -> dict:
    """全量构建索引"""
    storage = config.get("storage", {})
    db_path = os.path.join(project_dir, storage.get("db_path", ".code-kb/lancedb"))
    snapshot_file = os.path.join(project_dir, storage.get("snapshot_file", ".code-kb/snapshot.json"))
    graph_file = os.path.join(project_dir, storage.get("graph_file", ".code-kb/graph.json"))
    batch_size = config.get("embedding", {}).get("batch_size", 32)

    # 1. 收集文件
    print("[1/4] Collecting source files...", file=sys.stderr)
    files = collect_files(project_dir, config)
    print(f"  Found {len(files)} files to index", file=sys.stderr)

    if not files:
        print("  No source files found. Check config.yaml extensions.", file=sys.stderr)
        return {"indexed_files": 0, "indexed_chunks": 0}

    # 2. AST 切分
    print("[2/4] Chunking files (AST)...", file=sys.stderr)
    all_chunks: list[dict] = []
    file_hashes: dict[str, str] = {}
    chunked_count = 0

    for file_path in files:
        rel_path = os.path.relpath(file_path, project_dir)
        try:
            chunks, file_hash = chunk_file(file_path)
            if chunks:
                all_chunks.extend(chunks)
                chunked_count += 1
                file_hashes[rel_path] = file_hash
        except Exception as e:
            print(f"  [warn] {rel_path}: {e}", file=sys.stderr)

    print(f"  Chunked {chunked_count}/{len(files)} files → {len(all_chunks)} chunks", file=sys.stderr)

    if not all_chunks:
        print("  No chunks produced. Check file contents.", file=sys.stderr)
        return {"indexed_files": 0, "indexed_chunks": 0}

    # 3. 向量嵌入 + LanceDB
    print("[3/4] Embedding and storing to LanceDB...", file=sys.stderr)
    embed_cache = config.get("embedding", {}).get("cache_dir", ".code-kb/models")
    embedder = LocalEmbedder(
        cache_dir=os.path.join(project_dir, embed_cache),
        batch_size=batch_size,
    )
    retriever = VectorRetriever(db_path=db_path)
    retriever.index(all_chunks, embedder, batch_size=batch_size)

    # 4. 依赖图
    print("[4/4] Building dependency graph...", file=sys.stderr)
    graph = DependencyGraph()
    graph.build_from_chunks(all_chunks)
    graph.save(graph_file)

    # 5. 保存快照
    snapshot = {
        "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_dir": project_dir,
        "indexed_files": chunked_count,
        "indexed_chunks": len(all_chunks),
        "file_hashes": file_hashes,
    }
    os.makedirs(os.path.dirname(snapshot_file), exist_ok=True)
    with open(snapshot_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"\nDone! Indexed {chunked_count} files, {len(all_chunks)} chunks, "
          f"{graph.node_count()} graph nodes, {graph.edge_count()} edges.", file=sys.stderr)

    return {
        "indexed_files": chunked_count,
        "indexed_chunks": len(all_chunks),
        "graph_nodes": graph.node_count(),
        "graph_edges": graph.edge_count(),
    }


def incremental_update(project_dir: str, file_path: str, config: dict) -> dict:
    """增量更新单个文件"""
    # Normalize to absolute path — critical for matching graph nodes and
    # LanceDB records that store absolute paths from build_index.
    file_path = os.path.abspath(file_path)
    storage = config.get("storage", {})
    db_path = os.path.join(project_dir, storage.get("db_path", ".code-kb/lancedb"))
    snapshot_file = os.path.join(project_dir, storage.get("snapshot_file", ".code-kb/snapshot.json"))
    graph_file = os.path.join(project_dir, storage.get("graph_file", ".code-kb/graph.json"))

    rel_path = os.path.relpath(file_path, project_dir)

    # 1. 检查是否需要更新（比较哈希）
    import hashlib
    try:
        with open(file_path, "rb") as f:
            current_hash = hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return {"updated": False, "reason": f"cannot read {rel_path}"}

    # 加载旧快照
    snapshot = None
    if os.path.exists(snapshot_file):
        try:
            with open(snapshot_file, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
            old_hash = snapshot.get("file_hashes", {}).get(rel_path, "")
            if old_hash == current_hash:
                return {"updated": False, "reason": "no change"}
        except (json.JSONDecodeError, IOError) as e:
            print(f"  [warn] snapshot corrupted, forcing re-index: {e}", file=sys.stderr)
            snapshot = None  # 强制重建

    # 2. 删除旧 chunk
    retriever = VectorRetriever(db_path=db_path)
    removed = retriever.delete_chunks(file_path)

    # 3. 重新切分 + 嵌入（append=True 避免覆盖其他文件的数据）
    chunks, _ = chunk_file(file_path)
    if chunks:
        embedder = LocalEmbedder(
            cache_dir=os.path.join(project_dir,
                                   config.get("embedding", {}).get("cache_dir", ".code-kb/models")),
        )
        retriever.index(chunks, embedder, append=True)

    # 4. 更新依赖图
    if os.path.exists(graph_file):
        graph = DependencyGraph.load(graph_file)
        # 删除旧节点并清理相关边
        # 使用绝对路径匹配（build_index 存储绝对路径）
        old_nodes = [nid for nid, node in graph.nodes.items()
                     if os.path.abspath(node.get("file_path", "")) == file_path]
        for nid in old_nodes:
            graph.nodes.pop(nid, None)
            graph.callers.pop(nid, None)
            graph.callees.pop(nid, None)
            # 清理其他节点中指向此节点的残留边
            for caller_set in graph.callees.values():
                caller_set.discard(nid)
            for callee_set in graph.callers.values():
                callee_set.discard(nid)
        # file_nodes 也按绝对路径匹配清理
        for fpath in list(graph.file_nodes.keys()):
            if os.path.abspath(fpath) == file_path:
                graph.file_nodes.pop(fpath, None)
        # 添加新节点（merge=True 使新节点与已有节点建立边）
        if chunks:
            graph.build_from_chunks(chunks, merge=True)
        graph.save(graph_file)

    # 5. 更新快照
    is_new_file = False
    if snapshot is None or not os.path.exists(snapshot_file):
        snapshot = {"file_hashes": {}, "indexed_files": 0, "indexed_chunks": 0}
        is_new_file = True
    else:
        is_new_file = rel_path not in snapshot.get("file_hashes", {})

    snapshot["file_hashes"][rel_path] = current_hash
    snapshot["indexed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 更新计数：net change = new chunks - old chunks removed
    snapshot["indexed_chunks"] = max(0, snapshot.get("indexed_chunks", 0) + len(chunks) - removed)
    if is_new_file and chunks:
        snapshot["indexed_files"] = snapshot.get("indexed_files", 0) + 1
    elif not chunks and not is_new_file:
        # 文件变空，文件数减1
        snapshot["indexed_files"] = max(0, snapshot.get("indexed_files", 0) - 1)

    snapshot_dir = os.path.dirname(snapshot_file)
    if snapshot_dir:
        os.makedirs(snapshot_dir, exist_ok=True)
    with open(snapshot_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    return {
        "updated": True,
        "file": rel_path,
        "chunks_added": len(chunks),
        "chunks_removed": removed,
    }


def show_status(project_dir: str, config: dict) -> dict:
    """显示索引状态"""
    storage = config.get("storage", {})
    snapshot_file = os.path.join(project_dir, storage.get("snapshot_file", ".code-kb/snapshot.json"))
    graph_file = os.path.join(project_dir, storage.get("graph_file", ".code-kb/graph.json"))
    db_path = os.path.join(project_dir, storage.get("db_path", ".code-kb/lancedb"))

    status = {
        "project": project_dir,
        "indexed": False,
        "indexed_at": None,
        "indexed_files": 0,
        "indexed_chunks": 0,
    }

    if os.path.exists(snapshot_file):
        try:
            with open(snapshot_file, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
            status["indexed"] = True
            status["indexed_at"] = snapshot.get("indexed_at")
            status["indexed_files"] = snapshot.get("indexed_files", 0)
            status["indexed_chunks"] = snapshot.get("indexed_chunks", 0)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  [warn] snapshot corrupted: {e}", file=sys.stderr)

    if os.path.exists(graph_file):
        graph = DependencyGraph.load(graph_file)
        status["graph_nodes"] = graph.node_count()
        status["graph_edges"] = graph.edge_count()

    # 检查 LanceDB
    retriever = VectorRetriever(db_path=db_path)
    status["vector_count"] = retriever.count()

    return status


# ──── CLI ────

def main():
    parser = argparse.ArgumentParser(description="Codebase Skill — Index Engine")
    parser.add_argument("--project", "-p", default=os.getcwd(),
                       help="项目根目录 (默认: 当前目录)")
    parser.add_argument("--incremental", action="store_true",
                       help="增量更新模式")
    parser.add_argument("--file", "-f", help="增量更新：变更的文件路径")
    parser.add_argument("--status", action="store_true",
                       help="显示索引状态")
    parser.add_argument("--json", action="store_true",
                       help="以 JSON 格式输出")
    args = parser.parse_args()

    config = load_config(args.project)

    if args.status:
        result = show_status(args.project, config)
    elif args.incremental and args.file:
        result = incremental_update(args.project, args.file, config)
    else:
        result = build_index(args.project, config)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
