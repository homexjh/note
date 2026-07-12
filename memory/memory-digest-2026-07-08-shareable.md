# AI Memory 前沿情报 · 2026-07-08

> 一句话总览：今天最值得关注的是两件互补的事——**MRMS**（NxtLab 技术报告）把「可靠个性化 = 记忆设计问题」讲透，主张记忆必须**结构化、按需暴露、持续巩固、并带认识论标注**，而非把对话原样堆着；**Memora/FAMA**（ACL Findings）给出配套的尺子——**遗忘感知评测**，专门惩罚「复用已失效记忆」，实测包括 Mem0、MemoryOS、MemoBase 在内的六个记忆 agent 都频繁踩坑。一个给架构范式，一个给评测标准。此外 arXiv 冒出一条**记忆诱发谄媚（sycophancy）**的安全基准，值得所有做记忆注入的团队警惕。

---

## 📄 论文

### MRMS: A Multi-Resolution Memory Substrate for Long-Lived AI Agents（[原文](https://arxiv.org/abs/2607.04617)）
- **是什么**：NxtLab Innovations 2026 年 7 月技术报告。核心主张——「可靠的个性化本质是一个记忆设计问题」：有用的记忆应当是**结构化、有选择地暴露、持续巩固、且带认识论标注（epistemically labeled）**的，而不是把未分化的对话历史原样存下来。
- **怎么做的**：两条正交轴——**表征轴**（结构化记录 / 向量 / 图关系）× **时间轴**（短期痕迹 / 中期抽象 / 长期语义承诺）。关键设计约束叫「**结构化-向量-图三重同步**」：结构化记录管「资格准入」，向量负责「召回」，图关系在写入上下文前**裁定 support / contradiction / supersession（支持/矛盾/取代）**，最后经「门控上下文投影」才注入。强调记忆的**假阳性比假阴性更危险**——漏记只丢连续性，错记（陈旧偏好、被取代的事实、越界观察）会被反复重新引入未来推理，形成持久污染。
- **通用启发**：
  1. 给每条记忆打「认识论标签」（谁说的、直接观察还是推断、是否已验证），检索与合并时区别对待，能显著降低错记污染。
  2. 记忆写入前先做「矛盾/取代裁定」，命中就标记失效并保留旧值时间线，而非静默覆盖。
  3. 「门控投影」——不是所有记忆都塞进 prompt，按当前话题相关性有选择地暴露。

### From Recall to Forgetting: Benchmarking Long-Term Memory for Personalized Agents（Memora / FAMA）（[原文](https://aclanthology.org/2026.findings-acl.1337.pdf)）
- **是什么**：ACL 2026 Findings。指出现有长期记忆评测几乎都把问题框成「从过去对话里做事实检索」，无法反映 agent **随时间巩固记忆、处理频繁知识更新**的能力。提出 **Memora** 基准（跨越数周到数月的用户对话）与 **FAMA（Forgetting-Aware Memory Accuracy，遗忘感知记忆准确率）**。
- **怎么做的**：建十个职业画像，每个分三类**用户中心记忆**——**preference（偏好）/ activity（活动）/ goal（目标）**，用会话模拟器在时间线上引入/更新/失效这些实体（含偏好漂移、重复活动、长期任务增量推进），并**记录每次会话前后的完整记忆状态**形成显式记忆轨迹。FAMA 用两组二元判据打分：**memory presence**（有效信息是否被正确包含）+ **forgetting absence**（失效/已删信息是否被正确排除），三 LLM 裁判多数投票，与人工一致率 88.3%。评测 GPT-5.2、Claude Sonnet 4.5、Gemini 3 Pro、Qwen3-32B 及 A-Mem/LangMem/Mem0/MemoBase/MemoryOS/Nemori。结论：普遍**频繁复用已失效记忆**，记忆 agent 相比纯 LLM 只有边际提升；时间跨度越长越差；「推理」类任务对所有系统都难。
- **通用启发**：
  1. 记忆系统评测别只测「提取准不准」，要补一把「有没有错误复用旧值」的尺子（新值被采纳 + 旧值不再被引用）。
  2. preference / activity / goal 是好用的用户记忆三分类，尤其「goal（长期目标）」在只做事实抽取时最容易被漏掉。
  3. 别高估任何单一开源记忆库的开箱效果——在「失效治理」上目前普遍薄弱。

### What Deserves Memory: Adaptive Memory Distillation for LLM Agents（NEMORI）（[原文](https://aclanthology.org/2026.acl-long.1607.pdf) · [代码](https://github.com/nemori-ai/nemori)）
- **是什么**：ACL 2026 Long Paper。批评现有「什么值得记」的做法依赖预设启发式（importance score、情绪标签、事实模板），本质是编码设计者直觉。提出 **NEMORI**：把「经验的未来效用」重新表述为「**可预测性**」——用预测误差决定该记什么。开源。
- **怎么做的**：两个级联模块——**Episodic Memory Integration**（把原始交互整合成连贯叙事）+ **Semantic Knowledge Distillation**（通过**预测误差**提炼：模型能轻松预测的信息量低、不值得留；预测失败/意外的才是高价值经验）。对下游记忆管理保持无关，性能/效率/存储压缩均有优势。
- **通用启发**：
  1. 「预测误差 = 记忆价值」是取代人工启发式的数据驱动信号：符合预期的交互增量价值低，意外事件才优先蒸馏。
  2. 「先整合成叙事、再蒸馏语义」的两段式，比直接从零散消息抽事实更抗碎片化。
  3. 坑：纯预测误差会**低估「稳定但关键」的事实**（越可预测越不记）——关键字段需设「无条件留存」白名单，不能全交给打分。

---

## ⚠️ 安全 / 评测新面

### MemSyco-Bench: Benchmarking Sycophancy in Agent Memory（[原文](https://arxiv.org/abs/2607.01071) · 2026-07-01）
- **是什么**：记忆已是 Agent 基石，但**被检索回来的记忆会诱发谄媚**——让 Agent 过度迎合用户而牺牲事实准确性与客观推理。现有记忆基准只评「存/取/更新是否正确」，忽略「取回的记忆如何影响下游推理与决策」，该工作专门给这一盲区建基准。
- **通用启发**：把用户偏好类记忆注入 prompt 后，模型可能为迎合用户而弱化客观建议。任何有「立场/安全」诉求的产品都应：① 评测集加入「记忆诱导谄媚」对抗用例；② 注入时对「用户偏好」与「客观事实」做角色区分，明确「偏好不得覆盖事实性/安全性结论」。可视作 FAMA「forgetting absence」之外再补一维「sycophancy absence」。

### Your Agent's Memories Are Not Its Own（FARMA 攻击 + SENTINEL 防御）（[原文](https://arxiv.org/abs/2607.05029) · 2026-07-06, cs.CR）
- **是什么**：持久记忆让 Agent 存事实、决策与**推理历史**，推理历史成了新攻击面。FARMA 投毒「被记住的推理过程」（非事实），成功率最高 100%；SENTINEL 防御降到 0% 且 326 条正常轨迹零误报。同期还有 MemGhost（[2607.05189](https://arxiv.org/abs/2607.05189)）用邮件载荷隐蔽注入持久记忆。
- **通用启发**：只落「事实性记忆」比缓存「推理链」更安全；若要缓存理由/推理，从存储读回后写入前须做来源校验清洗，别把对话中的诱导内容当可信记忆固化。

---

## 🛠 产品与工程

### Oracle AI Agent Memory 26.6：自定义抽取、混合检索与更强生命周期控制（[原文](https://blogs.oracle.com/developers/whats-new-in-oracle-ai-agent-memory-custom-extraction-hybrid-search-and-more-control) · 2026-07-07）
- **是什么**：数据库支撑的记忆层（存 messages、durable memories、summaries、prompt-ready context），本次更新聚焦开发者体感。
- **值得抄的点**：(1) **Custom Extraction Instructions**——用指令引导「什么该变成持久记忆」；(2) **Hybrid Search**（向量+文本）语义召回与精确匹配兼得；(3) **Context Cards** 把长对话压成 prompt-ready 短期上下文；(4) **Metadata filtering + update APIs** 控制作用域/检索边界/生命周期；(5) **TTL**（默认保留期 + 单条覆盖 + 过期感知检索）；(6) 用 LongMemEval、BEAM 做基准验证。
- **通用启发**：TTL / 过期感知检索是治理「记忆失效」最轻量的手段，和 FAMA 的 forgetting absence 正好配套；对「专有名词、数值阈值」类内容，纯语义召回易漏，混合检索更稳。

### MOSS: Memory-Orchestrated Semantic System（[原文](https://arxiv.org/abs/2607.04391) · 2026-07-05）
- **是什么**：Agent 驱动在**关系数据库**上做检索，每步「可记录、可审查」。已在真实生产语料跑一年——约 4400 万 token、110,183 段、569 个归纳概念。少见的「关系库 + agentic 检索 + 全程可审计」生产验证。
- **通用启发**：把每次记忆检索/注入的「取了哪些条、为什么取、命中路径」结构化落库，是排查「注入了却没生效」最实用的可观测性手段。

### Graph-based Agent Memory（Neo Kim, System Design #160）（[原文](https://newsletter.systemdesign.one/p/graph-based-agent-memory) · 2026-07-07）
- **是什么**：实用向图记忆指南，讲「一条查询如何检索」「何时该用、何时不该用」。亮点是 **Omnigraph**——共享对象存储上的**类型化图**，**每次变更先 propose → review → merge 才进入共享记忆**。
- **通用启发**：把「合并」显式化为可审计的一步（记录 diff），每次记忆变更可回放；但「简单偏好记忆别上图」——扁平结构化+向量足矣，图数据库的运维重量只在「事实随时间演化/多跳关系」时才值得。

### Mem0 v3 开源版移除图记忆层
- **一句话**：Mem0 v3 开源重写**删掉图记忆模块**（仅 OSS，托管平台仍保留图记忆+时间衰减），转向「单遍抽取 + 多信号检索融合（语义+BM25+实体匹配）」，报告 LoCoMo 91.6（[2026 框架评测](https://dev.to/agdex_ai/ai-agent-memory-in-2026-mem0-vs-zep-vs-letta-vs-cognee-a-practical-guide-cfa)）。
- **通用启发**：又一个「稳定偏好召回场景，向量+抽取+多信号融合常比上图更划算」的务实信号；真正需要图/时序的是「事实随时间变化」，届时才考虑 Zep/Graphiti 式的事实有效期窗口。

---

## 💡 想法与趋势

### SelfMem: Self-Optimizing Memory for AI Agents（[原文](https://arxiv.org/abs/2607.03726) · 2026-07-04）
- **是什么**：不用固定的检索/摘要规则，让 Agent 通过工具反馈自行迭代记忆策略。BEAM 长上下文基准上，100K/500K/1M token 规模分别比最强基线 +48.7% / +40.8% / +41.9%。
- **通用启发**：把「怎么记、怎么取」从硬编码变成可学习闭环是明确趋势；高风险领域宜先做**离线**策略选择（对比不同抽取/合并策略在真实对话上的质量），不做线上自改写。

### Autonomous Information Seeking: Agentic 推荐系统路线图（综述）（[原文](https://arxiv.org/abs/2607.04433) · 2026-07-05）
- **是什么**：把 agentic 推荐系统按自主性归为三范式——agent 辅助 / agent 即推荐器 / agent 即用户模拟器；梳理终身用户建模、记忆、隐私与评测方法学的开放挑战。
- **通用启发**：「agent 即用户模拟器」范式很适合**离线评测对话/追问质量**——用模拟用户跑多轮，看系统是否真正推进有用信息获取而非泛泛提问。

---

## 🔭 明日追踪
- **ACL 2026（7/2–7/7，San Diego）记忆专题余量**：SYNAPSE（episodic-semantic via spreading activation, findings.1108）、MAGMA（multi-graph agentic memory, acl-long.1709）、LiCoMemory（轻量认知记忆, findings.1835）、TiMem（时间-层级巩固, findings.1091）等多篇待深读。
- **MRMS 是否放出原型代码/基准数**：目前仅技术报告 + 轻量原型描述。
- **Memora 基准是否公开数据/榜单**：若放出可作为各记忆方案的统一对照基线。
