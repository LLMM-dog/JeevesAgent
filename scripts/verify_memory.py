"""
记忆提取的真实模型验证。

## 与 pytest 的分工

pytest 用假 LLM 验证【控制流】：每条分支都能被精确触发。
这个脚本用真实模型验证【契约能不能被真模型满足】：

- 它会不会输出合法 JSON
- page_id 引用对不对
- SEARCH 片段是否逐字符一致（patch 能不能打上）
- 工具调用的参数格式对不对
- 第二次提取会不会改已有记忆而不是新建重复的

这两件事无法互相替代。假 LLM 永远输出正确格式，测不出契约是否可满足；
真模型不可复现，测不出"第 2 轮走了 patch 修复分支"。

## 用法

    uv run python scripts/verify_memory.py            # 两种模式都跑
    uv run python scripts/verify_memory.py --lazy     # 只跑工具调用模式
    uv run python scripts/verify_memory.py --keep     # 保留产物

凭证从 .env.verify 读（VERIFY_BASE_URL / VERIFY_API_KEY / VERIFY_MODEL）。
Key 只当请求头用，输出里只显示尾 4 位。

写真实的 data/，用固定的 adf_verify 智能体，不碰你已有的数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# 单轮 LLM 调用超时。
#
# 推理模型（deepseek-v4-pro 这类）单轮要几分钟 —— 实测首轮 2 分 47 秒，
# 其中绝大部分是推理 token。给 5 分钟，超了就当这一轮失败继续，
# 而不是让整个脚本无声挂住。
LLM_TIMEOUT_SECONDS = 300

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "tests"))

AGENT_ID = "adf_verify"

PASS = "  [OK]  "
FAIL = "  [FAIL]"
INFO = "  ..    "


class Checks:
    """收集断言结果。全部跑完再汇总，不中途退出 —— 一次运行要能看到全貌。"""

    def __init__(self) -> None:
        self.items: list[tuple[bool, str, str]] = []

    def add(self, ok: bool, name: str, detail: str = "") -> bool:
        self.items.append((ok, name, detail))
        print(f"{PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))
        return ok

    @property
    def failed(self) -> list[tuple[bool, str, str]]:
        return [i for i in self.items if not i[0]]

    def report(self) -> bool:
        ok = len(self.items) - len(self.failed)
        print(f"\n{'=' * 78}\n断言 {ok}/{len(self.items)} 通过")
        for _, name, detail in self.failed:
            print(f"  失败：{name} — {detail}")
        return not self.failed


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"缺少 {path}。见文件顶部说明。")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


async def build_llm_call(env: dict[str, str], checks: Checks) -> Any:
    """
    造一个真实的 llm_call，形状与提取循环要求的一致：
    `async (messages, tools) -> (text, tool_calls)`
    """
    from app.infra.llm.openai_compat import OpenAICompatLLM
    from app.infra.llm.port import ResolvedModel
    from app.modules.memory.extract_tools import ToolCall

    base = env.get("VERIFY_BASE_URL", "")
    key = env.get("VERIFY_API_KEY", "")
    if not base or not key:
        raise SystemExit("VERIFY_BASE_URL / VERIFY_API_KEY 未填")

    llm = OpenAICompatLLM()
    names = await llm.list_models(base, key)
    want = env.get("VERIFY_MODEL", "")
    model_id = want if want in names else (names[0] if names else "")
    checks.add(bool(model_id), "模型可用", f"{model_id}（key ...{key[-4:]}）")

    resolved = ResolvedModel(model_id=model_id, base_url=base, api_key=key)
    stats: dict[str, int] = {"rounds": 0, "tool_rounds": 0, "reasoning_chars": 0}

    async def call(messages: list[dict[str, Any]], tools: Any = None) -> tuple[str, list[ToolCall]]:
        stats["rounds"] += 1
        n = stats["rounds"]
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
        print(
            f"{INFO} LLM 第 {n} 轮：提示词 {prompt_chars} 字符，"
            f"工具 {'开' if tools else '关'} … ",
            end="",
            flush=True,
        )

        text: list[str] = []
        reasoning_chars = 0
        merged: dict[int, dict[str, str]] = {}
        t0 = time.monotonic()

        # 【必须有超时】。推理模型单轮可能跑几分钟，而没有超时的话
        # 卡住和"正在推理"从外部完全无法区分 —— 实测首轮花了 2 分 47 秒，
        # 看起来就是死锁。
        try:
            async with asyncio.timeout(LLM_TIMEOUT_SECONDS):
                async for chunk in llm.stream_chat(model=resolved, messages=messages, tools=tools):
                    # 【是 "content" 不是 "text"】。ChunkKind 的字面量是
                    # content/reasoning/tool_call/usage/done（port.py:17）。
                    # 我第一版写成 "text"，于是把所有正文丢掉了，
                    # 表现为"模型返回空 → parse_error"，看起来像模型不听话。
                    if chunk.kind == "content" and chunk.text:
                        text.append(chunk.text)
                    elif chunk.kind == "reasoning" and chunk.text:
                        # 推理内容【不参与解析】，只统计。它是模型的思考过程，
                        # 混进正文会让 JSON 解析必然失败。
                        reasoning_chars += len(chunk.text)
                    elif chunk.kind == "tool_call" and chunk.tool_call:
                        d = chunk.tool_call
                        cur = merged.setdefault(d.index, {"id": "", "name": "", "args": ""})
                        if d.call_id:
                            cur["id"] = d.call_id
                        if d.name:
                            cur["name"] = d.name
                        cur["args"] += d.arguments_delta
        except TimeoutError:
            print(f"超时（{LLM_TIMEOUT_SECONDS}s）")
            return "", []

        stats["reasoning_chars"] += reasoning_chars
        calls = [
            ToolCall(call_id=v["id"] or f"call_{i}", name=v["name"], arguments=v["args"] or "{}")
            for i, v in sorted(merged.items())
            if v["name"]
        ]
        if calls:
            stats["tool_rounds"] += 1

        body = "".join(text)
        print(
            f"{time.monotonic() - t0:.1f}s，"
            f"推理 {reasoning_chars} 字符，正文 {len(body)} 字符"
            + (f"，工具调用 {len(calls)} 个" if calls else "")
        )
        return body, calls

    call.stats = stats  # type: ignore[attr-defined]
    return call


async def run_round(
    db: Any, *, seed: str, llm_call: Any, checks: Checks, label: str
) -> Any:
    from app.modules.memory.commit import commit_session
    from app.modules.session import repo
    from tests.seed import seed_session

    ws = await repo.ensure_default_workspace(db, str(PROJECT_ROOT / "workspace"))
    sid = await seed_session(db, seed, workspace_id=ws.id, agent_id=AGENT_ID)

    print(f"\n{'-' * 78}\n{label}（会话 {sid}）")
    report = await commit_session(db, session_id=sid, agent_id=AGENT_ID, llm_call=llm_call)

    print(f"{INFO} {report.summary()}")
    if report.outcome is not None:
        print(f"{INFO} 循环路径 {[s.kind for s in report.outcome.steps]}")
        if report.outcome.tools_used:
            for t in report.outcome.tools_used:
                print(f"{INFO} 工具 {t['name']}({json.dumps(t['args'], ensure_ascii=False)[:70]})")
        print(f"{INFO} 模型判断：{report.outcome.reasoning[:150]}")
    for w in report.warnings:
        print(f"{INFO} 警告：{w}")

    checks.add(not report.skipped, f"{label}：没有被跳过", report.skipped)
    if report.outcome is not None:
        checks.add(
            report.outcome.iterations <= 6,
            f"{label}：迭代次数合理",
            f"{report.outcome.iterations} 轮",
        )
        # 真模型能不能满足 JSON 契约 —— 这是最核心的一条
        checks.add(
            not any(s.kind == "parse_error" for s in report.outcome.steps),
            f"{label}：首轮就输出了合法 JSON",
        )
    if report.batch is not None:
        checks.add(
            report.batch.ok,
            f"{label}：所有写入成功",
            "; ".join(report.batch.errors)[:200],
        )
    return report


async def verify_mode(db: Any, *, eager: bool, llm_call: Any, checks: Checks) -> None:
    from app.core.config import settings
    from app.modules.memory import service as memory
    from app.modules.memory.models import MemoryScope

    mode = "eager（全预取，无工具）" if eager else "lazy（按需 read，带工具）"
    print(f"\n{'=' * 78}\n模式：{mode}\n{'=' * 78}")
    settings.memory.eager_prefetch = eager

    await memory.init_agent(AGENT_ID, db=db)

    # ── 第一次提取：全新记忆 ──
    r1 = await run_round(db, seed="ses_first_memory", llm_call=llm_call, checks=checks, label="首次提取")
    if r1.batch is not None:
        checks.add(len(r1.batch.written) > 0, "首次提取：产出了新记忆", f"{len(r1.batch.written)} 条")

    scope = MemoryScope(agent_id=AGENT_ID)
    prefs = await memory.list_items(scope, "preferences")
    print(f"{INFO} 提取到 {len(prefs)} 条偏好：{[p.title for p in prefs]}")
    checks.add(len(prefs) > 0, "首次提取：至少记住一条偏好")

    # ── 第二次提取：必须改已有而非新建重复 ──
    before = len(prefs)
    r2 = await run_round(
        db, seed="ses_accumulated", llm_call=llm_call, checks=checks, label="二次提取"
    )

    prefs_after = await memory.list_items(scope, "preferences")
    print(f"{INFO} 现在 {len(prefs_after)} 条偏好：{[p.title for p in prefs_after]}")

    # 去重的核心断言：偏好数不该翻倍
    checks.add(
        len(prefs_after) <= before + 2,
        "二次提取：没有大量新建重复偏好",
        f"{before} → {len(prefs_after)}",
    )

    if r2.batch is not None and not eager:
        checks.add(
            bool(r2.outcome and r2.outcome.tools_used),
            "lazy 模式：模型实际调用了工具",
            f"{len(r2.outcome.tools_used) if r2.outcome else 0} 次",
        )

    # ── 记忆内容质量 ──
    all_items = await memory.list_items(scope)
    types_seen = {i.memory_type for i in all_items}
    print(f"{INFO} 覆盖的记忆类型：{sorted(types_seen)}")
    checks.add(len(types_seen) >= 2, "覆盖了多种记忆类型", str(sorted(types_seen)))

    empty = [i.uri for i in all_items if not i.body.strip()]
    checks.add(not empty, "没有产生空记忆", str(empty[:3]))

    # 事件必须落在按日期分层的路径下
    events = [i for i in all_items if i.memory_type == "events"]
    if events:
        checks.add(
            all("/20" in e.uri for e in events),
            "事件按日期分层存放",
            events[0].uri,
        )


async def reset_agent(db: Any) -> None:
    """清空该智能体的记忆【和会话】。两者必须一起清，见调用点的说明。"""
    from app.modules.memory import service as memory
    from app.modules.session import repo
    from app.modules.session.models import Session
    from sqlalchemy import select

    await memory.drop_agent(AGENT_ID, db=db)
    rows = (await db.execute(select(Session).where(Session.agent_id == AGENT_ID))).scalars().all()
    for row in rows:
        await repo.delete_session(db, row.id)


async def verify_embedding(db: Any, env: dict[str, str], checks: Checks) -> None:
    """
    用【真实嵌入模型】验证向量化与语义搜索。

    与假嵌入的分工：假嵌入验"维度不一致时跳过"这类控制流；
    真实嵌入验"语义相近的内容真的会被排在前面"——那是假嵌入
    按关键词命中造出来的，证明不了真实模型的行为。
    """
    from app.infra.llm.embedding import probe_dim
    from app.infra.llm.port import ResolvedModel
    from app.modules.memory import service as memory
    from app.modules.memory import vectorize as vec
    from app.modules.memory.models import MemoryScope

    base = env.get("VERIFY_EMBEDDING_BASE_URL") or env.get("VERIFY_BASE_URL", "")
    key = env.get("VERIFY_EMBEDDING_API_KEY") or env.get("VERIFY_API_KEY", "")
    model_id = env.get("VERIFY_EMBEDDING_MODEL", "").strip()

    print(f"\n{'=' * 78}\n真实嵌入模型验证\n{'=' * 78}")
    if not model_id:
        print(f"{INFO} 未配 VERIFY_EMBEDDING_MODEL，跳过")
        return

    resolved = ResolvedModel(
        model_id=model_id, base_url=base, api_key=key, purpose="embedding"
    )
    dim = await probe_dim(resolved)
    if not checks.add(dim > 0, "嵌入模型可用", f"{model_id}，{dim} 维"):
        return

    # 让 service 用这个模型（正常情况下从 endpoint 表解析）
    async def resolve(_db: Any) -> ResolvedModel:
        return resolved

    memory.resolve_embedding_model = resolve  # type: ignore[assignment]

    await reset_agent(db)
    await memory.init_agent(AGENT_ID, db=db)
    scope = MemoryScope(agent_id=AGENT_ID)

    # 写三条语义上明显不同的记忆
    written = []
    for topic, content in (
        ("testing", "- 用 pytest -q 跑测试，看完整报告"),
        ("database", "- 数据库迁移用 alembic，SQLite 要 batch 模式"),
        ("cooking", "- 喜欢吃川菜，尤其是水煮鱼"),
    ):
        r = await memory.write(scope, "preferences", {"topic": topic, "content": content}, db=db)
        written.append(r.uri)

    # 只增类型也要向量化 —— 这是核心验证点
    traj = await memory.write(scope, "trajectories", {
        "trajectory_name": "async_hang_fix",
        "task_query": "修复异步测试挂住的问题",
        "outcome": "success",
        "retrieval_anchor": "场景：异步测试挂住不返回；能力：定位被吞掉的 CancelledError",
        "content": "- 步骤：\n  1. 检查 except 范围",
    }, db=db)
    written.append(traj.uri)

    report = await memory.vectorize(db, written)
    print(f"{INFO} {report.summary()}")
    checks.add(report.succeeded == len(written), "全部记忆向量化成功", report.summary())
    checks.add(report.dim == dim, "向量维度与探测一致", f"{report.dim} vs {dim}")

    # 只增类型确实有向量
    row = await _index_row(db, traj.uri)
    checks.add(
        row is not None and row.embedding is not None,
        "只增类型（trajectories）也被向量化",
    )
    if row is not None:
        checks.add(
            row.embedded_hash == row.content_hash,
            "embedded_hash 与 content_hash 一致",
        )

    # 语义搜索：查"单元测试怎么跑"该命中 testing 而不是 cooking
    hits = await memory.search_semantic(db, scope, "单元测试应该怎么运行")
    if checks.add(bool(hits), "语义搜索返回结果", f"{len(hits)} 条"):
        top = hits[0]
        print(f"{INFO} 命中排序：{[(h.title, round(h.score, 3)) for h in hits[:4]]}")
        checks.add(
            top.title == "testing",
            "语义最相关的排第一",
            f"实际第一：{top.title}（{top.score:.3f}）",
        )
        cooking = next((h for h in hits if h.title == "cooking"), None)
        checks.add(
            cooking is None or cooking.score < top.score,
            "无关记忆的分数更低",
            f"cooking={cooking.score:.3f}" if cooking else "未命中",
        )

    # 只增类型能被召回
    traj_hits = await memory.search_semantic(db, scope, "异步测试卡住不返回怎么查")
    checks.add(
        any(h.uri == traj.uri for h in traj_hits),
        "只增类型能被语义召回",
        f"{[h.title for h in traj_hits[:3]]}",
    )

    # 会话隔离。
    #
    # 【必须用 scope: session 的类型】。第一版我用了 preferences，
    # 但它是 scope: agent —— 写它时 session_id 被正确忽略，
    # 于是"其他会话也能搜到"是对的行为，而断言失败看起来像隔离坏了。
    # events 才是会话级的。
    ev_scope = MemoryScope(agent_id=AGENT_ID, session_id="ses_isolation")
    ev = await memory.write(
        ev_scope,
        "events",
        {
            "event_name": "session_only_fact",
            "goal": "记一个只属于本会话的事实",
            "summary": "本次会话确认了一个只属于这个会话的临时约定。",
            "outcome": "success",
            "ranges": "0",
        },
        db=db,
        extract_context=_isolation_ctx(),
    )
    await memory.vectorize(db, [ev.uri])

    from_own = await memory.search_semantic(db, ev_scope, "本会话的临时约定")
    from_other = await memory.search_semantic(
        db, MemoryScope(agent_id=AGENT_ID, session_id="ses_other"), "本会话的临时约定"
    )
    checks.add(any(h.uri == ev.uri for h in from_own), "本会话能搜到自己的会话级记忆")
    checks.add(
        not any(h.uri == ev.uri for h in from_other),
        "会话级记忆对其他会话不可见",
        f"其他会话命中了 {[h.title for h in from_other]}",
    )
    # agent 级查询也不该看到任何会话记忆
    from_agent = await memory.search_semantic(db, scope, "本会话的临时约定")
    checks.add(
        not any(h.uri == ev.uri for h in from_agent),
        "agent 级查询不含会话记忆",
    )

    # 换模型 → 旧向量失效 → 一键重算恢复
    stats_before = await memory.vector_status(db)
    print(f"{INFO} 换模型前：{stats_before}")

    fake_other = ResolvedModel(
        model_id=model_id + "-v2", base_url=base, api_key=key, purpose="embedding"
    )

    async def resolve_other(_db: Any) -> ResolvedModel:
        return fake_other

    memory.resolve_embedding_model = resolve_other  # type: ignore[assignment]
    stats_after = await memory.vector_status(db)
    checks.add(
        stats_after["model"] > 0,
        "换模型后旧向量被标记为失效",
        f"{stats_after['model']} 条",
    )
    checks.add(
        await memory.search_semantic(db, scope, "测试") == [],
        "换模型后旧向量停止参与召回",
    )

    # 换回来并重算
    memory.resolve_embedding_model = resolve  # type: ignore[assignment]
    rebuild = await memory.revectorize(db, only_stale=False)
    print(f"{INFO} 一键重算：{rebuild.summary()}")
    checks.add(rebuild.succeeded > 0, "一键重算成功", rebuild.summary())
    checks.add(
        bool(await memory.search_semantic(db, scope, "单元测试怎么跑")),
        "重算后召回恢复",
    )

    cleared = await memory.clear_vectors(db)
    checks.add(cleared > 0, "清空向量", f"{cleared} 条")
    checks.add(
        await memory.search_semantic(db, scope, "测试") == [],
        "清空后语义搜索返回空（回落关键词）",
    )
    # 记忆文件本身不受影响
    checks.add(
        await memory.read_uri(written[0]) is not None,
        "清空向量不影响记忆文件",
    )
    _ = vec  # 保持 import 显式，说明这段验证的是 vectorize 模块


def _isolation_ctx() -> Any:
    """events 的日期路径需要 extract_context。手工构造一个最小的。"""
    from app.modules.agent.messages import Msg
    from app.modules.memory.extract_context import from_messages

    return from_messages(
        [Msg(role="user", content="这个约定只在本次会话有效")], [1786608000000]
    )


async def _index_row(db: Any, uri: str) -> Any:
    from app.modules.memory.models_db import MemoryIndex
    from sqlalchemy import select

    return (
        await db.execute(select(MemoryIndex).where(MemoryIndex.uri == uri))
    ).scalars().one_or_none()


async def dump_and_cleanup(db: Any, *, keep: bool) -> None:
    from app.modules.memory import layout
    from app.modules.session.models import Session
    from sqlalchemy import select

    print(f"\n{'=' * 78}\n产出的记忆\n{'=' * 78}")
    root = layout.memory_root()
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if AGENT_ID not in rel and "global" not in rel:
            continue
        print(f"\n----- {rel} -----")
        print(path.read_text(encoding="utf-8").rstrip()[:1800])

    trace_dir = root / "agents" / AGENT_ID / ".trace"
    if trace_dir.is_dir():
        print(f"\n痕迹文件：{len(list(trace_dir.glob('*.json')))} 个")

    if keep:
        print(f"\n--keep：产物保留在 {root}")
        return

    rows = (await db.execute(select(Session).where(Session.agent_id == AGENT_ID))).scalars().all()
    await reset_agent(db)
    print(f"\n已清理 {len(rows)} 个会话 + {AGENT_ID} 的记忆")


async def main() -> None:
    parser = argparse.ArgumentParser(description="记忆提取的真实模型验证")
    parser.add_argument("--eager", action="store_true", help="只跑 eager 模式")
    parser.add_argument("--lazy", action="store_true", help="只跑 lazy（工具调用）模式")
    parser.add_argument("--keep", action="store_true", help="保留产物")
    parser.add_argument(
        "--embedding",
        action="store_true",
        help="只验证向量化与语义搜索（用 VERIFY_EMBEDDING_* 配的嵌入模型）",
    )
    args = parser.parse_args()

    env = load_env(PROJECT_ROOT / ".env.verify")
    checks = Checks()

    from app.infra.db.session import get_sessionmaker

    # 只验向量时不需要对话模型 —— 那能省掉几分钟的提取等待
    if args.embedding:
        maker = get_sessionmaker()
        async with maker() as db:
            try:
                await verify_embedding(db, env, checks)
            finally:
                await reset_agent(db)
        raise SystemExit(0 if checks.report() else 1)

    llm_call = await build_llm_call(env, checks)

    modes: list[bool] = []
    if args.eager or not args.lazy:
        modes.append(True)
    if args.lazy or not args.eager:
        modes.append(False)

    maker = get_sessionmaker()
    async with maker() as db:
        try:
            for eager in modes:
                await verify_mode(db, eager=eager, llm_call=llm_call, checks=checks)
                if len(modes) > 1:
                    # 模式之间【连会话一起清】。
                    #
                    # 只 drop_agent 会留下上一模式的会话行，于是第二种模式
                    # 又导入一份同样的对话 —— 而记忆虽然被删了，模型看到的是
                    # "这段对话我刚处理过"的重复内容，可能判断"没有新东西可记"。
                    #
                    # 实测踩到：lazy 模式首次提取产出 0 条，看起来像 lazy 有 bug，
                    # 实际是它对着第二份重复会话做了正确判断。
                    await reset_agent(db)
            await verify_embedding(db, env, checks)
            await dump_and_cleanup(db, keep=args.keep)
        finally:
            # stats 挂在 llm_call 上（build_llm_call 里的闭包），
            # 不是 main 的局部变量 —— 直接写 stats[...] 会 NameError。
            s = llm_call.stats
            print(
                f"\nLLM 调用 {s['rounds']} 轮（其中 {s['tool_rounds']} 轮返回工具调用），"
                f"推理约 {s['reasoning_chars']} 字符"
            )

    raise SystemExit(0 if checks.report() else 1)


if __name__ == "__main__":
    asyncio.run(main())
