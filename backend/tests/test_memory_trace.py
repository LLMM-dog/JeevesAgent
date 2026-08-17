"""
记忆写入的痕迹，以及试验场暴露出的三个真 bug 的回归测试。

## 这些 bug 是怎么被发现的

不是靠断言想出来的，是靠 scripts/memory_playground.py 把记忆写进真实
data/ 目录后【肉眼看文件】发现的：

1. events 完全写不进去（'extract_context' is undefined）
2. tool_notes 的正文被裸字符串整体顶掉，两个小节静默消失
3. content_template 的壳被重复叠加 —— run_shell.md 长出两个
   "# 工具：run_shell" 标题和两组计数行，version 每涨一次多一层

前两个在 diff 里能看到，第三个只有看文件才发现。这是"痕迹要留全文"
和"要在真实路径上跑一遍"两件事的直接价值。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.modules.agent.messages import Msg, ToolCall
from app.modules.memory import layout, registry
from app.modules.memory import service as memory
from app.modules.memory.extract_context import ExtractContext, from_messages
from app.modules.memory.models import MemoryScope, WriteOp
from sqlalchemy.ext.asyncio import AsyncSession

AGENT = "adf_trace"


@pytest.fixture
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    monkeypatch.setattr(type(layout.settings), "memory_dir", property(lambda _self: root))
    registry.reset()
    yield root
    registry.reset()


@pytest_asyncio.fixture
async def ready(memory_dir: Path, db: AsyncSession) -> AsyncIterator[AsyncSession]:
    await memory.init_agent(AGENT, db=db)
    yield db


def _ctx() -> ExtractContext:
    """两条消息 + 真实时间戳。2026-08-13 12:00 UTC。"""
    msgs = [
        Msg(role="user", content="把 verbose 默认值改回 False"),
        Msg(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="edit_file", arguments='{"path":"src/cli.py"}')],
        ),
        Msg(role="tool", content="已替换 1 处", tool_name="edit_file", tool_call_id="c1"),
    ]
    base = 1786_608_000_000
    return from_messages(msgs, [base, base + 1000, base + 2000])


def _patch(search: str, replace: str) -> dict[str, object]:
    return {"blocks": [{"search": search, "replace": replace}]}


# ── 回归 1：events 需要 extract_context ─────────────


@pytest.mark.asyncio
async def test_events_can_be_written(ready: AsyncSession) -> None:
    """
    【回归】events 的路径模板调 extract_context.get_year(ranges)。
    不传上下文时渲染直接失败，而这个类型在重写后一直没被测过。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_t")

    result = await memory.write(
        scope,
        "events",
        {
            "event_name": "verbose_default_fixed",
            "goal": "修正默认值",
            "summary": "verbose 默认值从 True 改回 False。",
            "outcome": "success",
            "ranges": "0-2",
        },
        db=ready,
        extract_context=_ctx(),
    )

    assert result.ok, result.error
    # 日期来自消息时间戳，不是"今天"
    assert "/2026/08/" in result.uri
    assert result.uri.endswith("verbose_default_fixed.md")


@pytest.mark.asyncio
async def test_event_body_embeds_real_conversation(ready: AsyncSession) -> None:
    """
    对话原文由系统按 ranges 取，不让 LLM 提供 —— 它会凭记忆重写，
    而重写过的对话不再是证据。工具调用也要在里面。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_t")
    result = await memory.write(
        scope,
        "events",
        {
            "event_name": "e",
            "goal": "g",
            "summary": "s",
            "outcome": "success",
            "ranges": "0-2",
        },
        db=ready,
        extract_context=_ctx(),
    )

    item = await memory.read_uri(result.uri)
    assert item is not None
    assert "把 verbose 默认值改回 False" in item.body
    assert "edit_file" in item.body, "工具调用是事件的证据链，不能只渲染文本"


@pytest.mark.asyncio
async def test_events_without_context_fails_loudly(ready: AsyncSession) -> None:
    """
    不传 extract_context 时要报错而不是写出一个路径怪异的文件。
    StrictUndefined 保证这一点。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_t")
    with pytest.raises(Exception, match="extract_context"):
        await memory.write(
            scope,
            "events",
            {"event_name": "e", "goal": "g", "summary": "s", "ranges": "0"},
            db=ready,
        )


# ── 回归 2：模板壳不能被重复叠加 ──────────────────


@pytest.mark.asyncio
async def test_content_template_shell_is_not_duplicated(ready: AsyncSession) -> None:
    """
    【回归】tool_notes 的 content_template 会套一层 "# 工具：xxx" + 计数行。

    第一版实现把【渲染结果】当下一次合并的输入，于是那层壳每次写入
    都被重新套一遍 —— 文件里长出两个标题、两组计数行，version 每涨一次多一层。

    修法：raw_content 单独存渲染前的原始值（见 MemoryItem.raw_content）。
    """
    scope = MemoryScope(agent_id=AGENT)
    fields = {"tool_name": "run_shell", "total_calls": 2, "content": "## 适用场景\n- 跑测试"}

    await memory.write(scope, "tool_notes", fields, db=ready)
    for _ in range(3):
        await memory.write(
            scope,
            "tool_notes",
            {
                "tool_name": "run_shell",
                "total_calls": 1,
                "content": _patch("## 适用场景\n- 跑测试", "## 适用场景\n- 跑测试和 lint"),
            },
            db=ready,
        )

    item = await memory.get(scope, "tool_notes", "run_shell")
    assert item is not None
    assert item.body.count("# 工具：run_shell") == 1, "模板壳被重复叠加了"
    assert item.body.count("累计调用") == 1
    # 计数器仍然正常累加：2 + 1×3
    assert item.fields["total_calls"] == 5


@pytest.mark.asyncio
async def test_raw_content_survives_round_trip(ready: AsyncSession) -> None:
    """原始值要能从文件里读回来，否则下一次合并拿不到正确的 current。"""
    scope = MemoryScope(agent_id=AGENT)
    raw = "## 适用场景\n- 跑测试和 lint"

    await memory.write(
        scope, "tool_notes", {"tool_name": "t", "total_calls": 1, "content": raw}, db=ready
    )

    item = await memory.get(scope, "tool_notes", "t")
    assert item is not None
    assert item.raw_content == raw
    assert item.merge_source == raw
    # 正文是渲染后的，与原始值不同
    assert item.body != raw
    assert "# 工具：t" in item.body


@pytest.mark.asyncio
async def test_raw_content_is_stored_as_offset_not_a_copy(
    ready: AsyncSession, memory_dir: Path
) -> None:
    """
    原始值原样出现在正文里时只存偏移，不存副本。

    实测存副本的代价：trajectories 的操作契约 700 字符、文件总长 2200，
    其中 33% 是重复内容 —— 而记忆目录要进 git，每次改动在 diff 里出现两遍。
    """
    scope = MemoryScope(agent_id=AGENT)
    raw = "## 适用场景\n- 跑测试和 lint\n\n## 参数要点\n- 长任务要给 timeout"
    result = await memory.write(
        scope, "tool_notes", {"tool_name": "t", "total_calls": 1, "content": raw}, db=ready
    )

    text = (memory_dir / result.uri).read_text(encoding="utf-8")

    assert "raw_content_span:" in text
    assert "JEEVES_RAW_CONTENT" not in text, "能用偏移时不该再存副本"
    assert text.count("- 长任务要给 timeout") == 1, "正文不该出现两份"

    # 偏移能正确还原
    item = await memory.get(scope, "tool_notes", "t")
    assert item is not None
    assert item.raw_content == raw


@pytest.mark.asyncio
async def test_hand_edited_file_degrades_gracefully(
    ready: AsyncSession, memory_dir: Path
) -> None:
    """
    人手工改记忆文件是文件形态的核心卖点，必须支持。

    改动会让偏移失效。那时回落"整个正文当原始值"—— 代价是模板壳
    可能被叠加一次，比抛异常好：记忆还在，只是格式变丑。
    """
    scope = MemoryScope(agent_id=AGENT)
    result = await memory.write(
        scope, "tool_notes", {"tool_name": "t", "total_calls": 1, "content": "## 原始\n- x"}, db=ready
    )
    path = memory_dir / result.uri

    # 模拟手工编辑：在正文开头插一段，偏移随之失效
    text = path.read_text(encoding="utf-8")
    head, _, body = text.partition("---\n\n")
    path.write_text(f"{head}---\n\n我手工加的一段说明\n\n{body}", encoding="utf-8")

    item = await memory.get(scope, "tool_notes", "t")
    assert item is not None
    assert "我手工加的一段说明" in item.body
    # 不崩、仍能拿到一个可用的 merge_source
    assert item.merge_source


@pytest.mark.asyncio
async def test_no_template_means_body_is_the_merge_source(ready: AsyncSession) -> None:
    """
    没有 content_template 的类型（preferences）不需要 raw_content ——
    正文就是原始值。不该白存一份。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(
        scope, "preferences", {"topic": "t", "content": "- 一条偏好"}, db=ready
    )

    item = await memory.get(scope, "preferences", "t")
    assert item is not None
    assert item.raw_content == ""
    assert item.merge_source == item.body == "- 一条偏好"


# ── 回归 3：重复 patch 是 no-op ────────────────────


@pytest.mark.asyncio
async def test_repeated_patch_does_not_duplicate_section(ready: AsyncSession) -> None:
    """
    【回归】"把 A 扩写成 A+B" 的 patch（search=A, replace=A+B）重复应用时
    会再匹配一次 A 并再插一份 B。实测同一场景连跑两次产生 4 份同样的小节。

    提取流程理论上不该重复输出同一个 patch，但模型重新提取、用户重跑、
    commit 失败重试都会导致它发生。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "t", "content": "## A\n- 一"}, db=ready)

    grow = _patch("## A\n- 一", "## A\n- 一\n\n## B\n- 二")
    first = await memory.write(scope, "preferences", {"topic": "t", "content": grow}, db=ready)
    second = await memory.write(scope, "preferences", {"topic": "t", "content": grow}, db=ready)

    assert first.changed is True
    assert second.changed is False, "重复应用同一个 patch 必须是 no-op"

    item = await memory.get(scope, "preferences", "t")
    assert item is not None
    assert item.body.count("## B") == 1


# ── 痕迹本身 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_write_result_carries_before_and_after(ready: AsyncSession) -> None:
    """
    只有布尔量说不清"记忆变成了什么"。断言 changed=True 完全不能证明
    patch 打对了位置。
    """
    scope = MemoryScope(agent_id=AGENT)
    created = await memory.write(
        scope, "preferences", {"topic": "t", "content": "- 旧偏好"}, db=ready
    )
    assert created.before == ""
    assert created.after == "- 旧偏好"

    updated = await memory.write(
        scope, "preferences", {"topic": "t", "content": _patch("- 旧偏好", "- 新偏好")}, db=ready
    )
    assert updated.before == "- 旧偏好"
    assert updated.after == "- 新偏好"


@pytest.mark.asyncio
async def test_batch_diff_separates_adds_updates_and_unchanged(ready: AsyncSession) -> None:
    """
    diff 的 operations 只记有效改动；no-op 进 summary 的 unchanged。

    混在一起会掩盖问题："提取跑了但什么都没变"和"提取没跑"是两回事。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "old", "content": "- 已有"}, db=ready)

    batch = await memory.write_many(
        [
            # 新建
            WriteOp(scope=scope, memory_type="preferences", fields={"topic": "new", "content": "- 新的"}),
            # 内容相同 → unchanged
            WriteOp(scope=scope, memory_type="preferences", fields={"topic": "old", "content": "- 已有"}),
            # 改写 → update
            WriteOp(
                scope=scope,
                memory_type="preferences",
                fields={"topic": "old", "content": _patch("- 已有", "- 改过")},
            ),
        ],
        db=ready,
        extraction_id="ext_test",
    )

    diff = batch.to_diff()
    assert diff["summary"] == {
        "total_adds": 1,
        "total_updates": 1,
        "total_deletes": 0,
        "total_unchanged": 1,
        "total_errors": 0,
    }
    assert diff["extraction_id"] == "ext_test"
    # update 条目要带 before/after 全文
    upd = diff["operations"]["updates"][0]
    assert upd["before"] == "- 已有"
    assert upd["after"] == "- 改过"
    # add 条目只有 after
    assert "before" not in diff["operations"]["adds"][0]


@pytest.mark.asyncio
async def test_diff_is_written_to_disk(ready: AsyncSession, memory_dir: Path) -> None:
    """
    痕迹落盘而不只打日志：日志会滚动、会被过滤。而"上周它还知道我用 uv"
    这类问题需要按时间回溯记忆的变更史。
    """
    import json

    scope = MemoryScope(agent_id=AGENT)
    batch = await memory.write_many(
        [WriteOp(scope=scope, memory_type="preferences", fields={"topic": "t", "content": "- x"})],
        db=ready,
        extraction_id="ext_ondisk",
    )
    await memory.write_diff(batch, scope=scope)

    path = memory_dir / "agents" / AGENT / ".trace" / "ext_ondisk.json"
    assert path.is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["summary"]["total_adds"] == 1
    assert saved["operations"]["adds"][0]["after"] == "- x"


@pytest.mark.asyncio
async def test_delete_keeps_content_for_audit(ready: AsyncSession) -> None:
    """
    删掉的记忆没有别处可查。不留正文的话"模型删了一条重要经验"
    只剩一行 uri，无法判断该不该恢复，也无法恢复。
    """
    scope = MemoryScope(agent_id=AGENT)
    written = await memory.write(
        scope, "preferences", {"topic": "doomed", "content": "- 即将被删"}, db=ready
    )

    result = await memory.delete_with_trace(written.uri, db=ready)

    assert result.ok
    assert result.memory_type == "preferences"
    assert result.deleted_content == "- 即将被删"
    assert await memory.read_uri(written.uri) is None


@pytest.mark.asyncio
async def test_failed_merge_appears_in_diff_errors(ready: AsyncSession) -> None:
    """失败必须出现在 diff 里 —— 静默失败会让提取看起来成功了。"""
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "t", "content": "- 真实内容"}, db=ready)

    batch = await memory.write_many(
        [
            WriteOp(
                scope=scope,
                memory_type="preferences",
                fields={"topic": "t", "content": _patch("- 不存在", "x")},
            )
        ],
        db=ready,
    )

    assert batch.ok is False
    diff = batch.to_diff()
    assert diff["summary"]["total_errors"] == 1
    assert "找不到" in diff["errors"][0]


# ── ranges 解析 ──────────────────────────────────


def test_ranges_parsing_is_tolerant() -> None:
    """
    ranges 来自 LLM。它会写带空格的、反着写的、超范围的。
    拒绝一条记忆等于永久丢信息，宽容解析只是范围略有偏差。
    """
    ctx = from_messages([Msg(role="user", content=str(i)) for i in range(10)], [1] * 10)

    assert ctx.parse_ranges("0-3") == [0, 1, 2, 3]
    assert ctx.parse_ranges("7") == [7]
    assert ctx.parse_ranges("0-2, 5") == [0, 1, 2, 5]
    # 反着写要能纠正
    assert ctx.parse_ranges("3-1") == [1, 2, 3]
    # 超上限被截断，不会生成十万个下标
    assert ctx.parse_ranges("8-99999") == [8, 9]
    # 全无法解析 → 空列表，由调用方决定怎么办
    assert ctx.parse_ranges("abc") == []
    assert ctx.parse_ranges("") == []


def test_chat_log_reports_missing_range_instead_of_crashing() -> None:
    ctx = from_messages([], [])
    assert "无对应对话" in ctx.get_chat_log("0-3")


def test_date_falls_back_when_timestamp_missing() -> None:
    """
    时间戳缺失（手工构造、被压缩掉的消息）不该导致记忆写不进去。
    归到今天略有偏差，但记忆还在。
    """
    ctx = ExtractContext(messages=[Msg(role="user", content="x")], timestamps=[])
    assert len(ctx.get_year()) == 4
    assert len(ctx.get_month()) == 2


@pytest.mark.asyncio
async def test_list_traces_all_agents_and_session_filter(
    ready: AsyncSession, memory_dir: Path
) -> None:
    """
    追踪页面的两个过滤诉求：
    1. agent_id 留空 = 列【全部】智能体，而不是只全局
    2. session 过滤按痕迹里记录的 session_id 筛
    """
    import json

    scope = MemoryScope(agent_id=AGENT, session_id="ses_x")
    batch = await memory.write_many(
        [WriteOp(scope=scope, memory_type="preferences", fields={"topic": "t", "content": "- x"})],
        db=ready,
        extraction_id="ext_agent_ses",
    )
    await memory.write_diff(batch, scope=scope)

    # 痕迹 JSON 要记录 agent/session（否则前端无法过滤）
    saved = memory_dir / "agents" / AGENT / ".trace" / "ext_agent_ses.json"
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["agent_id"] == AGENT
    assert payload["session_id"] == "ses_x"

    # 全部智能体：agent_id 空 → 列出 agent 痕迹
    all_traces = await memory.list_traces(MemoryScope(agent_id="", session_id=""))
    assert {t["extraction_id"] for t in all_traces} >= {"ext_agent_ses"}

    # 会话过滤
    filtered = await memory.list_traces(MemoryScope(agent_id=AGENT, session_id="ses_x"))
    assert [t["extraction_id"] for t in filtered] == ["ext_agent_ses"]

    # 别的会话过滤不到
    assert await memory.list_traces(MemoryScope(agent_id=AGENT, session_id="ses_other")) == []


@pytest.mark.asyncio
async def test_read_trace_without_agent_id(ready: AsyncSession, memory_dir: Path) -> None:
    """
    agent_id 为空（"全部智能体"筛选）时，read_trace 也要能读到 agent 级痕迹。

    列表能搜全部目录，详情却只查全局一个的话，点开就是 404。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_x")
    batch = await memory.write_many(
        [WriteOp(scope=scope, memory_type="preferences", fields={"topic": "t", "content": "- x"})],
        db=ready,
        extraction_id="ext_detail",
    )
    await memory.write_diff(batch, scope=scope)

    trace = await memory.read_trace(MemoryScope(agent_id="", session_id=""), "ext_detail")
    assert trace is not None
    assert trace["extraction_id"] == "ext_detail"


@pytest.mark.asyncio
async def test_session_memory_is_flat_not_nested(ready: AsyncSession, memory_dir: Path) -> None:
    """
    会话记忆应平级于 agents，不嵌在 agents/<id>/sessions/ 下。

    会话记忆（events/entities）属于会话本身，只被第一个智能体代表会话修改，
    不按智能体隔离。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_flat")
    result = await memory.write(
        scope,
        "events",
        {
            "event_name": "flat_check",
            "goal": "验证目录",
            "summary": "会话记忆应该平级。",
            "outcome": "success",
            "ranges": "0-2",
        },
        db=ready,
        extract_context=_ctx(),
    )
    assert result.ok, result.error
    # 平级：sessions/<sid>/events/...，而不是 agents/<id>/sessions/<sid>/...
    assert result.uri.startswith("sessions/ses_flat/"), result.uri


@pytest.mark.asyncio
async def test_drop_session_deletes_memories(ready: AsyncSession, memory_dir: Path) -> None:
    """drop_session 删除会话记忆（events 等 session 级记忆）。"""
    scope = MemoryScope(agent_id=AGENT, session_id="ses_drop")
    await memory.write(
        scope,
        "events",
        {
            "event_name": "doomed",
            "goal": "将被删",
            "summary": "会话删除时这条要跟着消失。",
            "outcome": "success",
            "ranges": "0-2",
        },
        db=ready,
        extract_context=_ctx(),
    )
    assert await memory.list_items(scope, "events")

    await memory.drop_session("ses_drop", db=ready)

    assert await memory.list_items(scope, "events") == []
