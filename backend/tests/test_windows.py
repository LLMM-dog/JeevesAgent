"""
模型名归一化与窗口查找测试。

失败模式：所有模型都回落到 32K 默认窗口，导致大窗口模型被过早压缩
（用户只觉得"怎么老是压缩"）或小窗口模型直接 400。全程无报错。
"""

import pytest
from app.modules.endpoint.windows import (
    DEFAULT_WINDOW,
    detect_model_type,
    looks_non_chat,
    lookup_window,
    normalize_model_name,
)


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # 中转站前缀
            ("openai/gpt-4o", "gpt-4o"),
            ("anthropic/claude-3-5-sonnet", "claude-3-5-sonnet"),
            ("accounts/fireworks/models/qwen-72b", "qwen-72b"),
            ("Pro/deepseek-ai/DeepSeek-V3", "deepseek-v3"),
            ("deepseek-ai/DeepSeek-R1", "deepseek-r1"),
            # 点号分隔
            ("anthropic.claude-3-5-sonnet-20241022", "claude-3-5-sonnet"),
            # 日期后缀
            ("gpt-4o-2024-11-20", "gpt-4o"),
            ("gpt-4o-20241120", "gpt-4o"),
            ("claude-3-5-sonnet-latest", "claude-3-5-sonnet"),
            # 大小写
            ("GPT-4O", "gpt-4o"),
            # 无需处理
            ("deepseek-chat", "deepseek-chat"),
        ],
    )
    def test_cases(self, raw: str, expected: str) -> None:
        assert normalize_model_name(raw) == expected

    @pytest.mark.parametrize("raw", ["qwen2.5-72b", "glm-4.5", "moonshot-v1-8k"])
    def test_version_numbers_preserved(self, raw: str) -> None:
        """
        版本号里的点和 -8k 这种窗口标识不能被误剥 ——
        剥掉 moonshot-v1-8k 的 8k 会让它匹配到错误的窗口。
        """
        assert normalize_model_name(raw) == raw.lower()


class TestLookupWindow:
    def test_longest_prefix_wins(self) -> None:
        """gpt-4o-mini 必须命中 gpt-4o-mini，不能命中更短的 gpt-4o。"""
        assert lookup_window("gpt-4o-mini") == (128_000, "matched")

    def test_gpt4_not_confused_with_gpt4o(self) -> None:
        assert lookup_window("gpt-4")[0] == 8_192
        assert lookup_window("gpt-4o")[0] == 128_000

    @pytest.mark.parametrize(
        "raw,window",
        [
            ("deepseek-chat", 65_536),
            ("openai/gpt-4o-2024-11-20", 128_000),
            ("Pro/deepseek-ai/DeepSeek-V3", 65_536),
            ("moonshot-v1-128k", 131_072),
            ("moonshot-v1-8k", 8_192),
            ("claude-3-5-sonnet-20241022", 200_000),
            # v4 是 1M 窗口。真实验证时这两个名字匹配不到，回落 32K 默认值 ——
            # 0.75 阈值下会在 24K 就开始压缩，白丢 97% 的可用窗口。
            ("deepseek-v4-pro", 1_000_000),
            ("deepseek-v4-flash", 1_000_000),
        ],
    )
    def test_real_world_names(self, raw: str, window: int) -> None:
        got, source = lookup_window(raw)
        assert got == window and source == "matched"

    def test_v4_not_matched_by_v3_entry(self) -> None:
        """
        v4 不能被 v3 的条目匹配上 —— 那会把 1M 当成 64K。
        前缀匹配要取最长匹配，这个测试防止有人图省事只留一个 deepseek 条目。
        """
        v4, _ = lookup_window("deepseek-v4-pro")
        v3, _ = lookup_window("deepseek-v3")
        assert v4 == 1_000_000
        assert v3 == 65_536

    def test_unknown_falls_back_and_marks_source(self) -> None:
        """
        匹配不到时必须标记 source=default，让 UI 能提示用户手动设置。
        静默用默认值是 那类问题的根源。
        """
        window, source = lookup_window("some-custom-model-xyz")
        assert window == DEFAULT_WINDOW
        assert source == "default"


class TestNonChat:
    @pytest.mark.parametrize(
        "name",
        ["text-embedding-3-large", "bge-large-zh", "tts-1", "whisper-1", "bge-reranker-v2"],
    )
    def test_detects_non_chat(self, name: str) -> None:
        assert looks_non_chat(name)

    @pytest.mark.parametrize("name", ["gpt-4o", "deepseek-chat", "claude-3-5-sonnet"])
    def test_chat_models_pass(self, name: str) -> None:
        assert not looks_non_chat(name)


class TestDetectModelType:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("deepseek-reasoner", "reasoning"),
            ("deepseek-ai/DeepSeek-R1", "reasoning"),
            ("qwq-32b", "reasoning"),
            ("o1", "reasoning"),
            ("o3-mini", "reasoning"),
            ("gpt-5", "reasoning"),
            ("text-embedding-3-large", "embedding"),
            ("bge-large-zh", "embedding"),
            ("jina-embeddings-v3", "embedding"),
            ("bge-reranker-v2-m3", "rerank"),
            ("tts-1", "tts"),
            ("whisper-1", "audio"),
            ("dall-e-3", "image"),
            ("deepseek-chat", "chat"),
            ("gpt-4o", "chat"),
            ("claude-3-5-sonnet", "chat"),
            ("deepseek-v4-pro", "chat"),
        ],
    )
    def test_cases(self, name: str, expected: str) -> None:
        assert detect_model_type(name) == expected
