"""
验证会话置顶与截断重发。

## 验什么

置顶和 truncate_from 的后端代码 M1 就在了，但一直【没有测试也没有前端
入口】。这次接上线之后要确认：

1. 置顶真的改变列表顺序（不只是字段能存）
2. 截断的边界正确：删该条及其之后，前面一条不少
3. message_count / last_message_at 跟着变，列表页显示不会和实际内容对不上
4. 流式生成中截断被拒（409），且数据完好
5. 截断后重发能拿到正确的新回答 —— 这是整个功能的目的

第 5 条必须用真实模型：截断后上下文是否干净只有让模型答一次才知道。
如果 tool 消息没删干净，请求会直接 400。

用法：
  1. 起后端
  2. uv run python scripts/verify_session_ops.py
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:9000"
ROOT = Path(__file__).resolve().parent.parent


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


async def chat(c: httpx.AsyncClient, sid: str, content: str) -> str:
    """发一轮，返回回复文本。"""
    buf = ""
    out: list[str] = []
    async with c.stream(
        "POST", f"{BASE}/api/chat", json={"session_id": sid, "content": content}
    ) as r:
        if r.status_code != 200:
            body = (await r.aread()).decode()
            return f"[HTTP {r.status_code}] {body[:200]}"
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
                    out.append(data["delta"])
                elif name == "error":
                    out.append(f"[错误] {data.get('message')}")
    return "".join(out)


async def main() -> int:  # noqa: PLR0915
    v = load_env()
    failures: list[str] = []
    created: list[str] = []

    async with httpx.AsyncClient(timeout=1800.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        try:
            print("1. 登记模型")
            pr = await c.post(
                f"{BASE}/api/providers",
                json={
                    "name": f"sops-{int(asyncio.get_running_loop().time())}",
                    "base_url": v["VERIFY_BASE_URL"],
                    "api_key": v["VERIFY_API_KEY"],
                    "models": [
                        {
                            "model_id": v.get("VERIFY_MODEL") or "deepseek-v4-pro",
                            "context_window": 131072,
                        }
                    ],
                },
            )
            if pr.status_code != 201:
                print(f"   失败 {pr.status_code}: {pr.text[:300]}")
                return 1
            pid = pr.json()["id"]
            created.append(pid)
            pk = (await c.get(f"{BASE}/api/models?provider_id={pid}")).json()["items"][0]["id"]
            for purpose in ("chat", "title", "compact"):
                await c.put(f"{BASE}/api/bindings", json={"purpose": purpose, "model_pk": pk})

            # ── 置顶 ──
            print("\n2. 置顶：应改变列表顺序")
            tag = f"pin{int(asyncio.get_running_loop().time() * 1000) % 100000}"
            s_old = (
                await c.post(f"{BASE}/api/sessions", json={"title": f"{tag}-旧"})
            ).json()["id"]
            await asyncio.sleep(0.05)
            s_new = (
                await c.post(f"{BASE}/api/sessions", json={"title": f"{tag}-新"})
            ).json()["id"]

            # 用标题搜索过滤，只看这次建的两个。
            #
            # 不能靠 size 拉全量 —— 上限是 100（routes_chat.py:101），
            # 而库里已经有上百个历史会话，新建的会被挤出第一页。
            items = (await c.get(f"{BASE}/api/sessions?q={tag}&size=100")).json()["items"]
            ids = [x["id"] for x in items]
            if s_new not in ids or s_old not in ids:
                failures.append(
                    f"新建的会话不在列表里（共 {len(ids)} 个）—— "
                    "last_message_at 可能是 0 导致排到最后"
                )
                return _finish(failures, c, created)
            print(f"   置顶前：新的在第 {ids.index(s_new)} 位，旧的在第 {ids.index(s_old)} 位")
            if ids.index(s_new) > ids.index(s_old):
                failures.append("默认排序不对，新建的会话应该在前")

            r = await c.patch(f"{BASE}/api/sessions/{s_old}", json={"pinned": True})
            if r.status_code != 200:
                failures.append(f"置顶接口失败 {r.status_code}: {r.text[:200]}")
            elif not r.json().get("pinned"):
                failures.append("置顶后返回的 pinned 不是 true")

            items = (await c.get(f"{BASE}/api/sessions?q={tag}&size=100")).json()["items"]
            ids = [x["id"] for x in items]
            print(f"   置顶后：旧的在第 {ids.index(s_old)} 位")
            if ids[0] != s_old:
                failures.append("置顶的会话没有排到最前")
            else:
                print("   ✓ 置顶生效")

            await c.patch(f"{BASE}/api/sessions/{s_old}", json={"pinned": False})
            items = (await c.get(f"{BASE}/api/sessions?q={tag}&size=100")).json()["items"]
            if [x["id"] for x in items].index(s_new) > [x["id"] for x in items].index(s_old):
                failures.append("取消置顶后没回到时间序")
            else:
                print("   ✓ 取消置顶后回到时间序")

            # ── 截断 ──
            print("\n3. 造三轮对话（其中一轮会调工具，产生 tool 消息）")
            sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid}", json={"approval_mode": "auto"})

            a1 = await chat(c, sid, "记住数字 7391。只回复'好'。")
            print(f"   第一轮：{a1[:40]}")
            a2 = await chat(c, sid, "用 run_shell 执行 echo hello。")
            print(f"   第二轮：{a2[:40]}")
            a3 = await chat(c, sid, "我刚让你记的数字是多少？只回答数字。")
            print(f"   第三轮：{a3[:40]}")
            if "7391" not in a3:
                failures.append(f"第三轮没记住数字，上下文可能有问题：{a3[:100]}")

            msgs = (await c.get(f"{BASE}/api/sessions/{sid}/messages")).json()["items"]
            roles = [m["role"] for m in msgs]
            print(f"   消息角色序列：{roles}")
            has_tool = "tool" in roles
            print(f"   含 tool 消息：{has_tool}")

            sess = (await c.get(f"{BASE}/api/sessions/{sid}")).json()
            print(f"   message_count={sess['message_count']}  last_message_at={sess['last_message_at']}")

            # 从第三轮的用户消息截断
            user_msgs = [m for m in msgs if m["role"] == "user"]
            if len(user_msgs) < 3:
                failures.append(f"用户消息只有 {len(user_msgs)} 条，造数据失败")
                return _finish(failures, c, created)
            third = user_msgs[2]
            idx = next(i for i, m in enumerate(msgs) if m["id"] == third["id"])
            expect_deleted = len(msgs) - idx
            print(f"\n4. 从第三轮用户消息截断（预计删 {expect_deleted} 条）")

            r = await c.delete(f"{BASE}/api/sessions/{sid}/messages/{third['id']}")
            print(f"   HTTP {r.status_code}  {r.text[:80]}")
            if r.status_code != 200:
                failures.append(f"截断失败 {r.status_code}: {r.text[:200]}")
            else:
                n = r.json()["deleted_count"]
                if n != expect_deleted:
                    failures.append(f"删除条数不对：预计 {expect_deleted}，实际 {n}")
                else:
                    print(f"   ✓ 删了 {n} 条")

            after = (await c.get(f"{BASE}/api/sessions/{sid}/messages")).json()["items"]
            print(f"   剩余角色：{[m['role'] for m in after]}")
            if any(m["id"] == third["id"] for m in after):
                failures.append("目标消息自己没被删掉")
            if len(after) != idx:
                failures.append(f"剩余条数不对：预计 {idx}，实际 {len(after)}")
            else:
                print("   ✓ 边界正确（前面的都保留）")

            sess2 = (await c.get(f"{BASE}/api/sessions/{sid}")).json()
            print(f"   截断后 message_count={sess2['message_count']}")
            if sess2["message_count"] >= sess["message_count"]:
                failures.append("message_count 没有减少")
            elif sess2["message_count"] < 0:
                failures.append(f"message_count 变负数：{sess2['message_count']}")
            else:
                print("   ✓ 计数同步了")

            if after:
                newest = max(m["created_at"] for m in after)
                if sess2["last_message_at"] != newest:
                    failures.append(
                        f"last_message_at 没回退：{sess2['last_message_at']} != {newest}"
                    )
                else:
                    print("   ✓ last_message_at 回退到剩余消息")

            # ── 截断后重发 ──
            print("\n5. 截断后重发：上下文要干净，模型仍能答对")
            a3b = await chat(c, sid, "我刚让你记的数字是多少？只回答数字。")
            print(f"   重发回复：{a3b[:60]}")
            if "HTTP 4" in a3b or "错误" in a3b:
                failures.append(
                    f"截断后请求失败 —— 上下文可能残留孤立的 tool 消息：{a3b[:200]}"
                )
            elif "7391" not in a3b:
                failures.append(f"截断后模型答不出数字，上下文被破坏了：{a3b[:120]}")
            else:
                print("   ✓ 上下文干净，模型答对了")

            # ── 跑着的时候截断应被拒 ──
            print("\n6. 流式生成中截断应返回 409")
            msgs3 = (await c.get(f"{BASE}/api/sessions/{sid}/messages")).json()["items"]
            first_user = next(m for m in msgs3 if m["role"] == "user")
            before_count = len(msgs3)

            # 用 meta 事件当信号，不用固定 sleep。
            #
            # 最初写的是 sleep(2.5)，结果模型 2 秒就答完了 —— DELETE 落在
            # run 结束之后，返回 200，看起来像"守卫没生效"。
            # 实际是测试自己的时序问题，守卫是好的。
            #
            # meta 是第一个事件（带 run_id / user_message_id），它一到就说明
            # run 已注册且还在跑。
            started = asyncio.Event()

            async def _long_chat() -> None:
                buf = ""
                async with c.stream(
                    "POST",
                    f"{BASE}/api/chat",
                    json={"session_id": sid, "content": "从 1 数到 60，每个数字单独一行。"},
                ) as r:
                    async for raw in r.aiter_bytes():
                        buf += raw.decode("utf-8", errors="replace")
                        while "\n\n" in buf:
                            block, buf = buf.split("\n\n", 1)
                            nm = ""
                            for line in block.strip().split("\n"):
                                if line.startswith("event:"):
                                    nm = line[6:].strip()
                            if nm == "meta":
                                started.set()

            task = asyncio.create_task(_long_chat())
            await asyncio.wait_for(started.wait(), timeout=60)
            r409 = await c.delete(f"{BASE}/api/sessions/{sid}/messages/{first_user['id']}")
            print(f"   HTTP {r409.status_code}")
            if r409.status_code == 409:
                body = (r409.json() or {}).get("detail", {})
                print(f"   code={body.get('code')}  hint={str(body.get('hint'))[:70]}")
                if body.get("code") != "run_in_progress":
                    failures.append(f"错误码不对：{body.get('code')}")
                elif not body.get("hint"):
                    failures.append("409 没给下一步提示")
                else:
                    print("   ✓ 拒了，且提示了下一步")
            else:
                failures.append(
                    f"生成中截断没被拒（HTTP {r409.status_code}）—— "
                    "run 会继续往被删的历史后面写消息"
                )
            await task
            msgs4 = (await c.get(f"{BASE}/api/sessions/{sid}/messages")).json()["items"]
            if not any(m["id"] == first_user["id"] for m in msgs4):
                failures.append("被拒的截断仍然删掉了消息")
            else:
                print(f"   ✓ 数据完好（截断前 {before_count} 条，现在 {len(msgs4)} 条）")

            return _finish(failures, c, created)
        finally:
            for pid in created:
                await c.delete(f"{BASE}/api/providers/{pid}")
            print(f"（已清理 {len(created)} 个端点）")


def _finish(failures: list[str], c: object, created: list[str]) -> int:
    print("\n" + "=" * 58)
    if failures:
        for f in failures:
            print(f"✗ {f}")
        return 1
    print("通过：置顶排序、截断边界、计数同步、重发后上下文干净、生成中拒绝截断")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
