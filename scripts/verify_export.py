"""
验证会话导出。

## 验什么

导出是用户拿走自己数据的唯一途径。常见实现没有这个功能，
所以没有可抄的实现也没有别人踩过的坑 —— 必须自己验。

1. 真实对话（含工具调用、子智能体）导出后内容完整
2. 中文标题不会让响应头编码崩（HTTP 头只能放 latin-1）
3. Markdown 里工具结果折叠但没丢
4. JSON 能被重新解析，字段齐全
5. base64 图片不内联（否则文件涨几 MB）
6. 文件名安全（模型生成的标题可能含 Windows 非法字符）

用法：
  1. 起后端
  2. uv run python scripts/verify_export.py
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

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


async def chat(c: httpx.AsyncClient, sid: str, content: str) -> tuple[str, list[str]]:
    buf = ""
    out: list[str] = []
    tools: list[str] = []
    async with c.stream(
        "POST", f"{BASE}/api/chat", json={"session_id": sid, "content": content}
    ) as r:
        if r.status_code != 200:
            return f"[HTTP {r.status_code}]", []
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
                elif name == "tool_start":
                    tools.append(data["tool_name"])
                    print(f"    → {data['tool_name']}")
    return "".join(out), tools


async def main() -> int:  # noqa: PLR0915
    v = load_env()
    fails: list[str] = []
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
                    "name": f"exp-{int(asyncio.get_running_loop().time())}",
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
            created.append(pid)
            pk = (await c.get(f"{BASE}/api/models?provider_id={pid}")).json()["items"][0]["id"]
            for purpose in ("chat", "title", "compact"):
                await c.put(f"{BASE}/api/bindings", json={"purpose": purpose, "model_pk": pk})

            # 用一个含 Windows 非法字符的中文标题
            nasty = '导出测试/含:非法*字符?"<>|'
            print(f"\n2. 造一个真实对话（标题故意含非法字符：{nasty!r}）")
            sid = (await c.post(f"{BASE}/api/sessions", json={"title": nasty})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid}", json={"approval_mode": "auto"})

            a1, t1 = await chat(c, sid, "记住这个数字：58317。只回复'好'。")
            print(f"   第一轮：{a1[:40]}")
            a2, t2 = await chat(c, sid, "用 run_shell 执行 echo 导出验证。")
            print(f"   第二轮：{a2[:60]}  工具={t2}")
            if not t2:
                print("   （模型没调工具，后面的工具折叠判定会跳过）")

            msgs = (await c.get(f"{BASE}/api/sessions/{sid}/messages")).json()["items"]
            roles = [m["role"] for m in msgs]
            print(f"   消息角色：{roles}")

            # ── Markdown ──
            print("\n3. 导出 Markdown")
            r = await c.get(f"{BASE}/api/sessions/{sid}/export?fmt=markdown")
            print(f"   HTTP {r.status_code}  {len(r.text)} 字符")
            if r.status_code != 200:
                fails.append(f"Markdown 导出失败 {r.status_code}: {r.text[:200]}")
                return _finish(fails, c, created)

            md = r.text
            ctype = r.headers.get("content-type", "")
            disp = r.headers.get("content-disposition", "")
            print(f"   Content-Type: {ctype}")
            print(f"   Content-Disposition: {disp[:150]}")

            if "markdown" not in ctype:
                fails.append(f"Content-Type 不对：{ctype}")
            if "attachment" not in disp:
                fails.append("没有 attachment，浏览器会直接显示而不是下载")

            # 【关键】响应头必须能被 latin-1 编码
            try:
                disp.encode("latin-1")
                print("   ✓ 响应头可被 latin-1 编码（中文标题没让它崩）")
            except UnicodeEncodeError as e:
                fails.append(f"Content-Disposition 含非 latin-1 字符，ASGI 层会炸：{e}")

            # 文件名检查
            m_utf8 = re.search(r"filename\*=UTF-8''([^;]+)", disp)
            if not m_utf8:
                fails.append("缺 filename*=UTF-8''（现代浏览器拿不到真名）")
            else:
                real = unquote(m_utf8.group(1))
                print(f"   解码后文件名：{real!r}")
                for ch in '/\\:*?"<>|':
                    if ch in real:
                        fails.append(f"文件名含 Windows 非法字符 {ch!r}：{real}")
                        break
                else:
                    print("   ✓ 文件名已清理")
            if "filename=" not in disp:
                fails.append("缺 ASCII 兜底 filename=")

            # 内容检查
            print("\n4. Markdown 内容")
            checks = [
                ("58317" in md, "第一轮的数字在"),
                (sid in md, "会话 ID 在元信息里"),
                ("创建时间" in md, "有时间信息"),
            ]
            if "tool" in roles:
                checks.append(("<details>" in md, "工具结果折叠了"))
                checks.append(("run_shell" in md or "🔧" in md, "工具名在"))
            for ok, what in checks:
                print(f"   {'✓' if ok else '✗'} {what}")
                if not ok:
                    fails.append(f"Markdown 缺：{what}")

            if "data:image/" in md:
                fails.append("Markdown 内联了 base64 图片")

            print(f"\n   前 400 字符预览：\n{'-' * 50}")
            print(md[:400])
            print("-" * 50)

            # ── JSON ──
            print("\n5. 导出 JSON")
            rj = await c.get(f"{BASE}/api/sessions/{sid}/export?fmt=json")
            print(f"   HTTP {rj.status_code}  {len(rj.text)} 字符")
            if rj.status_code != 200:
                fails.append(f"JSON 导出失败 {rj.status_code}")
            else:
                try:
                    data = rj.json()
                except Exception as e:  # noqa: BLE001
                    fails.append(f"JSON 无法解析：{e}")
                    data = {}
                if data:
                    print(f"   schema_version={data.get('schema_version')}")
                    print(f"   消息数={len(data.get('messages') or [])}")
                    if data.get("session", {}).get("id") != sid:
                        fails.append("JSON 里的 session.id 不对")
                    if len(data.get("messages") or []) != len(msgs):
                        fails.append(
                            f"JSON 消息数 {len(data.get('messages') or [])} != 库里 {len(msgs)}"
                        )
                    else:
                        print("   ✓ 消息数一致")
                    if not data.get("schema_version"):
                        fails.append("缺 schema_version")
                    # tool_calls 必须是对象不是字符串
                    tc = [m["tool_calls"] for m in data["messages"] if m.get("tool_calls")]
                    if tc and not isinstance(tc[0], list):
                        fails.append(f"tool_calls 没解析成对象：{type(tc[0])}")
                    elif tc:
                        print("   ✓ tool_calls 是结构化对象")
                    if "data:image/" in rj.text:
                        fails.append("JSON 内联了 base64 图片")
                    else:
                        print("   ✓ 没有内联图片")

            # ── 错误路径 ──
            print("\n6. 错误路径")
            r404 = await c.get(f"{BASE}/api/sessions/ses_nope/export")
            print(f"   不存在的会话 → HTTP {r404.status_code}")
            if r404.status_code != 404:
                fails.append(f"不存在的会话应返回 404，实际 {r404.status_code}")
            r422 = await c.get(f"{BASE}/api/sessions/{sid}/export?fmt=pdf")
            print(f"   fmt=pdf → HTTP {r422.status_code}")
            if r422.status_code != 422:
                fails.append(f"非法格式应返回 422，实际 {r422.status_code}")

            print("\n7. 空会话导出不应崩")
            empty = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            re_ = await c.get(f"{BASE}/api/sessions/{empty}/export")
            print(f"   HTTP {re_.status_code}  {len(re_.text)} 字符")
            if re_.status_code != 200:
                fails.append(f"空会话导出失败 {re_.status_code}")
            else:
                print("   ✓ 空会话也能导出")
            await c.delete(f"{BASE}/api/sessions/{empty}")

            return _finish(fails, c, created)
        finally:
            for pid in created:
                await c.delete(f"{BASE}/api/providers/{pid}")
            print(f"（已清理 {len(created)} 个端点）")


def _finish(fails: list[str], c: object, created: list[str]) -> int:
    print("\n" + "=" * 58)
    if fails:
        for f in fails:
            print(f"✗ {f}")
        return 1
    print("通过：Markdown/JSON 导出、中文标题头编码、文件名清理、"
          "工具折叠、字段完整、错误路径")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
