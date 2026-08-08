# 第三层：代码级强制执行计划

> 将 AGENTS.md 中的行为规则从"建议"升级为"硬约束"。
> 每条规则对应一个具体的钩子实现，不改 AgentLoop。

---

## 规则 → 钩子映射

| AGENTS.md 规则 | 违反表现 | 钩子点 | 强制执行方式 |
|---------------|----------|--------|-------------|
| "先了解现状再动手" | Agent 上来就 write_file，之前没 read_file | `BEFORE_TOOL` | write_file/edit_file 前检查本轮是否调过 read_file/grep/glob |
| "一次只做一个步骤" | 一个 turn 里连续 3 次 write_file | `AFTER_TOOL` | 累计写操作数，超过阈值注入"慢一点"提示 |
| "依赖方向：不依赖后面的步" | todo_write 的计划有反向依赖 | `AFTER_LLM` | 解析 todo_write 调用的 steps 列表，检测循环 |
| "立即验证" | write_file 后直接开始下一个 write_file | `AFTER_TOOL` | 写操作后计数，第 2 次写之前没跑过 run_shell test → 注入验证提示 |
| "出错停下来分析" | 同一个工具连续 3 次 is_error=True | `AFTER_TOOL` | 累计连续失败，超过阈值注入"停下来分析"提示 |
| "完成了要验证" | Agent 说"完成"但没跑过测试 | `SHOULD_STOP` | final_text 不含"测试"且本会话从未调过 run_shell → block |

---

## 实现计划

### 1. "先读再写"守卫 (`BEFORE_TOOL`)

```python
# hooks_builtin.py — 挂在 ToolRegistry.hooks 上

def require_read_before_write(ctx: BeforeToolContext) -> str | None:
    """write_file/edit_file 之前，本轮必须至少调过一次 read_file/grep/glob。"""

    if ctx.tool_name not in ("write_file", "edit_file"):
        return None

    # 检查本轮（自上次 user 消息以来）是否调过读取工具
    recent_tools = _get_tools_since_last_user_message(ctx.session_id)
    has_read = any(t in ("read_file", "grep", "glob", "list_dir") for t in recent_tools)

    if not has_read:
        return (
            "请先读取相关代码再修改。"
            "使用 read_file / grep 了解当前实现后再调用 write_file。"
        )
    return None
```

**复杂度**：需要追踪"本轮调过哪些工具"——可以在 `AFTER_TOOL` 钩子里维护一个 `session_id → [tool_names]` 的 dict。或者更简单：检查 `ctx.ctx.extra` 里是否有上游传递的本轮已调工具列表。

**备用简化方案**：不精确追踪，只在 system_reminder 里强调"先读再改"。等观察到 Agent 频繁违规再加代码守卫。

---

### 2. "一次只做一步"限流 (`AFTER_TOOL`)

```python
# hooks_builtin.py

_write_count_by_session: dict[str, int] = {}

def limit_writes_per_turn(ctx: AfterToolContext) -> None:
    """连续写操作超过 3 次时，下一次 LLM 调用注入提醒。"""

    if ctx.tool_name not in ("write_file", "edit_file"):
        _write_count_by_session[ctx.session_id] = 0  # 遇到非写操作重置
        return

    _write_count_by_session.setdefault(ctx.session_id, 0)
    _write_count_by_session[ctx.session_id] += 1
    # 副作用：设置一个 marker，_reason 读它决定是否注入额外提醒
```

**复杂度**：需要全局 dict 跨钩子通信。可以和 `_reason` 里的 system_reminder 机制配合——`AFTER_TOOL` 设置 flag，`_reason` 读 flag 决定是否加 "慢一点" 提示。

---

### 3. "立即验证"注入 (`AFTER_TOOL`)

```python
_unverified_writes: dict[str, int] = {}

def inject_verification_after_writes(ctx: AfterToolContext) -> None:
    """写操作后累计未验证次数，超阈值标记需要注入验证请求。"""

    if ctx.tool_name in ("write_file", "edit_file"):
        _unverified_writes.setdefault(ctx.session_id, 0)
        _unverified_writes[ctx.session_id] += 1

    elif ctx.tool_name == "run_shell" and _is_test_command(ctx.args.get("command", "")):
        _unverified_writes[ctx.session_id] = 0  # 跑测试了，重置
```

`_reason` 在注入 system_reminder 时检查 `_unverified_writes[session_id] >= 2`，如果是就追加 "你已经改了 2 个文件还没跑测试，请先验证"。

---

### 4. "出错停下来"检测 (`AFTER_TOOL`)

```python
_consecutive_errors: dict[str, int] = {}

def detect_error_loop(ctx: AfterToolContext) -> None:
    """同一工具连续失败 3 次时标记。"""

    if ctx.result.is_error:
        _consecutive_errors.setdefault(ctx.session_id, 0)
        _consecutive_errors[ctx.session_id] += 1
    else:
        _consecutive_errors[ctx.session_id] = 0
```

`_reason` 检查 >= 3 时注入 "你已经连续失败 3 次了。停下来分析根因，不要继续微调。"

---

### 5. "完成了要验证"守卫 (`SHOULD_STOP`)

```python
def require_verification_before_stop(ctx: ShouldStopContext) -> str | None:
    """Agent 说完成但没有验证证据时，阻止停止。"""

    verified_markers = ["测试通过", "test pass", "验证通过"]
    if any(m.lower() in ctx.final_text.lower() for m in verified_markers):
        return None

    return (
        "[系统] 请先验证你的修改。"
        "使用 run_shell 执行测试命令，确认通过后再结束。"
    )
```

---

## 实施优先级

| 优先级 | 规则 | 理由 |
|--------|------|------|
| **先做** | "完成了要验证" (#5) | 最简单（纯文本匹配），效果最明显 |
| **再做** | "出错停下来" (#4) | 防死循环，改动小 |
| **后做** | "立即验证" (#3) | 需要追踪写操作，复杂度中 |
| **最后** | "一次只做一步" (#2) | 需要跨轮计数 |
| **观望** | "先读再写" (#1) | 当前 system_reminder 已覆盖，等观察到违规再加 |

---

## 实现方式

所有守卫放在 `backend/app/modules/agent/hooks_builtin.py`，在 `chat_service.py` 创建 AgentLoop 后统一挂载。不改 AgentLoop 一行代码——全部通过已有的 6 个钩子点实现。
