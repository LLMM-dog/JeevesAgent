"""
用真实模型验证长期记忆。

## 验什么

1. **跨会话记住**：会话 A 说的偏好，会话 B 能用上
2. **不乱记**：普通问答不该产生记忆
3. **不重复记**：同一件事说两次只留一条
4. **能纠正**：偏好变了能更新旧记忆，且留下变更原因
5. **不污染**：注入的记忆不被当成用户输入再次提炼（自反馈）
6. **开关生效**：private_mode 不写、amnesia_mode 不读
7. **记忆是背景不是指令**：不该因为记了"偏好 X"就无条件用 X

用法：
  1. 起后端
  2. uv run python scripts/verify_memory.py
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:9000"
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "jeeves.db"


def load_env() -> dict[str, str]:
    f = ROOT / ".env.verify"
    if not f.exists():
        print("缺 .env.verify")
        sys.exit(2)
    vals: dict[str, str] = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip().strip("\"'")
    return vals


async def chat(c: httpx.AsyncClient, sid: str, content: str) -> dict[str, object]:
    buf = ""
    text: list[str] = []
    tools: list[str] = []
    recalled: list[dict] = []
    errors: list[str] = []
    async with c.stream(
        "POST", f"{BASE}/api/chat", json={"session_id": sid, "content": content}
    ) as r:
        if r.status_code != 200:
            body = (await r.aread()).decode()
            return {"ok": False, "errors": [f"HTTP {r.status_code}: {body[:200]}"]}
        async for raw in r.aiter_bytes():
            buf += raw.decode("utf-8", errors="replace")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                name = ""
                data = None
                for line in block.strip().split("\n"):
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:].strip())
                if data is None:
                    continue
                if name == "message":
                    text.append(data["delta"])
                elif name == "tool_start":
                    tools.append(data["tool_name"])
                    print(f"    → {data['tool_name']}")
                elif name == "memory_recalled":
                    recalled = data["items"]
                    print(f"    ◆ 召回 {data['count']} 条记忆")
                elif name == "error":
                    errors.append(str(data.get("message")))
                    print(f"    [错误] {str(data.get('message'))[:150]}")
    return {
        "ok": not errors,
        "reply": "".join(text),
        "tools": tools,
        "recalled": recalled,
        "errors": errors,
    }


def db_memories(include_archived: bool = False) -> list[tuple]:
    conn = sqlite3.connect(str(DB))
    q = "select id, content, theme, source, confidence, hit, archived_at, history from memory"
    if not include_archived:
        q += " where archived_at is null"
    rows = list(conn.execute(q))
    conn.close()
    return rows


def clear_memories() -> None:
    conn = sqlite3.connect(str(DB))
    conn.execute("delete from memory")
    conn.commit()
    conn.close()


async def new_session(c: httpx.AsyncClient, **flags: object) -> str:
    sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
    body: dict[str, object] = {"approval_mode": "auto"}
    body.update(flags)
    await c.patch(f"{BASE}/api/sessions/{sid}", json=body)
    return sid


async def main() -> int:
    vals = load_env()
    async with httpx.AsyncClient(timeout=1800.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        print("0. 清空已有记忆，保证结果干净")
        clear_memories()

        print("1. 登记端点")
        pr = await c.post(
            f"{BASE}/api/providers",
            json={
                "name": f"mem-test-{int(asyncio.get_running_loop().time())}",
                "base_url": vals["VERIFY_BASE_URL"],
                "api_key": vals["VERIFY_API_KEY"],
                "models": [
                    {
                        "model_id": vals.get("VERIFY_MODEL") or "deepseek-v4-pro",
                        "context_window": 131072,
                    }
                ],
            },
        )
        if pr.status_code != 201:
            print(f"   失败 {pr.status_code}: {pr.text[:300]}")
            return 1
        pid = pr.json()["id"]

        try:
            models = (await c.get(f"{BASE}/api/models?provider_id={pid}")).json()["items"]
            pk = models[0]["id"]
            for purpose in ("chat", "title", "compact"):
                await c.put(f"{BASE}/api/bindings", json={"purpose": purpose, "model_pk": pk})

            failures: list[str] = []

            # ── 场景一：说一个偏好，看它记不记 ──
            print("\n2. 会话 A：陈述一个长期偏好")
            sid_a = await new_session(c)
            r1 = await chat(
                c,
                sid_a,
                "记住一件事：我这个项目的后端统一用 FastAPI，不要给我推荐 Flask 或 Django。",
            )
            if not r1["ok"]:
                print(f"   失败：{r1['errors']}")
                return 1
            await asyncio.sleep(0.5)

            mems = db_memories()
            print(f"   记忆库现有 {len(mems)} 条：")
            for m in mems:
                print(f"     [{m[2]}] {m[1]}  (来源={m[3]} 置信={m[4]})")
            if not mems:
                failures.append("说了「记住」但没有产生任何记忆")
            elif not any("FastAPI" in m[1] for m in mems):
                failures.append(f"记忆内容里没有 FastAPI：{[m[1] for m in mems]}")
            else:
                print("   ✓ 偏好被记下了")

            # ── 场景二：新会话能不能用上 ──
            print("\n3. 会话 B（全新会话）：问一个会触发该偏好的问题")
            sid_b = await new_session(c)
            r2 = await chat(c, sid_b, "我要新写一个后端接口服务，用什么框架好？")
            if not r2["ok"]:
                print(f"   失败：{r2['errors']}")
                return 1
            recalled = r2["recalled"]
            if not recalled:
                failures.append(
                    "新会话没有召回任何记忆 —— 跨会话记忆没生效，这是长期记忆的全部意义"
                )
            else:
                print(f"   ✓ 跨会话召回了 {len(recalled)} 条")
                for m in recalled:
                    print(f"     [{m['theme']}] {m['content']}")

            reply2 = str(r2["reply"])
            if "FastAPI" in reply2:
                print("   ✓ 回答用上了记住的偏好")
            else:
                failures.append("召回了记忆但回答没体现出来")

            # ── 场景三：普通问答不该乱记 ──
            print("\n4. 会话 C：普通问答，不该产生记忆")
            before = len(db_memories())
            sid_c = await new_session(c)
            await chat(c, sid_c, "1 加 1 等于几？只回答数字。")
            await asyncio.sleep(0.5)
            after = len(db_memories())
            if after > before:
                new_ones = [m[1] for m in db_memories()][before:]
                failures.append(
                    f"普通问答产生了 {after - before} 条记忆（记忆污染）：{new_ones}"
                )
            else:
                print(f"   ✓ 没有乱记（仍是 {after} 条）")

            # ── 场景四：同一件事说两次，不该记两条 ──
            print("\n5. 会话 D：重复陈述同一偏好，测去重")
            before = len(db_memories())
            sid_d = await new_session(c)
            await chat(
                c, sid_d, "再强调一次：后端就用 FastAPI，请记住这一点。"
            )
            await asyncio.sleep(0.5)
            after = len(db_memories())
            fastapi_count = sum(1 for m in db_memories() if "FastAPI" in m[1])
            print(f"   含 FastAPI 的记忆条数：{fastapi_count}")
            if fastapi_count > 1:
                failures.append(
                    f"同一件事记了 {fastapi_count} 条 —— 去重没生效"
                )
            else:
                print("   ✓ 没有重复记录")

            # ── 场景五：偏好变了，能不能更新且留痕 ──
            print("\n6. 会话 E：偏好变更，测更新与溯源")
            sid_e = await new_session(c)
            await chat(
                c,
                sid_e,
                "情况变了，这个项目后端改用 Django 了，请更新你记住的框架偏好。",
            )
            await asyncio.sleep(0.5)
            mems = db_memories(include_archived=True)
            print("   记忆库当前状态：")
            changed = False
            for m in mems:
                hist = json.loads(m[7] or "[]")
                arch = "（已归档）" if m[6] else ""
                print(f"     [{m[2]}] {m[1]}{arch}")
                for h in hist:
                    print(f"        ← {h['op']}: {h['reason']}")
                    changed = True
            if not changed:
                failures.append(
                    "偏好变更后没有任何 history 记录 —— 无法追溯记忆怎么变的"
                )
            else:
                print("   ✓ 变更留下了原因（可追溯）")
            if not any("Django" in m[1] for m in mems if not m[6]):
                failures.append("更新后的记忆里没有 Django")

            # ── 场景六：private_mode 不写 ──
            print("\n7. private_mode：这轮不该写记忆")
            before = len(db_memories(include_archived=True))
            sid_p = await new_session(c, private_mode=1)
            await chat(
                c, sid_p, "记住：我的测试数据库密码规则是 test_ 开头。"
            )
            await asyncio.sleep(0.5)
            after = len(db_memories(include_archived=True))
            if after > before:
                failures.append(
                    f"private_mode 下仍然写入了 {after - before} 条记忆"
                )
            else:
                print("   ✓ private_mode 生效，没写入")

            # ── 场景七：amnesia_mode 不读 ──
            print("\n8. amnesia_mode：这轮不该召回记忆")
            sid_am = await new_session(c, amnesia_mode=1)
            r7 = await chat(c, sid_am, "我要写后端服务，用什么框架？")
            if r7["recalled"]:
                failures.append(
                    f"amnesia_mode 下仍然召回了 {len(r7['recalled'])} 条记忆"
                )
            else:
                print("   ✓ amnesia_mode 生效，没召回")

            # ── 场景八：自反馈检查 ──
            print("\n9. 自反馈检查：注入的记忆不该被当成用户输入再记一遍")
            conn = sqlite3.connect(str(DB))
            marker_rows = list(
                conn.execute(
                    "select content from memory where content like '%相关记忆%'"
                )
            )
            conn.close()
            if marker_rows:
                failures.append(
                    f"记忆库里出现了注入标记的内容（自反馈）：{marker_rows}"
                )
            else:
                print("   ✓ 没有把注入的记忆再记一遍")

            # ── 场景九：记忆是背景不是指令 ──
            print("\n10. 记忆不该变成硬指令")
            sid_f = await new_session(c)
            r9 = await chat(
                c,
                sid_f,
                "这次我想专门试试 Flask，就这一次。给我一个 Flask 的最小示例。",
            )
            reply9 = str(r9["reply"]).lower()
            if "flask" not in reply9:
                failures.append(
                    "用户明确要 Flask 却没给 —— 记忆被当成了硬指令而不是背景"
                )
            else:
                print("   ✓ 用户明确要求时能覆盖记忆里的偏好")

            # ── 召回接口 ──
            print("\n11. 召回打分（调试接口）")
            s = (await c.get(f"{BASE}/api/memories-search?q=后端框架用什么")).json()
            for it in s["items"]:
                print(f"   {it['score']:.3f}  [{it['theme']}] {it['content']}")
            if not s["items"]:
                failures.append("召回接口返回空")

            print("\n" + "=" * 58)
            if failures:
                for f in failures:
                    print(f"✗ {f}")
                return 1
            print("通过：跨会话记忆、去重、溯源、双开关、防自反馈全部生效")
            return 0
        finally:
            await c.delete(f"{BASE}/api/providers/{pid}")
            print("（已清理测试端点）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
