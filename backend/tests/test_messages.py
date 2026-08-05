"""
tool 配对修复测试。

这是最该有测试的地方之一：失败模式是"某些会话突然每次打开都 400",
而复现条件依赖具体的消息序列，手动测很难碰到。

每发现一种新的不一致形态就往这里加一个 case，永不删除。
"""

from app.modules.agent.messages import (
    Msg,
    ToolCall,
    find_missing_tool_calls,
    repair_tool_pairing,
)


def _tc(cid: str, name: str = "read_file") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments='{"path":"a.py"}')


def _assert_valid(msgs: list[Msg]) -> None:
    """
    断言消息序列对 LLM 合法：
    - 每个 tool 消息前面能找到声明它的 assistant
    - 每个 assistant 的 tool_calls 都有对应的 tool 消息紧随其后
    """
    declared: set[str] = set()
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.role == "assistant" and m.tool_calls:
            ids = [tc.id for tc in m.tool_calls]
            declared.update(ids)
            following = []
            j = i + 1
            while j < len(msgs) and msgs[j].role == "tool":
                following.append(msgs[j].tool_call_id)
                j += 1
            assert following == ids, f"tool 顺序/数量不匹配: 期望 {ids} 实际 {following}"
            i = j
            continue
        assert m.role != "tool", f"孤立的 tool 消息: {m.tool_call_id}"
        i += 1


class TestRepairToolPairing:
    def test_clean_sequence_untouched(self) -> None:
        msgs = [
            Msg(role="user", content="hi"),
            Msg(role="assistant", tool_calls=[_tc("c1")]),
            Msg(role="tool", content="ok", tool_call_id="c1", tool_name="read_file"),
            Msg(role="assistant", content="done"),
        ]
        out, fixes = repair_tool_pairing(msgs)
        assert fixes == 0
        assert len(out) == 4
        _assert_valid(out)

    def test_missing_tool_result_gets_placeholder(self) -> None:
        """取消发生在工具执行中途：assistant 说要调工具，但没有结果。"""
        msgs = [
            Msg(role="user", content="hi"),
            Msg(role="assistant", tool_calls=[_tc("c1")]),
        ]
        out, fixes = repair_tool_pairing(msgs)
        assert fixes == 1
        assert out[-1].role == "tool"
        assert out[-1].tool_call_id == "c1"
        assert out[-1].is_error is True
        _assert_valid(out)

    def test_partial_parallel_calls(self) -> None:
        """三个并行工具，只有第一和第三个完成了。"""
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("c1"), _tc("c2"), _tc("c3")]),
            Msg(role="tool", content="r1", tool_call_id="c1"),
            Msg(role="tool", content="r3", tool_call_id="c3"),
        ]
        out, fixes = repair_tool_pairing(msgs)
        assert fixes == 1
        tool_ids = [m.tool_call_id for m in out if m.role == "tool"]
        # 顺序必须与 tool_calls 声明顺序一致，不是"已有的在前补的在后"
        assert tool_ids == ["c1", "c2", "c3"]
        assert out[2].is_error is True and out[2].content == "（该工具调用未完成）"
        _assert_valid(out)

    def test_orphan_tool_message_dropped(self) -> None:
        """tool 消息前面没有声明它的 assistant（手改库/脏数据）。"""
        msgs = [
            Msg(role="user", content="hi"),
            Msg(role="tool", content="huh", tool_call_id="ghost"),
            Msg(role="assistant", content="ok"),
        ]
        out, fixes = repair_tool_pairing(msgs)
        assert fixes == 1
        assert all(m.role != "tool" for m in out)
        _assert_valid(out)

    def test_stray_tool_id_dropped_and_placeholder_added(self) -> None:
        """assistant 声明 c1，但跟着的 tool 消息 id 是 c9。"""
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("c1")]),
            Msg(role="tool", content="wrong", tool_call_id="c9"),
        ]
        out, fixes = repair_tool_pairing(msgs)
        assert fixes == 2  # 丢弃 c9 + 补 c1
        tool_msgs = [m for m in out if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "c1"
        _assert_valid(out)

    def test_two_consecutive_tool_groups(self) -> None:
        """连续两轮工具调用，第二轮不完整。"""
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("a1")]),
            Msg(role="tool", content="r", tool_call_id="a1"),
            Msg(role="assistant", tool_calls=[_tc("b1"), _tc("b2")]),
            Msg(role="tool", content="r", tool_call_id="b1"),
        ]
        out, fixes = repair_tool_pairing(msgs)
        assert fixes == 1
        _assert_valid(out)

    def test_empty_list(self) -> None:
        out, fixes = repair_tool_pairing([])
        assert out == [] and fixes == 0

    def test_idempotent(self) -> None:
        """修复结果再修一次不应有任何变化。"""
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("c1"), _tc("c2")]),
            Msg(role="tool", content="r", tool_call_id="c2"),
        ]
        once, f1 = repair_tool_pairing(msgs)
        twice, f2 = repair_tool_pairing(once)
        assert f1 == 1 and f2 == 0
        assert [(m.role, m.tool_call_id) for m in once] == [
            (m.role, m.tool_call_id) for m in twice
        ]


class TestFindMissingToolCalls:
    def test_none_when_all_answered(self) -> None:
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("c1")]),
            Msg(role="tool", content="r", tool_call_id="c1"),
        ]
        assert find_missing_tool_calls(msgs) == []

    def test_finds_unanswered(self) -> None:
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("c1"), _tc("c2")]),
            Msg(role="tool", content="r", tool_call_id="c1"),
        ]
        missing = find_missing_tool_calls(msgs)
        assert [tc.id for tc in missing] == ["c2"]

    def test_no_tool_calls(self) -> None:
        assert find_missing_tool_calls([Msg(role="assistant", content="hi")]) == []

    def test_empty(self) -> None:
        assert find_missing_tool_calls([]) == []


class TestToApi:
    def test_summary_maps_to_user(self) -> None:
        """
        摘要以 user 角色发送。放 system 位等于给注入开升格通道。
        """
        assert Msg(role="summary", content="s").to_api()["role"] == "user"

    def test_artifact_maps_to_assistant(self) -> None:
        assert Msg(role="artifact", content="code").to_api()["role"] == "assistant"

    def test_reasoning_not_sent_upstream(self) -> None:
        api = Msg(role="assistant", content="x", reasoning="think").to_api()
        assert "reasoning" not in api and "reasoning_content" not in api

    def test_tool_call_shape(self) -> None:
        api = Msg(role="assistant", tool_calls=[_tc("c1", "grep")]).to_api()
        assert api["tool_calls"][0]["function"]["name"] == "grep"
        assert api["tool_calls"][0]["type"] == "function"

    def test_bad_json_args_return_empty_dict(self) -> None:
        """模型吐出不完整 JSON 时不能抛异常。"""
        assert ToolCall(id="c", name="t", arguments="{bad").parsed_args() == {}
