"""
记忆系统试验场。

## 它做什么

1. 把 backend/tests/data/sessions/<seed>/messages.jsonl 导入【真实数据库】
   （data/jeeves.db 的 message 表）
2. 用那段对话产出记忆，写进【真实记忆目录】（data/memory/）
3. 把记忆文件全文 + memory_diff 痕迹打印出来

## 为什么不在 tmp 里跑

tmp 里的断言只能验证代码逻辑，看不出"记忆读起来像不像话"。
而记忆的质量（事件切得够不够原子、偏好写得够不够具体）只能人看。
所以跑在真实路径上，产物留在磁盘上供检查。

## 为什么消息走数据库

消息存 SQL 是本项目的决定（见 docs/architecture/memory.md）。
jsonl 只是【人可读的种子】，导入后就不再被读 —— 记忆提取从数据库读对话。
直接读 jsonl 会绕过 seq 分配、artifact upsert、CASCADE，
那三件正是"消息为什么留在 SQL"的全部理由。

## 用法

    uv run python scripts/memory_playground.py --scenario first
    uv run python scripts/memory_playground.py --scenario accumulated
    uv run python scripts/memory_playground.py --scenario both

默认跑完【清理】自己造的数据（会话行 + 该智能体的记忆目录）。
加 --keep 保留产物。

【注意】它会写真实的 data/。用固定的 adf_playground / 独立会话 id，
不碰你已有的智能体和会话。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

AGENT_ID = "adf_playground"


def _hr(title: str, char: str = "=") -> None:
    print(f"\n{char * 78}\n{title}\n{char * 78}")


async def _seed_conversation(db, seed_name: str) -> str:
    """导入对话到 message 表，返回 session_id。"""
    sys.path.insert(0, str(PROJECT_ROOT / "backend" / "tests"))
    from app.modules.session import repo
    from tests.seed import load_conversation, seed_session

    ws = await repo.ensure_default_workspace(db, str(PROJECT_ROOT / "workspace"))
    sid = await seed_session(db, seed_name, workspace_id=ws.id, agent_id=AGENT_ID)

    msgs = await load_conversation(db, sid)
    tool_calls = sum(len(m.tool_calls) for m in msgs)
    print(f"  会话 {sid}：{len(msgs)} 条消息、{tool_calls} 次工具调用（已写入 message 表）")
    return sid


def _memories_from_first_session(scope_agent, scope_session) -> list:
    """
    ses_first_memory 该产出的记忆。

    【手写而非调 LLM】—— 提取流程还没实现。这里模拟"LLM 已经提取好了"
    的输出，用来验证存储层：合并、幂等、隔离、痕迹。

    字段值刻意贴近真实 LLM 输出：events 带 ranges、tool_notes 带计数。
    """
    from app.modules.memory.models import WriteOp

    return [
        WriteOp(scope=scope_agent, memory_type="profile", fields={
            "content": "# LLMM-dog\n- 个人开发者，做 Python 项目\n- 在意工具链一致性（截至 2026-08）",
        }),
        WriteOp(scope=scope_agent, memory_type="preferences", fields={
            "topic": "testing",
            "content": "- 用 `pytest -x --tb=short` 跑测试，遇到第一个失败就停",
        }),
        WriteOp(scope=scope_agent, memory_type="preferences", fields={
            "topic": "code_style",
            "content": "- 提交前必须过 `ruff check`，这是硬性要求\n- 配置集中到 dataclass，不用散落的全局变量",
        }),
        WriteOp(scope=scope_agent, memory_type="preferences", fields={
            "topic": "collaboration",
            "content": "- 涉及布尔参数默认值时先跟他确认，不要自己定",
        }),
        WriteOp(scope=scope_agent, memory_type="experiences", fields={
            "experience_name": "pytest_tmpdir_deprecation",
            "content": (
                "## Situation\n- 测试报 DeprecationWarning 提到 tmpdir\n\n"
                "## Approach\n- 把 fixture 参数从 tmpdir 改成 tmp_path\n"
                "- 把 `tmpdir.join('x')` 改成 `tmp_path / 'x'`\n\n"
                "## Reflect\n- 绝不在 pytest 7+ 的新测试里用 tmpdir"
            ),
        }),
        WriteOp(scope=scope_agent, memory_type="trajectories", fields={
            "trajectory_name": "cli_verbose_and_config_refactor",
            "goal": "给 src/cli.py 加 --verbose 并把配置集中到 dataclass",
            "outcome": "success",
            "content": (
                "1. read_file 读 src/cli.py，确认用的是 argparse\n"
                "2. edit_file 加 Config dataclass 和 --verbose\n"
                "3. 【返工】verbose 默认值设成了 True，用户要求改回 False\n"
                "4. edit_file 修正默认值\n"
                "5. run_shell 跑 ruff check，通过"
            ),
        }),
        WriteOp(scope=scope_agent, memory_type="tool_notes", fields={
            "tool_name": "edit_file",
            "total_calls": 3,
            "fail_count": 0,
            "content": "## 适用场景\n- 小范围精确替换，比重写整个文件安全",
        }),
        # 计数与 fixture 里的真实调用次数一致：run_shell×2
        WriteOp(scope=scope_agent, memory_type="tool_notes", fields={
            "tool_name": "run_shell",
            "total_calls": 2,
            "fail_count": 0,
            "content": "## 适用场景\n- 跑测试和 lint\n\n## 参数要点\n- 长任务要给 timeout",
        }),
        WriteOp(scope=scope_session, memory_type="events", fields={
            "event_name": "verbose_flag_added",
            "goal": "给 CLI 加详细输出开关",
            "summary": "给 src/cli.py 加了 --verbose 参数，并把配置集中到 Config dataclass。",
            "outcome": "success",
            "ranges": "4-6",
        }),
        WriteOp(scope=scope_session, memory_type="events", fields={
            "event_name": "verbose_default_corrected",
            "goal": "修正布尔默认值",
            "summary": "verbose 的默认值从 True 改回 False，并约定以后布尔默认值先确认。",
            "outcome": "success",
            "ranges": "10-13",
        }),
    ]


def _memories_from_second_session(scope_agent, scope_session) -> list:
    """
    ses_accumulated 该产出的记忆。故意与已有记忆冲突。

    这里是整个试验的重点：pytest 偏好【必须被改写】而不是追加。
    """
    from app.modules.memory.models import WriteOp

    return [
        # 用 SEARCH/REPLACE 改写旧偏好 —— 这是关键验证点
        WriteOp(scope=scope_agent, memory_type="preferences", fields={
            "topic": "testing",
            "content": {"blocks": [{
                "search": "- 用 `pytest -x --tb=short` 跑测试，遇到第一个失败就停",
                "replace": "- 用 `pytest -q` 跑全量测试，看完整报告（2026-08 从 -x 改过来）",
            }]},
        }),
        # 内容完全相同 → 应该触发幂等，version 不变
        WriteOp(scope=scope_agent, memory_type="preferences", fields={
            "topic": "code_style",
            "content": "- 提交前必须过 `ruff check`，这是硬性要求\n- 配置集中到 dataclass，不用散落的全局变量",
        }),
        WriteOp(scope=scope_agent, memory_type="experiences", fields={
            "experience_name": "frozen_dataclass_needs_replace",
            "content": (
                "## Situation\n- 给 dataclass 加了 frozen=True 后测试报 FrozenInstanceError\n\n"
                "## Approach\n- 把直接赋值改成 `dataclasses.replace(obj, field=value)`\n\n"
                "## Reflect\n- 绝不在 frozen dataclass 上直接赋值"
            ),
        }),
        # 计数器累加（2 + 3 = 5），同时用 SEARCH/REPLACE 追加一节而不是覆盖。
        #
        # 【这里刻意用 blocks 而不是裸字符串】：第一版写成裸字符串，
        # 结果把"适用场景/参数要点"两节整体顶掉了 —— 痕迹文件里
        # 才看出来。裸字符串对 patch 字段是信息丢失，现在会打 warning。
        WriteOp(scope=scope_agent, memory_type="tool_notes", fields={
            "tool_name": "run_shell",
            "total_calls": 3,
            "fail_count": 1,
            "content": {"blocks": [{
                "search": "## 参数要点\n- 长任务要给 timeout",
                "replace": (
                    "## 参数要点\n- 长任务要给 timeout\n\n"
                    "## 常见失败\n- 测试失败时退出码非 0，要看 is_error 而不是只看有没有输出"
                ),
            }]},
        }),
        WriteOp(scope=scope_session, memory_type="entities", fields={
            "category": "people",
            "name": "xiaoming",
            "content": "# 小明\n用户的后端组同事。\n\n## 关键事实\n- 2026-08 起接手 billing 服务的发布",
        }),
        WriteOp(scope=scope_session, memory_type="events", fields={
            "event_name": "billing_release_owner_assigned",
            "goal": "确认发布负责人",
            "summary": "小明将于下周起接手 billing 服务的发布工作。",
            "outcome": "success",
            "ranges": "13-14",
        }),
    ]


async def _build_extract_context(db, session_id: str):
    """
    从数据库读对话，造出提取期上下文。

    events 的日期路径与对话原文都靠它 —— 日期来自消息的真实 created_at，
    不能让 LLM 提供。
    """
    from app.modules.memory.extract_context import from_messages
    from app.modules.session import repo

    rows = await repo.load_messages(db, session_id, agent_name="")
    return from_messages([repo.row_to_msg(r) for r in rows], [r.created_at for r in rows])


async def _run_scenario(db, name: str, seed: str, build_ops) -> None:
    from app.modules.memory import service as memory
    from app.modules.memory.models import MemoryScope

    _hr(f"场景：{name}")
    sid = await _seed_conversation(db, seed)

    scope_agent = MemoryScope(agent_id=AGENT_ID)
    scope_session = MemoryScope(agent_id=AGENT_ID, session_id=sid)

    await memory.init_agent(AGENT_ID, db=db)
    ctx = await _build_extract_context(db, sid)
    ops = build_ops(scope_agent, scope_session)
    for op in ops:
        op.extract_context = ctx

    extraction_id = f"ext_{name}"
    batch = await memory.write_many(ops, db=db, extraction_id=extraction_id, trace_id=f"trc_{name}")

    for mt in ("preferences", "experiences", "trajectories", "tool_notes"):
        await memory.refresh_overview(scope_agent, mt)

    diff_path = await memory.write_diff(batch, scope=scope_agent)

    print(f"\n  新建 {len(batch.written)} / 更新 {len(batch.edited)} / 未变 {len(batch.unchanged)}")
    if batch.errors:
        print("  ！错误：")
        for e in batch.errors:
            print(f"    - {e}")

    print("\n  ── 逐条痕迹 ──")
    for r in batch.results:
        if r.error:
            mark, detail = "ERR ", r.error
        elif r.created:
            mark, detail = "ADD ", f"v{r.version}，{len(r.after)} 字符"
        elif r.changed:
            mark, detail = "UPD ", f"v{r.version}，{len(r.before)} → {len(r.after)} 字符"
        else:
            mark, detail = "SAME", f"v{r.version}（幂等，未写盘）"
        print(f"    {mark} {r.memory_type:14} {detail}")

    print(f"\n  痕迹文件：data/memory/agents/{AGENT_ID}/.trace/{diff_path}")


async def _dump_memory_tree() -> None:
    from app.modules.memory import layout

    _hr("记忆目录内容", "-")
    root = layout.memory_root()
    if not root.is_dir():
        print("  （空）")
        return

    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        print(f"\n----- {rel} -----")
        print(path.read_text(encoding="utf-8").rstrip())


async def _cleanup(db) -> None:
    from app.modules.memory import service as memory
    from app.modules.session import repo
    from app.modules.session.models import Session
    from sqlalchemy import select

    await memory.drop_agent(AGENT_ID, db=db)
    rows = (await db.execute(select(Session).where(Session.agent_id == AGENT_ID))).scalars().all()
    for row in rows:
        await repo.delete_session(db, row.id)
    print(f"\n已清理：{len(rows)} 个会话 + 智能体 {AGENT_ID} 的记忆目录")


async def _run_pipeline(db, name: str, seed: str, llm_call) -> None:
    """
    跑【完整提取管线】：截断 → 预取 → ReAct → 合并 → 写入 → 痕迹。

    与 _run_scenario 的区别：那个手写记忆内容（只验证存储层），
    这个走真实的 LLM 编排。
    """
    from app.modules.memory.commit import commit_session

    _hr(f"管线：{name}")
    sid = await _seed_conversation(db, seed)

    report = await commit_session(db, session_id=sid, agent_id=AGENT_ID, llm_call=llm_call)

    print(f"\n  {report.summary()}")
    if report.skipped:
        return
    if report.outcome is not None:
        print(f"  循环路径：{[s.kind for s in report.outcome.steps]}")
        print(f"  模型判断：{report.outcome.reasoning[:120]}")
    for w in report.warnings:
        print(f"  ！{w}")
    if report.batch is not None:
        print("\n  ── 逐条痕迹 ──")
        for r in report.batch.results:
            if r.error:
                mark, detail = "ERR ", r.error[:60]
            elif r.created:
                mark, detail = "ADD ", f"v{r.version}，{len(r.after)} 字符"
            elif r.changed:
                mark, detail = "UPD ", f"v{r.version}，{len(r.before)} → {len(r.after)} 字符"
            else:
                mark, detail = "SAME", f"v{r.version}（幂等）"
            print(f"    {mark} {r.memory_type:14} {detail}")


async def _real_llm_call(db):
    """
    真实模型调用。走 memory 功能位（未绑定时回落 compact → chat）。
    """
    from app.infra.llm.openai_compat import OpenAICompatLLM
    from app.modules.endpoint import service as endpoint_service

    resolved = await endpoint_service.resolve(db, purpose="memory")
    llm = OpenAICompatLLM()
    print(f"  提取模型：{resolved.model_id}（{resolved.endpoint_name}）")

    async def call(messages: list[dict[str, str]]) -> str:
        chunks: list[str] = []
        async for ev in llm.stream_chat(
            messages=messages,
            model=resolved.model_id,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
        ):
            if ev.get("type") == "delta" and ev.get("text"):
                chunks.append(str(ev["text"]))
        return "".join(chunks)

    return call


async def main() -> None:
    parser = argparse.ArgumentParser(description="记忆系统试验场（写真实 data/）")
    parser.add_argument("--scenario", choices=["first", "accumulated", "both"], default="both")
    parser.add_argument("--keep", action="store_true", help="保留产物，不清理")
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="跑完整提取管线（需要配好模型）。不加则用手写记忆只验证存储层",
    )
    args = parser.parse_args()

    from app.infra.db.session import get_sessionmaker

    maker = get_sessionmaker()
    async with maker() as db:
        try:
            if args.pipeline:
                llm_call = await _real_llm_call(db)
                if args.scenario in ("first", "both"):
                    await _run_pipeline(db, "first", "ses_first_memory", llm_call)
                if args.scenario in ("accumulated", "both"):
                    await _run_pipeline(db, "accumulated", "ses_accumulated", llm_call)
            else:
                if args.scenario in ("first", "both"):
                    await _run_scenario(
                        db, "first", "ses_first_memory", _memories_from_first_session
                    )
                if args.scenario in ("accumulated", "both"):
                    await _run_scenario(
                        db, "accumulated", "ses_accumulated", _memories_from_second_session
                    )
            await _dump_memory_tree()
        finally:
            if not args.keep:
                await _cleanup(db)
            else:
                print("\n--keep：产物保留在 data/ 下")


if __name__ == "__main__":
    asyncio.run(main())
