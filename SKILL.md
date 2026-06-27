# Codebase Skill

> 让 Claude Code 在大型项目中精确定位代码 — 索引、检索、改写三步闭环

## 触发命令

| 命令 | 功能 |
|------|------|
| `/codebase index` | 全量构建项目代码索引 |
| `/codebase search <查询>` | 用大白话检索代码（自动改写为精确查询） |
| `/codebase status` | 查看索引状态（文件数、chunk 数、向量数、图节点数） |
| `/codebase reindex` | 清空索引并重建 |

## 使用方式

### 第一步：构建索引

```
/codebase index
```

扫描项目所有源码文件 → AST 切分（函数/类/方法/变量/类型注解）→ 向量嵌入 → 存入 LanceDB → 构建依赖图。100 文件以内 < 30 秒。

### 第二步：检索代码

```
/codebase search 支付回调偶发空指针异常
```

Skill 自动完成三步：

1. **改写** — 将大白话翻译为精确技术查询，语义距离大的先让你确认
2. **多路召回** — BM25 关键词 + 语义向量 + 依赖图，三路并行
3. **RRF 融合** — 分数归一化 + 去重 + 排序 → Top-5 结果

### 第三步（可选）：配置自动增量更新

将 `hooks/post-edit.json` 的内容合并到项目 `.claude/settings.json`，之后每次 Edit/Write 操作自动更新变化文件的索引，无需手动重建。

### 查看状态

```
/codebase status
```

输出当前索引的项目、文件数、chunk 总数、图节点数、边数、向量数。

## 检索结果格式

每次 `/codebase search` 返回如下结构：

```json
{
  "query": "支付回调偶发空指针异常",
  "rewritten": "handle_payment_callback 中未捕获异常导致 NullPointerException",
  "results": [
    {
      "rank": 1,
      "score": 0.92,
      "chunk_id": "src/services/payment.py:handle_payment_callback:74-94",
      "file_path": "src/services/payment.py",
      "line_start": 74,
      "line_end": 94,
      "name": "handle_payment_callback",
      "chunk_type": "function",
      "parent": "",
      "snippet": "def handle_payment_callback(payload: dict) -> bool:\n    ...",
      "source": "vector",
      "metadata": {
        "context": {
          "callers": [
            {"name": "payment_callback", "file_path": "src/api/payment_api.py", "line_start": 46, "line_end": 52, "type": "function"}
          ],
          "callees": [
            {"name": "process_payment_success", "file_path": "src/services/payment.py", "line_start": 97, "line_end": 120, "type": "function"},
            {"name": "process_refund", "file_path": "src/services/payment.py", "line_start": 123, "line_end": 150, "type": "function"}
          ],
          "sibling_functions": [
            {"name": "verify_payment_sign", "file_path": "src/services/payment.py", "line_start": 153, "line_end": 159, "type": "function"}
          ]
        }
      }
    }
  ],
  "count": 5
}
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `query` | 原始查询文本 |
| `rewritten` | 改写后的精确查询（如果跳过了改写则与原查询相同） |
| `needs_confirmation` | 是否需要用户确认（仅在语义距离大时出现） |
| `results[].score` | RRF 融合后的归一化分数 |
| `results[].source` | 命中来源 — `bm25` / `vector` / `graph:callers` / `graph:callees` / `graph:siblings` |
| `results[].chunk_type` | 代码单元类型 — `function` / `method` / `class` / `variable` / `interface` |
| `results[].metadata.context` | 调用上下文：谁调了它 / 它调了谁 / 同文件兄弟函数 |

## 容错机制

Skill 按"逐层降级"设计，部分依赖缺失不影响基本使用：

| 依赖 | 作用 | 缺失时行为 |
|------|------|-----------|
| LanceDB | 向量 ANN 检索 | 降级为 JSON 索引 + 暴力余弦搜索 |
| ONNX 模型 | 语义向量嵌入 | 降级为 n-gram 哈希伪向量（确定性） |
| Anthropic SDK | 查询改写 | 跳过改写，原始查询直接检索 |
| rank-bm25 | BM25 关键词检索 | 降级为内置 TF-IDF |
| 索引文件损坏 | — | 自动检测并重建，不崩溃 |

## 增量更新原理

```
Edit/Write 文件
      │
      ▼
PostToolUse Hook 触发
      │
      ▼
SHA256 内容哈希比较 ──→ 未变化 → 跳过
      │
      ▼ 已变化
① delete_chunks(file_path) ──→ 删除旧 chunk（LanceDB + JSON 双路径）
② chunk_file(file_path)    ──→ AST 重新切分
③ index(chunks, append=True) ──→ 追加新 chunk（不覆盖其他文件数据）
④ build_from_chunks(merge=True) ──→ 更新依赖图（新节点与已有节点建立边）
⑤ 更新快照（文件哈希 + 计数器）
```

## 配置

所有参数在 `config.yaml` 中可调，也支持项目级覆盖文件 `.codebase-skill.yaml`：

```yaml
# 索引范围
index:
  extensions: [".py", ".js", ".ts", ".jsx", ".tsx"]
  exclude_dirs: ["__pycache__", "node_modules", ".git", ".venv", "dist", "build", ".code-kb"]

# 检索参数
retrieval:
  top_k: 5                # 返回结果数
  rrf_k: 60               # RRF 融合常数

# 查询改写
rewrite:
  model: "claude-haiku-4-5-20251001"
  confirmation:
    small_distance_threshold: 0.3   # 低于此值跳过确认
    large_distance_threshold: 0.7   # 高于此值强制确认
```

## 项目位置

```
<你的安装目录>/codebase-skills/
```

完整设计规格见 `docs/specs/2026-06-27-codebase-skill-design.md`
