#### 4. agent 系统执行多步任务失败如何处理

---

多步任务失败是 Agent 系统的常态问题。工程上要从**预防、检测、恢复、兜底**四个层面来处理。

---

## 一、多步任务失败的常见原因

| 失败类型 | 典型表现 | 根因 |
|---------|---------|------|
| **工具调用失败** | API 超时、返回 500、参数错误 | 外部服务不稳定、参数格式问题 |
| **推理偏离** | LLM 选错工具、理解错用户意图 | 提示不清晰、上下文不足、模型能力不够 |
| **中间状态错误** | 上一步结果不对，导致后续全错 | 没有校验中间结果、错误传播 |
| **循环 / 死锁** | 反复调用同一个工具、反复道歉 | 缺乏终止条件、反思机制 |
| **上下文超限** | 历史太长导致遗忘早期目标 | 多轮后 token 超限 |
| **权限 / 安全拦截** | 工具调用被安全策略拒绝 | 正常任务触发了敏感规则 |
| **用户意图模糊** | 任务做到一半发现信息不够 | 一开始没有澄清需求 |

---

## 二、分层处理策略

### 1. 预防层：让失败不容易发生

#### ① 任务规划时拆分清晰

不要把复杂任务一次性丢给 LLM，而是拆成可验证的步骤：

```python
steps = [
    {"id": 1, "action": "查询用户信息", "tool": "user_api", "validate": "user_exists"},
    {"id": 2, "action": "查询订单列表", "tool": "order_api", "depends_on": 1},
    {"id": 3, "action": "生成退款申请", "tool": "refund_api", "depends_on": 2},
]
```

#### ② 每步加校验

```python
def validate_tool_result(result, schema):
    if not schema.validate(result):
        raise StepValidationError("工具返回结果格式不符")
    if result.get("error"):
        raise ToolError(result["error"])
```

#### ③ 明确终止条件

- 最大步数限制：`max_steps = 10`
- 重复调用检测：同一工具同参数 3 次就停止
- 用户确认点：关键步骤必须确认

---

### 2. 检测层：快速发现失败

#### ① 工具调用级检测

```python
def call_tool(tool_name, params):
    try:
        result = tool_client.invoke(tool_name, params)
        if result.status >= 400:
            raise ToolInvocationError(f"{tool_name} failed: {result}")
        return result
    except TimeoutError:
        raise ToolTimeoutError(f"{tool_name} timeout")
```

#### ② 步骤级检测

```python
def execute_step(step, context):
    output = llm.plan_and_act(step, context)
    
    # 检测是否偏离目标
    if divergence_score(output, step.goal) > 0.7:
        raise StepDivergenceError("执行偏离目标")
    
    return output
```

#### ③ 任务级检测

```python
def is_task_complete(task, final_output):
    return judge_llm.evaluate(task, final_output)
```

---

### 3. 恢复层：失败后可以自愈

#### ① 重试（Retry）

适合瞬态故障：

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(1, 4))
def call_tool(tool_name, params):
    return tool_client.invoke(tool_name, params)
```

注意：只对**幂等操作**重试，写操作要谨慎。

#### ② 降级（Fallback）

主路径失败时换备用方案：

```python
def search_knowledge(query):
    try:
        return primary_kb.search(query)
    except KBError:
        return fallback_web_search(query)
```

#### ③ 重新规划（Re-plan）

某一步失败后，让 LLM 重新规划剩余步骤：

```python
def replan(task, executed_steps, failed_step, error):
    prompt = f"""
    原任务：{task}
    已执行：{executed_steps}
    失败步骤：{failed_step}
    错误：{error}
    
    请重新规划剩余步骤，避开失败路径。
    """
    return llm.generate_plan(prompt)
```

#### ④ 局部回滚（Rollback）

如果某一步修改了状态，要尽量回滚：

```python
def execute_with_rollback(step):
    try:
        result = step.run()
        return result
    except Exception as e:
        step.undo()  # 调用对应的补偿操作
        raise
```

#### ⑤ 反射修正（Reflection）

让 LLM 自己分析失败原因并修正：

```python
reflection_prompt = """
上一步执行失败：
- 目标：{step_goal}
- 动作：{action}
- 结果：{result}
- 错误：{error}

请分析失败原因，并给出修正后的下一步动作。
"""
```

---

### 4. 兜底层：恢复不了就优雅退出

#### ① 转人工

复杂或高风险失败时，把会话转给人工：

```python
if failure.severity == "high" or retry_count > max_retry:
    escalate_to_human(task, context, failure_reason)
```

#### ② 向用户说明状态

不要直接崩溃，要告诉用户：

```text
很抱歉，我在执行“查询订单并申请退款”时遇到了问题：
- 已完成：查询到您的 3 笔订单
- 卡住的原因：退款接口暂时不可用
- 建议您：稍后重试，或联系客服处理
```

#### ③ 保存断点，支持恢复

```python
checkpoint = {
    "task_id": task.id,
    "completed_steps": [...],
    "current_step": 3,
    "context": context,
    "status": "paused"
}
save_checkpoint(checkpoint)
```

用户下次来时可以从断点继续。

---

## 三、一个完整的失败处理流程

```python
def execute_task(task):
    context = {}
    executed_steps = []
    
    for step in task.steps:
        try:
            # 1. 执行步骤
            result = execute_step(step, context)
            
            # 2. 校验结果
            validate_step_result(step, result)
            
            # 3. 更新上下文
            context[step.id] = result
            executed_steps.append(step)
            
        except ToolTimeoutError:
            # 瞬态失败：重试
            result = retry_step(step, context)
            
        except ToolInvocationError as e:
            # 工具错误：降级或重新规划
            if has_fallback(step):
                result = fallback_step(step, context)
            else:
                return replan_or_escalate(task, executed_steps, step, e)
                
        except StepDivergenceError as e:
            # 偏离目标：反思修正
            result = reflect_and_correct(step, context, e)
            
        except ValidationError as e:
            # 结果校验失败：回滚并重新执行
            rollback_last_step(step)
            result = retry_step_with_clarification(step, context)
    
    # 任务完成检测
    if not is_task_complete(task, context):
        return replan_or_escalate(task, executed_steps, None, "任务未完成")
    
    return generate_final_answer(task, context)
```

---

## 四、关键设计原则

| 原则 | 说明 |
|------|------|
| **快速失败，慢恢复** | 发现错误立即停，不要带病执行 |
| **错误要分类** | 不同错误不同处理，不能一刀切 |
| **状态要保存** | 支持断点续作，不要从头再来 |
| **用户要知情** | 失败时给用户清晰的状态说明 |
| **人工要能接管** | 高风险场景必须能转人工 |
| **失败要可观测** | 每个失败都要记录，便于归因 |

---

## 五、监控与优化

| 监控项 | 作用 |
|--------|------|
| 步骤失败率 | 哪个步骤最容易失败 |
| 失败类型分布 | 是工具问题、模型问题还是策略问题 |
| 重试成功率 | 重试机制是否有效 |
| 人工接管率 | 哪些任务经常需要人工 |
| 平均恢复时间 | 从失败到恢复需要多久 |
| 用户满意度 | 失败后的用户体验如何 |

---

## 六、和 prompt 注入防御的关系

多步任务失败处理中，有一类特殊失败是**安全策略触发**的：

- Agent 检测到 prompt 注入尝试，拒绝执行
- 工具调用被权限系统拦截
- 用户请求触发了越权检查

这类失败**不能简单重试**，而要明确返回安全原因，并记录审计日志：

```python
except SecurityPolicyViolation as e:
    log_security_event(task, step, e)
    return SecurityRefusalResponse(e.rule_id)
```

安全拒绝也是“正确行为”，不应被计入普通失败率。
