"""
压缩的切点计算测试。

切点选错的后果是上游 400（tool_calls 与 tool 被拆开），而这只在长会话里
出现 —— 开发时撞不到，上线后必然撞到。所以穷举边界。

plan_compaction 是纯函数，这些测试不需要数据库也不需要 LLM。
"""

import pytest
from app.core.config import settings
from app.modules.agent.compaction import (
    build_summary_input,
    plan_compaction,
)
from app.modules.agent.messages import Msg, ToolCall, find_missing_tool_calls


def sys_msg(text: str = "系统指令") -> Msg:
    return Msg(role="system", content=text)


def user(text: str) -> Msg:
    return Msg(role="user", content=text)


def assistant(text: str = "") -> Msg:
    return Msg(role="assistant", content=text)


def calls(*specs: tuple[str, str]) -> Msg:
    """带 tool_calls 的 assistant。specs 是 (call_id, tool_name) 列表。"""
    return Msg(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(id=cid, name=name, arguments="{}") for cid, name in specs
        ],
    )


def result(call_id: str, name: str, text: str = "ok", is_error: bool = False) -> Msg:
    return Msg(
        role="tool",
        content=text,
        tool_call_id=call_id,
        tool_name=name,
        is_error=is_error,
    )


def summary(text: str = "更早的摘要") -> Msg:
    return Msg(role="summary", content=text)


def turn(n: int) -> list[Msg]:
    """一个普通的问答轮次。"""
    return [user(f"问题{n}"), assistant(f"回答{n}")]


def tool_turn(n: int) -> list[Msg]:
    """一个带工具调用的轮次：user → assistant(calls) → tool → assistant。"""
    return [
        user(f"任务{n}"),
        calls((f"c{n}", "read_file")),
        result(f"c{n}", "read_file"),
        assistant(f"完成{n}"),
    ]


class TestNoPlanCases:
    def test_empty(self) -> None:
        assert plan_compaction([]) is None

    def test_only_system(self) -> None:
        assert plan_compaction([sys_msg()]) is None

    def test_short_conversation_all_in_tail(self) -> None:
        """轮次不够 keep_tail_turns 时全部保留，不压。"""
        msgs = [sys_msg(), *turn(1), *turn(2)]
        assert plan_compaction(msgs, keep_tail_turns=4) is None

    def test_only_summaries_not_recompacted(self) -> None:
        """
        候选集全是已有摘要时不压。反复压缩摘要会让信息越来越糊，
        而且每次都花一次 LLM 调用。

        构造：摘要在前，后面的真实轮次全部落进 tail，
        于是候选集只剩那两条 summary。
        """
        msgs = [sys_msg(), summary("A"), summary("B"), *turn(1), *turn(2)]
        assert plan_compaction(msgs, keep_tail_turns=2) is None

    def test_summary_excluded_but_real_messages_still_compacted(self) -> None:
        """
        混合情况：已有摘要不进候选集，但它后面的真实消息照常压。
        """
        msgs = [sys_msg(), summary("旧摘要")]
        for i in range(8):
            msgs.extend(turn(i))
        plan = plan_compaction(msgs, keep_tail_turns=2)
        assert plan is not None
        assert all(m.role != "summary" for m in plan.victims)
        assert any(m.content == "问题0" for m in plan.victims)


class TestToolBoundary:
    """切点绝不能落在 tool 组内部。"""

    def test_cut_never_splits_a_tool_group(self) -> None:
        # 6 个带工具的轮次，keep 1 轮
        msgs = [sys_msg()]
        for i in range(6):
            msgs.extend(tool_turn(i))

        plan = plan_compaction(msgs, keep_tail_turns=1)
        assert plan is not None

        kept = msgs[: plan.head_end] + msgs[plan.cut :]
        # 保留段里不能有孤立的 tool 消息
        assert find_missing_tool_calls(kept) == []
        # 保留段的第一条非 system 消息不能是 tool
        body = [m for m in kept if m.role != "system"]
        assert body[0].role != "tool", "保留段以孤立的 tool 消息开头，上游会 400"

    def test_cut_at_tool_message_moves_back_past_declaring_assistant(self) -> None:
        """
        切点正好落在 tool 消息上时，要一路退到声明它的 assistant 之前。
        """
        msgs = [
            sys_msg(),
            user("任务"),
            calls(("c1", "t"), ("c2", "t")),
            result("c1", "t"),
            result("c2", "t"),  # 假设切点落这里
            assistant("完成"),
            user("下一问"),
            assistant("答"),
        ]
        plan = plan_compaction(msgs, keep_tail_turns=1)
        if plan is None:
            pytest.skip("这个长度下不触发压缩")
        kept = msgs[: plan.head_end] + msgs[plan.cut :]
        assert find_missing_tool_calls(kept) == []
        for m in kept:
            if m.role == "tool":
                # 每个 tool 消息前面必须有声明它的 assistant
                idx = kept.index(m)
                declared = any(
                    tc.id == m.tool_call_id
                    for prev in kept[:idx]
                    for tc in (prev.tool_calls or [])
                )
                assert declared, f"tool {m.tool_call_id} 没有对应的 assistant"

    def test_parallel_tool_calls_kept_together(self) -> None:
        """一个 assistant 声明 3 个并行调用时，三个结果要一起保留或一起压掉。"""
        msgs = [sys_msg()]
        for i in range(5):
            msgs.append(user(f"任务{i}"))
            msgs.append(calls((f"a{i}", "t"), (f"b{i}", "t"), (f"c{i}", "t")))
            msgs.append(result(f"a{i}", "t"))
            msgs.append(result(f"b{i}", "t"))
            msgs.append(result(f"c{i}", "t"))
            msgs.append(assistant(f"完成{i}"))

        plan = plan_compaction(msgs, keep_tail_turns=2)
        assert plan is not None
        kept = msgs[: plan.head_end] + msgs[plan.cut :]
        assert find_missing_tool_calls(kept) == []

    @pytest.mark.parametrize("keep", [0, 1, 2, 3, 5])
    def test_no_orphan_at_any_keep_value(self, keep: int) -> None:
        """穷举 keep 值，任何一个都不能产生孤立 tool_call。"""
        msgs = [sys_msg()]
        for i in range(8):
            msgs.extend(tool_turn(i) if i % 2 == 0 else turn(i))

        plan = plan_compaction(msgs, keep_tail_turns=keep)
        if plan is None:
            return
        kept = msgs[: plan.head_end] + msgs[plan.cut :]
        assert find_missing_tool_calls(kept) == [], f"keep={keep} 时产生了孤立 tool_call"


class TestTailPreserved:
    def test_keeps_requested_number_of_turns(self) -> None:
        msgs = [sys_msg()]
        for i in range(10):
            msgs.extend(turn(i))

        plan = plan_compaction(msgs, keep_tail_turns=3)
        assert plan is not None
        kept = msgs[plan.tail_start :]
        user_count = sum(1 for m in kept if m.role == "user")
        assert user_count == 3, f"应保留 3 轮，实际 {user_count} 轮"

    def test_tail_contains_most_recent_messages(self) -> None:
        msgs = [sys_msg()]
        for i in range(10):
            msgs.extend(turn(i))

        plan = plan_compaction(msgs, keep_tail_turns=2)
        assert plan is not None
        kept = msgs[plan.cut :]
        # 最后一轮必须在
        assert any(m.content == "问题9" for m in kept)
        assert any(m.content == "回答9" for m in kept)
        # 最早的轮次必须被压掉
        assert not any(m.content == "问题0" for m in kept)


class TestPromptPlaceholder:
    """
    压缩提示词的占位符名字必须和模板里的一致。

    传错名字【不会报错】—— render 只做字符串替换，找不到就原样留着。
    实测踩过：传 conversation= 而模板里是 {{history}}，模型收到字面的
    "{{history}}"，回复"无对话历史内容，请提供需要压缩的对话历史"，
    这条回复被当成摘要存了下来。压缩事件正常、token 正常下降、日志正常，
    只有摘要是垃圾，会话从此越来越糊涂。
    """

    def test_compact_template_placeholder_is_history(self) -> None:
        from app.modules.agent import prompts

        tpl = prompts.load_builtin("compact")
        assert "{{history}}" in tpl, "模板占位符变了，compaction.py 要同步改"

    def test_render_fills_all_placeholders(self) -> None:
        """
        【所有】占位符都要传。

        模板后来加了长度预算（budget_tokens 等四个），只传 history 的话
        剩下四个会以字面 "{{budget_tokens}}" 留在提示词里 —— 模型收到
        "压到 {{budget_tokens}} token 以内"这种指令，行为不可预测。

        compaction.py 里有 "{{" 检测兜底，但那是最后一道 ——
        这条测试要在改模板时就发现。
        """
        from app.modules.agent import prompts

        tpl = prompts.load_builtin("compact")
        out = prompts.render(
            tpl,
            history="一些对话内容",
            budget_tokens="2000",
            budget_chars="1400",
            window_tokens="10000",
            budget_percent="20",
        )
        assert "一些对话内容" in out
        assert "{{" not in out, "还有没被替换的占位符"

    def test_all_template_vars_are_passed_by_caller(self) -> None:
        """
        模板里的占位符集合必须和 compaction.py 传的一致。

        任何一边加了变量而另一边没跟上，结果都是提示词里留着字面的
        "{{xxx}}"—— 而那【不会报错】。
        """
        import re
        from pathlib import Path

        from app.modules.agent import prompts

        tpl = prompts.load_builtin("compact")
        in_template = set(re.findall(r"\{\{(\w+)\}\}", tpl))

        src = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "modules"
            / "agent"
            / "compaction.py"
        ).read_text(encoding="utf-8")
        # 取 prompts.render(...) 那一段里的关键字参数
        m = re.search(r'prompts\.load_builtin\("compact"\),([\s\S]*?)\n    \)', src)
        assert m, "找不到 render 调用"
        passed = set(re.findall(r"(\w+)=", m.group(1)))

        missing = in_template - passed
        assert not missing, f"模板里有但没传：{missing}"

    def test_wrong_name_leaves_placeholder(self) -> None:
        """
        锁住这个行为本身：名字错了占位符会留在原文里。
        compaction 里靠检测 "{{" 来兜住这种情况。
        """
        from app.modules.agent import prompts

        tpl = prompts.load_builtin("compact")
        out = prompts.render(tpl, conversation="内容")
        assert "{{history}}" in out


class TestSummaryInput:
    def test_includes_roles_and_tool_names(self) -> None:
        """
        摘要模型需要知道谁说的、调了什么工具。
        丢掉工具名会让摘要漏掉"已经建过某个文件"这类事实。
        """
        victims = (
            user("帮我建个文件"),
            calls(("c1", "write_file")),
            result("c1", "write_file", "已写入 12 行"),
            assistant("建好了"),
        )
        text = build_summary_input(victims)
        assert "[用户] 帮我建个文件" in text
        assert "write_file" in text
        assert "已写入 12 行" in text
        assert "[助手] 建好了" in text

    def test_marks_failed_tools(self) -> None:
        """失败的尝试是最容易在摘要里丢掉的，必须标出来。"""
        victims = (result("c1", "run_shell", "命令不存在", is_error=True),)
        text = build_summary_input(victims)
        assert "失败" in text
        assert "命令不存在" in text

    def test_truncates_huge_tool_output(self) -> None:
        """
        工具输出可能几万字符。全塞进去会让摘要请求本身超限 ——
        压缩动作自己撞墙，这是最尴尬的失败方式。
        """
        victims = (result("c1", "grep", "x" * 50_000),)
        text = build_summary_input(victims)
        assert len(text) < settings.agent.compact_tool_excerpt + 200

    def test_reasoning_not_included(self) -> None:
        """思维链是过程不是结论，占 token 且对重建上下文无用。"""
        victims = (
            Msg(role="assistant", content="结论", reasoning="很长的思考过程" * 50),
        )
        text = build_summary_input(victims)
        assert "很长的思考过程" not in text
        assert "结论" in text


class TestWorthDoing:
    def test_too_few_victims_not_worth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.agent, "compact_min_victims", 10)
        msgs = [sys_msg()]
        for i in range(6):
            msgs.extend(turn(i))
        plan = plan_compaction(msgs, keep_tail_turns=4)
        assert plan is not None
        assert plan.is_worth_doing() is False

    def test_enough_victims_worth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.agent, "compact_min_victims", 4)
        msgs = [sys_msg()]
        for i in range(10):
            msgs.extend(turn(i))
        plan = plan_compaction(msgs, keep_tail_turns=2)
        assert plan is not None
        assert plan.is_worth_doing() is True

    def test_urgent_ignores_min_victims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        已经超窗口时不能再挑三拣四。

        实测撞到过：候选集 3 条、min_victims=4，于是拒绝压缩，
        上下文一路涨到窗口的 221% 才被上游 400 打断。
        此时"压两条也比 400 好"。
        """
        monkeypatch.setattr(settings.agent, "compact_min_victims", 10)
        msgs = [sys_msg()]
        for i in range(6):
            msgs.extend(turn(i))
        plan = plan_compaction(msgs, keep_tail_turns=4)
        assert plan is not None
        assert plan.is_worth_doing() is False
        assert plan.is_worth_doing(urgent=True) is True


class TestTailTokenBudget:
    """
    tail 必须同时受轮次和 token 两个限制。

    只按轮次会出事：keep=4 但每轮是 3000 token 的长文时，
    tail 自己就 12000 token，而窗口可能只有 4200。
    """

    def test_budget_shrinks_tail_when_turns_are_huge(self) -> None:
        msgs = [sys_msg()]
        # 每轮一条 4000 字符的长回答
        for i in range(6):
            msgs.append(user(f"问题{i}"))
            msgs.append(assistant("很长的回答内容" * 400))

        loose = plan_compaction(msgs, keep_tail_turns=4)
        tight = plan_compaction(msgs, keep_tail_turns=4, tail_token_budget=1000)
        assert loose is not None and tight is not None
        # token 预算收紧后，保留段更短、候选集更多
        assert tight.tail_start > loose.tail_start
        assert len(tight.victims) > len(loose.victims)

    def test_budget_does_not_expand_tail(self) -> None:
        """预算很大时不能反过来把 tail 变长 —— 轮次限制仍然有效。"""
        msgs = [sys_msg()]
        for i in range(10):
            msgs.extend(turn(i))
        a = plan_compaction(msgs, keep_tail_turns=2)
        b = plan_compaction(msgs, keep_tail_turns=2, tail_token_budget=10_000_000)
        assert a is not None and b is not None
        assert a.tail_start == b.tail_start

    def test_tail_within_budget(self) -> None:
        """收紧后 tail 的实际 token 数要落在预算内。"""
        from app.modules.agent.tokens import estimate_tokens

        msgs = [sys_msg()]
        for i in range(8):
            msgs.append(user(f"问{i}"))
            msgs.append(assistant("回答" * 500))

        budget = 2000
        plan = plan_compaction(msgs, keep_tail_turns=6, tail_token_budget=budget)
        assert plan is not None
        tail_tokens = estimate_tokens([m.to_api() for m in msgs[plan.tail_start :]])
        assert tail_tokens <= budget, f"tail {tail_tokens} 超出预算 {budget}"

    def test_last_user_message_never_compacted(self) -> None:
        """
        最后一个 user 消息是硬下限，即使它自己就超预算。

        压掉它之后模型收到的是「system + 摘要 + 历史」，没有任何当前诉求 ——
        模型只能猜或者反问"你想做什么"。这比上下文超限更糟：
        超限有报错，这个没有。
        """
        msgs = [sys_msg()]
        for i in range(4):
            msgs.extend(turn(i))
        # 用户粘了一大段代码进来，这一条自己就远超预算
        msgs.append(user("帮我看这段代码：" + "x = 1\n" * 3000))

        plan = plan_compaction(msgs, keep_tail_turns=1, tail_token_budget=100)
        assert plan is not None
        kept = msgs[plan.tail_start :]
        assert any(
            m.role == "user" and "帮我看这段代码" in m.content for m in kept
        ), "当前用户问题被压掉了"
        # 但更早的历史要被压
        assert len(plan.victims) > 0

    def test_huge_assistant_reply_compacted_but_question_kept(self) -> None:
        """超大的 assistant 回答可以压掉，但它前面的 user 问题要留。"""
        msgs = [sys_msg()]
        for i in range(4):
            msgs.extend(turn(i))
        msgs.append(user("最后一问"))
        msgs.append(assistant("超大回答" * 3000))

        plan = plan_compaction(msgs, keep_tail_turns=1, tail_token_budget=200)
        assert plan is not None
        kept = msgs[plan.tail_start :]
        assert any(m.content == "最后一问" for m in kept)
