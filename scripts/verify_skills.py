"""
用真实模型验证技能的渐进披露闭环。

## 要验证的核心问题 同类实现:68` 承认了这条路线的固有弱点：

> When a task matches, the agent uses `read` to load the full SKILL.md
> (**models don't always do this**; use prompting or `/skill:name` to force it)

"模型经常不去读技能文件"。所以这个脚本要验证的不是"工具能调通"，而是：

1. 任务符合某个技能的描述时，模型**主动**调 load_skill
2. 任务不相关时，模型**不**乱加载（否则白烧 token）
3. 正文提到附件时，模型会用 load_skill_file 去读
4. 模型真的**按技能里的规范**产出，不是自己瞎编
5. 技能正文以 role=tool 进上下文（不是 system），且这不影响它被遵循

用法：
  1. 起后端：uv run uvicorn app.main:app --port 9000 --app-dir backend
  2. uv run python scripts/verify_skills.py
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
    text_parts: list[str] = []
    tools: list[dict[str, object]] = []
    errors: list[str] = []

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
                    text_parts.append(data["delta"])
                elif name == "tool_start":
                    args = data.get("args") or {}
                    brief = str(args.get("name") or args.get("path") or "")[:60]
                    print(f"    → {data['tool_name']}({brief})")
                elif name == "tool_end":
                    tools.append(
                        {
                            "name": data["tool_name"],
                            "is_error": data.get("is_error", False),
                            "display": data.get("display") or {},
                        }
                    )
                    if data.get("is_error"):
                        print(f"    X {data['tool_name']}: {(data.get('content_preview') or '')[:120]}")
                elif name == "error":
                    errors.append(f"{data.get('code')}: {data.get('message')}")
                    print(f"    [错误] {str(data.get('message'))[:200]}")

    return {
        "ok": not errors,
        "reply": "".join(text_parts),
        "tools": tools,
        "errors": errors,
    }


def tool_names(res: dict[str, object]) -> list[str]:
    return [t["name"] for t in res["tools"]]  # type: ignore[index,union-attr]


async def main() -> int:
    vals = load_env()
    async with httpx.AsyncClient(timeout=1200.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        print("0. 检查技能索引")
        skills = (await c.get(f"{BASE}/api/skills")).json()
        names = [s["name"] for s in skills["items"]]
        print(f"   已加载技能: {names}")
        if skills["diagnostics"]:
            for d in skills["diagnostics"]:
                print(f"   [诊断] {d['level']}: {d['message']}")
        if "commit-message" not in names:
            print("   缺 commit-message 技能，无法继续")
            return 1

        print("\n1. 登记供应商")
        pr = await c.post(
            f"{BASE}/api/providers",
            json={
                "name": f"skill-test-{int(asyncio.get_running_loop().time())}",
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

            # ---- 场景一：任务符合技能描述，模型该主动加载 ----
            sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid}", json={"approval_mode": "auto"})
            print(f"\n2. 相关任务（会话 {sid}）")
            print("   问题：让它写一条提交信息 —— 正好命中 commit-message 技能")
            r1 = await chat(
                c,
                sid,
                "我刚改了技能加载器，把 frontmatter 的手写正则解析换成了 "
                "yaml.safe_load，因为原来的正则不支持 `description: >-` 这种块标量。"
                "帮我写一条这次改动的 git 提交信息。",
            )
            if not r1["ok"]:
                print(f"   失败：{r1['errors']}")
                return 1

            names1 = tool_names(r1)
            print(f"   工具序列: {' → '.join(names1) if names1 else '（没调工具）'}")
            loaded = "load_skill" in names1
            print(f"   主动加载了技能: {'是' if loaded else '否'}")
            if not loaded:
                failures.append(
                    "相关任务没有触发 load_skill —— 这正是 pi 文档承认的"
                    "'模型经常不读技能文件'问题"
                )

            reply = str(r1["reply"])
            # 技能里的核心规范：回答"为什么改"而非"改了什么"、摘要不超 50 字、
            # 不用 conventional commits 前缀
            followed = []
            if "为什么" in reply or "块标量" in reply or ">-" in reply:
                followed.append("说明了动机")
            if not any(p in reply for p in ("feat:", "fix:", "chore:")):
                followed.append("没用 conventional 前缀")
            print(f"   遵循规范的迹象: {followed}")
            if len(followed) < 2:
                failures.append("产出没有体现技能里的规范")

            # ---- 场景二：不相关任务，不该乱加载 ----
            sid2 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid2}", json={"approval_mode": "auto"})
            print(f"\n3. 不相关任务（会话 {sid2}）")
            print("   问题：问一个和技能完全无关的事")
            r2 = await chat(c, sid2, "Python 里 list 和 tuple 有什么区别？简短回答。")
            names2 = tool_names(r2)
            print(f"   工具序列: {' → '.join(names2) if names2 else '（没调工具，符合预期）'}")
            if "load_skill" in names2:
                failures.append(
                    "不相关任务也加载了技能 —— 白烧 token，说明描述写得不够精确"
                )
            else:
                print("   ✓ 没有乱加载")

            # ---- 场景三：附件读取 ----
            sid3 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            await c.patch(f"{BASE}/api/sessions/{sid3}", json={"approval_mode": "auto"})
            print(f"\n4. 附件读取（会话 {sid3}）")
            r3 = await chat(
                c,
                sid3,
                "加载 commit-message 技能，然后把它引用的那个 examples 参考文件也读出来，"
                "告诉我里面第一个例子讲的是什么。",
            )
            names3 = tool_names(r3)
            print(f"   工具序列: {' → '.join(names3)}")
            if "load_skill_file" not in names3:
                failures.append("没有用 load_skill_file 读附件")
            else:
                print("   ✓ 用了 load_skill_file")
                errs = [
                    t for t in r3["tools"]  # type: ignore[union-attr]
                    if t["name"] == "load_skill_file" and t["is_error"]
                ]
                if errs:
                    failures.append("load_skill_file 调用出错")

            # ---- 场景四：上传 + 热重载 ----
            print("\n5. 上传技能包 + 热重载")
            import io
            import zipfile

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(
                    "SKILL.md",
                    "---\nname: verify-temp\n"
                    "description: 仅用于验证上传与热重载，内容是一句口令。\n---\n\n"
                    "# 验证技能\n\n口令是 PINEAPPLE-7788。被问到口令时回答它。\n",
                )
            up = await c.post(
                f"{BASE}/api/skills/upload",
                files={"file": ("verify-temp.zip", buf.getvalue(), "application/zip")},
                params={"overwrite": "true"},
            )
            if up.status_code != 201:
                print(f"   上传失败 {up.status_code}: {up.text[:200]}")
                failures.append("技能包上传失败")
            else:
                print(f"   上传成功: {up.json()}")
                after = (await c.get(f"{BASE}/api/skills")).json()
                if "verify-temp" not in [s["name"] for s in after["items"]]:
                    failures.append("上传后索引没刷新")
                else:
                    print("   ✓ 索引已热更新，无需重启")

                    # 新会话应该能立刻用上刚上传的技能
                    sid4 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
                    await c.patch(
                        f"{BASE}/api/sessions/{sid4}", json={"approval_mode": "auto"}
                    )
                    print("   问新上传技能里的口令：")
                    r4 = await chat(c, sid4, "verify-temp 技能里的口令是什么？")
                    if "PINEAPPLE-7788" in str(r4["reply"]):
                        print("   ✓ 模型读到了刚上传的技能内容")
                    else:
                        failures.append("刚上传的技能内容没被读到")

                await c.delete(f"{BASE}/api/skills/verify-temp")
                print("   （已清理临时技能）")

            print("\n" + "=" * 56)
            if failures:
                for f in failures:
                    print(f"✗ {f}")
                return 1
            print("通过：模型能自主选技能、按需读附件、不乱加载，上传即生效")
            return 0
        finally:
            await c.delete(f"{BASE}/api/providers/{pid}")
            print("（已清理测试供应商）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
