"""
主动压缩：工具、动态预算、占用跟随变化。

## 背景

原来只有被动压缩（上下文涨到窗口 75% 时自动触发）。两个问题：

  1. 时机不由模型决定。模型知道"调研阶段结束了、几十条工具输出已经
     没用了"，而阈值只看总量。于是常见情形是调研完还剩一半空间，
     等到 75% 触发时，正在进行的实现细节和早已无用的调研输出被一起压。
  2. 撞阈值时压缩是打断式的：正在推理的中途插入一次 LLM 调用。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from app.modules.agent.tools.base import ToolContext
from app.modules.agent.tools.context import CompactContextTool

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"


def _ctx(**extra: Any) -> ToolContext:
    return ToolContext(
        session_id="ses_test000000000000000000",
        run_id="run_test000000000000000000",
        workspace=Path("/tmp"),
        db=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        extra=extra,
    )


class TestCompactTool:
    """工具本身的行为。"""

    def test_registered(self) -> None:
        """
        没注册的话模型永远看不到它 —— 而这是整个功能的入口。
        """
        src = (APP / "main.py").read_text(encoding="utf-8")
        assert "CompactContextTool()" in src

    def test_no_approval_needed(self) -> None:
        """
        不动文件、不执行命令，最坏结果是白花一次 LLM 调用。
        要审批的话每次都弹窗，模型会因为怕打扰用户而不敢用。
        """
        assert CompactContextTool().requires_approval is False

    def test_description_says_when_to_use(self) -> None:
        """
        描述必须给出具体判断依据。

        模型感觉不到上下文压力 —— 没有任何信号告诉它"你已经用了 60%"。
        只写"需要时调用"的话它永远不会调。
        """
        d = CompactContextTool().description
        assert "什么时候用" in d
        assert "不要用在" in d
        # 要说清这是有损的
        assert "有损" in d or "丢" in d

    def test_requires_reason(self) -> None:
        """
        reason 是必填。压缩之后上下文突然变短，用户需要知道为什么 ——
        不写理由的话界面上只能显示"上下文被压缩了"。
        """
        p = CompactContextTool().parameters()
        assert p["required"] == ["reason"]

    async def test_without_hook_returns_error(self) -> None:
        """
        子 agent 里没有回调。如实报错而不是静默成功 ——
        静默成功会让模型以为压缩了，然后继续往上下文里塞东西。
        """
        r = await CompactContextTool().run(_ctx(), reason="测试")
        assert r.is_error is True
        assert "不支持" in r.content

    async def test_hook_called_with_reason(self) -> None:
        seen: list[str] = []

        async def hook(reason: str) -> dict[str, object]:
            seen.append(reason)
            return {
                "compacted": True,
                "victim_count": 5,
                "before_tokens": 9000,
                "after_tokens": 3000,
            }

        r = await CompactContextTool().run(
            _ctx(compact_now=hook), reason="调研阶段结束"
        )
        assert seen == ["调研阶段结束"]
        assert r.is_error is False
        assert "5" in r.content
        assert "9000" in r.content and "3000" in r.content
        # 省下多少要算出来，别让模型自己减
        assert "6000" in r.content

    async def test_nothing_to_compact_is_not_an_error(self) -> None:
        """
        "现在没什么可压的"是正常答复。

        标成 is_error 会让模型以为工具坏了、开始重试 ——
        而重试每次都会得到同样的结果。
        """

        async def hook(reason: str) -> dict[str, object]:
            return {"compacted": False, "reason": "历史太短"}

        r = await CompactContextTool().run(_ctx(compact_now=hook), reason="试试")
        assert r.is_error is False
        assert "历史太短" in r.content

    async def test_warns_about_lost_detail(self) -> None:
        """
        压缩后要提醒模型"细节只在摘要里了"。

        不提醒的话它会继续引用压缩掉的文件内容，
        而那些内容已经不在上下文里 —— 于是开始编。
        """

        async def hook(reason: str) -> dict[str, object]:
            return {
                "compacted": True,
                "victim_count": 3,
                "before_tokens": 8000,
                "after_tokens": 2000,
            }

        r = await CompactContextTool().run(_ctx(compact_now=hook), reason="x")
        assert "重新读" in r.content


class TestDynamicBudget:
    """
    压缩提示词按窗口动态算目标长度。

    ## 为什么不能写死字数

    8K 窗口的模型和 128K 的差 16 倍。写死"压到 2000 字"的话：
    小窗口压完仍然超限（压了等于没压），大窗口则白丢信息
    （明明还有 100K 空间，却把细节砍光）。
    """

    def test_template_has_budget_placeholders(self) -> None:
        from app.modules.agent import prompts

        tpl = prompts.load_builtin("compact")
        for name in (
            "budget_tokens",
            "budget_chars",
            "window_tokens",
            "budget_percent",
        ):
            assert f"{{{{{name}}}}}" in tpl, f"模板缺 {name}"

    def test_budget_computed_from_window(self) -> None:
        src = (APP / "modules" / "agent" / "compaction.py").read_text(encoding="utf-8")
        assert "model.context_window * settings.agent.compact_target_ratio" in src

    def test_budget_has_floor(self) -> None:
        """
        小窗口模型（8K * 20% = 1600）还行，但配错成 1K 窗口时
        20% 只有 200 —— 那点长度装不下六类必留信息。
        给个下限，宁可超预算也不要摘要退化成一句话。
        """
        src = (APP / "modules" / "agent" / "compaction.py").read_text(encoding="utf-8")
        assert "max(200," in src

    def test_ratio_only_covers_conversation(self) -> None:
        """
        20% 只算对话部分，不含系统提示词和工具定义。

        那两项是固定开销（本项目 18 个工具就 4300 token），
        算进来的话 8K 窗口下摘要额度会被挤到几乎没有。
        """
        cfg = (APP / "core" / "config.py").read_text(encoding="utf-8")
        i = cfg.index("compact_target_ratio")
        around = cfg[max(0, i - 700) : i + 100]
        assert "只算对话部分" in around

    @pytest.mark.parametrize(
        ("window", "expect_min"),
        [(8192, 1638), (65536, 13107), (131072, 26214)],
    )
    def test_budget_scales(self, window: int, expect_min: int) -> None:
        """预算随窗口线性变化。"""
        from app.core.config import settings

        got = max(200, int(window * settings.agent.compact_target_ratio))
        assert got == expect_min

    def test_over_budget_logged_not_truncated(self) -> None:
        """
        超预算只记日志，不截断。

        摘要是结构化的（小标题分段），从中间切断会切掉最后一节 ——
        而"失败原因"那一节完全可能排在末尾，切掉它比超预算糟得多。
        """
        src = (APP / "modules" / "agent" / "compaction.py").read_text(encoding="utf-8")
        assert "compaction_over_budget" in src
        # 不该有截断
        assert "summary_text[:budget" not in src


class TestUsageFollowsCompaction:
    """
    占用要跟着压缩变化。

    ## 为什么必须重发事件

    不发的话界面上的条还停在压缩前的数字，而用户刚看到"已压缩"——
    两者矛盾，他会以为压缩没生效。
    """

    def test_emit_after_compact(self) -> None:
        src = (APP / "modules" / "agent" / "loop.py").read_text(encoding="utf-8")
        i = src.index("async def _compact_on_request")
        body = src[i : i + 3000]
        assert "_emit_context_usage" in body, "压缩后没重发占用"

    def test_emit_is_shared_method(self) -> None:
        """
        抽成方法而不是复制一遍 —— 两处逻辑不一致的话
        主动压缩后的数字和正常轮次的算法不同，用户会看到跳变。
        """
        src = (APP / "modules" / "agent" / "loop.py").read_text(encoding="utf-8")
        assert "async def _emit_context_usage" in src
        # 至少三处调用：正常轮次、收尾、主动压缩
        assert len(re.findall(r"self\._emit_context_usage\(", src)) >= 3

    def test_clears_last_prompt_tokens(self) -> None:
        """
        压缩后上一轮的 usage 不再代表当前上下文大小。
        不清的话下一轮会立刻再触发一次被动压缩。
        """
        src = (APP / "modules" / "agent" / "loop.py").read_text(encoding="utf-8")
        i = src.index("async def _compact_on_request")
        body = src[i : i + 3000]
        assert "_last_prompt_tokens = 0" in body

    def test_final_emit_includes_reply(self) -> None:
        """
        收尾时要把回复算进去。

        最后一次 LLM 调用的 prompt_tokens 是"这轮发出去多少"，
        不含模型刚写的回复。而用户看这个条是想知道"下一轮发多少"——
        差一整条回复（长回复能差几千 token）。

        停在旧值的现象是：模型写了一大段，条却几乎没动，
        用户以为回复不占上下文。
        """
        src = (APP / "modules" / "agent" / "loop.py").read_text(encoding="utf-8")
        assert (
            "accum.usage.prompt_tokens + accum.usage.completion_tokens" in src
        ), "收尾没把回复算进去"

    def test_final_emit_not_marked_estimate(self) -> None:
        """
        prompt_tokens + completion_tokens 都是模型返回的真实值，
        相加仍然是真实值 —— 标成估算会让用户不信任它。
        """
        src = (APP / "modules" / "agent" / "loop.py").read_text(encoding="utf-8")
        i = src.index("accum.usage.prompt_tokens + accum.usage.completion_tokens")
        around = src[i : i + 200]
        assert "is_estimate=False" in around


class TestSubAgentHasNoHook:
    def test_hook_only_for_depth_zero(self) -> None:
        """
        子 agent 的上下文独立且短暂，压缩没有意义。

        注入的话它会压缩自己的上下文，而那个上下文马上就要被丢弃 ——
        白花一次 LLM 调用。
        """
        src = (APP / "modules" / "agent" / "loop.py").read_text(encoding="utf-8")
        i = src.index('"compact_now"')
        around = src[max(0, i - 400) : i + 200]
        assert "self.depth == 0" in around
