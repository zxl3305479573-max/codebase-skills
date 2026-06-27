# Codebase Skill — 设计规格

> 日期：2026-06-27  
> 状态：待审核  
> 作者：Sun

---

## 1. 概述

### 1.1 定位

`codebase` 是一个 Claude Code skill，面向个人开发者。解决的核心问题是：**当项目代码量超过人脑记忆范围时，帮助 Claude 精确定位需要修改的代码**。

skill 只做三件事：
- **索引**：将项目代码按 AST 结构切分、向量化、建依赖图
- **检索**：多路召回（BM25 + 语义向量 + 依赖图）+ RRF 融合
- **改写**：将用户的大白话翻译为精确的技术查询，防跑偏

代码修改、测试、git 操作复用 Claude Code 自身能力。

### 1.2 使用场景

| 场景 | 输入 | 输出 |
|------|------|------|
| 修复 bug | "支付回调偶发空指针" | `handle_payment` 函数 + 相关调用链 |
| 定位功能 | "用户登录逻辑在哪" | `auth/login.py` 中相关函数列表 |
| 理解影响面 | "改了 `calculate_discount` 会影响哪些地方" | 所有 caller 列表 |
| 重构导航 | "把订单模块的异常处理统一改掉" | 所有订单模块中 try/except 块 |

---

## 2. 架构设计

### 2.1 系统分层

```
┌──────────────────────────────────────────────────┐
│  Claude Code 层（已有，不开发）                    │
│  Read / Edit / Bash / Grep / Glob                │
└──────────────────┬───────────────────────────────┘
                   │ 调用 skill，接收检索结果
┌──────────────────┴───────────────────────────────┐
│  codebase skill 层（本项目）                       │
│                                                   │
│  ┌─────────────┐ ┌─────────────┐ ┌────────────┐  │
│  │ Query       │ │ 检索引擎     │ │ 索引引擎    │  │
│  │ Rewriter    │ │             │ │             │  │
│  │             │ │ BM25 关键词  │ │ AST 切分    │  │
│  │ 实体对齐    │ │ 语义向量     │ │ 向量嵌入    │  │
│  │ 意图分类    │ │ 依赖图遍历   │ │ 依赖图构建  │  │
│  │ 约束改写    │ │ RRF 融合     │ │ 增量更新    │  │
│  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘  │
│         │               │               │         │
│         └───────────────┼───────────────┘         │
│                         ▼                          │
│         ┌─────────────────────────────┐           │
│         │  LanceDB 嵌入式向量数据库     │           │
│         │  .code-kb/ 项目级存储         │           │
│         └─────────────────────────────┘           │
└──────────────────────────────────────────────────┘
```

### 2.2 项目文件结构

```
<你的安装目录>/codebase-skills/
├── SKILL.md                    # skill 入口指令（Claude Code 加载）
├── scripts/
│   ├── index.py                # 索引入口（全量构建 / 增量更新）
│   ├── search.py               # 检索入口（多路召回 + RRF 融合）
│   ├── rewrite.py              # Query 改写（约束式）
│   ├── chunker/
│   │   ├── __init__.py
│   │   ├── base.py             # 抽象切分器
│   │   ├── python.py           # Python AST 切分（ast 标准库）
│   │   ├── javascript.py       # JS/TS tree-sitter 切分
│   │   └── ...                 # 按需扩展语言
│   ├── retriever/
│   │   ├── __init__.py
│   │   ├── bm25.py             # BM25 关键词检索
│   │   ├── vector.py           # 向量语义检索（LanceDB）
│   │   ├── graph.py            # 依赖图遍历检索
│   │   └── fusion.py           # RRF 融合 + 去重
│   └── embedder/
│       ├── __init__.py
│       └── local.py            # 本地 embedding 模型（ONNX）
├── hooks/
│   └── post-edit.json          # PostToolUse hook 配置模板
├── config.yaml                 # 可配置项
├── requirements.txt
├── docs/
│   └── specs/
│       └── 2026-06-27-codebase-skill-design.md
└── README.md
```

### 2.3 数据流

```
用户输入大白话
      │
      ▼
┌──────────────┐    语义距离大 → 用户确认
│ Query        │ ──→ "我理解你要做：【xxx】，对吗？"
│ Rewriter     │    语义距离小 → 跳过确认，直接检索
└──────┬───────┘
       │ 改写后的精确查询
       ▼
┌──────────────┐
│ 多路检索      │
│              │
│ BM25 ────────┤  并行执行
│ 向量 ────────┤
│ 依赖图 ──────┤
└──────┬───────┘
       │ 三路结果
       ▼
┌──────────────┐
│ RRF 融合     │  分数归一化 + 去重 + 排序
└──────┬───────┘
       │ Top-K 结果
       ▼
┌──────────────┐
│ 上下文扩展    │  拉入 caller/callee/同文件相邻函数
└──────┬───────┘
       │
       ▼
  返回 Claude Code
  （file_path + line_range + 代码片段 + 相关性分数）
```

---

## 3. 组件详细设计

### 3.1 索引引擎

#### 3.1.1 AST 切分

**原则**：每个 chunk 必须是一个完整的语法单元，禁止拦腰截断。

| 语言 | 切分工具 | 切分单元 |
|------|---------|---------|
| Python | `ast` 标准库（零额外依赖） | 函数、类、方法、模块级变量 |
| JavaScript/TypeScript | tree-sitter | 函数、类、方法、箭头函数 |
| Go | tree-sitter | 函数、方法、结构体 |
| Java | tree-sitter | 方法、类、接口 |
| Rust | tree-sitter | 函数、impl 块、结构体 |

#### 3.1.2 元数据记录

每个 chunk 随附：

```json
{
  "chunk_id": "src.services.order.OrderService.process_order:15-42",
  "file_path": "src/services/order.py",
  "language": "python",
  "type": "method",
  "name": "process_order",
  "parent": "OrderService",
  "line_start": 15,
  "line_end": 42,
  "dependencies": ["get_items", "calculate_total"],
  "callers": ["api.orders.create_order"],
  "docstring": "处理订单的核心流程，包含库存校验和支付",
  "content_hash": "sha256..."
}
```

- `dependencies`：它调用了谁（callee）
- `callers`：谁调用了它
- `content_hash`：用于增量更新检测

#### 3.1.3 向量嵌入

- 模型：`BAAI/bge-small-en-v1.5`（384维，ONNX Runtime 本地推理）
- 嵌入对象：函数签名 + docstring + 代码前 10 行摘要
- 批处理：默认 batch_size=32

#### 3.1.4 增量更新

通过 Claude Code hook 触发。当 `Edit` 或 `Write` 工具操作源码文件后：

1. 计算被修改文件的 content hash
2. 与索引快照中的 hash 对比
3. 仅对有变化的文件：重新切分 → 嵌入 → 更新 LanceDB
4. 更新依赖图（受影响文件的 caller/callee 关系）

#### 3.1.5 全量构建

首次调用 `/codebase index` 时：
1. 扫描项目目录，收集所有支持的源码文件
2. 并行 AST 切分
3. 批量嵌入 + 写入 LanceDB
4. 构建全局依赖图
5. 记录索引快照（文件路径 → hash 映射）

### 3.2 检索引擎

#### 3.2.1 BM25 关键词检索

- 检索字段：函数名、类名、变量名、文件路径、docstring
- 实现：`rank_bm25` 库
- 权重策略：精确匹配函数名 > 部分匹配 > docstring 匹配

#### 3.2.2 语义向量检索

- 将查询文本通过同一 embedding 模型向量化
- 在 LanceDB 中做 ANN（近似最近邻）搜索
- 返回 top_k × 2 候选（扩大候选池供 RRF 融合）

#### 3.2.3 依赖图遍历

当检索命中某函数时：
- **上行扩展**：返回所有 callers（谁调用了它）
- **下行扩展**：返回所有 callees（它调了谁）
- **同级扩展**：同一文件内的相邻函数/类

这样 Claude 拿到的不只是一个孤立函数，而是完整的调用上下文。

#### 3.2.4 RRF 融合

复用在 RAG_AGENT 项目中已验证过的 RRF 实现：

```
RRF_score(doc) = Σ 1 / (k + rank_i(doc))
# k=60, rank_i 是文档在第 i 路检索中的排名
```

三路分别排序 → RRF 融合 → 去重 → Top-K（默认 K=5）

#### 3.2.5 上下文扩展

RRF 排序后的每个结果，自动补充：
- 该函数所在文件的同级函数列表
- 直接 caller（最多 3 个）
- 直接 callee（最多 5 个）

### 3.3 Query Rewriter

#### 3.3.1 设计目标

将用户的大白话翻译为精确的技术查询。核心约束：**只做等价替换，禁止脑补**。

#### 3.3.2 系统 Prompt

```
你是代码查询改写器。将用户的非正式描述改写为精确的技术查询。

强制规则：
1. 只做等价替换 — 口语词 → 代码实体名，模糊描述 → 工程术语
2. 禁止推断 — 用户没说"高并发"，不能加"高并发"
3. 禁止扩大范围 — 用户说"改登录"，不能写成"重构认证系统"
4. 不确定时保持原样 — 宁可保留模糊，也不脑补确定

输出 JSON：
{
  "rewritten": "<改写后的精确查询>",
  "intent": "bug|feature|refactor|perf|unsure",
  "entities": ["<对齐到的函数名/类名/文件名>"],
  "uncertainties": ["<因不确定而保持原样的部分>"]
}
```

#### 3.3.3 语义距离判断

改写前后比较，决定是否需要用户确认：

| 改写类型 | 语义距离 | 是否确认 |
|---------|---------|:---:|
| 纯实体替换（"支付"→"handle_payment"） | 小 | 跳过 |
| 补充了结构信息（加了调用链） | 中 | 确认 |
| 意图模糊无法归类 | 大 | 确认 + 追问 |

#### 3.3.4 确认输出格式

```
我理解你要：
  【bug 修复】handle_payment 函数中存在未处理异常导致偶发空指针
  涉及文件：src/services/order.py, src/api/payment.py
  未确定的部分：无

→ 按 Enter 确认，或修改描述后重新检索
```

---

## 4. 触发机制

### 4.1 Hook 自动增量索引

项目 `.claude/settings.json` 中配置：

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{
          "type": "command",
          "command": "python \"<PATH-TO>/codebase-skills/scripts/index.py\" --incremental --file \"$CLAUDE_FILE_PATH\" --project \"$CLAUDE_PROJECT_DIR\""
        }]
      }
    ]
  }
}
```

### 4.2 Skill 入口命令

| 命令 | 功能 |
|------|------|
| `/codebase index` | 全量构建索引 |
| `/codebase search <query>` | 检索相关代码（含 query rewrite） |
| `/codebase status` | 查看索引状态（已索引文件数、最后更新） |
| `/codebase reindex` | 清空索引并重建 |

### 4.3 兜底逻辑

当 `/codebase search` 被调用时：
1. 检查 `.code-kb/` 是否存在且非空
2. 不存在 → 自动执行全量索引，然后检索
3. 存在但过期 → 先增量更新，再检索
4. 存在且最新 → 直接检索

---

## 5. 技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| AST 解析 | tree-sitter + Python `ast` | 10+ 语言支持，Python 零额外依赖 |
| 向量嵌入 | BAAI/bge-small-en-v1.5 (384D) | 轻量级，ONNX 本地推理，支持中英双语 |
| 向量数据库 | LanceDB | 嵌入式，零部署，Python 原生 API |
| BM25 | rank-bm25 | 纯 Python，轻量 |
| Query 改写 | Claude API (Haiku) | 改写不需要复杂推理，最快最省 |
| 配置管理 | PyYAML | 标准方案 |
| Python | ≥3.10 | 兼容性优先 |

### 依赖汇总 (requirements.txt)

```
lancedb>=0.12
tree-sitter>=0.21
rank-bm25>=0.2
optimum-onnx>=1.15
onnxruntime>=1.18
pyyaml>=6.0
anthropic>=0.30
```

---

## 6. MVP 范围

### 6.1 包含 ✅

- Python 代码 AST 切分（`ast` 标准库）
- JavaScript/TypeScript 代码 AST 切分（tree-sitter）
- 语义向量检索（LanceDB + bge-small）
- BM25 关键词检索
- 依赖图构建与遍历（静态 import/call 分析）
- RRF 多路融合
- 约束式 Query Rewriter
- 用户确认 Gate（语义距离判断）
- Hook 驱动增量索引
- 全量索引 + 兜底自动构建
- 上下文扩展（caller/callee/同级函数）

### 6.2 不做 ❌（MVP 范围外）

- Go/Java/Rust/C++ 等语言 AST 切分（保留扩展点）
- 多用户/团队协作
- Web UI
- 多项目联合检索
- 自动代码修复（Claude Code 做）
- 代码质量分析 / 测试生成
- Docker 部署（全部本地运行）

---

## 7. 成功标准

1. **检索准确率**：对"修复某功能 bug"类查询，Top-3 结果中至少包含目标函数
2. **索引速度**：100 文件以内的项目，首次全量索引 < 30 秒
3. **检索速度**：从输入查询到返回结果 < 5 秒
4. **改写保真度**：Query Rewriter 输出被用户一次确认通过率 ≥ 80%
5. **零部署**：`pip install -r requirements.txt` + 配置 hook 即可使用

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Embedding 模型对中文查询效果差 | 检索不准 | bge-small 支持中英双语；降级为纯 BM25 |
| 大型项目（1000+ 文件）索引慢 | 首次体验差 | 增量索引 + 并行切分 + 进度提示 |
| tree-sitter 对某些语言解析不稳定 | 切分失败 | 降级为固定长度切割 + 语法边界检测 |
| Query Rewriter 改写出错 | 检索方向错误 | 确认 Gate + uncertainties 字段暴露不确定点 |
| LanceDB 大项目存储膨胀 | 磁盘占用大 | 定期清理旧索引 + 限制 chunk 大小上限 |

---

## 附录 A：参考项目

| 项目 | 借鉴点 |
|------|--------|
| [agent-brain](https://github.com/spillwavesolutions/agent-brain) | AST 切分 + GraphRAG + 多模式检索 |
| [CTX-Retriever](https://pypi.org/project/ctx-retriever/) | 触发类型路由 + 依赖图遍历 |
| [knowledge-rag](https://www.npmjs.com/package/knowledge-rag) | 混合检索 + ONNX 本地推理 |
| [memory-search](https://github.com/rjyo/memory-search) | Claude Code skill 打包模式 |
| RAG_AGENT（学习项目） | RRF 融合实现 + ChatEngine 架构 |
