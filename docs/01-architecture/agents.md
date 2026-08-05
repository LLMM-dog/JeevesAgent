# 智能体注册表与 SubAgent

## AgentSpec

一个智能体 = 提示词 + 工具集 + 若干开关。用 frozen dataclass 声明：

```python
@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str              # 给主智能体看的"什么时候派这个子智能体"
    prompt_key: str               # prompts/ 下的文件名
    tool_factories: tuple[ToolFactory, ...]
    produces_artifact: bool       # 产出是否存为 artifact
    allow_final: bool             # 能否直接给出最终答复（子智能体通常为 False）
    max_turns: int
```

```python
SPECS: dict[str, AgentSpec] = {
    "main": AgentSpec(...),
    "researcher": AgentSpec(...),
    "coder": AgentSpec(...),
}
```

### 为什么值得抽出来

新增一个智能体只需在 `SPECS` 里加一条，然后：

- 图的接线自动跟上（`build_agent_graph` 是通用的）
- 工具注册自动跟上（`tool_factories`）
- 模型绑定自动跟上（按 `agent_name` 查 `model_binding`）
- 记忆线自动隔离（`message.agent_name`）
- `subagent` 工具的可选目标列表自动包含它

没有这层抽象的话，新增一个智能体要改五六个地方，漏一处就出现"子智能体能跑但没有记忆"这类问题。

## 两阶段接线

问题：主智能体需要能派 `researcher`，而 `researcher` 在某些场景下也需要能派 `coder` —— 构造时互相引用，死循环。

解法：

```python
def build_agent_mesh() -> dict[str, Agent]:
    # 阶段 1：建所有实例，此时都不带 subagent 工具
    agents = {name: Agent(spec) for name, spec in SPECS.items()}

    # 阶段 2：互相注册 SubAgentTool
    for name, agent in agents.items():
        targets = {n: a for n, a in agents.items() if n != name}
        agent.registry.register(SubAgentTool(targets))

    return agents
```

## 智能体的记忆线

`message.agent_name` 字段：

- 空串 `""` = 用户可见的主线
- 有值 = 该智能体的私有记忆线

一个会话里并存多条线，互不污染。主线里只有用户输入和主智能体的最终答复；子智能体的中间过程在它自己的线里。

**这是"每个智能体有自己的记忆"的全部实现**。组装上下文时按 `(session_id, agent_name)` 过滤即可。

前端默认只显示主线，展开某个 subagent 卡片时才拉它的线。

## SubAgent 是一个工具

不做独立的编排层。`subagent` 就是一个普通工具：

```python
async def run(self, ctx, agent: str, task: str) -> ToolResult:
    """
    agent: 目标智能体名
    task:  完整的任务描述（子智能体看不到父会话的历史，必须自包含）
    """
```

### task 必须自包含

子智能体**不继承父会话的消息历史**。它只看到 `task` 这一段文字。

这是刻意的：继承历史的话，子智能体的上下文和父会话一样大，"派子智能体来省上下文"的目的就落空了。

代价是主智能体必须把必要背景写进 `task`。工具描述里要强调这点：

```
task 必须是自包含的完整描述。子智能体看不到当前对话，
不要写"按上面说的做"、"继续刚才的任务"这类依赖上下文的表述。
```

### 深度限制是双保险

**第一道**：`subagent` 不在任何子智能体的工具白名单里（`specs.NEVER_FOR_SUBAGENT`）。模型看不到的工具不会去调，比调了被拒更省一轮。

**第二道**：ContextVar 深度计数，上限 2。

为什么要两道 —— 白名单是**配置**，配置会被写错。有实现只靠白名单，它的 `worker.md` 没写 `tools:` 字段就拿到全集，成了潜在的无限递归口子（而有的实现 没有任何深度防护）。

#### 为什么必须用 ContextVar 而不是全局计数器

注释说得最清楚：

> 深度追踪 — 使用 ContextVar 实现每个 asyncio Task 独立的计数。
> 并发调用（同一层级的多个子 Agent）互不干扰；
> 链式递归（子 Agent 再调子 Agent）才会递增深度。

全局计数器会把 5 个并行子代理误判成深度 5。实测验证过：8 个并发委派，每个分支看到的深度都是 1。

`finally` 里用 `reset(token)` 恢复而非 `-1` 递减 —— 异常场景下递减会算错。

### 并发上限、超时、取消级联

三件必须一起做，缺一不可。它们是同一类问题：**资源失控**。

```python
MAX_PARALLEL = 6        # 一次 tool_calls 里最多几个委派
MAX_CONCURRENCY = 3     # 实际同时跑几个
TIMEOUT_S = 600.0       # 单个子代理墙钟上限
```

常见实现各自只做了一到两件：

| |  |  | 同类实现 | 本项目 |
| --- | --- | --- | --- | --- |
| 并发上限 | 无 | 无 | 8/4 | 6/3 |
| 超时 | **无** | **无** | **无** | 600s |
| 取消级联 | **不级联** | 有 | 有 | 有 |

**超时是常见实现共同的缺陷** —— 说明这是个容易被忽略的点。后果分别是： 永久占 worker slot、 永久阻塞父代理（裸 `await pending_future`）、同类实现 永久占并发槽位。

超时要转成**给模型的错误字符串**，不向上抛 —— 父代理应该能决定"拆小重试"还是"自己做"。

取消级联靠 `await` 直连（子任务天然挂在父 Task 树上）。 做不到是因为它把子代理丢给了全局 worker 的独立 `asyncio.Task`，与父代理无 Task 树关系 —— 用户 Ctrl+C 后子代理继续烧 token。

### 返回什么：必须截断，且分层可见

子智能体的**最终答复文本**作为 `ToolResult.content` 返回。中间过程不返回。

**50KB 硬上限**，按 UTF-8 字节截断（不是字符 —— 中文一个字符 3 字节，按字符算能塞进三倍的量）。

```
模型可见   截断后的结论 + "完整结果在工具详情里，不需要重新委派"
UI 可见    display.full_text 全量
```

截断提示必须**告诉模型全量在哪**，否则它以为信息丢了，可能重新派一次子代理去拿 —— 那比不截断更贵。

#### 为什么这条不能省

委派存在的**唯一理由**是省上下文。结果原样回灌等于把污染从"过程"搬到了"结果"，收益归零。

 是最直接的反例：

```python
outputs: Annotated[str, operator.add]        # type_def.py:239
```

子代理跑一小时的全部文本输出累加后，通过 `query_sub_assistant` 一次性进父上下文。讽刺的是  恰好是常见实现里上下文压缩做得最好的那个。

有的实现是唯一做对的，而且是**两端都踩过**才定在 50KB —— 最早只回 100 字符，模型看不懂发生了什么（CHANGELOG #4710）。

### token 归集

工具事件带 `agent_name`，子代理的 token 算在它自己头上，前端卡片上直接显示。

少见实现做了 token 聚合，另两个的子代理开销是黑洞 —— "这次委派花了多少钱"无法回答。

**不能靠 span depth 判断归属**：`emit` 读的是**当前** span，工具执行时 agent span 已不在栈顶，`tool_end` 拿到的 depth 是 0。这个坑，它同时造出了假阴性和假阳性。

## 两个内置子智能体

只放两个。多了反而让主模型难选 —— 委派本身是有成本的决策，候选太多它会花轮次在"该派谁"上。

两个都是**只读**的。

### researcher

调研型：读大量文件后给结论。工具集 `read_file` / `list_dir` / `glob` / `grep` / `load_skill*`。

为什么单独抽出来：调研会产生大量中间内容，全塞进主会话会迅速撑爆。实测同一个"读 6 个文件提取结论"的任务，父上下文从 8399 降到 5489 token。

### reviewer

代码审查型：只报告具体问题（带 `文件:行号`），不改代码。

提示词里明确写了"**编造问题比漏掉问题更糟**" —— 它会让对方浪费时间去改不存在的缺陷。

### 用户可覆盖

`agents/*.md`，同名**覆盖内置**。内置的只是默认值，想改 researcher 的提示词应该能直接改，不用换个名字。

```markdown
---
name: researcher
description: 当需要读大量文件后给结论时派它
tools: read_file, glob, grep
max_turns: 20
---

（正文就是 system prompt）
```

`description` 是主模型选择子代理的唯一依据；正文为空则跳过 —— 没有独立人格的子代理会像跟人聊天一样回答父代理（子代理就是这样，它与主 Agent 用**完全相同**的 system prompt）。

不声明 `tools` 时给保守默认（`read_file` / `list_dir` / `glob` / `grep`），**不是全集**。

## 每个智能体可绑不同模型

**模型不写在 spec 的 frontmatter 里**，走 `model_binding` 表的 `agent_name` 字段（在设置页绑定）。

理由：模型是**部署期**决定的，和"这台机器上配了哪些供应商"绑在一起；而 spec 是**设计期**的资产，要能跨机器复用。把 `model_id` 写进 md 会让定义在另一台机器上直接失效 —— 那台机器上未必有同名模型。

有实现把 `model:` 写进 frontmatter（`agents.ts`），对它的单机 CLI 场景合适；本项目是多供应商配置的服务端，两者约束不同。

`model_binding` 表带 `agent_name` 字段。解析顺序：

```
1. (agent_name, purpose) 精确匹配
2. ("", purpose) 全局默认
3. ("", "chat") 兜底
4. 都没有 → ProviderError
```

用途：`researcher` 可以配便宜的长上下文模型，`coder` 配最强的。

降级时发 `model_fallback` 事件，见 [providers.md](providers.md#绑定解析与降级)。
