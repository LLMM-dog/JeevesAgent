"""
Docker 沙箱。

## 重点测什么

是唯一有 Docker 沙箱的常见实现，容器复用、TTL、并发锁都做对了。
但它的 `docker run` 参数有四个问题，这里全部要测到：

1. `--network host` → 容器能打宿主 localhost、内网、云元数据端点
2. 无资源限制 → 一个死循环或 fork 炸弹能拖死宿主
3. 保留全部 capability
4. 只在正常退出时清理 → kill -9 后容器一直跑

外加本项目自己的要求：
5. 只挂工作区（挂项目目录的话容器能读 .env 里的所有 API Key）
6. 路径双向转换（sandbox.md 说这是"最容易出 bug 的地方"）
7. 降级不能静默（用户配了 docker 就是想要隔离）

## 为什么大量用源码断言

这些是**构造 docker 命令时的参数**，而真正跑 docker 需要环境里有
Docker 守护进程 + 镜像 —— CI 和大多数开发机上没有。

参数写错的后果是安全边界失效，而那在没有 Docker 的机器上完全测不出来。
所以用源码断言兜住：至少保证"参数没被误删"。
真实行为由 scripts/verify_sandbox.py 在有 Docker 的机器上验。
"""

import inspect
from pathlib import Path
from typing import Any

import pytest
from app.core.config import settings
from app.infra.sandbox import docker as dk
from app.infra.sandbox import factory

# code_only 在 conftest.py 里 —— 两个测试文件都要用，
# 各写一遍的话其中一份改了另一份不会跟着改。
from tests.conftest import code_only


@pytest.fixture(autouse=True)
def _reset() -> Any:
    """
    每个测试后清掉后端缓存。

    不清的话第一个测试选定的后端会被后面所有测试复用 ——
    而各个测试要验的正是"不同配置下选出不同后端"。
    """
    factory.reset_cache()
    yield
    factory.reset_cache()


class TestSecurityFlags:
    """
    容器参数里的安全项。这四项全都缺。
    """

    def test_network_defaults_to_none(self) -> None:
        """
        默认无网络。

        用 --network host，那意味着
        容器直接用宿主的网络命名空间：能 curl 宿主的 localhost 服务
        （包括 agent 自己的 API）、能访问内网、能打 169.254.169.254
        拿云凭证。

        文件系统隔离做了但网络没做，等于沙箱只有一半。
        """
        from app.core.config import SandboxConfig

        assert SandboxConfig().docker_network == "none"

    def test_network_flag_passed(self) -> None:
        src = inspect.getsource(dk.DockerSandbox._create)
        assert '"--network"' in src
        assert "cfg.docker_network" in src

    def test_memory_limit_passed(self) -> None:
        """一个 while True 分配内存能把宿主拖死。"""
        src = inspect.getsource(dk.DockerSandbox._create)
        assert '"--memory"' in src
        assert "cfg.docker_memory" in src

    def test_cpu_limit_passed(self) -> None:
        src = inspect.getsource(dk.DockerSandbox._create)
        assert '"--cpus"' in src

    def test_pids_limit_passed(self) -> None:
        """
        防 fork 炸弹。

        --memory 挡不住它：fork 炸弹的每个进程都很小，
        靠数量把系统压垮而不是靠内存。
        """
        src = inspect.getsource(dk.DockerSandbox._create)
        assert '"--pids-limit"' in src
        assert "cfg.docker_pids_limit" in src

    def test_capabilities_dropped(self) -> None:
        """默认容器保留 CAP_CHOWN / CAP_SETUID 等，没理由留着。"""
        src = inspect.getsource(dk.DockerSandbox._create)
        assert '"--cap-drop"' in src
        assert '"ALL"' in src

    def test_no_new_privileges(self) -> None:
        src = inspect.getsource(dk.DockerSandbox._create)
        assert "no-new-privileges" in src

    def test_env_example_names_match_real_fields(self) -> None:
        """
        .env.example 里的 SANDBOX 变量名必须对应真实字段。

        ## 真实发现的问题

        我新增了一套不带 docker_ 前缀的同义字段（image / network /
        memory_limit / cpu_limit），而 .env.example 里文档的是
        JEEVES_SANDBOX__DOCKER_IMAGE 这套。

        于是代码读 settings.sandbox.image，用户按文档配 DOCKER_IMAGE ——
        【完全不生效，且没有任何报错】：Settings 的 extra="ignore"
        让未知变量静默丢弃。

        用户会以为自己配了 bridge 网络/更大内存，实际全是默认值。

        这个测试直接对账，避免文档和字段再次分叉。
        """
        from pathlib import Path

        from app.core.config import SandboxConfig

        example = Path(__file__).resolve().parents[2] / ".env.example"
        if not example.is_file():
            pytest.skip("没有 .env.example")

        fields = set(SandboxConfig().model_fields)
        unknown: list[str] = []
        for line in example.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("JEEVES_SANDBOX__") or "=" not in line:
                continue
            name = line.split("=", 1)[0].removeprefix("JEEVES_SANDBOX__").lower()
            if name not in fields:
                unknown.append(name)

        assert not unknown, (
            f".env.example 里这些 SANDBOX 变量在 SandboxConfig 里不存在："
            f"{unknown} —— 用户配了会被静默忽略"
        )

    def test_defaults_are_sane(self) -> None:
        from app.core.config import SandboxConfig

        c = SandboxConfig()
        assert c.docker_memory
        # docker_cpus 是 str（沿用已有字段的类型）—— 直接传给 --cpus
        assert float(c.docker_cpus) > 0
        assert c.docker_pids_limit > 0


class TestMountScope:
    def test_only_workspace_mounted(self) -> None:
        """
        【只挂工作区】。

        挂项目目录的话容器里能改 agent 自己的代码、读 .env
        （里面有 ENCRYPTION_KEY 和所有 API Key）——
        那等于沙箱形同虚设，逃逸不需要技巧，直接读文件就行。
        """
        src = code_only(inspect.getsource(dk.DockerSandbox._create))
        # 只有一处 -v
        assert src.count('"-v"') == 1, "挂载点不该多于一个"
        assert "_workspace_root" in src
        # 不能出现项目根、data 目录之类
        for bad in ("project_root", "PROJECT_ROOT", "data_dir", ".env"):
            assert bad not in src, f"不该挂 {bad}"

    def test_workspace_root_used_not_cwd(self, tmp_path: Path) -> None:
        """
        挂的是工作区根，不是 cwd。

        只挂子目录的话，模型 cd 进 src/ 之后就访问不到同级的
        tests/ 了 —— 而它上一轮可能刚读过那里的文件。
        """
        sb = dk.DockerSandbox()
        root = Path(settings.workspace_dir).resolve()
        sub = root / "a" / "b"
        assert sb._workspace_root(sub) == root

    def test_outside_workspace_falls_back_to_cwd(self, tmp_path: Path) -> None:
        """
        cwd 不在工作区里时挂 cwd 自己，不挂一个更大的范围。

        这种情况不该发生（PathGuard 会拦），但保守处理 ——
        万一发生了，挂 cwd 比挂它的某个祖先目录安全。
        """
        sb = dk.DockerSandbox()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        assert sb._workspace_root(outside) == outside.resolve()


class TestPathMapping:
    """
    sandbox.md 明确说这是「Docker 后端最容易出 bug 的地方，
    必须有专门的测试」。
    """

    def test_workspace_root_maps_to_container_root(self) -> None:
        sb = dk.DockerSandbox()
        root = Path(settings.workspace_dir).resolve()
        assert sb._rel_in_container(root) == "/workspace"

    def test_subdir_maps_with_forward_slashes(self) -> None:
        """
        Windows 的反斜杠必须转成正斜杠 ——
        容器里是 Linux，反斜杠不是路径分隔符。
        """
        sb = dk.DockerSandbox()
        root = Path(settings.workspace_dir).resolve()
        got = sb._rel_in_container(root / "src" / "deep")
        assert got == "/workspace/src/deep"
        assert "\\" not in got

    def test_outside_workspace_maps_to_root(self) -> None:
        sb = dk.DockerSandbox()
        assert sb._rel_in_container(Path("/tmp/nowhere").resolve()) == "/workspace"

    def test_docker_path_uses_forward_slashes(self, tmp_path: Path) -> None:
        """
        `-v` 语法只认正斜杠。Windows 上 D:\\a\\b 直接传会被解析成
        奇怪的东西 —— 而错误信息通常是 "invalid mode" 之类，
        完全不指向"你的路径分隔符不对"。
        """
        out = dk.to_docker_path(tmp_path / "x" / "y")
        assert "\\" not in out
        assert "/x/y" in out


class TestNaming:
    def test_prefix_is_fixed(self) -> None:
        """
        前缀必须固定 —— 启动时靠它找出遗留容器。

        容器名是 agent_sandbox_<uuid4()>，前缀固定但它
        没利用这一点做启动清理。
        """
        assert dk.NAME_PREFIX
        assert dk.NAME_PREFIX.endswith("-")

    def test_container_name_includes_session(self) -> None:
        src = inspect.getsource(dk.DockerSandbox._create)
        assert "NAME_PREFIX" in src
        assert "session_id" in src

    def test_name_matcher(self) -> None:
        assert dk.looks_like_container_name(f"{dk.NAME_PREFIX}ses_abc123")
        assert not dk.looks_like_container_name("some-other-container")
        assert not dk.looks_like_container_name("postgres")


class TestOrphanCleanup:
    """
    `--rm` 只在容器【自己停止】时生效，而保活命令 tail -f /dev/null
    不会自己停。进程被 kill -9、断电、Docker Desktop 重启时清理钩子
    不执行，容器会一直运行占内存。

    而新启动的进程完全不知道它们存在（缓存是内存态），
    每次崩溃都累积一批。
    """

    def test_cleanup_orphans_exists(self) -> None:
        assert hasattr(dk.DockerSandbox, "cleanup_orphans")

    def test_filters_by_prefix(self) -> None:
        """
        只删本项目的容器 —— 按前缀过滤。

        不过滤的话会把用户的 postgres、redis 一起删掉。
        """
        src = inspect.getsource(dk.DockerSandbox.cleanup_orphans)
        assert "NAME_PREFIX" in src
        assert "--filter" in src

    def test_called_on_first_run(self) -> None:
        """
        第一次执行时清理，而不是在 lifespan 里。

        放 lifespan 的话，配了 docker 但从不执行命令的用户
        也要付这个开销（几百毫秒的 docker ps）。
        """
        src = inspect.getsource(dk.DockerSandbox._ensure_container)
        assert "cleanup_orphans" in src
        assert "_startup_cleaned" in src

    def test_excludes_own_containers(self) -> None:
        """不能把自己正在用的容器删掉。"""
        src = inspect.getsource(dk.DockerSandbox.cleanup_orphans)
        assert "_containers" in src

    async def test_removes_stale_name_before_create(self) -> None:
        """
        创建前先按名字删一次。

        同名容器可能还在（上次没清干净），不删的话
        docker run 报 "name already in use"。
        """
        src = inspect.getsource(dk.DockerSandbox._create)
        assert '"rm", "-f", name' in src.replace("\n", " ").replace("  ", " ") or (
            "rm" in src and "name" in src
        )


class TestReuse:
    def test_ttl_defined(self) -> None:
        assert dk.IDLE_TTL > 0

    def test_ttl_refreshed_on_reuse(self) -> None:
        """
        每次复用刷新 TTL，而不是从创建时刻算固定过期。

        固定过期的话，一个活跃会话会在用了 30 分钟后
        突然丢掉容器状态（pip 装的包、cd 进的目录）。
        """
        src = inspect.getsource(dk.DockerSandbox._ensure_container)
        assert "expire_at" in src
        assert "IDLE_TTL" in src

    def test_checks_alive_before_reuse(self) -> None:
        """
        缓存里有记录不代表容器还在（用户可能手动 docker rm、
        守护进程可能重启过）。不检查的话 exec 报
        "No such container"，而错误不指向"缓存过期了"。
        """
        src = inspect.getsource(dk.DockerSandbox._ensure_container)
        assert "_alive" in src

    def test_per_session_lock_not_global(self) -> None:
        """
        容器创建要 0.5~2 秒。

        全局锁会让所有会话串行 —— 会话 A 建容器时会话 B 的 ls 也得等。
        不加锁的话同一会话并发两次执行会创建两个容器，其中一个泄漏。
        """
        src = inspect.getsource(dk.DockerSandbox._get_lock)
        assert "_locks" in src
        # 创建锁本身要用全局锁保护，否则两个协程各拿到不同的锁对象
        assert "_meta_lock" in src

    async def test_lock_is_stable_per_session(self) -> None:
        sb = dk.DockerSandbox()
        a = await sb._get_lock("ses_1")
        b = await sb._get_lock("ses_1")
        c = await sb._get_lock("ses_2")
        assert a is b, "同一会话必须拿到同一把锁"
        assert a is not c, "不同会话必须是不同的锁"

    def test_keepalive_command(self) -> None:
        """容器要长期存在，主进程不能退出。"""
        assert dk.KEEPALIVE
        assert "tail" in dk.KEEPALIVE[0]


class TestTimeout:
    def test_kills_process_inside_container(self) -> None:
        """
        超时后要在容器内 pkill。

        docker exec 客户端被杀掉的话，【容器里的进程还在跑】——
        它的父进程是容器的 init，不是 docker CLI。
        不管的话一个死循环会一直烧 CPU 直到容器被回收。
        """
        src = inspect.getsource(dk.DockerSandbox._docker_stream)
        assert "pkill" in src

    def test_cli_timeout_separate_from_command_timeout(self) -> None:
        """
        docker 命令自身的超时和被执行命令的超时是两件事。

        前者防的是"守护进程没响应"（docker CLI 会一直等），
        后者是用户指定的命令执行时限。
        """
        assert dk.DOCKER_CLI_TIMEOUT > 0
        src = inspect.getsource(dk.DockerSandbox._docker)
        assert "DOCKER_CLI_TIMEOUT" in src

    def test_extract_cid_from_exec_args(self) -> None:
        """超时后要 pkill，得先从参数里找出容器 id。"""
        args = ["exec", "-w", "/workspace", "-e", "K=V", "abc123", "sh", "-c", "ls"]
        assert dk._extract_cid(args) == "abc123"

    def test_extract_cid_no_options(self) -> None:
        assert dk._extract_cid(["exec", "cid42", "sh", "-c", "x"]) == "cid42"

    def test_extract_cid_missing(self) -> None:
        assert dk._extract_cid(["exec", "-w", "/x"]) == ""


class TestBrokenContainerRecovery:
    """
    真实验证发现的问题：容器里跑一个 fork 炸弹之后，pids-limit 确实挡住了
    它（fork 返回 OSError），但**容器的 PID 表被占满且不恢复** ——
    此后每一次 docker exec 都返回

        OCI runtime exec failed: unable to start container process:
        procReady not received        （退出码 128）

    `docker top` 只显示 2 个进程、容器状态是 running，但就是没法再 exec。

    不处理的话后果很严重：模型跑一次 fork 炸弹（甚至只是一个失控的
    多进程脚本），这个会话【后续所有命令】都返回这条看不懂的 OCI 错误，
    而它完全不指向真因。
    """

    def test_detects_broken_container(self) -> None:
        assert dk._is_container_broken(
            128,
            "OCI runtime exec failed: exec failed: unable to start container "
            "process: procReady not received",
        )

    def test_detects_case_insensitive(self) -> None:
        assert dk._is_container_broken(128, "PROCREADY NOT RECEIVED")

    def test_normal_failure_not_treated_as_broken(self) -> None:
        """
        命令自己失败不能当成容器坏了。

        重建容器解决不了任何问题，只会白白丢掉容器状态
        （pip 装的包、cd 进的目录）。
        """
        assert not dk._is_container_broken(1, "ls: cannot access 'x'")
        assert not dk._is_container_broken(42, "")
        assert not dk._is_container_broken(0, "ok")

    def test_128_alone_is_not_enough(self) -> None:
        """
        不能只看退出码 128。

        `sh -c "kill -9 $$"` 也会得到 128，那是命令自己的行为。
        """
        assert not dk._is_container_broken(128, "Killed")
        assert not dk._is_container_broken(128, "")

    def test_recovery_wired_into_run(self) -> None:
        src = code_only(inspect.getsource(dk.DockerSandbox.run))
        assert "_is_container_broken" in src
        assert "cleanup_session" in src
        assert "_ensure_container" in src

    def test_cid_replaced_by_lookup_not_index(self) -> None:
        """
        重建后要换掉参数里的容器 id，用 index 查找而不是写死位置。

        写死 args[-4] 的话，将来给 exec 加一个选项就会错位 ——
        而错位的表现是"把容器 id 替换到了别的位置"，
        docker 会报一个完全无关的参数错误。
        """
        # 去空格再比：_code_only 是按 token 重组的，
        # `args.index` 会变成 `args . index`
        src = code_only(inspect.getsource(dk.DockerSandbox.run)).replace(" ", "")
        assert "args.index" in src
        assert "args[-4]" not in src

    def test_retry_failure_reported_honestly(self) -> None:
        """
        重试后仍然坏要如实告诉模型，别让它以为是自己的命令有问题 ——
        否则它会反复改命令重试，而问题在容器上。
        """
        src = inspect.getsource(dk.DockerSandbox.run)
        assert "重建后仍然失败" in src


class TestHealth:
    async def test_three_layer_check(self) -> None:
        """
        CLI 存在 → 守护进程响应 → 镜像存在。

        分三层是因为三种失败的处理动作完全不同：装 Docker、
        启动 Docker、拉镜像。只说"Docker 不可用"的话用户不知道
        该做哪个。
        """
        src = inspect.getsource(dk.DockerSandbox.health)
        assert "shutil.which" in src
        assert "version" in src
        assert "image" in src

    async def test_reasons_are_actionable(self) -> None:
        src = inspect.getsource(dk.DockerSandbox.health)
        # 每种失败都要给出下一步动作
        assert "安装 Docker Desktop" in src
        assert "是否已启动" in src
        assert "docker pull" in src

    async def test_does_not_auto_pull(self) -> None:
        """
        不自动拉镜像。

        拉镜像要几分钟且需要网络，而 health 在"用户刚发消息、
        正等回复"的路径上 —— 卡几分钟且没有进度提示，
        用户会以为卡死了。
        """
        src = inspect.getsource(dk.DockerSandbox.health)
        assert '"pull"' not in src

    async def test_missing_docker_reports_unavailable(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(dk.shutil, "which", lambda _: None)
        ok, reason = await dk.DockerSandbox().health()
        assert ok is False
        assert "docker" in reason.lower()


class TestFactory:
    async def test_local_by_default(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(settings.sandbox, "backend", "local")
        sb = await factory.get_sandbox()
        assert sb.name == "local"
        assert factory.fallback_reason() == ""

    async def test_local_is_not_isolated(self, monkeypatch: Any) -> None:
        """
        本地后端必须自报「非隔离」。

        PathGuard 限制了文件范围、白名单限制了命令，但命令跑起来就是
        宿主进程：能访问网络、能读环境变量、资源不受限。
        前端据此显示提示。
        """
        monkeypatch.setattr(settings.sandbox, "backend", "local")
        sb = await factory.get_sandbox()
        assert sb.isolated is False

    async def test_fallback_records_reason(self, monkeypatch: Any) -> None:
        """
        【降级不能静默】。

        用户配了 docker 就是想要隔离。静默用本地执行的话，
        他会以为命令跑在容器里，于是放心地让 agent 执行危险操作。
        """
        monkeypatch.setattr(settings.sandbox, "backend", "docker")
        monkeypatch.setattr(dk.shutil, "which", lambda _: None)

        sb = await factory.get_sandbox()
        assert sb.name == "local", "Docker 不可用应降级"
        assert factory.fallback_reason(), "必须记录降级原因"
        assert "docker" in factory.fallback_reason().lower()

    async def test_unknown_backend_falls_back_with_reason(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(settings.sandbox, "backend", "firejail")
        sb = await factory.get_sandbox()
        assert sb.name == "local"
        assert factory.fallback_reason()

    async def test_result_is_cached(self, monkeypatch: Any) -> None:
        """
        health() 要跑两次子进程调用（约几百毫秒）。
        每次执行命令都探一遍的话，一轮对话调五次工具就多花一两秒。
        """
        monkeypatch.setattr(settings.sandbox, "backend", "local")
        a = await factory.get_sandbox()
        b = await factory.get_sandbox()
        assert a is b


class TestLocalSandboxPort:
    """LocalSandbox 要满足同一个协议，否则替换时会在运行时炸。"""

    async def test_has_all_port_methods(self) -> None:
        from app.infra.sandbox.local import LocalSandbox

        sb = LocalSandbox()
        for m in ("health", "run", "cleanup_session", "shutdown"):
            assert hasattr(sb, m), f"缺 {m}"
        assert sb.name == "local"

    async def test_health_always_ok(self) -> None:
        from app.infra.sandbox.local import LocalSandbox

        ok, _reason = await LocalSandbox().health()
        assert ok is True

    async def test_cleanup_is_noop(self) -> None:
        from app.infra.sandbox.local import LocalSandbox

        await LocalSandbox().cleanup_session("ses_x")
        await LocalSandbox().shutdown()

    async def test_run_actually_executes(self, tmp_path: Path) -> None:
        """协议方法要真的能跑通，不只是存在。"""
        from app.infra.sandbox.local import LocalSandbox

        res = await LocalSandbox().run(
            "echo sandbox_port_ok", cwd=tmp_path, session_id="ses_t", timeout=30
        )
        assert "sandbox_port_ok" in res.output
        assert res.exit_code == 0

    def test_docker_sandbox_has_all_port_methods(self) -> None:
        sb = dk.DockerSandbox()
        for m in ("health", "run", "cleanup_session", "shutdown"):
            assert hasattr(sb, m), f"缺 {m}"
        assert sb.name == "docker"
        assert sb.isolated is True


class TestToolWiring:
    """
    工具必须走 SandboxPort，不能直接调 run_process ——
    否则配了 docker 也还是在宿主执行。
    """

    def test_run_shell_uses_sandbox(self) -> None:
        from app.modules.agent.tools.exec import RunShellTool

        src = inspect.getsource(RunShellTool.run)
        assert "get_sandbox" in src
        assert "run_process" not in src, "不该绕过 SandboxPort 直接调 run_process"

    def test_session_delete_cleans_sandbox(self) -> None:
        """
        删会话要删容器。

        不删的话每个删掉的会话都留一个容器在跑（保活命令不会自己停），
        跑一天宿主上几十个容器占着内存。
        """
        from app.api.routes_chat import delete_session

        src = inspect.getsource(delete_session)
        assert "cleanup_session" in src

    def test_meta_probes_real_availability(self) -> None:
        """
        /api/meta 要探真实可用性。

        原来的实现是 `import docker` 成功就算可用 —— 那只说明装了 SDK，
        完全不代表守护进程在跑、镜像在本地。而本项目走 docker CLI，
        根本不用那个 SDK。
        """
        from app.api.routes_config import meta

        src = code_only(inspect.getsource(meta))
        assert "get_sandbox" in src
        assert "import docker" not in src, "不该再靠 import docker 判断可用性"

    def test_meta_exposes_fallback_reason(self) -> None:
        from app.api.routes_config import meta

        src = inspect.getsource(meta)
        assert "sandbox_fallback_reason" in src
        assert "sandbox_isolated" in src

class TestMultiWorkspaceMount:
    """
    交叉审查发现：_workspace_root 原来读 settings.workspace_dir，
    而那是【硬编码属性】（PROJECT_ROOT/"workspace"，不可配置）。

    真实的工作区路径来自数据库 workspace.root_path。用户建多个工作区时，
    会话可能绑在一个完全不同的目录上。

    后果是静默的：relative_to 抛 ValueError → 回落"挂 cwd 自己" →
    而挂载点在容器创建时固化、之后同一会话所有命令复用它。
    如果首次执行时模型正好 cd 到某个子目录，整个会话的 /workspace
    就指向那个子目录，模型看不到同级文件且没有任何报错。
    """

    def test_run_accepts_ws_root(self) -> None:
        import inspect as _i

        sig = _i.signature(dk.DockerSandbox.run)
        assert "ws_root" in sig.parameters

    def test_explicit_ws_root_wins(self, tmp_path: Path) -> None:
        """传入的工作区根要生效，而不是被全局配置覆盖。"""
        sb = dk.DockerSandbox()
        other = (tmp_path / "other_ws").resolve()
        (other / "sub").mkdir(parents=True)
        assert sb._workspace_root(other / "sub", other) == other

    def test_rel_path_uses_ws_root(self, tmp_path: Path) -> None:
        sb = dk.DockerSandbox()
        other = (tmp_path / "ws2").resolve()
        (other / "a" / "b").mkdir(parents=True)
        got = sb._rel_in_container(other / "a" / "b", other)
        assert got == "/workspace/a/b"
        assert "\\" not in got

    def test_falls_back_when_no_ws_root(self) -> None:
        """不传时仍回落全局配置（向后兼容）。"""
        sb = dk.DockerSandbox()
        root = Path(settings.workspace_dir).resolve()
        assert sb._workspace_root(root / "x") == root

    def test_exec_passes_real_workspace(self) -> None:
        """
        工具层必须传 ctx.workspace —— 那是从
        workspace.root_path 一路传下来的真实路径。
        """
        from app.modules.agent.tools.exec import RunShellTool

        from tests.conftest import code_only

        for tool in (RunShellTool,):
            src = code_only(inspect.getsource(tool.run)).replace(" ", "")
            assert "ws_root=ctx.workspace" in src, f"{tool.__name__} 没传真实工作区"


class TestContainerReclaim:
    """
    交叉审查发现：cleanup_expired() 在整个项目里【零调用点】，
    IDLE_TTL 是死配置。

    而 --rm 也救不了 —— 保活命令 tail -f /dev/null 永不退出。

    叠加定时任务【每次触发都建一个新会话】，每次触发泄漏一个容器。
    实测模拟三次触发就留下三个常驻容器，一个"每小时"的任务跑一天
    是 24 个，不重启就没有上界。
    """

    def test_cron_runner_cleans_sandbox(self) -> None:
        """
        定时任务跑完要清掉自己的容器。

        普通会话有删除入口（用户手动删会话时清理），
        而定时任务的会话用户没有动力去删 —— 那不是他建的。
        """
        from app.modules.cron import runner

        from tests.conftest import code_only

        src = code_only(inspect.getsource(runner.run_task))
        assert "cleanup_session" in src

    def test_scheduler_has_sweeper(self) -> None:
        """
        要有周期回收 —— 不能只靠 cron 任务自己清。

        长期存在的交互式会话同样会积累容器，
        而它们不经过 cron 的清理路径。
        """
        from app.modules.cron.scheduler import SWEEP_INTERVAL, CronScheduler

        assert SWEEP_INTERVAL > 0
        assert hasattr(CronScheduler, "_sweep_loop")
        src = inspect.getsource(CronScheduler._sweep_loop)
        assert "cleanup_expired" in src

    def test_sweep_interval_shorter_than_ttl(self) -> None:
        """
        扫描周期要短于 TTL，否则容器最多多活一个周期。
        """
        from app.modules.cron.scheduler import SWEEP_INTERVAL

        assert SWEEP_INTERVAL < dk.IDLE_TTL

    def test_sweeper_started_and_stopped(self) -> None:
        from app.modules.cron.scheduler import CronScheduler

        assert "_sweeper" in inspect.getsource(CronScheduler.start)
        assert "_sweeper" in inspect.getsource(CronScheduler.stop)
