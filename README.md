# Codebase Skill

> 让 Claude Code 在大型项目中精确定位代码 — 索引、检索、改写三步闭环。

## 解决的问题

当项目代码量超过人脑记忆范围时，找到需要修改的代码成为最大瓶颈。`grep` 太脆弱（变量名叫 `txn` 而不是 `transaction` 就搜不到），靠翻目录太慢。这个 Skill 用 AST 切分 + 向量嵌入 + 多路检索，把"大白话描述"直接映射到"具体代码位置"。

## 解决思路

```
大白话描述 (如"支付回调偶发空指针")
        │
        ▼
  ① 查询改写 ──→ 语义距离判断 ──→ 用户确认门控
        │
        ▼
  ② 多路检索
     ├── BM25 关键词匹配
     ├── 语义向量检索 (LanceDB + BGE-small)
     └── 依赖图遍历 (调用者/被调用者)
        │
        ▼
  ③ RRF 融合 → 去重 → Top-K → 上下文扩展
        │
        ▼
  返回代码位置 + 片段 + 调用链 → Claude Code
```

## 快速开始

```bash
# 1. 一键安装（依赖 + 模型下载）
python scripts/install.py

# 2. 在你的项目目录中构建索引
python scripts/index.py --project /path/to/your/project

# 3. 用大白话检索代码
python scripts/search.py --query "支付回调偶发空指针异常" --project /path/to/your/project

# 4. (可选) 配置 Hook 实现自动增量索引
#    将 hooks/post-edit.json 的内容合并到项目的 .claude/settings.json
#    （记得把脚本路径改成你的实际安装位置）
```

## Claude Code 命令

| 命令 | 功能 |
|------|------|
| `/codebase index` | 全量构建项目代码索引 |
| `/codebase search <query>` | 用大白话检索代码（自动改写为精确查询） |
| `/codebase status` | 查看索引统计（文件数、chunk 数、向量数） |
| `/codebase reindex` | 清空索引并重建 |

## 架构

```
┌──────────────────────────────────────────────────┐
│  Claude Code 层（已有，本 Skill 不开发）           │
│  Read / Edit / Bash / Grep / Glob                │
└──────────────────┬───────────────────────────────┘
                   │
┌──────────────────┴───────────────────────────────┐
│  codebase skill 层（本项目）                       │
│                                                   │
│  ┌──────────────┐ ┌──────────────┐ ┌───────────┐ │
│  │ Query        │ │ 检索引擎      │ │ 索引引擎   │ │
│  │ Rewriter     │ │              │ │            │ │
│  │ 大白话→精确  │ │ BM25 关键词  │ │ AST 切分   │ │
│  │ 意图分类    │ │ 语义向量     │ │ 向量嵌入   │ │
│  │ 实体对齐    │ │ 依赖图遍历   │ │ 依赖图构建 │ │
│  │ 确认门控    │ │ RRF 融合     │ │ 增量更新   │ │
│  └──────────────┘ └──────────────┘ └───────────┘ │
│                         │                         │
│                   ┌─────┴─────┐                   │
│                   │  LanceDB  │                   │
│                   │  嵌入式DB  │                   │
│                   │ .code-kb/  │                   │
│                   └───────────┘                   │
└──────────────────────────────────────────────────┘
```

## 技术栈

| 组件 | 选型 | 理由 |
|------|------|------|
| AST 解析 | tree-sitter + Python `ast` | 多语言支持，Python 零额外依赖 |
| 向量嵌入 | BAAI/bge-small-en-v1.5 (384维) | 轻量级，ONNX 本地推理，中英双语 |
| 向量数据库 | LanceDB | 嵌入式零部署，Python 原生 API |
| 关键词检索 | rank-bm25 | 纯 Python 轻量实现 |
| 查询改写 | Claude API (Haiku) | 最快模型，改写任务无需复杂推理 |

## 容错设计

整个系统按"逐层降级"原则设计，即使部分依赖缺失也能正常工作：

| 场景 | 正常路径 | 降级方案 |
|------|---------|---------|
| LanceDB 未安装 | ANN 向量检索 | JSON 文件索引 + 暴力余弦相似度 |
| ONNX 模型未下载 | BGE-small 语义向量 | 字符 n-gram 哈希伪向量（确定性，相同文本→相似向量） |
| Anthropic SDK 未安装 | Claude 查询改写 | 跳过改写，原始查询直接检索 |
| 索引快照损坏 | 正常读取 | 自动视为空快照，重新索引 |
| 依赖图损坏 | 图遍历扩展 | 返回空图，搜索不中断 |
| 增量更新路径不一致 | 精确匹配 | 入口处 `abspath` 标准化后再匹配 |

## 项目结构

```
codebase-skills/
├── SKILL.md                    # Claude Code skill 入口指令
├── README.md                   # 本文件
├── LICENSE                     # MIT
├── config.yaml                 # 可配置参数（模型、阈值、路径等）
├── requirements.txt            # Python 依赖列表
├── scripts/
│   ├── install.py              # 一键安装（pip + 模型下载 + 环境验证）
│   ├── index.py                # 索引引擎（全量构建 + 增量更新 + 状态查询）
│   ├── search.py               # 检索引擎（多路召回 + RRF 融合 + 上下文扩展）
│   ├── rewrite.py              # 查询改写器（约束式改写 + 语义距离判断 + 确认门控）
│   ├── chunker/                # AST 切分器
│   │   ├── __init__.py         #   语言注册表 + 工厂方法
│   │   ├── base.py             #   Chunk 数据模型 + 抽象基类
│   │   ├── python.py           #   Python 切分（ast 标准库，支持函数/类/方法/变量/类型注解变量）
│   │   └── javascript.py       #   JS/TS 切分（tree-sitter，支持函数/类/箭头函数/export）
│   ├── embedder/               # 嵌入模型
│   │   ├── __init__.py
│   │   └── local.py            #   ONNX Runtime 本地推理 + 哈希降级
│   └── retriever/              # 检索引擎
│       ├── __init__.py
│       ├── bm25.py             #   BM25 关键词检索（含 TF-IDF 降级）
│       ├── vector.py           #   语义向量检索（LanceDB ANN + JSON 暴力搜索双模式）
│       ├── graph.py            #   依赖图遍历检索（调用者/被调用者/同级函数扩展）
│       └── fusion.py           #   RRF 融合 + 去重
├── hooks/
│   └── post-edit.json          # Claude Code PostToolUse hook 配置模板
├── docs/specs/
│   └── 2026-06-27-codebase-skill-design.md  # 详细设计规格
└── demo-project/               # 演示项目（Python 电商后端）
    ├── .gitignore
    └── src/
        ├── api/payment_api.py
        ├── services/{auth, order, payment, coupon}.py
        └── utils/{db, cache}.py
```

## 功能清单

### AST 切分
- ✅ Python：函数、异步函数、类、方法、模块级变量、带类型注解变量（`x: int = 1`）
- ✅ JavaScript/TypeScript：函数声明、类声明、箭头函数、方法、export 声明
- ✅ 不拦腰截断 — 每个 chunk 都是完整语法单元
- ✅ 自动提取依赖关系（被调用函数名）

### 多路检索
- ✅ **BM25 关键词检索**：加权匹配函数名/类名/路径/docstring/源代码，rank-bm25 不可用时降级为 TF-IDF
- ✅ **语义向量检索**：BGE-small 384维向量 + LanceDB ANN 搜索，LanceDB 不可用时降级为 JSON 暴力搜索
- ✅ **依赖图遍历**：上行（谁调用了我）+ 下行（我调用了谁）+ 同级（同文件相邻函数）
- ✅ **RRF 融合**：Reciprocal Rank Fusion 归一化三路分数 + 去重
- ✅ **上下文扩展**：每个命中结果自动附带 caller/callee/sibling 信息

### 查询改写
- ✅ 约束式 system prompt — 只做等价替换，禁止脑补
- ✅ 意图分类（bug/feature/refactor/perf/locate）
- ✅ 实体对齐（口语 → 代码实体名）
- ✅ 语义距离判断 — 小幅改写直接检索，大幅改写触发用户确认

### 增量更新
- ✅ Hook 触发 — `PostToolUse` 自动更新变更文件
- ✅ 内容哈希比较 — 只处理真正变化的文件
- ✅ 追加模式 — 不会覆盖其他文件的索引数据
- ✅ 依赖图增量合并 — 新节点与已有节点自动建立边

## MVP 范围

**包含 ✅**
- Python + JS/TS AST 切分（零依赖 / tree-sitter）
- 语义向量检索（LanceDB + 暴力搜索双模式）
- BM25 关键词检索（rank-bm25 + TF-IDF 双模式）
- 依赖图构建与遍历（静态调用分析 + 增量合并）
- RRF 多路融合 + 去重
- 约束式 Query Rewriter（含确认门控）
- Hook 驱动增量索引
- 一键安装脚本
- 全量索引 + 首次搜索自动构建
- 所有可选依赖的逐层降级

**不做 ❌**
- Go/Java/Rust/C++ 等语言的 AST 切分（保留扩展点）
- 多用户/团队协作
- Web UI
- Docker 部署
- 自动代码修复（Claude Code 自己做）
- 代码质量分析 / 测试生成

## 成功标准

| 指标 | 目标 | 实测 |
|------|------|------|
| 检索准确率 | Top-3 至少包含目标函数 | ✅ |
| 索引速度 | 100 文件 < 30 秒 | ✅ |
| 检索速度 | 输入到返回 < 5 秒 | ✅ |
| 零部署 | pip install + 配置 hook 即用 | ✅ |

## License

MIT — 详见 [LICENSE](LICENSE)
