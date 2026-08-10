# 测试策略

## 原则：只测会真出错的地方

个人项目没有精力也没有必要追覆盖率。测试的投入应该按**"出错了多难发现"**分配，不是按代码行数。

| 类别 | 出错时的表现 | 测试力度 |
| --- | --- | --- |
| 静默错误 | 不报错，结果悄悄不对 | **重点测** |
| 跨层契约 | 一端改了另一端不知道 | **重点测** |
| 显性错误 | 直接抛异常/报 400 | 轻测，跑一次就发现 |
| UI 渲染 | 肉眼可见 | 不测，手动验证 |

## 必须测的六块

按重要性排序。这六块的共同点：**错了不会报错。**

### 1. 压缩不拆 tool 对

```python
async def test_compaction_never_splits_tool_group():
    """
    切点落在 tool_calls 与其 tool 结果之间时，API 返回 400。
    这个测试要覆盖：切点正好在 assistant(tool_calls) 之后、
    正好在多个 tool 结果中间、tool 组跨越 tail 边界。
    """
```

构造各种消息序列，压缩后断言：任何 `role=tool` 消息的前面必须能找到声明它的 assistant。

**这是最该有的测试。** 它的失败模式是"某些对话突然全部报 400"，而复现条件依赖于具体的消息序列，手动测很难碰到。

### 2. artifact 不被压缩吃掉

```python
async def test_artifact_survives_compaction():
    """
    压缩后 artifact 必须还在，且内容完整。
    失败模式：用户说"改深一点"时模型手里没代码了，全程零报错。
    """
```

### 3. 取消后 journal 落库完整

```python
async def test_cancel_preserves_journal():
    """
    在 agent_end 之后立即取消，落库的消息必须完整。
    失败模式：只剩 user 消息 + 空的 assistant 占位。
    """
```

用 `FakeLLM` 控制时序，在特定点触发 `task.cancel()`。

### 4. SSE 事件表与前后端实现一致

这是**跨层契约**测试，最容易腐化的地方。

后端侧：

```python
def test_all_emitted_events_are_documented():
    """
    扫描代码里所有 emit() 调用的第一个参数，
    与 docs/api/sse-events.md 里的表对比，两边必须完全一致。
    多了或少了都失败。
    """
```

用 `ast` 解析源码找 `emit(...)` 调用。这个测试能抓到"加了事件忘了写文档"。

前端侧：`EventHandlers` interface 的所有字段必需（不用 `?`），后端加事件时 TS 编译报错。见 [../architecture/frontend-sse.md](../architecture/frontend-sse.md#dispatch)。

两边配合，任一方漏了都会被发现。

### 5. 路径守卫

```python
@pytest.mark.parametrize("path", [
    "../../../etc/passwd",
    "workspace/../../secret",
    "/absolute/outside",
    "C:\\Windows\\System32",
    "workspace/link_to_outside",     # 符号链接
    ".env",                          # HARD_DENY
    "sub/.env",
])
async def test_pathguard_denies(path):
    with pytest.raises(PathDeniedError):
        guard.check(Path(path))
```

安全测试必须是穷举式的参数化测试。**每发现一个新的绕过方式就加一个 case**，永不删除。

同样测拒止锚：目录里放 `.jeeves_blocker` 后，该目录及子目录的访问必须被阻断。

### 6. 模型名归一化

```python
@pytest.mark.parametrize("raw,expected", [
    ("openai/gpt-4o", "gpt-4o"),
    ("accounts/fireworks/models/qwen-72b", "qwen-72b"),
    ("Pro/deepseek-ai/DeepSeek-V3", "deepseek-v3"),
    ("anthropic.claude-3-5-sonnet-20241022", "claude-3-5-sonnet"),
])
def test_normalize_model_name(raw, expected):
    assert normalize_model_name(raw) == expected
```

以及 base_url 规范化的参数化测试（表见 [../architecture/providers.md](../architecture/providers.md#base_url-规范化)）。

失败模式：所有模型都回落到 32K 默认窗口，导致大窗口模型被过早压缩——用户只觉得"怎么老是压缩"。

## FakeLLM

Agent loop 的测试不能真调 API（慢、要钱、不确定）。`FakeLLM` 实现 `LLMPort`，返回预设的 chunk 序列：

```python
class FakeLLM:
    def __init__(self, scripts: list[list[LLMChunk]]):
        """
        scripts[0] 是第一轮的响应，scripts[1] 是第二轮…
        这样可以编排"第一轮调工具、第二轮给答案"这类多轮场景。
        """
        self._scripts = scripts
        self._call_count = 0

    async def stream_chat(self, *args, **kwargs) -> AsyncIterator[LLMChunk]:
        script = self._scripts[self._call_count]
        self._call_count += 1
        for chunk in script:
            yield chunk
```

提供便捷构造器：

```python
fake_text("你好")                        # 纯文本响应
fake_tool_call("read_file", {"path": "a.py"})
fake_with_usage(text, prompt_tokens=50000)   # 用于触发压缩
```

这是 `LLMPort` 存在的主要理由（只有一个真实实现）。

## 测试数据库

每个测试用独立的内存 SQLite：

```python
@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ...
```

**用 `create_all` 而非跑 alembic**。测试关心的是"当前的表结构对不对"，不是"迁移链能不能跑通"。迁移单独测（见下）。

**但要记得开 `foreign_keys=ON`**——SQLite 默认关闭。测试里不开的话，外键约束的 bug 在测试里发现不了，到生产才暴露。

## 迁移测试

```python
def test_migrations_up_and_down():
    """
    在临时文件库上：upgrade head → downgrade base → upgrade head。
    确保每个迁移的 downgrade 都真的可逆。
    """
```

只要一个测试。用文件库不用内存库（alembic 需要独立连接）。

这个测试的价值：写 downgrade 时人总是敷衍，真要回退时才发现回不去。

## 不测的东西

明确列出，避免精力浪费：

| 不测 | 理由 |
| --- | --- |
| React 组件渲染 | 肉眼可见，手动验证更快 |
| CRUD 接口的正常路径 | 打开页面点一下就知道 |
| Pydantic 校验 | 框架的事 |
| SQLAlchemy 查询本身 | 库的事 |
| 真实 LLM 调用 | 慢、要钱、不确定。手动跑一遍就够 |
| 真实 MCP 服务器 | 同上 |
| Docker 沙箱的容器操作 | 需要 Docker 环境，手动验证 |
| 覆盖率数字 | 不设目标，不看报告 |

**唯一例外**：Docker 沙箱的**路径映射转换**要测（纯函数，宿主路径 ↔ 容器路径双向转换）。这是 Docker 后端最容易出 bug 的地方，且错了的表现是"模型说文件不存在但文件明明在"。

## 手动验收清单

每个路线图阶段的验收标准（见 ../guides/路线图）就是手动测试清单。

M2 的例子：

```
 让它在测试目录新建 Python 文件 → 运行 → 根据报错自行修正到跑通
 尝试读白名单外的路径 → 被拒绝，错误信息清楚
 目录里放拒止锚 → 访问被阻断，前端有明显提示
 检查模式下每次执行都等确认
 切到自动模式后不再弹框，且顶栏有 auto 标记
 审批弹框超时后视为拒绝，模型收到"审批超时"
 执行 `python -c "print('中文')"` → 输出无乱码
 执行一个死循环 → 120s 后超时并 kill 掉，无孤儿进程残留
```

最后一条要用任务管理器确认没有残留进程——进程组 kill 写错了很难发现。

## 手动验收里最该反复做的三件事

这三件事覆盖了最多的隐性 bug：

1. **长对话到触发压缩**，然后追问早前的约定。检查模型是否还记得，以及产物是否还在。
2. **生成中途强制取消**，然后刷新页面。检查消息记录完整、无空占位、无孤儿进程。
3. **换一台机器（或换个目录）clone 后从零跑 setup**。这能一次性暴露所有路径解析、初始化、依赖问题。

第 3 条至少在每个路线图阶段结束时做一次。它是唯一能发现"只在我这台机器上能跑"这类问题的方法。

## 运行

```bash
uv run pytest                    # 全部
uv run pytest -k compaction      # 只跑压缩相关
uv run pytest -x                 # 第一个失败就停
```

`pyproject.toml`：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"            # 不用给每个 async 测试加 @pytest.mark.asyncio
testpaths = ["backend/tests"]
```
