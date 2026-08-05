"""
长期记忆测试。

## 常见实现的状况

| | 长期记忆 | 溯源 | 去重 | 读写开关 | 测试 |
| --- | --- | --- | --- | --- | --- |
| | **无**（memory_agent_node.py 是 0 字节空文件） | — | — | — | 无 |
| 同类实现 | **无**（静态 AGENTS.md，模型只读） | git | — | 只能整体关 | 无 |
| | 有 | **history + reason** | merge 工具 | **读写分离** | 1180 行 |

所以测试重点是 做对的两件（溯源、读写分离）和它做错的两件
（`lru_cache` 永不失效、召回 O(记忆总量)）。
"""

import json
from typing import Any

import pytest
from app.core.exceptions import BadRequestError, NotFoundError
from app.modules.memory import service as ms


async def mk(db: Any, content: str, theme: str = "测试", **kw: Any) -> Any:
    m = await ms.create(db, content=content, theme=theme, **kw)
    await db.commit()
    return m


class TestCreate:
    async def test_basic(self, db: Any) -> None:
        m = await mk(db, "用户偏好 Python")
        assert m.id.startswith("mem_")
        assert m.content == "用户偏好 Python"
        assert m.hit == 0
        assert m.archived_at is None

    async def test_empty_rejected(self, db: Any) -> None:
        with pytest.raises(BadRequestError):
            await ms.create(db, content="   ")

    async def test_overlong_truncated_not_rejected(self, db: Any) -> None:
        """
        超长截断而不是拒绝。

        直接拒绝会让模型反复重试同一件事；截断后仍然可用。
        """
        m = await mk(db, "x" * 500)
        assert len(m.content) == ms.MAX_CONTENT_CHARS

    async def test_whitespace_normalized(self, db: Any) -> None:
        m = await mk(db, "用户   偏好\n\nPython")
        assert m.content == "用户 偏好 Python"

    async def test_source_confidence_defaults(self, db: Any) -> None:
        """
        置信度按来源分层。常见实现没有置信度字段 ——
        结果是"用户随口说过一次"和"反复确认过"在召回时权重相同。
        """
        auto = await mk(db, "自动提炼的", source="auto", confidence=0.6)
        manual = await mk(db, "用户手写的", source="manual", confidence=1.0)
        assert auto.confidence < manual.confidence


class TestHistoryTraceability:
    """
    每次变更必须留痕。这是排查"AI 为什么以为我喜欢 X"的唯一线索。

    少见实现做了，而且它把 reason
    做成 update/delete/merge 的必需参数 —— 这条直接照抄。
    """

    async def test_update_records_reason_and_before(self, db: Any) -> None:
        m = await mk(db, "用户用 Python")
        await ms.update(db, m.id, reason="用户说改用 Go 了", content="用户用 Go")
        await db.commit()

        h = ms.parse_history(m)
        assert len(h) == 1
        assert h[0]["op"] == "update"
        assert h[0]["reason"] == "用户说改用 Go 了"
        # 改之前是什么必须留下 —— 否则无法判断"这个改动对不对"
        assert h[0]["before"]["content"] == "用户用 Python"
        assert m.content == "用户用 Go"

    async def test_update_without_reason_rejected(self, db: Any) -> None:
        """
        reason 是必需的。做成可选参数的话 LLM 一定会省略它。
        """
        m = await mk(db, "内容")
        with pytest.raises(BadRequestError):
            await ms.update(db, m.id, reason="   ", content="新内容")

    async def test_archive_without_reason_rejected(self, db: Any) -> None:
        m = await mk(db, "内容")
        with pytest.raises(BadRequestError):
            await ms.archive(db, m.id, reason="")

    async def test_multiple_updates_accumulate(self, db: Any) -> None:
        m = await mk(db, "v1")
        await ms.update(db, m.id, reason="改1", content="v2")
        await ms.update(db, m.id, reason="改2", content="v3")
        await db.commit()
        h = ms.parse_history(m)
        assert [x["reason"] for x in h] == ["改1", "改2"]
        assert h[0]["before"]["content"] == "v1"
        assert h[1]["before"]["content"] == "v2"

    async def test_history_capped(self, db: Any) -> None:
        """
        history 有上限。无限增长的话一条被反复修改的记忆会把整行撑到
        几十 KB，而早期变更几乎没有参考价值。
        """
        m = await mk(db, "v0")
        for i in range(30):
            await ms.update(db, m.id, reason=f"改{i}", content=f"v{i}")
        await db.commit()
        assert len(ms.parse_history(m)) == 20

    async def test_corrupt_history_does_not_block_update(self, db: Any) -> None:
        """
        history 损坏时从空数组重建，不抛异常 ——
        历史坏了不该让记忆本身变得不可修改。
        """
        m = await mk(db, "内容")
        m.history = "这不是 JSON"
        await db.commit()
        await ms.update(db, m.id, reason="仍然可改", content="新内容")
        await db.commit()
        assert m.content == "新内容"
        assert len(ms.parse_history(m)) == 1


class TestArchiveNotDelete:
    async def test_archive_hides_from_recall(self, db: Any) -> None:
        m = await mk(db, "塔罗牌工具正在重构")
        hits = await ms.recall(db, "塔罗牌重构")
        assert len(hits) == 1

        await ms.archive(db, m.id, reason="已完成")
        await db.commit()
        assert await ms.recall(db, "塔罗牌重构") == []

    async def test_archived_still_listable(self, db: Any) -> None:
        """
        归档不是真删。用户仍能看到并恢复 ——
        真删的话模型下一轮可能重新提炼出同一条，用户得反复删。
        """
        m = await mk(db, "内容")
        await ms.archive(db, m.id, reason="不需要了")
        await db.commit()
        assert await ms.list_all(db) == []
        assert len(await ms.list_all(db, include_archived=True)) == 1

    async def test_restore(self, db: Any) -> None:
        m = await mk(db, "塔罗牌重构")
        await ms.archive(db, m.id, reason="误判")
        await db.commit()
        await ms.restore(db, m.id)
        await db.commit()
        assert len(await ms.recall(db, "塔罗牌重构")) == 1
        # 恢复也留痕
        assert [h["op"] for h in ms.parse_history(m)] == ["archive", "restore"]


class TestDedup:
    async def test_find_similar_catches_near_duplicate(self, db: Any) -> None:
        """
        写入前去重。不做的话同一件事会被反复记 ——
        长会话里模型可能在第 3 轮和第 20 轮都觉得"用户偏好 Python"值得记。
        """
        await mk(db, "用户偏好使用 Python 写后端")
        similar = await ms.find_similar(db, "用户偏好使用 Python 写后端服务")
        assert len(similar) >= 1

    async def test_unrelated_not_similar(self, db: Any) -> None:
        await mk(db, "用户偏好 Python")
        assert await ms.find_similar(db, "项目部署在 Docker 里") == []

    async def test_merge_keeps_history_of_both(self, db: Any) -> None:
        a = await mk(db, "用户叫小明", theme="个人信息")
        b = await mk(db, "小明住在杭州", theme="个人信息")
        keep = await ms.merge(
            db,
            keep_id=a.id,
            drop_id=b.id,
            content="用户叫小明，住在杭州",
            reason="两条都是身份信息",
        )
        await db.commit()

        assert keep.id == a.id
        assert keep.content == "用户叫小明，住在杭州"
        # 被合并的那条归档，且留痕说明合进了哪里
        assert b.archived_at is not None
        assert "合并进" in ms.parse_history(b)[0]["reason"]
        # 保留方的 history 记下了合并前的内容
        assert ms.parse_history(keep)[0]["before"]["merged_from"] == "小明住在杭州"

    async def test_merge_sums_hits(self, db: Any) -> None:
        """
        合并后 hit 相加、confidence 取高 —— 两条独立记录指向同一件事，
        本身就是这件事更可信的证据。
        """
        a = await mk(db, "内容A", confidence=0.6)
        b = await mk(db, "内容B", confidence=0.9)
        a.hit, b.hit = 3, 5
        await db.commit()
        keep = await ms.merge(
            db, keep_id=a.id, drop_id=b.id, content="合并", reason="重复"
        )
        await db.commit()
        assert keep.hit == 8
        assert keep.confidence == 0.9

    async def test_merge_self_rejected(self, db: Any) -> None:
        m = await mk(db, "内容")
        with pytest.raises(BadRequestError):
            await ms.merge(
                db, keep_id=m.id, drop_id=m.id, content="x", reason="y"
            )


class TestRecall:
    async def test_chinese_matching(self, db: Any) -> None:
        await mk(db, "塔罗牌工具是彩蛋，正在重构", theme="项目")
        hits = await ms.recall(db, "塔罗牌重构进度如何")
        assert len(hits) == 1

    async def test_english_matching(self, db: Any) -> None:
        await mk(db, "项目用 FastAPI 和 SQLAlchemy", theme="技术栈")
        hits = await ms.recall(db, "fastapi 怎么配")
        assert len(hits) == 1

    async def test_no_match_returns_empty(self, db: Any) -> None:
        """
        没有相关内容就返回空，不硬凑。

        检索提示词专门写了这条：
        "如果确实没有任何相关内容，返回空数组 []，不要硬凑。"
        没有这条约束模型会为了显得有用而返回不相关的记忆。
        """
        await mk(db, "用户偏好 Python")
        assert await ms.recall(db, "今天天气怎么样") == []

    async def test_top_k_capped(self, db: Any) -> None:
        for i in range(20):
            await mk(db, f"Python 相关的第 {i} 条记忆")
        hits = await ms.recall(db, "Python")
        assert len(hits) <= ms.DEFAULT_TOP_K

    async def test_confidence_affects_ranking(self, db: Any) -> None:
        await mk(db, "用户偏好 Python 三", confidence=0.3)
        await mk(db, "用户偏好 Python 九", confidence=1.0)
        hits = await ms.recall(db, "用户偏好 Python")
        assert hits[0].memory.confidence == 1.0

    async def test_hit_affects_ranking(self, db: Any) -> None:
        await mk(db, "Python 用法 A")
        high = await mk(db, "Python 用法 B")
        high.hit = 10
        await db.commit()
        hits = await ms.recall(db, "Python 用法")
        assert hits[0].memory.id == high.id

    async def test_stopwords_ignored(self, db: Any) -> None:
        """
        停用词不参与匹配。_tokens 对纯单字停用词的查询应返回空 token 列表，
        进而召回返回空（因为 toks 为空直接 return []）。
        """
        from app.modules.memory.service import _tokens

        # 单字停用词不产生有意义的 2-gram
        toks = _tokens("的 了 是 在 我 你")
        # 单字中文不满足 2-gram 长度 ≥ 2 的要求，所以 token 集为空
        assert not toks
        # 因此召回也是空
        await mk(db, "用户偏好 Python")
        assert await ms.recall(db, "的 了 是 在 我 你") == []

    async def test_empty_query(self, db: Any) -> None:
        await mk(db, "内容")
        assert await ms.recall(db, "") == []

    async def test_touch_hits(self, db: Any) -> None:
        m = await mk(db, "Python 偏好")
        await ms.touch_hits(db, [m.id])
        await db.commit()
        assert m.hit == 1
        assert m.last_hit_at is not None


class TestInjectionFormat:
    async def test_marker_present(self, db: Any) -> None:
        """
        注入文本必须带标记 —— 提炼记忆时要靠它把自己注入的消息过滤掉。
        """
        await mk(db, "用户偏好 Python", theme="技术偏好")
        hits = await ms.recall(db, "Python")
        text = ms.format_for_injection(hits)
        assert text.startswith(ms.INJECTION_MARKER)

    async def test_says_not_instruction(self, db: Any) -> None:
        """
        必须明说"这是背景不是指令"。

        记忆是观察到的事实。不加这句的话，"用户上次用了 Python"
        容易被理解成"必须用 Python"。
        """
        await mk(db, "用户偏好 Python")
        text = ms.format_for_injection(await ms.recall(db, "Python"))
        assert "不是指令" in text

    async def test_char_budget(self, db: Any) -> None:
        """
        top-k 内也要有字数预算 —— 5 条超长记忆同样能撑爆上下文。
        """
        for i in range(5):
            await mk(db, "Python " + "长内容" * 90 + str(i))
        text = ms.format_for_injection(await ms.recall(db, "Python"))
        assert len(text) < ms.RECALL_CHAR_BUDGET + 200

    async def test_empty_hits_gives_empty_string(self, db: Any) -> None:
        assert ms.format_for_injection([]) == ""


class TestSelfFeedbackGuard:
    """
    防自反馈：注入的记忆不能被当成用户输入再次提炼。

    不过滤的话同一件事越记越多，且措辞逐轮漂移。
    也做了这个过滤，是个容易漏的坑。
    """

    async def test_injection_marker_is_distinctive(self) -> None:
        # 标记要足够特殊，不会和真实用户输入撞
        assert ms.INJECTION_MARKER.startswith("【")
        assert len(ms.INJECTION_MARKER) >= 4

    async def test_chat_service_filters_injected_message(self) -> None:
        """
        chat_service 的召回查询必须跳过带标记的消息。
        """
        import inspect

        from app.modules.agent.chat_service import ChatService

        src = inspect.getsource(ChatService._inject_memories)
        assert "INJECTION_MARKER" in src
        assert "continue" in src

    async def test_injected_message_not_persisted(self) -> None:
        """
        注入的消息不落库。

        落库的话下一轮 load_context 会读回来，与新召回的内容并存 ——
        同一条记忆在上下文里出现两次三次。
        """
        import inspect

        from app.modules.agent.chat_service import ChatService

        src = inspect.getsource(ChatService._inject_memories)
        # 直接 append 到 loop.messages，不走 _persist / append_message
        assert "loop.messages.append" in src
        assert "append_message" not in src


class TestNoLruCacheOnReads:
    """
    读取路径绝不加 lru_cache。

    get_narrative() 加了 @lru_cache(maxsize=1)
    ，而 cache_clear() 全库只在测试里出现 ——
    写入的记忆读不出来，必须重启进程。

    它在技能加载器上犯过同一个错误。同一个坑踩两次说明这是习惯问题，
    所以这里当红线锁住。
    """

    def test_service_has_no_lru_cache(self) -> None:
        import ast
        import inspect

        from app.modules.memory import service

        # 只检查装饰器，不检查字符串字面量 ——
        # 本模块的 docstring 里就提到了 lru_cache（说明为什么不用它），
        # 按字符串匹配会被自己的注释绊倒。
        tree = ast.parse(inspect.getsource(service))
        cache_decorators = [
            ast.unparse(dec)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            for dec in node.decorator_list
            if "lru_cache" in ast.unparse(dec) or ast.unparse(dec).endswith("cache")
        ]
        assert not cache_decorators, (
            "记忆读取路径加了 lru_cache —— 写入的记忆将读不出来。"
            "读文件/读库不是纯函数，数据会变 —— 缓存这类结果必然出错"
        )

    async def test_write_then_read_immediately_visible(self, db: Any) -> None:
        """写完立刻能读到，不需要任何缓存失效操作。"""
        assert await ms.recall(db, "杭州") == []
        await mk(db, "用户住在杭州")
        assert len(await ms.recall(db, "杭州")) == 1


class TestThemes:
    async def test_grouping(self, db: Any) -> None:
        await mk(db, "偏好 A", theme="技术偏好")
        await mk(db, "偏好 B", theme="技术偏好")
        await mk(db, "约定 A", theme="项目约定")
        themes = dict(await ms.themes(db))
        assert themes["技术偏好"] == 2
        assert themes["项目约定"] == 1

    async def test_archived_excluded_from_themes(self, db: Any) -> None:
        m = await mk(db, "内容", theme="临时")
        await ms.archive(db, m.id, reason="不要了")
        await db.commit()
        assert dict(await ms.themes(db)) == {}


class TestNotFound:
    async def test_update_missing(self, db: Any) -> None:
        with pytest.raises(NotFoundError):
            await ms.update(db, "mem_nonexistent", reason="x", content="y")

    async def test_archive_missing(self, db: Any) -> None:
        with pytest.raises(NotFoundError):
            await ms.archive(db, "mem_nonexistent", reason="x")


class TestToolDescriptions:
    """工具描述里必须写清什么该记、什么不该记。"""

    def test_remember_says_what_not_to_record(self) -> None:
        from app.modules.agent.tools.memory import RememberTool

        d = RememberTool.description
        assert "不要记" in d
        # 三个最容易记错的类别
        assert "临时" in d or "todo" in d
        assert "推测" in d

    def test_remember_says_one_thing_per_entry(self) -> None:
        """
        一条只记一件事。塞多件的话想改其中一件就得重写整条。
        """
        from app.modules.agent.tools.memory import RememberTool

        assert "一件事" in RememberTool.description

    def test_recall_says_usually_unnecessary(self) -> None:
        """
        自动注入已经覆盖大部分场景，要告诉模型别重复调 ——
        否则它每轮都主动检索一遍，白烧 token。
        """
        from app.modules.agent.tools.memory import RecallTool

        assert "不需要" in RecallTool.description

    def test_mutation_tools_demand_reason(self) -> None:
        from app.modules.agent.tools.memory import (
            ForgetMemoryTool,
            UpdateMemoryTool,
        )

        for t in (UpdateMemoryTool, ForgetMemoryTool):
            assert "reason" in t().parameters()["required"], f"{t.__name__} 没强制 reason"

    def test_forget_explains_soft_delete(self) -> None:
        """
        要告诉模型归档不是真删，否则它会因为"怕删错"而不敢用。
        """
        from app.modules.agent.tools.memory import ForgetMemoryTool

        assert "不是真删" in ForgetMemoryTool.description


class TestPrivateModeBlocksWrites:
    """
    private_mode 必须在【工具层】拦写入。

    这是真实验证抓到的 bug：最初只在召回侧做了 amnesia_mode，以为写入侧
    不用管 —— 但 `remember` 是模型主动调的工具，模型看不到会话开关，
    照样会调。实测在 private_mode 会话里它真的写进去了。

    读和写是两个方向，要两处分别拦。这也正是 session 表上是两个字段
    而不是一个的原因。
    """

    async def test_all_write_tools_check_private(self) -> None:
        import inspect

        from app.modules.agent.tools import memory as mt

        for tool in (mt.RememberTool, mt.UpdateMemoryTool, mt.ForgetMemoryTool):
            src = inspect.getsource(tool.run)
            assert "_is_private" in src, (
                f"{tool.__name__} 没检查 private_mode —— "
                "模型看不到会话开关，会照样写入"
            )

    async def test_recall_tool_does_not_check_private(self) -> None:
        """
        召回工具【不】受 private_mode 影响。

        private_mode 是"不写"，不是"不读"。混在一起的话
        "让它记但这轮别提"和"这轮别记但可以用旧记忆"就分不开了。
        """
        import inspect

        from app.modules.agent.tools import memory as mt

        assert "_is_private" not in inspect.getsource(mt.RecallTool.run)

    async def test_private_check_fails_closed(self) -> None:
        """
        查不到会话时按【不允许写】处理。

        反过来（默认允许）会在异常路径上悄悄写入本该保密的内容 ——
        隐私开关必须 fail-closed。
        """
        from app.modules.agent.tools.memory import _is_private

        class _Ctx:
            session_id = "ses_nonexistent"

            class db:  # noqa: N801
                @staticmethod
                async def execute(*a: Any, **kw: Any) -> Any:
                    raise RuntimeError("库挂了")

        assert await _is_private(_Ctx()) is True  # type: ignore[arg-type]


class TestHistoryJson:
    async def test_history_is_valid_json(self, db: Any) -> None:
        m = await mk(db, "内容")
        await ms.update(db, m.id, reason="改", content="新")
        await db.commit()
        parsed = json.loads(m.history)
        assert isinstance(parsed, list)
        assert parsed[0]["reason"] == "改"
