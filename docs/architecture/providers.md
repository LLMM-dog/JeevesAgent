# 供应商与模型

用户点名的需求：**配置模型网址和 API Key 后可以获取模型列表，不用手动输入。** 这是本模块的核心。

## 三张表的职责

| 表 | 一条记录代表 |
| --- | --- |
| `provider` | 一个 OpenAI 兼容端点：`base_url` + 加密的 `api_key` |
| `model` | 该端点下的一个模型：`model_id` + 能力标记 + 上下文窗口 |
| `model_binding` | 一个功能位绑到哪个模型 |

DDL 见 [../architecture/data-schema-2.md](../architecture/data-schema-2.md#provider)。

### 为什么用独立强类型表而不是通用 KV / JSON 配置

约束需要数据库来保证：`model_binding` 的功能位唯一、`model` 被绑定时不能删、`api_key_cipher` 需要列级处理。做成一个 `settings.json` 的话这些全落到应用层自觉，早晚出现"绑定指向了已删除的模型"这种脏数据。

## 只支持 OpenAI 兼容协议

不做 Anthropic / Gemini 原生协议适配。理由：

- 绝大多数供应商（DeepSeek、Kimi、智谱、通义、OpenRouter、SiliconFlow）和几乎所有中转站都提供 OpenAI 兼容端点
- Ollama、vLLM、LM Studio 本地部署也都提供
- Anthropic / Gemini 官方 API 可通过中转站或 LiteLLM 转成兼容格式
- 一条协议路径 = 一份代码 = 一处 bug

代价：用不到各家的独家参数（如 Anthropic 的 prompt caching 精细控制）。可接受。

## 模型探测

### 流程

```
POST /api/providers/probe
  body: { base_url, api_key }

1. 规范化 base_url（补 /v1、去尾部斜杠）
2. GET {base_url}/models，带 Authorization: Bearer {api_key}
3. 解析 data.id 得到模型列表
4. 对每个模型：从内置模型库匹配上下文窗口（模糊匹配）
5. 返回列表，标注哪些能匹配到窗口大小
6. 前端展示，用户勾选要用的模型
7. POST /api/providers 保存供应商 + 勾选的模型
```

**不在 probe 阶段就落库**。用户可能只是想看看有什么模型，或者填错了 Key。probe 是纯查询。

### base_url 规范化

用户实际会填成各种样子，必须容错：

| 用户填的 | 规范化后 |
| --- | --- |
| `https://api.deepseek.com` | `https://api.deepseek.com/v1` |
| `https://api.deepseek.com/` | `https://api.deepseek.com/v1` |
| `https://api.deepseek.com/v1` | `https://api.deepseek.com/v1` |
| `https://api.deepseek.com/v1/` | `https://api.deepseek.com/v1` |
| `https://api.deepseek.com/v1/chat/completions` | `https://api.deepseek.com/v1` |

规则：去尾斜杠 → 如果结尾是 `/chat/completions` 则砍掉 → 如果不以 `/v1` 或 `/v\d+` 结尾则补 `/v1`。

**例外**：某些自建服务的路径不含 `/v1`。所以规范化后如果 probe 失败，要用原始 URL 再试一次，两次都失败才报错。

### 探测失败的错误要具体

这是用户体验的关键点。"连不上"这种笼统提示会让人无从下手。

| 情况 | 返回给用户的信息 |
| --- | --- |
| DNS 解析失败 | 域名无法解析，检查 base_url 是否拼写正确 |
| 连接超时 | 连接超时，检查网络或是否需要代理 |
| 401 / 403 | API Key 无效或无权限 |
| 404 | 端点不存在。已尝试 `{规范化后的 url}`，可能该服务的路径不是 `/v1/models` |
| 200 但 JSON 结构不符 | 端点返回了非标准格式，请手动输入模型名 |
| 200 但列表为空 | 该 Key 下没有可用模型 |

**最后一条兜底**：探测失败时仍允许用户手动输入模型名保存。有些中转站故意不开放 `/models`。

## 能力核验

`/models` 端点只给模型 ID，不说明能力。需要额外核验的有两项：

### vision（多模态）

核验方式：发一个极小的测试请求，content 里带一张 1x1 像素的 base64 图片。

```
成功  → vision = true
400 且报错提到 image/vision/multimodal → vision = false
其它错误 → vision = unknown（不阻止使用，但不允许开视觉模式）
```

**核验是按需触发的**，不在 probe 时对所有模型跑一遍——一个供应商可能有几十个模型，全测一遍要几十次请求。

触发时机：用户把某个模型绑到 `vision` 位时，或用户手动点"核验"。

结果落 `model.supports_vision`。核验未通过的模型，前端的"图像认知"开关置灰。

### 上下文窗口

`/models` 端点通常不返回窗口大小（OpenAI 官方返回，多数中转站不返回）。

做法：内置一份 `model_context_windows` 映射表，按模型名**模糊匹配**。

必须处理中转站的前缀污染：

```
openai/gpt-4o                          → gpt-4o
accounts/fireworks/models/qwen-72b     → qwen-72b
Pro/deepseek-ai/DeepSeek-V3            → deepseek-v3
anthropic.claude-3-5-sonnet-20241022   → claude-3-5-sonnet
```

匹配策略：小写化 → 按 `/` 取最后一段 → 去掉常见前缀（`Pro`、`Free`）→ 在映射表里找最长前缀匹配。

匹配不到时默认 32768 并在 UI 上标注"窗口大小未知，已按 32K 处理，可手动修改"。**允许用户手动改**这个值，因为它直接影响压缩阈值。

## 功能位

模型按**用途槽位**配置，不按"能力类型"。

| 功能位 | 用途 | 选型考虑 |
| --- | --- | --- |
| `chat` | 主对话与工具调用 | 最强的那个。必须支持 function calling |
| `vision` | 图片理解 | 需通过 vision 核验。可以和 chat 是同一个 |
| `title` | 自动生成会话标题 | 最便宜的即可，这是一次性的短任务 |
| `compact` | 上下文压缩 | **不能太弱**，见下 |
| `embedding` | 向量化（延后功能） | 可不配 |

### 为什么按功能位而不是"配一个模型全用"

成本与要求差别很大。`title` 位每次调用只花几十 token，用旗舰模型是浪费；`chat` 位用便宜模型则工具调用会频繁出错。混用会让"省钱"和"够用"两个目标都达不到。

### compact 位的特殊性

反直觉但重要：**压缩不是简单任务。**

- 其它位出错的影响是局部的：`title` 错了标题难看，`vision` 错了这张图看错
- **`compact` 错了影响整个会话往后的全部推理**。摘要漏掉关键约定（"用户要求所有函数加类型注解"），模型后面所有产出都不符合要求，而且不会报任何错

所以 compact 位应配和 chat 同档或最多低一档的模型。UI 上在这里放一句提示。

### 绑定解析与降级

```python
async def resolve(purpose: str) -> ResolvedModel:
    """
    1. 查 model_binding 找该功能位绑的模型
    2. 没绑 → 回落到 chat 位
    3. chat 位也没绑 → 抛 ProviderError，提示去设置页配置
    4. 绑的模型被删了（脏数据）→ 回落到 chat 位并发 model_fallback 事件
    """
```

**降级必须可见**。降级本身可以接受，降级不可见不行——用户会困惑"为什么标题生成得这么好/这么差"。发 `model_fallback` 事件让前端提示一次。

## API Key 存储

```
api_key_cipher    TEXT NOT NULL    Fernet 密文，带 "v1:" 版本前缀
key_hint          TEXT NOT NULL    尾 4 位，如 "…a3f9"
```

**没有明文列。** 任何接口都不返回明文，只返回 `key_hint` 供用户辨认。

`v1:` 前缀是为了将来换加密算法时能识别旧密文并平滑迁移。没有版本前缀的话，换算法就得全表重新加密且无法回滚。

加密密钥从 `settings.security.encryption_key` 读（.env）。**这个值缺失时拒绝启动** —— 没有它所有对话都会因解密失败而挂掉，而报错信息是"解密错误"，排查方向完全错。

编辑供应商时不传 `api_key` 字段则保持原值不变，传了才更新。UI 上输入框 placeholder 显示 `key_hint`。

## LLMPort

```python
class LLMPort(Protocol):
    async def stream_chat(
        self,
        model: ResolvedModel,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[LLMChunk]: ...

    async def list_models(self, base_url: str, api_key: str) -> list[str]: ...
```

`LLMChunk` 区分四种增量：`content` / `reasoning` / `tool_call` / `usage`。

`reasoning` 单独一类，因为推理模型（DeepSeek-R1、o 系列）的思维链要在前端折叠显示，不能混在正文里。不同供应商的字段名不一致（`reasoning_content` / `reasoning`），在适配器里归一。

### 只有一个实现，为什么还要 port

测试。`FakeLLM` 直接实现这个 Protocol，返回预设的 chunk 序列，agent loop 的测试就不需要 mock HTTP 或真实调用 API。

见 [../development/testing.md](../development/testing.md)。
