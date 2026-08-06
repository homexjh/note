# ReMe 记忆系统：完整设计与实现级解析

> 基于 ReMe v0.4.1.4 源码实测（2026-08-05，`reme/__init__.py:3`）。所有路径、schema、配置默认值、prompt 规则均为真实代码读取结果，非概念推测。引用格式 `文件:行号`，可在仓库中直接核验。
> 仓库根目录以下文简称 `$REME`；运行时工作区默认为 `<项目>/.reme/`（`reme/schema/application_config.py:35`）。

---

## 0. 一句话总览

ReMe 是一个 **local-first、文件原生（file-native）的 Agent 记忆层**：把会话与外部资源逐级提炼为「原文 JSONL → 日记卡片 → 长期 digest」三层 Markdown 记忆，再用 **BM25 + 向量 + wikilink 关系图** 三路融合索引支撑召回；整个生命周期由声明式的 **Job（Step 流水线）** 驱动——捕获（auto_memory / auto_resource）、索引（watcher）、巩固（auto_dream，每晚 23:00）、召回（search / traverse / proactive）各是一条 Job。

核心原则：**用户拥有的 Markdown 文件是唯一真相来源；索引、向量、图全部是 `metadata/` 下可重建的派生物**（`AGENTS.md` Project Principles）。

---

## 1. 数据流全景

```
对话 / 外部文件
   │
   ▼ auto_memory / auto_memory_cc / auto_resource（捕获 Job）
① 原文层 L0 ── session/dialog/<session_id>.jsonl（一字不改，剥离 tool_result）
   │            resource/YYYY-MM-DD/<原材料>
   ▼ LLM 提炼（Agent + 受限工具）
② 日记层 L1 ── daily/YYYY-MM-DD/<主题>.md（frontmatter 携 source_conversation 回链）
   │            daily/YYYY-MM-DD.md（自动生成的当日索引页）
   ▼ index_update_loop watcher：切 chunk → BM25 / 向量 / wikilink 图（索引）
   │
   ▼ dream_cron（每天 23:00）：extract → integrate → topics → finish（巩固 Job）
③ 长期层 L2 ── digest/{personal,procedure,wiki}/<slug>.md（去重合并、互链成图）
   │            daily/YYYY-MM-DD/interests.yaml（当日兴趣主题）
   ▼
④ 召回 ── search（BM25⊕向量 RRF⊕图扩展）/ node_search / traverse / proactive
```

横向还有两条支撑线：

- **派生状态层**：`.reme/metadata/*.jsonl.zst`——chunk+向量、BM25 索引、wikilink 图、catalog，全部可 `reme reindex` 从源文件重建；
- **接入层**：`reme start` 起 HTTP/MCP 服务，宿主 Agent 通过 `skills/reme_memory`（SKILL.md + CLI）读写记忆，Claude Code 走 `plugins/reme` 插件 + `auto_memory_cc`。

---

## 2. 运行时工作区与配置

### 2.1 目录结构（README §Directory Structure + `application_config.py:35-42` 实测）

```text
<workspace_dir>/            # 默认 .reme/
├── metadata/               # 派生状态：索引、图、catalog（可重建，zstd 压缩 JSONL）
├── session/                # L0 原始会话
│   └── dialog/<session_id>.jsonl
├── resource/               # L0 外部原材料
│   └── YYYY-MM-DD/<resource>.<ext>
├── daily/                  # L1 轻度加工记忆
│   ├── YYYY-MM-DD.md       # 当日索引页（refresh_day_index 自动重建）
│   └── YYYY-MM-DD/
│       ├── <session_event>.md    # 会话提炼卡
│       ├── <resource_stem>.md    # 资源解读卡
│       └── interests.yaml        # dream 产出的兴趣主题
└── digest/                 # L2 长期记忆三桶
    ├── personal/           # 身份、偏好、约定、avoid-rule
    ├── procedure/          # 可复用流程 / runbook
    └── wiki/               # 通用知识（兜底桶）
```

### 2.2 关键应用配置（`application_config.py` 实测默认值）

| 字段 | 默认 | 作用 |
|---|---|---|
| `workspace_dir` | `.reme` | 运行时根目录 |
| `metadata_dir` | `metadata` | 派生状态子目录 |
| `session_dir` / `dialog_dir` | `session` / `session/dialog` | 会话原文 |
| `resource_dir` / `daily_dir` / `digest_dir` | `resource` / `daily` / `digest` | 三层记忆目录 |
| `timezone` | `Asia/Shanghai` | 日记日期、dream 日期推断 |
| `language` | `""` | LLM 交互语言（影响 prompt 选 zh/en 模板） |

### 2.3 组件配置（`reme/config/default.yaml` 实测要点）

- `as_llm.default`：OpenAI 兼容后端，`LLM_MODEL_NAME` 默认 `qwen3.7-plus`，`context_size: 200000`，`max_retries: 3`，`stream: true`（`default.yaml:648-660`）；
- `agent_wrapper.default`：AgentScope ReAct，`max_iters: 30`，上下文压缩 `trigger_ratio: 0.8 / reserve_ratio: 0.1 / tool_result_limit: 50000`；另有 `claude_code` / `codex` 后端（`default.yaml:662-695`）；
- `as_embedding` / `embedding_store`：**默认整体注释关闭**，`file_store.embedding_store: ""`（`default.yaml:633-647, 740`）；
- `keyword_index.default`：BM25 + regex 分词（可换 jieba）（`default.yaml:729-732`）；
- `file_graph.default`：local（纯 Python）；另有 neo4j 后端；
- `file_chunker`：markdown（AST 切分，`embed_toc: true`，`max_ast_sections: 100`）/ json / jsonl / default（txt、log）（`default.yaml:711-727`）。

---

## 3. 记忆分层详解

### 3.1 L0 原文层：先保住不可再生的数据

**auto_memory 的第一步不是总结，而是落盘原文**（`reme/steps/evolve/auto_memory.py:154-210`）：

- 路径：`session/dialog/<session_id>.jsonl`；按消息 `id` 合并（新旧取并集）、按 `created_at` 排序；前缀一致则增量 append，否则整体重写；
- **落盘前剥离两类内容**（`_sanitize_msg_for_save`，`auto_memory.py:20-36`）：
  - `tool_result` 块——工具结果里常含"检索出来的旧记忆"，留着会让它在未来 auto_memory 中**伪装成用户亲口提供的上下文**（代码注释原话）；
  - base64 数据块——避免巨型二进制进原文；
- 时间戳归一：支持 `time_created / timestamp / createdAt / timeCreated / created_time` 等别名映射到 `created_at`（`auto_memory.py:39-56`），并从消息时间推断日记归属日期（`_messages_day`，取最大日期）。

**设计意图**：原始数据不可再生，必须先保住；上层一切记忆都可从它重建。防污染在这一步就开始了。

**resource/** 是另一类 L0：用户丢入的外部文件（md/txt/json/jsonl/csv/yaml/html，`default.yaml:23`），由 `resource_watch_loop` 监听。

### 3.2 L1 日记层：LLM「像人一样」挑重点

auto_memory 落盘原文后，调起一个 Agent 写当日记忆卡（`auto_memory.py:243-388` + prompt 模板 `reme/steps/evolve/auto_memory.yaml`）：

**查找已有笔记**（`_find_session_note`，`auto_memory.py:98-113`）：先按 frontmatter `session_id` 匹配，再按 `source_conversation` wikilink 兜底——**一个 session 一天只对应一条笔记**。

**create / update 两分支，工具权限严格收敛**：

| 分支 | 可用工具 | 约束 |
|---|---|---|
| 新建 | 仅 `daily_write` | 一次性创建；prompt 要求先"跳过检查"（纯寒暄不写；模棱两可默认写——"丢失记忆比多写一条更糟"） |
| 更新 | `read` / `edit` / `frontmatter_update` / `write` | 通过 `injected_job_kwargs={"_allowed_paths": [note_path]}` **限制只能操作这一个文件**（`auto_memory.py:319-320`） |

**更新合并规则**（`auto_memory.yaml:210-212`）：时间线/历史条目**只追加不删除**；当下状态类条目（进度、卡点、下一步）**整段重写**反映最新快照；其余合并去重。`edit` 反复失败可退回 `write` 全量重写。

**frontmatter 契约**（`auto_memory.yaml:19-23`）：`name` = 简洁稳定的主题文件名 stem（不含日期）；`description` = 详尽总结（"notes"这类模糊描述不被接受）；`status` 是下游保留字段，**永不设置**。

**写后处理**：按 frontmatter `name` 重命名文件（`move` Job 会全工作区改写 `[[旧名]]`→`[[新名]]`，见 §5.3）；刷新当日索引页 `daily/<date>.md`（`refresh_day_index`）。

**auto_resource**（`reme/steps/evolve/auto_resource.py`，562 行）同理：watcher 捕获 resource 变更批次 → Agent 解读成带 `derived_from` 来源链接的日记卡。

### 3.3 L2 长期层：auto_dream「做梦」巩固

每晚 `dream_cron`（cron 表达式 `"0 23 * * *"`，`default.yaml:55-69`）顺序执行四个 Step；也可随时 `reme auto_dream` 手动触发。

#### ① dream_extract（`reme/steps/evolve/dream/extract.py`）

- 扫描最近 `scan_days`（默认 2）天的 daily 目录，与 dream catalog 中的 `st_mtime` 比对，**只处理变化的文件**（`extract.py:69-75`）；已删除的文件从 catalog 清除；
- 把变化文件打包（`pack_paths`）交给 LLM 全局提炼，产出结构化 `DreamExtractOutput`（`reme/schema/dream.py`）：

```python
class DreamUnit(BaseModel):      # 一条跨文件记忆单元
    name: str                    # kebab-case 抽象名
    bucket: DreamBucketEnum      # personal / procedure / wiki（未知值路由到 wiki）
    summary: str                 # 带证据指针的摘要
    paths: list[str]             # 来源文件

class DreamTopic(BaseModel):     # 一条兴趣主题候选
    title: str; reason: str; evidence: str
    keywords: list[str]; paths: list[str]
```

- 上限 `max_units`（默认 5）；无变化则跳过；无 LLM 配置则明确报错而不是静默（`extract.py:105-109`）。

#### ② dream_integrate（`dream/integrate.py` + `integrate.yaml`）

逐个 unit 调起整合 Agent，按 bucket 使用**三套不同的 system prompt**（`integrate.yaml`，实测 510 行）。三桶的共同契约：

| 规则 | 内容 |
|---|---|
| 抽象层定位 | "Digest 不是材料复刻"，正文 50–200 词，细节留在 daily/resource |
| 先召回 | 第一步必须 `node_search`（limit 20–30）跨桶召回，分类为 same_abstraction / related / unrelated |
| 四种动作 | **CREATE**（新建）、**CORROBORATE**（再次印证，追加 derived_from）、**REFINE**（补充边界/例外/步骤）、**CORRECT**（冲突修正，可内联 `> note: contradicted by [[path]]` 标注） |
| 只增不删 | UPDATE 必须是 additive：永不删除已有 wikilink 与 derived_from；"默认多织，而不是少织" |
| 溯源 | `derived_from:: [[daily/<date>/<session>.md]]` 至少一条；"纯文本 provenance 不算，只有 wikilink 会在未来更新中保留下来" |
| 单目标 | 每次 session 只处理一个 target，"不要顺手编辑其他节点" |

三桶的差异（正文形态）：

- **personal**：短规则而非传记——Rule/fact 一句 + `Why:` + `How to apply:`；偏好类**一条偏好一个节点**（下游搜索更易命中）；不凭空加例外、不软化硬偏好；
- **procedure**：runbook 而非 recap——Trigger 一行、动词开头的 Steps、前置条件、失败模式；
- **wiki**：百科风格——首行一句话定义，属性/子主张/区分/一行例子；无法分类时的兜底桶。

返回结构化 `IntegrateOutcome`：`action ∈ {CREATE, CORROBORATE, REFINE, CORRECT}` + `target_path` + `note`（`dream.py` IntegrateOutcome）。

#### ③ dream_topics（`dream/topics.py` + `topics.yaml`）

从候选中选出最终兴趣主题写入 `daily/<date>/interests.yaml`：`topic_count` 默认 3；`topic_diversity_days` 默认 7（回看 7 天 interests.yaml 避免重复）；保留当天已有主题除非明显重复；偏好"具体、反复出现、可行动"的兴趣，拒绝"复述文件名"的伪主题。

#### ④ dream_finish

持久化 dream catalog（记录各文件 mtime，供下次增量比对），汇总运行状态。

### 3.4 召回层：记忆怎么被用回来

| Job | 用途 | 关键参数（`default.yaml` 实测） |
|---|---|---|
| `search` | 混合检索 | `vector_weight: 0.7`、`candidate_multiplier: 5.0`、`expand_links: true`、`max_links_per_direction: 10` |
| `node_search` | digest 节点召回（dream integrate 的去重工具） | `vector_weight: 0.7`，limit 默认 20 |
| `traverse` | 沿 wikilink 图游走 | `depth` 默认 1，`direction: forward/backward/both` |
| `proactive` | 读 `daily/<date>/interests.yaml` 暴露兴趣主题 | `include_content` 默认 true |

详见 §7。

---

## 4. 文件抽象：四层 Schema（`reme/schema/` 实测）

| Schema | 文件 | 关键字段 | 角色 |
|---|---|---|---|
| `FileFrontMatter` | `file_front_matter.py` | `name`、`description` + **任意扩展键**（`extra="allow"`） | frontmatter 解析，schema 演进不破坏旧文件 |
| `FileLink` | `file_link.py` | `source_path`、`target_path`、`target_anchor`、`predicate` | 一条 wikilink 边（`extra="forbid"`） |
| `FileNode` | `file_node.py` | `path`、`st_mtime`、`links[]`、`chunk_ids[]`、`front_matter` | 一个文件 = 一个图节点 |
| `EmbNode` | `emb_node.py` | `id`（uuid4）、`text`、`embedding`（**float16** ndarray）、`metadata` | 向量节点基类 |
| `FileChunk` | `file_chunk.py` | `path`、`start_line`/`end_line`（1-based 闭区间）、`scores{}`（按检索阶段记分） | 检索单元，继承 EmbNode |

`FileChunk.set_hash_id()`（`file_chunk.py:21-25`）：id = `hash(path + start_line + end_line + text)`——**内容不变则 id 不变**，增量索引天然幂等。

---

## 5. Wikilink 体系：单一事实源 + 会"生长"的图

### 5.1 语法契约（`reme/utils/wikilink_handler.py`，387 行，模块 docstring 实测）

- 支持 `[[target]]`、`[[target#anchor]]`、`[[target|alias]]`、`![[图片]]`、Dataview 行级 `predicate:: [[target]]` 与行内 `[predicate:: [[target]]]`；
- **目标按字面取**：`[[X]]` → `target="X"`，不补 `.md`、不做 basename 短链搜索、不做 folder-note 展开；推荐写法是带扩展名的工作区相对路径 `[[topics/x.md]]`；
- predicate 推断优先级：行内括号 > 行级 Dataview > 无（`_predicate_for`，`wikilink_handler.py:229-249`）；
- 边按 `(target_path, predicate, anchor)` 去重。

### 5.2 虚拟边与自动晋升（`reme/components/file_graph/local_file_graph.py`）

`LocalFileGraph` 维护两张反向邻接表：

- `_inverse`：指向**真实存在**文件的边；
- `_pending`：指向**尚不存在**文件的边（虚拟目标）。

晋升/降级规则（`local_file_graph.py:80-103`）：

- 目标文件被创建（`upsert_nodes`）→ `_pending` 中的边自动**晋升**为真实边；
- 目标文件被删除（`delete_nodes`）→ 真实边**降级**回 `_pending`，不丢信息。

**这意味着可以先写 `[[尚不存在的概念]]`，知识库随文件出现自动接通**——"LLM Wiki"用法的基础，也是"自组织而非预定义 schema"的落地机制。查询按 `LinkScopeEnum`（REAL / VIRTUAL / ALL）过滤。

### 5.3 引用完整性维护

- **move**（`default.yaml:464-488`）：重命名后 `retarget_links` 从图的反向索引找候选源（**不做全盘 fs 扫描**），把入向 `[[src]]` 字面改写为 `[[dst]]`，anchor/alias/图片标记原样保留（`wikilink_handler.py:329-387`）；`retarget` 默认 true；
- **delete**（`default.yaml:490-502`）：删除前 `find_inbound` 统计仍指向目标的引用清单（按文件分组计数）返回给调用者，便于清理（`wikilink_handler.py:272-326`）。

---

## 6. 存储与索引：三位一体的 FileStore

### 6.1 组件组合（`reme/components/file_store/local_file_store.py:42-69`）

`LocalFileStore` = `file_graph`（**必选**）+ `keyword_index`（BM25）+ `embedding_store`（可选）。构造时校验：embedding 与 keyword **至少一个**，`file_graph` 必须存在。依赖通过 `bind()` 声明（`base_component.py:113-133`）——**空名字短路返回 None**，这是 `embedding_store: ""` 即关闭向量的机制。

### 6.2 持久化：zstd 压缩 JSONL

- chunk 存 `metadata/file_chunks_<name>_v1.jsonl.zst`（`local_file_store.py:68`）；图存 `metadata/<name>.jsonl.zst`（`local_file_graph.py:21`）；向量以 **float16 → base64** 内联在 chunk 行里（字段 `_embedding_f16_b64`，`local_file_store.py:25, 219-223`）；
- 启动 `load()` 全量读入内存，关闭 `dump()` 写回；格式透明、可 diff、可 `reindex` 全量重建。

### 6.3 向量的生命周期

1. **写入时**：watcher → `update_index_step` 切 chunk → `file_store.upsert()` → `_embed_pending()` 批量算向量（`local_file_store.py:467-477, 540`）；
2. **启动时**：`_start_embedding_backfill` 后台任务补齐缺向量的 chunk，**不阻塞启动**（`local_file_store.py:231-288`）；
3. **换模型时**：加载发现维度不匹配的旧向量由 `_drop_stale_embedding` 丢弃重算（`local_file_store.py:116-131`）；
4. **运行时故障**：`_disable_embedding` 把 embedding_store 置 None 并记错误日志，检索自动退回 BM25-only，服务不崩（`local_file_store.py:102-107`）；
5. **未配置时**：`vector_search` 首行短路 `return []`（`local_file_store.py:603-605`）。

### 6.4 Markdown 切分器（`reme/components/file_chunker/markdown_file_chunker`）

- 无 AST 先数标题：超过 `max_ast_sections`（100）退化为纯文本字节切块；
- 否则 mistletoe AST → `MdNode` 树（section 按标题级别嵌套）→ 递归切块并在父节点合并相邻小子树；
- 叶子块（表格/代码/列表/段落）沿内部边界切分，每片标注 `[Part X/N]`；
- `embed_toc: true` 时 chunk 只携带**祖先标题面包屑**（兄弟标题不重复进每个 chunk，避免文档大纲二次膨胀）；
- wikilink 提取委托给 `WikilinkHandler`（单一事实源）。

---

## 7. 检索：RRF 融合的混合搜索（`reme/steps/index/search.py`，277 行）

### 7.1 执行流程

1. **并行两路**：`asyncio.gather(vector_search, keyword_search)`；权重 0/1 边界时自动退化单路（`search.py:215-225`）；
2. **RRF 融合**（`_rrf_merge`，`search.py:46-78`）：`contrib = weight / (60 + rank)`，向量默认权重 0.7、关键词 0.3，按 chunk.id 合并，每路原始分与融合分都记入 `scores{}`；
3. **日期过滤**：`start_date/end_date` 归一化为 `YYYY-MM-DD` 后与路径中提取的日期做字典序比较（非法日期记 warning 并忽略，`search.py:184-206`；`_matches_search_filter`，`local_file_store.py:721-751`）；`strict_date_filter` 可要求无日期路径直接排除；
4. **tool_context 去重**：同一 Agent 上下文（`tool_context_id`）24h TTL 内已返回过的 chunk 不再重复返回，统计见 `metadata.dedup`（`search.py:94-135`）；状态存 `app_context.metadata["tool_contexts"]`；
5. **链接扩展**：对命中路径沿 wikilink **双向扩展**（`expand_links`，`reme/utils/link_expansion.py`），邻居附 name/description meta 与 predicate/anchor 边描述，渲染为 `outlinks (n): → path ...` 块；
6. **输出**：每条结果带 `path:start-end [score=… vector=… keyword=…]` 定位头。

### 7.2 上限与默认值

`limit` 默认 5（可用环境变量 `REME_SEARCH_LIMIT` 覆盖）；候选上限 200；`candidate_multiplier` 5.0；单方向链接扩展上限 10。

---

## 8. Job / Step 执行模型

### 8.1 Job = 声明式 Step 流水线

所有记忆行为都是 `default.yaml` 里声明的 Job：`backend`（base / background / cron）+ `steps`（有序 Step 列表）+ JSON Schema `parameters`。后台 watcher Job 用 `init_changes_step`（启动全量扫描）+ `watch_changes_step`（持续监听，带 `dispatch_steps` 分发与 `persist` 控制）。

### 8.2 全部内置 Job 一览（`default.yaml` 实测）

| 类别 | Job | 触发 | 作用 |
|---|---|---|---|
| 后台 | `index_update_loop` | watcher（daily/digest 的 md） | 增量维护 chunk/BM25/图 |
| 后台 | `resource_watch_loop` | watcher（resource 多后缀） | 更新 catalog + auto_resource |
| 后台 | `digest_watch_loop` | watcher | 更新 digest catalog + 变更日志 |
| 定时 | `dream_cron` | cron `0 23 * * *` | 做梦巩固（extract→integrate→topics→finish） |
| 定时 | `optimize_index_cron` | cron `0 2 * * *` | 索引优化 |
| 捕获 | `auto_memory` / `auto_memory_cc` / `auto_resource` | 手动/宿主调用 | §3.1–3.2 |
| 巩固 | `auto_dream` | 手动 | 同 dream_cron 管线 |
| 召回 | `search` / `node_search` / `traverse` / `proactive` | 手动 | §3.4、§7 |
| 文件 | `read` / `write` / `daily_write` / `edit` / `move` / `delete` / `list` / `stat` / `read_image` / `frontmatter_*` / `daily_list` / `daily_reindex` | 手动/Agent 工具 | 工作区文件操作（Agent 的记忆读写工具面） |
| 维护 | `reindex`（清空重建）/ `health_check` / `status` / `help` / `version` | 手动 | 运维 |

### 8.3 Step 无状态原则（`AGENTS.md` Step State Model）

每个 Step 都是无状态的，`BaseJob` 每次调用构建全新实例。状态按生命周期放置：

| 生命周期 | 位置 |
|---|---|
| 单次 Job 执行内共享 | `self.context`（RuntimeContext） |
| 进程级跨调用共享 | `self.app_context.metadata`（窄命名空间键，如 `tool_contexts`；需考虑并发） |
| 重启后仍存活 | 工作区文件 / 专用组件 |
| 禁止 | Step 实例字段存可变状态；`Response.metadata` 当状态存储 |

---

## 9. 组件体系与注册机制

- **注册**：`R.register("backend_name")` 装饰器（`reme/components/component_registry.py`）；组件包必须经 `reme/components/__init__.py` 可达，Step 模块必须经 `reme/steps/__init__.py` 可达——**实现 + 注册 + import 副作用是一个整体变更**（AGENTS.md Change Workflow）；
- **依赖**：`bind(name, BaseCls, default_factory=…)` 返回 `Dependency` 占位符，`start` 时解析；standalone 模式用 `default_factory`，有 ApplicationContext 时从容器解析；
- **可替换后端**（实测存在的注册项）：

| 组件类型 | 后端 |
|---|---|
| service / client | http、mcp、cli |
| job | base、background、cron、stream |
| agent_wrapper | agentscope、claude_code、codex |
| file_graph | local、nx、neo4j |
| file_store | local、faiss |
| file_catalog | local |
| keyword_index | bm25 |
| tokenizer | regex、jieba |
| file_chunker | markdown、json、jsonl、default |
| outbound_proxy | fixed_http、ssh_http |

---

## 10. 服务与宿主接入

### 10.1 进程模型（`reme/reme.py` 实测）

- `reme start` → `ReMe(**kwargs).run_app()` 起服务（默认 HTTP backend，`default.yaml:1-2`）；
- 其他 CLI 动作（`reme search …` 等）→ `call_server()`：**自动探测正在运行的 server 的实际配置**（backend/transport/host/port），客户端按 server 真实启动方式连接（`reme.py:20-59`）；未检测到 server 时退回本地配置；
- 支持 http REST 与 mcp（streamable-http / sse / stdio）两种接入。

### 10.2 宿主 Agent 接入

- **通用**：`skills/reme_memory/SKILL.md`——约定宿主在回答涉及历史/偏好/决策的问题前**先 `reme search`**，会话出现持久事实时调 `reme auto_memory`（带稳定 session_id + messages + memory_hint）；"不要手改工作区文件，除非用户明确要求"；
- **Claude Code**：`plugins/reme/` 插件 + `auto_memory_cc` Job——从磁盘解析 Claude Code 会话 transcript（按 session_id），走同一套日记写入管线；
- **记忆姿态**：SKILL.md 明确"避免存秘密或敏感个人数据，除非用户明确要求"。

---

## 11. 端到端例子：一条记忆的五天

**8月3日上午**，你告诉 Agent："检索融合定了用 RRF；另外我咖啡因过敏。"

1. **捕获**：对话追加进 `session/dialog/sess-a1b2.jsonl`（剥离 tool_result）；
2. **日记**：Agent 写出 `daily/2026-08-03/reme-retrieval-rrf-decision.md`，frontmatter 带 `source_conversation: "[[session/dialog/sess-a1b2.jsonl]]"`；watcher 立刻切 chunk、更新 BM25 与图；下午继续聊 → 同一笔记合并更新（历史追加、状态重写、只动这一个文件）；
3. **当晚 23:00 做梦**：extract 发现该文件 mtime 变化 → 提炼出两个 unit（`user-caffeine-allergy` → personal；`hybrid-search-rrf-fusion` → wiki）；integrate 先 `node_search` 查重——发现已有 `digest/personal/user-dietary-restrictions.md`，选择 **REFINE/CORROBORATE** 合并并互链，而不是新建重复节点；topics 写入 `interests.yaml`；
4. **8月10日召回**：你问"融合方案当时怎么定的" → `search` 并行向量+BM25，RRF 融合命中 digest 节点与日记卡，链接扩展带出关联节点，附 `path:行号` 定位；
5. **主动记忆**：若宿主行动前调 `reme proactive`，会读到最近 interests.yaml 里的"混合检索融合方案"主题。

---

## 12. 与主流 Agent 记忆架构的对照

| ReMe 机制 | 学术/工业界对应物 |
|---|---|
| session/dialog JSONL 原文先落盘 | 事件溯源式持久化；MemGPT/Letta 的 message persistence |
| daily 日记卡（LLM 提炼 + 合并更新规则） | 反射式记忆写入（reflection）；MemGPT archival memory 的文件化变体 |
| auto_dream 夜间巩固（extract→integrate） | **Sleep-time compute**（Letta 2025）；记忆巩固（memory consolidation） |
| digest 三桶 personal/procedure/wiki | 认知科学 declarative vs procedural memory；语义/情景/程序记忆分层 |
| CREATE/CORROBORATE/REFINE/CORRECT 四动作 | A-MEM 的记忆进化（memory evolution）；增量知识库维护 |
| wikilink 图 + 虚拟边自动晋升 | A-MEM / Zep 实体关系图；Obsidian 双链 |
| BM25 ⊕ 向量 RRF ⊕ 图扩展 | 经典混合检索（hybrid retrieval）+ 图增强 RAG |
| derived_from:: 溯源边 + path:line 定位 | 记忆可审计性 / provenance |
| 剥离 tool_result 防自我污染 | 反记忆循环强化（anti self-reinforcement） |
| `reindex` 全量可重建 | 物化视图思想：索引是源文件的派生物 |

---

## 13. 设计哲学总结

1. **文件即真相**：三层记忆全是人可读的 Markdown/YAML/JSONL；索引、向量、图都是 `metadata/` 下可随时重建的派生物——用户拥有数据，系统不锁数据。
2. **先保原文，再谈提炼**：不可再生的原始对话一字不改先落盘；上层记忆全部可从原文+daily 重建。
3. **写入比读取难**：合并规则（历史只追加/状态重写）、四种整合动作、只增不删约束、单目标单文件工具限制——宁慢勿错。
4. **防污染是系统性工程**：落盘剥离 tool_result（防检索结果伪装成用户事实）、dream 按 mtime 增量（防重复提炼）、`status` 保留字段、tool_context 去重（防同一上下文重复召回）。
5. **自组织而非预定义**：frontmatter 开放扩展键、wikilink 虚拟边自动晋升、predicate 词表开放——结构从使用中生长，不靠预先设计的数据库 schema。
6. **优雅降级**：无 embedding 配置 = 纯本地 BM25+图模式；embedding 运行时故障自动退回关键词检索；无 LLM 明确报错而非静默产出垃圾。
7. **声明式装配**：Job/Step/组件全部 YAML 声明 + 注册表发现；行为改动 = 改配置 + 注册新实现，不动框架。

---

## 14. 诚实边界（本文未覆盖/无法从静态代码确认的部分）

- 各 Agent wrapper（AgentScope / Claude Code / Codex）内部的上下文压缩与工具注入细节（属外部库行为）；
- `optimize_index_step` 的具体优化策略实现细节（本文仅确认其 cron 注册与入口）；
- neo4j / faiss 后端的实际行为（默认配置未启用，本文以 local 后端为准）；
- cookbook 目录下 daily_paper / auto_fin 两个示例应用属于"用法示例"而非记忆框架本体，未展开；
- `mem_session_dir` 的实际消费者（配置存在，默认配置中未见 watcher 引用）。
