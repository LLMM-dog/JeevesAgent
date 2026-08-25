"""
初始化与卸载脚本。

## 为什么值得测

这两个脚本是【新用户接触项目的第一步】和【最后一步】。它们坏了的表现
都很隐蔽：setup 静默跳过某一步，用户直到启动失败才发现，
而那时错误已经指向别处。

典型表现：Windows 上 npm 永远被跳过（`subprocess` 不执行 .cmd），
而第 1 步的环境检查却显示 "✓ Node v22" —— 看起来自相矛盾。
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _src(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


class TestSetupScript:
    def test_resolves_exe_before_spawn(self) -> None:
        """
        【Windows 上必须先把命令名解析成全路径】。

        npm 是 npm.cmd（批处理），不是 .exe。subprocess 不带 shell=True
        走 CreateProcess，而它不执行批处理文件 —— 传裸 "npm" 抛
        FileNotFoundError。

        而 shutil.which("npm") 是能找到的，所以第 1 步的环境检查显示
        "✓ Node v22"，第 3 步却报"找不到 npm"。

        实测后果：Windows 上每次 setup 都静默跳过前端依赖安装，
        用户直到 start 起不来才发现（vite 找不到、页面白屏）。
        """
        src = _src("setup.py")
        assert "shutil.which(cmd[0])" in src, "必须解析全路径再 spawn"
        # 不能用 shell=True —— 项目路径带空格时参数会被拆断。
        #
        # 去注释再断言：这条注释本身就写着 shell=True，
        # 直接搜原文会命中自己的解释（开发笔记 里记过这类假失败）
        from tests.conftest import code_only

        assert "shell=True" not in code_only(src).replace(" ", "")

    def test_npm_cmd_is_not_executable_directly(self) -> None:
        """
        证明上面那条不是臆想：裸 npm 在 Windows 上真的会抛。

        非 Windows 上跳过 —— 那里 npm 是可执行文件，没这个问题。
        """
        if sys.platform != "win32":
            pytest.skip("只有 Windows 有这个问题")
        import shutil

        if shutil.which("npm") is None:
            pytest.skip("本机没装 npm")

        with pytest.raises(FileNotFoundError):
            subprocess.run(["npm", "--version"], capture_output=True, check=False)

        # 全路径就能跑
        r = subprocess.run(
            [shutil.which("npm"), "--version"], capture_output=True, check=False
        )
        assert r.returncode == 0

    def test_installs_optional_extras(self) -> None:
        """
        默认要装上 mcp / search / web / cron。

        只装 dev 的话联网搜索、网页正文提取、定时任务、MCP 全都不注册
        —— 而这些是文档里介绍过的功能。用户按快速开始跑完，
        发现"说好的联网搜索呢"，而工具列表里就是没有，
        也没有任何提示说少装了一个组。
        """
        src = _src("setup.py")
        for extra in ("mcp", "search", "web", "cron"):
            assert f'"{extra}"' in src, f"setup 应该装 {extra} extra"

    def test_docker_extra_not_installed_by_default(self) -> None:
        """
        docker 组不默认装 —— 它需要本机跑着 Docker 守护进程，
        装了 SDK 但没有守护进程只会让沙箱探活多绕一圈。
        """
        src = _src("setup.py")
        # 只在注释里出现，不在 uv sync 的参数里
        sync_call = src[src.index('"uv",\n            "sync"') : src.index('ROOT,\n        "后端依赖"')]
        assert '"docker"' not in sync_call


class TestUninstallScript:
    def test_exists_and_documented(self) -> None:
        """
        必须有卸载脚本。

        项目自己的数据全在文件夹内，但 Docker 容器不是 ——
        它们是项目创建的、用户不知道存在的、且会一直占内存的东西。
        删了项目文件夹之后就再也没有东西会去清理它们
        （启动时的 cleanup_orphans 依赖"下次启动"，而已经没有下次了）。
        """
        assert (SCRIPTS / "uninstall.py").is_file()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "uninstall.py" in readme, "README 要告诉用户怎么卸载"

    def test_cleans_containers_by_prefix(self) -> None:
        src = _src("uninstall.py")
        assert "jeeves-" in src
        assert "docker" in src and "rm" in src

    def test_image_and_cache_not_removed_by_default(self) -> None:
        """
        镜像和包缓存默认不删 —— 别的项目可能也在用。
        默认删掉会造成"卸载一个项目导致另一个项目要重新下载依赖"。
        """
        src = _src("uninstall.py")
        assert "--all" in src
        assert "force" in src

    def test_warns_about_unremovable_files(self) -> None:
        """
        agent 可能写到项目外的文件是【无法自动清理】的 ——
        必须明确告诉用户，不能假装卸载很干净。
        """
        src = _src("uninstall.py")
        assert "无法自动清理" in src
        assert "run_shell" in src

    def test_dry_run_supported(self) -> None:
        """卸载是破坏性操作，要能先看会做什么。"""
        assert "--dry-run" in _src("uninstall.py")

    def test_runs_clean(self) -> None:
        """真跑一次 dry-run，确认不抛异常。"""
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "uninstall.py"), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert r.returncode == 0, r.stderr[-400:]
        assert "完成" in r.stdout


class TestReadme:
    """
    README 是新用户的入口。这里只测"不会撒谎"的部分。
    """

    def test_exists(self) -> None:
        assert (ROOT / "README.md").is_file()

    def test_mentions_real_entry_points(self) -> None:
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        # 提到的脚本必须真的存在
        for name in ("scripts/setup.py", "scripts/uninstall.py"):
            assert name in r
            assert (ROOT / name).is_file(), f"README 提到了不存在的 {name}"
        for name in ("start.sh", "start.bat"):
            assert name in r
            assert (ROOT / name).is_file()

    def test_states_security_boundaries(self) -> None:
        """
        安全边界必须写在明面上 —— 默认无鉴权、run_shell 没有上界、
        定时任务自动批准。这些是用户需要提前知道的。
        """
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "127.0.0.1" in r
        assert "鉴权" in r
        assert "run_shell" in r

    def test_states_what_uninstall_cannot_clean(self) -> None:
        """
        不能声称"删文件夹就彻底干净" —— Docker 容器和
        agent 写到项目外的文件都带不走。
        """
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "Docker" in r
        assert "白名单" in r

    def test_no_placeholder_left(self) -> None:
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        for bad in ("TODO", "FIXME", "XXX", "待补充"):
            assert bad not in r, f"README 里有未填的 {bad}"

    def test_referenced_docs_exist(self) -> None:
        """README 里链接的文档必须存在 —— 死链比没链更糟。"""
        import re

        r = (ROOT / "README.md").read_text(encoding="utf-8")
        for m in re.finditer(r"\]\((docs/[^)]+\.md)\)", r):
            p = ROOT / m.group(1)
            assert p.is_file(), f"README 链接了不存在的 {m.group(1)}"


class TestMigrationOnFreshClone:
    def test_env_creates_data_dir(self) -> None:
        """
        SQLite 不会自己建父目录。

        全新克隆的仓库里没有 data/（在 .gitignore 里），此时
        `alembic upgrade head` 直接失败：

            sqlite3.OperationalError: unable to open database file

        而这个报错完全不指向"目录不存在" —— 看起来像权限问题。

        应用自己启动时不会踩到（get_engine 里有 mkdir），
        所以只有"先跑迁移再起服务"这个顺序会中招，
        而那恰恰是文档推荐的顺序，也是 CI 的顺序。
        """
        src = (ROOT / "backend" / "migrations" / "env.py").read_text(encoding="utf-8")
        assert "data_dir.mkdir" in src


class TestWebSearchToggle:
    """
    联网搜索默认关（会把查询词发给第三方，必须显式同意），
    但要能在设置页开 —— "改 .env 再重启"对使用者门槛太高。
    """

    def test_runtime_toggle_exists(self) -> None:
        src = (ROOT / "backend" / "app" / "api" / "routes_config.py").read_text(
            encoding="utf-8"
        )
        assert "/websearch" in src

    def test_rebuilds_tools_on_switch(self) -> None:
        """
        切换后必须重建工具，否则要重启才生效。

        而且【必须先摘掉旧的】—— 只 register 不 unregister 的话，
        从 ddg 切到 tavily 后 registry 里还是旧 provider 的实例。
        """
        from app.api import routes_config

        src = inspect.getsource(routes_config.websearch_put)
        assert "unregister" in src
        assert src.index("unregister") < src.index("build_web_tools")

    def test_says_not_persisted(self) -> None:
        """
        这个开关改的是运行时对象，重启回落 .env。
        不说清楚的话用户重启后发现又关了，会以为是 bug。
        """
        from app.api import routes_config

        src = inspect.getsource(routes_config.websearch_put)
        assert "persisted" in src
        assert "persist_hint" in src

class TestLocalFilesManifest:
    """
    local-files.yaml 登记所有"不进 git 但真实存在"的路径。

    ## 为什么要测它

    这份清单同时是升级时的保留列表、卸载时的删除列表、排查时的索引。
    过时的清单比没有清单更糟 —— 用户照着它备份，结果漏了一个文件。

    所以让测试盯着它和 .gitignore 对齐。

    模式抄自常见实现 local-config-manifest.yaml。
    """

    @staticmethod
    def _load() -> dict:
        import yaml

        got = yaml.safe_load((ROOT / "local-files.yaml").read_text(encoding="utf-8"))
        assert isinstance(got, dict)
        return got

    def test_exists(self) -> None:
        assert (ROOT / "local-files.yaml").is_file()

    def test_critical_files_declared(self) -> None:
        """
        丢了就丢数据的三样必须标 critical：.env（里面是解密所有
        API Key 的唯一凭据）、数据库、上传的图片（那是唯一副本）。
        """
        m = self._load()
        crit = {e["path"] for e in m["paths"] if e.get("critical")}
        for need in (".env", "data/jeeves.db", "data/uploads/"):
            assert need in crit, f"{need} 必须标 critical"

    def test_every_path_is_gitignored(self) -> None:
        """
        清单里的路径必须真的不进 git。

        进了 git 的话它就不是"本地文件"，登记在这里会误导 ——
        用户以为要手动备份，其实 clone 就有。
        """
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        m = self._load()
        for e in m["paths"]:
            p = e["path"]
            stem = p.rstrip("/").split("/")[0]
            assert stem in ignored or p in ignored, f"{p} 不在 .gitignore 里"

    def test_templates_exist(self) -> None:
        """声明了模板的，模板文件必须真的在 —— 否则用户按说明找不到。"""
        m = self._load()
        for e in m["paths"]:
            tpl = e.get("template")
            if tpl:
                assert (ROOT / tpl).is_file(), f"{e['path']} 的模板 {tpl} 不存在"

    def test_external_section_covers_docker(self) -> None:
        """
        项目文件夹外的残留必须登记，尤其是 Docker 容器 ——
        那是项目自己创建的、用户不知道存在的东西。
        """
        m = self._load()
        blob = str(m["external"])
        assert "jeeves-" in blob
        assert "uninstall.py" in blob

    def test_external_admits_what_cannot_be_cleaned(self) -> None:
        """
        不能假装卸载很干净。agent 通过 run_shell 写到项目外的文件
        是无法自动清理的，必须写明。
        """
        m = self._load()
        blob = str(m["external"])
        assert "无法自动清理" in blob
        assert "run_shell" in blob

    def test_readme_links_manifest(self) -> None:
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "local-files.yaml" in r


class TestShellScriptPortability:
    """
    常见实现对比里发现的问题：Linux/macOS 用户第一步就卡住。
    """

    def test_start_sh_has_exec_bit(self) -> None:
        """
        start.sh 必须带执行位提交。

        100644 的话用户 clone 下来直接 ./start.sh 报
        Permission denied —— 而 README 如果没写 chmod +x，
        他就卡在第一步。

        README 每次都要写 `chmod +x setup.sh` 才能跑，
        就是因为脚本没带执行位。
        """
        r = subprocess.run(
            ["git", "ls-files", "-s", "start.sh"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            pytest.skip("不在 git 仓库里")
        assert r.stdout.startswith("100755"), (
            f"start.sh 应该是 100755（可执行），实际 {r.stdout.split()[0]}"
        )

    def test_gitattributes_pins_line_endings(self) -> None:
        """
        .sh 必须锁 LF。

        Windows 上 git 默认 autocrlf=true，检出成 CRLF 后 bash 报
        "/usr/bin/env: 'bash\\r': No such file or directory" ——
        而错误完全不提行尾，排查方向会跑到 shebang 路径上去。
        """
        ga = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        assert "*.sh text eol=lf" in ga
        assert "*.ps1 text eol=crlf" in ga

    def test_readme_mentions_chmod_and_policy(self) -> None:
        """
        两个平台的第一道坎都要在 README 里写出来：
        Windows 的执行策略、Unix 的执行位。
        """
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "chmod +x" in r
        assert "ExecutionPolicy" in r


class TestReadmeOnboarding:
    """
    常见实现对比后补的：光"能启动"不够，用户还要知道拿它干什么。
    """

    def test_has_concrete_provider_example(self) -> None:
        """
        必须给一个能直接照抄的端点配置。

        只说"填一个 OpenAI 兼容的端点"的话，用户不知道 base_url
        要不要带 /v1、模型名该写什么。README 给了
        DeepSeek 的完整示例，这一点值得抄。
        """
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "api.deepseek.com" in r
        assert "/v1" in r

    def test_has_example_prompts(self) -> None:
        """
        要有可以直接粘贴的第一批指令。

        "启动成功"到"知道能拿它干什么"之间有个空档 ——
        用"初次开始对话"一节填上了。
        """
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "workspace" in r
        # 至少要示范到记忆这种非显然的能力
        assert "记住" in r

    def test_has_prerequisites(self) -> None:
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "前置要求" in r
        assert "Node" in r

class TestDoubleClickLauncher:
    """
    start.bat 是 Windows 上的主入口（README 让用户双击它）。

    ## 为什么它必须存在且默认生产模式

    双击是 Windows 上最自然的启动方式，而 .ps1 双击默认被执行策略挡下，
    报错只说"无法加载文件"，不说怎么办。

    而 dev 模式对普通用户是错的四件事：两个进程而不是一个、
    --reload 白监视没人改的文件、两个端口要理解（5173 vs 9000）、
    首屏更慢。生产模式由后端伺服构建产物，一个进程一个地址。
    """

    def test_start_bat_exists(self) -> None:
        assert (ROOT / "start.bat").is_file(), "Windows 主入口缺失"

    def test_defaults_to_prod(self) -> None:
        """默认必须是生产模式，否则普通用户拿到的是开发服务器。"""
        src = (ROOT / "start.bat").read_text(encoding="utf-8", errors="replace")
        assert "-Prod" in src
        assert "set MODE=-Prod" in src

    def test_bypasses_execution_policy(self) -> None:
        """
        不带 -ExecutionPolicy Bypass 的话，双击会报"无法加载文件"——
        而这个包装器存在的全部意义就是绕开它。
        """
        src = (ROOT / "start.bat").read_text(encoding="utf-8", errors="replace")
        assert "-ExecutionPolicy Bypass" in src
        assert "-NoProfile" in src

    def test_pauses_on_exit(self) -> None:
        """
        没有 pause 的话，双击时错误信息一闪而过，用户只看到空屏。
        """
        src = (ROOT / "start.bat").read_text(encoding="utf-8", errors="replace")
        assert "pause" in src

    def test_ascii_only(self) -> None:
        """
        .bat 按控制台 OEM 代码页读取（中文 Windows 是 936）。
        存成 UTF-8 的话每个中文注释字节都会被当成命令，
        cmd 会刷一屏"不是内部或外部命令"。
        """
        raw = (ROOT / "start.bat").read_bytes()
        bad = [b for b in raw if b > 0x7F]
        assert not bad, f"start.bat 含 {len(bad)} 个非 ASCII 字节，会被 cmd 当成命令"

    def test_checks_prerequisites(self) -> None:
        """缺 uv 或 .env 时要给出下一步做什么，而不是让 PowerShell 报晦涩错误。"""
        src = (ROOT / "start.bat").read_text(encoding="utf-8", errors="replace")
        assert "where uv" in src
        assert ".env" in src
        # 首次运行 .env 缺失时自动调 setup
        assert "setup" in src.lower()

    def test_dev_escape_hatch(self) -> None:
        """要能切回开发模式 —— 否则改代码的人没法用这个入口。"""
        src = (ROOT / "start.bat").read_text(encoding="utf-8", errors="replace")
        assert "-Dev" in src

    def test_readme_points_at_bat(self) -> None:
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "双击 `start.bat`" in r
        # 生产模式的地址是 9000，不是 vite 的 5173
        assert "127.0.0.1:9000" in r


class TestBuildSkip:
    """
    生产模式下源码没改就不该重新构建。

    无条件构建意味着每次启动白等 5-10 秒 —— 而生产模式是普通用户
    的默认路径，他每天启动几次，每次都在等一个没必要的构建。
    """

    def test_ps1_skips_when_fresh(self) -> None:
        src = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
        assert "跳过构建" in src
        # 必须比对 index.html 而不是目录 —— 目录 mtime 不随内容更新
        assert "dist\\index.html" in src or "dist/index.html" in src

    def test_sh_skips_when_fresh(self) -> None:
        src = (ROOT / "start.sh").read_text(encoding="utf-8")
        assert "跳过构建" in src
        # find -newer 是 POSIX 的；stat 的参数在 GNU 和 BSD 上不一样
        assert "-newer" in src

    def test_watches_config_files(self) -> None:
        """
        只看 src/ 不够 —— 改了 vite.config 却不重新构建的话，
        "改了没生效"是最难排查的一类问题。
        """
        for name in ("scripts/start.ps1", "start.sh"):
            src = (ROOT / name).read_text(encoding="utf-8")
            assert "vite.config.ts" in src, f"{name} 没监视 vite.config.ts"
            assert "package.json" in src, f"{name} 没监视 package.json"

class TestSetupNonInteractive:
    """
    setup 默认不问任何问题。

    ## 为什么

    双击 start.bat 的人以为是"装依赖"，结果卡在一个要填 Base URL 和
    API Key 的提示上 —— 而它前面已经跑了几分钟的依赖安装，
    用户可能已经走开了。

    另外命令行里填 API Key 会进 shell 历史。

    同样的事在设置页里做更好：能看到探测到的模型列表、能改、能删、
    填错了有明确报错。
    """

    def test_default_skips_interactive_steps(self) -> None:
        src = (ROOT / "scripts" / "setup.py").read_text(encoding="utf-8")
        # 交互步骤必须在 args.interactive 分支里
        idx = src.index("if args.interactive:")
        branch = src[idx : idx + 400]
        assert "step5_model()" in branch, "配模型没放进 --interactive 分支"

    def test_interactive_flag_exists(self) -> None:
        """要保留命令行配置的入口 —— 有人就想在终端里做完。"""
        src = (ROOT / "scripts" / "setup.py").read_text(encoding="utf-8")
        assert '"--interactive"' in src

    def test_step_count_matches_default(self) -> None:
        """
        默认 TOTAL=4。写死 6 的话输出是 [4/6] 然后就结束了，
        用户以为还有两步没跑完。
        """
        src = (ROOT / "scripts" / "setup.py").read_text(encoding="utf-8")
        assert "TOTAL = 4" in src

    def test_outro_matches_readme(self) -> None:
        r"""
        收尾提示是用户唯一的指引，和 README 不一致时他会照这里做。

        踩过：这里写着 `.\start.ps1` 和 5173，而主入口已经变成
        双击 start.bat（生产模式，9000）。
        """
        src = (ROOT / "scripts" / "setup.py").read_text(encoding="utf-8")
        idx = src.index("def outro(")
        body = src[idx : idx + 1600]
        assert "start.bat" in body, "收尾没提 start.bat"
        assert "9000" in body, "收尾没给正确端口"
        # 5173 只该出现在"开发模式"的说明里
        if "5173" in body:
            assert "开发" in body, "提到 5173 但没说明那是开发模式"


class TestModelsPanelGrouping:
    """
    模型配置按模型组分组，但增删以【单个模型】为单位。

    ## 原来的问题

    设置页只有一个"已配置的端点"列表，删除按钮删的是整个模型组 ——
    连带它下面所有模型和功能位绑定。用户只想去掉一个不用的模型，
    结果配置全没了。
    """

    def test_panel_exists(self) -> None:
        f = ROOT / "frontend" / "src" / "components" / "ModelsPanel.tsx"
        assert f.is_file(), "ModelsPanel 不存在"

    def test_has_per_model_delete(self) -> None:
        src = (
            ROOT / "frontend" / "src" / "components" / "ModelsPanel.tsx"
        ).read_text(encoding="utf-8")
        assert "deleteModel" in src, "没有单个模型的删除"
        assert "同组的其它模型不受影响" in src, "删除提示没说清影响范围"

    def test_has_collapsible_groups(self) -> None:
        src = (
            ROOT / "frontend" / "src" / "components" / "ModelsPanel.tsx"
        ).read_text(encoding="utf-8")
        assert "aria-expanded" in src, "可收起的分组要有 aria-expanded"
        assert "collapsed" in src

    def test_has_enable_toggle(self) -> None:
        src = (
            ROOT / "frontend" / "src" / "components" / "ModelsPanel.tsx"
        ).read_text(encoding="utf-8")
        assert "patchModel" in src
        assert "enabled" in src
        # 要说清禁用不等于删除
        assert "配置保留" in src

    def test_provider_delete_warns_about_scope(self) -> None:
        """
        删整个模型组仍然可以，但提示必须写清后果，
        并指出"只想删一个模型"的做法。
        """
        src = (
            ROOT / "frontend" / "src" / "components" / "ModelsPanel.tsx"
        ).read_text(encoding="utf-8")
        assert "删除整个模型组" in src
        assert "用模型行上的删除按钮" in src

    def test_settings_page_uses_it(self) -> None:
        src = (ROOT / "frontend" / "src" / "pages" / "SettingsPage.tsx").read_text(
            encoding="utf-8"
        )
        assert "<ModelsPanel />" in src
        # 旧的端点列表不该还在
        assert "已配置的端点" not in src, "旧的端点列表没删掉"

    def test_settings_shows_all_models(self) -> None:
        """
        设置页要看到【全部】模型，包括禁用的 —— 否则没法重新启用。
        对话页的菜单才用 enabled_only。
        """
        src = (
            ROOT / "frontend" / "src" / "components" / "ModelsPanel.tsx"
        ).read_text(encoding="utf-8")
        assert "api.models()" in src, "设置页不该只拉启用的"
        switcher = (
            ROOT / "frontend" / "src" / "components" / "ModelSwitcher.tsx"
        ).read_text(encoding="utf-8")
        assert "enabledOnly: true" in switcher, "对话页菜单该只列启用的"

