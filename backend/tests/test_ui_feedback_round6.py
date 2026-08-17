"""
第六轮：技能增删改查、私密/失忆开关、skill/mcp 开关、模型能编辑技能。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.modules.agent.tools.asset import ManageAssetTool
from app.modules.agent.tools.base import ToolContext
from app.modules.skill import authoring
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend" / "src"
APP = ROOT / "backend" / "app"


@pytest_asyncio.fixture
async def client(db: AsyncSession, workspace_id: str) -> Any:
    from app.api import deps
    from app.infra.db.session import get_db
    from app.main import create_app
    from app.modules.agent.tools.base import ToolRegistry

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


def _ctx() -> ToolContext:
    return ToolContext(
        session_id="ses_r6000000000000000000",
        run_id="run_r6000000000000000000",
        workspace=Path(tempfile.gettempdir()),
        db=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
    )


class TestNameValidation:
    """
    名字直接当目录名，必须限制字符集。

    ## 为什么不只防 ../

    Windows 上 CON、NUL 这类保留名建不出目录；空格结尾的目录名会被
    静默去掉，于是"我的技能 "和"我的技能"指向同一个目录 ——
    而用户以为是两个。
    """

    @pytest.mark.parametrize(
        "bad",
        [
            "../../etc/passwd",
            "..\\..\\windows",
            "a/b",
            "a\\b",
            "a:b",
            "",
            "   ",
            "x" * 61,
        ],
    )
    def test_rejects_dangerous(self, bad: str) -> None:
        with pytest.raises(BadRequestError):
            authoring.validate_name(bad)

    @pytest.mark.parametrize("bad", ["con", "CON", "nul", "com1", "LPT9"])
    def test_rejects_windows_reserved(self, bad: str) -> None:
        """
        单独报这一类 —— 用户看到"my-skill 不行"会以为是字符问题，
        而实际原因是撞了系统保留名。
        """
        with pytest.raises(BadRequestError) as ei:
            authoring.validate_name(bad)
        assert ei.value.code == "reserved_name"

    @pytest.mark.parametrize(
        "ok", ["部署流程", "deploy-prod", "my_skill", "a1", "混合name-2"]
    )
    def test_accepts_reasonable(self, ok: str) -> None:
        assert authoring.validate_name(ok) == ok

    def test_strips_whitespace(self) -> None:
        assert authoring.validate_name("  abc  ") == "abc"


class TestBuildDocument:
    def test_requires_description(self) -> None:
        """
        缺 description 的条目会被加载器【静默跳过】，只留一条 warning
        诊断。用户填完保存以为建好了，而列表里什么都没多 ——
        这个失败模式必须在这一层挡掉。
        """
        with pytest.raises(BadRequestError) as ei:
            authoring.build_document(name="x", description="", body="y")
        assert ei.value.code == "missing_description"

    def test_description_flattened(self) -> None:
        """
        多行描述会破坏 frontmatter 的 YAML 结构。压成一行。
        """
        doc = authoring.build_document(
            name="x", description="第一行\n第二行\n\n第三行", body="b"
        )
        assert "description: 第一行 第二行 第三行" in doc

    def test_roundtrip_parses(self) -> None:
        """
        自己拼的文档必须能被自己的 parse_frontmatter 读回来 ——
        不然就是两套不一致的格式。
        """
        from app.modules.skill.loader import parse_frontmatter

        doc = authoring.build_document(
            name="测试",
            description="当用户说测试时使用",
            body="# 标题\n\n正文",
            keywords=["a", "b"],
        )
        meta, body = parse_frontmatter(doc)
        assert meta["name"] == "测试"
        assert meta["description"] == "当用户说测试时使用"
        assert meta["keywords"] == ["a", "b"]
        assert "# 标题" in body

    def test_chinese_not_escaped(self) -> None:
        """
        yaml.dump 会把中文转成 \\uXXXX。这些文件用户要直接看，
        转义之后完全不可读。
        """
        doc = authoring.build_document(
            name="部署", description="中文描述", body="中文正文"
        )
        assert "中文描述" in doc
        assert "\\u" not in doc


class TestAuthoringCrud:
    """在临时目录上跑，不碰真实的 skills/。"""

    @pytest.fixture(autouse=True)
    def _tmp_dirs(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """
        改 PROJECT_ROOT 而不是 settings.skills_dir。

        那个是 @property（`PROJECT_ROOT / "skills"`），没有 setter ——
        monkeypatch.setattr 会报 "property has no setter"。
        真正的接缝是 PROJECT_ROOT。
        """
        import app.core.config as cfg

        d = Path(tempfile.mkdtemp())
        (d / "skills").mkdir(parents=True)
        monkeypatch.setattr(cfg, "PROJECT_ROOT", d)
        yield d

    def test_create_then_read(self) -> None:
        r = authoring.upsert(
            kind="skill",
            name="流程A",
            description="当用户说 A 时使用",
            body="# A\n\n步骤",
            keywords=["a"],
        )
        assert r.created is True
        desc, body, kw, _raw = authoring.read_source(kind="skill", name="流程A")
        assert desc == "当用户说 A 时使用"
        assert "步骤" in body
        assert kw == ["a"]

    def test_collision_needs_overwrite(self) -> None:
        """
        模型起的名字撞车很常见。默认覆盖会悄悄冲掉用户手写的技能 ——
        而他不会收到任何提示。
        """
        authoring.upsert(
            kind="skill", name="撞名", description="第一次", body="1"
        )
        with pytest.raises(ConflictError) as ei:
            authoring.upsert(
                kind="skill", name="撞名", description="第二次", body="2"
            )
        assert ei.value.code == "already_exists"

    def test_overwrite_works(self) -> None:
        authoring.upsert(kind="skill", name="改", description="旧", body="旧")
        r = authoring.upsert(
            kind="skill",
            name="改",
            description="新描述",
            body="新正文",
            overwrite=True,
        )
        assert r.created is False
        desc, body, _kw, _raw = authoring.read_source(kind="skill", name="改")
        assert desc == "新描述"
        assert "新正文" in body

    def test_delete_removes_whole_dir(self) -> None:
        """
        技能可以带附件。只删 SKILL.md 会留一堆孤儿文件，
        而目录仍然存在 —— 下次 reload 时它既不是有效技能也不会被清理。
        """
        from app.core.config import settings

        authoring.upsert(kind="skill", name="带附件", description="d", body="b")
        extra = settings.skills_dir / "带附件" / "ref.md"
        extra.write_text("附件", encoding="utf-8")

        authoring.remove(kind="skill", name="带附件")
        assert not (settings.skills_dir / "带附件").exists()

    def test_delete_missing_raises(self) -> None:
        with pytest.raises(NotFoundError):
            authoring.remove(kind="skill", name="不存在")

    def test_bad_kind_rejected(self) -> None:
        with pytest.raises(BadRequestError):
            authoring.upsert(
                kind="persona", name="x", description="d", body="b"
            )


class TestManageAssetTool:
    """模型自己建技能的工具。"""

    @pytest.fixture(autouse=True)
    def _tmp_dirs(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """
        改 PROJECT_ROOT 而不是 settings.skills_dir。

        那个是 @property（`PROJECT_ROOT / "skills"`），没有 setter ——
        monkeypatch.setattr 会报 "property has no setter"。
        真正的接缝是 PROJECT_ROOT。
        """
        import app.core.config as cfg

        d = Path(tempfile.mkdtemp())
        (d / "skills").mkdir(parents=True)
        monkeypatch.setattr(cfg, "PROJECT_ROOT", d)
        yield d

    def test_registered(self) -> None:
        src = (APP / "main.py").read_text(encoding="utf-8")
        assert "ManageAssetTool()" in src

    def test_requires_approval(self) -> None:
        """
        它写的是【会进后续所有对话上下文】的文件。一个错误的技能描述
        能影响模型之后的全部行为 —— 比改一个源码文件影响面更大。
        """
        assert ManageAssetTool().requires_approval is True

    def test_description_explains_trigger_wording(self) -> None:
        """
        description 字段决定这个技能什么时候会被想起来。
        工具描述里必须说清这件事，否则模型会写"关于 X 的说明"
        这种永远不会被触发的描述。
        """
        d = ManageAssetTool().description
        assert "触发条件" in d or "什么时候" in d

    async def test_create_and_read(self) -> None:
        t = ManageAssetTool()
        r = await t.run(
            _ctx(),
            action="create",
            kind="skill",
            name="模型建的",
            description="当用户说 X 时使用",
            body="# X",
        )
        assert r.is_error is False
        assert "已新建" in r.content

        r2 = await t.run(_ctx(), action="read", kind="skill", name="模型建的")
        assert r2.is_error is False
        assert "当用户说 X 时使用" in r2.content

    async def test_create_collision_tells_to_use_update(self) -> None:
        """
        错误信息里要含着模型需要的信息 —— 它应该改用 update，
        而不是重试同样的调用。
        """
        t = ManageAssetTool()
        await t.run(
            _ctx(),
            action="create",
            kind="skill",
            name="撞",
            description="d",
            body="b",
        )
        r = await t.run(
            _ctx(),
            action="create",
            kind="skill",
            name="撞",
            description="d2",
            body="b2",
        )
        assert r.is_error is True
        assert "overwrite" in r.content or "已经存在" in r.content

    async def test_update_overwrites(self) -> None:
        t = ManageAssetTool()
        await t.run(
            _ctx(), action="create", kind="skill", name="u", description="旧", body="b"
        )
        r = await t.run(
            _ctx(),
            action="update",
            kind="skill",
            name="u",
            description="新",
            body="b2",
        )
        assert r.is_error is False
        assert "已更新" in r.content

    async def test_empty_description_rejected_with_reason(self) -> None:
        r = await ManageAssetTool().run(
            _ctx(), action="create", kind="skill", name="无描述", description="", body="b"
        )
        assert r.is_error is True
        # 要说清后果，不能只说"不能为空"
        assert "静默" in r.content or "跳过" in r.content

    @pytest.mark.parametrize("bad", ["../x", "a/b", "con"])
    async def test_path_escape_blocked(self, bad: str) -> None:
        r = await ManageAssetTool().run(
            _ctx(), action="create", kind="skill", name=bad, description="d", body="b"
        )
        assert r.is_error is True

    async def test_bad_kind(self) -> None:
        r = await ManageAssetTool().run(_ctx(), action="list", kind="persona")
        assert r.is_error is True

    async def test_list_empty(self) -> None:
        r = await ManageAssetTool().run(_ctx(), action="list", kind="skill")
        assert r.is_error is False

    async def test_delete(self) -> None:
        t = ManageAssetTool()
        await t.run(
            _ctx(), action="create", kind="skill", name="删我", description="d", body="b"
        )
        r = await t.run(_ctx(), action="delete", kind="skill", name="删我")
        assert r.is_error is False
        assert "已删除" in r.content



class TestSkillToggle:
    async def test_disabled_filtered_from_prompt(self, db: AsyncSession) -> None:
        """
        关掉的技能不进系统提示词 —— 那就是这个开关的全部意义。
        """
        from app.modules.skill import state as skill_state

        pairs = [("a", "描述A"), ("b", "描述B")]
        await skill_state.set_enabled(db, "a", False)
        got = await skill_state.filter_l1(db, pairs)
        assert got == [("b", "描述B")]

    async def test_no_record_means_enabled(self, db: AsyncSession) -> None:
        """
        表里没记录 = 启用。默认关闭会让用户装完发现模型看不见它，
        而没有任何提示说明原因。
        """
        from app.modules.skill import state as skill_state

        pairs = [("never-touched", "d")]
        assert await skill_state.filter_l1(db, pairs) == pairs

    async def test_toggle_is_idempotent(self, db: AsyncSession) -> None:
        """
        用户会反复开关同一个技能。每次 insert 会撞唯一约束，
        而 IntegrityError 完全不指向"你已经设置过了"。
        """
        from app.modules.skill import state as skill_state

        for v in (False, False, True, False):
            await skill_state.set_enabled(db, "反复", v)
        assert await skill_state.disabled_names(db) == {"反复"}

    def test_filter_applied_in_chat_service(self) -> None:
        src = (APP / "modules" / "agent" / "chat_service.py").read_text(
            encoding="utf-8"
        )
        assert "skill_state.filter_l1" in src

    def test_list_api_does_not_filter(self) -> None:
        """
        界面要能显示被关掉的技能，否则用户没法再打开它。
        """
        src = (APP / "api" / "routes_config.py").read_text(encoding="utf-8")
        i = src.index("async def list_skills")
        body = src[i : i + 1400]
        assert '"enabled": m.name not in off' in body

    def test_overhead_endpoint_includes_skills(self) -> None:
        """
        /context-overhead 必须把技能清单算进去。

        ## 实测发现的问题

        它原来只传 tool_names，不传 skills —— 于是：

          1. 上下文条少报几百 token（技能清单是常驻的）
          2. 开关技能时这个数字【不变】，用户点了看不到反应，
             会以为开关没生效

        真实验证里看到"关=1728 开=1728"就是这个原因。
        """
        src = (APP / "api" / "routes_models.py").read_text(encoding="utf-8")
        i = src.index("async def context_overhead")
        body = src[i : i + 2600]
        assert "skills=" in body, "没传技能清单"
        assert "skill_state.filter_l1" in body, "没按开关过滤"

    def test_panel_has_toggle(self) -> None:
        src = (FRONT / "components" / "SkillsPanel.tsx").read_text(encoding="utf-8")
        assert "api.toggleSkill" in src
        assert "contextOverhead" in src, "开关后没让固定开销失效"


class TestMcpToggle:
    def test_writes_yaml_preserving_comments(self) -> None:
        """
        mcp_servers.yaml 是用户手写并且要继续手写的文件。
        yaml.safe_dump 会丢掉全部注释 —— 点一次开关就把他的注释删了。
        """
        from app.modules.mcp.loader import load_configs, set_enabled

        sample = """# 顶部注释
- server_id: alpha
  transport: stdio
  command: echo
  command_approved: true
  # 块内注释
  enabled: true

- server_id: beta          # 行尾注释
  transport: http
  url: https://example.com/mcp
"""
        d = Path(tempfile.mkdtemp())
        f = d / "mcp_servers.yaml"
        f.write_text(sample, encoding="utf-8", newline="\n")

        assert set_enabled("alpha", False, path=f) is True
        after = f.read_text(encoding="utf-8")
        assert "# 顶部注释" in after
        assert "# 块内注释" in after
        assert "# 行尾注释" in after

        cfgs, errs = load_configs(path=f)
        assert not errs
        assert {c.server_id: c.enabled for c in cfgs} == {
            "alpha": False,
            "beta": True,
        }

    def test_inserts_enabled_when_absent(self) -> None:
        """
        块里没有 enabled 行时要插一行。找不到就放弃的话
        用户关一个没写 enabled 的服务器会静默失败。
        """
        from app.modules.mcp.loader import load_configs, set_enabled

        sample = """- server_id: gamma
  transport: http
  url: https://example.com/mcp
"""
        d = Path(tempfile.mkdtemp())
        f = d / "m.yaml"
        f.write_text(sample, encoding="utf-8", newline="\n")
        assert set_enabled("gamma", False, path=f) is True
        cfgs, _ = load_configs(path=f)
        assert cfgs[0].enabled is False

    def test_unknown_id_returns_false(self) -> None:
        """
        宁可报"没找到"，也不要在看不懂的格式上乱写 ——
        那个文件里可能有 token。
        """
        from app.modules.mcp.loader import set_enabled

        d = Path(tempfile.mkdtemp())
        f = d / "m.yaml"
        f.write_text("- server_id: only\n  transport: http\n", encoding="utf-8")
        assert set_enabled("nope", False, path=f) is False

    def test_status_api_reports_enabled(self) -> None:
        """
        关掉的服务器没有连接状态，只看 status 的话
        "用户关掉的"和"连不上的"长得一样 —— 前者不该显示成错误。
        """
        src = (APP / "api" / "routes_config.py").read_text(encoding="utf-8")
        assert 'd["enabled"] = enabled_map.get' in src

    def test_panel_has_toggle(self) -> None:
        src = (FRONT / "components" / "McpPanel.tsx").read_text(encoding="utf-8")
        assert "api.toggleMcpServer" in src
        # 过时的文案要去掉 —— 现在有开关了
        assert "在 yaml 里设" not in src

    def test_reregister_helper_shared(self) -> None:
        """
        开关单个服务器和整体 reload 要走同一段工具重注册逻辑。
        复制一遍的话症状是"用开关关掉的服务器工具还在，
        用 reload 关掉的就没了"。
        """
        src = (APP / "api" / "routes_config.py").read_text(encoding="utf-8")
        assert src.count("_reregister_mcp_tools") >= 3


class TestPrivateAmnesiaSwitches:
    """
    后端链路早就完整，只缺前端两个开关。
    """

    def test_store_has_actions(self) -> None:
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        assert "setPrivateMode" in src
        assert "setAmnesiaMode" in src

    def test_store_reads_on_open(self) -> None:
        """切会话时要读回来，否则开关显示的是上一个会话的状态。"""
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        assert "privateMode: session.private_mode" in src
        assert "amnesiaMode: session.amnesia_mode" in src

    def test_rollback_on_failure(self) -> None:
        """
        失败要回滚并显示原因。静默弹回去的话用户会以为开关坏了 ——
        而这个开关关系到"我说的话会不会被记住"。
        """
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        i = src.index("async setPrivateMode")
        body = src[i : i + 700]
        assert "previous" in body
        assert "toBanner(err)" in body

    def test_composer_has_both_buttons(self) -> None:
        src = (FRONT / "components" / "Composer.tsx").read_text(encoding="utf-8")
        assert "setPrivateMode" in src
        assert "setAmnesiaMode" in src
        assert "私密" in src
        assert "失忆" in src

    async def test_patch_actually_persists(
        self, client: AsyncClient, workspace_id: str
    ) -> None:
        sid = (await client.post("/api/sessions", json={"title": "m"})).json()["id"]
        r = await client.patch(
            f"/api/sessions/{sid}",
            json={"private_mode": True, "amnesia_mode": True},
        )
        assert r.status_code == 200
        d = (await client.get(f"/api/sessions/{sid}")).json()
        assert d["private_mode"] is True
        assert d["amnesia_mode"] is True
