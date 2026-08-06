# QwenPaw 记忆系统设计详解（完整版）

> 本文档聚焦"记忆本身"，以认知科学的三分法（**工作记忆 / 短期记忆 / 长期记忆**）为骨架组织内容，并落到代码实现级：类、方法、行号、字段、配置默认值与数据流。
>
> 源码依据：
> - `src/qwenpaw/agents/memory/`（`base_memory_manager.py`、`reme_light_memory_manager.py`、`reme_config.py`、`adbpg_memory_manager.py` / `adbpg_client.py`、`agent_md_manager.py`、`dummy.py`、`prompts.py`、`__init__.py`）
> - `src/qwenpaw/agents/context/scroll/`（`manager.py`、`memoryspace.py`、`history.py`、`eviction_index.py`、`continuation_summary.py`、`recall_tool.py`、`serialize.py`、`sync.py`、`prompt.py`、`repl.py`）
> - `src/qwenpaw/agents/middlewares.py`（MemoryMiddleware 生命周期接入）
> - `src/qwenpaw/config/config.py`（各 Memory / Context 配置类）
> - `src/qwenpaw/app/workspace/workspace.py`（memory_manager 服务注册与回退）

---

## 0. 关于"记忆"的框架：三层映射

QwenPaw 的代码并**不**使用"工作/短期/长期"命名——它内部管这两套通道叫 **Scroll（上下文记忆）** 和 **BaseMemoryManager（长期记忆）**。但用认知科学的三分法来映射，是理解整个系统最清晰的视角：

| 认知科学概念 | QwenPaw 对应实现 | 持久化 | 内容形态 |
|------|------|------|------|
| **工作记忆（Working）** | `agent.state.context` 当前 prompt 窗口（由 Scroll + MemoryMiddleware 实时拼装） | **不单独持久化**（随一次推理生灭的视图） | 原文 + recall 捞回的早期原文 + 长期检索结果 的拼装 |
| **短期/会话记忆（Short-term）** | Scroll 的 `HistoryStore`（`history.db`） | 持久化（SQLite WAL，`history_retention_days` 滚动清理） | **逐字**对话原文，可压缩、可召回、可跨会话续接 |
| **长期记忆（Long-term）** | `BaseMemoryManager` 后端（`remelight` / `adbpg` / `none`） | 持久化（本地 Markdown vault / 云端 AnalyticDB） | **提炼过**的知识/结论，语义检索 |

**两个关键澄清（基于此前对话）：**

1. **"上下文压缩"不是一种独立的记忆，而是短期记忆内部的一个子能力。** 它和"存储 / 召回 / 跨会话续接"并列，都是 Scroll 子系统的一部分。准确说法是：压缩 ⊆ 上下文记忆，而不是"上下文属于压缩"。被压缩掉的原文并没有消失——它仍躺在 `history.db`，可被 `recall_history` 捞回；重要的内容还会先经过 `auto_memory` 提炼进长期记忆。
2. **"QwenPaw 记忆 = ReMe"是错的。** ReMe 只是长期记忆层的一个可插拔后端（`remelight`，默认）。即使走 remelight，也是"薄封装 + 组件注入"——真正干活的模型与存储来自 QwenPaw 自己。长期记忆层还有 `adbpg`（云端）、`none`（关闭）两个后端；短期记忆层（Scroll）则完全自研，与 ReMe 无关。

一句话收口：**记忆系统 = 接入层 + （Scroll 短期记忆 / BaseMemoryManager 长期记忆）两条通道；工作记忆是这两者在一次推理时拼出的窗口。**

---

## 1. 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                       Agent (AgentScope ReAct)                    │
│  build_middlewares → MemoryMiddleware ──┐                          │
│  context_manager (Scroll) ─────────────┤  recall_history[_python] │
└───────────────────────────────────────┼──────────────────────────┘
                                         │
        ┌────────────────────────────────┴─────────────────────────┐
        │                  BaseMemoryManager (registry)             │
        │  start() / close() / get_memory_prompt() /               │
        │  list_memory_tools() / summarize() / dream() /           │
        │  auto_memory_search() / auto_memory()                    │
        ├──────────────┬───────────────┬───────────────────────────┤
        │ remelight    │ adbpg         │ none (Noop)               │
        │ (默认, 内嵌   │ (云端         │ (禁用全部记忆)             │
        │  ReMe 应用)  │  AnalyticDB)  │                            │
        └──────────────┴───────────────┴───────────────────────────┘
        │ 记忆文件 (Markdown vault)         │ 云端 REST API
        │ memory/ digest/ resource/ ...    │
        └──────────────────────────────────┘
  独立并行：Scroll 上下文管理（SQLite history.db + EvictionIndex + ContinuationSummary）
```

记忆相关概念在代码中的落点：
- **长期记忆** → `agents/memory/` 全部 + `MemoryMiddleware` 的检索/抽取钩子
- **短期记忆** → `agents/context/scroll/` 全部
- **工作记忆** → `agent.state.context`（无独立模块，是上述两者的实时拼装，见第 4 章）

---

## 2. 长期记忆（Long-term Memory）

### 2.1 抽象层：`BaseMemoryManager`

文件：`agents/memory/base_memory_manager.py`。核心类 `BaseMemoryManager(ABC)`，模块级 `memory_registry: Registry[BaseMemoryManager]`。

#### 2.1.1 生命周期契约

| 方法 | 抽象? | 作用 |
|------|-------|------|
| `start()` | 抽象 | 初始化存储后端（一次） |
| `close()` → `bool` | 抽象 | flush 并释放资源，返回是否干净关闭 |
| `get_memory_prompt()` → `str` | 抽象 | 注入系统提示的记忆使用指引 |
| `list_memory_tools()` → `list[Callable]` | 抽象 | 返回要注册给 agent 的记忆工具（如 `memory_search`） |
| `build_middlewares()` | 有默认实现 | 返回 `[MemoryMiddleware(self)]` |
| `summarize(messages, **kw)` | 可选，默认返回 `""` | 提炼对话并落盘 |
| `dream(**kw)` | 可选，默认空 | 后台整理/合并记忆 |
| `auto_memory_search(messages, ...)` | 可选，默认 `None` | 回帖前自动检索，返回可合并的 kwargs |
| `auto_memory(all_messages, ...)` | 可选，默认空 | 回复后周期性自动抽取记忆 |
| `reme_status()` / `rebuild_index()` | 可选，默认 `None` | 后端特定 |

构造签名统一为 `__init__(working_dir: str, agent_id: str)`。

#### 2.1.2 注册表与工厂

- `@memory_registry.register("xxx")` 装饰器登记后端类。
- `get_memory_manager_backend(backend: str) -> type[BaseMemoryManager]`：查表；**找不到时回退到第一个已注册后端**（并 warning），无后端则抛 `ValueError`。
- 实例化在 `workspace.py:374`：`get_memory_manager_backend(ws._config.running.memory_manager_backend)`，传入 `working_dir=ws.workspace_dir`、`agent_id=ws.agent_id`；服务 `optional=True`（ReMe 依赖 `agentscope.token` 缺失时允许整个 workspace 正常启动，仅记忆失效）。

#### 2.1.3 后台摘要 workers

基类内置一个**串行 FIFO 摘要 worker**（与后端解耦，所有后端可用）：

- `add_summarize_task(messages, **kw)`：确保 worker 在跑（`asyncio.create_task(self._summarize_worker())`），自增 `_task_counter` 生成 `task_<n>`，入队 `asyncio.Queue`。
- `_summarize_worker()`：循环取任务 → 调 `self.summarize()` → 更新 `_summary_task_info[task_id]["status"]`（`running/completed/failed/cancelled`）；`CancelledError` 标 `cancelled` 并 re-raise。
- `_shutdown_summarize_worker(timeout=5.0)`：置 `_worker_stopping=True` 后 `worker.cancel()`；**双 cancel 防护**（首个 cancel 可能被嵌套模型调用吞掉，二次 cancel 兜底），超时返回 `False`。
- `list_summarize_status()` / `_prune_summary_task_info()`：保留活跃任务 + 最多 `MAX_SUMMARY_TASK_HISTORY=100` 条终态历史。

#### 2.1.4 auto-memory-search 合成消息

`_build_auto_memory_search_msg(query, max_results, text) -> AssistantMsg`：把一次记忆检索"伪装"成一条已有的 assistant 工具交互（`memory_search` 的 tool_call + tool_result），这样模型在后续对话里能看到"我之前查过记忆"，但该消息带 `AUTO_MEMORY_SEARCH_BLOCK_IDS_KEY` 元数据标记。后续 `_messages_without_auto_memory_search()` / `message_without_auto_memory_search()` 可在**持久化**时把这些合成块剔除，避免污染真实历史。

#### 2.1.5 自动记忆 turn 状态

`get_auto_memory_turn_state(session_id)`：按 session 维护 `pending/seen/touched_at` 字典，`AUTO_MEMORY_TURN_STATE_TTL_SECONDS=24h` 过期清理；供 middleware 实现"每 N 轮自动记忆"的节拍跟踪。

### 2.2 默认后端：`ReMeLightMemoryManager`（registry key `"remelight"`）

文件：`agents/memory/reme_light_memory_manager.py`。**它自身几乎不实现记忆逻辑**，而是把 QwenPaw 的工作目录当作 ReMe 的 vault，通过 ReMe 的内部 **job 框架**执行每日记忆、检索、auto-memory、auto-dream 等。类名保留历史 `ReMeLight` 命名以兼容旧配置。

#### 2.2.1 初始化与启动

- `__init__`：尝试 `from reme import ReMe`，用 `get_reme_app_config(...)` 构造内嵌 ReMe 应用；失败则 `logger.warning` 置 `self._reme=None`（记忆禁用，不崩）。
- `start()`：先 `_update_qwenpaw_model()`（见 2.2.4），再 `await self._reme.start()`。
- 关键常量：`INBOX_RESULT_JOB_NAMES = {"auto_memory","auto_dream","auto_resource"}`、`MAX_INBOX_BODY_CHARS=4000`、`_REME_SESSION_ID_PREFIX="qpsid_"`。

#### 2.2.2 session_id 安全编码 `_to_reme_session_id()`

ReMe 0.4 把 `session_id` 当作**文件名组件**，而 QwenPaw 的 channel id 可能含 `telegram:123` 这类非法字符（Windows 非法）。策略：
- 普通安全 id（无非法字符、非保留名、非 `..`、≤240 字符、不以前缀开头）原样返回；
- 否则 base64url 编码（`qpsid_b64_...`），仍超限则 **sha256 摘要**（`qpsid_sha256_...`）。映射确定且可逆（base64 可解）。

#### 2.2.3 对外接口（薄封装 ReMe jobs）

| 方法 | 调度的 ReMe job | 说明 |
|------|-----------------|------|
| `memory_search(query, max_results=5, min_score=0)` | `search` | **混合检索**（向量 + BM25，RRF 融合）；`min_score` 建议保持 0（不同分数尺度会误杀关键词匹配） |
| `summarize(messages, session_id, memory_hint)` | `auto_memory`（`needs_llm=True`） | 把对话提炼进当日 daily note |
| `auto_memory(all_messages, session_id)` | → `add_summarize_task` → `auto_memory` job | 后台串行抽取 |
| `auto_memory_search(messages, ...)` | `search` | 回帖前自动检索，返回合成 assistant 消息 |
| `dream(date, hint)` | `auto_dream`（`needs_llm=True`） | 梦境整理：扫描 daily/digest，抽取 topic、写 `interests.yaml` |
| `reme_status()` | `status` | 内存/组件估计、进程 RSS |
| `rebuild_index()` | `reindex` | 清空并重建索引（带 `_reindex_lock` 防并发） |

#### 2.2.4 复用 QwenPaw 模型

`_update_qwenpaw_model()`：`create_model_and_formatter(agent_id)` 拿到当前模型对象，调用 `self._reme.update_component("as_llm", "default", model=model)`，让 ReMe 内部 agent 复用 QwenPaw 活跃模型（避免重复配置 key）。

#### 2.2.5 结果回推 Inbox

- `_install_reme_result_hook()`：把 `_handle_reme_result_hook` 挂到 ReMe 的 `context.metadata[INBOX_RESULT_HOOK_KEY]`，使 ReMe 后台步骤也能回调。
- `_append_reme_job_result_to_inbox(name, response, kwargs)`：仅对 `INBOX_RESULT_JOB_NAMES` 内的 job，且 `inbox_push_enabled` 且 `modified != False` 时，向 inbox 推送一条 `source_type="memory"`、`event_type=f"{name}_result"` 的事件（title/body 取自 response.answer，超 `MAX_INBOX_BODY_CHARS` 截断）。用 `INBOX_EMITTED_METADATA_KEY` 防止重复推送。失败/异常被吞（仅日志），不影响主流程。

### 2.3 ReMe 应用配置

文件：`agents/memory/reme_config.py`，入口 `get_reme_app_config(...)`（deepcopy 安全）。要点：

- **目录映射**（均来自 `agent_config.running.reme_light_memory_config`）：`workspace_dir`、`metadata_dir`(`mem_metadata`)、`session_dir`(`mem_session`)、`mem_session_dir`(`mem_agent`)、`resource_dir`(`resource`)、`daily_dir`(`memory`)、`digest_dir`(`digest`)、`language`、`timezone`（默认 `Asia/Shanghai`）。
- **watch 策略**：`index_update_loop` 监控 `daily_dir`/`digest_dir` 的 `*.md`；`resource_watch_loop` 监控 `resource_dir`（md/txt/json/...）。二者都 `background` 后端，init + watch 两步，变更即更新索引/目录/跑 `auto_resource_step`。
- **job 集合**（描述性配置，ReMe 按 `backend:"base"` + `steps` 执行）：`version`、`status`、`reindex`、`search`（vector_weight=0.7, candidate_multiplier=3.0, expand_links）、`node_search`、`daily_list`、`daily_reindex`、`frontmatter_*`、`stat`、`list`、`move`、`delete`、`read`/`read_image`、`write`/`daily_write`/`edit`、`auto_dream`（4 步：extract→integrate→topics→finish）、`proactive`、`auto_memory`、`auto_resource`。
- **components**（ReMe 组件装配）：
  - `tokenizer: regex`；`as_llm.default` 占位（`qwenpaw-injected`，实际模型由 2.2.4 注入）；`agent_wrapper` 用 `agentscope` 后端、`permission_mode:"bypass"`、`react max_iters:30`、`context_config(trigger_ratio=0.8, reserve_ratio=0.1, tool_result_limit=50000)`。
  - `file_graph/file_catalog`：local（default/resource/digest/dream 四类目录）。
  - `file_chunker`：markdown + jsonl。
  - `keyword_index`：BM25。
  - `as_embedding` + `embedding_store`：OpenAI 兼容 / gemini / ollama，**当 embedding 未配置时显式清空**（`embedding_store` 置空、`pop` 掉 `as_embedding`），避免 LocalFileStore 仍去查找默认 store。
- **embedding 配置映射**（`_apply_embedding_config`）：`_is_embedding_enabled` 判定（openai/dashscope/gemini 需 `api_key`，ollama 恒真）；`_embedding_credential` 按后端拼 `api_key/base_url` 或 `host`；维度/`enable_cache`/`max_cache_size`/`max_input_length`/`max_batch_size` 透传。

> `_OPENAI_COMPAT_EMBEDDING_BACKENDS = {openai, dashscope, dashscope_multimodal}`，需与前端 `ReMeLightMemoryCard.tsx` 保持一致。

### 2.4 云端后端：`ADBPGMemoryManager`（registry key `"adbpg"`）

文件：`agents/memory/adbpg_memory_manager.py` + `adbpg_client.py`。把长期记忆存到 **AnalyticDB for PostgreSQL**（云端 REST）。**上下文压缩与 tool-result 修剪由原生 AgentScope / ToolResultPruningMiddleware 负责，本类只管长期存储与检索**。

- 配置：`ADBPGMemoryConfig(rest_base_url, rest_api_key, memory_isolation=True, search_timeout=10.0, auto_memory_search_config)`。缺 URL/key 时 `start()` 置 `client=None`（记忆禁用，不崩）。
- **隔离**：`memory_isolation=True` 时 `_effective_agent_id=agent_id`（每 agent 独立）；`False` 时 `"shared"`。`user_id/run_id` 固定 `"shared"`。
- **auto_memory 间隔固定为 1**（每轮持久化用户消息），由 ADBPG 服务端做事实抽取，QwenPaw 侧不过滤 interval。
- `summarize` / `auto_memory` 走 `_schedule_add` → `asyncio.create_task` **fire-and-forget**（不阻塞回复），任务完成回调从 `_pending_add_tasks` 移除。`close()` 时 `gather` 等待未决任务。
- `memory_search` **双源融合**：① ADBPG 语义检索（`score >= min_score`，默认 0.1）；② 本地 `MEMORY.md` + `memory/*.md` 关键词段落匹配（按 token 命中率打分）。任一源失败仅 warning，不影响另一源。
- `auto_memory_search`：在 `auto_memory_search_config.enabled` 时于 `on_model_call` 前检索，复用基类合成消息机制，无结果返回 `None`。

### 2.5 禁用后端：`NoopMemoryManager`（registry key `"none"`）

文件：`agents/memory/dummy.py`。`enabled=False`，所有抽象方法返回空/空列表、`build_middlewares()` 返回 `[]`、`get_auto_memory_interval()` 返回 0。**彻底关闭记忆系统**用。

### 2.6 `AgentMdManager`：Markdown 文件读写助手

文件：`agents/memory/agent_md_manager.py`（`class AgentMdManager`，非 registry 成员，纯工具类）。提供 workdir/memory/digest 三类目录的 md 读写，重点是**路径安全**：

- 目录：`memory_dir = working_dir/<reme_config.daily_dir>`（默认 `memory`），`digest_dir = working_dir/<reme_config.digest_dir>`（默认 `digest`）。
- `_sanitize_md_name`：拒绝 `..` 穿越、只取末段文件名。
- `_normalize_md_path`：规范化相对路径、补 `.md`、拒绝空/`.`/`..`。
- `_assert_within_dir`：用 `Path.resolve().relative_to(base)` **防越界**（跟随符号链接后比较，堵住绕过向量）。
- 方法：`list_working_mds` / `read_working_md` / `write_working_md` / `list_memory_mds` / `read_memory_md` / `write_memory_md`（按 `digest` 前缀分流到两个根目录）。

---

## 3. 短期/会话记忆（Short-term / Episodic Memory）— Scroll

范围：`agents/context/scroll/`。这是 QwenPaw 自研的**长上下文滚动管理**：AgentScope 原生压缩会"就地总结并丢弃原文"，Scroll 改为**把中段对话驱逐到 SQLite 持久存储**，保留逐字记录，模型可 `recall_history` 按需取回，且**跨会话可检索**。

### 3.1 解决的问题 & 启用条件

- 替代原生压缩（`ContextManager` Protocol，`context/base.py:21`）。
- 装配函数 `build_scroll_components`（`context/__init__.py:123`）：仅当 `light_context_config.strategy == "scroll"` **且**有 workspace 时返回 `ScrollComponents(context_manager, repl_tool, recall_tool)`；否则返回 `None`，agent 退回原生管理。

### 3.2 核心组件

| 组件 | 位置 | 职责 |
|------|------|------|
| `ScrollComponents` | `context/__init__.py:65` | 装配容器 |
| `ScrollContextManager` | `scroll/manager.py:78` | 主控制器，持 live/seq/leaf 簿记，驱动 8 阶段压缩流水线 |
| `HistoryStore` | `scroll/history.py:55` | SQLite WAL 持久读写（**读路径见 3.3**） |
| `EvictionIndex` | `scroll/eviction_index.py:144`（`Leaf:71`/`Line:79`/`Block:111`） | 层级 odometer，渲染成单个 `[context compressed]` 占位块（**算法见 3.4**） |
| `ContinuationSummary` | `scroll/continuation_summary.py:431` | 纯 Markdown 任务状态 + seq 溯源指针 |
| `RecallLoopGuard` | `scroll/recall_tool.py:110` | 防重复 recall 同页（**见 3.5**） |
| `make_recall_history` | `scroll/recall_tool.py:444` | 结构化 recall 工具（expand/search/recall_tool） |
| `make_recall_history_python` | `scroll/repl.py:84` | 沙箱化 Python REPL recall |
| `MemorySpace` | `scroll/memoryspace.py:172` | 只读 ATTACH 历史 + 可写 scratch 的查询引擎 |

### 3.3 存储：`history.db`（SQLite WAL）— 具体 schema 与写入路径

`HistoryStore`（`history.py:55`）**持有唯一的读-写连接**，模型侧经 `MemorySpace` 以 `ATTACH` 只读方式访问同一文件（WAL 下读写共存）。要点：

- 连接以 **WAL** 打开（`PRAGMA journal_mode=WAL`，`busy_timeout=5000`）；`check_same_thread=False` + 一把 `self._lock` 串行化访问（压缩从 worker 线程 `asyncio.to_thread` 写，`on_save` 在 loop 线程写，共用此连接）。
- 启动 `PRAGMA quick_check` 损坏检测；损坏则 `_quarantine`（把坏文件+`-wal/-shm` 重命名为 `.corrupt-<时间戳>`）后重建空库——"坏记忆"降级为"丢历史"，不崩启动。
- db 路径 = `Path(workspace_dir) / scroll_config.db_filename`（默认 `history.db`）；scratch 根 = `workspace/.scroll`。
- `degraded` / `write_failures`：首次 write-through 失败时翻转 `degraded=True` 并 prominent 日志；监控可据此判定"全持久化"保证已失效。

#### 3.3.1 表结构 `conversation_history`

```sql
CREATE TABLE conversation_history (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 全局唯一水位，跨会话/跨 agent
    session_id   TEXT NOT NULL,
    agent_id     TEXT,
    kind         TEXT NOT NULL,    -- model_turn / context_msg / tool_result ...
    role         TEXT,             -- user / assistant / system
    name         TEXT,             -- 发送者名（recall 工具自身轮次 name∈_RECALL_TOOL_NAMES 不入 FTS）
    content      TEXT,             -- 主体文本（FTS5 索引列）
    tool_call_id TEXT,             -- 工具调用 id（tool_result 行的身份）
    tool_input   TEXT,             -- 工具入参（JSON）
    tool_state   TEXT,             -- 工具状态
    headline     TEXT,             -- 模型写的 ⟦ … ⟧ 摘要（≤2000 字符持久，模型视图截断到 400）
    blocks       TEXT,             -- 结构化块（JSON）
    metadata     TEXT,             -- 元数据（JSON）
    created_at   TEXT,             -- ISO-8601；NULL 则永不进入 purge
    dedup_key    TEXT              -- 会话内稳定身份（turn=msg.id, result=tool_call_id）；NULL 不幂等
)
```

索引：
- `ch_session ON (session_id)`、`ch_agent ON (agent_id)`、`ch_kind ON (kind)` —— 查询加速。
- **`ux_dedup UNIQUE(session_id, dedup_key)`** —— 幂等网：同 `(session_id, dedup_key)` 二次写入被 `ON CONFLICT DO NOTHING` 丢弃；**NULL dedup_key 永不冲突**（永不幂等）。用于 resume 重放已恢复窗口时不复制行。
- **FTS5 外部内容索引**：`conversation_history_fts USING fts5(content, content='conversation_history', content_rowid='seq', tokenize='porter unicode61')`。外部内容（不复制文本）；`append`/`update_entry` 手动同步；porter 词干 + unicode61 大小写折叠（"tanks" 匹配 "tank"）。**`_RECALL_TOOL_NAMES=("recall_history_python","recall_history")` 的行不进 FTS**——否则模型自己读记忆的查询/回溯会污染后续 `search`（自污染反馈环）。FTS 缺失时（`no such module: fts5`）降级为 LIKE 扫描，store 仍全功能，仅日志一次 warning。

#### 3.3.2 写入路径（write-through）

- `append(*, session_id, entry, agent_id, dedup_key)` → 返回分配的 `seq`（水位）。写入 `ON CONFLICT(session_id, dedup_key) DO NOTHING`；若 `rowcount==0`（冲突，已持久）则返回**已有** seq（让调用方重链接簿记而不复制行），且不写 FTS。成功且非 recall 工具名则 `INSERT` 进 FTS。
- `append_many(*, session_id, entries: Sequence[tuple[LogEntry, dedup_key]], agent_id)` → **单事务**批量写，返回新增行数；用于 startup backfill（避免每行 fsync 拖慢启动）。行为与 `append` 一致（重复 key 跳过、新行进 FTS）。
- `update_entry(seq, *, content, headline, blocks, tool_call_id, name, tool_state, tool_input)` → **原地刷新**保持 FTS 同步：AgentScope 把整段回复累积进单个 assistant Msg，故首写后若某 turn 又长出后续 tool call，需刷新 scalar 列；先 `SELECT content` 再 `DELETE` 旧 FTS 行、`INSERT` 新 FTS 行。`seq` 不变。
- 序列化 `msg_to_entries`（`serialize.py:248`）：非 result 消息 → 1 行 `model_turn`/`context_msg`，每个 `tool_result` → 1 行。token 超 `effective_hard_limit` 时若 write-through 失败，抛 `ContextWindowUnfitError`。
- `_to_json(value)`：None→None，str→原样，其它→`json.dumps(value, default=str)`。

#### 3.3.3 读路径

- `count(session_id)`、`existing_seqs(seqs)`（全局地址校验存在性）、`contents_by_seqs(seqs)`（按主键精确取 content，用于 summary evidence；按 500 分块防超参）。
- `_purge_where(before, kinds)`：共享 WHERE 构造，恒 `created_at IS NOT NULL AND created_at < ?`，可选 `kind IN (...)`（如 `("tool_result",)` 只清工具输出保留对话）。
- `estimate_purge(*, before, kinds)` → `{"rows", "content_bytes"}`：dry-run 估算（不删）。
- `purge(*, before, dry_run, kinds)` → 删除行数；`dry_run=True` 只数不删；删除前先从 FTS 移除对应行；NULL `created_at` 永不匹配。**只 DELETE 不 VACUUM**，故文件不实缩。
- `vacuum()`：单独 O(db size) 步骤重写文件回收空间（retention purge 路径不内联调用，避免阻塞）。

### 3.4 驱逐与预算（EvictionIndex 分层 odometer）

触发：`should_compress(tokens, trigger)`（`manager.py:157`）：`tokens > trigger`。`compress`（`manager.py:366`）预算常量：

```python
hard_limit = agent.model.context_size
output_reserve = min(4096, max(1, int(hard_limit * 0.05)))   # _OUTPUT_RESERVE_RATIO=0.05
effective_hard_limit = hard_limit - output_reserve
trigger = cfg.trigger_ratio * agent.model.context_size          # 默认 0.8
reserve = min(40000, max(cfg.reserve_ratio*context_size, min(10000, context_size*0.1)))
```

#### 3.4.1 8 阶段压缩流水线（`manager.py:379-394`）

1. persist（异步 `_persist_guarded_async` write-through 到 `history.db`）
2. trigger 检查
3. pre-fold 已完成轮 tool results（`_batch_fold_completed_tool_results`，≤200 字符不替换）
4. `split_for_compression`（经 `scroll/_as_internals.py`）分离 middle/tail
5. 更新续接摘要（`ContinuationSummary`）
6. `_index_evicted` + `_rebuild_context`（重建 `[memory]` 占位 + tail）
7. `_fold_tool_results_under_pressure`
8. `_batch_fold_seen_active_results`（硬限）

#### 3.4.2 EvictionIndex 分层折叠（具体算法）

整个索引以**一个占位块**存在于 prompt，模型始终"看见地图"。结构是一叠 tier：

- **Tier 0（最底）**：最新驱逐，每个 block 列出其 turn 全文（headline）。
- **Tier k（k≥1）**：更旧历史，向上携带并压缩成端点（head-tail）。

每个 tier 最多 `_TIER_CAP=10` 个 block。每次驱逐：`add_eviction(leaves, *, seq_lo, seq_hi)`（`eviction_index.py:158`）在 Tier 0 追加一个新 block（含 `leaves` 里程碑 turn；无 milestone 的 span 记 `(no milestone)`），然后 `_carry(k)`。

**`_carry` 级联**（像 odometer 过 9 进位）：
- 当 tier k 满（`len >= _TIER_CAP`），`_carry_run(k, count=len-1)`：保留最新 1 个 block，把其余 `count` 个 block 通过 `_collapse(blocks)` 折叠成**每个一行**（只留该 block 的 `seq_lo/seq_hi` 端点 headline），堆叠成一个新 block 推到 tier k+1，再 `_carry(k+1)` 级联。
- `_collapse` 自相似（`_collapse` of already-collapsed 仍只取左右端点），故 turn / span / span-of-spans 同法归约，可无损级联到任意深度。

数据类：`Leaf(seq, headline)`（单个里程碑 turn）、`Line(seq_lo, seq_hi, head, tail)`（block 内一行，单 turn 时 `lo==hi`）、`Block(seq_lo, seq_hi, lines)`（一个 tier 内的 run）。

**渲染 `render()`**（`eviction_index.py:259`）：输出单个 `<system-info>` 占位，含：
- 前缀：`[context compressed]` 说明这是归档地图（非实时对话）；
- 重扩指引：`recall_history(op="expand", lo, hi)` / `op="search"`；
- tier 目录：最高 tier（最旧）在顶，Tier 0（最近压缩）在底，每 `·` 行带 `seq` span 与 `⟦ headline ⟧`；
- 接缝横幅 `_ARCHIVED_INDEX_END` + `_LIVE_TURN_BANNER`：明确告诉模型"地图是归档、下面的实时 turn 才是请求，绝不回答地图上的 `⟦headline⟧`"——这是针对弱模型（GLM/DeepSeek）会把 headline 当成请求的机制性防御，且恒定文本不影响 KV-cache 前缀。
- **KV-cache safe**：前缀 + tier 横幅恒定，新驱逐只在 Tier 0 底部 append，故其上方字节不变；carry 才重塑上层（固有代价）。
- `describe()`（`eviction_index.py:316`）：去掉前缀的 tier/span 地图，用于用户态 `/compact` 回复。
- `to_dict()` / `from_dict()`：索引序列化为普通数据，供 agent checkpoint（兼容旧 `levels` 键名）。

模型视图预算 `_HEADLINE_VIEW_CHARS=400`：超预算时先截断 headline，再折叠成端点行，最后整个目录退化成**一个全局 seq span**（仍精确可 `expand` 恢复，损失less 的持久 state 不变）。

### 3.5 召回（Recall）— 具体

#### 3.5.1 防回声：`RecallLoopGuard`（`recall_tool.py:110`）

按 `turn_id + generation` 去重同页，原子拒绝重复 recall：

- `begin_turn(turn_id)`：真实用户 turn 变化时 `generation += 1` 并清空 `blocked`/`_in_flight`。
- `claim(op, payload)` → `(generation, notice)`：已在 `blocked` → 返回 `_RECALL_BLOCKED_NOTICE`（该页本 turn 已完成）；已在 `_in_flight` → `_RECALL_IN_FLIGHT_NOTICE`（并发重复）；否则登记并返回 generation。
- `finish(op, payload, generation, *, block)`：慢完成的旧 turn 不污染新 turn 的 claim；若 `block` 且 generation 当前则加入 `blocked`。
- `allow_restart(op, payload)`：快照漂移后允许无 cursor 重启。
- `is_blocked(...)`：暴露状态供集成测试。

`key(op, payload)`：规范化参数（expand→lo/hi，search→query/k/kind/all_agents/session_id/agent_id，recall_tool→tool_call_id），附加 cursor 后 JSON；保证"同参同页"稳定判定。

#### 3.5.2 三种 op：`make_recall_history`（`recall_tool.py:444`）

构造绑定到单 session 的 `recall_history` 工具（**进程内执行，无需沙箱、治理类型 `internal`**——每个 op 是绑定参数的只读 SQL，模型不供给代码；`recall_history_python` 才是需沙箱的逃生口）。每调用新建一个 `MemorySpace`（只读 ATTACH + authorizer 与 REPL 一致，调用间不泄漏连接），用 `asyncio.to_thread` 跑 `_run`，返回 `ToolChunk`。

| op | 参数 | 行为 |
|----|------|------|
| `expand` | `lo`, `hi`（seq span） | `ms.expand(lo, hi)` 按 `seq BETWEEN ? AND ?` 逐字回放；一轮即 `lo==hi` |
| `search` | `query`, `k=10`, `kind`, `all_agents`, `session_id`, `agent_id` | FTS5 bm25（`ms.search`），回退 `_search_like`；补搜已落盘 saved tool-output 文件；关键词非整句，OR 放宽，k 取大 |
| `recall_tool` | `tool_call_id` | 取回某工具调用结果，标注 artifact 指针（大输出给 saved 全量文件路径） |

**防回声（排除活动轮）**：`_active_turn_floor` 不回显"最新真实 user 请求之后"的 seq；recall 工具自身轮次不入 FTS。

#### 3.5.3 分页 cursor（请求-快照绑定）

- `_request_fingerprint(op, payload)`：对"决定结果集"的参数做 sha256（排除 cursor）。
- `_result_fingerprint(rows)`：对结果集做 sha256。
- `_encode_cursor(row_index, char_offset, request_fingerprint, result_fingerprint)` → `v1.<base64url>`：绑定请求指纹 + 结果快照指纹。
- `_parse_cursor(...)`：校验 version==v1、request 指纹一致（否则"属于不同请求"）、result 指纹一致（否则 `RecallSnapshotChangedError` 需无 cursor 重启）、row/char 越界。
- `_render_page(rows, ...)`：按 `page_max_bytes`（默认 `DEFAULT_MAX_BYTES`）分页，每页带 `next_cursor`；`_bound_observation` 对所有输出（含失败/空读）做 UTF-8 字节上限截断，末尾附 `[… recall observation truncated]`。
- 空结果**显式区分于失败**（"0 rows… history genuinely holds nothing" 且 `ok=True`），避免模型误判为错误。

### 3.6 续接 / 摘要（Continuation）

`ContinuationSummary`（`continuation_summary.py:431`）字段：`covered_seq`、`active_task`、`status`(in_progress|blocked|completed|unknown)、`current_state`、`constraints`、`decisions`、`open_work`（各为 `SummaryItem`）。`covered_seq` 跟踪已压缩覆盖的 seq 上限，实现压缩轮次追踪与**跨会话恢复**——重建上下文时摘要渲染进 `<system-info>` UserMsg。

`build_update_prompt`(`:74`) 生成 initial/update 两种（en/zh）固定 heading 更新提示（含 `redact_secrets`）。模型回写经 `parse_plain_markdown`(`:610`) 解析（失败返回 None，**fail closed**），再 `validate_summary_quality`(`:690`) 做确定性校验（status、`identifier` 须来自 evidence、seq 指针须在 covered 范围内）。`manager._update_continuation_summary`(`manager.py:1227`) 内 `asyncio.timeout(60.0)`、最多 2 次重试；任何失败保留上一值。

### 3.7 同步（Sync）

`sync.py` 在**每次服务启动**时把 `sessions/*.json` 会话文件导入 `history.db`，解决"启用 scroll 前/未 write-through 的历史不可 recall"问题。

- `sync_all_scroll_agents`(:480) → 遍历 `config.agents.profiles`，仅处理 `strategy=="scroll"`，用 **profile ref 路径**的 `workspace_dir`（避免 clone 互相覆盖）。
- `sync_sessions_to_history`(:346，`retention_days=0` 表示永久)：**非破坏**（只读源）、**幂等**（复用 `ux_dedup` + `.synced.json` 清单）、**忠实**（复用 `msg_to_entries`）。dedup key 与实时写入一致：turn 用 `msg.id`，tool result 用 `tool_call_id` 或位置回退。每文件一事务。`_purge_old_history`(:446) 按 retention 启动即删旧行。整体异常隔离，绝不阻塞启动。

### 3.8 集成点

- config 读取：`context_config = agent_config.running.light_context_config` → `strategy`、`scroll_config`、`tool_result_pruning_config`。
- 装配：`builder._build_scroll_components`(`builder.py:864`) → 在 agent 构造处挂 `context_manager=(scroll.context_manager if scroll else None)`；若 `scroll is None` 则 `_context_manager=None`（原生压缩）。
- Recall 工具注入：`_append_scroll_recall_tools`(`builder.py:984`) 总注册结构化 `recall_history`，仅当 `_scroll_repl_runnable` 成立才注册 `recall_history_python`（否则降级）。降级判定 `_scroll_recall_runnable`(:905)：governor 存在即可；否则需 `QWENPAW_ALLOW_UNSANDBOXED_RECALL` 环境变量 **且** `scroll_config.allow_unsandboxed` 双开，否则 `scroll=None` 退回原生。
- 系统提示：`ScrollContextContributor`(`prompt_contributors.py:315`, priority=86) 在 `strategy=="scroll"` 时注入 `build_scroll_system_prompt(language)`(`scroll/prompt.py:191`)，教模型写 `⟦ … ⟧` headline、读 `[context compressed]` 地图、用 `recall_history` 三类 op、"召回优先于猜测"。
- 运行时委托：`QwenPawAgent` 持 `_context_manager`，在 `recover_from_context_overflow`/`compress` 等钩子委托 `ScrollContextManager`；`on_save` 由中间件触发持久化。

---

## 4. 工作记忆（Working Memory）

### 4.1 本质：不是单独存的一份，而是 `agent.state.context`

QwenPaw 模型推理吃的就是 `agent.state.context` 这个消息列表。`ContextManager` **不在内存另存一份"工作记忆"**——它管的是这个列表的「持久化 / 压缩重建 / 加载恢复」。所以**工作记忆窗口 = 此刻 `agent.state.context` 里的消息**，是短期记忆 + 长期记忆 + 折叠索引的实时拼装视图，随一次推理生灭，不单独落库。

### 4.2 窗口由 4–5 类块拼成（顺序：左→右）

1. **`memory` 占位块 `<system-info>`（窗口最前）**：被折叠掉的历史的"索引/摘要视图"（`_rebuild_context`，`manager.py:1784` 渲染 `_index.render()` + `_continuation_summary.render_background()`，拼成 `UserMsg(name="memory")`），再 `[placeholder] + tail` 重组窗口（L1824）。合成消息，记在 `_synthetic_ids`，压缩时 `real()`（L509）过滤。
2. **近期对话原文 tail**：真实 user/assistant/tool 最近几轮。`split_for_compression`（L514）分 `to_compress`(中段) 与 `to_reserve`(tail)，tail 保留，当前 active turn（`_active_turn_tail`，L529）原序拼最后。
3. **`recall_history` 返回（模型按需）**：模型主动调 `recall_history` 后，Scroll 从 `history.db` 捞回的早期原文；有 `_recall_loop_guard` 防回声循环。
4. **`auto_memory_search` 结果（长期记忆）**：`MemoryMiddleware.on_model_call`（`middlewares.py:71`）——新用户 turn 时调 `auto_memory_search`(L86)，结果 `memory_msgs` 追加进 `input_kwargs["messages"]` **和** `agent.state.context`(L97-105)。
5. **system prompt 记忆片段**：`on_system_prompt`(L58) 把 `memory_manager.get_memory_prompt()` 追加到系统提示末(L64-68)。长期记忆"静态提示"入口，与 (4) 互补。

### 4.3 三层记忆在此交汇

一次推理窗口里三类记忆同时在线：
- **短期记忆**：tail 原文 + recall 捞回的早期原文（都来自 `history.db`）
- **长期记忆**：`auto_memory_search` 结果 + system prompt 记忆片段（来自 `BaseMemoryManager`）
- **折叠索引**：`memory` 占位块（短期记忆被压缩后的摘要视图）

### 4.4 关键代码锚点

| 位置 | 作用 |
|------|------|
| `manager.py:1784` `_rebuild_context` | 窗口重组成 `[placeholder] + tail` |
| `manager.py:508` `real()` | 过滤合成消息 |
| `manager.py:514` `split_for_compression` | 分 tail / middle |
| `manager.py:573` `_index_evicted` | 折叠历史进索引 |
| `middlewares.py:58` `on_system_prompt` | 系统提示注入长期记忆 |
| `middlewares.py:71` `on_model_call` | `auto_memory_search` 注入窗口 |

---

## 5. 接入层：`MemoryMiddleware`

文件：`agents/middlewares.py`，`MemoryMiddleware(MiddlewareBase)`，由 `BaseMemoryManager.build_middlewares()` 注入。仅负责**生命周期级**记忆行为（工具注册归 toolkit 构造）。

| 钩子 | 行为 |
|------|------|
| `on_system_prompt` | 把 `memory_manager.get_memory_prompt()` 追加到系统提示（去重、空则跳过） |
| `on_model_call` | 非自动化请求时，取最新外部 user query；若该 turn 未检索过则调 `auto_memory_search`，把合成记忆消息并入 `messages` 与 `context` |
| `on_reply` | 非自动化时，按 turn marker 累计 pending；每满 `get_auto_memory_interval()` 轮触发一次 `_flush_auto_memory` |
| `on_compress_context` | 若 `summarize_when_compact` 且 pending 且将触发压缩，则先 flush auto_memory 再压缩 |

关键细节：
- `_is_automation_request`：请求 `source` ∈ `_AUTOMATION_MEMORY_SKIP_SOURCES`（如 cron）时**完全跳过**记忆检索/抽取，避免无人值守任务污染记忆或空耗 token。
- `get_auto_memory_interval()`：取 `memory_manager.get_auto_memory_interval()`（remelight 默认 5、adbpg 固定 1、none 为 0）。
- `_auto_memory_turn_state`：转发到基类 `get_auto_memory_turn_state(session_id)`，TTL 24h。
- `on_compress_context` 内 `_will_compress_context`：用 `agent.model.count_tokens` 估算，阈值 = `cfg.trigger_ratio * context_size`；若 agent 自定义 `should_compress` 则优先用其判定。

---

## 6. Proactive（主动记忆，简述）

`agents/memory/proactive/`（`__init__.py`、`proactive_trigger.py`、`proactive_responder.py`、`proactive_types.py`、`proactive_prompts.py`、`proactive_utils.py`）实现"基于用户兴趣话题的主动触达"，依赖 ReMe 的 `proactive`/`auto_dream` job 产出的兴趣话题（`interests.yaml`）。因与 `react_agent` 存在循环依赖，采用 `memory/__init__.py` 中的 `__getattr__` **运行时懒加载**导出（`_PROACTIVE_EXPORTS`），`TYPE_CHECKING` 块仅做静态分析。

---

## 7. 配置速查（`config.py`）

默认后端 `memory_manager_backend = "remelight"`（`config.py:1568`）。相关配置类：

### 7.1 `ReMeLightMemoryConfig`（`config.py:669`）
| 字段 | 默认 | 说明 |
|------|------|------|
| `metadata_dir` | `mem_metadata` | ReMe 持久状态子目录 |
| `session_dir` | `mem_session` | 源对话日志（auto-memory 用） |
| `mem_session_dir` | `mem_agent` | ReMe 内部记忆 agent 会话 |
| `resource_dir` | `resource` | 外部资源 |
| `daily_dir` | `memory` | 每日记忆（vault） |
| `digest_dir` | `digest` | 摘要记忆 |
| `summarize_when_compact` | `True` | 压缩时是否触发记忆总结 |
| `inbox_push_enabled` | `True` | 把 auto_memory/dream/resource 结果推 inbox |
| `auto_memory_interval` | `5` | 每 N 轮自动记忆；1=每轮；None/≤0 关闭（**建议别太小，会高 token 消耗**） |
| `dream_cron_enabled` | `True` | 开启 dream 整理任务 |
| `dream_cron` | `0 23 * * *` | dream cron（启动随机延迟 0–60s） |
| `auto_memory_search_config` | `AutoMemorySearchConfig` | 回帖前自动检索配置 |
| `embedding_model_config` | `EmbeddingModelConfig` | 嵌入模型 |

### 7.2 `EmbeddingModelConfig`（`config.py:609`）
`backend`(openai)、`api_key`、`base_url`、`model_name`、`dimensions`(1024)、`enable_cache`(True)、`use_dimensions`(False)、`max_cache_size`(10000)、`max_input_length`(8192)、`max_batch_size`(10)。

### 7.3 `ADBPGMemoryConfig`（`config.py:647`）
`rest_base_url`、`rest_api_key`、`memory_isolation`(True)、`search_timeout`(10.0)、`auto_memory_search_config`(enabled=True, max_results=3)。

### 7.4 `LightContextConfig`（`config.py:946`，默认 `strategy="scroll"`）
`strategy`(native\|scroll)、`dialog_path`(`dialog`)、`token_count_estimate_divisor`(4，范围 2–5)、`context_compact_config`、`tool_result_pruning_config`、`scroll_config`、`visual_compact_config`。

### 7.5 `ScrollContextConfig`（`config.py:857`）
`db_filename`(`history.db`)、`repl_timeout_s`(300)、`history_retention_days`(30，0=永久)、`allow_unsandboxed`(False，危险逃生口)、`offload_dialog`(False)。

### 7.6 `ToolResultPruningConfig`（`config.py:779`）
`enabled`(True)、`pruning_recent_n`(2)、`pruning_old_msg_max_bytes`(3000)、`pruning_recent_msg_max_bytes`(50000)、`offload_retention_days`(30)、`tool_results_cache`(`tool_results`)、`exempt_file_extensions`([.md])、`exempt_tool_names`([chat_with_agent])。

### 7.7 `ContextCompactConfig`（`config.py:748`）
`enabled`(True)、`compact_threshold_ratio`(0.8)、`reserve_threshold_ratio`(0.1)。

---

## 8. 数据写入与生命周期总结

| 数据 | 存储位置 | 写入时机 | 生命周期 |
|------|----------|----------|----------|
| 长期记忆（每日笔记/摘要/兴趣） | `workspace/memory/*.md`、`digest/*.md`、`interests.yaml` 等（ReMe vault） | `auto_memory` / `dream` / `auto_resource` job | 持久化（磁盘） |
| 记忆索引 | ReMe `file_store`（本地 embedding_store + BM25） | 文件变更 watch / `reindex` | 持久化 |
| 云端记忆 | ADBPG REST（AnalyticDB） | 每轮 `auto_memory`（fire-and-forget） | 云端持久 |
| 会话逐字历史 | `workspace/history.db`（SQLite WAL） | Scroll write-through（`on_save`） | 持久化，`history_retention_days` 滚动清理 |
| 驱逐占位 | 当前上下文内的 `[context compressed]` EvictionIndex 块 | `compress` 时 | 仅当次上下文 |
| 续接摘要 | 当前上下文内 `<system-info>` + `ContinuationSummary` | 压缩时模型更新 | 随上下文 |
| 摘要任务状态 | 内存 `_summary_task_info` | `add_summarize_task` | 进程内（最多 100 条历史） |
| auto-memory turn 状态 | 内存 `_auto_memory_turn_states` | middleware 节拍 | 24h TTL |
| 工作记忆窗口 | `agent.state.context`（不落库） | 每次模型调用实时拼装 | 随一次推理生灭 |

**关键写入一致性**：实时 Scroll write-through 与 `sync.py` 启动同步共用同一套序列化（`msg_to_entries`）与 dedup key（`msg.id` / `tool_call_id`），保证幂等、历史可追溯。

---

## 9. 设计要点与"坑"

1. **三层记忆分工明确**：工作记忆（窗口拼装）= 短期记忆（Scroll 逐字 history.db）+ 长期记忆（BaseMemoryManager 提炼索引）的实时并集；短期管"这次聊了什么原话"，长期管"关于这个人/事我沉淀了什么结论"。
2. **"上下文压缩"是短期记忆的子能力，不是独立记忆**：压缩掉的原文仍留在 `history.db`，可 `recall_history` 捞回；它和存储/召回/续接并列属于 Scroll。
3. **后端可插拔 + 失败优雅降级**：`memory_registry` 工厂 + `optional=True` 服务，ReMe 导入失败/ADBPG 缺配置时记忆静默禁用，不阻塞 workspace 启动；`HistoryStore` 损坏则 quarantine 后重建空库（`degraded` 标志可读）。
4. **自动化请求跳过记忆**：cron 等 `source` 自动化来源在 middleware 与 auto_memory 层被整体跳过，避免无人值守任务污染长期记忆、空耗 token。
5. **auto_memory_interval 别太小**：remelight 默认 5；设为 1–3 会高频跑 LLM 抽取，token 消耗与后台负担陡增（配置注释明确警告）。
6. **embedding 未配置要显式清空**：否则 ReMe 的 LocalFileStore 仍会去找默认 embedding_store 导致报错（`_apply_embedding_config` 的 empty-path 处理）。
7. **Scroll fail-closed 多处**：sandbox 缺失且未开 `allow_unsandboxed` → recall REPL 返回 `DENIED`；summary 解析/校验失败 → 保留上一值；db 损坏 → quarantine 后重建；`HistoryStore` write-through 失败 → `degraded=True` 但主流程继续。
8. **session_id 文件名安全**：channel id 含 `:` 等非法字符，ReMe 用 base64/sha256 编码，映射确定。
9. **合成记忆消息要剔除**：auto_memory_search 注入的 assistant tool 消息带特殊元数据，持久化前必须 `_messages_without_auto_memory_search` 清洗，否则污染真实历史。
10. **recall 防回声（两层）**：① recall 工具自身轮次（`_RECALL_TOOL_NAMES`）不入 FTS，避免模型读记忆的查询自污染后续 search；② `_active_turn_floor` 排除"最新真实 user 请求之后"的 seq；`RecallLoopGuard` 按 `turn_id+generation` 原子拒绝同页重复 recall。
11. **跨会话记忆恢复**：Scroll 的 `history.db` + `ContinuationSummary.covered_seq` 让压缩上下文可在新会话重建；`sync.py` 把启用前的 `sessions/*.json` 无损导入（复用 `ux_dedup` 幂等），保证历史可 recall。
12. **ReMe 复用主模型**：`_update_qwenpaw_model` 把 QwenPaw 活跃模型注入 ReMe 的 `as_llm`，避免重复配置与 key 不一致。
13. **EvictionIndex 是"地图"不是"丢失"**：分层 odometer 折叠到 tier 顶仍保留每个 block 的精确 `seq` span，模型随时 `expand(lo, hi)` 精确取回原文；KV-cache 友好的前缀设计使常规驱逐不破坏缓存前缀。
14. **`dedup_key` 语义**：`NULL` 永不幂等；turn 用 `msg.id`、tool result 用 `tool_call_id`——resume 重放已恢复窗口时靠它避免复制行，是实时写入与 `sync.py` backfill 一致性的基石。

---

*文档基于源码静态阅读整理，行号/字段以阅读时为准，后续重构可能变动。记忆系统横跨长期记忆（ReMe/ADBPG）、上下文滚动（Scroll）、生命周期接入（MemoryMiddleware）三大块，建议结合 `docs/cron_subsystem.md`（cron 中 `dream` 即调用 `workspace.memory_manager.dream()`）一起理解。*
