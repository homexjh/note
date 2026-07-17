#### 7. 怎么保证大模型输出的 JSON 格式

---

保证大模型输出 JSON 格式，需要从**提示工程、模型约束、后处理、校验兜底**四个层面一起做。没有任何一层能 100% 保证，必须组合使用。

---

## 一、为什么 JSON 输出会出问题？

| 问题 | 表现 |
|------|------|
| **多嘴** | JSON 前后加了 ``` 代码块、解释文字 |
| **截断** | 长 JSON 被 token 限制截断 |
| **格式错误** | 缺逗号、引号不闭合、用了单引号 |
| **类型错误** | 数字写成了字符串，布尔写成了 yes/no |
| **字段缺失** | 漏了必填字段 |
| **非法字符** | JSON 里混入了换行、控制字符 |
| **注释** | LLM 在 JSON 里加了 // 注释 |

---

## 二、提示工程层：让模型知道要输出什么

### 1. 明确指定输出格式

```text
你必须只输出合法的 JSON，不要添加任何解释、代码块标记或其他内容。
JSON 格式如下：
{
  "name": "string",
  "age": "integer",
  "items": ["string"]
}
```

### 2. 给完整示例（Few-shot）

```text
输入：一只黑色的猫
输出：
{"name": "猫", "color": "黑色", "type": "动物"}

输入：三辆红色的汽车
输出：
{"name": "汽车", "color": "红色", "type": "交通工具", "count": 3}
```

### 3. 加角色设定

```text
你是一个 JSON 生成器。你的唯一任务是输出合法 JSON，不输出任何其他文本。
```

### 4. 明确字段类型和约束

```text
{
  "price": "number, 必须保留两位小数",
  "available": "boolean, 只能是 true 或 false",
  "tags": "array of string, 至少一个"
}
```

---

## 三、模型/推理层：用结构化输出能力

### 1. JSON Mode / Structured Outputs

很多模型和 API 提供了强制 JSON 输出的模式：

```python
# OpenAI / 兼容接口
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    response_format={"type": "json_object"}
)
```

```python
# 如果支持 JSON Schema 约束
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "product",
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "price": {"type": "number"}
                },
                "required": ["name", "price"]
            }
        }
    }
)
```

**注意：** 不是所有模型/平台都支持。支持的情况下这是最可靠的方式。

### 2. 控制温度

```python
temperature = 0.0  # 或 0.1
```

低温度减少随机性，对 JSON 格式有帮助。

### 3. 限制 max_tokens

不要让模型生成过长，降低截断风险，但要预留足够空间。

---

## 四、后处理层：清洗和修复

即使模型输出 JSON，也经常不干净，需要后处理。

### 1. 提取 JSON 块

```python
import re
import json

def extract_json(text):
    # 先尝试整个字符串解析
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    
    # 提取 ```json ... ``` 块
    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    
    # 提取 { ... } 或 [ ... ] 块
    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    
    return None
```

### 2. 自动修复常见错误

```python
import json_repair  # pip install json-repair

def safe_parse_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json_repair.loads(text)
```

`json-repair` 可以处理很多常见错误，比如：

- 单引号变双引号
- 末尾缺逗号
- 注释去除
- 多余文本清理

### 3. 用 LLM 自我修复

如果自动修复失败，可以让另一个 LLM 调用修复：

```python
def repair_with_llm(raw_text, schema_description):
    prompt = f"""
    下面的文本应该是一个 JSON，但解析失败了。
    请修复它，只输出修复后的合法 JSON，不要解释。
    
    期望格式：{schema_description}
    
    文本：
    {raw_text}
    """
    return call_llm(prompt)
```

---

## 五、校验层：确保 JSON 符合预期

### 1. JSON Schema 校验

```python
from jsonschema import validate, ValidationError

schema = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
        "email": {"type": "string", "format": "email"}
    },
    "required": ["name", "age"]
}

def validate_output(data):
    try:
        validate(instance=data, schema=schema)
        return True
    except ValidationError as e:
        print(f"校验失败: {e.message}")
        return False
```

### 2. 业务规则校验

```python
def business_validate(data):
    if data["price"] < 0:
        raise ValueError("price 不能为负数")
    if len(data["items"]) == 0:
        raise ValueError("items 不能为空")
```

---

## 六、兜底策略：实在解析不了怎么办

### 1. 重试

```python
for attempt in range(3):
    raw = call_llm(prompt)
    data = extract_json(raw)
    if data and validate_output(data):
        return data
```

### 2. 降级

返回结构化失败信息：

```python
return {
    "error": "json_parse_failed",
    "raw_output": raw,
    "message": "模型输出无法解析为 JSON"
}
```

### 3. 人工兜底

关键业务场景，JSON 解析失败时转人工审核。

---

## 七、推荐的完整流程

```python
import json
import json_repair
from jsonschema import validate

def get_structured_output(prompt, schema, max_retry=3):
    for attempt in range(max_retry):
        # 1. 调用模型
        raw = llm.generate(prompt, temperature=0.0)
        
        # 2. 提取 JSON
        data = extract_json(raw)
        
        # 3. 自动修复
        if data is None:
            try:
                data = json_repair.loads(raw)
            except Exception:
                pass
        
        # 4. 校验
        if data is not None:
            try:
                validate(instance=data, schema=schema)
                return data
            except Exception as e:
                # 把校验错误加入 prompt，让模型下次修正
                prompt += f"\n上次输出解析失败或校验不通过：{e}\n请修正后重新输出。"
                continue
    
    # 5. 兜底
    return {
        "error": "json_parse_failed",
        "raw_output": raw
    }
```

---

## 八、关键原则

1. **能用 JSON Mode / Structured Outputs 就用**，这是最稳的
2. **提示要明确**：格式、示例、类型约束都要写清楚
3. **温度要低**：`temperature=0.0` 减少随机性
4. **后处理必须有**：提取、修复、校验三步不能少
5. **失败要重试 + 兜底**：不要把解析失败直接抛给用户
6. **复杂结构分步生成**：特别复杂的 JSON 可以分多次生成再组装
