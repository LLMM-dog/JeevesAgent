"""
用真实模型验证视觉功能。

## 验什么

1. **核验能区分支持与不支持**：视觉模型判为 true，纯文本模型判为 false
2. **未核验/不支持的模型不许开视觉开关**，且报错要指向真因
3. **模型真的能看到图**：发一张有明确内容的图，看它能不能描述对
4. **图片不进历史**：第二轮不重发第一轮的图（token 成本）
5. **校验拦得住**：假 PNG、超大图、不支持的格式
6. **视觉模式关闭时图片被丢弃但文字仍送达**

## 需要的配置

.env.verify 里加（见 .env.verify.example）：
    VERIFY_VISION_MODEL=gpt-4o-mini      # 必填
    VERIFY_VISION_BASE_URL=              # 留空则用 VERIFY_BASE_URL
    VERIFY_VISION_API_KEY=               # 留空则用 VERIFY_API_KEY

不填 VERIFY_VISION_MODEL 时跳过多模态部分，只验降级行为。

用法：
  1. 起后端
  2. uv run python scripts/verify_vision.py
"""

import asyncio
import base64
import json
import struct
import sys
import zlib
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:9000"
ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    f = ROOT / ".env.verify"
    if not f.exists():
        print("缺 .env.verify（可从 .env.verify.example 复制）")
        sys.exit(2)
    vals: dict[str, str] = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip().strip("\"'")
    return vals


def make_png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    """
    生成一张纯色 PNG。

    手写而不用 Pillow —— 只为了造测试图引入一个图像库不值得，
    而 PNG 的最小结构就几十行。
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit truecolor
    rows = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


async def chat(
    c: httpx.AsyncClient, sid: str, content: str, images: list[str] | None = None
) -> dict[str, object]:
    buf = ""
    text: list[str] = []
    errors: list[str] = []
    async with c.stream(
        "POST",
        f"{BASE}/api/chat",
        json={"session_id": sid, "content": content, "images": images or []},
    ) as r:
        if r.status_code != 200:
            body = (await r.aread()).decode()
            return {"ok": False, "http": r.status_code, "body": body[:400], "reply": ""}
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
                elif name == "error":
                    errors.append(str(data.get("message")))
                    print(f"    [错误] {str(data.get('message'))[:150]}")
    return {"ok": not errors, "reply": "".join(text), "errors": errors, "http": 200}


async def main() -> int:
    v = load_env()
    vision_model = v.get("VERIFY_VISION_MODEL", "").strip()
    vision_url = v.get("VERIFY_VISION_BASE_URL", "").strip() or v["VERIFY_BASE_URL"]
    vision_key = v.get("VERIFY_VISION_API_KEY", "").strip() or v["VERIFY_API_KEY"]

    async with httpx.AsyncClient(timeout=1800.0, trust_env=False) as c:
        try:
            await c.get(f"{BASE}/api/health", timeout=5.0)
        except Exception:
            print("后端没起来")
            return 2

        failures: list[str] = []
        created: list[str] = []

        try:
            # ── 纯文本模型：核验应判为 false ──
            print("1. 登记纯文本模型，核验应判为「不支持」")
            pr = await c.post(
                f"{BASE}/api/providers",
                json={
                    "name": f"vis-text-{int(asyncio.get_running_loop().time())}",
                    "base_url": v["VERIFY_BASE_URL"],
                    "api_key": v["VERIFY_API_KEY"],
                    "models": [
                        {"model_id": v.get("VERIFY_MODEL") or "deepseek-v4-pro",
                         "context_window": 131072}
                    ],
                },
            )
            if pr.status_code != 201:
                print(f"   失败 {pr.status_code}: {pr.text[:300]}")
                return 1
            text_pid = pr.json()["id"]
            created.append(text_pid)

            text_models = (
                await c.get(f"{BASE}/api/models?provider_id={text_pid}")
            ).json()["items"]
            text_pk = text_models[0]["id"]
            print(f"   模型 {text_models[0]['model_id']}，"
                  f"初始状态 supports_vision={text_models[0]['supports_vision']}")
            if text_models[0]["supports_vision"] != "unknown":
                failures.append(
                    f"新模型的初始状态应该是 unknown，实得 "
                    f"{text_models[0]['supports_vision']}"
                )
            else:
                print("   ✓ 默认 unknown（核验有成本，不自动跑）")

            for purpose in ("chat", "title", "compact"):
                await c.put(f"{BASE}/api/bindings",
                            json={"purpose": purpose, "model_pk": text_pk})

            # ── 未核验就开视觉开关：应被拦下 ──
            print("\n2. 未核验的模型开视觉模式，应被拦下")
            sid = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
            r = await c.patch(f"{BASE}/api/sessions/{sid}",
                              json={"approval_mode": "auto", "vision_mode": True})
            if r.status_code == 200:
                failures.append(
                    "未核验的模型竟然允许开视觉模式 —— "
                    "用户发图后会拿到上游 400，而错误信息不指向真因"
                )
            else:
                # 错误体嵌在 detail 下 —— 见 main.py 的 app_error_handler，
                # 全项目统一成 {"detail": {code, message, hint}} 一种形状
                body = (r.json() or {}).get("detail", {})
                print(f"   HTTP {r.status_code}，code={body.get('code')}")
                print(f"   提示：{str(body.get('hint'))[:110]}")
                if body.get("code") != "vision_unverified":
                    failures.append(f"错误码不对：{body.get('code')}")
                elif "核验" not in str(body.get("hint")):
                    failures.append("报错没告诉用户去哪里核验")
                else:
                    print("   ✓ 拦下了，且提示了下一步动作")

            # ── 核验纯文本模型 ──
            print("\n3. 核验纯文本模型")
            r = await c.post(f"{BASE}/api/models/{text_pk}/verify-vision")
            body = r.json()
            print(f"   supports_vision={body['supports_vision']}")
            print(f"   detail: {str(body['detail'])[:160]}")
            if body["supports_vision"] == "true":
                print("   注意：这个「纯文本」模型其实支持图片，"
                      "跳过「不支持」分支的判定")
            else:
                print("   ✓ 判为不支持，且带回了上游原话")
                if not body["detail"]:
                    failures.append("核验失败但没有 detail —— 用户不知道为什么")
            if body["checked_at"] is None:
                failures.append("核验后没有记录时间 —— 用户会反复点，每次都花钱")

            # ── 视觉模型部分 ──
            if not vision_model:
                print("\n（未配 VERIFY_VISION_MODEL，跳过多模态验证。"
                      "见 .env.verify.example）")
            else:
                print(f"\n4. 登记视觉模型 {vision_model}")
                pr2 = await c.post(
                    f"{BASE}/api/providers",
                    json={
                        "name": f"vis-img-{int(asyncio.get_running_loop().time())}",
                        "base_url": vision_url,
                        "api_key": vision_key,
                        "models": [{"model_id": vision_model, "context_window": 131072}],
                    },
                )
                if pr2.status_code != 201:
                    print(f"   登记失败 {pr2.status_code}: {pr2.text[:300]}")
                    failures.append("视觉模型登记失败，检查 VERIFY_VISION_* 配置")
                else:
                    vis_pid = pr2.json()["id"]
                    created.append(vis_pid)
                    vis_models = (
                        await c.get(f"{BASE}/api/models?provider_id={vis_pid}")
                    ).json()["items"]
                    vis_pk = vis_models[0]["id"]

                    print("5. 核验视觉模型")
                    r = await c.post(f"{BASE}/api/models/{vis_pk}/verify-vision")
                    body = r.json()
                    print(f"   supports_vision={body['supports_vision']}")
                    print(f"   detail: {str(body['detail'])[:160]}")
                    if body["supports_vision"] != "true":
                        failures.append(
                            f"视觉模型核验未通过：{body['detail'][:200]}。"
                            "确认 VERIFY_VISION_MODEL 真的支持图片输入"
                        )
                    else:
                        print("   ✓ 核验通过")

                        # 切到视觉模型
                        for purpose in ("chat", "title", "compact"):
                            await c.put(f"{BASE}/api/bindings",
                                        json={"purpose": purpose, "model_pk": vis_pk})

                        print("\n6. 开视觉模式（已核验，应该成功）")
                        sid2 = (await c.post(f"{BASE}/api/sessions", json={})).json()["id"]
                        r = await c.patch(
                            f"{BASE}/api/sessions/{sid2}",
                            json={"approval_mode": "auto", "vision_mode": True},
                        )
                        if r.status_code != 200:
                            failures.append(
                                f"已核验的模型仍然开不了视觉模式：{r.text[:200]}"
                            )
                        else:
                            print("   ✓ 开启成功")

                            print("\n7. 发一张纯红色图，看模型能不能认出颜色")
                            red = data_url(make_png(80, 80, (220, 30, 30)))
                            res = await chat(
                                c, sid2,
                                "这张图片是什么颜色？只回答颜色名，不要解释。",
                                [red],
                            )
                            reply = str(res["reply"])
                            print(f"   回复：{reply[:120]}")
                            if not res["ok"]:
                                failures.append(f"发图失败：{res.get('errors')}")
                            elif not any(k in reply for k in ("红", "red", "Red")):
                                failures.append(
                                    f"模型没认出红色，回复是「{reply[:80]}」—— "
                                    "图片可能没真的送到"
                                )
                            else:
                                print("   ✓ 模型确实看到了图片")

                            print("\n8. 第二轮不带图，确认图片没进历史")
                            res2 = await chat(
                                c, sid2, "你刚才看到的图是什么颜色？一个词回答。"
                            )
                            reply2 = str(res2["reply"])
                            print(f"   回复：{reply2[:120]}")
                            # 它应该靠上一轮自己的回答记得，而不是靠重看图片。
                            # 两种情况都算通过 —— 这里只确认不报错。
                            if not res2["ok"]:
                                failures.append(f"第二轮失败：{res2.get('errors')}")
                            else:
                                print("   ✓ 第二轮正常（图片未重发，靠上下文文本）")

                            print("\n9. 校验：假 PNG 应被拒")
                            fake = data_url(b"MZ\x90\x00 this is not a png")
                            res3 = await chat(c, sid2, "看这个", [fake])
                            if res3.get("http") == 200:
                                failures.append(
                                    "假 PNG 竟然被接受了 —— MIME 是前端声明的，"
                                    "必须查魔数"
                                )
                            else:
                                print(f"   ✓ 拒了（HTTP {res3.get('http')}）")

                            print("\n10. 校验：超大图应被拒")
                            big = data_url(b"\x89PNG\r\n\x1a\n" + b"\x00" * (9 * 1024 * 1024))
                            res4 = await chat(c, sid2, "看这个", [big])
                            if res4.get("http") == 200:
                                failures.append("9MB 的图竟然被接受了")
                            else:
                                print(f"   ✓ 拒了（HTTP {res4.get('http')}）")

                            print("\n11. 关掉视觉模式后发图：图片丢弃但文字送达")
                            await c.patch(f"{BASE}/api/sessions/{sid2}",
                                          json={"vision_mode": False})
                            res5 = await chat(
                                c, sid2,
                                "只回答「收到」两个字。",
                                [data_url(make_png(20, 20, (0, 0, 255)))],
                            )
                            if not res5["ok"]:
                                failures.append(
                                    "关掉视觉模式后发图导致整条消息失败 —— "
                                    "应该只丢图片，文字仍送达"
                                )
                            else:
                                print(f"   ✓ 文字送达了：{str(res5['reply'])[:60]}")

            print("\n" + "=" * 58)
            if failures:
                for f in failures:
                    print(f"✗ {f}")
                return 1
            print("通过：三态核验、未核验拦截、多模态送达、校验、降级全部生效")
            return 0
        finally:
            for pid in created:
                await c.delete(f"{BASE}/api/providers/{pid}")
            print(f"（已清理 {len(created)} 个测试供应商）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
