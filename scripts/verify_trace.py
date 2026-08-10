"""
用真实模型验证 span 落库。

## 验什么

1. 一次真实对话产生的 span 树能不能重现执行过程
2. 子代理的 span 是不是挂在正确的父节点下
3. token / 成本有没有正确归集，子代理的有没有上卷到父 run
4. **密钥有没有被脱敏** —— 常见实现没做这件事
5. 追踪写入失败会不会影响对话（这条靠单测，这里只确认 dropped/failed 为 0）

用法：
  1. 起后端
  2. uv run python scripts/verify_trace.py
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
    """跑一轮，返回 run_id。"""
    buf = ""
    run_id = ""
    async with c.stream(
        "POST", f"{BASE}/api/chat", json={"session_id": sid, "content": content}
    ) as r:
        if r.status_code != 200:
            print(f"   HTTP {r.status_code}: {(await r.aread()).decode()[:200]}")
            return ""
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
                if name == "meta" and data.get("run_id"):
                    run_id = data["run_id"]
                elif name == "tool_start":
                    who = data.get("agent_name") or ""
                    print(f"    {'  ' if who else ''}→ {data['tool_name']}")
                elif name == "error":
                    print(f"    [错误] {str(data.get('message'))[:150]}")
    return run_id


def show_tree(spans: list[dict], indent: int = 0) -> int:
    n = 0
    for s in spans:
        n += 1
        pad = "  " * indent
        tok = f" {s['total_tokens']}tok" if s["total_tokens"] else ""
        cost = f" ${s['cost_usd']:.6f}" if s["cost_usd"] else ""
        dur = f" {s['duration_ms']}ms" if s["duration_ms"] is not None else ""
        flag = "" if s["status"] == "ok" else f" [{s['status']}]"
        trunc = " (截断)" if s["output_truncated"] else ""
        print(f"    {pad}{s['kind']}:{s['name']}{dur}{tok}{cost}{flag}{trunc}")
        n += show_tree(s["children"], indent + 1)
    return n


async def main() -> int:
    vals = load_env()
    async with httpx.AsyncClient(timeout=1800.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        print("1. 登记端点（带单价，好验成本计算）")
        pr = await c.post(
            f"{BASE}/api/providers",
            json={
                "name": f"trace-test-{int(asyncio.get_running_loop().time())}",
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

            # 直接写库设单价 —— 没有单价的话成本永远是 0，
            # compute_cost 那条路就没被验证过。
            import sqlite3

            conn = sqlite3.connect(str(ROOT / "data" / "jeeves.db"))
            conn.execute(
                "update model set price_in_per_1m=0.27, price_out_per_1m=1.1 where id=?",
                (pk,),
            )
            conn.commit()
            conn.close()
            print("   已设单价 in=0.27 out=1.1 USD/1M")
            for purpose in ("chat", "title", "compact"):
                await c.put(f"{BASE}/api/bindings", json={"purpose": purpose, "model_pk": pk})

            failures: list[str] = []

            # ---- 场景一：带工具调用的普通对话 ----
            sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid}", json={"approval_mode": "auto"})
            print(f"\n2. 普通对话（会话 {sid}）")
            run1 = await chat(
                c, sid, "列出工作区根目录的文件，然后告诉我有几个。"
            )
            if not run1:
                print("   没拿到 run_id")
                return 1

            await asyncio.sleep(1.5)  # 等异步写入落库

            t = (await c.get(f"{BASE}/api/traces/{run1}")).json()
            print(f"   run {run1}: {t['stop_reason']}, {t['turns']} 轮, "
                  f"{t['total_tokens']} tok")
            print("   span 树：")
            count = show_tree(t["spans"])
            print(f"   共 {count} 条 span")

            if count == 0:
                failures.append("span 树是空的 —— 落库没生效")
            kinds = {s["kind"] for s in t["spans"]}
            if "agent" not in kinds:
                failures.append(f"根 span 不是 agent 类型（实得 {kinds}）")
            # 至少要有 llm span
            flat: list[dict] = []

            def walk(xs: list[dict]) -> None:
                for x in xs:
                    flat.append(x)
                    walk(x["children"])

            walk(t["spans"])
            if not any(s["kind"] == "llm" for s in flat):
                failures.append("没有 llm span")
            else:
                print("   ✓ 有 llm span")
            if not any(s["kind"] == "tool" for s in flat):
                failures.append("没有 tool span")
            else:
                print("   ✓ 有 tool span")

            # 耗时必须实算，不能是 0
            zero_dur = [s for s in flat if s["duration_ms"] == 0 and s["kind"] == "llm"]
            if zero_dur:
                failures.append(f"{len(zero_dur)} 条 llm span 的耗时是 0")

            # ---- 场景二：子代理的 span 归属 ----
            sid2 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid2}", json={"approval_mode": "auto"})
            print(f"\n3. 委派场景（会话 {sid2}）")
            # 任务必须【真的值得委派】。
            #
            # 最初用的是"看看根目录有哪些文件"，模型自己做了没委派 ——
            # 而这完全符合预期：subagent 的工具描述里明确写了
            # "任务简单，你自己两三步就能做完 —— 委派本身有开销，杀鸡不用牛刀"。
            # 模型遵守了这条指引，是验证脚本的任务选错了。
            await chat(
                c,
                sid2,
                "在工作区建 tracecorpus 目录，里面建 5 个 md 文件 a.md~e.md，"
                "每个写 30 行内容，各自包含一条'关键结论'。建完只回'完成'。",
            )
            run2 = await chat(
                c,
                sid2,
                "用 subagent 派 researcher 去读 tracecorpus 下全部 5 个 md 文件，"
                "提取每个文件的'关键结论'并汇总给我。",
            )
            await asyncio.sleep(1.5)

            t2 = (await c.get(f"{BASE}/api/traces/{run2}")).json()
            print("   span 树：")
            show_tree(t2["spans"])
            flat2: list[dict] = []

            def walk2(xs: list[dict]) -> None:
                for x in xs:
                    flat2.append(x)
                    walk2(x["children"])

            walk2(t2["spans"])
            sub_spans = [s for s in flat2 if s["agent_name"] == "researcher"]
            if sub_spans:
                print(f"   ✓ 子代理产生了 {len(sub_spans)} 条 span")
                # 深度必须大于 0
                if all(s["depth"] > 0 for s in sub_spans):
                    print("   ✓ 子代理 span 的 depth > 0")
                else:
                    failures.append("子代理 span 的 depth 是 0")
            else:
                failures.append("子代理没有产生 span")

            # span_totals 是从 span 表汇总的真实用量，含子代理。
            # run.total_tokens 只有主 loop 的量 —— 子代理与父代理共享
            # run_id，所以 run 行上的 rollup 字段永远等于 total。
            st2 = t2["span_totals"]
            print(f"   run 主 loop {t2['total_tokens']} tok，"
                  f"span 汇总（含子代理）{st2['total_tokens']} tok")
            for a in st2["by_agent"]:
                print(f"     {a['agent_name']}: {a['total_tokens']} tok / "
                      f"{a['llm_calls']} 次 llm 调用")
            agents_seen = {a["agent_name"] for a in st2["by_agent"]}
            if "researcher" not in agents_seen:
                failures.append(
                    f"span 汇总里没有 researcher（实得 {agents_seen}）—— "
                    "无法回答'委派花了多少'"
                )
            elif st2["total_tokens"] <= t2["total_tokens"]:
                failures.append(
                    f"span 汇总 {st2['total_tokens']} 不大于主 loop "
                    f"{t2['total_tokens']}，子代理的量没算进来"
                )
            else:
                print("   ✓ 子代理的 token 被正确归集且可拆分")

            # ---- 场景三：脱敏 ----
            sid3 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid3}", json={"approval_mode": "auto"})
            print(f"\n4. 脱敏（会话 {sid3}）")
            fake_key = "sk-verify1234567890abcdef"
            run3 = await chat(
                c,
                sid3,
                f"把这段文本写进 workspace/keytest.txt："
                f"API_KEY={fake_key} 然后读回来确认。",
            )
            await asyncio.sleep(1.5)

            t3 = (await c.get(f"{BASE}/api/traces/{run3}")).json()
            blob = json.dumps(t3, ensure_ascii=False)
            if fake_key in blob:
                failures.append(
                    f"密钥 {fake_key} 出现在 span 数据里 —— 脱敏没生效！"
                    "追踪里泄露密钥是常见疏漏，不能犯"
                )
            else:
                print("   ✓ 密钥未出现在 span 里（脱敏生效）")
                if "sk-***" in blob or "***" in blob:
                    print("   ✓ 能看到脱敏标记，说明内容被记录但已遮蔽")

            # ---- 写入器健康度 ----
            print("\n5. 写入器状态")
            st = (await c.get(f"{BASE}/api/traces-stats")).json()
            print(f"   runs={st['runs']} spans={st['spans']} "
                  f"总 token={st['total_tokens']} 总成本=${st['total_cost_usd']}")
            if st["total_cost_usd"] <= 0:
                failures.append(
                    "总成本是 0 —— 单价配了却没算出成本，compute_cost 那条路有问题"
                )
            else:
                print(f"   ✓ 成本计算生效：${st['total_cost_usd']}")
            w = st.get("writer") or {}
            print(f"   写入 {w.get('written')} 丢弃 {w.get('dropped')} "
                  f"失败 {w.get('failed')}")
            if w.get("failed"):
                failures.append(f"有 {w['failed']} 条写入失败：{w.get('recent_errors')}")
            if w.get("dropped"):
                failures.append(f"有 {w['dropped']} 条被丢弃（队列满）")

            # ---- 列表接口 ----
            lst = (await c.get(f"{BASE}/api/traces?limit=10")).json()
            print(f"   列表接口返回 {len(lst['items'])} 条")
            if not lst["items"]:
                failures.append("列表接口是空的")

            # 清理测试产生的文件
            await chat(c, sid3, "删掉 workspace/keytest.txt，只回复'已删'。")
            await chat(c, sid2, "删掉 tracecorpus 目录及里面所有文件，只回'已清理'。")

            print("\n" + "=" * 56)
            if failures:
                for f in failures:
                    print(f"✗ {f}")
                return 1
            print("通过：span 树完整、子代理归属正确、脱敏生效、零写入失败")
            return 0
        finally:
            await c.delete(f"{BASE}/api/providers/{pid}")
            print("（已清理测试端点）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
