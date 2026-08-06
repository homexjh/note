# Kimi Work 记忆系统：完整设计与实现级解析

> 基于本机运行时实测（2026-08-05）。所有路径、schema、配置开关均为真实文件读取结果，非概念推测。
> 运行时根目录：`~/Library/Application Support/kimi-desktop/daimon-share/daimon/`（下文简称 `$DAIMON`）。

---

## 0. 一句话总览

Kimi Work 的记忆是一套 **五层持久化体系**：会话日志（wire）→ 按天转录归档（transcripts）→ 夜间"做梦"提炼管线（dream pipeline）→ Markdown 知识库（vault）→ 受限注入的用户画像（about_user.md，≤6000 字符）。外加两类横向记忆：**短期向量记忆**（会话内语义检索）与 **程序性记忆**（Skills）。

---

## 1. 数据流全景

```
用户对话
   │
   ▼
① Sessions 层 ── wire.jsonl 事件流逐轮落盘（可跨进程重启恢复）
   │
   ▼ 内核按天导出
② Transcripts 层 ── memory/transcripts/days/<日期>/conv-*.jsonl（原始原料仓）
   │
   ▼ 每天首次活动触发 dream orchestrator
③ Dream Pipeline ── vault-memory feature：提炼实体/事实 → decorators + lint 刷新布局
   │
   ▼
④ Vault 层 ── Markdown 知识库：entities/（人/项目/地点/概念）+ sections/（六分区）
   │
   ▼ 组装器按 sections.yaml 拼装、剥离注释、≤6000 字符硬上限
⑤ 注入层 ── about_user.md 注入下一轮对话的 prompt 上下文
```

---

## 2. 第①层：会话层（Sessions）—— "活记忆"地基

**路径**：`$DAIMON/runtime/kimi-code/home/sessions/<wd_工作区哈希>/conv-<会话ID>/`

每个会话一个目录，实测结构：

```
conv-e933dd0fb01f673155c4b825/
├── state.json                  # 会话元数据
└── agents/main/wire.jsonl      # 完整事件流（本会话实测 137 行 / 820KB）
```

**state.json 实测字段**：

| 字段 | 作用 |
|---|---|
| `createdAt` / `updatedAt` | 会话生命周期时间戳 |
| `title` / `isCustomTitle` | 标题（可自动生成） |
| `custom.stableSessionKey` | 稳定键，如 `agent:main:main` |
| `custom.conversationKey` | 如 `agent:main:main:conversation:<UUID>`，贯穿转录层 |
| `custom.workTag` | 来源标记，如 `cron`（定时任务产生的会话） |
| `custom.workspacePath` / `workDir` | 绑定的项目工作区 |
| `lastPrompt` | 最后一条用户输入 |

**wire.jsonl 事件类型实测分布**（一个真实调研会话）：

| 条数 | 事件类型 | 含义 |
|---|---|---|
| 89 | `context.append_loop_event` | 上下文追加（对话主体） |
| 12 | `llm.request` | 每次模型调用 |
| 12 | `usage.record` | token 用量记录 |
| 8 | `tools.register_user_tool` | 工具注册 |
| 4 | `tools.update_store` | 工具存储更新 |
| 3 | `config.update` / `tools.set_active_tools` | 配置与激活工具集变更 |
| 2 | `permission.set_mode` | 权限模式切换 |
| 1 | `metadata` / `turn.prompt` / `context.append_message` / `llm.tools_snapshot` | 边界标记 |

**设计要点**：这是事件溯源（event-sourcing）式存储——不是只存对话文本，而是把工具注册、权限变更、配置变化全部入流，因此进程重启后可以**精确恢复**到断点状态继续执行。

---

## 3. 第②层：转录归档（Transcripts）—— 长期记忆的原料仓

**路径**：`$DAIMON/agents/main/memory/transcripts/days/YYYY-MM-DD/conv-<ID>.jsonl`

**每行记录 schema（实测）**：

```json
{
  "ts": "2026-08-03T03:08:00.543Z",
  "role": "user",                      // user / assistant / tool
  "content": "……",
  "meta": {
    "source": "kimi-code-records",
    "agentId": "main",
    "sessionId": "conv-3fc2ea4ef363…",
    "sourcePath": "…/sessions/wd_reme_…/conv-…/agents/main/wire.jsonl",
    "sourceType": "kimi-code:user:text",        // 还有 assistant:text、assistant:tool-result
    "conversationKey": "agent:main:main:conversation:405105f3-…",
    "messageId": "0:user:0",
    "turnId": "0"
  }
}
```

**本机实测规模**：21 个日期桶（2026-06-30 ~ 2026-08-05），共 44 个会话文件，约 7.4 MB。单个会话实测角色分布示例：user 5 条 / assistant 11 条 / tool-result 39 条。

**设计要点**：
- 与第①层相比，这是**清洗后的对话视图**（user/assistant/tool 三角色），剥离了事件流的工程细节；
- `sourcePath` 保留到 wire.jsonl 的回溯指针，可追溯原始事件；
- 按日期分桶正是为了 **dream pipeline 增量扫描**——每晚只需处理"未处理的日期 × 会话"组合。

---

## 4. 第③层：Dream Pipeline —— 记忆的"睡眠整理"

### 4.1 系统工具三件套（SystemList 实测）

| 工具 | 类型 | 说明 | 本机状态 |
|---|---|---|---|
| `dream` | orchestrator | 在共享锁（`system-tools.lock`）下顺序运行所有启用的 feature 工具 | ✅ enabled |
| `vault-memory` | feature | 对未处理的 session/日期转录跑整理轮次，经 decorators + lint 刷新 vault | ❌ disabled |
| `skill-summary` | feature | 回顾近期对话，维护托管技能 | ❌ disabled |

### 4.2 触发机制（实测）

- **每天首次活动触发**：`$DAIMON/agents/main/system-tools/daily-first-activity-maintenance.v1.json` 记录：

```json
{
  "lastTriggeredLocalDate": "2026-08-05",
  "status": "completed",
  "summary": "selectedTargets=3 selectedDays=3 vault-memory=skipped skill-summary=skipped"
}
```

即：今天（2026-08-05）首次活动时扫描出 3 个待处理目标 / 3 个日期，但两个 feature 均被跳过（开关关闭）。

- **运行记录**：`system-tools/dream-last-run.json` → `{"lastRunLocalDate": "2026-08-05"}`。
- **手动补跑**：`vault-memory` 支持 `targetDate: YYYY-MM-DD` 参数，可对指定日期重跑整理。

### 4.3 整理轮次做什么

1. 读取目标日期的 transcripts；
2. 提炼**实体**（人/项目/地点/概念）写入 `entities/`，实体间用 `[[wikilink]]` 互连成图；
3. 更新六个 **sections** 分区文件；
4. 运行**自动归档脚本**：巡检 this_month 的项目条目，最新相关会话 ≥7 天或跨月的移入 earlier_months，跨月归档进 `sections/archived_month/<YM>/this_month.md`（归档容量无限）；
5. 经 **decorators + lint** 校验并刷新 vault 布局；
6. 写 `log.md`（Dream Log）留痕；
7. 按 `sections.yaml` 重新组装 `about_user.md`，更新 frontmatter 的 `last_dream`。

---

## 5. 第④层：Vault —— 核心长期记忆库

**路径**：`$DAIMON/agents/main/memory/vault/`，纯 Markdown（Obsidian 风格，可人工阅读、可版本化）。

```
vault/
├── index.md              # 实体索引
├── about_user.md         # 注入用用户画像（组装成品）
├── log.md                # Dream Log
├── sections.yaml         # 组装配置
├── sections/             # 六个画像分区
│   ├── personal_context.md
│   ├── work_context.md
│   ├── this_month.md
│   ├── earlier_months.md
│   ├── taste.md
│   └── memory_tips.md
└── entities/             # 实体知识图谱
    ├── people/  ├── projects/  ├── places/  └── concepts/
```

### 5.1 sections.yaml —— 组装配置（全文实测）

```yaml
sections:                      # about_user.md 的拼装顺序；改此列表即可重塑画像 schema
  - personal_context
  - work_context
  - this_month
  - earlier_months
  - taste
  - memory_tips

inject_char_cap: 6000          # 组装后正文硬上限（不含 frontmatter），超限组装器直接报错
strip_comments_on_assemble: true   # 组装时剥离 Obsidian %%注释%%（模板可携带操作手册，注入保持精简）
earlier_months_view_count: 3   # earlier_months.md 只展示近 3 个归档月
```

### 5.2 about_user.md frontmatter（实测）

```yaml
---
created: 2026-06-29
last_dream: 2026-06-29
last_avatar: never
chars: 0
tags: [user-profile]
---
```

### 5.3 六个分区的写入规则（模板内嵌 `%%操作手册%%` 逐条译解）

**personal_context（个人背景）**
- 只收**稳定身份事实**：姓名（保留原始文字与变音符号）、国籍、城市（≥市级）、结构性家庭角色。
- **最高信号字段是语言与表达习惯**：简/繁体、中英 code-switch 模式（哪些术语保持英文）、方言语域——因为它决定下个会话用什么语言和语域回复。
- 写入阈值：用户自我介绍，或同一事实跨多个会话出现。
- 自检问题："这条一年后仍然正确且有用吗？"
- 格式：1-3 句短句、用用户母语、像"了解他的朋友"在介绍，禁止标签罗列；末尾元数据行 `[updated:: YYYY-MM-DD] [chats:: <UUID>, ...]`。
- 具体的人/地/事**下推给 entities**，此处只留 `[[wikilink]]`。

**work_context（工作上下文）**
- 1-2 句：职业、雇主、当前聚焦方向、工作理念（如有稳定模式）。
- 主体是指向 projects/places 实体的 wikilinks；具体代码/配置/决策全部下推实体页。
- 丢弃"事件叙述"（如"用户在某会话中纠正了……"），只写**持久事实**。

**this_month（本月活跃项目看板）**
- 每项目一行：`- [[project_wikilink]] 一行状态短语. [chats:: <UUID>, ...]`
- 项目进入活跃期时新增条目；有进展时更新状态短语并追加会话 ID。
- **容量 5~8 条**，超出说明项目已不活跃，应自然掉落。
- 跨月归档**全自动**，模型不需自己管理。

**earlier_months（早前月份）**
- 归档视图，展示近 3 个月（完整归档在 `archived_month/` 下，无界）。

**taste（品味）**
- 只收**持久的审美轴**：喜欢什么 + 拒绝什么 + 一行理由（优先用用户原话 `==quote==`）。
- 稳定句式："user values X" / "user refuses Y because Z"；不写具体事件。
- **明令禁止 Barnum 式心理学概括**（"重视质量""追求严谨"这类适配 80% 用户的描述=无效信息）。

**memory_tips（记忆使用须知）—— 元记忆**
- 这是 Agent 写给未来自己的"记忆姿态校准"：用户如何信任/校验记忆（当场纠正？掠过？显式抽查？）、什么信号代表"这事要真记住"、会话开始时重建身份所需的最小线索。
- 明确标注：**"常常正确地空着"**——没有真实信号就不写，禁止编造。

### 5.4 entities —— 实体知识图谱

- 四类目录：`people/`、`projects/`、`places/`、`concepts/`，每实体一个 md 页。
- 实体间通过正文中的 `[[wikilink]]` 形成边；宿主注入给 memory-widget 的数据契约证实了图结构：

```js
window.DaimonWidget.onVaultData(function (vault) {
  // vault.nodes: [{ id, name, type, tags[], created, lastUpdated, content, linkCount }]
  // vault.edges: [{ source, target }]        // 实体↔实体，来自正文 [[wikilinks]]
  // vault.about_user: { name, aliases[], facts[], topTags[] }
});
```

- 分区的 `%%手册%%` 中反复出现 **"Push to entities"** 原则：分区只留一句话 + 链接，富细节全部沉淀到实体页——这与知识图谱的"轻节点摘要、重实体页"分层一致。

### 5.5 注入：闭环的最后一公里

- 组装器按 sections.yaml 顺序拼接六个分区 → 剥离 `%%注释%%` → 校验 ≤6000 字符 → 写入 about_user.md。
- 下一轮对话启动时，about_user.md 注入 prompt 上下文（配置项 `memory.inject.promptContext`）。
- 引用规则：回答用户时引用记忆须给出 `path:line`，让主人可核验——**可审计性是硬约束**。

---

## 6. 第⑤层：短期记忆与横向记忆

**短期记忆（short-term）**
- 当前会话的轮次做**向量索引**，支持语义检索；过老轮次被**压缩**（摘要化）以节省上下文窗口。
- 配置项实测：`features."memory.extract.shortTerm" = true`（本机开启）。
- 实现细节（embedding 模型、向量库格式）在本地文件中不可观测，属于内核内部。

**程序性记忆（Skills）**
- `$DAIMON/skills/<name>/SKILL.md`（本机 39 个）+ 插件托管技能。
- 存的是"怎么做事"（流程、契约、禁忌），与 vault 的"知道什么"（事实）互补——对应认知科学里 procedural vs declarative memory 的划分。

**未来记忆（Automation）**
- Blueprint Automation 让未来的时间点唤醒 Agent 继续工作；配合通知投递，相当于"指向未来的记忆"。

---

## 7. 功能开关全景（config.json 实测）

```json
{
  "features": {
    "memory": false,                       // 长期记忆总开关
    "memory.inject.promptContext": true,   // 画像注入：开
    "memory.extract.shortTerm": true,      // 短期提取：开
    "memory.dream": false                  // dream/vault 提炼：关
  },
  "runtime": {
    "dream": { "autoTriggerEnabled": false },
    "skillManagement": { "enabled": false }
  }
}
```

**这解释了你机器上的现状**：vault 骨架已于 2026-06-29 建好（目录、模板、配置齐全），但因 `memory.dream=false`，21 天、44 个会话的转录原料**从未被提炼**——entities 四个目录全空、about_user.md 正文 0 字符、index 无条目、Dream Log 空白。今天的维护扫描发现了 3 个待处理目标，但因开关关闭而跳过。短期记忆与画像注入开关是开的，所以管线处于"半成品激活"状态。

---

## 8. 与主流 Agent 记忆架构的对照

| Kimi Work 机制 | 学术/工业界对应物 |
|---|---|
| wire.jsonl 事件溯源会话层 | Letta/MemGPT 的 message persistence；事件溯源模式 |
| transcripts 按天分桶 | recall memory 的原始存储；增量批处理前置 |
| dream pipeline（夜间整理） | **Sleep-time compute**（Letta 2025）；记忆巩固（consolidation） |
| vault entities + wikilink 图谱 | A-MEM / Zep 的实体-关系图记忆 |
| about_user.md ≤6000 字符注入 | MemGPT 的 **core memory blocks**（受限、可编辑、始终驻留上下文） |
| this_month 自动归档（≥7 天） | 记忆驱逐/衰退策略（eviction & decay） |
| 分区 `%%操作手册%%` | 自描述记忆（memory schema 即 prompt）——schema 变更不改模型行为 |
| `memory_tips` 元记忆分区 | 元认知（metacognition）：Agent 对自己记忆机制的反思 |
| 禁 Barnum 原则、"常常正确地空着" | 反幻觉写入策略：宁可空，不可编造 |
| `path:line` 引用规则 | 记忆可审计性 / provenance |

---

## 9. 设计哲学总结

1. **文件即记忆**：长期记忆是纯 Markdown，人类可读、可 diff、可版本化，不锁在向量库里。
2. **写入比读取难**：每个分区都有阈值（"跨多个会话出现""一年后还有用吗"）、有反 Barnum 红线、有"宁可空"原则——宁可少记，不可错记。
3. **schema 与行为分离**：sections.yaml 改列表即重塑画像结构，不动模型、不动脚本。
4. **预算硬约束**：6000 字符上限、5~8 条看板容量、3 个月归档视图——注入内容永远精简。
5. **巩固与遗忘自动化**：dream 每晚增量提炼 + 归档脚本自动驱逐陈旧条目，模型不操心生命周期。
6. **可审计**：`path:line` 引用 + Dream Log + sourcePath 回溯指针，每条记忆可溯源到原始对话。

---

## 10. 无法从本地确认的部分（诚实边界）

- 短期记忆的 embedding 模型、向量库存储格式与检索算法（内核内部，文件不可见）；
- about_user.md 注入到 prompt 的具体位置与包裹方式；
- dream 提炼轮次使用的具体模型与 prompt（feature 未启用，无运行产物可观测）。
