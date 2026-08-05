"""
用真实模型验证执行工具闭环。

## 判定标准是"自修到跑通"，不是"调用了工具"

工具能被调用不代表可用。真正要验证的链路是：

    写文件 → 执行 → 拿到报错 → 读懂报错 → 改代码 → 再执行 → 通过

这条链路上任何一环坏了都会卡住，而且症状都是"模型在原地打转"：
  - 报错信息被截断在错误的位置 → 模型看不到 traceback，只能猜
  - 退出码被 shell 归一化 → 模型分不清"测试失败"和"命令没找到"
  - stdin 没关 → 命令挂死到超时
  - 超时只杀主进程 → 后台进程继续占端口，下次执行报"端口被占用"

## 审批模式设成 auto

审批逻辑本身有单元测试（test_approval.py）。这个脚本要测的是执行链路，
每一步都停下来等人点确认的话就没法自动跑。

用法：
  1. 起后端：uv run uvicorn app.main:app --port 9000 --app-dir backend
  2. uv run python scripts/verify_exec.py
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
    """发一轮对话，收集工具调用轨迹。"""
    buf = ""
    text_parts: list[str] = []
    tools: list[dict[str, object]] = []
    approvals = 0
    errors: list[str] = []

    async with c.stream(
        "POST", f"{BASE}/api/chat", json={"session_id": sid, "content": content}
    ) as r:
        if r.status_code != 200:
            body = (await r.aread()).decode()
            return {"ok": False, "error": f"HTTP {r.status_code}: {body[:300]}"}

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
                    brief = (
                        str(args.get("command") or args.get("code") or args.get("path") or "")
                    ).replace("\n", " ")[:90]
                    print(f"    → {data['tool_name']}({brief})")
                elif name == "tool_end":
                    tools.append(
                        {
                            "name": data["tool_name"],
                            "is_error": data.get("is_error", False),
                            "display": data.get("display") or {},
                            "preview": (data.get("content_preview") or "")[:200],
                        }
                    )
                    d = data.get("display") or {}
                    mark = "X" if data.get("is_error") else "OK"
                    extra = ""
                    if "exit_code" in d:
                        extra = f" exit={d['exit_code']}"
                        if d.get("timed_out"):
                            extra += " 超时"
                        if d.get("truncated"):
                            extra += f" 截断({d.get('total_lines')}行)"
                    print(f"    {mark} {data['tool_name']}{extra}")
                    if data.get("is_error"):
                        print(f"       {(data.get('content_preview') or '')[:160]}")
                elif name == "approval_required":
                    approvals += 1
                elif name == "error":
                    errors.append(f"{data.get('code')}: {data.get('message')}")
                    print(f"    [错误] {data.get('code')}: {str(data.get('message'))[:200]}")

    return {
        "ok": not errors,
        "reply": "".join(text_parts),
        "tools": tools,
        "approvals": approvals,
        "errors": errors,
    }


async def main() -> int:
    vals = load_env()
    base_url = vals["VERIFY_BASE_URL"]
    api_key = vals["VERIFY_API_KEY"]
    model_id = vals.get("VERIFY_MODEL") or "deepseek-v4-pro"

    async with httpx.AsyncClient(timeout=1200.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        print("1. 登记供应商")
        pr = await c.post(
            f"{BASE}/api/providers",
            json={
                "name": f"exec-test-{int(asyncio.get_running_loop().time())}",
                "base_url": base_url,
                "api_key": api_key,
                "models": [{"model_id": model_id, "context_window": 131072}],
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
            print("   已绑定 chat / title / compact")

            sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            # 审批逻辑有单元测试，这里要测执行链路，所以设成 auto
            await c.patch(f"{BASE}/api/sessions/{sid}", json={"approval_mode": "auto"})
            print(f"\n2. 建会话 {sid}（approval_mode=auto）")

            failures: list[str] = []

            print("\n3. 基础执行：Python 版本")
            r1 = await chat(
                c, sid, "用 run_shell 查一下当前 Python 版本，然后告诉我版本号。"
            )
            if not r1["ok"]:
                print(f"   失败：{r1['errors']}")
                return 1
            shell_calls = [t for t in r1["tools"] if t["name"] == "run_shell"]  # type: ignore[union-attr]
            if not shell_calls:
                failures.append("没有调用 run_shell")
            else:
                print(f"   调用了 {len(shell_calls)} 次 run_shell")

            print("\n4. 关键链路：写一个有 bug 的脚本，让它自己跑通")
            r2 = await chat(
                c,
                sid,
                "在工作区创建 calc.py，实现一个函数 divide(a, b) 返回 a/b，"
                "并在文件末尾加几行自测代码，其中要包含 divide(1, 0) 这个调用。"
                "写完后用 run_shell 执行它。如果报错，请修好代码再执行，"
                "直到能正常跑完为止（divide(1,0) 应该被妥善处理而不是崩溃）。",
            )
            if not r2["ok"]:
                print(f"   失败：{r2['errors']}")
                return 1

            names = [t["name"] for t in r2["tools"]]  # type: ignore[union-attr]
            print(f"\n   工具序列: {' → '.join(names)}")

            wrote = any(n == "write_file" for n in names)
            ran = [t for t in r2["tools"] if t["name"] in ("run_shell", "run_python")]  # type: ignore[union-attr]
            had_error = any(t["is_error"] for t in ran)
            last_ok = ran and not ran[-1]["is_error"]

            if not wrote:
                failures.append("没有创建文件")
            if not ran:
                failures.append("没有执行脚本")
            if not last_ok:
                failures.append("最后一次执行仍然失败 —— 没能自修到跑通")

            print(f"   创建文件: {'是' if wrote else '否'}")
            print(f"   执行次数: {len(ran)}")
            print(f"   中间出现过报错: {'是' if had_error else '否'}")
            print(f"   最终执行成功: {'是' if last_ok else '否'}")

            if had_error and last_ok:
                print("   ✓ 完整闭环：报错 → 读懂 → 修改 → 跑通")
            elif last_ok and not had_error:
                print("   ~ 一次就写对了（没走到自修路径）")

            print("\n4b. 强制自修：给一个必然报错的文件，看它能不能改到跑通")
            # 自己种一个有真实 bug 的文件。上一步模型可能一次就写对了，
            # 那样自修路径根本没被走到 —— 而那才是最关键的链路。
            #
            # 这个 bug 是刻意选的：TypeError 发生在运行时而不是语法层，
            # 模型必须【读懂 traceback】才能定位，不能靠扫一眼代码猜出来。
            ws_dir = ROOT / "workspace"
            ws_dir.mkdir(parents=True, exist_ok=True)
            broken = ws_dir / "broken.py"
            broken.write_text(
                "def total(items):\n"
                "    # bug: 应该累加 item['price']，这里直接加了 dict\n"
                "    s = 0\n"
                "    for it in items:\n"
                "        s += it\n"
                "    return s\n"
                "\n"
                "\n"
                "DATA = [{'price': 3}, {'price': 4}]\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    got = total(DATA)\n"
                "    assert got == 7, f'期望 7，实得 {got}'\n"
                "    print('OK', got)\n",
                encoding="utf-8",
            )
            r2b = await chat(
                c,
                sid,
                "工作区里有个 broken.py，用 run_shell 执行它。"
                "它会报错 —— 请根据报错信息修好它，然后重新执行，"
                "直到能打印出 OK 为止。不要改动 DATA 和那行 assert。",
            )
            if not r2b["ok"]:
                print(f"   失败：{r2b['errors']}")
                return 1

            b_names = [t["name"] for t in r2b["tools"]]  # type: ignore[union-attr]
            print(f"\n   工具序列: {' → '.join(b_names)}")
            b_ran = [
                t for t in r2b["tools"] if t["name"] in ("run_shell", "run_python")  # type: ignore[union-attr]
            ]
            b_had_error = any(t["is_error"] for t in b_ran)
            b_last_ok = b_ran and not b_ran[-1]["is_error"]
            fixed = broken.read_text(encoding="utf-8") if broken.exists() else ""

            print(f"   执行次数: {len(b_ran)}")
            print(f"   看到报错: {'是' if b_had_error else '否'}")
            print(f"   最终跑通: {'是' if b_last_ok else '否'}")

            if not b_had_error:
                failures.append("种了 bug 但第一次执行没报错 —— 报错没能传到模型")
            if not b_last_ok:
                failures.append("没能自修到跑通 —— 这是执行链路最关键的一环")
            if "OK" not in str(r2b["reply"]) and not b_last_ok:
                failures.append("模型没报告成功")
            # assert 那行不能被删掉 —— 那是"改到跑通"的判据
            if fixed and "assert got == 7" not in fixed:
                failures.append("模型删掉了 assert 而不是修 bug")
            if b_had_error and b_last_ok:
                print("   ✓ 完整闭环：执行 → 读 traceback → 定位 → 修改 → 跑通")

            print("\n4c. 大输出：截断后模型能不能拿到完整内容")
            r2c = await chat(
                c,
                sid,
                "用 run_shell 执行一条命令，打印 1 到 3000 每个数字各占一行。"
                "执行完后告诉我：输出有没有被截断？如果截断了，"
                "完整输出被保存到哪个文件？第 1 行的内容是什么？",
            )
            if r2c["ok"]:
                c_ran = [t for t in r2c["tools"] if t["name"] == "run_shell"]  # type: ignore[union-attr]
                truncated_any = any(
                    t["display"].get("truncated") for t in c_ran  # type: ignore[union-attr]
                )
                read_back = any(t["name"] == "read_file" for t in r2c["tools"])  # type: ignore[union-attr]
                print(f"   触发截断: {'是' if truncated_any else '否'}")
                print(f"   模型回读了完整输出文件: {'是' if read_back else '否'}")
                if truncated_any and not read_back:
                    print("   ~ 没回读。落盘路径给了但模型没用 —— 不算缺陷，但说明提示可以更明确")
                elif truncated_any and read_back:
                    print("   ✓ 截断 → 落盘 → 模型自己回读，链路通")

            print("\n5. 退出码保真：让它执行一个必然失败的命令")
            r3 = await chat(
                c,
                sid,
                "用 run_shell 执行 `python -c \"import sys; sys.exit(3)\"`，"
                "然后告诉我这条命令的退出码是多少。不要修它，只报告退出码。",
            )
            if r3["ok"]:
                codes = [
                    t["display"].get("exit_code")  # type: ignore[union-attr]
                    for t in r3["tools"]  # type: ignore[union-attr]
                    if t["name"] == "run_shell"
                ]
                print(f"   工具报告的退出码: {codes}")
                if 3 in codes:
                    print("   ✓ 退出码保真（PowerShell 会把非零退出归一成 1，已处理）")
                elif 1 in codes:
                    failures.append("退出码被归一成 1 —— exit $LASTEXITCODE 没生效")
                if "3" in str(r3["reply"]):
                    print("   ✓ 模型正确读出了退出码")

            print("\n6. 校验文件真的落地了")
            ws = ROOT / "workspace"
            calc = ws / "calc.py"
            if calc.exists():
                content = calc.read_text(encoding="utf-8")
                print(f"   calc.py 存在（{len(content)} 字符）")
                print("   " + "\n   ".join(content.splitlines()[:12]))
            else:
                failures.append("calc.py 没有落地")

            print("\n" + "=" * 56)
            if failures:
                for f in failures:
                    print(f"✗ {f}")
                return 1
            print("通过：执行链路完整，模型能写代码、跑代码、读报错、自修")
            return 0
        finally:
            await c.delete(f"{BASE}/api/providers/{pid}")
            print("（已清理测试供应商）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
