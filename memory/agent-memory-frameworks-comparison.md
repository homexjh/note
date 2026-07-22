# AI 智能体记忆框架全面对比（2026）

> 范围：聚焦 **Agent Memory（智能体长期记忆）** 这一细分领域，横向剖析 6 个主流框架的 **存储架构、写入路径、检索/融合算法、基准表现与适用场景**。
> 重点维度：**混合检索的融合方式**（RRF / 加权求和 / 图遍历）——这是本报告的暗线。

---

## 目录

1. [为什么需要记忆框架](#一为什么需要记忆框架)
2. [评估基准速览](#二评估基准速览重要)
3. [框架逐一深度剖析](#三框架逐一深度剖析)
   - [3.1 ReMe / ReMeLight](#31-remeremelight-agentscope-ai)
   - [3.2 Mem0](#32-mem0)
   - [3.3 Zep / Graphiti](#33-zep--graphiti)
   - [3.4 Letta / MemGPT](#34-letta--memgpt)
   - [3.5 LangGraph](#35-langgraph)
   - [3.6 Microsoft GraphRAG](#36-microsoft-graphrag)
4. [跨框架综合对比](#四跨框架综合对比)
5. [关键结论与选型建议](#五关键结论与选型建议)

---

## 一、为什么需要记忆框架

AI 智能体在生产环境里必然撞上两堵墙：

| 问题 | 表现 | 全上下文方案的代价 |
|------|------|------------------|
| **上下文窗口限制** | 长对话早期信息被截断/丢失 | LoCoMo 基准上每轮约 **26,000 tokens** |
| **会话状态丢失** | 新会话从零开始，无法继承历史 | p95 延迟飙到 **17.12 秒**，token 成本线性膨胀 |

Mem0 ECAI 2025 论文量化了取舍：选择性外部记忆相比"全上下文塞满"，准确率仅下降约 6 个百分点，却换来 **91% 更低的 p95 延迟** 和 **90% 更少的 token 消耗**。对绝大多数生产负载，这笔交易是显然的。

记忆框架要解决的，就是把"该记住的"抽出来、**持久化**、并在未来"按相关性"召回。

---

## 二、评估基准速览（重要）

读懂后面分数前，先认清这几个基准：

| 基准 | 发布 | 考察点 | 规模 |
|------|------|--------|------|
| **LoCoMo** | 2024-02（UNC/USC/Snap） | 单跳/多跳/开放域/时间/新信息 5 类长期对话记忆 | 1,540 题 |
| **LongMemEval** | 2024-10（Salesforce/UCSD） | 知识更新、多会话、时间推理 | 500 题 |
| **BEAM** | 2025 | 1M / 10M token 量级生产级评估，10 类能力 | 700 / 200 题 |
| **HaluMem** | 2025 | 记忆完整性 / 记忆准确性 / 问答准确性（幻觉记忆） | — |
| **DMR**（Deep Memory Retrieval） | MemGPT 原基准 | 深度记忆召回 | — |

> ⚠️ **跨论文分数不可直接比较**。各家用不同 backbone、judge、检索步数（单步 vs agentic loop）。例如 ReMe 论文的 LoCoMo 表里 Mem0 仅 61.00，而 Mem0 自己的 V3 报告是 92.5——这是不同年代/不同 harness 的结果，下文汇总时会标注来源。

---

## 三、框架逐一深度剖析

### 3.1 ReMe / ReMeLight (agentscope-ai)

**定位与起源**：开源（Apache 2.0），由 AgentScope 团队出品，名字取 "Remember Me, Refine Me"。同时提供 **文件基（ReMeLight）** 与 **向量基** 两套系统，主攻"上下文压缩 + 跨会话持久化"。论文宣称在 LoCoMo / HaluMem 上达到 SOTA。

**双系统设计**：
- **ReMeLight（文件基）**：记忆即 Markdown 文件，可读可编辑可复制。
- **向量基 ReMe**：个人 / 过程 / 工具 三类记忆，存于向量库（Chroma / Qdrant / Elasticsearch / OBVec）。

**ReMeLight 文件结构**：
```
working_dir/
├── MEMORY.md            # 长期记忆：用户偏好等持久信息（"宪法"）
├── memory/
│   └── YYYY-MM-DD.md    # 每日日记：对话结束后异步写入的总结
├── dialog/
│   └── YYYY-MM-DD.jsonl # 原始对话（压缩前的完整记录，可追溯）
└── tool_result/         # 超长工具输出缓存（带 TTL，自动清理）
```

**写入路径（pre_reasoning_hook 四步流水线）**，每次 Agent 推理前自动执行：
1. `compact_tool_result` — 截断长工具输出，全文转存 `tool_result/`，消息留引用
2. `check_context` — Token 计数，超阈值则按尾部分配 reserve tokens 拆分"待压缩/保留"
3. `compact_memory` — `Compactor`（ReActAgent）生成结构化摘要（目标/约束/决策/进度）
4. `summary_memory` — `Summarizer`（ReActAgent + 文件工具 read/write/edit）异步持久化到 Markdown

**读取路径（MemorySearch）—— 融合算法的关键**：
- 主框架的 `SearchStep`（`reme/steps/index/search.py`）使用 **RRF（Reciprocal Rank Fusion）**：
  ```
  score(d) = vector_weight × 1/(k + rank_vec) + (1 - vector_weight) × 1/(k + rank_bm25)
  ```
  - `k = 60`（`_RRF_K`），压制离群高排名
  - `vector_weight = 0.7`（语义偏置）
  - `candidate_multiplier = 5.0`（先各取 5 倍候选再融合，上限 200）
- 附加能力：**Wikilink Expansion**（基于 file_graph 做一跳遍历，返回相关日记/资源卡/长期摘要节点）、**Tool Context Deduplication**（同会话去重，TTL 24h）
- ⚠️ 轻量 `ReMeLight.memory_search` 文档描述为 `vector_weight` 加权混合（偏向加权求和），与完整框架的 RRF 是两套实现。

**基准（来自 ReMe 论文，GPT-4o-mini judge）**：

| 基准 | 分数 | 明细 |
|------|------|------|
| **LoCoMo** | **86.23%** overall | single-hop 89.89 / multi-hop 82.98 / temporal 83.80 / open-domain 71.88 |
| **HaluMem** | **88.78%** QA | Memory Integrity 67.72 / Memory Accuracy 94.06 |

> ReMe 论文的 LoCoMo 表里，它（86.23）高于 Zep（81.06）、MemOS（75.87）、Mem0（61.00）等。但 Mem0 的 61.00 是旧版/不同 harness，见 3.2 注。

**代码骨架**：
```python
import asyncio
from reme.reme_light import ReMeLight

async def main():
    reme = ReMeLight(
        default_as_llm_config={"model_name": "qwen3.5-35b-a3b"},
        default_file_store_config={"fts_enabled": True, "vector_enabled": False},
    )
    await reme.start()
    processed, summary = await reme.pre_reasoning_hook(
        messages=messages, system_prompt="You are a helpful AI assistant.",
        max_input_length=128000, compact_ratio=0.7,
    )
    hits = await reme.memory_search(query="Python version preference", max_results=5)
    await reme.close()
asyncio.run(main())
```

**优点**：文件透明、零数据库依赖、易调试迁移；自动压缩流水线成熟；RRF 融合稳健。
**缺点**：重度依赖 LLM（Compactor/Summarizer 都是 ReActAgent），写入延迟与成本较高；生态/集成不如 Mem0 成熟。

---

### 3.2 Mem0

**定位与起源**：最广泛部署的记忆层（Apache 2.0，~60k stars），YC 支持，2025-10 完成 $24.5M A 轮。定位"通用记忆层"，提供托管 API 与自托管 OSS。

**三层存储**：
- **SQL 数据库** — 事实与元数据（source of truth）
- **向量数据库** — embeddings（语义检索）
- **实体/图存储** — 实体与关系（实体链接、可选 graph memory）

**写入路径（Extract 阶段）**：
- LLM 从对话中**抽取原子事实**（不是存原对话）
- 去重 + 每条 embed
- **实体链接**：抽命名实体并 embed，把提及同一实体的记忆聚到一起（不重复存）
- **ADD-only 架构**（V3）：新事实标时间失效，**不删除**旧事实 → 保留时间线，修复了旧 UPDATE/DELETE 模型破坏时间上下文的盲区
- 多范围作用域（multi-scope）：每条记忆打 `user_id / agent_id / run_id / app_id` 标签，检索时自动合并排序、隔离跨用户污染

**读取路径（Retrieve 阶段）—— 融合算法的关键**：
- **三路并行打分**，然后**加权融合**（注意：**不是 RRF**，是分数加权）：
  1. 语义（向量余弦）
  2. 关键词（BM25）
  3. 实体匹配（entity boost）
- 再叠加 **时间衰减**（temporal boost / decay）
- 可选 **reranker**（cross-encoder，约 150–200ms 额外延迟）
- 提供 `explain=True` 返回 `score_details`：`semantic_score / normalized_bm25 / entity_boost / raw_combined / final_score`

**基准（Mem0 V3，单步检索，~7,000 tokens/次）**：

| 基准 | V3 分数 | 旧算法 | 提升 |
|------|---------|--------|------|
| **LoCoMo** | **92.5**（top200） | 71.4 → | +21 |
| **LongMemEval** | **94.4**（top200） | 67.8 → | +26.6 |
| **BEAM 1M** | **64.1** | — | — |
| **BEAM 10M** | **48.6** | — | — |

> 最大单项增益：temporal reasoning +42.1（ADD-only 保住时间线）、single-session-assistant +53.6（旧版对 agent 生成事实有盲区，V3 将其列为一等公民）。
> 注：Mem0 2025 论文的 LoCoMo 是 67.13%（LLM-as-judge），相对 OpenAI 内置记忆 +26%；这是不同年代的数字，与 ReMe 表里的 61.00 同样不可横向比。

**代码骨架**：
```python
from mem0 import Memory
m = Memory.from_config({
    "vector_store": {"provider": "qdrant", "config": {"host": "localhost", "port": 6333}},
    "llm": {"provider": "anthropic", "config": {"model": "claude-sonnet-4-6"}},
    "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
})
# 写：自动抽取事实
m.add(messages=[{"role":"user","content":"I just moved to Lisbon."}], user_id="u42")
# 读：多信号融合召回
hits = m.search("Where does the user live?", user_id="u42", limit=5, rerank=True)
```

**优点**：事实蒸馏让索引"干净"（相似度搜的是信号而非噪声）；多信号融合召回率高；异步写入延迟低；基准领先；生态集成最全（LangChain/CrewAI/OpenAI SDK）。
**缺点**：加权融合存在"分数尺度不可比"的固有脆弱性（这正是 RRF 要解决的）；纯向量+抽取对深层时间/多跳推理较弱（Zep 的靶子）；托管有成本（$19–$249/月，graph memory 需 Pro）。

---

### 3.3 Zep / Graphiti

**定位与起源**：最"架构激进"的方案——主张"多数记忆失败不是检索失败，而是**时间推理失败**"。核心引擎是开源 **Graphiti**（Apache 2.0，~28k stars）时间知识图谱。Zep 论文 arXiv:2501.13956。

**存储：双时态知识图谱（bi-temporal）**：
- 每个事实（边）带 **4 个时间戳**：`valid_from / valid_to / observed / recorded`
- 新事实与旧事实冲突时，**旧事实标记失效而非删除** → 可答"一月时他用什么套餐"
- 三张子图：episode（情节）、semantic entity（语义实体）、community（社区），对应 Tulving 记忆三层

**写入路径**：
- 持续从对话/JSON/文档抽取实体、关系、事实
- 赋有效性窗口 + 来源（provenance）
- 图随交互演化，失效旧边、保留历史

**读取路径 —— 融合算法的关键**：
- **三路召回**（高召回）：
  1. 余弦语义（向量）
  2. BM25 关键词
  3. **图 BFS 遍历**（图谱独有：顺关系边找间接关联，如"张三→推荐→海底捞"）
- **重排阶段**（提精度），多策略可选：
  - **RRF**（融合多个排序列表的倒数排名）
  - **MMR**（最大边际相关性，保多样）
  - **Episode-mentions**（按提及频率）
  - **Node distance**（到查询核心的图跳数）
  - **Cross-encoder**（LLM 精排，成本最高）
- 种子节点遍历：以最近对话实体为起点 BFS，优先返回当前话题相关信息
- 检索**不需要 LLM 推理**（最慢只耗在 embed query 上）

**基准**：
- **DMR 94.8%**（vs MemGPT 93.4%）
- LongMemEval 最高 **+18.5%** 精度，相对 baseline RAG **90% 延迟降低**
- LoCoMo 单次检索 **80.32% @ 189ms**，p95 检索 < 200ms

**代码骨架**：
```python
from zep_cloud import Zep
client = Zep(api_key="...")
# 检索尊重有效性窗口，返回 query time 有效的事实
results = client.graph.search(user_id=user_id, query="What plan is this user on?", limit=5)
```

**优点**：时间推理与矛盾消解最强；图遍历能做向量做不到的关系推理；无需检索期 LLM 调用→低延迟；provenance 可审计。
**缺点**：需运维图数据库（Neo4j/FalkorDB/Kuzu/Neptune），操作重量大于 Mem0 的向量库；schema 与抽取开销真实存在；对纯 QA agent 收益有限。

---

### 3.4 Letta / MemGPT

**定位与起源**：UC Berkeley 的 MemGPT 研究项目演化的生产平台。核心理念 **LLM-as-OS**：让模型像操作系统管 RAM/磁盘一样**自己管理内存层级**。

**三层内存**（OS 类比）：
| 层 | 类比 | 行为 |
|----|------|------|
| **Core Memory** | RAM | 常驻上下文的 memory blocks（persona/human/system），Agent 用工具直接读写，有字符上限（~2000/块） |
| **Recall Memory** | 磁盘缓存 | 可搜的对话历史，按时间/内容检索 |
| **Archival Memory** | 冷存储 | 无限向量库，Agent 按需 `archival_memory_search` |

**关键差异：Agent 自管内存**：
- Agent 调用 `core_memory_replace / archival_memory_insert / conversation_search` 等工具，**自己决定**何时 page in/out
- 上下文将满时收到系统提示"你快溢出上下文了"，必须决定驱逐什么、摘要什么、归档什么
- 支持**递归摘要**（Recursive Summary）、**心跳机制**（heartbeat，自主多步循环）、**inner thoughts**（私有推理）

**基准**：LoCoMo **74.0%**（GPT-4o mini backend）、DMR **93.4%**。Letta Code 在 Terminal-Bench 居开源 coding agent 第一。

**代码骨架**：
```python
from letta_client import Letta
client = Letta(token="...")
agent = client.agents.create(
    model="anthropic/claude-sonnet-4-6",
    embedding="openai/text-embedding-3-small",
    memory_blocks=[{"label":"persona","value":"You are Aria."},
                   {"label":"human","value":"Name: unknown."}],
)
# Agent 学到新事实时会自己调用 core_memory_replace
```

**优点**：内存管理逻辑**透明可审计**（不像托管系统黑盒）；适合长程（天~月）agent、需演进 persona/任务态；完全自托管。
**缺点**：每轮可能触发内存管理工具调用 → 延迟与 token 成本上升（约 1.5–3× 无状态 agent）；运维重（Postgres + server + 每 agent 状态）；基准分数不突出（LoCoMo 74%）。

---

### 3.5 LangGraph

**定位与起源**：LangChain 生态的 agent 编排框架，记忆是"双轨"原语而非独立产品。定位是给开发者**存储原语**，融合策略留给你自己。

**双轨记忆**：
- **Checkpointer（短期）**：按 `thread_id` 持久化图状态（对话级），支撑断点续跑/时间旅行
- **Store（长期）**：跨线程共享的 KV 库，命名空间隔离（如 `("memories", user_id)`）

**存储**：Store 默认存 **JSON 文档**，可配 `index` 启用 **语义搜索**（embed 模型 + 向量索引，如 PostgresStore + pgvector）。

**检索 —— 融合算法的关键**：
- `store.search(namespace, query=...)` 按**向量语义相似度单信号**排序
- **无内置 vector+BM25 混合**，无 RRF——框架只提供原语，混合靠你自己接
- 支持 `get`（精确键取）、`search`（语义+过滤）、`list_namespaces`

**代码骨架**：
```python
from langgraph.store.postgres import PostgresStore
store = PostgresStore(connection_string=DB_URI, index={"dims":1536,
    "embed":"openai:text-embedding-3-small", "fields":["text"]})
store.setup()
# 写
await runtime.store.aput((user_id,"memories"), str(uuid4()), {"memory":"User prefers dark mode"})
# 读（单一向量信号）
memories = await runtime.store.asearch((user_id,"memories"), query=last_msg, limit=3)
```

**优点**：极简、与编排天然融合；命名空间隔离清晰；生产可落 Postgres/MongoDB；想怎么融自己定。
**缺点**：只给原语，混合检索/RRF/图都要自己搭；无事实蒸馏（存原始 JSON）；无时间推理/实体链接。

---

### 3.6 Microsoft GraphRAG

**定位与起源**：Microsoft Research 2024-04 开源，面向**文档/语料级**检索增强（非对话 agent），解决 naive RAG "连点失败 / 全局综合弱" 的痛点。

**三阶段管线（Index）**：
1. **抽取**：文本切块 → LLM 抽实体/关系/关键声明
2. **社区发现**：Leiden 算法做层次聚类（L0 实体 → L3 全局社区）
3. **社区摘要**：自底向上为每个社区生成 LLM 摘要

**查询（Query）—— 融合算法的关键**：
- embed query → 与**社区摘要**语义排名 → 选 top-K 社区
- 在社区内做**图遍历**拿子图 + 原文
- 本质是 **"向量找社区 + 图遍历找关系"**
- 查询模式：Global（全局综合）/ Local（特定实体邻居）/ DRIFT（实体+社区）/ Basic（标准 top-k 向量，即 baseline RAG）
- Azure Postgres 实现中，向量+图分数最终用 **RRF** 融合

**基准（微软自家语料）**：
- 播客语料：复杂问题比 baseline RAG **多答对 38%**
- 技术文档：多跳查询质量 **+52%**
- 成本：**查询费 3–5× baseline RAG**（建图需大量 LLM 抽取调用）

**优点**：多跳推理、全局综合、可溯源最强；擅长"跨文档主题/依赖"类问题。
**缺点**：贵（建图+查询 LLM 调用多）；偏离线批处理索引，不适合实时对话记忆；运维图存储。

---

## 四、跨框架综合对比

### 4.1 融合算法全景（你最关心的暗线）

| 框架 | 检索信号 | 融合/重排方式 | 阵营 |
|------|---------|--------------|------|
| **ReMe（主框架）** | 向量 + BM25 | **RRF**（k=60，语义权重0.7）+ Wikilink | 🟢 RRF 派 |
| **ReMeLight（轻量）** | 向量 + BM25 | 加权混合（描述为 `vector_weight`） | 🟡 加权派 |
| **Mem0** | 语义 + BM25 + 实体 + 时间 | **多信号加权融合** + 可选 reranker | 🟡 加权派 |
| **Zep / Graphiti** | 余弦 + BM25 + **图BFS** | 多路召回 → **RRF/MMR/节点距离/cross-encoder** 重排 | 🟢 RRF + 🔵 图 |
| **GraphRAG** | 向量(社区排名) + **图遍历** | 向量定位社区→图内遍历（Azure 版 RRF） | 🟢 RRF + 🔵 图 |
| **LangGraph** | 向量（单信号） | 无内置混合 | ⚪ 单信号 |
| **Letta** | Archival=向量 / Recall=时间内容 | Agent 自主分页，无统一融合 | ⚪ 分仓 |

**结论**：
- **RRF 派**（按名次融合，对分数尺度鲁棒）：ReMe 主框架、Zep（重排器之一）、GraphRAG/Azure
- **加权派**（按分数融合，尺度脆弱）：Mem0、ReMeLight 轻量版
- **图遍历作为第三信号**（超越 vector+keyword 的本质差异）：Zep、GraphRAG

### 4.2 信号维度演进

记忆框架的竞争力，正从"怎么融合向量和关键词"转移到"要不要引入第三信号"：

```
向量相似 ──→ + 关键词(BM25) ──→ + 实体/时间 ──→ + 图关系
  LangGraph        Mem0/ReMe         Mem0/Zep      Zep/GraphRAG
（最基础）      （混合检索标配）   （多信号加权）  （关系推理）
```

### 4.3 存储范式对比

| 范式 | 代表 | 特点 |
|------|------|------|
| **文件（Markdown）** | ReMeLight | 透明、可编辑、零 DB 依赖 |
| **向量库** | Mem0 / LangGraph / Letta(archival) | 语义检索标准方案 |
| **SQL + 向量 + 图** | Mem0 | 事实/嵌入/关系分仓 |
| **时间知识图谱** | Zep / Graphiti | 双时态、关系+时间 |
| **知识图谱 + 社区摘要** | GraphRAG | 多跳、全局综合 |

### 4.4 基准分数汇总（标注来源，勿跨论文横比）

| 框架 | LoCoMo | LongMemEval | HaluMem QA | DMR | 备注 |
|------|--------|-------------|-----------|-----|------|
| **ReMe** | 86.23（论文表） | — | 88.78 | — | ReMe 论文 harness |
| **Mem0 V3** | 92.5（top200） | 94.4（top200） | — | — | Mem0 2026 研究页 |
| **Zep** | 80.32% @189ms | +18.5% vs RAG | — | 94.8% | Zep 论文 |
| **Letta** | 74.0%（GPT-4o-mini） | — | — | 93.4% | Letta 平台 |
| **Mem0 2025论文** | 67.13% | 49.0% | — | — | 旧 harness，仅作参照 |

> 再次提醒：ReMe 表列 Mem0=61.00 与 Mem0 自报 92.5 是不同年代/harness，不能直接说"ReMe 碾压 Mem0"。真实结论应是：各家在各自基准上都显著优于 full-context baseline，且都在 80%+ 区间竞争。

### 4.5 选型决策树

```
你最需要什么？
│
├─ 透明可读、能直接打开文件改、零 DB 依赖
│   └─► ReMeLight
│
├─ 事实蒸馏 + 多信号高召回 + 最快落地 + 生态集成
│   └─► Mem0
│
├─ 时间推理 / 实体关系 / 矛盾消解（CRM、客服、医疗）
│   └─► Zep / Graphiti
│
├─ 长程 agent 自管内存、内存逻辑需可审计、演进 persona
│   └─► Letta / MemGPT
│
├─ 已用 LangGraph，只要存储原语、融合自己定
│   └─► LangGraph Store
│
└─ 文档/语料级多跳推理、全局综合、可溯源（非对话记忆）
    └─► Microsoft GraphRAG
```

---

## 五、关键结论与选型建议

1. **混合检索已是标配，但融合分两派**：RRF（按名次，尺度鲁棒）vs 加权（按分数，尺度脆弱）。你之前问到的 ReMeLight 主框架用的是 **RRF（k=60）**，而 Mem0 用的是**加权**——这就是两者在融合层最本质的区别。

2. **真正的差异化在"第三信号"**：越往高端，框架都在 vector+keyword 之外加维度。Mem0 加**实体/时间**，Zep 与 GraphRAG 加**图关系**。纯向量（LangGraph）和 Agent 分仓（Letta）则是另一种哲学——把"融不融"的问题交给使用者或 Agent 自己。

3. **ReMeLight 的独特定位**：卡在"LangGraph 纯向量"与"Zep/GraphRAG 重图谱"之间——用 **文件透明性 + RRF 混合** 提供比纯向量更聪明、比图谱引擎更轻、且人类可读可改的方案。它的最大卖点不是检索多强，而是**记忆对你可见、可编辑、可迁移**。

4. **没有万能框架**：基准数字高 ≠ 适合你。生产里 ~95% 的调用其实落在 working memory（上下文+scratchpad），语义/图谱层调用极少（~1%）——多数 agent 失败靠" sharpen 当前上下文"而非"加记忆层"解决（Anthropic context engineering 指南同向）。先想清你的查询类型（语义？关键词？实体关系？时间？多跳？）再选。

5. **基准读法**：所有分数带 ±1~±2 置信区间且高度依赖 embedding 模型质量；跨论文横比无意义。看趋势（都比 full-context 好很多）比看绝对值更有价值。

---

*资料来源：各框架官方文档 / GitHub / 论文（ReMe arXiv、Mem0 ECAI2025 & 2026 研究页、Zep arXiv:2501.13956、MemGPT arXiv:2310.08560、GraphRAG Microsoft Research）、及 developersdigest / agentmarketcap / datarekha 等 2026 横向评测。所有分数均标注来源与 harness，请避免跨论文直接比较。*
