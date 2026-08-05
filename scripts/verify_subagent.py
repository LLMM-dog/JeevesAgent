"""
用真实模型验证 SubAgent 的核心命题：**委派真的能省父上下文吗**。

## 为什么这个必须实测

委派的唯一理由是省上下文。如果子代理的中间过程仍然进了父上下文，
或者结果原样回灌，那委派就只是增加了一层开销。

常见实现里两个在这点上是错的：`outputs` 是
`operator.add` 无限累加回灌父上下文，也无限制 ——
两者都让子代理失去了存在意义。

所以这个脚本比对的是**同一个任务**走两条路时父会话的 token：
  A. 主代理自己读一堆文件
  B. 派 researcher 去读，只拿结论

用法：
  1. 起后端
  2. uv run python scripts/verify_subagent.py
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


async def chat(c: httpx.AsyncClient, sid: str, content: str) -> dict[str, object]:
    buf = ""
    text: list[str] = []
    tools: list[dict[str, object]] = []
    agents: list[dict[str, object]] = []
    errors: list[str] = []
    usage: dict[str, object] = {}

    async with c.stream(
        "POST", f"{BASE}/api/chat", json={"session_id": sid, "content": content}
    ) as r:
        if r.status_code != 200:
            body = (await r.aread()).decode()
            return {"ok": False, "errors": [f"HTTP {r.status_code}: {body[:300]}"]}

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
                if not name or data is None:
                    continue

                if name == "message":
                    text.append(data["delta"])
                elif name == "tool_start":
                    # 用 agent_name 判断归属，不用 depth ——
                    # emit 读的是当前 span，工具执行时 agent span 已不在栈顶，
                    # tool_end 的 depth 会是 0。这个坑让最初的判定全错了。
                    who = data.get("agent_name") or ""
                    indent = "      " if who else "    "
                    args = data.get("args") or {}
                    brief = str(
                        args.get("agent") or args.get("path") or args.get("pattern") or ""
                    )[:50]
                    print(f"{indent}→ {data['tool_name']}({brief})")
                elif name == "tool_end":
                    tools.append(
                        {
                            "name": data["tool_name"],
                            "agent": data.get("agent_name") or "",
                            "is_error": data.get("is_error", False),
                            "display": data.get("display") or {},
                        }
                    )
                elif name == "agent_start":
                    if (data.get("depth") or 0) > 0:
                        agents.append({"name": data["agent_name"], "end": None})
                        print(f"    ┌─ 子智能体 {data['agent_name']} 启动")
                elif name == "agent_end":
                    if (data.get("depth") or 0) > 0:
                        tok = (data.get("prompt_tokens") or 0) + (
                            data.get("completion_tokens") or 0
                        )
                        for a in agents:
                            if a["name"] == data["agent_name"] and a["end"] is None:
                                a["end"] = {
                                    "stop_reason": data.get("stop_reason"),
                                    "turns": data.get("turns"),
                                    "tokens": tok,
                                }
                                break
                        print(
                            f"    └─ {data['agent_name']} 结束"
                            f"（{data.get('stop_reason')}，{data.get('turns')} 轮，{tok} tok）"
                        )
                elif name == "context_usage":
                    usage = data
                elif name == "error":
                    errors.append(str(data.get("message")))
                    print(f"    [错误] {str(data.get('message'))[:200]}")

    return {
        "ok": not errors,
        "reply": "".join(text),
        "tools": tools,
        "agents": agents,
        "errors": errors,
        "usage": usage,
    }


async def new_session(c: httpx.AsyncClient) -> str:
    sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
    await c.patch(f"{BASE}/api/sessions/{sid}", json={"approval_mode": "auto"})
    return sid


async def main() -> int:
    vals = load_env()
    async with httpx.AsyncClient(timeout=1800.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        print("1. 登记供应商")
        pr = await c.post(
            f"{BASE}/api/providers",
            json={
                "name": f"sub-test-{int(asyncio.get_running_loop().time())}",
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

            # 先准备一批文件让它去读，保证两条路的工作量可比
            print("\n2. 准备测试文件")
            sid_prep = await new_session(c)
            await chat(
                c,
                sid_prep,
                "在工作区建一个 corpus 目录，里面建 6 个 .md 文件："
                "a.md 到 f.md。每个文件写 40 行左右的内容，"
                "主题分别是：缓存策略、重试退避、连接池、日志分级、"
                "配置热加载、优雅停机。每个文件里明确写出一条'关键结论'。"
                "建完只回复'完成'。",
            )
            ls = await chat(c, sid_prep, "列出 corpus 目录下的文件，只回文件名。")
            print(f"   {str(ls['reply'])[:150]}")

            # ---- A：不委派，主代理自己读 ----
            print("\n3. 路线 A：主代理自己读全部 6 个文件")
            sid_a = await new_session(c)
            ra = await chat(
                c,
                sid_a,
                "读 corpus 目录下全部 6 个 md 文件，把每个文件里的'关键结论'提取出来，"
                "汇总成一个列表。不要用 subagent，你自己读。",
            )
            if not ra["ok"]:
                print(f"   失败：{ra['errors']}")
                return 1
            usage_a = ra["usage"] or {}
            tok_a = int(usage_a.get("used_tokens") or 0)  # type: ignore[union-attr]
            tools_a = [t for t in ra["tools"] if t["agent"] == ""]  # type: ignore[index,union-attr]
            print(f"   父上下文 used_tokens: {tok_a}")
            print(f"   父层工具调用: {len(tools_a)} 次")

            # ---- B：委派 researcher ----
            print("\n4. 路线 B：派 researcher 去读，只拿结论")
            sid_b = await new_session(c)
            rb = await chat(
                c,
                sid_b,
                "用 subagent 派 researcher 去读 corpus 目录下全部 6 个 md 文件，"
                "让它提取每个文件里的'关键结论'并汇总。"
                "把它的汇总结果告诉我。",
            )
            if not rb["ok"]:
                print(f"   失败：{rb['errors']}")
                return 1
            usage_b = rb["usage"] or {}
            tok_b = int(usage_b.get("used_tokens") or 0)  # type: ignore[union-attr]
            all_tools_b = rb["tools"]
            parent_tools_b = [t for t in all_tools_b if t["agent"] == ""]  # type: ignore[index,union-attr]
            child_tools_b = [t for t in all_tools_b if t["agent"] != ""]  # type: ignore[index,union-attr]
            print(f"   父上下文 used_tokens: {tok_b}")
            print(f"   父层工具调用: {len(parent_tools_b)} 次")
            print(f"   子层工具调用: {len(child_tools_b)} 次（这些不进父上下文）")

            agents_b = rb["agents"]
            if not agents_b:
                failures.append("没有派出子智能体 —— subagent 工具没被调用")
            else:
                print(f"   子智能体: {[a['name'] for a in agents_b]}")  # type: ignore[index]

            # ---- 核心判定 ----
            print("\n5. 判定")
            if child_tools_b:
                print(f"   ✓ 子智能体确实干了活（{len(child_tools_b)} 次工具调用）")
            else:
                failures.append("子智能体没有调用任何工具，可能根本没跑起来")

            if len(parent_tools_b) < len(tools_a):
                print(
                    f"   ✓ 父层工具调用减少：{len(tools_a)} → {len(parent_tools_b)}"
                )
            else:
                failures.append(
                    f"父层工具调用没减少（A={len(tools_a)}, B={len(parent_tools_b)}）"
                )

            if tok_a > 0 and tok_b > 0:
                saved = (tok_a - tok_b) / tok_a * 100
                print(f"   父上下文 token: {tok_a} → {tok_b}（{saved:+.0f}%）")
                if tok_b >= tok_a:
                    failures.append(
                        f"委派没有省下父上下文（{tok_a} → {tok_b}）—— "
                        "这是委派存在的唯一理由，没省就是白做"
                    )
                else:
                    print("   ✓ 委派省下了父上下文")

            # 结论必须真的传回来了
            reply_b = str(rb["reply"])
            if len(reply_b) < 50:
                failures.append("父代理没有把子智能体的结论传达出来")
            else:
                print(f"   父代理最终答复 {len(reply_b)} 字符")

            # ---- 递归防护 ----
            print("\n6. 递归防护：子智能体不该拿到 subagent 工具")
            sid_c = await new_session(c)
            rc = await chat(
                c,
                sid_c,
                "派 researcher 去做这件事：让它自己再派一个 researcher "
                "去读 corpus/a.md。如果它做不到，让它直接说明原因。",
            )
            nested = [t for t in rc["tools"] if t["agent"] != "" and t["name"] == "subagent"]  # type: ignore[index,union-attr]
            if nested:
                failures.append("子智能体成功调用了 subagent —— 递归防护失效")
            else:
                print("   ✓ 子智能体没有 subagent 工具，无法递归")

            # ---- 只读约束 ----
            print("\n7. 只读约束：researcher 不该能写文件")
            sid_d = await new_session(c)
            rd = await chat(
                c,
                sid_d,
                "派 researcher 去把 corpus/a.md 的内容改成'已被修改'。"
                "如果它做不到，让它说明原因。",
            )
            wrote = [
                t
                for t in rd["tools"]  # type: ignore[union-attr]
                if t["agent"] != ""
                and t["name"] in ("write_file", "edit_file", "run_shell", "run_python")
            ]
            if wrote:
                failures.append(
                    f"researcher 拿到了写入或执行工具：{[t['name'] for t in wrote]}"
                )
            else:
                print("   ✓ researcher 没有写入能力")

            # 清理
            await chat(
                c, sid_prep, "删掉 corpus 目录及里面所有文件。只回复'已清理'。"
            )

            print("\n" + "=" * 56)
            if failures:
                for f in failures:
                    print(f"✗ {f}")
                return 1
            print("通过：委派真的省了父上下文，递归与只读约束都生效")
            return 0
        finally:
            await c.delete(f"{BASE}/api/providers/{pid}")
            print("（已清理测试供应商）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
