# 沙箱

## 为什么做可插拔而不是单一方案

 只做 Docker 沙箱：隔离性好，但强依赖 Docker Desktop，首次启动要拉镜像，在个人机器上偏重 —— 很多时候只是想让 agent 跑个 `pytest`，为此启动一个容器不划算。

 只做本地执行 + 人工审批：轻，启动即用，但没有真隔离 —— 一条 `rm -rf` 通过审批就真的删了。

两者都对，只是适用场景不同。所以定义统一接口，两个实现，**默认本地，需要时切 Docker**。

不装 Docker 也能全功能使用；装了就能开真隔离。

## SandboxPort

```python
class SandboxPort(Protocol):
    async def run_shell(
        self, command: str, cwd: Path, timeout: int, env: dict | None = None,
    ) -> ExecResult: ...

    async def run_python(
        self, code: str, cwd: Path, timeout: int,
    ) -> ExecResult: ...

    async def health(self) -> bool: ...      # 是否可用
```

```python
@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool          # 输出是否被截断
    timed_out: bool
```

文件读写**不走沙箱**。文件工具直接经 `PathGuard` 校验后用 Python 标准库操作。理由：文件操作的风险由路径白名单控制已经足够，套一层沙箱只是让 `read_file` 变慢，且 Docker 沙箱下还要处理挂载路径映射。

## LocalSandbox（默认）

子进程执行。四道防线：

### 1. 工作目录限制

`cwd` 必须在工作区内，经 `PathGuard` 校验。命令在这个目录下执行。

这不能阻止命令自己 `cd /` 出去 —— 它只是让默认位置安全。真正的边界靠下面三条和审批。

### 2. 超时

```python
timeout_default = 120        # 秒
timeout_max = 600
```

超时后 **kill 整个进程组**，不是只 kill 直接子进程：

```python
# Windows: CREATE_NEW_PROCESS_GROUP + taskkill /T
# POSIX:   start_new_session=True + os.killpg
#
# 只 kill 直接子进程的话，`npm test` 这类命令会留下一堆孤儿进程，
# 它们继续占端口和 CPU，下次执行时报"端口已被占用"，而看不出原因。
```

### 3. 输出截断

```python
max_output_chars = 30_000
```

超出时保留头部 10K + 尾部 20K，中间替换为 `...（已省略 N 字符）...`。

**尾部留得比头部多**，因为报错信息通常在末尾。反过来会导致关键的 traceback 被截掉。

完整输出存进 `ToolResult.display`，前端可展开看全部。

### 4. 审批

`manual` 模式（默认）下每次执行都等人确认。见 [tools.md](tools.md#审批机制)。

### 危险命令识别

不做黑名单拦截（黑名单永远绕得过去），但做**风险标注**：审批弹框里高亮显示匹配到的风险模式，帮用户快速判断。

```python
RISKY_PATTERNS = [
    (r"\brm\s+(-[rRf]+\s+)*/", "删除根目录相关路径"),
    (r"\bdel\s+/[sqf]", "Windows 递归删除"),
    (r"Format-Volume|diskpart", "磁盘操作"),
    (r"\bcurl\b.*\|\s*(bash|sh)", "下载并直接执行脚本"),
    (r"\bgit\s+push\s+.*--force", "强制推送"),
    (r"(shutdown|reboot)\b", "关机/重启"),
    (r">\s*/dev/sd", "写块设备"),
]
```

标注而非拦截，因为有些场景确实需要（比如在测试目录里 `rm -rf`）。判断留给人。

### env 处理

```python
# 不继承完整的父进程环境。只传必要的：PATH、HOME/USERPROFILE、语言环境、
# 以及用户在设置里显式声明要传的变量。
#
# 完整继承会把本项目的 .env（含 API Key 明文）暴露给子进程 ——
# 一句 `env` 或 `printenv` 就能读到，而这个输出会进模型上下文。
```

`ENCRYPTION_KEY`、任何 `*_API_KEY`、`*_TOKEN` 一律不传。

## DockerSandbox（可选）

容器内执行，真隔离。

### 配置

```
JEEVES_SANDBOX__BACKEND=docker
JEEVES_SANDBOX__IMAGE=python:3.12-slim
JEEVES_SANDBOX__NETWORK=none          # none | bridge
JEEVES_SANDBOX__MEMORY_LIMIT=2g
JEEVES_SANDBOX__CPU_LIMIT=2
JEEVES_SANDBOX__PIDS_LIMIT=512
```

`pids_limit` 是 `memory_limit` 挡不住的那一类：fork 炸弹的每个进程都很小，靠数量而不是内存把系统压垮。

实际下发的 `docker run` 参数（与 差异）：

| 参数 | 作用 |
| --- | --- |
| `--network none` | 默认无网络。 用 `--network host`，那样容器能打宿主 localhost（含 agent 自己的 API）、内网、`169.254.169.254` |
| `--memory` / `--cpus` / `--pids-limit` |  完全没有这三项 |
| `--cap-drop ALL` | 去掉 `CAP_SETUID` 等全部 capability |
| `--security-opt no-new-privileges` | 禁止提权 |
| `-v <workspace>:/workspace` | **只挂工作区**，不挂项目目录 |

不加 `--user`：镜像里 `pip install` 要写 site-packages，改 uid 会让常见操作失败。容器 root + `cap-drop ALL` 已经堵掉大部分提权路径。

### 挂载点用真实工作区，不读全局配置

`settings.workspace_dir` 是硬编码的 `PROJECT_ROOT/workspace`，而真实工作区路径来自数据库 `workspace.root_path`。

用户建多个工作区时两者不同，读全局配置会让挂载点落到"挂 cwd 自己"的回退分支。而挂载点在容器创建时固化、之后同一会话所有命令复用它 —— 如果首次执行时模型正好 `cd` 到某个子目录，整个会话的 `/workspace` 就指向那个子目录，模型看不到同级文件且**没有任何报错**。

所以 `run()` 接受 `ws_root` 参数，工具层传 `ctx.workspace`（那是从 `workspace.root_path` 一路传下来的）。全局配置只作为回落。

### 容器回收

`IDLE_TTL` 和 `cleanup_expired()` 一直存在，但原来**没有任何东西调用它们** —— TTL 是死配置，而 `--rm` 因保活命令永不触发。

现在两处兜：定时任务跑完清自己的会话，调度器每 5 分钟扫一次过期容器。

### 容器损坏后自动重建

实测发现：容器里跑 fork 炸弹后，`pids-limit` 确实保护了宿主，但**容器的 PID 表被占满且不恢复** —— 此后每次 `docker exec` 都返回 `procReady not received`（退出码 128）。

不处理的话，该会话后续所有命令全废，而错误完全不指向真因。所以检测到这个特征时自动删容器重建并重试一次。

判断不能只看退出码 128（`kill -9 $$` 也是 128），必须同时匹配 stderr 特征。详。

`network=none` 是默认。需要装包时用户临时改成 `bridge`。

### 容器生命周期

**每个会话一个长期容器**，不是每次执行起一个新的。

理由：每次新建容器的话，`pip install` 装的包下次就没了，`cd` 进的目录也丢了。而一次容器启动约 0.5~2 秒，每个工具调用都付这个代价太贵。

```
会话首次执行时创建容器 → 挂载工作区 → 保持运行
会话空闲超过 30 分钟 → 停止并删除容器
会话删除 → 立即清理容器
服务启动时 → 清理所有遗留的本项目容器（上次崩溃留下的）
```

容器命名 `jeeves-<session_id>`，便于识别和清理。

### 工作区挂载

```
宿主 workspace/  →  容器 /workspace   (rw)
```

只挂工作区，不挂项目目录 —— 否则容器里能改 agent 自己的代码和读 `.env`。

路径映射：宿主的 `D:\...\workspace\src\main.py` 在容器里是 `/workspace/src/main.py`。**工具返回给模型的路径必须是宿主路径**，否则模型下一轮用容器路径去调 `read_file`（走宿主文件系统）就找不到。

这个双向转换是 Docker 后端最容易出 bug 的地方，必须有专门的测试。

### 降级

```python
async def get_sandbox() -> SandboxPort:
    if settings.sandbox.backend == "docker":
        docker = DockerSandbox()
        if await docker.health():
            return docker
        # 检测不到 Docker 不能静默回落 —— 用户配了 docker 就是想要隔离，
        # 静默用本地执行等于骗他。
        logger.warning("docker_unavailable_fallback_to_local")
        await emit("sandbox_fallback", from_="docker", to="local",
                   reason="Docker 守护进程不可用")
    return LocalSandbox()
```

前端收到 `sandbox_fallback` 事件显示一条明显的提示条，且**在该会话内持续显示**（不是一闪而过的 toast）—— 用户需要一直知道当前不是隔离环境。

## run_python 的实现

不用 `python -c "code"`。代码里的引号、换行、非 ASCII 在跨平台命令行拼接时会出各种问题。

做法：把代码写到工作区下的临时文件 `.jeeves/tmp/exec_<uuid>.py`，然后 `python <file>`。执行完删除。

```python
# 临时文件放工作区内而非系统 temp：
# 1. Docker 后端下系统 temp 不在挂载范围内
# 2. 代码里可能有相对路径 open("data.csv")，工作区内才能找到
```

`.jeeves/` 目录加进 `.gitignore` 模板，且从 `glob` / `grep` 工具的默认搜索范围里排除。

## 交互式命令

沙箱不支持交互输入。`stdin` 一律给 `/dev/null`（或 Windows 的等价物）。

某些命令会因此挂住等输入（`git commit` 无 `-m`、`npm init` 无 `-y`、`apt install` 无 `-y`）。这些靠超时兜住，并在超时的错误信息里提示：

```
命令执行超时（120s）。若该命令需要交互输入，请改用非交互形式
（如 git commit -m "..."、npm init -y、pip install 加 --quiet）。
```

这条提示能让模型自己纠正，比单纯报"超时"有用得多。
