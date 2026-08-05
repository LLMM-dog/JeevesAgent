"""
真实模型的手动验证脚本。

不进 pytest —— 它需要真实 API Key 和网络，且要花钱。

用法（推荐）：
  1. 在项目根的 .env.verify 里填 VERIFY_BASE_URL / VERIFY_API_KEY
  2. uv run python scripts/verify_real.py --env

或直接传参：
  uv run python scripts/verify_real.py <base_url> <api_key> [模型名]

走完整链路：探测 → 建供应商 → 绑定 → 对话 → 工具调用 → 落库校验 → 第二轮。

【凭证处理】Key 只用于构造请求头。输出里只显示尾 4 位，
不打印完整值，不写进日志。
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

BASE = "http://127.0.0.1:9000/api"
VERIFY_ENV = ROOT / ".env.verify"


def load_env_creds() -> tuple[str, str, str | None]:
    """
    从 .env.verify 读凭证。只取需要的三个键，不回显内容。
    """
    if not VERIFY_ENV.exists():
        raise SystemExit(
            f"找不到 {VERIFY_ENV}\n"
            "请先复制一份并填入 VERIFY_BASE_URL / VERIFY_API_KEY"
        )
    values: dict[str, str] = {}
    for line in VERIFY_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in ("VERIFY_BASE_URL", "VERIFY_API_KEY", "VERIFY_MODEL"):
            values[k] = v.strip().strip("\"'")

    base_url = values.get("VERIFY_BASE_URL", "")
    api_key = values.get("VERIFY_API_KEY", "")
    model = values.get("VERIFY_MODEL", "") or None

    missing = [
        k
        for k, v in (("VERIFY_BASE_URL", base_url), ("VERIFY_API_KEY", api_key))
        if not v
    ]
    if missing:
        raise SystemExit(f"{VERIFY_ENV} 里这些项还是空的：{', '.join(missing)}")
    return base_url, api_key, model


async def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--env":
        base_url, api_key, want_model = load_env_creds()
        hint = api_key[-4:] if len(api_key) >= 4 else "****"
        print(f"凭证来自 {VERIFY_ENV.name}（Key 尾 4 位 {hint}）")
    elif len(sys.argv) >= 3:
        base_url, api_key = sys.argv[1], sys.argv[2]
        want_model = sys.argv[3] if len(sys.argv) > 3 else None
    else:
        print(__doc__)
        return 2

    # trust_env=False：与生产一致，绕开系统代理
    async with httpx.AsyncClient(timeout=330.0, trust_env=False) as c:
        print("=" * 60)
        print("1. 探测模型列表")
        r = await c.post(
            f"{BASE}/providers/probe", json={"base_url": base_url, "api_key": api_key}
        )
        if r.status_code != 200:
            print(f"   探测失败 HTTP {r.status_code}: {r.text[:400]}")
            return 1
        data = r.json()
        models = data["models"]
        print(f"   规范化后的地址: {data['normalized_base_url']}")
        print(f"   共 {len(models)} 个模型，前 8 个：")
        for m in models[:8]:
            tag = "（非对话）" if m["looks_non_chat"] else ""
            src = "匹配到" if m["window_source"] == "matched" else "默认值"
            print(f"     {m['model_id']:42} {m['context_window']:>9,} tokens [{src}]{tag}")

        chat_models = [m for m in models if not m["looks_non_chat"]]
        if not chat_models:
            print("   没有可用于对话的模型")
            return 1
        chosen = next(
            (m for m in chat_models if m["model_id"] == want_model), chat_models[0]
        )
        print(f"   选用: {chosen['model_id']}")

        # 统计窗口匹配率 —— 匹配率过低说明映射表需要补充
        matched = sum(1 for m in models if m["window_source"] == "matched")
        print(f"   窗口匹配率: {matched}/{len(models)}")

        print("\n2. 创建供应商（自动绑定 chat 位）")
        import time

        name = f"verify-{int(time.time())}"
        r = await c.post(
            f"{BASE}/providers",
            json={
                "name": name,
                "base_url": base_url,
                "api_key": api_key,
                "models": [
                    {"model_id": m["model_id"], "context_window": m["context_window"]}
                    for m in chat_models[:10]
                ],
            },
        )
        if r.status_code != 201:
            print(f"   失败 HTTP {r.status_code}: {r.text[:300]}")
            return 1
        prov = r.json()
        print(f"   provider={prov['id']}  key_hint={prov['key_hint']}  模型数={prov['model_count']}")
        assert "api_key" not in json.dumps(prov), "响应里出现了明文 Key！"
        print("   已确认响应中无明文 API Key")

        # 把 chat 位绑到我们选的模型
        ms = (await c.get(f"{BASE}/models", params={"provider_id": prov["id"]})).json()["items"]
        target = next((m for m in ms if m["model_id"] == chosen["model_id"]), ms[0])
        await c.put(f"{BASE}/bindings", json={"purpose": "chat", "model_pk": target["id"]})
        await c.put(f"{BASE}/bindings", json={"purpose": "title", "model_pk": target["id"]})
        print(f"   已绑定 chat / title → {target['model_id']}")

        print("\n3. 建会话并对话（流式）")
        sid = (await c.post(f"{BASE}/sessions", json={})).json()["id"]
        question = (
            "用 list_dir 看一下工作区根目录里有什么，"
            "然后用 write_file 在工作区新建 hello.py，内容是打印中文'你好'。"
            "最后简单说一下你做了什么。"
        )

        counts: dict[str, int] = {}
        text_parts: list[str] = []
        thinking_chars = 0
        run_id = ""
        t0 = time.time()

        async with c.stream(
            "POST", f"{BASE}/chat", json={"session_id": sid, "content": question}
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                print(f"   失败 HTTP {resp.status_code}: {body.decode()[:400]}")
                return 1
            event = ""
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    payload = json.loads(line[5:].strip())
                    counts[event] = counts.get(event, 0) + 1
                    if event == "meta":
                        run_id = payload["run_id"]
                        print(f"   run_id={run_id}")
                    elif event == "message":
                        text_parts.append(payload["delta"])
                    elif event == "thinking":
                        thinking_chars += len(payload.get("delta", ""))
                    elif event == "tool_start":
                        print(f"   → 调用 {payload['tool_name']}  {json.dumps(payload.get('args'), ensure_ascii=False)[:100]}")
                    elif event == "tool_end":
                        flag = "失败" if payload["is_error"] else "成功"
                        print(f"   ← {payload['tool_name']} {flag} {payload['duration_ms']}ms")
                    elif event == "context_usage":
                        print(f"   上下文 {payload['used_tokens']:,}/{payload['window_tokens']:,} ({payload['ratio']:.1%})")
                    elif event == "agent_end":
                        print(f"   结束原因={payload['stop_reason']} 轮次={payload['turns']}")
                    elif event == "title":
                        print(f"   标题：{payload['title']}")
                    elif event == "error":
                        print(f"   错误 [{payload['code']}] {payload['message']}")
                        if payload.get("hint"):
                            print(f"        {payload['hint']}")

        elapsed = time.time() - t0
        print(f"\n   耗时 {elapsed:.1f}s")
        print(f"   事件统计: {json.dumps(counts, ensure_ascii=False)}")
        if thinking_chars:
            print(f"   思维链 {thinking_chars} 字符")
        print(f"\n   回复：\n{''.join(text_parts)[:600]}")

        print("\n4. 校验落库")
        msgs = (await c.get(f"{BASE}/sessions/{sid}/messages")).json()["items"]
        print(f"   共 {len(msgs)} 条消息：")
        for m in msgs:
            extra = ""
            if m["tool_calls"]:
                extra = f" → {[t['name'] for t in m['tool_calls']]}"
            if m["role"] == "tool":
                extra = f" ({m['tool_name']}{'，错误' if m['is_error'] else ''})"
            print(f"     seq={m['seq']} {m['role']}{extra}  {len(m['content'])} 字符")

        # 关键校验：tool 配对完整（否则下一轮会 400）
        declared: list[str] = []
        answered: list[str] = []
        for m in msgs:
            if m["tool_calls"]:
                declared += [t["id"] for t in m["tool_calls"]]
            if m["role"] == "tool":
                answered.append(m["tool_call_id"])
        missing = set(declared) - set(answered)
        print(f"   tool 配对: 声明 {len(declared)} 个，应答 {len(answered)} 个"
              f"{'  ✗ 缺失 ' + str(missing) if missing else '  ✓ 完整'}")

        # 文件真的写出来了吗
        target_file = ROOT / "workspace" / "hello.py"
        print(f"   hello.py 存在: {target_file.exists()}")
        if target_file.exists():
            print(f"   内容：{target_file.read_text(encoding='utf-8')[:200]}")

        print("\n5. 第二轮（验证上下文延续 + 不会因配对问题 400）")
        parts2: list[str] = []
        err2 = None
        async with c.stream(
            "POST", f"{BASE}/chat", json={"session_id": sid, "content": "刚才你创建的文件叫什么名字？只回文件名。"}
        ) as resp:
            event = ""
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    p = json.loads(line[5:].strip())
                    if event == "message":
                        parts2.append(p["delta"])
                    elif event == "error":
                        err2 = f"[{p['code']}] {p['message']}"
        if err2:
            print(f"   第二轮报错：{err2}")
            return 1
        print(f"   回复：{''.join(parts2)[:200]}")

        print("\n" + "=" * 60)
        print("全部通过")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
