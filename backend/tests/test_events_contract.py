"""
事件契约：`Ev` 枚举、文档表、前端 switch 三者必须一致。

## 为什么需要这个测试

`core/events.py` 的模块 docstring 一直写着"backend/tests/test_events_contract.py
会扫描枚举与文档表比对，多了或少了都会测试失败"——**而这个文件之前不存在**。

于是四个事件（approval_resolved / compacting / memory_recalled / refs_expanded）
悄悄漏出了文档：前端处理了、后端在发，只有 docs/03-api/sse-events.md 不知道。
而那份文档自称是"事件的唯一真源"。

漏文档的后果不是文档难看，是**下一个改这块的人（包括我自己）会照着过时的表
去改前端 switch**，然后某个事件静默丢失——没有报错，只是界面上少显示了东西。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.events import Ev

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "03-api" / "sse-events.md"
STORE = ROOT / "frontend" / "src" / "store" / "chat.ts"
TYPES = ROOT / "frontend" / "src" / "lib" / "types.ts"


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


class TestDocCoversAllEvents:
    def test_every_event_appears_in_doc(self) -> None:
        """
        每个 Ev 成员都要在文档里出现（带反引号，避免误匹配散文里的词）。
        """
        doc = _doc_text()
        missing = [str(e) for e in Ev if f"`{e}`" not in doc]
        assert not missing, (
            f"这些事件没写进 {DOC.name}：{missing}。"
            "那份文档自称事件的唯一真源，漏了的话下一个人会照着过时的表改前端"
        )

    def test_doc_has_no_phantom_events(self) -> None:
        """
        反向：事件总表里不能有代码里不存在的事件。

        删掉一个事件而忘了改文档，症状是后来的人去前端加一个永远不会
        触发的 case —— 而那不会报错。

        ## 为什么只扫总表而不是全文

        文档里每个事件都有一节字段表，那些行也长得像 `| \\`xxx\\` |`
        （xxx 是字段名如 delta / call_id）。扫全文会把 50 多个字段名
        当成"幽灵事件"报出来 —— 噪音大到没人会看。
        """
        doc = _doc_text()
        # 截取"事件总表"到下一个二级标题之间
        m = re.search(r"^##\s*事件总表\s*$(.*?)^##\s", doc, re.M | re.S)
        assert m, "找不到「事件总表」章节，文档结构变了"
        listed = set(re.findall(r"^\|\s*`([a-z_]+)`", m.group(1), re.M))
        assert listed, "事件总表里一行都没解出来，正则或表格格式变了"
        phantom = listed - {str(e) for e in Ev}
        assert not phantom, f"事件总表里有代码不存在的事件：{sorted(phantom)}"


class TestFrontendHandlesAllEvents:
    """
    前端不必处理每个事件，但"该处理却没处理"要能被发现。

    ## 为什么用白名单而不是要求全覆盖

    有些事件前端确实不需要动作：ping 是心跳、meta 在 sse.ts 里单独消费。
    强制全覆盖会逼人写空 case，那比不写更糟 —— 空 case 看起来像"已处理"。
    """

    # 明确不需要在 store 的 switch 里处理的
    NOT_IN_STORE = {
        "ping",  # 心跳，只为保持连接
        "meta",  # sse.ts 里消费，拿 run_id
        # 下面三个【枚举里有定义但后端从没 emit 过】。
        #
        # 它们是早期设计留下的占位：interact_required 原本要做
        # ask_user/ask_choice 那类阻塞交互，sandbox_fallback 和
        # mcp_unavailable 原本要在界面上提示降级 —— 但这些提示最后
        # 都改成了走 /api/meta 的字段（前端轮询读，不靠事件）。
        #
        # 留在枚举里不删是因为删了要动 sse-events.md 的编号；
        # 但要求前端处理一个永远不会到达的事件是没有意义的。
        "interact_required",
        "sandbox_fallback",
        "mcp_unavailable",
    }

    def test_store_switch_covers_events(self) -> None:
        src = STORE.read_text(encoding="utf-8")
        missing = []
        for e in Ev:
            name = str(e)
            if name in self.NOT_IN_STORE:
                continue
            if f'case "{name}"' not in src:
                missing.append(name)
        assert not missing, (
            f"store/chat.ts 的 switch 没处理这些事件：{missing}。"
            "后端在发而前端不接 = 功能等于不存在，且零报错"
        )

    def test_event_map_declares_all(self) -> None:
        """
        SseEventMap 要声明全部事件，否则 TS 不会在漏 case 时报错。
        """
        src = TYPES.read_text(encoding="utf-8")
        i = src.find("SseEventMap")
        assert i >= 0, "types.ts 里找不到 SseEventMap"
        block = src[i : i + 3000]
        missing = [str(e) for e in Ev if str(e) not in block]
        assert not missing, f"SseEventMap 缺这些事件：{missing}"


class TestDeltaEventsAreDocumented:
    """
    可丢弃的事件（队列满时直接扔）必须在文档里标明。

    不标的话前端会假设事件不会丢，于是把状态机建在"每个 delta 都到达"
    这个前提上 —— 而那个前提在队列压力下不成立。
    """

    def test_delta_set_matches_doc(self) -> None:
        from app.core.events import _DELTA_EVENTS

        doc = _doc_text()
        # 文档里应该点名这两个
        for e in _DELTA_EVENTS:
            assert str(e) in doc, f"可丢弃事件 {e} 没在文档里说明"

    def test_only_delta_events_are_droppable(self) -> None:
        """
        锁住这个集合。往里加事件要非常谨慎 ——
        丢 tool_end 那个工具卡片会永远停在"执行中"。
        """
        from app.core.events import _DELTA_EVENTS

        assert {str(e) for e in _DELTA_EVENTS} == {"thinking", "message"}
