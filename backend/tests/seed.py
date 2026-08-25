"""
把对话种子数据导入数据库。

## 为什么种子是 jsonl，而消息存数据库

两件事分开：

- **载体**：jsonl。人可读、可 diff、评审时能看清改了哪句话。
  一个 SQL dump 或 pickle 做不到这些。
- **被测存储**：`message` 表。测试必须走 `repo.append_message` 的真实路径，
  否则 seq 唯一索引、ON DELETE CASCADE 一个都没被验证过 ——
  而那正是"消息为什么留在 SQL"的全部理由。

所以 jsonl 只是【输入源】，导入后就不再被读。测试从数据库读。

如果直接让测试读 jsonl，就等于绕过了自己选的存储层去测它 —— 那种测试
在生产改坏 seq 分配时不会失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.modules.agent.messages import Msg, ToolCall
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession

DATA_DIR = Path(__file__).resolve().parent / "data"
SESSIONS_DIR = DATA_DIR / "sessions"


def seed_path(name: str) -> Path:
    """种子文件路径。缺失时直接报错并指出该放哪 —— 见 data/README.md 的 gitignore 坑。"""
    path = SESSIONS_DIR / name / "messages.jsonl"
    if not path.is_file():
        raise FileNotFoundError(
            f"缺少对话种子：{path}\n"
            "如果它本该存在，检查 .gitignore —— `data/` 会匹配任意层级的 data 目录，"
            "需要 `!backend/tests/data/**` 取反。见 backend/tests/data/README.md"
        )
    return path


def load_seed(name: str) -> list[dict[str, Any]]:
    """
    读 jsonl 成 dict 列表。

    严格解析：坏行直接抛错并带行号。种子数据是测试的前提，
    静默跳过一行会让"少了 3 条消息"这件事无声发生，而断言失败时
    看起来像被测代码的问题。
    """
    rows: list[dict[str, Any]] = []
    text = seed_path(name).read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{name}/messages.jsonl 第 {lineno} 行不是合法 JSON：{e}") from e
        if not isinstance(row, dict):
            raise ValueError(f"{name}/messages.jsonl 第 {lineno} 行不是对象")
        rows.append(row)
    if not rows:
        raise ValueError(f"{name}/messages.jsonl 是空的")
    return rows


def row_to_msg(row: dict[str, Any]) -> Msg:
    """
    种子行 → Msg。

    `tool_calls` 在种子里是 **JSON 字符串**（与数据库列的存法一致），
    而且要同时接受两种形状：

      OpenAI 风格：{"id", "type", "function": {"name", "arguments"}}
      扁平风格：  {"id", "name", "arguments"}

    真实数据里两种都出现过 —— 前者来自上游 API 原样落库，后者是
    repo.append_message 自己序列化的格式（见 repo.py:238）。
    只认一种会让种子数据看起来"对"但导入后 tool_calls 全空。
    """
    raw = row.get("tool_calls")
    parsed: list[Any] = []
    if isinstance(raw, str) and raw.strip():
        parsed = json.loads(raw)
    elif isinstance(raw, list):
        parsed = raw

    calls: list[ToolCall] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else None
        name = (fn or item).get("name")
        if not name:
            continue
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{len(calls)}"),
                name=str(name),
                arguments=str((fn or item).get("arguments") or "{}"),
            )
        )

    return Msg(
        role=row.get("role", "user"),
        content=row.get("content") or "",
        reasoning=row.get("reasoning"),
        tool_calls=calls,
        tool_call_id=row.get("tool_call_id"),
        tool_name=row.get("tool_name"),
        is_error=bool(row.get("is_error")),
        agent_name=row.get("agent_name", ""),
    )


async def seed_session(
    db: AsyncSession,
    seed_name: str,
    *,
    workspace_id: str,
    agent_id: str = "",
) -> str:
    """
    建会话 + 把种子消息逐条写进 message 表。返回 session_id。

    ## 为什么逐条 append 而不是批量 INSERT

    批量 INSERT 会绕过 _next_seq 和"更新会话冗余计数"这两段逻辑。
    那样种子数据在库里【看起来对】，但 session.message_count 是 0、
    seq 可能重复 —— 而这些正是我们想验证的东西。

    种子里的 seq 字段只用来排序，不直接写库：seq 由 _next_seq 分配，
    让唯一索引真正参与进来。
    """
    session = await repo.create_session(db, workspace_id=workspace_id)

    # agent_id 不是 create_session 的参数（它只收 workspace_id 和 title），
    # 所以建完再设。
    if agent_id:
        session.set_agent_ids([agent_id])
        await db.commit()

    rows = sorted(load_seed(seed_name), key=lambda r: int(r.get("seq", 0)))
    for row in rows:
        await repo.append_message(
            db,
            session.id,
            row_to_msg(row),
            display=json.loads(row["tool_display"]) if row.get("tool_display") else None,
        )
    return session.id


async def load_conversation(db: AsyncSession, session_id: str) -> list[Msg]:
    """从数据库读回整段对话。测试和提取流程都走这条路径。"""
    rows = await repo.load_messages(db, session_id, agent_name="")
    return [repo.row_to_msg(r) for r in rows]
