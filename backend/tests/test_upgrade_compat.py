"""
更新兼容：升级后旧数据必须还在。

## 为什么需要这个测试

用户问"每次更新后保留旧的数据吗"。答案取决于三件事：

  1. 用户数据文件不被 git 跟踪（否则 git pull 直接失败或覆盖）
  2. 迁移在启动时自动跑（否则新代码撞旧表结构）
  3. 迁移只加不删（否则旧数据丢字段）

三条都是隐式约定，没有测试的话很容易在某次改动里破掉一条 ——
而破掉的后果是用户升级后丢对话历史，那是不可逆的。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestUserDataNotTracked:
    """
    用户数据不能被 git 跟踪。

    ## 真实踩到的问题

    personas/AGENTS.md 原来是被跟踪的，而设置页允许用户编辑它。
    用户改完之后 git pull 直接失败：

        error: Your local changes to the following files would be
        overwritten by merge: personas/AGENTS.md
        Aborting

    用户要么丢掉自己的修改，要么学会 git stash。两个都不该要求。
    """

    def _tracked(self, path: str) -> bool:
        r = subprocess.run(
            ["git", "ls-files", "--error-unmatch", path],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        return r.returncode == 0

    def test_database_not_tracked(self) -> None:
        assert not self._tracked("data/jeeves.db")

    def test_env_not_tracked(self) -> None:
        """.env 里有加密密钥和 API Key。"""
        assert not self._tracked(".env")

    def test_persona_files_not_tracked(self) -> None:
        """三个人格文件都能在设置页里改，都不能被跟踪。"""
        for name in ("SOUL.md", "USER.md", "AGENTS.md"):
            assert not self._tracked(f"personas/{name}"), (
                f"personas/{name} 被跟踪 —— 用户改了它之后 git pull 会失败"
            )

    def test_examples_are_tracked(self) -> None:
        """
        .example.md 必须被跟踪 —— 首次启动靠它生成实际文件。
        不跟踪的话新用户装完没有人格设定，行为规则缺失会直接报错。
        """
        for name in ("SOUL.example.md", "USER.example.md", "AGENTS.example.md"):
            assert self._tracked(f"personas/{name}"), f"缺 {name}"

    def test_workspace_not_tracked(self) -> None:
        r = subprocess.run(
            ["git", "ls-files", "workspace/"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert not r.stdout.strip(), "workspace 下有被跟踪的文件"


class TestFirstRunCopiesExamples:
    def test_all_three_personas_copied(self) -> None:
        src = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        m = re.search(r'for name in \(([^)]*)\):', src)
        assert m, "找不到复制人格文件的循环"
        names = m.group(1)
        for want in ("SOUL", "USER", "AGENTS"):
            assert want in names, f"首次启动没复制 {want}"

    def test_never_overwrites_existing(self) -> None:
        """
        已有文件绝不覆盖 —— 否则每次升级都把用户的人格设定
        重置回默认，而他不会想到是升级干的。
        """
        src = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        i = src.index('for name in ("SOUL"')
        body = src[i : i + 500]
        assert "not target.exists()" in body, "没有'已存在就跳过'的判断"


class TestMigrationsRunOnStartup:
    """
    迁移必须在启动时自动跑。

    不跑的话新代码撞旧表结构 —— 用户看到的是一堆
    "no such column" 而不是"需要升级数据库"。
    """

    def test_lifespan_runs_migrations(self) -> None:
        src = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        assert "_run_migrations()" in src
        assert 'command.upgrade(cfg, "head")' in src

    def test_migration_chain_is_linear(self) -> None:
        """
        单链、无分叉。

        分叉的话 alembic 报 "Multiple head revisions" 并拒绝升级 ——
        而用户只是想启动应用，看到的却是一个 alembic 内部错误。
        """
        vdir = ROOT / "backend" / "migrations" / "versions"
        revs: dict[str, str | None] = {}
        for f in vdir.glob("*.py"):
            txt = f.read_text(encoding="utf-8")
            # 引号两种都要认：alembic 自动生成的用单引号，
            # 手写的用双引号。只认一种会漏掉整条链。
            rev = re.search(r"""^revision: str = ['"]([^'"]+)['"]""", txt, re.M)
            down = re.search(r"^down_revision: str \| None = (.+)$", txt, re.M)
            if rev:
                d = (down.group(1).strip() if down else "None").strip("'\"")
                revs[rev.group(1)] = None if d == "None" else d

        assert revs, "一个迁移都没有"
        # 每个 down_revision 最多被一个迁移引用
        parents = [d for d in revs.values() if d]
        dupes = {p for p in parents if parents.count(p) > 1}
        assert not dupes, f"迁移分叉了：{dupes} 被多个迁移指为父节点"
        # 恰好一个根
        roots = [r for r, d in revs.items() if d is None]
        assert len(roots) == 1, f"有 {len(roots)} 个根迁移"
        # 恰好一个头
        heads = [r for r in revs if r not in parents]
        assert len(heads) == 1, f"有 {len(heads)} 个 head：{heads}"

    def test_migrations_have_downgrade(self) -> None:
        """
        每个迁移都要能回滚。

        升级出问题时用户需要退回去 —— 没有 downgrade 的话
        唯一的办法是删库重来，那等于丢掉全部历史。
        """
        vdir = ROOT / "backend" / "migrations" / "versions"
        missing = []
        for f in vdir.glob("*.py"):
            txt = f.read_text(encoding="utf-8")
            i = txt.find("def downgrade()")
            if i == -1:
                missing.append(f.name)
                continue
            body = txt[i:]
            # 只有 pass 不算实现
            if re.fullmatch(r"def downgrade\(\) -> None:\s*(\"\"\".*?\"\"\")?\s*pass\s*", body, re.S):
                missing.append(f.name)
        assert not missing, f"这些迁移不能回滚：{missing}"


class TestUpgradePreservesData:
    """
    真正的问题：升级后对话历史还在吗。

    这里验的是"迁移只加不删"—— 破坏性操作要显式列出来审查。
    """

    def test_no_drop_table_on_user_data(self) -> None:
        vdir = ROOT / "backend" / "migrations" / "versions"
        bad = []
        for f in vdir.glob("*.py"):
            txt = f.read_text(encoding="utf-8")
            up = txt[txt.index("def upgrade()") : txt.index("def downgrade()")]
            for m in re.finditer(r"drop_table\(\s*[\"'](\w+)[\"']", up):
                if m.group(1) in {"session", "message", "workspace", "memory"}:
                    bad.append(f"{f.name}: drop_table({m.group(1)})")
        assert not bad, f"迁移里删了用户数据表：{bad}"

    def test_no_drop_column_on_message(self) -> None:
        """
        message 表是对话历史。删列等于丢历史的一部分，
        而用户升级时不会被问"你同意丢掉推理内容吗"。
        """
        vdir = ROOT / "backend" / "migrations" / "versions"
        bad = []
        for f in vdir.glob("*.py"):
            txt = f.read_text(encoding="utf-8")
            up = txt[txt.index("def upgrade()") : txt.index("def downgrade()")]
            if "drop_column" in up and ("message" in up or "session" in up):
                # batch_alter_table("message") 里的 drop_column 才算
                for m in re.finditer(
                    r'batch_alter_table\(\s*["\'](\w+)["\'][\s\S]{0,600}?drop_column',
                    up,
                ):
                    if m.group(1) in {"message", "session"}:
                        bad.append(f"{f.name}: {m.group(1)}.drop_column")
        assert not bad, f"删了对话历史的列：{bad}"

    def test_added_columns_have_defaults(self) -> None:
        """
        【往已有表加列】时必须给 server_default。

        不给的话已有行没法满足 NOT NULL，SQLite 直接报
        "Cannot add a NOT NULL column with default value NULL"——
        而那时用户的库已经开始迁移了，一半改了一半没改。

        ## 为什么只看 add_column

        create_table 里的 NOT NULL 不需要默认值 —— 新建的表没有行。
        我第一版把两者混在一起查，于是初始迁移里 13 个正常的建表列
        全被报成问题，而它们没有任何毛病。
        """
        vdir = ROOT / "backend" / "migrations" / "versions"
        bad = []
        for f in vdir.glob("*.py"):
            txt = f.read_text(encoding="utf-8")
            up = txt[txt.index("def upgrade()") : txt.index("def downgrade()")]
            # 只查 add_column(...) 里的列定义
            for m in re.finditer(r"add_column\(\s*sa\.Column\(([\s\S]{0,400}?)\)\s*\)", up):
                seg = m.group(1)
                if "nullable=False" in seg and "server_default" not in seg:
                    bad.append(f"{f.name}: {' '.join(seg.split())[:70]}")
        assert not bad, f"往已有表加了 NOT NULL 列但没给默认值：{bad}"

class TestTokenAccountingHonest:
    """
    token 数必须诚实：真实值不能标成估算，固定开销要看得见。

    ## 用户报的现象

    "我只是发送你好，包括返回和思考才 100 字不到，你的追踪估算到
    4000 token"。

    ## 实际情况

    span 里 in=4551 是【模型返回的真实值】—— 流式请求早就开了
    stream_options.include_usage。拆开看：

        系统提示词  1311  （行为规则 681 + 性格 231 + 自述 149 + 环境 250）
        工具定义    ~3200 （18 个工具的 JSON schema）
        用户消息       2

    所以数字是对的，问题是界面没有任何线索解释它，
    而我上一轮还把这个真实值标成了"（估算）"—— 双重误导。
    """

    def test_stream_requests_ask_for_usage(self) -> None:
        """
        流式必须显式要 usage。

        不带 stream_options.include_usage 的话 OpenAI 兼容端点
        【不会】在流里返回 usage —— 那时才真的只能靠本地估算，
        而估算永远解释不清用户的疑问。
        """
        src = (
            ROOT / "backend" / "app" / "infra" / "llm" / "openai_compat.py"
        ).read_text(encoding="utf-8")
        assert '"include_usage": True' in src, "流式没要 usage"

    def test_real_usage_not_marked_estimate(self) -> None:
        """
        从库里恢复的 prompt_tokens 是真实值，不能标成估算。

        标错的话用户看到"4551（估算）"，会以为最可信的那个数字
        不可信 —— 而它是模型自己报的。
        """
        src = (ROOT / "frontend" / "src" / "store" / "chat.ts").read_text(
            encoding="utf-8"
        )
        i = src.index("function restoreUsage")
        body = src[i : i + 1800]
        assert "is_estimate: false" in body, "把真实 usage 标成了估算"

    def test_fixed_overhead_reported(self) -> None:
        """
        工具定义和系统提示词要单独报出来。

        不报的话用户面对"你好 = 4551"只能得出"计数错了"这个结论。
        """
        src = (ROOT / "backend" / "app" / "modules" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )
        assert "tools_tokens=" in src
        assert "system_tokens=" in src
        assert "tool_count=" in src

    def test_ui_shows_breakdown(self) -> None:
        """
        界面要展示固定开销的构成。

        后来从 Composer 挪到了独立的 ContextBar：一根单色条改成
        三段着色（工具定义 / 系统提示词 / 对话内容），带百分比。
        """
        src = (ROOT / "frontend" / "src" / "components" / "ContextBar.tsx").read_text(
            encoding="utf-8"
        )
        assert "工具定义" in src, "没展示工具定义占多少"
        assert "系统提示词" in src
        assert "对话内容" in src
        # 要给出可操作的建议，不能只报数字
        assert "MCP" in src, "没告诉用户怎么降下来"

    def test_context_bar_always_visible(self) -> None:
        """
        没有实测数据时也要显示窗口大小。

        原来只在收到 context_usage 事件后才出现，也就是发过消息才有。
        但"这个模型有多大窗口"是发消息【之前】就该知道的 ——
        尤其准备粘一段长代码进去的时候。
        """
        src = (ROOT / "frontend" / "src" / "components" / "ContextBar.tsx").read_text(
            encoding="utf-8"
        )
        # 没有实测数据时用 /context-overhead 的估算值，
        # 而不是整块消失或只剩一段
        assert "api.contextOverhead" in src, "没有 usage 时拿不到固定开销"
        assert "windowTokens" in src

    def test_context_bar_segments_clamped(self) -> None:
        """
        分段宽度必须 clamp 到 0。

        分项是按占比估的，四舍五入后可能比总数多 1~2 ——
        不 clamp 的话对话段是负宽度，整条渐变错位。
        """
        src = (ROOT / "frontend" / "src" / "components" / "ContextBar.tsx").read_text(
            encoding="utf-8"
        )
        assert "Math.max(0, usage.used_tokens - tools - system)" in src

    def test_estimator_uses_real_tokenizer(self) -> None:
        """
        兜底估算用 tiktoken，不是"字符数除以 4"。

        除以 4 对中文错得很远 —— 中文一个字往往就是 1~2 个 token，
        而英文 4 个字符才 1 个。混排文本按固定比例算必然偏。
        """
        src = (ROOT / "backend" / "app" / "modules" / "agent" / "tokens.py").read_text(
            encoding="utf-8"
        )
        assert "tiktoken" in src
        assert "enc.encode(" in src

    def test_breakdown_is_proportional_not_subtracted(self) -> None:
        """
        分项必须按比例分摊，不能拿真实总数减本地分项。

        ## 实测数据

        本地 tiktoken(cl100k_base) 数出 tools 4298 + system 1845 = 6143，
        而模型报的整个 prompt 才 4547 —— 本地高 35%（DeepSeek 用自己的
        分词器）。

        直接相减的话"对话内容"是 -1596。用户看到一个负数只会更确信
        "这个计数是坏的"，而这个拆分本来就是为了解释它没坏。

        按比例分摊之后：4547 = 工具 3181 + 系统 1365 + 对话 1。
        """
        src = (ROOT / "backend" / "app" / "modules" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )
        i = src.index("local_tools = count_tools")
        body = src[i : i + 1600]
        assert "scale = min(1.0, used / local_total)" in body, "没有按比例分摊"
        assert "不能直接相减" in body, "没解释为什么不能相减"

    def test_ui_marks_breakdown_as_approximate(self) -> None:
        """
        分项是估的，必须标出来。

        总数是模型给的准确值，分项是本地占比推的 —— 两者可信度不同，
        混在一起显示会让用户以为分项也是精确的。
        """
        src = (ROOT / "frontend" / "src" / "components" / "ContextBar.tsx").read_text(
            encoding="utf-8"
        )
        # 分段图例本身就说明了构成，而"估算"由 is_estimate 标注
        assert "is_estimate" in src

    def test_estimator_counts_tools(self) -> None:
        """
        估算必须带上工具定义 —— 它占的比对话内容还多。
        漏掉的话进度条少报几千，用户以为还很空。
        """
        src = (ROOT / "backend" / "app" / "modules" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )
        assert "estimate_tokens(api_msgs, specs)" in src

