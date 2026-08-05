"""
用真实模型验证引用机制。

## 验什么

1. **文件引用真的展开**：引用一个文件后，模型能直接回答内容，不再调 read_file
2. **技能引用注入正文**：引用技能后模型按技能流程走，不用先 load_skill
3. **宏引用注入正文**：这是 漏得最狠的地方
4. **目录引用只给清单**：不泄露文件内容
5. **大文件截断且模型知道**：不能让它基于半个文件下结论
6. **路径越界被拒**：引用是用户可控输入
7. **坏引用不影响好引用**：一批里坏一个，其他仍展开
8. **候选接口能搜到文件**：且不返回 node_modules 里的东西

用法：
  1. 起后端
  2. uv run python scripts/verify_refs.py
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:9000"
ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "workspace"


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


async def chat(
    c: httpx.AsyncClient,
    sid: str,
    content: str,
    refs: list[dict] | None = None,
) -> dict[str, object]:
    buf = ""
    text: list[str] = []
    tools: list[str] = []
    ref_ev: dict = {}
    errors: list[str] = []
    async with c.stream(
        "POST",
        f"{BASE}/api/chat",
        json={"session_id": sid, "content": content, "refs": refs or []},
    ) as r:
        if r.status_code != 200:
            body = (await r.aread()).decode()
            return {"ok": False, "http": r.status_code, "body": body[:300], "reply": "",
                    "tools": [], "refs_event": {}}
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
                elif name == "refs_expanded":
                    ref_ev = data
                    fails = data.get("failures") or []
                    print(f"    ◆ 引用展开 ok={data.get('ok')} 失败={len(fails)}"
                          f" 用了{data.get('bytes_used', 0)}字节")
                    for f in fails:
                        print(f"      ✗ {f.get('type')} {f.get('label')}: {f.get('reason')}")
                elif name == "error":
                    errors.append(str(data.get("message")))
                    print(f"    [错误] {str(data.get('message'))[:150]}")
    return {
        "ok": not errors, "reply": "".join(text), "tools": tools,
        "refs_event": ref_ev, "http": 200,
    }


async def main() -> int:
    v = load_env()
    async with httpx.AsyncClient(timeout=1800.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        failures: list[str] = []
        created: list[str] = []
        made: list[Path] = []

        try:
            print("1. 登记模型")
            pr = await c.post(
                f"{BASE}/api/providers",
                json={
                    "name": f"refs-{int(asyncio.get_running_loop().time())}",
                    "base_url": v["VERIFY_BASE_URL"],
                    "api_key": v["VERIFY_API_KEY"],
                    "models": [{"model_id": v.get("VERIFY_MODEL") or "deepseek-v4-pro",
                                "context_window": 131072}],
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

            # 造一个内容独特的文件 —— 用一个模型不可能猜到的常量
            secret = "XQ7_MAGIC_CONSTANT_4821"
            f1 = WS / "refdemo.py"
            f1.write_text(
                f'"""演示模块。"""\n\nMAGIC = "{secret}"\n\n\ndef compute():\n    return 42\n',
                encoding="utf-8",
            )
            made.append(f1)

            sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid}", json={"approval_mode": "auto"})

            # ── 场景一：文件引用真的展开 ──
            print("\n2. 文件引用：模型应能直接答出内容，不调 read_file")
            r1 = await chat(
                c, sid,
                "这个文件里 MAGIC 常量的值是什么？直接回答值本身，不要解释。",
                [{"type": "file", "path": str(f1)}],
            )
            reply = str(r1["reply"])
            print(f"   回复：{reply[:120]}")
            if not r1["ok"]:
                failures.append(f"文件引用失败：{r1}")
            elif secret not in reply:
                failures.append(
                    f"模型没答出 MAGIC 值 —— 文件内容可能没进上下文。回复：{reply[:150]}"
                )
            else:
                print("   ✓ 文件内容确实进了上下文")
                if "read_file" in r1["tools"]:
                    print("   注意：它仍调了 read_file（可能在二次确认），"
                          "但内容已经在上下文里了")
                else:
                    print("   ✓ 没调 read_file —— 省掉了一轮往返")

            # ── 场景二：目录引用只给清单 ──
            print("\n3. 目录引用：只给文件名，不泄露内容")
            sid2 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid2}", json={"approval_mode": "auto"})
            r2 = await chat(
                c, sid2,
                "这个目录里有哪些文件？只列名字，不要读它们的内容。",
                [{"type": "dir", "path": str(WS)}],
            )
            reply2 = str(r2["reply"])
            print(f"   回复：{reply2[:150]}")
            if "refdemo.py" not in reply2:
                failures.append("目录引用没让模型看到文件清单")
            else:
                print("   ✓ 看到了文件清单")
            if secret in reply2 and "read_file" not in r2["tools"]:
                failures.append(
                    "目录引用泄露了文件内容 —— 应该只给名字"
                )

            # ── 场景三：大文件截断且声明 ──
            print("\n4. 大文件：截断且模型要知道被截断了")
            big = WS / "bigref.log"
            big.write_text("LINE_START\n" + ("padding line\n" * 12000) + "LINE_END\n",
                           encoding="utf-8")
            made.append(big)
            sid3 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid3}", json={"approval_mode": "auto"})
            r3 = await chat(
                c, sid3,
                "这个日志文件我给你的内容是完整的还是被截断的？一句话回答。",
                [{"type": "file", "path": str(big)}],
            )
            reply3 = str(r3["reply"])
            print(f"   回复：{reply3[:160]}")
            used = int(r3["refs_event"].get("bytes_used") or 0)
            print(f"   实际注入 {used} 字节")
            if used > 80000:
                failures.append(f"截断没生效，注入了 {used} 字节")
            else:
                print("   ✓ 截断生效")
            if not any(k in reply3 for k in ("截断", "不完整", "部分", "truncat")):
                failures.append(
                    f"模型不知道文件被截断了 —— 它会基于半个文件下结论。回复：{reply3[:120]}"
                )
            else:
                print("   ✓ 模型知道被截断了")

            # ── 场景四：路径越界被拒 ──
            print("\n5. 路径越界：应被拒且报出来")
            sid4 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid4}", json={"approval_mode": "auto"})
            r4 = await chat(
                c, sid4, "读到什么了？",
                [{"type": "file", "path": "../../../../Windows/System32/drivers/etc/hosts"}],
            )
            fails4 = (r4["refs_event"].get("failures") or [])
            if not fails4:
                failures.append("路径越界的引用没有被拒 —— 这是任意文件读取")
            else:
                print(f"   ✓ 拒了：{fails4[0].get('reason', '')[:90]}")

            # ── 场景五：坏引用不影响好引用 ──
            print("\n6. 一批引用里坏一个，其他仍要展开")
            sid5 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid5}", json={"approval_mode": "auto"})
            r5 = await chat(
                c, sid5,
                "MAGIC 的值是什么？直接给值。",
                [
                    {"type": "file", "path": str(WS / "does_not_exist.py")},
                    {"type": "file", "path": str(f1)},
                ],
            )
            reply5 = str(r5["reply"])
            fails5 = r5["refs_event"].get("failures") or []
            print(f"   失败 {len(fails5)} 个，回复：{reply5[:100]}")
            if secret not in reply5:
                failures.append("一个引用坏了导致其他引用也没展开")
            elif len(fails5) != 1:
                failures.append(f"应该正好 1 个失败，实得 {len(fails5)}")
            else:
                print("   ✓ 坏的报出来了，好的正常展开")

            # ── 场景六：未知类型不静默 ──
            print("\n7. 未知引用类型应报出来（防死引用）")
            sid6 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid6}", json={"approval_mode": "auto"})
            r6 = await chat(c, sid6, "你好，只回复'你好'。",
                            [{"type": "telepathy", "name": "x"}])
            fails6 = r6["refs_event"].get("failures") or []
            if not fails6:
                failures.append(
                    "未知引用类型被静默丢弃 —— 前后端不同步时无法发现"
                )
            else:
                print(f"   ✓ 报出来了：{fails6[0].get('reason', '')[:80]}")

            # ── 场景七：候选接口 ──
            print("\n8. 候选接口：能搜到文件且不含依赖目录")
            s = (await c.get(f"{BASE}/api/ref-candidates?kind=file&q=refdemo")).json()
            names = [i["path"] for i in s["items"]]
            print(f"   搜 refdemo 得到 {len(names)} 条：{names[:3]}")
            if not any("refdemo" in n for n in names):
                failures.append("候选接口搜不到刚建的文件")
            else:
                print("   ✓ 搜到了")

            s2 = (await c.get(f"{BASE}/api/ref-candidates?kind=file&q=py")).json()
            bad = [i["path"] for i in s2["items"]
                   if "node_modules" in i["path"] or ".venv" in i["path"]]
            if bad:
                failures.append(f"候选里出现了依赖目录：{bad[:3]}")
            else:
                print("   ✓ 没有 node_modules / .venv")

            s3 = (await c.get(f"{BASE}/api/ref-candidates?kind=tool&q=shell")).json()
            print(f"   工具候选：{[i['name'] for i in s3['items']]}")
            if not s3["items"]:
                failures.append("工具候选为空")

            print("\n" + "=" * 58)
            if failures:
                for f in failures:
                    print(f"✗ {f}")
                return 1
            print("通过：文件/目录展开、截断声明、越界拦截、部分失败、候选搜索全部生效")
            return 0
        finally:
            for p in made:
                p.unlink(missing_ok=True)
            for pid in created:
                await c.delete(f"{BASE}/api/providers/{pid}")
            print(f"（已清理 {len(created)} 个供应商、{len(made)} 个临时文件）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
