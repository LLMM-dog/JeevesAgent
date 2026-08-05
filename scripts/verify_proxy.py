"""
通过 Vite 代理走一遍真实对话，验证前端假设。

这个脚本【不进 pytest】——它需要前后端都在跑。

用法：
  1. 起后端：uv run uvicorn app.main:app --port 9000 --app-dir backend
  2. 起前端：cd frontend && npm run dev
  3. uv run python scripts/verify_proxy.py

它模拟浏览器的行为：走 5173 的代理、按 frontend/src/lib/sse.ts 的同一套
逻辑解析 SSE。能验证 pytest 测不到的东西 —— Vite 代理是否缓冲了 SSE。

代理缓冲是个隐蔽问题：功能全对，只是所有事件在生成结束后【一次性】到达，
流式效果完全消失。而这在后端测试里永远发现不了。
"""

import asyncio
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = "http://localhost:5173"
BACKEND = "http://127.0.0.1:9000"


async def check_alive(client: httpx.AsyncClient) -> bool:
    ok = True
    for name, url in (("后端", f"{BACKEND}/api/health"), ("前端", f"{FRONTEND}/")):
        try:
            r = await client.get(url, timeout=5.0)
            print(f"  {name} {url} → HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            print(f"  {name} {url} → 连不上（{type(e).__name__}）")
            ok = False
    return ok


async def main() -> int:
    # trust_env=False 与生产一致：绕开系统代理
    async with httpx.AsyncClient(timeout=330.0, trust_env=False) as c:
        print("1. 检查服务")
        if not await check_alive(c):
            print("\n请先启动前后端，见本文件顶部的用法说明")
            return 2

        print("\n2. 通过代理读元信息")
        meta = (await c.get(f"{FRONTEND}/api/meta")).json()
        print(f"   工具 {len(meta['tool_names'])} 个：{', '.join(meta['tool_names'])}")
        print(f"   has_chat_model={meta['has_chat_model']}")
        if not meta["has_chat_model"]:
            print(
                "\n   没有绑定对话模型，无法验证 SSE。\n"
                "   请先在设置页添加供应商，或用 scripts/verify_real.py"
            )
            return 1

        print("\n3. 通过代理建会话")
        sid = (await c.post(f"{FRONTEND}/api/sessions", json={})).json()["id"]
        print(f"   {sid}")

        print("\n4. 通过代理消费 SSE（关键：验证代理没有缓冲）")
        arrivals: list[tuple[float, str]] = []
        buffer = ""
        t0 = time.monotonic()

        async with c.stream(
            "POST",
            f"{FRONTEND}/api/chat",
            json={
                "session_id": sid,
                "content": "用 list_dir 看一下工作区，然后一句话说说看到了什么。",
            },
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                print(f"   失败 HTTP {resp.status_code}: {body.decode()[:300]}")
                return 1

            # 按 frontend/src/lib/sse.ts 的同一套逻辑：
            # 累积到空行才解析，处理事件跨 chunk
            async for raw in resp.aiter_bytes():
                buffer += raw.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    name = ""
                    data_lines: list[str] = []
                    for line in block.strip().split("\n"):
                        if line.startswith("event:"):
                            name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                    if not name or not data_lines:
                        continue
                    if len(data_lines) > 1:
                        print(f"   ✗ 事件 {name} 的 data 跨了多行，前端会解析失败")
                        return 1
                    payload = json.loads(data_lines[0])
                    arrivals.append((time.monotonic() - t0, name))

                    if name == "tool_start":
                        print(f"   → {payload['tool_name']}")
                    elif name == "tool_end":
                        flag = "失败" if payload["is_error"] else "成功"
                        print(f"   ← {payload['tool_name']} {flag} {payload['duration_ms']}ms")
                    elif name == "error":
                        print(f"   错误 [{payload['code']}] {payload['message']}")

        total = time.monotonic() - t0
        msg_times = [t for t, n in arrivals if n == "message"]
        print(f"\n   共 {len(arrivals)} 个事件，耗时 {total:.1f}s")

        # 核心判定：如果所有 message 事件都在最后 200ms 内到达，
        # 说明代理把整个流缓冲住了 —— 用户看不到逐字输出
        if len(msg_times) >= 3:
            span = msg_times[-1] - msg_times[0]
            print(
                f"   message 事件跨度 {span:.2f}s"
                f"（首个 {msg_times[0]:.2f}s，末个 {msg_times[-1]:.2f}s）"
            )
            if span < 0.2:
                print("   ✗ 所有增量几乎同时到达 —— 代理缓冲了 SSE，流式效果失效")
                return 1
            print("   ✓ 增量是逐步到达的，代理没有缓冲")
        else:
            print(f"   message 事件太少（{len(msg_times)} 个），无法判断缓冲")

        # 事件顺序
        names = [n for _, n in arrivals]
        if names[0] != "meta":
            print(f"   ✗ 首个事件应是 meta，实际是 {names[0]}")
            return 1
        if names[-1] != "done":
            print(f"   ✗ 末个事件应是 done，实际是 {names[-1]}")
            return 1
        print("   ✓ meta 在首、done 在末")

        print("\n5. 通过代理校验落库")
        msgs = (await c.get(f"{FRONTEND}/api/sessions/{sid}/messages")).json()["items"]
        print(f"   {len(msgs)} 条消息：{[m['role'] for m in msgs]}")

        declared = [t["id"] for m in msgs if m["tool_calls"] for t in m["tool_calls"]]
        answered = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
        missing = set(declared) - set(answered)
        if missing:
            print(f"   ✗ tool 配对缺失：{missing}")
            return 1
        print(f"   ✓ tool 配对完整（{len(declared)} 个）")

        print("\n" + "=" * 56)
        print("通过：前端代理下的完整链路正常")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
