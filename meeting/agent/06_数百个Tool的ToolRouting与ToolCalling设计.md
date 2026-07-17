#### 6. 当一个 Agent 拥有数百个 Tool 时，如何设计 ToolRouting 与 ToolCalling 系统

---

当 Agent 拥有数百个 Tool 时，核心矛盾是：**LLM 上下文窗口有限、推理成本高，不可能每次请求都把全部工具描述塞进去**。必须设计一个 **Tool Routing → Tool Calling** 的分层系统。

---

## 一、核心设计思想

> **不是让 LLM 从 500 个工具里选，而是先由路由层把候选集缩到 5~20 个，再让 LLM 做精确选择。**

整体架构：

```
用户输入
   ↓
┌─────────────────┐
│  Intent Router  │  ← 先理解用户意图，锁定领域/场景
│  （意图路由层）  │
└─────────────────┘
   ↓
┌─────────────────┐
│  Tool Retriever │  ← 从候选池中召回相关工具
│  （工具召回层）  │
└─────────────────┘
   ↓  top-k 工具（5~20 个）
┌─────────────────┐
│  Tool Selector  │  ← LLM 从候选里选择并生成参数
│  （工具选择层）  │
└─────────────────┘
   ↓
┌─────────────────┐
│  Tool Executor  │  ← 执行工具，处理结果
│  （工具执行层）  │
└─────────────────┘
   ↓
结果回注 / 下一轮推理
```

---

## 二、Tool 的组织方式

### 1. 分层分类

不要把工具平铺，要按层级组织：

```yaml
tools:
  ecommerce:
    order:
      - query_order
      - cancel_order
      - refund_order
    payment:
      - query_payment
      - create_payment
  hr:
    employee:
      - query_employee
      - update_employee
    leave:
      - apply_leave
      - approve_leave
```

### 2. 每个工具打标签

```json
{
  "name": "refund_order",
  "category": "ecommerce.order",
  "domain": "电商",
  "capabilities": ["退款", "售后"],
  "required_permissions": ["order_refund"],
  "input_schema": {...},
  "output_schema": {...},
  "dangerous": true,
  "examples": [
    "我要退款订单 12345",
    "订单 12345 申请退货"
  ]
}
```

### 3. 工具描述向量化

把工具名称、描述、示例、标签生成 embedding，用于语义召回：

```python
tool_embedding = encoder.encode(
    f"{name} {description} {tags} {' '.join(examples)}"
)
```

---

## 三、Tool Routing 层设计

### 1. 意图路由（Intent Router）

先用一个轻量模型或规则，判断用户意图属于哪个大领域。

**示例：**

```python
def route_intent(query):
    # 可以用小模型分类，也可以用关键词规则
    if classifier.predict(query) == "电商售后":
        return ["ecommerce.order", "ecommerce.payment"]
    elif classifier.predict(query) == "人力资源":
        return ["hr.*"]
    ...
```

**实现方式：**

| 方式 | 适用场景 |
|------|---------|
| 规则路由 | 领域边界清晰，如按部门/产品线划分 |
| 分类模型 | 领域较多但语义可区分 |
| LLM 路由 | 领域模糊、需要理解复杂意图 |

### 2. 工具召回（Tool Retriever）

在锁定领域内，用向量检索召回最相关的工具：

```python
def retrieve_tools(query, intent_domains, top_k=10):
    query_vec = encoder.encode(query)
    
    # 先按领域过滤
    candidates = [t for t in all_tools if t.domain in intent_domains]
    
    # 再按语义相似度排序
    scores = cosine_similarity(query_vec, [t.embedding for t in candidates])
    return candidates[top_k_indices(scores, top_k)]
```

### 3. 关键词/混合召回

向量检索可能漏掉精确匹配，可以结合：

- BM25 关键词匹配
- 工具别名匹配
- 示例 query 匹配

```python
def hybrid_retrieve(query, top_k=10):
    vector_results = vector_search(query, top_k=top_k*2)
    keyword_results = bm25_search(query, top_k=top_k*2)
    return rerank_and_deduplicate(vector_results + keyword_results, top_k)
```

---

## 四、Tool Calling 层设计

### 1. LLM 精确选择

把召回的 top-k 工具描述塞进 prompt：

```text
用户请求：{query}

请从以下工具中选择 0~N 个执行，并生成参数：

工具 1: query_order
描述：根据订单 ID 查询订单详情
参数：order_id (string, required)

工具 2: cancel_order
描述：取消未发货订单
参数：order_id (string, required)
...

请输出 JSON：
{
  "reasoning": "为什么选这些工具",
  "tool_calls": [
    {"name": "query_order", "arguments": {"order_id": "12345"}}
  ]
}
```

### 2. 参数校验

LLM 输出后必须严格校验：

```python
def validate_tool_call(call, schemas):
    schema = schemas[call["name"]]
    validate(call["arguments"], schema)  # JSON Schema 校验
    
    # 业务规则校验
    if schema.dangerous and not has_permission(user, call["name"]):
        raise PermissionError()
```

### 3. 多工具链编排

复杂任务需要多轮工具调用：

```python
while not task_complete and steps < max_steps:
    # 1. routing
    candidate_tools = route_and_retrieve(query, context)
    
    # 2. tool calling
    plan = llm.select_tools(query, candidate_tools, context)
    
    # 3. execute
    results = execute_tools(plan.tool_calls)
    
    # 4. update context
    context += results
    
    # 5. check completion
    task_complete = llm.is_complete(query, context)
```

---

## 五、高级优化策略

### 1. 工具摘要压缩

工具描述太长时，可以分层摘要：

```python
# 第一层：一句话摘要（用于粗排）
short_desc = "查询订单"

# 第二层：完整描述（用于精排）
full_desc = "根据订单 ID 查询订单详情，包括状态、金额、商品列表..."

# 第三层：完整 schema（用于调用）
schema = {...}
```

### 2. 工具组合预定义

对于常见工作流，预定义工具组合：

```python
workflows = {
    "退款流程": ["query_order", "create_refund", "notify_user"],
    "请假流程": ["query_leave_balance", "apply_leave", "notify_manager"]
}

# 先匹配 workflow，再决定具体工具
```

### 3. 缓存热门路由

高频查询的路由结果可以缓存：

```python
@cache(ttl=3600)
def route_intent(query_hash):
    return classifier.predict(query_hash)
```

### 4. 级联路由

先 cheap model 粗筛，再 strong model 精排：

```
用户输入
   ↓
小模型：确定领域（10ms）
   ↓
向量检索：召回 20 个工具（20ms）
   ↓
小模型：粗筛到 5 个（30ms）
   ↓
大模型：精确选择 + 生成参数（500ms）
```

---

## 六、可观测性与迭代

| 监控项 | 作用 |
|--------|------|
| 路由准确率 | 意图路由是否分到正确领域 |
| 召回命中率 | 正确工具是否在 top-k 内 |
| LLM 选择准确率 | LLM 是否从候选里选对工具 |
| 工具调用成功率 | 参数生成是否正确 |
| 端到端延迟 | routing + calling 总耗时 |
| 成本 per query | 各层模型 token 消耗 |

---

## 七、一个最小可运行架构

```python
class ToolRouter:
    def __init__(self, tools):
        self.tools = tools
        self.encoder = SentenceTransformer(...)
        self.index = build_faiss_index(tools)
    
    def route(self, query):
        # 1. 意图路由
        domain = self.intent_classifier(query)
        
        # 2. 召回候选工具
        candidates = self.retrieve(query, domain_filter=domain, top_k=15)
        
        # 3. 粗筛
        candidates = self.rerank(query, candidates, top_k=5)
        
        return candidates

class ToolCaller:
    def __init__(self, llm, schemas):
        self.llm = llm
        self.schemas = schemas
    
    def call(self, query, candidate_tools):
        # 4. LLM 精确选择并生成参数
        plan = self.llm.select_tools(query, candidate_tools)
        
        # 5. 校验
        for call in plan.tool_calls:
            validate(call, self.schemas)
        
        # 6. 执行
        results = execute(call)
        return results
```

---

## 八、关键设计原则

1. **分层解耦**：路由、召回、选择、执行四层独立演进
2. **缩小 LLM 决策范围**：每次只给 LLM 5~15 个最相关工具
3. **混合召回**：向量 + 关键词 + 规则，提高召回率
4. **严格校验**：LLM 输出必须过 schema 和业务规则
5. **缓存 + 预定义**：降低延迟和成本
6. **持续观测**：路由准确率、召回命中率、调用成功率一个都不能少
