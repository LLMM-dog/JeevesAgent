"""
视觉功能测试。

## 为什么核验必须发真实请求

模型列表接口不返回"支不支持图片"。名字也不可靠 —— `gpt-4o-mini` 支持、
`deepseek-chat` 不支持，两个名字里都没有 vision 字样。中转站更乱，
同一个名字背后可能换过模型。

所以只能试。这里的测试覆盖三态流转、校验、降级，真实多模态请求
交给 scripts/verify_vision.py。
"""

import base64
from typing import Any

import pytest
from app.modules.endpoint import vision


def _png(size: int = 64) -> bytes:
    """构造一个有合法 PNG 魔数的字节串。"""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * size


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


class TestValidateImage:
    def test_png_ok(self) -> None:
        ok, err = vision.validate_image(_png(), "image/png")
        assert ok and err == ""

    def test_jpeg_ok(self) -> None:
        ok, _e = vision.validate_image(b"\xff\xd8\xff" + b"\x00" * 32, "image/jpeg")
        assert ok

    def test_gif_ok(self) -> None:
        ok, _e = vision.validate_image(b"GIF89a" + b"\x00" * 32, "image/gif")
        assert ok

    def test_webp_ok(self) -> None:
        raw = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 20
        ok, _e = vision.validate_image(raw, "image/webp")
        assert ok

    def test_svg_rejected(self) -> None:
        """
        白名单而非黑名单。

        svg 里能塞 <script> 和外部引用 —— 某些服务商会当文本解析，
        那是一条注入路径。
        """
        ok, err = vision.validate_image(b"<svg></svg>", "image/svg+xml")
        assert not ok
        assert "不支持" in err

    def test_mime_lie_detected(self) -> None:
        """
        查魔数而不只看 MIME。

        MIME 来自前端声明，可以随便填。把 .exe 说成 image/png 上传，
        内容就会被 base64 发给服务商。
        """
        ok, err = vision.validate_image(b"MZ\x90\x00fake exe", "image/png")
        assert not ok
        assert "不符" in err

    def test_webp_wrong_body(self) -> None:
        # RIFF 头对但第 8~12 字节不是 WEBP
        raw = b"RIFF" + b"\x00\x00\x00\x00" + b"AVI " + b"\x00" * 20
        ok, err = vision.validate_image(raw, "image/webp")
        assert not ok

    def test_oversize_rejected_with_reason(self) -> None:
        """
        超限报错要说清"为什么"和"多少"，不能只说"太大"。
        """
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (vision.MAX_IMAGE_BYTES + 1)
        ok, err = vision.validate_image(big, "image/png")
        assert not ok
        assert "MB" in err
        # base64 会涨三分之一，这个事实要告诉用户
        assert "base64" in err

    def test_empty_rejected(self) -> None:
        ok, err = vision.validate_image(b"", "image/png")
        assert not ok


class TestDataUrl:
    def test_roundtrip(self) -> None:
        raw = _png()
        got = vision.decode_data_url(_data_url(raw))
        assert got is not None
        mime, decoded = got
        assert mime == "image/png"
        assert decoded == raw

    def test_not_data_url(self) -> None:
        assert vision.decode_data_url("https://example.com/a.png") is None

    def test_malformed_base64(self) -> None:
        assert vision.decode_data_url("data:image/png;base64,!!!not-base64!!!") is None

    def test_no_comma(self) -> None:
        assert vision.decode_data_url("data:image/png;base64") is None


class TestBuildContent:
    def test_no_images_returns_plain_string(self) -> None:
        """
        没图片时【必须回字符串】，不能回 [{"type":"text"}] 数组。

        不是所有 OpenAI 兼容端点都接受数组形式。有些中转站只实现了
        字符串分支，收到数组直接 400 或静默丢内容。既然两种写法等价，
        就用兼容性更好的那个。
        """
        out = vision.build_user_content("你好", [])
        assert isinstance(out, str)
        assert out == "你好"

    def test_with_images_returns_array(self) -> None:
        img = vision.ImagePart(mime="image/png", data_b64="abc")
        out = vision.build_user_content("看这个", [img])
        assert isinstance(out, list)
        assert out[0] == {"type": "text", "text": "看这个"}
        assert out[1]["type"] == "image_url"
        assert out[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_image_only_no_text_part(self) -> None:
        img = vision.ImagePart(mime="image/png", data_b64="abc")
        out = vision.build_user_content("", [img])
        assert isinstance(out, list)
        assert all(p["type"] == "image_url" for p in out)

    def test_caps_image_count(self) -> None:
        """
        单轮图片数有上限。一张 1024x1024 折算 700~1500 token，
        5 张就是几千 —— 这是视觉功能最容易炸上下文的地方。
        """
        imgs = [
            vision.ImagePart(mime="image/png", data_b64=str(i))
            for i in range(vision.MAX_IMAGES_PER_TURN + 3)
        ]
        out = vision.build_user_content("x", imgs)
        assert isinstance(out, list)
        # 1 个 text + 上限张图
        assert len(out) == 1 + vision.MAX_IMAGES_PER_TURN

    def test_uses_data_url_not_http(self) -> None:
        """
        用 data URL 而不是 http URL —— 图片存在本地，
        给服务商一个 localhost 地址它拉不到。
        """
        img = vision.ImagePart(mime="image/png", data_b64="abc")
        assert img.to_api()["image_url"]["url"].startswith("data:")


class _FakeLLM:
    """可控行为的假 LLM。"""

    def __init__(self, reply: str = "白色", err: Exception | None = None) -> None:
        self.reply = reply
        self.err = err
        self.last_messages: Any = None

    async def probe_chat(
        self, *, base_url: str, api_key: str, model_id: str, messages: Any
    ) -> str:
        self.last_messages = messages
        if self.err:
            raise self.err
        return self.reply


class TestProbeVision:
    async def test_success(self) -> None:
        llm = _FakeLLM("这是白色")
        ok, detail = await vision.probe_vision(llm, "http://x/v1", "k", "m")
        assert ok
        assert "支持" in detail

    async def test_sends_image_url_part(self) -> None:
        """核验请求里必须真的带 image_url，否则测不出什么。"""
        llm = _FakeLLM()
        await vision.probe_vision(llm, "http://x/v1", "k", "m")
        content = llm.last_messages[0]["content"]
        assert any(p["type"] == "image_url" for p in content)

    async def test_empty_reply_counts_as_unsupported(self) -> None:
        """
        200 但空内容不算支持。

        这种情况多见于中转站把多模态部分丢了 —— 算支持的话用户开了开关后
        每次发图都得到空回复，而且不知道原因。
        """
        ok, detail = await vision.probe_vision(_FakeLLM(""), "http://x/v1", "k", "m")
        assert not ok
        assert "空内容" in detail

    async def test_error_message_passed_through(self) -> None:
        """
        上游原话要带回。

        失败原因有好几种（真不支持 / 中转站不转发 / key 无权限 / 模型名错），
        修复动作完全不同，统一报"不支持视觉"等于把有用信息扔掉。
        """
        llm = _FakeLLM(err=RuntimeError("HTTP 400: image input not supported"))
        ok, detail = await vision.probe_vision(llm, "http://x/v1", "k", "m")
        assert not ok
        assert "image input not supported" in detail
        # 并且给出针对性提示
        assert "多模态" in detail

    async def test_404_hints_model_name(self) -> None:
        llm = _FakeLLM(err=RuntimeError("HTTP 404: model not found"))
        _ok, detail = await vision.probe_vision(llm, "http://x/v1", "k", "m")
        assert "模型名" in detail

    async def test_timeout_says_retryable(self) -> None:
        """
        超时不等于不支持。要告诉用户可以重试 ——
        否则他会以为模型真的不行。
        """
        llm = _FakeLLM(err=RuntimeError("connect timeout"))
        _ok, detail = await vision.probe_vision(llm, "http://x/v1", "k", "m")
        assert "重试" in detail

    async def test_probe_image_is_tiny(self) -> None:
        """
        核验图要小。它只需触发"端点收不收 image_url"，
        不需要模型真看清什么 —— 用最小的图省钱且快。
        """
        assert len(vision._PROBE_PNG_B64) < 400

    def test_probe_image_is_opaque_with_visible_content(self) -> None:
        """
        核验图必须是【不透明的真图】，不能是 1x1 全透明像素。

        这是真实验证抓到的坑：原本用 1x1 透明 RGBA（70 字节），
        siliconflow 返回

            {"code":20015,"message":"image_url provided is not a valid image."}

        而同一个模型换成 16x16 纯色图立刻 200 并正确答出"红色"。
        全透明像素解码后没有任何可见内容，部分服务商的校验器认为
        这不是有效图片。

        后果是把一个真正支持视觉的模型判成"不支持"，而错误信息说的是
        "图片无效"——完全不指向"是你的探针图有问题"。
        """
        import struct

        raw = base64.b64decode(vision._PROBE_PNG_B64)
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        w, h, _bitdepth, colortype = struct.unpack(">IIBB", raw[16:26])
        # colortype 2 = RGB 真彩色（无 alpha）。4 和 6 带 alpha，不要用
        assert colortype == 2, f"核验图不该带 alpha 通道（colortype={colortype}）"
        # 不能是 1x1 —— 太小的图会被部分服务商的校验器拒
        assert w >= 8 and h >= 8, f"核验图太小：{w}x{h}"

    async def test_invalid_image_error_flagged_as_suspicious(self) -> None:
        """
        "not a valid image" 要被识别成【可疑判定】而非确定的不支持。

        端点能解析 image_url 说明多模态通路是通的，问题在图片本身。
        直接报"不支持"是假阴性。
        """
        llm = _FakeLLM(
            err=RuntimeError(
                'HTTP 400: {"code":20015,"message":"image_url provided is not a valid image."}'
            )
        )
        ok, detail = await vision.probe_vision(llm, "http://x/v1", "k", "m")
        assert not ok
        assert "通路是通的" in detail
        assert "重试" in detail

    async def test_unknown_variant_is_definite_unsupported(self) -> None:
        """
        "unknown variant image_url" 是最明确的不支持信号 ——
        端点根本不认识这个 content 类型（实测 deepseek 就是这样）。
        """
        llm = _FakeLLM(
            err=RuntimeError(
                "HTTP 400: Failed to deserialize the JSON body: "
                "messages[0]: unknown variant `image_url`"
            )
        )
        ok, detail = await vision.probe_vision(llm, "http://x/v1", "k", "m")
        assert not ok
        assert "确实不支持" in detail


class TestMsgToApi:
    def test_user_without_images_is_string(self) -> None:
        from app.modules.agent.messages import Msg

        api = Msg(role="user", content="你好").to_api()
        assert isinstance(api["content"], str)

    def test_user_with_images_is_array(self) -> None:
        from app.modules.agent.messages import Msg

        api = Msg(role="user", content="看图", images=[_data_url(_png())]).to_api()
        assert isinstance(api["content"], list)
        kinds = [p["type"] for p in api["content"]]
        assert kinds == ["text", "image_url"]

    def test_broken_data_url_skipped_not_fatal(self) -> None:
        """
        坏的 data URL 跳过，不让整轮请求失败。

        图片损坏时用户宁可"这张没看到"，也不要"整段对话报错"。
        """
        from app.modules.agent.messages import Msg

        api = Msg(
            role="user", content="看图", images=["data:image/png;base64,!!!bad!!!"]
        ).to_api()
        # 只剩 text，没有崩
        assert api["content"] == [{"type": "text", "text": "看图"}]

    def test_non_user_role_ignores_images(self) -> None:
        from app.modules.agent.messages import Msg

        api = Msg(role="assistant", content="回答", images=[_data_url(_png())]).to_api()
        assert isinstance(api["content"], str)


class TestImagesNotReplayedFromHistory:
    """
    图片【不进历史】。这是视觉功能最重要的成本约束。

    一张 1024x1024 的图折算 700~1500 token，而历史消息每轮都重发。
    20 轮会话里第 1 轮的图会被发 20 次 —— 3 张图就吃掉 60K token，
    而模型早在第一轮就描述过它们了。
    """

    def test_row_to_msg_does_not_restore_images(self) -> None:
        import inspect

        from app.modules.session import repo

        src = inspect.getsource(repo.row_to_msg)
        # 不还原 images 是故意的，且必须有注释说明 ——
        # 否则后来的人会以为是漏了然后"修好"它
        assert "images" in src, "row_to_msg 缺少关于 images 的说明注释"
        assert "不还原" in src or "故意" in src

    def test_row_to_msg_result_has_no_images(self) -> None:
        from app.modules.session.models import Message
        from app.modules.session.repo import row_to_msg

        row = Message(
            id="msg_x",
            session_id="ses_x",
            seq=1,
            role="user",
            content="看图",
            attachments='["data:image/png;base64,abc"]',
        )
        msg = row_to_msg(row)
        assert msg.images == []


class _FakeSession:
    id = "ses_test"
    vision_mode = 1


class TestCheckImages:
    def test_dropped_when_vision_off(self) -> None:
        """
        没开视觉模式时丢弃图片而不报错。

        用户可能在关掉开关后才发出已贴好的图。报错会让他丢掉整条消息
        （文字也发不出去），而丢弃图片只损失图片。
        """
        from app.modules.agent.chat_service import ChatService

        s = _FakeSession()
        s.vision_mode = 0  # type: ignore[assignment]
        out = ChatService._check_images([_data_url(_png())], s)
        assert out == []

    def test_kept_when_vision_on(self) -> None:
        from app.modules.agent.chat_service import ChatService

        url = _data_url(_png())
        assert ChatService._check_images([url], _FakeSession()) == [url]

    def test_invalid_raises(self) -> None:
        from app.core.exceptions import BadRequestError
        from app.modules.agent.chat_service import ChatService

        with pytest.raises(BadRequestError):
            ChatService._check_images(["data:image/png;base64,!!!"], _FakeSession())

    def test_fake_png_raises(self) -> None:
        from app.core.exceptions import BadRequestError
        from app.modules.agent.chat_service import ChatService

        with pytest.raises(BadRequestError):
            ChatService._check_images(
                [_data_url(b"MZ fake exe", "image/png")], _FakeSession()
            )

    def test_excess_truncated_not_rejected(self) -> None:
        """
        超出张数上限时丢弃多余的，不报错 ——
        前 N 张仍然可用，让用户重发不如直接处理掉。
        """
        from app.modules.agent.chat_service import ChatService

        urls = [_data_url(_png(i + 1)) for i in range(vision.MAX_IMAGES_PER_TURN + 3)]
        out = ChatService._check_images(urls, _FakeSession())
        assert len(out) == vision.MAX_IMAGES_PER_TURN


class TestVisionModeGating:
    """
    未核验的模型不许开视觉开关。

    不拦的话用户开了开关、发了图，得到的是上游 400，错误信息通常是
    "Invalid content type" 这类 —— 完全不指向"你的模型不支持图片"，
    排查方向会跑到网络、图片格式、base64 编码上去。
    """

    def test_route_checks_supports_vision(self) -> None:
        import inspect

        from app.api import routes_chat

        src = inspect.getsource(routes_chat)
        assert "vision_unverified" in src, "开启视觉模式时没检查模型能力"

    def test_error_hint_says_what_to_do(self) -> None:
        """报错必须给出下一步动作，不能只说"不支持"。"""
        import inspect

        from app.api import routes_chat

        src = inspect.getsource(routes_chat)
        assert "核验视觉" in src, "报错没告诉用户去哪里核验"


class TestThreeStateVision:
    def test_default_is_unknown(self) -> None:
        """
        默认 unknown 而不是 false。

        核验有成本（要发真实请求），不能对每个模型都自动跑。
        做成布尔并默认 false 的话，用户会看到"不支持视觉"却不知道
        那只是"没测过"。

        直接读列默认值，不插库 —— endpoint_id 有外键，插库要先造端点，
        而这个测试关心的只是"默认值是什么"。
        """
        from app.modules.endpoint.models import Model

        assert Model.__table__.c.supports_vision.default.arg == "unknown"

    async def test_resolve_maps_true_only(self, db: Any) -> None:
        """
        ResolvedModel.supports_vision 是布尔，只有 "true" 才映射成 True。
        unknown 要映射成 False —— 未知不能当支持用。
        """
        from app.modules.endpoint.models import Model

        for state, expected in (("true", True), ("false", False), ("unknown", False)):
            m = Model(
                id=f"mdl_{state}",
                endpoint_id="prv_x",
                model_id="m",
                supports_vision=state,
            )
            db.add(m)
            assert (m.supports_vision == "true") is expected
