# WorkBuddy 记忆系统完整设计

> 本文基于本机实证梳理：读取 `~/.workbuddy/` 下记忆文件与配置、`WorkBuddy.app/.../app.asar` 渲染包、`app.asar.unpacked/cli/dist/codebuddy.js` 守护进程源码。
> 结论：**四记忆面（云端 profile / conversation_search / 本地文件 / 工作区）+ 语义检索 + 持久化底座**，由统一的 `MemoryManager` 引擎驱动。

---

## 一、总览

WorkBuddy 的记忆不是单一仓库，而是一套分层、分作用域、带语义检索的系统：

| 面 | 作用域 | 存储位置 | 谁来写 | 默认开关 |
|---|---|---|---|---|
| ① 云端 profile（Layer 1A） | 跨项目 / 跨会话（用户级） | `~/.workbuddy/memory/<uid>_memory.md` + 服务端 | 服务端异步生成 | 开（`generateMemoryEnabled`） |
| ①B conversation_search（Layer 1B） | 全量历史会话 | 服务端索引 | 工具调用 | 开（内置） |
| ② 本地文件（Layer 2） | 用户级 / 身份级 | `~/.workbuddy/MEMORY.md`、`SOUL.md` | 守护进程 `updateMemoryBody` / `updateSoulBody` | 关（`localMemoryEnabled`） |
| ③ 工作区（Layer 3） | 当前项目 | `.workbuddy/memory/YYYY-MM-DD.md`、`.workbuddy/memory/MEMORY.md` | 模型用 Edit 工具 | 开（指令驱动） |

引擎层是守护进程里的 **`MemoryManager`**，它把上述"面"统一为 `{scope, type, content}` 条目，并负责检索与注入。

---

## 二、系统分层契约（注入到模型的指令）

模型在每次会话收到的系统提示（system prompt）中，记忆系统被显式约定为三层：

### Layer 1 — Cloud Memory
- **(A) Auto-injected profile**：服务端生成的长期用户画像摘要，会话开始时注入系统提示的 `<user_memory>` 块；本地缓存于 `~/.workbuddy/memory/`。**只读**——本地写入会被服务端覆盖。
- **(B) Historical conversation retrieval**：`conversation_search` 工具，服务端排序，跨全部历史会话检索；**对当前会话零访问**，查询必须自包含。

### Layer 2 — User-level Local Memory
- 文件：`~/.workbuddy/MEMORY.md`，作用域为所有项目，上限约 4000 字符/会话。
- 由模型在用户明确要求长期记住某件事时，用 Edit 工具就地更新。

### Layer 3 — Workspace Memory
- 目录：`<workspace>/.workbuddy/memory/`，作用域仅当前项目。
- `YYYY-MM-DD.md`：每日工作日志，**仅追加（append-only）**，永不覆盖。
- `MEMORY.md`：整理后的长期项目笔记，上限约 3000 字符/会话。
- 完成实质工作后追加简短记录；跨项目/不确定位置时用 `conversation_search`。

---

## 三、守护进程引擎（codebuddy.js，真实实现）

`MemoryManager` 的条目模型是**二维作用域 × 类型**：

```js
scope ∈ { USER, PROJECT, PROJECT_LOCAL }
type  ∈ { ALWAYS }   // ALWAYS = 始终注入
```

所有记忆操作都收敛到 `Memory Controller`（经桌面端 `daemonClient` 调用）。关键 API：

### `getMemoryProfile()`
把 `memoryManager.get({})` 的所有条目拼成 `memoryBlock`：

```js
memoryBlock: (await this.memoryManager.get({}))
    .map(e => `# ${e.scope}: ${e.type}\n${e.content}`).join("\n\n")
// 同时返回：
// { memoryBlock, generateMemoryEnabled: !0, updatedAt: new Date().toISOString() }
```

### `importMemoryContent({ content })`
- 校验 `content.length >= 20`（`MIN_CONTENT_LENGTH = 20`）。
- 落到 `memoryManager.addFact(content, MemoryScope.USER)`。
- 对应前端"记忆导入"弹窗（`MemoryImportModal`）。

### `submitMemorySuggestion({ suggestion })`
- AI 主动提议的记忆，同样 `addFact(suggestion, MemoryScope.USER)`。

### `saveMemorySettings({ generateMemoryEnabled })`
- 持久化"是否云端自动生成"开关。

### `checkMemoryUpdating()`
- 轮询云端 profile 是否正在（重新）生成。
- 前端每 **2500ms** 轮询一次（`POLL_INTERVAL = 2500`）。

### `injectRelevantMemories()`
- 用 `memoryRelevanceService` 做**语义检索**：取最后一条用户 query，从 `memoryDir` 拉取相关记忆注入。这是 RAG 式"按需召回"，而非全量注入。

### 提示注入的三条并行通道
在 `SystemReminderAgentRunInterceptor` / `PromptContextInitializer` 中完成：

1. `<user_memory>` —— 服务端长周期 profile（会话开始注入）。
2. `<memory>` —— 由 `generateLegacyMemoryPrompt()` 读 `memoryDir/MEMORY.md`（截断 200 行 / 40000 字符），或由 `buildMemoryPrompt()` 从 auto-memory 目录生成。
3. `userMemory` / `projectMemory` 变量 —— `PromptContextInitializer` 直接 `memoryManager.get({scope: USER})` / `get({scope: PROJECT})` 塞进上下文。

渲染端对 `<memory>` 的写入约定（`generateLegacyMemoryPrompt` 内嵌说明）：
- `MEMORY.md` 始终加载进上下文，**超过 200 行会被截断**，需保持简洁。
- 按语义主题组织，而非按时间；用 Write/Edit 更新；可建 `debugging.md`、`patterns.md` 等专题文件并从 `MEMORY.md` 链接。
- 保存稳定模式、架构决策、用户偏好、反复出现的解决方案；不保存易变的事务性细节。

---

## 四、云端 profile 的落盘格式

本机实测文件：`~/.workbuddy/memory/bbc41e1b-c979-4d65-b1a3-f7fd66d5d9f4_memory.md`
（uid 与 `~/.workbuddy/settings.json` 的 `claw.legacyOwnerUid` 完全一致，确认身份绑定。）

文件 = 可读 Markdown + 机器可读 `RAW_JSON` 块：

```markdown
# User Memory Profile
> Last updated: 2026-08-04T06:54:58+08:00
> Version: 9

## Memory Block
**工作背景** ...
**个人背景** ...
**当前关注** ...
**近期动态** ...

---
<!-- RAW_JSON_START
{
  "uid": "bbc41e1b-c979-4d65-b1a3-f7fd66d5d9f4",
  "memoryBlock": "...",
  "version": 9,
  "updatedAt": "2026-08-04T06:54:58+08:00"
}
RAW_JSON_END -->
```

要点：
- 分段用 `**标题**` 粗体（前端 `parseMemoryText()` 正是把 `**xxx**` 渲染为加粗）。
- `version` 每次重生成自增；`updatedAt` 为生成时间。
- 这是"Layer 1A 自动注入 profile"的**本地缓存副本**，会话开始由服务端拼进系统提示的 `<user_memory>`。

---

## 五、两个开关（设计核心）

`MemorySettingsPanel` 维护一份 `memorySettingsSnapshot`：

```js
{
  generateMemoryEnabled: true,   // 云端自动生成 profile（默认开）
  localMemoryEnabled:  false,   // 本地 MEMORY.md / SOUL.md 文件记忆（默认关）
  memoryPreview: "",
  memoryUpdatedAt: ""
}
```

- **`generateMemoryEnabled`（默认开）**：会话历史被服务端聚合为长周期 profile，异步生成、可轮询。
- **`localMemoryEnabled`（默认关）**：是否启用"模块B" `MEMORY.md` 与"模块A" `SOUL.md` 的本地文件记忆。注意 `updateMemoryBody` / `updateSoulBody` 在适配器里均带 `?.` 可选链——未启用时直接返回 `{success:false, error:"not supported"}`，不会落盘。

相关 UI 组件：
- `MemorySettingsPanel` —— 设置面板（两个开关 + 预览）。
- `MemoryManageModal` —— 编辑 / 重置。
- `MemoryImportModal` —— 导入（`MIN_CONTENT_LENGTH = 20`，空内容等于重置）。
- `FormattedMemoryText` / `parseMemoryText` —— `**标题**` 渲染。

---

## 六、本地文件记忆（Layer 2 / 模块 A·B）

| 文件 | 角色 | 写回接口 | 模块 |
|---|---|---|---|
| `~/.workbuddy/MEMORY.md` | 用户级长期记忆（跨项目） | `daemonClient.updateMemoryBody(body)` | 模块 B |
| `~/.workbuddy/SOUL.md` | 身份 / 人格设定 | `daemonClient.updateSoulBody(body)` | 模块 A |

- 受 `localMemoryEnabled` 开关保护；关闭时不写盘。
- 设置面板通过 `getMemoryProfile()` 读取 `memoryBlock` 做预览（取前 200 字符）。
- 模型侧（Layer 2 指令）通过 Edit 工具直接维护 `~/.workbuddy/MEMORY.md`。

---

## 七、工作区记忆（Layer 3）

- **每日日志** `<workspace>/.workbuddy/memory/YYYY-MM-DD.md`
  - 仅追加（append-only），完成实质工作后追加简短记录。
  - 不记录瞬时信息（搜索结果、临时路径、工具错误）。
- **项目笔记** `<workspace>/.workbuddy/memory/MEMORY.md`
  - 整理后的长期项目笔记，上限约 3000 字符/会话。
  - 超过 30 天的日志可蒸馏进此处后删除旧文件。
- **项目级记忆文件** `CODEBUDDY.md`
  - `SystemReminderAgentRunInterceptor` 会检查 `system-reminder-md` 模板；若缺失则跳过 `CODEBUDDY.md` 注入并告警。
  - 类比 `CLAUDE.md`：项目级常驻说明文件。

---

## 八、语义检索与提示注入（完整链路）

```mermaid
flowchart TD
    Q[最后一条用户 query] --> R[injectRelevantMemories]
    R --> RS{memoryRelevanceService.isEnabled?}
    RS -- 是 --> RET[语义检索 memoryDir 相关记忆]
    RS -- 否 --> SKIP[跳过]
    RET --> INJ[注入 <memory> 相关片段]
    GP[getMemoryProfile] --> MB[memoryBlock: # scope: type\ncontent]
    MB --> UM[<user_memory> 注入]
    CTX[PromptContextInitializer] --> UV[userMemory = get(scope:USER)]
    CTX --> PV[projectMemory = get(scope:PROJECT)]
```

注入通道汇总：

| 通道 | 来源 | 时机 |
|---|---|---|
| `<user_memory>` | 服务端长周期 profile（本地缓存 `<uid>_memory.md`） | 会话开始 |
| `<memory>` | `MEMORY.md`（200 行 / 40KB 截断）或 auto-memory 目录 | 每次 agent run |
| `userMemory` / `projectMemory` | `MemoryManager` 的 USER / PROJECT 作用域 | 上下文初始化 |
| `injectRelevantMemories` | 语义检索相关记忆 | 按需（基于 query） |

---

## 九、持久化底座

### `workbuddy.db`（Drizzle 迁移，表清单）

| 表 | 字段要点 | 作用 |
|---|---|---|
| `sessions` | id, cwd, user_id, title, model, mode, expert_id, project_id, created_at, updated_at… | 会话持久化 |
| `automations` | id, name, prompt, schedule_type, rrule, cwds, model_id, skills_json, permission_mode… | 定时任务定义 |
| `automation_runs` | thread_id, automation_id, status, runs_json, result_success… | 定时任务运行记录 |
| `automation_runtime_state` | automation_id, last_run_at, last_error, running… | 运行时状态 |
| `automation_delivery_outbox` | — | 投递出箱 |
| `workspaces` | path, last_opened_at | 工作区登记 |
| `__workbuddy_drizzle_migrations` / `migration_meta` | — | 迁移元数据 |

### 关键目录（`~/.workbuddy/`）

| 目录 | 内容 |
|---|---|
| `memory/` | 云端 profile 缓存 `<uid>_memory.md` + `.bak` |
| `blobs/` | SHA256 内容寻址 blob 存储 |
| `file-history/` | 文件历史（SHA256 blob + `index.json`） |
| `projects/` | 各项目状态（按路径命名目录） |
| `tasks/` | 任务管理状态 |
| `traces/` | 执行轨迹 |
| `sessions/` | 会话原始数据 |
| `local_storage/` | 渲染端本地镜像（`*.info` 入口） |
| `edge-sync-mapping.db` | 边缘同步映射（云端 profile ↔ 本机） |

---

## 十、一次会话的记忆数据流

```
会话历史 ──(服务端聚合, 异步)──▶ 云端 profile
                                     │ 边缘同步
                                     ▼
                       ~/.workbuddy/memory/<uid>_memory.md
                                     │ 注入 <user_memory>
        ┌────────────────────────────┼──────────────────────────┐
        ▼                            ▼                          ▼
 MemoryManager(USER/PROJECT/      <memory> (MEMORY.md,        injectRelevantMemories
  PROJECT_LOCAL × ALWAYS)          200 行 / 40KB 截断)        (语义检索相关记忆)
        │                            │                          │
        └──────────── 拼装进系统提示 ┴──────────────────────────┘
                                     │
                                     ▼
                              模型推理 / Edit 写回
                                     │
                  ┌──────────────────┼──────────────────┐
                  ▼                  ▼                  ▼
            workbuddy.db        blobs / file-history   projects / tasks
            (sessions,           (内容寻址)            (工作区状态)
             automations)
```

---

## 十一、关键文件与代码索引

| 位置 | 关键符号 / 内容 |
|---|---|
| `~/.workbuddy/memory/<uid>_memory.md` | 云端 profile 缓存（Markdown + `RAW_JSON{uuid, memoryBlock, version, updatedAt}`） |
| `~/.workbuddy/settings.json` | `claw.legacyOwnerUid` = `bbc41e1b-...`（= profile uid） |
| `~/.workbuddy/MEMORY.md` | Layer 2 用户级长期记忆（模型 Edit 维护） |
| `app.asar`（渲染包） | `MemorySettingsPanel` / `MemoryManageModal` / `MemoryImportModal` / `FormattedMemoryText` / `parseMemoryText`；`daemonClient.updateMemoryBody` / `updateSoulBody` 调用点；`conversation_search` 注册为 `DESKTOP_MCP_BUILTIN_TOOL` |
| `app.asar.unpacked/cli/dist/codebuddy.js`（守护进程） | `MemoryManager`（scope USER/PROJECT/PROJECT_LOCAL × type ALWAYS）、`getMemoryProfile`、`importMemoryContent`、`submitMemorySuggestion`、`saveMemorySettings`、`checkMemoryUpdating`、`injectRelevantMemories`、`generateLegacyMemoryPrompt`、`buildMemoryPrompt`、`PromptContextInitializer` |
| `workbuddy.db` | `sessions` / `automations` / `automation_runs` / `automation_runtime_state` / `automation_delivery_outbox` / `workspaces` |

---

## 十二、设计要点小结

1. **作用域更细**：不是简单的"用户 / 工作区"两档，而是 `USER / PROJECT / PROJECT_LOCAL` 三作用域 × `ALWAYS` 类型，由统一 `MemoryManager` 管理。
2. **检索是 RAG 式**：`injectRelevantMemories` + `memoryRelevanceService` 按当前 query 语义召回，而非每次全量注入。
3. **双开关解耦**：云端自动生成（默认开）与本地文件记忆（默认关）彼此独立，可单独关闭本地落盘而保留云端 profile。
4. **本地文件记忆经守护进程写回**：`MEMORY.md` / `SOUL.md` 不是模型直接写，而是通过 `daemonClient.updateMemoryBody / updateSoulBody` 由守护进程落盘，带 `not supported` 保护。
5. **云端 profile 只读缓存**：本地 `<uid>_memory.md` 是服务端生成结果的缓存，本地写入会被覆盖。
