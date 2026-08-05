"""
token 估算的测试。

重点是"工具定义必须计入"—— 漏算它会让估算偏低 45%，
后果是进度条少报一千多 token、压缩晚触发甚至直接 400。
"""

from typing import Any

from app.modules.agent.tokens import count_text, count_tools, estimate_tokens


def tool_spec(name: str, desc_len: int = 200) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "描述" * (desc_len // 2),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "内容"},
                },
                "required": ["path"],
            },
        },
    }


class TestToolsCounted:
    def test_tools_add_to_estimate(self) -> None:
        """
        实测：本项目 8 个工具的 JSON schema 是 1446 tokens，
        而一次真实会话的 prompt 才 3249。漏算就是偏低 45%。
        """
        msgs = [{"role": "user", "content": "你好"}]
        without = estimate_tokens(msgs)
        with_tools = estimate_tokens(msgs, [tool_spec(f"t{i}") for i in range(8)])
        assert with_tools > without
        # 8 个工具至少几百 token
        assert with_tools - without > 500

    def test_empty_tools_no_effect(self) -> None:
        msgs = [{"role": "user", "content": "x"}]
        assert estimate_tokens(msgs, []) == estimate_tokens(msgs)
        assert estimate_tokens(msgs, None) == estimate_tokens(msgs)

    def test_tool_token_count_is_cached_by_names(self) -> None:
        """
        缓存键是工具名元组。工具集变了（SubAgent 用 forked registry）
        必须重新算，不能取到错的值。
        """
        a = [tool_spec("read_file"), tool_spec("write_file")]
        b = [tool_spec("read_file")]
        assert count_tools(a) != count_tools(b)
        # 同一组重复调用结果一致
        assert count_tools(a) == count_tools(a)


class TestMessageCounting:
    def test_tool_call_arguments_counted(self) -> None:
        """tool_calls 的 arguments 占上下文，漏算会让带工具调用的会话偏低。"""
        plain = [{"role": "assistant", "content": ""}]
        # 用真实感的中文内容而不是重复字符：tiktoken 会把 "xxxx..." 合并成
        # 很少的 token（500 个 x 只有几十个 token），拿它做断言会误判成"没算"
        args = '{"path":"src/main.py","content":"这是一段真实的文件内容" * 20}'
        with_calls = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": args * 10},
                    }
                ],
            }
        ]
        assert estimate_tokens(with_calls) > estimate_tokens(plain) + 100

    def test_multimodal_image_counted(self) -> None:
        """图片远不止几个 token，不能按文本长度算。"""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
        assert estimate_tokens(msgs) > 500

    def test_chinese_text_not_underestimated(self) -> None:
        """
        中文不能按"字符数/4"估（那是英文的经验值）。
        中文一个字通常接近 1 个 token。
        """
        text = "这是一段中文测试文本" * 20  # 200 字
        tokens = count_text(text)
        # 200 个中文字至少 100 token，不能只算出 50
        assert tokens > 100, f"中文估算偏低：200 字算出 {tokens} tokens"


class TestCaching:
    def test_same_text_cached(self) -> None:
        """
        同一段文本会被反复编码：每轮都估算整个上下文，
        而历史消息一个字没变。缓存后测试套件从 80s 回到 12s。
        """
        text = "缓存测试" * 100
        first = count_text(text)
        second = count_text(text)
        assert first == second

    def test_huge_text_still_correct(self) -> None:
        """超长文本不进缓存，但结果必须正确。"""
        text = "长文本" * 5000  # 15000 字符，超过 8192 阈值
        tokens = count_text(text)
        assert tokens > 1000

    def test_empty_string(self) -> None:
        assert count_text("") == 0


class TestRealWorldRatio:
    def test_estimate_close_to_observed_real_usage(self) -> None:
        """
        用真实观测值回归。

        实测一次会话：上游报 prompt_tokens=3249，其中
          系统提示词 1310 + 工具定义 1446 + 对话内容 ~490
        修好之前估算 1787（0.55 倍），修好之后 3233（0.995 倍）。

        这个测试锁住"工具定义必须计入"这件事，防止将来有人
        为了省事把 tools 参数去掉。
        """
        system = "系统提示词内容" * 150  # 约 1300 token 量级
        tools = [tool_spec(f"tool_{i}", desc_len=300) for i in range(8)]
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": "帮我建个文件"},
            {"role": "assistant", "content": "好的"},
        ]

        with_tools = estimate_tokens(msgs, tools)
        without = estimate_tokens(msgs)
        # 工具定义应该占总量的相当一部分（实测约 45%）
        share = (with_tools - without) / with_tools
        assert 0.2 < share < 0.8, f"工具定义占比 {share:.0%}，与实测量级不符"
