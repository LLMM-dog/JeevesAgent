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
    mark_stale_file_reads,
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


class TestMarkStaleFileReads:
    """读取后又被修改的 read_file 结果应折叠成过时占位。"""

    def test_read_then_edit_marks_stale(self) -> None:
        msgs = [
            Msg(role="user", content="改 a.py"),
            Msg(role="assistant", tool_calls=[_tc("c1", "read_file")]),
            Msg(role="tool", content="<a.py 旧内容>", tool_call_id="c1", tool_name="read_file"),
            Msg(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c2", name="edit_file", arguments='{"path":"a.py"}')
                ],
            ),
            Msg(role="tool", content="已修改 a.py", tool_call_id="c2", tool_name="edit_file"),
        ]
        assert mark_stale_file_reads(msgs) == 1
        assert "已过时的文件快照" in msgs[2].content

    def test_read_without_edit_untouched(self) -> None:
        msgs = [
            Msg(role="user", content="读 a.py"),
            Msg(role="assistant", tool_calls=[_tc("c1", "read_file")]),
            Msg(role="tool", content="<a.py 内容>", tool_call_id="c1", tool_name="read_file"),
            Msg(role="assistant", content="看完了"),
        ]
        assert mark_stale_file_reads(msgs) == 0
        assert msgs[2].content == "<a.py 内容>"

    def test_edit_then_read_not_stale(self) -> None:
        """read 发生在 edit 之后，读到的是最新内容，不该被标记。"""
        msgs = [
            Msg(role="assistant", tool_calls=[ToolCall(id="c1", name="edit_file", arguments='{"path":"a.py"}')]),
            Msg(role="tool", content="已修改 a.py", tool_call_id="c1", tool_name="edit_file"),
            Msg(role="assistant", tool_calls=[_tc("c2", "read_file")]),
            Msg(role="tool", content="<a.py 新内容>", tool_call_id="c2", tool_name="read_file"),
        ]
        assert mark_stale_file_reads(msgs) == 0
        assert msgs[3].content == "<a.py 新内容>"

    def test_write_also_marks_stale(self) -> None:
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("c1", "read_file")]),
            Msg(role="tool", content="<旧>", tool_call_id="c1", tool_name="read_file"),
            Msg(role="assistant", tool_calls=[ToolCall(id="c2", name="write_file", arguments='{"path":"a.py"}')]),
            Msg(role="tool", content="已写入", tool_call_id="c2", tool_name="write_file"),
        ]
        assert mark_stale_file_reads(msgs) == 1

    def test_different_file_untouched(self) -> None:
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("c1", "read_file")]),
            Msg(role="tool", content="<a.py>", tool_call_id="c1", tool_name="read_file"),
            Msg(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c2", name="edit_file", arguments='{"path":"b.py"}')
                ],
            ),
            Msg(role="tool", content="已修改 b.py", tool_call_id="c2", tool_name="edit_file"),
        ]
        assert mark_stale_file_reads(msgs) == 0
        assert msgs[1].content == "<a.py>"

    def test_multiple_edits_mark_all_prior_reads(self) -> None:
        """文件被改两次，两次修改之前的所有 read 都应 stale。"""
        msgs = [
            Msg(role="assistant", tool_calls=[_tc("r1", "read_file")]),
            Msg(role="tool", content="<v1>", tool_call_id="r1", tool_name="read_file"),
            Msg(role="assistant", tool_calls=[ToolCall(id="e1", name="edit_file", arguments='{"path":"a.py"}')]),
            Msg(role="tool", content="改1", tool_call_id="e1", tool_name="edit_file"),
            Msg(role="assistant", tool_calls=[_tc("r2", "read_file")]),
            Msg(role="tool", content="<v2>", tool_call_id="r2", tool_name="read_file"),
            Msg(role="assistant", tool_calls=[ToolCall(id="e2", name="edit_file", arguments='{"path":"a.py"}')]),
            Msg(role="tool", content="改2", tool_call_id="e2", tool_name="edit_file"),
        ]
        assert mark_stale_file_reads(msgs) == 2
        assert "已过时" in msgs[1].content  # v1 在 e1 之前，stale
        assert "已过时" in msgs[5].content  # v2 在 e1 之后但 e2 之前，stale

    def test_empty(self) -> None:
        assert mark_stale_file_reads([]) == 0

    def test_same_round_read_and_edit_not_stale(self) -> None:
        """同一轮里先 read 再 edit，read 结果对这次 edit 仍有效。"""
        msgs = [
            Msg(
                role="assistant",
                tool_calls=[
                    _tc("c1", "read_file"),
                    ToolCall(id="c2", name="edit_file", arguments='{"path":"a.py"}'),
                ],
            ),
            Msg(role="tool", content="<a.py>", tool_call_id="c1", tool_name="read_file"),
            Msg(role="tool", content="已修改", tool_call_id="c2", tool_name="edit_file"),
        ]
        assert mark_stale_file_reads(msgs) == 0

    def test_path_normalization_matches(self) -> None:
        """read 和 edit 的路径写法不一致（./a.py vs a.py）也要能折叠。"""
        msgs = [
            Msg(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments='{"path":"./a.py"}')
                ],
            ),
            Msg(role="tool", content="<旧>", tool_call_id="c1", tool_name="read_file"),
            Msg(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c2", name="edit_file", arguments='{"path":"a.py"}')
                ],
            ),
            Msg(role="tool", content="已改", tool_call_id="c2", tool_name="edit_file"),
        ]
        assert mark_stale_file_reads(msgs) == 1
        assert "已过时" in msgs[1].content

    def test_path_normalization_backslash_and_trailing_slash(self) -> None:
        """反斜杠、末尾斜杠、../ 都要规范化成同一路径。"""
        msgs = [
            Msg(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments='{"path":"src\\\\a.py/"}')
                ],
            ),
            Msg(role="tool", content="<旧>", tool_call_id="c1", tool_name="read_file"),
            Msg(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c2", name="edit_file", arguments='{"path":"./src/a.py"}')
                ],
            ),
            Msg(role="tool", content="已改", tool_call_id="c2", tool_name="edit_file"),
        ]
        assert mark_stale_file_reads(msgs) == 1
        assert "已过时" in msgs[1].content


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
