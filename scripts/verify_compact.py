"""
用真实模型验证压缩。

## 怎么让压缩必然触发

不改代码、不 mock —— 把模型的 `context_window` 登记成一个很小的值
（比如 6000）。压缩阈值是 `window * 0.75`，几轮对话就会越过去。

这也顺带验证了一件事：**窗口值填错时压缩机制能不能兜住**。真实场景里
用户手填窗口、或者探测回落到默认值，都可能比实际值小得多。

## 判定标准

压缩后模型必须仍然记得【早期约定的具体细节】。摘要写漏的话，
模型会答不出来或者答错 —— 而这不会有任何报错。

用法：
  1. 起后端：uv run uvicorn app.main:app --port 9000 --app-dir backend
  2. uv run python scripts/verify_compact.py
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:9000"
ROOT = Path(__file__).resolve().parent.parent

# 故意登记一个很小的窗口，让压缩必然触发
SMALL_WINDOW = 4200

# 早期约定的细节。压缩后要能答出来 —— 这是判定摘要质量的唯一标准。
SECRET_RULES = {
    "变量名": "jeeves_magic_7788",
    "端口": "49173",
    "禁止": "不要用 requests 库",
}


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
    c: httpx.AsyncClient, sid: str, content: str, *, label: str
) -> dict[str, object]:
    """发一轮对话，返回统计。"""
    buf = ""
    text_parts: list[str] = []
    stats: dict[str, int] = {}
    compact_info: dict[str, object] | None = None

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
                stats[name] = stats.get(name, 0) + 1
                if name == "message":
                    text_parts.append(data["delta"])
                elif name == "compacting":
                    print(f"    [压缩开始] 待压缩 {data['victim_count']} 条")
                elif name == "compacted":
                    compact_info = data
                    saved = (
                        100 - int(data["after_tokens"] / data["before_tokens"] * 100)
                        if data["before_tokens"]
                        else 0
                    )
                    print(
                        f"    [压缩完成] {data['before_tokens']} → {data['after_tokens']} "
                        f"tokens（省 {saved}%），摘要 {data['summary_chars']} 字"
                    )
                elif name == "context_usage":
                    ratio = data["ratio"]
                    if ratio > 0.6:
                        print(
                            f"    [上下文] {data['used_tokens']}/{data['window_tokens']} "
                            f"({ratio:.0%}){'（估算）' if data['is_estimate'] else ''}"
                        )
                elif name == "error":
                    print(f"    [错误] {data['code']}: {data['message'][:200]}")
                    return {"ok": False, "error": data["message"]}

    reply = "".join(text_parts)
    print(f"  {label} → {reply[:120]}")
    return {"ok": True, "reply": reply, "stats": stats, "compacted": compact_info}


async def main() -> int:
    vals = load_env()
    base_url = vals["VERIFY_BASE_URL"]
    api_key = vals["VERIFY_API_KEY"]
    model_id = vals.get("VERIFY_MODEL") or "deepseek-v4-pro"

    async with httpx.AsyncClient(timeout=900.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        print(f"1. 登记端点（窗口故意设成 {SMALL_WINDOW}，让压缩必然触发）")
        pr = await c.post(
            f"{BASE}/api/providers",
            json={
                "name": f"compact-test-{int(asyncio.get_running_loop().time())}",
                "base_url": base_url,
                "api_key": api_key,
                "models": [{"model_id": model_id, "context_window": SMALL_WINDOW}],
            },
        )
        if pr.status_code != 201:
            print(f"   失败 {pr.status_code}: {pr.text[:300]}")
            return 1
        pid = pr.json()["id"]
        print(f"   provider={pid}")

        try:
            models = (await c.get(f"{BASE}/api/models?provider_id={pid}")).json()["items"]
            pk = models[0]["id"]
            for purpose in ("chat", "title", "compact"):
                await c.put(
                    f"{BASE}/api/bindings", json={"purpose": purpose, "model_pk": pk}
                )
            print("   已绑定 chat / title / compact")

            sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            print(f"\n2. 建会话 {sid}")

            print("\n3. 第一轮：约定几个必须记住的细节")
            r = await chat(
                c,
                sid,
                "我们约定几条规则，请简单确认：\n"
                f"1) 项目里的魔法变量必须叫 {SECRET_RULES['变量名']}\n"
                f"2) 服务端口固定用 {SECRET_RULES['端口']}\n"
                f"3) {SECRET_RULES['禁止']}\n"
                "只回复'已记录'加一句话复述即可，不要调用任何工具。",
                label="确认",
            )
            if not r["ok"]:
                print(f"   失败：{r['error']}")
                return 1

            print("\n4. 灌几轮无关对话，把上下文推过阈值")
            fillers = [
                "详细说说 Python 的列表和元组有什么区别，500 字以上，多举例子。",
                "详细说说 async 和多线程的区别，500 字以上，多举例子。",
                "详细说说 SQLite 的 WAL 模式是什么，500 字以上，多举例子。",
                "详细说说 HTTP 长连接的作用，500 字以上，多举例子。",
                "详细说说什么是幂等性，500 字以上，多举例子。",
            ]
            compacted_at: int | None = None
            for i, q in enumerate(fillers, 1):
                print(f"  第 {i} 轮填充…")
                rr = await chat(c, sid, q + "（不要调用工具）", label=f"填充{i}")
                if not rr["ok"]:
                    print(f"   失败：{rr['error']}")
                    return 1
                if rr["compacted"] and compacted_at is None:
                    compacted_at = i

            if compacted_at is None:
                print("\n   ✗ 一直没触发压缩。窗口可能还是设大了")
                return 1
            print(f"\n   压缩发生在第 {compacted_at} 轮填充")

            print("\n5. 关键验证：压缩后还记不记得早期约定")
            probe = await chat(
                c,
                sid,
                "回答三个问题，每个一行，不要调用工具：\n"
                "1) 我们约定的魔法变量叫什么？\n"
                "2) 约定的服务端口是多少？\n"
                "3) 我要求不要用哪个库？",
                label="回忆",
            )
            if not probe["ok"]:
                print(f"   失败：{probe['error']}")
                return 1

            reply = str(probe["reply"])
            print()
            missed: list[str] = []
            for label, value in SECRET_RULES.items():
                hit = value.replace("不要用 ", "").replace(" 库", "") in reply
                print(f"   {label}: {'✓ 记得' if hit else '✗ 忘了'}（{value}）")
                if not hit:
                    missed.append(label)

            print("\n6. 校验落库与摘要质量")
            msgs = (await c.get(f"{BASE}/api/sessions/{sid}/messages")).json()["items"]
            roles = [m["role"] for m in msgs]
            summaries = [m for m in msgs if m["role"] == "summary"]
            print(f"   {len(msgs)} 条消息，其中 summary {len(summaries)} 条")
            print(f"   角色序列: {' '.join(roles)}")

            summary_ok = True
            if not summaries:
                print("   ✗ 没有 summary 落库")
                summary_ok = False
            for s in summaries:
                text = s["content"]
                head = text[:200].replace("\n", " ")
                print(f"\n   摘要（{len(text)} 字）: {head}…")

                # 摘要太短一定是坏的。实测踩过：占位符没被替换，模型回复
                # "无对话历史内容"，17 个字符就把 9662 token 的历史换掉了。
                if len(text) < 100:
                    print(f"   ✗ 摘要只有 {len(text)} 字，不可能保住六类信息")
                    summary_ok = False
                    continue

                # 关键：摘要里必须能找到那些约定。
                # 模型答对不代表摘要好 —— 约定可能还留在 tail 里。
                kept = [
                    label
                    for label, v in SECRET_RULES.items()
                    if v.replace("不要用 ", "").replace(" 库", "") in text
                ]
                print(f"   摘要里保留了 {len(kept)}/{len(SECRET_RULES)} 条约定：{kept}")
                if len(kept) < len(SECRET_RULES):
                    lost = set(SECRET_RULES) - set(kept)
                    print(f"   ✗ 摘要漏掉了：{lost}")
                    summary_ok = False

            print("\n" + "=" * 56)
            if missed:
                print(f"压缩后遗忘了：{', '.join(missed)}")
                return 1
            if not summary_ok:
                print("模型答对了，但【摘要本身有问题】。")
                print("答对可能只是因为约定还留在 tail 里 —— 会话再长就会丢。")
                return 1
            print("通过：压缩后仍记得全部早期约定，且摘要本身保住了这些信息")
            return 0
        finally:
            await c.delete(f"{BASE}/api/providers/{pid}")
            print("（已清理测试端点）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
