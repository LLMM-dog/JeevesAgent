"""
SubAgent 的测试。

覆盖这几个容易做错的地方：

| | | | 同类实现 |
| --- | --- | --- | --- |
| 返回值截断 | 无（operator.add 无限累加） | 无 | 有（50KB） |
| 并发上限 | 无 | 无 | 有 |
| 超时 | **无** | **无** | **无** |
| 取消级联 | **不级联** | 有 | 有 |
| 递归防护 | 硬禁深度 1 | ContextVar 计数 | **无** |
| 状态隔离 | 黑名单式 {**parent} | 白名单 | 进程隔离 |

超时是常见实现共同的缺陷，所以这里必须有测试。
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.modules.agent import specs as agent_specs
from app.modules.agent.specs import (
    DEFAULT_TOOLS,
    MAX_DEPTH,
    NEVER_FOR_SUBAGENT,
    AgentSpec,
    load_specs,
)
from app.modules.agent.tools.base import ToolContext, ToolRegistry, ToolResult
from app.modules.agent.tools.subagent import (
    OUTPUT_CAP_BYTES,
    SubAgentTool,
    current_depth,
    truncate_for_model,
)


def mk_ctx(tmp: Path, *, depth: int = 0, registry: ToolRegistry | None = None) -> ToolContext:
    return ToolContext(
        session_id="s",
        run_id="r",
        workspace=tmp,
        db=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        depth=depth,
        registry=registry,
    )


@pytest.fixture(autouse=True)
def clean() -> Any:
    agent_specs.reset()
    yield
    agent_specs.reset()


class TestSpecLoading:
    def test_builtins_present(self) -> None:
        reg = load_specs(Path("does-not-exist"))
        assert "researcher" in reg.specs
        assert "reviewer" in reg.specs

    def test_readonly_agents_have_no_write_or_exec(self) -> None:
        """
        researcher 和 reviewer 必须是只读的。

        给了写入权限的"审查者"会直接改代码 —— 而派它的人只想要意见。
        """
        reg = load_specs(Path("does-not-exist"))
        for name in ("researcher", "reviewer"):
            tools = set(reg.specs[name].tools)
            assert not (tools & {"write_file", "edit_file", "run_shell", "run_python"}), (
                f"{name} 拿到了写入或执行权限"
            )

    def test_user_spec_overrides_builtin(self, tmp_path: Path) -> None:
        """
        用户同名定义覆盖内置。内置只是默认值，用户想改 researcher 的提示词
        应该能直接改，不用换个名字。
        """
        (tmp_path / "researcher.md").write_text(
            "---\nname: researcher\ndescription: 自定义调研员\n---\n\n自定义提示词",
            encoding="utf-8",
        )
        reg = load_specs(tmp_path)
        assert reg.specs["researcher"].description == "自定义调研员"
        assert "自定义提示词" in reg.specs["researcher"].prompt

    def test_missing_description_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "bad.md").write_text("---\nname: bad\n---\n\n正文", encoding="utf-8")
        reg = load_specs(tmp_path)
        assert "bad" not in reg.specs
        assert reg.diagnostics

    def test_empty_body_skipped(self, tmp_path: Path) -> None:
        """
        正文就是 system prompt。空的话子代理没有人格 —— 子代理
        就是这样（与主 Agent 用完全相同的 prompt），它会像跟人聊天一样回答
        父代理，带寒暄带反问，父代理还得再花一轮解析。
        """
        (tmp_path / "empty.md").write_text(
            "---\nname: empty\ndescription: 有描述但没正文\n---\n\n   \n",
            encoding="utf-8",
        )
        reg = load_specs(tmp_path)
        assert "empty" not in reg.specs

    def test_tools_string_form(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text(
            "---\nname: a\ndescription: d\ntools: read_file, grep glob\n---\n\n提示词",
            encoding="utf-8",
        )
        reg = load_specs(tmp_path)
        assert set(reg.specs["a"].tools) == {"read_file", "grep", "glob"}

    def test_tools_list_form(self, tmp_path: Path) -> None:
        (tmp_path / "b.md").write_text(
            "---\nname: b\ndescription: d\ntools:\n  - read_file\n  - grep\n---\n\n提示词",
            encoding="utf-8",
        )
        reg = load_specs(tmp_path)
        assert set(reg.specs["b"].tools) == {"read_file", "grep"}

    def test_no_tools_gets_conservative_default_not_everything(
        self, tmp_path: Path
    ) -> None:
        """
        不声明 tools 时给保守默认，【不是全集】。 同类实现.md 没写 tools 字段就拿到全集，成了潜在的无限递归口子
        —— 而有的实现 没有任何深度防护。
        """
        (tmp_path / "c.md").write_text(
            "---\nname: c\ndescription: d\n---\n\n提示词", encoding="utf-8"
        )
        reg = load_specs(tmp_path)
        assert reg.specs["c"].tools == DEFAULT_TOOLS
        assert "run_shell" not in reg.specs["c"].tools

    def test_max_turns_clamped(self, tmp_path: Path) -> None:
        (tmp_path / "d.md").write_text(
            "---\nname: d\ndescription: d\nmax_turns: 9999\n---\n\n提示词",
            encoding="utf-8",
        )
        reg = load_specs(tmp_path)
        assert reg.specs["d"].max_turns <= 40

    def test_bad_yaml_does_not_break_others(self, tmp_path: Path) -> None:
        (tmp_path / "ok.md").write_text(
            "---\nname: ok\ndescription: 正常\n---\n\n提示词", encoding="utf-8"
        )
        (tmp_path / "broken.md").write_text(
            "---\nname: [unclosed\n---\n\n提示词", encoding="utf-8"
        )
        reg = load_specs(tmp_path)
        assert "ok" in reg.specs


class TestToolWhitelist:
    def test_subagent_never_in_allowed(self) -> None:
        """
        递归防护第一道：子代理的工具集里永远没有 subagent。

        模型看不到的工具不会去调，比调了被拒更省一轮。
        """
        spec = AgentSpec(
            name="x",
            description="d",
            prompt="p",
            tools=("read_file", "subagent", "grep"),
        )
        allowed = spec.allowed_tools(["read_file", "subagent", "grep"])
        assert "subagent" not in allowed
        assert set(allowed) == {"read_file", "grep"}

    def test_whitelist_is_intersection_with_available(self) -> None:
        """
        声明了但未注册的工具要被过滤掉，否则模型会调一个不存在的东西。
        """
        spec = AgentSpec(
            name="x", description="d", prompt="p", tools=("read_file", "web_search")
        )
        allowed = spec.allowed_tools(["read_file", "grep"])
        assert allowed == ["read_file"]

    def test_never_list_is_not_empty(self) -> None:
        assert "subagent" in NEVER_FOR_SUBAGENT


class TestTruncation:
    def test_short_output_unchanged(self) -> None:
        text, was = truncate_for_model("短结论")
        assert text == "短结论"
        assert was is False

    def test_long_output_truncated(self) -> None:
        """
        返回值必须截断。

        委派存在的唯一理由是省上下文。结果原样回灌等于把污染从"过程"
        搬到了"结果"，收益归零。

        outputs 是 Annotated[str, operator.add] —— 子代理跑一小时的
        全部输出累加后一次性进父上下文。
        """
        big = "x" * (OUTPUT_CAP_BYTES + 5000)
        text, was = truncate_for_model(big)
        assert was is True
        assert len(text.encode("utf-8")) < len(big.encode("utf-8"))

    def test_truncation_note_tells_where_full_is(self) -> None:
        """
        截断后要告诉模型完整内容在哪，否则它以为信息丢了，
        可能重新派一次子代理去拿 —— 那比不截断更贵。
        """
        text, _ = truncate_for_model("y" * (OUTPUT_CAP_BYTES + 100))
        assert "工具详情" in text
        assert "不需要重新委派" in text

    def test_truncates_by_bytes_not_chars(self) -> None:
        """
        按 UTF-8 字节而不是字符。中文一个字符 3 字节，按字符算的话
        中文内容实际能塞进三倍的量。
        """
        # 每个中文字 3 字节，构造刚好超过上限的内容
        cn = "中" * (OUTPUT_CAP_BYTES // 3 + 100)
        text, was = truncate_for_model(cn)
        assert was is True
        # 截断后的正文部分不能超过上限
        body = text.split("\n\n[输出已截断")[0]
        assert len(body.encode("utf-8")) <= OUTPUT_CAP_BYTES

    def test_no_broken_utf8(self) -> None:
        """截断不能切出半个字符 —— errors='ignore' 保证这点。"""
        cn = "中" * (OUTPUT_CAP_BYTES // 3 + 50)
        text, _ = truncate_for_model(cn)
        text.encode("utf-8").decode("utf-8")  # 不抛就是没坏


class TestDepthGuard:
    async def test_depth_limit_rejects(self, tmp_path: Path) -> None:
        """
        递归防护第二道：深度到上限时拒绝。

        白名单是配置，配置会被写错 —— 所以需要一个代码级兜底。
        """
        reg = ToolRegistry()
        reg.register(SubAgentTool())

        # 手动把深度顶到上限
        from app.modules.agent.tools import subagent as sa

        token = sa._depth.set(MAX_DEPTH)
        try:
            r = await SubAgentTool().run(
                mk_ctx(tmp_path, registry=reg), agent="researcher", task="做点事"
            )
        finally:
            sa._depth.reset(token)

        assert r.is_error is True
        assert "深度上限" in r.content
        # 要告诉模型怎么补救，不只是说"不行"
        assert "拆成" in r.content

    def test_depth_starts_at_zero(self) -> None:
        assert current_depth() == 0

    async def test_concurrent_calls_do_not_inflate_depth(self, tmp_path: Path) -> None:
        """
        并发的兄弟子代理不能被误判成递归。

        这是 ContextVar 相对全局计数器的核心优势 —— 注释：
          「并发调用（同一层级的多个子 Agent）互不干扰；
            链式递归（子 Agent 再调子 Agent）才会递增深度。」

        全局计数器会把 5 个并行子代理误判成深度 5。
        """
        from app.modules.agent.tools import subagent as sa

        seen: list[int] = []

        async def fake_branch() -> None:
            # 模拟一个子代理调用：进入时 +1，记录，退出时恢复
            d = sa._depth.get()
            token = sa._depth.set(d + 1)
            try:
                await asyncio.sleep(0.01)
                seen.append(sa._depth.get())
            finally:
                sa._depth.reset(token)

        # asyncio.gather 里每个协程共享同一个 context？不 ——
        # create_task 会各自拷贝，所以要用 task
        await asyncio.gather(*(asyncio.create_task(fake_branch()) for _ in range(5)))

        # 五个并发分支各自看到的深度都该是 1，不是 1..5
        assert seen == [1, 1, 1, 1, 1], f"并发被误判成递归：{seen}"

    def test_depth_restored_after_exception(self) -> None:
        """
        finally 里恢复而非递减。注释：
        「恢复而非递减，避免异常场景下计数错乱」
        """
        from app.modules.agent.tools import subagent as sa

        before = sa._depth.get()
        token = sa._depth.set(before + 1)
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            pass
        finally:
            sa._depth.reset(token)
        assert sa._depth.get() == before


class TestConcurrencyTimeoutCancel:
    """
    并发上限、超时、取消级联 —— 三件必须一起做。

    这三件很容易只做一到两件：
      并发上限   只有 有实现有
      超时       三个都没有
      取消级联   不级联（子代理跑在全局 worker 的独立 Task 里）
    """

    async def test_concurrency_capped_and_depth_not_inflated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        走真实的 SubAgentTool.run 路径，同时发 8 个委派：
        - 并发峰值不能超过 MAX_CONCURRENCY
        - 每个分支看到的深度都该是 1（并发不是递归）
        """
        from app.modules.agent import subagent_runner
        from app.modules.agent.tools import subagent as sa

        state = {"cur": 0, "peak": 0}
        depths: list[int] = []

        async def fake_run(ctx: Any, spec: Any, task: str) -> ToolResult:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            depths.append(sa.current_depth())
            await asyncio.sleep(0.05)
            state["cur"] -= 1
            return ToolResult(content=f"done {task}")

        monkeypatch.setattr(subagent_runner, "run_subagent", fake_run)

        reg = ToolRegistry()
        reg.register(SubAgentTool())
        ctx = mk_ctx(tmp_path, registry=reg)
        tool = SubAgentTool()

        results = await asyncio.gather(
            *(
                asyncio.create_task(tool.run(ctx, agent="researcher", task=f"t{i}"))
                for i in range(8)
            )
        )

        assert all(not r.is_error for r in results)
        assert state["peak"] <= sa.MAX_CONCURRENCY, (
            f"并发峰值 {state['peak']} 超过上限 {sa.MAX_CONCURRENCY} —— "
            "LLM 一次发 30 个委派就能把机器打满"
        )
        assert set(depths) == {1}, f"并发被误判成递归：{set(depths)}"

    async def test_timeout_returns_error_not_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        超时要转成给模型的错误字符串，不向上抛。

        父代理应该能决定"换个更小的任务重试"还是"自己做" ——
        抛异常的话整个 run 就挂了。

        常见实现没有超时。后果：永久占 worker slot、
        永久阻塞父代理（裸 await pending_future）、
        同类实现 永久占并发槽位。
        """
        from app.modules.agent import subagent_runner
        from app.modules.agent.tools import subagent as sa

        async def hangs(ctx: Any, spec: Any, task: str) -> ToolResult:
            await asyncio.sleep(60)
            return ToolResult(content="never")

        monkeypatch.setattr(subagent_runner, "run_subagent", hangs)
        monkeypatch.setattr(sa, "TIMEOUT_S", 0.05)

        r = await SubAgentTool().run(
            mk_ctx(tmp_path, registry=ToolRegistry()), agent="researcher", task="卡住"
        )
        assert r.is_error is True
        assert "超时" in r.content
        # 要给补救方向
        assert "拆小" in r.content

    async def test_parent_cancel_cascades_to_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        父代理被取消时子代理必须一起停。

        做不到 —— 子代理由全局 worker 的独立 asyncio.Task 承载，
        与父代理的 Task 树无父子关系。用户 Ctrl+C 只停父代理，
        子代理继续烧 token。
        """
        from app.modules.agent import subagent_runner

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def long_child(ctx: Any, spec: Any, task: str) -> ToolResult:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return ToolResult(content="never")

        monkeypatch.setattr(subagent_runner, "run_subagent", long_child)

        task = asyncio.create_task(
            SubAgentTool().run(
                mk_ctx(tmp_path, registry=ToolRegistry()),
                agent="researcher",
                task="长任务",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 子代理收到了取消，不是被孤立在后台继续跑
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)
        assert cancelled.is_set(), "父取消没有级联到子代理"

    async def test_depth_restored_after_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """取消后深度计数要恢复，否则后续委派会误判成已达上限。"""
        from app.modules.agent import subagent_runner
        from app.modules.agent.tools import subagent as sa

        started = asyncio.Event()

        async def long_child(ctx: Any, spec: Any, task: str) -> ToolResult:
            started.set()
            await asyncio.sleep(30)
            return ToolResult(content="never")

        monkeypatch.setattr(subagent_runner, "run_subagent", long_child)

        before = sa.current_depth()
        task = asyncio.create_task(
            SubAgentTool().run(
                mk_ctx(tmp_path, registry=ToolRegistry()), agent="researcher", task="t"
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sa.current_depth() == before


class TestToolEventAttribution:
    """
    工具事件必须带 agent_name。

    这是真实验证跑出来的 bug：最初只靠 span depth 判断归属，而 emit 读的是
    【当前】span —— 工具执行时那个 agent span 已经不在栈顶，所以子智能体
    内部的 tool_end 拿到的 depth 是 0。

    后果是子智能体读的 6 个文件全被算成父代理自己读的，界面上完全看不出
    委派省了上下文。而且更糟的是：验证脚本因此误判"子智能体没调用任何工具"，
    同时"递归防护生效""只读约束生效"这两条**因为同样的原因侥幸通过** ——
    错误的观测既能造出假阴性也能造出假阳性。
    """

    async def test_tool_events_carry_agent_name(self, tmp_path: Path) -> None:
        # 直接检查 emit 调用点：loop 里三处 tool 事件都要带 agent_name
        import inspect

        from app.core.events import Ev, EventBus, reset_bus, set_bus
        from app.modules.agent.loop import AgentLoop

        src = inspect.getsource(AgentLoop._act)
        assert src.count("agent_name=self.agent_name") >= 2, (
            "tool_start / tool_end 没有都带 agent_name"
        )
        src_fill = inspect.getsource(AgentLoop._fill_missing_tool_results)
        assert "agent_name=self.agent_name" in src_fill, (
            "补占位的 tool_end 没带 agent_name"
        )

        # 顺带确认 EventBus 会把 agent_name 原样透传
        bus = EventBus()
        token = set_bus(bus)
        try:
            await bus.push(Ev.TOOL_START, {"agent_name": "researcher", "call_id": "c1"})
        finally:
            reset_bus(token)
        got = bus._queue.get_nowait()
        assert got["data"]["agent_name"] == "researcher"


class TestUnknownAgent:
    async def test_lists_available(self, tmp_path: Path) -> None:
        """不存在时列出可用的 —— 模型据此自我纠正，不用再猜。"""
        r = await SubAgentTool().run(
            mk_ctx(tmp_path), agent="nonexistent", task="做事"
        )
        assert r.is_error is True
        assert "researcher" in r.content

    async def test_empty_args(self, tmp_path: Path) -> None:
        r = await SubAgentTool().run(mk_ctx(tmp_path), agent="", task="")
        assert r.is_error is True


class TestToolDescription:
    def test_says_task_must_be_self_contained(self) -> None:
        """
        task 自包含是委派能省上下文的前提，必须在描述里说清。

        措辞可直接参考：
          "The instruction must be self-contained because the sub-agent does
           not have access to your conversation history"
        """
        d = SubAgentTool.description
        assert "自包含" in d
        assert "看不到" in d

    def test_says_when_not_to_use(self) -> None:
        """
        "什么时候不该用"必须写。有实现完全没有这类指引，而它委派成本很低，
        模型容易过度委派。
        """
        d = SubAgentTool.description
        assert "不要用" in d or "不该用" in d
        assert "简单" in d
        assert "自包含" in d

    def test_not_gated_by_approval(self) -> None:
        """
        委派本身不需要审批 —— 子代理内部的危险操作会各自触发审批。
        在这里再拦一层等于同一件事问两次。
        """
        assert SubAgentTool.requires_approval is False

    def test_parameters_enum_lists_agents(self) -> None:
        params = SubAgentTool().parameters()
        enum = params["properties"]["agent"]["enum"]
        assert "researcher" in enum
        assert "reviewer" in enum


class TestPromptBuilding:
    def test_subagent_prompt_is_not_main_prompt(self, tmp_path: Path) -> None:
        """
        子代理提示词独立，不复用主智能体的。

        子代理与主 Agent 用完全相同的 system prompt，
        导致它没有独立人格。
        """
        from app.modules.agent.subagent_runner import build_subagent_prompt

        spec = load_specs(Path("nope")).specs["researcher"]
        text = build_subagent_prompt(
            spec, workspace=str(tmp_path), tool_names=["read_file"]
        )
        assert "调研型子智能体" in text
        # 环境部分要有（工作区、工具清单是客观事实，两边应一致）
        assert str(tmp_path) in text or "read_file" in text

    def test_subagent_prompt_has_output_constraints(self, tmp_path: Path) -> None:
        """
        必须约束输出形态，否则子代理会带寒暄、反问、"还需要我做什么吗"，
        父代理拿到得再花一轮解析。
        """
        from app.modules.agent.subagent_runner import build_subagent_prompt

        spec = load_specs(Path("nope")).specs["researcher"]
        text = build_subagent_prompt(spec, workspace=str(tmp_path), tool_names=[])
        assert "不要在结尾提问" in text

    def test_no_skills_catalog_for_subagent(self, tmp_path: Path) -> None:
        """
        技能清单不给子代理 —— 它的任务是单一的，常驻清单对它是纯浪费。
        """
        from app.modules.agent.subagent_runner import build_subagent_prompt

        spec = load_specs(Path("nope")).specs["reviewer"]
        text = build_subagent_prompt(spec, workspace=str(tmp_path), tool_names=[])
        assert "可用技能" not in text


class TestSeedMessageType:
    """
    喂给子代理的首条消息必须是 Msg 实例，不能是 dict。

    这是真实验证抓到的 bug：塞 dict 进去后 find_missing_tool_calls 里
    `msgs[idx].role` 抛 AttributeError: 'dict' object has no attribute 'role'，
    **子代理第一轮就崩**。

    最坑的是表现：父代理拿到错误后自己编了一段结论交差，看起来像委派成功
    了，token 也真的省了（因为子代理啥也没干）。所有指标都"正常"，只有
    "子层工具调用 = 0" 这一条露了馅。
    """

    def test_seed_is_msg_instance(self) -> None:
        import inspect

        from app.modules.agent import subagent_runner

        src = inspect.getsource(subagent_runner.run_subagent)
        assert "Msg(role=\"user\"" in src, "首条消息不是 Msg 实例"
        assert 'loop.messages = [{"role"' not in src, "又塞回 dict 了"

    def test_find_missing_tool_calls_rejects_dict(self) -> None:
        """
        直接验证那个崩溃点：dict 进去会炸。

        锁住这个行为，好让"为什么必须用 Msg"这件事有据可查。
        """
        from app.modules.agent.messages import Msg, find_missing_tool_calls

        # Msg 正常
        assert find_missing_tool_calls([Msg(role="user", content="x")]) == []

        # dict 会炸 —— 这就是当初子代理崩掉的原因
        with pytest.raises(AttributeError):
            find_missing_tool_calls([{"role": "user", "content": "x"}])  # type: ignore[list-item]


class TestNoToolsCase:
    async def test_spec_with_no_available_tools_errors_early(
        self, tmp_path: Path
    ) -> None:
        """
        一个工具都没有的子代理只能空想。直接报错比让它跑一轮说"我做不到"好。
        """
        from app.modules.agent.subagent_runner import run_subagent

        spec = AgentSpec(
            name="ghost", description="d", prompt="p", tools=("web_search",)
        )
        empty = ToolRegistry()
        r = await run_subagent(mk_ctx(tmp_path, registry=empty), spec, "做事")
        assert r.is_error is True
        assert "没有可用工具" in r.content
