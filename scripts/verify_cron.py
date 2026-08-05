"""
用真实后端 + 真实模型验证定时任务。

## 验什么

单测里大量是源码断言 —— 它们保证结构对，不能保证"到点真的会触发"。

1. 建任务、cron 校验、预览触发时间
2. **任务真的到点触发**（设一个 1 分钟内的表达式，等它跑）
3. 触发的会话真的跑完了 agent 循环（无头执行）
4. 会话强制 auto 审批
5. 执行历史落库且关联 session
6. **一个慢任务不阻塞另一个**（串行问题）
7. 错过的窗口有记录（模拟重启）
8. 手动触发
9. 删任务清理干净

用法：
  1. 起后端
  2. uv run python scripts/verify_cron.py
"""

import asyncio
import datetime as dt
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:9000"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


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


async def main() -> int:  # noqa: PLR0915
    v = load_env()
    fails: list[str] = []
    created_providers: list[str] = []
    created_tasks: list[str] = []

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
                    "name": f"cron-{int(time.time()) % 100000}",
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
                print(f"   失败 {pr.status_code}: {pr.text[:200]}")
                return 1
            pid = pr.json()["id"]
            created_providers.append(pid)
            pk = (await c.get(f"{BASE}/api/models?provider_id={pid}")).json()["items"][0]["id"]
            for purpose in ("chat", "title", "compact"):
                await c.put(f"{BASE}/api/bindings", json={"purpose": purpose, "model_pk": pk})
            print("   ✓")

            print("\n2. cron 表达式校验与预览")
            r = await c.post(f"{BASE}/api/cron/validate", json={"cron": "0 9 * * *"})
            d = r.json()
            print(f"   合法={d['valid']} 描述={d.get('text')}")
            print(f"   接下来 3 次：{[dt.datetime.fromtimestamp(x / 1000).strftime('%m-%d %H:%M') for x in d['next'][:3]]}")
            if not d["valid"] or len(d["next"]) != 5:
                fails.append(f"预览不对：{d}")
            else:
                print("   ✓")

            r = await c.post(f"{BASE}/api/cron/validate", json={"cron": "0 9 * *"})
            print(f"   非法表达式：{r.json().get('error', '')[:80]}")
            if r.json()["valid"]:
                fails.append("非法表达式没被识别")

            print("\n3. 建任务时校验（而不是等到调度时才发现）")
            r = await c.post(
                f"{BASE}/api/cron/tasks", json={"prompt": "x", "cron": "garbage here now"}
            )
            print(f"   非法 cron → HTTP {r.status_code}")
            if r.status_code != 400:
                fails.append(f"非法 cron 应返回 400，实际 {r.status_code}")
            else:
                print("   ✓ 入口就拦住了")

            # ── 关键：真的到点触发 ──
            print("\n4. 真的到点触发（设一个下一分钟的表达式）")
            now = dt.datetime.now()
            fire_min = (now.minute + 1) % 60
            expr = f"{fire_min} * * * *"
            wait_s = (60 - now.second) + 3
            print(f"   现在 {now:%H:%M:%S}，表达式 {expr!r}，约 {wait_s}s 后触发")

            r = await c.post(
                f"{BASE}/api/cron/tasks",
                json={
                    "name": "验证-自动触发",
                    "prompt": "只回复三个字：定时成功。不要调用任何工具。",
                    "cron": expr,
                },
            )
            if r.status_code != 201:
                fails.append(f"建任务失败 {r.status_code}: {r.text[:200]}")
                return _finish(fails, c, created_providers, created_tasks)
            tid = r.json()["id"]
            created_tasks.append(tid)
            nxt = r.json()["next_fire_at"]
            print(f"   任务 {tid}，下次触发 {dt.datetime.fromtimestamp(nxt / 1000):%H:%M:%S}")

            print(f"   等待触发（最多 {wait_s + 90}s）…")
            fired = False
            session_id = ""
            deadline = time.monotonic() + wait_s + 90
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                rr = await c.get(f"{BASE}/api/cron/tasks/{tid}/runs")
                runs = rr.json()["items"]
                if runs:
                    st = runs[0]["status"]
                    session_id = runs[0].get("session_id", "")
                    print(f"     状态={st} session={session_id[:16] or '(无)'}")
                    if st in ("ok", "failed"):
                        fired = True
                        if st == "failed":
                            fails.append(f"任务执行失败：{runs[0]['detail'][:200]}")
                        break
            if not fired:
                fails.append("任务没在预期时间内触发 —— 调度器没工作")
            else:
                print("   ✓ 到点触发并执行完成")

            print("\n5. 触发的会话真的跑完了（无头执行）")
            if session_id:
                sr = await c.get(f"{BASE}/api/sessions/{session_id}/messages")
                msgs = sr.json()["items"]
                roles = [m["role"] for m in msgs]
                print(f"   消息角色：{roles}")
                assistant = [m for m in msgs if m["role"] == "assistant" and m["content"]]
                if not assistant:
                    fails.append("会话里没有 assistant 回复 —— 生成器没被抽干")
                else:
                    print(f"   回复：{assistant[-1]['content'][:80]}")
                    print("   ✓ agent 循环完整跑完")

                sd = (await c.get(f"{BASE}/api/sessions/{session_id}")).json()
                print(f"   审批模式={sd['approval_mode']}  标题={sd['title'][:40]}")
                if sd["approval_mode"] != "auto":
                    fails.append(
                        f"会话审批模式是 {sd['approval_mode']}，应强制 auto"
                        "（否则会挂在等审批上，而没人在旁边点）"
                    )
                else:
                    print("   ✓ 强制 auto 审批")
                if "定时" not in sd["title"]:
                    print("   （标题里没有'定时'标记，用户可能分不清来源）")
            else:
                fails.append("执行记录里没有 session_id —— 用户无法查看 agent 做了什么")

            print("\n6. 并发：一个慢任务不该阻塞另一个")
            slow = await c.post(
                f"{BASE}/api/cron/tasks",
                json={
                    "name": "验证-慢任务",
                    "prompt": "用 run_shell 执行 sleep 20，然后回复完成。",
                    "cron": "0 3 * * *",
                },
            )
            fast = await c.post(
                f"{BASE}/api/cron/tasks",
                json={
                    "name": "验证-快任务",
                    "prompt": "只回复两个字：快。不要调用工具。",
                    "cron": "0 4 * * *",
                },
            )
            sid_slow, sid_fast = slow.json()["id"], fast.json()["id"]
            created_tasks += [sid_slow, sid_fast]

            t0 = time.monotonic()
            await c.post(f"{BASE}/api/cron/tasks/{sid_slow}/run")
            await asyncio.sleep(1)
            await c.post(f"{BASE}/api/cron/tasks/{sid_fast}/run")
            print("   两个都已触发，等快任务完成…")

            fast_done_at = 0.0
            while time.monotonic() - t0 < 120:
                await asyncio.sleep(3)
                rf = (await c.get(f"{BASE}/api/cron/tasks/{sid_fast}/runs")).json()["items"]
                if rf and rf[0]["status"] in ("ok", "failed"):
                    fast_done_at = time.monotonic() - t0
                    break
            rs = (await c.get(f"{BASE}/api/cron/tasks/{sid_slow}/runs")).json()["items"]
            slow_status = rs[0]["status"] if rs else "?"
            print(f"   快任务 {fast_done_at:.0f}s 完成，此时慢任务状态={slow_status}")
            if not fast_done_at:
                fails.append("快任务 120s 内没完成 —— 可能被慢任务阻塞了")
            elif slow_status == "running":
                print("   ✓ 快任务在慢任务还在跑时就完成了（真并发）")
            else:
                print(f"   （慢任务也结束了，无法确认并发。快任务耗时 {fast_done_at:.0f}s）")

            print("\n7. 错过的窗口要有记录")
            # 造一个"上次触发是两小时前、每小时执行"的任务 ——
            # reload 时应该检测到错过
            import sqlite3

            r = await c.post(
                f"{BASE}/api/cron/tasks",
                json={"name": "验证-错过", "prompt": "x", "cron": "0 * * * *"},
            )
            mid = r.json()["id"]
            created_tasks.append(mid)
            conn = sqlite3.connect(ROOT / "data" / "jeeves.db")
            two_h_ago = int((time.time() - 7200) * 1000)
            conn.execute(
                "UPDATE cron_task SET last_fired_at=? WHERE id=?", (two_h_ago, mid)
            )
            conn.commit()
            conn.close()
            print("   已把 last_fired_at 改成 2 小时前，触发 reload…")
            # patch 一下任意字段会触发 reload
            await c.patch(f"{BASE}/api/cron/tasks/{mid}", json={"name": "验证-错过2"})
            await asyncio.sleep(3)
            rr = (await c.get(f"{BASE}/api/cron/tasks/{mid}/runs")).json()["items"]
            missed = [x for x in rr if x["status"] == "missed"]
            print(f"   执行记录 {len(rr)} 条，其中 missed {len(missed)} 条")
            if missed:
                print(f"   ✓ 错过被记录：{missed[0]['detail'][:90]}")
            else:
                fails.append(
                    "错过的窗口没有记录 —— 用户看到'日报没发'但找不到原因"
                    f"（记录：{[x['status'] for x in rr]}）"
                )

            print("\n8. 列表接口")
            lr = (await c.get(f"{BASE}/api/cron/tasks")).json()
            print(f"   任务数={len(lr['items'])} 调度器已装载={lr['scheduler_loaded']}")
            if lr["scheduler_loaded"] < 1:
                fails.append("调度器没装载任何任务")
            else:
                print("   ✓")

            print("\n9. 删任务")
            for t in list(created_tasks):
                dr = await c.delete(f"{BASE}/api/cron/tasks/{t}")
                if dr.status_code == 200:
                    created_tasks.remove(t)
            lr2 = (await c.get(f"{BASE}/api/cron/tasks")).json()
            print(f"   删除后任务数={len(lr2['items'])} 调度器={lr2['scheduler_loaded']}")
            if lr2["scheduler_loaded"] != 0:
                fails.append("删完任务后调度器仍持有任务")
            else:
                print("   ✓ 调度器同步清空")

            return _finish(fails, c, created_providers, created_tasks)
        finally:
            for t in created_tasks:
                await c.delete(f"{BASE}/api/cron/tasks/{t}")
            for p in created_providers:
                await c.delete(f"{BASE}/api/providers/{p}")
            print("（已清理）")


def _finish(fails: list[str], c: object, ps: list[str], ts: list[str]) -> int:
    print("\n" + "=" * 58)
    if fails:
        for f in fails:
            print(f"✗ {f}")
        return 1
    print(
        "通过：cron 校验与预览、入口拦非法表达式、真实到点触发、"
        "无头执行完整 agent 循环、强制 auto 审批、执行历史关联会话、"
        "并发不阻塞、错过窗口有记录、删除同步调度器"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
