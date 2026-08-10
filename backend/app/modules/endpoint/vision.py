"""
视觉：图片附件与模型能力核验。

## 为什么核验必须发真实请求

模型列表接口不返回"支不支持图片"这个信息。名字也不可靠 ——
`gpt-4o-mini` 支持、`gpt-4-turbo-preview` 支持、但 `deepseek-chat` 不支持，
而这三个名字里都没有 vision 字样。中转站更乱，同一个名字背后可能换过模型。

所以只能试：发一张 1x1 的图，看返回 200 还是 400。

## 为什么是三态而不是布尔

`supports_vision` 是 `true` / `false` / `unknown` 三个字符串。

`unknown` 必须存在，因为**核验有成本**（要发一次真实请求，花钱且要几秒），
不能对每个模型都自动跑一遍。用户加了 10 个模型，只有他想用图片的那个
需要核验。

如果做成布尔并默认 false，用户会看到"不支持视觉"却不知道那只是"没测过"。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# 支持的图片类型。
#
# 【白名单而非黑名单】。传 svg 上去的话，某些模型会当文本解析，
# 而 svg 里能塞 <script> 和外部引用 —— 那是一条注入路径。
ALLOWED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# 单张图上限 8MB（编码前）。
#
# base64 会让体积涨约 33%，8MB 变 10.7MB —— 已经接近多数模型的
# 请求体上限。再大的图应该先压缩。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 单轮最多几张图。
#
# 一张 1024x1024 的图在多数模型上折算 700~1500 token。5 张就是
# 几千 token，且它们【每轮都会重发】（如果进历史的话）——
# 这是视觉功能最容易炸上下文的地方。
MAX_IMAGES_PER_TURN = 5

def _make_probe_png() -> str:
    """
    生成核验用的图：16x16 纯红方块，RGB 真彩色，无 alpha。

    ## 为什么不用 1x1 透明 PNG

    最初用的是 1x1 全透明 RGBA（70 字节），实测被 siliconflow 拒了：

        {"code":20015,"message":"image_url provided is not a valid image."}

    而同一个模型换成 16x16 纯色图立刻返回 200 并正确答出"红色"。
    区别在于**全透明像素解码后没有任何可见内容**，部分模型的图片
    校验器认为这不是有效图片。

    这个假阴性很危险：它会把一个真正支持视觉的模型判成"不支持"，
    而错误信息说的是"图片无效"—— 完全不指向"是你的探针图有问题"。

    ## 为什么仍然要小

    16x16 编码后不到 200 字节，折算 token 极少。核验只需触发
    "端点收不收 image_url 类型的 content"，不需要模型看清细节。
    但**必须是一张真图**。

    ## 为什么现算而不写死字面量

    写死一长串 base64 的话，后来的人无法判断它是什么、能不能改。
    现算只需几十微秒，且代码本身就说明了这张图长什么样。
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    w = h = 16
    # colortype=2 是 RGB 真彩色，没有 alpha 通道 ——
    # 不透明是这张图的关键属性
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + bytes((220, 30, 30)) * w for _ in range(h))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


_PROBE_PNG_B64 = _make_probe_png()


@dataclass
class ImagePart:
    """一张待发送的图片。"""

    mime: str
    data_b64: str

    def to_api(self) -> dict[str, Any]:
        """
        转成 OpenAI 兼容的 image_url 结构。

        用 data URL 而不是 http URL：图片存在本地工作区，
        给模型一个 localhost 地址它拉不到。
        """
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{self.mime};base64,{self.data_b64}"},
        }


def validate_image(raw: bytes, mime: str) -> tuple[bool, str]:
    """
    校验图片。返回 (是否通过, 错误说明)。

    ## 为什么要查魔数而不只看 MIME

    MIME 来自前端声明，可以随便填。把一个 .exe 说成 image/png 上传，
    然后它被 base64 塞进请求发给模型 —— 虽然模型会拒绝，
    但我们不该把用户的任意文件内容外发。
    """
    if mime not in ALLOWED_MIME:
        return False, (
            f"不支持的图片类型 {mime}。"
            f"可用：{', '.join(sorted(ALLOWED_MIME))}"
        )
    if len(raw) > MAX_IMAGE_BYTES:
        mb = len(raw) / 1024 / 1024
        return False, (
            f"图片 {mb:.1f}MB 超过上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB。"
            "base64 编码会再涨三分之一，多数模型会直接拒绝"
        )
    if not raw:
        return False, "图片是空的"

    # 魔数校验
    sigs: dict[str, tuple[bytes, ...]] = {
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/gif": (b"GIF87a", b"GIF89a"),
        # webp 是 RIFF....WEBP，第 8~12 字节才是 WEBP
        "image/webp": (b"RIFF",),
    }
    expected = sigs.get(mime, ())
    if expected and not any(raw.startswith(s) for s in expected):
        return False, (
            f"文件内容不是 {mime} —— 声明的类型与实际内容不符。"
            "改扩展名不会改变文件格式"
        )
    if mime == "image/webp" and len(raw) >= 12 and raw[8:12] != b"WEBP":
        return False, "文件内容不是有效的 WebP"
    return True, ""


def decode_data_url(url: str) -> tuple[str, bytes] | None:
    """
    解析 data URL，返回 (mime, 原始字节)。解析失败返回 None。

    前端粘贴的图片是 data URL 形式，要在服务端还原出字节才能校验 ——
    只信前端校验等于没校验。
    """
    if not url.startswith("data:"):
        return None
    try:
        head, b64 = url.split(",", 1)
        mime = head[5:].split(";")[0]
        return mime, base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error):
        return None


def build_user_content(text: str, images: list[ImagePart]) -> str | list[dict[str, Any]]:
    """
    组装多模态 content。

    没有图片时**返回纯字符串**，不返回 `[{"type":"text",...}]` 的数组形式。

    理由：不是所有 OpenAI 兼容端点都接受数组形式的 content。有些中转站
    只实现了字符串分支，收到数组直接 400 或静默丢内容。既然没图片时
    两种写法等价，就用兼容性更好的那个。
    """
    if not images:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(img.to_api() for img in images[:MAX_IMAGES_PER_TURN])
    return parts


async def probe_vision(
    llm: Any, base_url: str, api_key: str, model_id: str
) -> tuple[bool, str]:
    """
    核验模型是否支持图片输入。返回 (支持, 说明)。

    ## 判定逻辑

    发一张 1x1 图并问"这是什么颜色"。只要**返回 200 且有内容**就算支持 ——
    不检查回答是否正确（1x1 透明像素本来就说不清颜色）。

    我们要测的是"端点接不接受 image_url 类型的 content"，
    不是"模型视觉能力有多强"。

    ## 为什么错误信息要原样带回

    失败原因有好几种：模型真不支持、中转站不转发多模态、key 权限不够、
    模型名写错。它们的修复动作完全不同，所以要把上游原话给用户看，
    而不是统一报"不支持视觉"。
    """
    content = [
        {"type": "text", "text": "这张图是什么颜色？只回答颜色名。"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{_PROBE_PNG_B64}"},
        },
    ]
    try:
        text = await llm.probe_chat(
            base_url=base_url,
            api_key=api_key,
            model_id=model_id,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        log.info("vision_probe_failed", model=model_id, err=msg[:200])
        # 常见原因给出针对性提示 —— 用户看到"不支持"时最想知道下一步做什么
        hint = ""
        low = msg.lower()
        if "unknown variant" in low or "unknown field" in low:
            # 端点根本不认识 image_url 这个 content 类型 —— 这是最明确的
            # "不支持"信号（实测 deepseek 返回
            # "unknown variant `image_url`"）
            hint = "这个模型的接口不接受 image_url 类型的内容，确实不支持图片。"
        elif "not a valid image" in low or "invalid image" in low:
            # 端点【认识】image_url，只是嫌这张图无效 —— 说明多模态通路是通的。
            #
            # 实测踩过：核验图原本是 1x1 全透明 PNG，siliconflow 返回
            # "image_url provided is not a valid image"，而同一模型换成
            # 16x16 纯色图立刻 200 并正确答出颜色。
            #
            # 这种情况下报"不支持"是假阴性，所以要提示可能是探针图的问题。
            hint = (
                "上游能解析 image_url 但认为图片无效 —— 多模态通路是通的，"
                "可能是探针图被它的校验器拒了。这是个可疑的判定，建议重试。"
            )
        elif "image" in low or "vision" in low or "multimodal" in low:
            hint = "上游明确拒绝了图片内容，这个模型或中转站不支持多模态。"
        elif "not found" in low or "404" in low:
            hint = "模型名可能写错了，或这个 key 没有该模型的权限。"
        elif "timeout" in low:
            hint = "请求超时，不代表不支持 —— 可以重试一次。"
        return False, f"{hint}上游返回：{msg[:300]}"

    if not (text or "").strip():
        # 200 但空内容。这种情况多见于中转站把多模态部分丢了 ——
        # 不能算支持，否则用户开了开关后每次发图都得到空回复。
        return False, "上游返回了空内容，可能是中转站丢弃了图片部分。不算支持"
    log.info("vision_probe_ok", model=model_id, reply=text[:60])
    return True, f"支持。测试回复：{text[:60]}"
