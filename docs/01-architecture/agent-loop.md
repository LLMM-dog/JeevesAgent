# Agent Loop

## 循环结构

一个直白的 `while` 循环，不用 LangGraph。实现在 `backend/app/modules/agent/loop.py`。

```
                 ┌────────────────────────────┐
                 │                            │
  load_context → reason ──有 tool_calls?──→ act ──┘
       │           │
   (修复配对)      └──无──→ 返回 final
```

`AgentLoop` 是**通用引擎**，主智能体和所有子智能体复用同一份代码，区别只在注册了什么工具、用了什么提示词。这是 `AgentSpec` 存在的意义（见 [agents.md](agents.md)）。

不做更复杂的结构。plan/execute/reflect 那类多节点编排在实践中收益有限而调试成本高——模型自己就会规划，把规划固化成节点反而限制它。

### 为什么不用 LangGraph

**这一节推翻了本文档的初始设计。** 写实现时发现 LangGraph 的三项核心机制在本项目全都用不上，而这个判断在四个常见实现的源码里都能找到印证。

| LangGraph 提供 | 本项目的实际情况 |
| --- | --- |
| 状态自动提交 + reducer | 压缩要**整体重写** `messages`（删中段、插摘要），reducer 只能追加。 为此在 `context_summary` 的**五个返回分支全部用 `Overwrite`** 显式关掉 reducer（`main_agent_node.py:233/258/330/358/377`），并额外需要一个 `messages_persist` 节点专门落库 |
| checkpointer 恢复状态 | 我们从 DB 重组 + `repair_tool_pairing` 无条件修一遍。** 也不用 checkpointer**（相关实现 是裸 `compile()`）。选了 `MemorySaver` 的  为此背了 80 行 `aupdate_state` 反写和一个 `as_node` 坑，且只能修取消，修不了崩溃/断电/手改库 |
| 回调与 trace 体系 | 事件全部走自己的 `EventBus` 显式 emit。 给 6 个节点里的 5 个加了 `trace=False`来关掉它，只为保留两个 `astream_events` hook |

剩下的价值只有图的可视化（`draw_mermaid_png`），代价是多一层间接。

**M6 的多智能体编排也不是理由。**  是四个项目里唯一实现了多智能体的，它用的是独立 `asyncio.Task` + 队列，**没用子图、没用 `Send`**。而且 `Send` 派发的分支无法单独取消，`stop_sub_agent` 靠 `self._running_tasks[task_id].cancel()` 精确停单个子 agent，`Send` 做不到。

`langgraph` 依赖暂时保留在 `pyproject.toml` 里，等 M6 确认不需要后移除。

## 状态

`AgentLoop` 的实例字段，不是 TypedDict：

```python
self.messages: list[Msg]        # 工作副本，压缩时整体重写
self.journal: list[Msg]         # append-only 流水，落库读这个
self._last_prompt_tokens: int   # 上一轮的真实 usage，压缩触发依据
self._last_call_sig              # 打转检测
self._repeat_count
```

### messages 与 journal 的分离

`messages` 是**发给 LLM 的工作副本**，压缩会重写它。
`journal` 是**append-only 的完整流水**。

压缩只动 `messages`，绝不动 `journal`——所以库里存的永远是完整原始对话，用户在前端能看到全部历史，即使模型自己已经"忘了"中段。

`journal_sink` 参数允许外部传入一个普通 list 持有流水，供子智能体编排使用。

### 落库顺序：先 DB 再内存

```python
async def _persist(self, msg: Msg, **kw) -> None:
    await repo.append_message(...)   # 先落库
    self.messages.append(msg)        # 成功后才入内存
    self.journal.append(msg)
```

**顺序反了会产生一个只在落库失败时暴露的 bug**：消息留在 `self.messages` 里，于是 `find_missing_tool_calls` 认为它已被应答、跳过补占位，而库里其实没有它——孤立 `tool_call` 就留下来了。

正常路径上完全看不出差别。有测试覆盖（`test_loop_guards.py::test_persist_failure_mid_batch_leaves_no_orphan`）。

### 落库时机

不等图跑完再落库。每产生一条消息就写一次：

- `user` 消息：请求刚进来就写，此时还没调 LLM
- `assistant` 消息：`reason` 节点拿到完整响应后立即写
- `tool` 消息：每个工具执行完立即写
- `artifact`：产物工具执行完立即写（upsert，见 [context.md](context.md#artifact)）

理由：取消和崩溃随时可能发生，只有立即落库才能保证"看到的就是存下的"。

## reason 节点

```
1. 组装上下文（见 context.md 的组装顺序）
2. 检查是否需要压缩 → 需要则先压缩
3. 调 LLM（流式）
4. 逐 chunk 发 thinking / message 事件
5. 拿到完整响应 → 写 assistant 消息 → append 到 journal sink
6. 记录真实 usage
7. 返回
```

### 流式与非流式共用一套代码

不加 `stream: bool` 参数。判断依据是**当前有没有事件订阅者**：

```python
# core/events.py 的 emit() 在无订阅者时静默 no-op。
# 所以这里永远走"流式"代码路径，非流式调用方只是收不到事件而已。
# 这避免了两套代码路径的分叉——两套一定会不同步。
```

LLM 那一侧始终用流式请求，因为非流式在长推理时更容易被中间层掐断。

### 真实 usage 优先

压缩的触发依据必须是**上游返回的真实 `usage.prompt_tokens`**，不是本地 tiktoken 估算。

估算与真实值的偏差在有工具定义、有 system 提示词、有图片时可以差 20% 以上。用估算触发会出现两种坏情况：估高了白压缩，估低了直接 400。

首轮请求拿不到 usage（还没调过），此时用估算值做保守判断。之后一律用真实值。

## act 节点

```
1. 取 assistant 消息里的 tool_calls
2. 逐个（顺序，不并行）执行：
   a. 发 tool_start 事件
   b. 若工具需审批且当前是 manual 模式 → 发 approval_required，阻塞等结果
   c. ToolRegistry.execute()
   d. 写 tool 消息 → append 到 journal sink
   e. 发 tool_end 事件
3. turn += 1
4. 返回
```

### 为什么不并行执行工具

模型经常在一轮里发出多个 `tool_calls`，理论上可并行。这里选择**顺序执行**。

两个用并行的常见实现都有问题：

-  直接用 LangGraph 的 `ToolNode`（相关实现，原生并行），**完全没有写冲突防护**。
-  自己包了 `ToolNode`，但只覆盖了 `_arun_one` 的异常处理，**并行调度没动**。它对冲突的应对是在模型层面阻止——一个 `conflict_tool_set = {"write_todos", "write_memory", "load_skill"}` 工具名黑名单+ prompt 告知，检测到重复就重试。这是工具名级别的黑名单，不是文件路径级的冲突检测。

顺序执行的其它好处：审批流程不会同时弹三个框；前一个工具失败后续可以跳过（模型下一轮会重新规划），并行则白跑。

并行收益主要在纯读取场景，等真的成为瓶颈再优化。

## 四项保护

这些都属于"没有它也能跑，但生产里会咬人"。全部有测试覆盖（`test_loop_guards.py`）。

### 1. 截断保护

`finish_reason == "length"` 时，最后一个 `tool_call` 的 `arguments` 可能是半截 JSON，例如 `{"path": "/very/long/pa`。

**必须整批作废**，返回错误文本让模型用完整参数重发。

不做的后果很具体：`parsed_args()` 对坏 JSON 返回空 `dict`（这是刻意的，见 [tools.md](tools.md)），于是 `write_file()` 会**以空参数被真的执行**。这类后果不可逆。

注意 `_Accum.to_msg` 里丢弃无名 call 的逻辑挡不住这种情况——被截断的 call 通常 `name` 已经完整，只有 `arguments` 是半截的。

### 2. 无产出的响应当可重试错误

判据是**没有正文，也没有工具调用**。思维链不算产出。

```python
@property
def is_unusable(self) -> bool:
    return not self.content and not self.calls
```

不做的后果：`final_text` 保持上一轮的值，用户看到一个和问题对不上的旧答案，全程无报错。中转站在限流或内部截断时返回空 choice 是常见的。

**判据不能写成"三者全空"**（含思维链）。推理模型有两种形状：

| content | reasoning | tool_calls | 判定 |
| --- | --- | --- | --- |
| 空 | 有 | 有 | 正常，真实 deepseek 就这样 |
| 空 | 有 | 空 | **不可用**，思考阶段耗尽了 max_tokens |

第二种会因为 `reasoning` 非空而被"三者全空"判为有效。详。

重试时区分成因：思维链被截断的情况附带一次"少想多说"的提示，否则原样重发大概率再次耗尽预算。

 明确当错误处理（相关实现 抛 `InvalidOutputsError`），走重试分支。 没有这个保护。

### 3. 单个工具超时

`settings.agent.tool_timeout` 默认 300 秒，包在 `registry.execute` 外面。

四个常见实现都没有这层统一超时（它们的 timeout 分散在各个工具内部，`run_python_code` 就完全没有）。缺了它，一个忘设 timeout 的工具能挂死整个 run，而 SSE 心跳让前端看不出异常，用户只是一直等。

我们有 `registry.execute` 这唯一入口，加在这里能全局兜住。`wait_for` 产生的 `CancelledError` 会被转成 `TimeoutError`，不污染真正的取消路径。

### 4. 跨轮打转检测

连续 `max_repeat_calls`（默认 3）轮以**完全相同的 (工具名, 参数)** 调用后，注入一次提示让模型换路。

四个常见实现都没有跨轮检测（ 只有单轮内的 `conflict_tool_set` 去重）。但 `max_turns=40` 下打转会烧掉 40 次调用才停，而检测只需几行。

用 `==` 而非 `>=` 判定，所以只注入一次，不会反复刷。与下面的轮次催促同一思路。

## 推理模型（思维链）

已用 deepseek-v4-pro 真实验证，观测到的行为：

- 一次对话 109~311 个 `thinking` 事件，思维链 168~624 字符
- **tool_call 轮次的 `content` 常常是 0 字符**，思维链才是它这一步的全部思考
- 思维链逐轮独立，不累积

### 思维链的三条规则

**一、逐字发 `thinking` 事件**，前端才能显示"正在思考"。与正文用不同事件名，前端分开渲染（思维链默认折叠）。

**二、`content` 为空不是异常**。见上面「四项保护」第 2 条——判据不能把思维链算作产出。

**三、只在带 `tool_calls` 的轮次回传给上游**：

```python
if self.tool_calls:
    out["tool_calls"] = [...]
    if self.reasoning and settings.llm.send_reasoning_back:
        out["reasoning_content"] = self.reasoning
```

DeepSeek 文档的规则是：两个 user 消息之间有工具调用时 `reasoning_content` 必须传回（让模型延续推理），没有则不必传、传了也会被忽略。文档说带 `tools` 不传会 400，**实测四种组合全返回 200**——文档比实际严格。

但质量理由是实质的：tool_call 轮次的正文是空的，丢掉思维链等于让模型每步从头想。

`llm.send_reasoning_back` 可关闭，用于对未知字段严格的端点。

### 落库

思维链存 `message.reasoning`，与 `content` 同级。压缩时**不保留**思维链——它对重建上下文没有价值，而且占的 token 不少。

## 循环终止

```python
if turn >= settings.agent.max_turns:
    # 达到上限不是异常，是保护。写明 stop_reason，前端显示"已达最大轮次"。
    return LoopResult(stop_reason="max_turns", ...)
```

`max_turns` 默认 40。这个值要够大——一个真实的代码修改任务轻易用掉 15~25 轮。设小了会在任务快完成时被截断，比多花点 token 糟糕得多。

到 80%（`warn_turn_ratio`）时**注入一次催促而非硬停**。模型收到催促通常会收敛给结论，硬停则留下半成品。照抄 `SYSTEM_ALERT_PROMPT` 机制，包括用 `==` 只注入一次的技巧。

## 重试

分两层，职责不同：

| 层 | 处理什么 | 为什么必须在这层 |
| --- | --- | --- |
| `openai_compat.stream_chat` | 连接失败、超时、429 | 对用户完全透明 |
| `AgentLoop._reason_with_retry` | `context_overflow`、空响应 | adapter 不知道 messages，压缩不了 |

### adapter 层的重试只能覆盖首个 chunk 之前

```python
started = False
try:
    ...
    async for chunk in self._iter_sse(resp):
        started = True      # 一旦吐过内容就不再重试
        yield chunk
except ... as e:
    if started or not self._should_retry(e) or attempt >= max_retries:
        raise
```

**已经 yield 过内容后重试，用户会看到重复的文字。** 状态码检查发生在任何 `yield` 之前，那一段是安全的重试窗口。

### 错误分类决定应对方式

`classify_error()` 把上游错误分成三类，照抄 `guess_exception_type`的两级匹配结构（先整词组，再 对象×动作 交叉）：

| 类别 | 应对 |
| --- | --- |
| `token_exceed` | **先压缩再重试**。直接重试会再超一次 |
| `rate_limit` | 指数退避后重试 |
| `others` | 视为永久错误，立即上抛 |

顺序很重要：`rate_limit` 必须先判。`"Limit 30000 tokens per min"` 同时含 token 和 limit 类词，判成 `token_exceed` 会白压缩一次还是失败。

压缩在 M1 实现。M0 的 `_force_compact()` 返回 `False`，让错误如实抛出——比假装压缩成功后再撞一次墙好。

## 取消

取消粒度是 **run**，不是 session。

```
POST /api/runs/{run_id}/cancel
  → run_registry 里找到该 run 的 asyncio.Task
  → task.cancel()
  → 图在下一个 await 点抛 CancelledError
  → 外层 catch → 补齐孤立 tool_calls（见下）→ 发 cancelled 事件
    → 用 journal sink 落库 → 清理注册表
```

### 取消必须补齐孤立的 tool_calls

工具执行中途被取消时，DB 里会留下一条带 `tool_calls` 的 assistant 消息，但没有对应的 `tool` 消息。**下一轮把这段历史发给 LLM 会直接 400。**

两处补齐，双重保险：

1. 取消处理里：为缺失的 `tool_calls` 补一条 `role=tool`、`is_error=1`、内容"用户取消了该工具调用"的消息。
2. **组装上下文时无条件校验**（见 [context.md](context.md#组装前的一致性校验)）。这是兜底——进程崩溃、断电走不到第 1 步，但会产生同样的不一致。

只做第 1 条的话，任何非正常退出都会留下一个**永久坏掉的会话**：每次打开都 400，用户只能删掉重开。

完整分析。

`run_registry.py` 是一个进程内 dict：`run_id → asyncio.Task`。单进程单用户，不需要分布式方案。

**补齐必须覆盖所有非正常退出路径**，不只是取消：

```python
except asyncio.CancelledError:
    await self._fill_missing_tool_results("用户取消了该工具调用")
    raise
except ProviderError as e:
    await self._fill_missing_tool_results("工具调用因上游错误中断")
    return LoopResult(stop_reason="error", ...)
except Exception:
    # 落库失败、事件总线异常这类。它们发生在
    # "assistant 已落库、部分 tool 结果已落库"的中间态。
    await self._fill_missing_tool_results("工具调用因内部错误中断")
    raise
```

注意工具自身的异常**不会**走到这里——`registry.execute` 一律把它们转成错误文本（那是它的铁律），所以工具失败时配对天然完整。

## 错误处理

| 错误来源 | 处理 |
| --- | --- |
| LLM 连接失败 / 超时 / 429 | adapter 层指数退避重试（仅限首个 chunk 之前），失败则上抛 |
| LLM 返回上下文超长 | loop 层先压缩再重试，压不动则上抛 |
| LLM 返回空响应 | loop 层直接重试，上限 `max_llm_retries` |
| LLM 返回 400（key 错/模型名错/参数错） | **不重试**。立即发 `error` 事件 |
| 工具执行异常 | 转错误文本给模型，run 继续。见 [tools.md](tools.md#异常处理) |
| 工具执行超时 | 转错误文本给模型，run 继续 |
| 工具审批被拒 | 转"用户拒绝执行"文本给模型，run 继续 |
| MCP 服务器不可用 | 该服务器的工具从列表里消失，其余不受影响 |
| 达到 max_turns | 正常结束，`stop_reason="max_turns"` |
| 落库失败等意外 | 补齐 tool 结果后上抛，由 chat_service 转成 `error` 事件 |

区分"程序 bug/永久错误"和"外部瞬时故障"很重要：瞬时故障重试，永久错误立即暴露。把 key 错误拿去重试会退避 8 次然后给出同样无用的提示，还让用户白等半分钟。
