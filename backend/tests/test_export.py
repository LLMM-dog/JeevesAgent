"""
会话导出。

## 为什么单独测

导出是**用户拿走自己数据的唯一途径**。对话记录里有他和模型一起理清的
思路、调试出的结论 —— 导出坏了等于数据被锁在 SQLite 里。

常见实现没有这个功能，所以没有可抄的实现，也没有别人踩过的坑。

## 重点

1. 文件名安全（模型生成的标题含 `/`、`:`、`*` 会让 Windows 保存失败）
2. 中文文件名的 HTTP 头编码（直接写会 UnicodeEncodeError → 500）
3. tool 消息要折叠但不能丢
4. base64 图片不能内联（一张图让文件涨几 MB）
5. 子智能体消息要导出且标注来源
"""

import json
from typing import Any

import pytest_asyncio
from app.modules.agent.messages import Msg, ToolCall
from app.modules.session import export as exp
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture(autouse=True)
async def _ws(db: AsyncSession) -> None:
    await repo.ensure_default_workspace(db, "/tmp/ws-export")


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> Any:
    """
    连到真实 app 的客户端，且【共用测试的 db】。

    ## 为什么必须覆盖 get_db

    不覆盖的话接口会开自己的连接（连到真实 data/jeeves.db），
    而测试数据写在内存库里 —— 表现是接口永远 404，
    而错误信息只说"会话不存在"，完全不指向"你连错库了"。

    ## 为什么不跑 lifespan

    跑的话会起追踪写入器和 LLM 客户端等后台任务，pytest 的 loop 关闭后
    它们仍引用旧 loop —— teardown 阶段一堆 "Event loop is closed"，
    而测试本身是通过的（噪声掩盖真实失败）。

    导出接口只需要 db，不需要工具注册表。
    """
    from app.infra.db.session import get_db
    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _mk(db: AsyncSession, title: str = "测试会话") -> str:
    s = await repo.create_session(db, title=title)
    return s.id


async def _fill(db: AsyncSession, sid: str) -> None:
    """造一轮带工具调用的完整对话。"""
    await repo.append_message(db, sid, Msg(role="user", content="帮我看看这个文件"))
    await repo.append_message(
        db,
        sid,
        Msg(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments='{"path":"a.py"}')],
        ),
    )
    await repo.append_message(
        db,
        sid,
        Msg(
            role="tool",
            content="print('hello')",
            tool_call_id="c1",
            tool_name="read_file",
        ),
    )
    await repo.append_message(db, sid, Msg(role="assistant", content="这是个打印语句。"))


class TestFilename:
    def test_windows_reserved_chars_replaced(self) -> None:
        """
        标题是模型生成的，可能含 Windows 不允许的字符。

        不清理的话保存会失败，而浏览器不提示原因 ——
        表现是"点了下载什么都没发生"。
        """
        bad = 'a/b\\c:d*e?f"g<h>i|j'
        out = exp.safe_filename(bad, "ses_x", ".md")
        for ch in '/\\:*?"<>|':
            assert ch not in out.replace("ses_x", ""), f"{ch!r} 没被替换"

    def test_control_chars_replaced(self) -> None:
        out = exp.safe_filename("a\x00b\x1fc\nd", "ses_x", ".md")
        assert all(ord(c) >= 32 for c in out)

    def test_empty_title_has_fallback(self) -> None:
        for t in ("", "   ", None):
            out = exp.safe_filename(t, "ses_x", ".md")  # type: ignore[arg-type]
            assert out.endswith("ses_x.md")
            assert len(out) > len("ses_x.md")

    def test_session_id_kept_for_uniqueness(self) -> None:
        """
        标题可能重复（模型对相似问题起相似标题），加 id 保证唯一。
        """
        a = exp.safe_filename("同一个标题", "ses_aaa", ".md")
        b = exp.safe_filename("同一个标题", "ses_bbb", ".md")
        assert a != b

    def test_long_title_truncated(self) -> None:
        out = exp.safe_filename("标" * 500, "ses_x", ".json")
        assert len(out) < 120

    def test_chinese_preserved(self) -> None:
        """中文要保留 —— 清理不该把正常字符也删掉。"""
        out = exp.safe_filename("修复登录问题", "ses_x", ".md")
        assert "修复登录问题" in out

    def test_trailing_dots_stripped(self) -> None:
        """
        Windows 不允许文件名以点或空格结尾（会被静默去掉或报错）。
        """
        out = exp.safe_filename("标题...  ", "ses_x", ".md")
        assert "标题_ses_x.md" == out or not out.split("_ses_x")[0].endswith(".")


class TestMarkdown:
    async def test_has_title_and_meta(self, db: AsyncSession) -> None:
        sid = await _mk(db, "我的会话")
        await _fill(db, sid)
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))

        assert md.startswith("# 我的会话")
        assert sid in md, "元信息里要有会话 ID —— 回头对照数据库要用"
        assert "创建时间" in md

    async def test_user_and_assistant_content_present(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        await _fill(db, sid)
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))

        assert "帮我看看这个文件" in md
        assert "这是个打印语句" in md

    async def test_tool_result_folded_not_dropped(self, db: AsyncSession) -> None:
        """
        工具结果要折叠（否则找不到对话主线）但不能丢 ——
        丢了的话导出看起来像模型凭空知道文件内容。
        """
        sid = await _mk(db)
        await _fill(db, sid)
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))

        assert "<details>" in md
        assert "read_file" in md
        assert "print('hello')" in md

    async def test_empty_assistant_shows_tool_call(self, db: AsyncSession) -> None:
        """
        只有 tool_calls 没正文的 assistant 消息不该渲染成空标题。
        """
        sid = await _mk(db)
        await _fill(db, sid)
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))

        assert "调用工具" in md
        # 不该有连续的空标题
        assert "## 助手\n\n\n## " not in md

    async def test_long_tool_output_truncated_with_notice(self, db: AsyncSession) -> None:
        """截断要说明，并指向 JSON 格式。"""
        sid = await _mk(db)
        await repo.append_message(db, sid, Msg(role="user", content="读文件"))
        await repo.append_message(
            db,
            sid,
            Msg(
                role="tool",
                content="x" * 50000,
                tool_call_id="c1",
                tool_name="read_file",
            ),
        )
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))

        assert len(md) < 20000
        assert "已截断" in md
        assert "JSON" in md

    async def test_images_not_inlined(self, db: AsyncSession) -> None:
        """
        base64 图片不能内联 —— 一张图能让 Markdown 涨几 MB，
        而大多数编辑器不渲染 data URI。
        """
        sid = await _mk(db)
        fake = "data:image/png;base64," + "A" * 5000
        await repo.append_message(
            db, sid, Msg(role="user", content="看这张图"), attachments=[fake]
        )
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))

        assert "data:image/" not in md
        assert "1 张图片" in md
        assert len(md) < 3000

    async def test_refs_annotated(self, db: AsyncSession) -> None:
        """
        引用要标注，否则回看时不知道"改一下这个文件"指的是哪个文件。
        """
        sid = await _mk(db)
        await repo.append_message(
            db,
            sid,
            Msg(role="user", content="改一下这个"),
            refs=[{"kind": "file", "path": "src/main.py"}],
        )
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))
        assert "src/main.py" in md

    async def test_subagent_labeled(self, db: AsyncSession) -> None:
        """
        子智能体的输出要标出来，否则人会以为是主模型说的。
        """
        sid = await _mk(db)
        await repo.append_message(
            db, sid, Msg(role="assistant", content="调研结论如下", agent_name="researcher")
        )
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))
        assert "researcher" in md

    async def test_private_mode_noted(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        s = await repo.get_session(db, sid)
        s.private_mode = 1
        await db.commit()
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))
        assert "隐私模式" in md

    async def test_empty_session_does_not_crash(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, [])
        assert md.startswith("# ")

    async def test_ends_with_newline(self, db: AsyncSession) -> None:
        """文本文件应以换行结尾（POSIX 惯例，很多工具依赖它）。"""
        sid = await _mk(db)
        await _fill(db, sid)
        s = await repo.get_session(db, sid)
        md = exp.to_markdown(s, await repo.load_messages(db, sid, agent_name=None))
        assert md.endswith("\n")
        assert not md.endswith("\n\n\n")


class TestJson:
    async def test_has_schema_version(self, db: AsyncSession) -> None:
        """
        不带版本号的话，两年后拿到一份导出文件没法判断能不能读。
        """
        sid = await _mk(db)
        s = await repo.get_session(db, sid)
        out = exp.to_json(s, [])
        assert out["schema_version"] >= 1

    async def test_preserves_all_message_fields(self, db: AsyncSession) -> None:
        """
        JSON 的用途是备份和迁移 —— 丢字段等于丢数据。
        """
        sid = await _mk(db)
        await _fill(db, sid)
        s = await repo.get_session(db, sid)
        out = exp.to_json(s, await repo.load_messages(db, sid, agent_name=None))

        m = out["messages"][0]
        for f in (
            "id",
            "seq",
            "role",
            "content",
            "reasoning",
            "tool_calls",
            "run_id",
            "prompt_tokens",
            "created_at",
        ):
            assert f in m, f"缺字段 {f}"

    async def test_tool_calls_parsed_not_string(self, db: AsyncSession) -> None:
        """
        tool_calls 在库里是 JSON 字符串，导出时要解析成对象 ——
        留成字符串的话导入方要二次解析，且转义层层叠加。
        """
        sid = await _mk(db)
        await _fill(db, sid)
        s = await repo.get_session(db, sid)
        out = exp.to_json(s, await repo.load_messages(db, sid, agent_name=None))

        calls = [m["tool_calls"] for m in out["messages"] if m["tool_calls"]]
        assert calls, "应该有带 tool_calls 的消息"
        assert isinstance(calls[0], list)
        assert calls[0][0]["name"] == "read_file"

    async def test_images_replaced_by_count(self, db: AsyncSession) -> None:
        """base64 图片不导出，只留个数。"""
        sid = await _mk(db)
        fake = "data:image/png;base64," + "A" * 5000
        await repo.append_message(
            db, sid, Msg(role="user", content="图"), attachments=[fake]
        )
        s = await repo.get_session(db, sid)
        out = exp.to_json(s, await repo.load_messages(db, sid, agent_name=None))

        assert out["messages"][0]["attachment_count"] == 1
        assert "data:image/" not in json.dumps(out)

    async def test_serializable(self, db: AsyncSession) -> None:
        """必须能被 json.dumps —— 有 ORM 对象漏进去就会抛。"""
        sid = await _mk(db)
        await _fill(db, sid)
        s = await repo.get_session(db, sid)
        out = exp.to_json(s, await repo.load_messages(db, sid, agent_name=None))
        json.dumps(out, ensure_ascii=False)

    async def test_session_settings_included(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        s = await repo.get_session(db, sid)
        out = exp.to_json(s, [])
        for f in ("private_mode", "vision_mode", "approval_mode", "pinned"):
            assert f in out["session"]


class TestRoute:
    async def test_markdown_download_headers(self, client: Any, db: AsyncSession) -> None:
        sid = await _mk(db, "接口测试")
        await _fill(db, sid)

        r = await client.get(f"/api/sessions/{sid}/export?fmt=markdown")
        assert r.status_code == 200
        assert "markdown" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        assert "接口测试" in r.text or "# " in r.text

    async def test_chinese_filename_does_not_500(self, client: Any, db: AsyncSession) -> None:
        """
        中文文件名必须用 RFC 5987 编码。

        HTTP 头只能放 latin-1 —— 直接写 filename="中文.md" 会让 uvicorn
        在编码响应头时抛 UnicodeEncodeError，表现是 500 而错误信息
        完全不指向文件名。
        """
        sid = await _mk(db, "中文标题带空格 和符号")
        r = await client.get(f"/api/sessions/{sid}/export")
        assert r.status_code == 200
        disp = r.headers["content-disposition"]
        # 两个都要有：ASCII 兜底 + UTF-8 真名
        assert "filename=" in disp
        assert "filename*=UTF-8''" in disp
        # 整个头必须能被 latin-1 编码（否则 ASGI 层会炸）
        disp.encode("latin-1")

    async def test_json_format(self, client: Any, db: AsyncSession) -> None:
        sid = await _mk(db)
        await _fill(db, sid)
        r = await client.get(f"/api/sessions/{sid}/export?fmt=json")
        assert r.status_code == 200
        data = r.json()
        assert data["session"]["id"] == sid
        assert len(data["messages"]) == 4

    async def test_default_is_markdown(self, client: Any, db: AsyncSession) -> None:
        sid = await _mk(db)
        r = await client.get(f"/api/sessions/{sid}/export")
        assert "markdown" in r.headers["content-type"]

    async def test_bad_format_rejected(self, client: Any, db: AsyncSession) -> None:
        sid = await _mk(db)
        r = await client.get(f"/api/sessions/{sid}/export?fmt=pdf")
        assert r.status_code == 422

    async def test_unknown_session_404(self, client: Any) -> None:
        r = await client.get("/api/sessions/ses_nonexistent/export")
        assert r.status_code == 404

    async def test_not_shadowed_by_detail_route(self, client: Any, db: AsyncSession) -> None:
        """
        /sessions/{id}/export 不能被 /sessions/{id} 抢走。

        FastAPI 按注册顺序匹配，而 detail 路由注册在前 —— 段数不同所以
        不会冲突，但这个断言保证将来改路由时不会退化
        （退化的表现是导出返回会话详情 JSON，而不是文件）。
        """
        sid = await _mk(db)
        r = await client.get(f"/api/sessions/{sid}/export")
        assert "content-disposition" in r.headers, "被 detail 路由抢走了"

    async def test_subagent_messages_included(self, client: Any, db: AsyncSession) -> None:
        """
        导出要包含子智能体消息（agent_name=None）。

        只导主线的话，导出的对话里会出现"我派个子智能体去查"
        然后突然有了结论，中间的过程全没了。
        """
        sid = await _mk(db)
        await repo.append_message(db, sid, Msg(role="user", content="调研一下"))
        await repo.append_message(
            db, sid, Msg(role="assistant", content="子智能体的发现", agent_name="researcher")
        )
        r = await client.get(f"/api/sessions/{sid}/export?fmt=json")
        roles = [m["agent_name"] for m in r.json()["messages"]]
        assert "researcher" in roles

def _tool_msg(name: str, content: str, agent: str = "") -> Any:
    """
    构造一条 tool 消息用于测 _md_tool。

    用轻量 stub 而不是走 repo.append_message：这几个测试只关心
    渲染逻辑，不需要数据库。
    """

    class _M:
        tool_name = name
        is_error = 0
        agent_name = agent

        def __init__(self, c: str) -> None:
            self.content = c

    return _M(content)


class TestExportHardening:
    """
    交叉审查发现的问题。
    """

    def test_scrubs_host_path_from_content(self) -> None:
        """
        被截断的工具输出会在正文里留一句「完整输出：<宿主绝对路径>」。

        两个理由必须去掉：
        1. 泄露服务端目录结构和用户名（贴进 issue 就走出去了）
        2. 对拿到导出的人没用 —— 文件不在他机器上，而且本机的
           data/tmp 会被 24 小时清理规则删掉，路径大概率已失效。
           指着一个不存在的路径比不给更误导。
        """
        raw = (
            "[输出过长，仅显示第 100~2000 行（共 2000 行）。"
            r"完整输出：D:\proj\data\tmp\jeeves_output_ab12.txt]" + "\nhello"
        )
        got = exp.scrub_paths(raw)
        assert "jeeves_output" not in got
        assert "D:\\proj" not in got
        # 截断信息本身要留着 —— 用户需要知道这里被截了
        assert "输出过长" in got
        assert "共 2000 行" in got
        assert "hello" in got

    def test_scrub_leaves_normal_text(self) -> None:
        assert exp.scrub_paths("普通输出") == "普通输出"
        assert exp.scrub_paths("") == ""

    def test_scrub_display_drops_path(self) -> None:
        got = exp._scrub_display({"exit_code": 0, "full_output_path": "D:/x/y.txt"})
        assert "full_output_path" not in got
        assert got["exit_code"] == 0, "别的字段要留着"

    def test_scrub_display_passthrough(self) -> None:
        assert exp._scrub_display(None) is None
        assert exp._scrub_display({"a": 1}) == {"a": 1}

    def test_fence_longer_than_content_backticks(self) -> None:
        """
        工具输出里含 ``` 极其常见（读 markdown、grep 代码块）。

        固定用三个反引号的话围栏提前闭合，后面的内容和 </details>
        一起串味，整个导出文件的结构从那里开始崩。
        """
        assert exp._fence("普通") == "```"
        assert len(exp._fence("有 ``` 围栏")) == 4
        assert len(exp._fence("````四个````")) == 5

    def test_tool_block_fence_survives_backticks(self) -> None:
        m = _tool_msg("read_file", "# 标题\n```python\nx=1\n```\n")
        out = "\n".join(exp._md_tool(m))
        # 围栏要比正文里最长的反引号串更长
        assert "````" in out
        assert out.count("</details>") == 1

    def test_subagent_tool_labelled(self) -> None:
        """
        子智能体的工具结果要标出来。

        不标的话父代理和子代理的 details 块长得一模一样，而子智能体的
        消息按时序插在父代理的 tool_calls 和 tool 结果之间 ——
        读者完全无法判断哪个是谁调的。
        """
        m = _tool_msg("run_shell", "ok", agent="researcher")
        out = "\n".join(exp._md_tool(m))
        assert "researcher" in out

    def test_main_agent_tool_not_labelled(self) -> None:
        m = _tool_msg("run_shell", "ok")
        out = "\n".join(exp._md_tool(m))
        assert "子智能体" not in out

    async def test_export_runs_off_event_loop(self) -> None:
        """
        序列化必须放线程池 —— 它是纯 CPU 的同步操作。

        直接在 event loop 上做的话整个进程卡住，而这个应用的核心是
        SSE 流式对话：某人导出大会话时所有正在进行的对话一起卡住不吐字。
        用户看到"模型突然卡死了"，排查方向会跑到API端点去。

        实测 3000 条消息（正文 71MB）阻塞 0.39 秒。
        """
        import inspect

        from app.api.routes_chat import export_session

        from tests.conftest import code_only

        src = code_only(inspect.getsource(export_session))
        assert "run_in_threadpool" in src
        # 不能还在 loop 上直接 dumps
        assert "await run_in_threadpool" in src
