# 环境搭建

## 前置要求

| 项 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.11+ | 用 `Self` 类型、`ExceptionGroup` 等特性 |
| uv | 最新 | 包管理。比 pip 快，锁文件可靠 |
| Node.js | 18+ | 前端 |
| Docker | 可选 | 只有开 Docker 沙箱才需要 |

选 uv 而非 poetry/pdm：装依赖快一个数量级，`uv.lock` 跨平台可复现，且能直接管 Python 版本本身。

## 一键初始化

```powershell
# Windows
python scripts\setup.py
# 或双击 start.bat（首次会自动初始化）

# macOS / Linux
python scripts/setup.py
```

`setup.py` 的六步（参考 引导式设计）：

| 步 | 内容 | 交互 |
| --- | --- | --- |
| 1/6 | 检查 Python / Node 版本 | 自动 |
| 2/6 | `uv sync` 装后端依赖 | 自动 |
| 3/6 | `npm install` 装前端依赖 | 自动 |
| 4/6 | 生成 `.env`，**自动生成 `ENCRYPTION_KEY`** | 自动 |
| 5/6 | **配置模型**：填 Base URL + Key → 自动拉模型列表 → 勾选 | **手动** |
| 6/6 | 填你的称呼，生成 `personas/USER.md` | **手动** |

### 第 4 步必须自动生成 ENCRYPTION_KEY

```python
from cryptography.fernet import Fernet
key = Fernet.generate_key().decode()
```

不能让用户手填——他会填一个短字符串然后遇到"Fernet key must be 32 url-safe base64-encoded bytes"这种错误，且完全不知道怎么办。

**生成后要提示用户备份**：这个 key 丢了，已存的 API Key 全部无法解密（只能重新填）。

### 第 5 步复用 probe 逻辑

setup 脚本和 Web 设置页调**同一个** service 函数，不各写一遍。

```python
from app.modules.provider.service import probe_models, normalize_base_url
```

否则两处的规范化规则、错误处理会逐渐分叉，用户在 setup 里能连上而在 Web 里连不上（或反之）。

## 手动搭建

```bash
# 后端
uv sync --extra dev
cp .env.example .env
# 编辑 .env，至少填 JEEVES_SECURITY__ENCRYPTION_KEY
# 注意变量名带 JEEVES_ 前缀，原因见下方"为什么变量名带前缀"

# 前端
cd frontend
npm install
```

## 启动

```bash
# Windows — 双击 start.bat，或命令行：
start.bat                    # 生产模式（推荐）
start.bat -Dev               # 开发模式（改代码即时生效）
start.bat -BackendOnly       # 只起后端

# macOS / Linux
./start.sh --prod            # 生产模式
./start.sh                   # 开发模式
./start.sh --backend-only    # 只起后端
PORT=8080 ./start.sh         # 换端口
```

`start.ps1` 已移到 `scripts/`，用户不需要直接碰它。

### 启动脚本做了哪些检查

| 检查 | 不做的后果 |
| --- | --- |
| uv 存在 | `uv: command not found`，用户不知道要装 uv |
| `.env` 存在 | 后端启动后所有配置回落默认值，表现为"配置没生效" |
| `ENCRYPTION_KEY` 非空 | 后端拒绝启动，但用户不知道怎么生成合法的 Fernet key |
| **端口未被占用** | uvicorn 报 `Address already in use` 后退出，而脚本只说"后端启动失败" |
| 前端依赖已装 | vite 报一堆模块找不到 |

端口检查是实测加上的：上次脚本被强杀（任务管理器、直接关终端）时 `finally` / `trap` 不会执行，uvicorn 会变成孤儿继续占端口。检测到占用时会问是否清理。

退出时脚本会**递归杀掉整棵进程树**并按端口兜底清理一次 —— 实测进程树是 `powershell → uv → python → uvicorn`，只杀顶层会留下 uvicorn 占着端口。

分别启动：

```bash
# 后端（开发模式，热重载）
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 9000 --app-dir backend

# 前端
cd frontend && npm run dev
```

访问 `http://localhost:5173`。

生产模式（前端构建后由后端托管，只需一个进程）：

```bash
cd frontend && npm run build
uv run uvicorn app.main:app --host 127.0.0.1 --port 9000 --app-dir backend
```

访问 `http://localhost:9000`。

### 为什么默认端口是 9000 而不是 8000

Windows 上 Hyper-V / WSL 会预留大段动态端口。本机实测 **7905-8928 整段不可用**，8000 就在里面：

```powershell
netsh interface ipv4 show excludedportrange protocol=tcp
```

被预留时的报错很有误导性——启动日志会先打完 `Application startup complete`（lifespan 全部跑完），只在最后一步 bind 失败：

```
ERROR: [Errno 13] error while attempting to bind on address ('127.0.0.1', 8000):
以一种访问权限不允许的方式做了一个访问套接字的尝试
```

容易误以为是应用自身的问题。改端口即可，或用上面那条命令确认哪些段可用。

### 为什么变量名带 JEEVES_ 前缀

配置分组名是 `app` / `agent` / `llm` / `security` 这类通用词。不加前缀时，**系统里任意同名环境变量都会覆盖整个分组**。

实测：本机存在一个与本项目无关的 `AGENT=1`，导致启动直接崩在

```
ValidationError: Input should be a valid dictionary or instance of AgentConfig,
input_value=1, input_type=int
```

报错指向本项目的配置类，但真正原因是一个外部环境变量。加 `JEEVES_` 前缀隔离掉这类冲突。

## 常见启动问题

按遇到频率排序。

### "ENCRYPTION_KEY 缺失，拒绝启动"

`.env` 里没填 `JEEVES_SECURITY__ENCRYPTION_KEY`。生成一个：

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 配置全是默认值 / "我的会话全没了"

**`.env` 没被加载，或数据库路径解析错了。**

两者都是同一个原因：路径按进程 cwd 解析。检查启动日志里打出的两行：

```
config loaded from: D:\proj\jeeves\.env
database: D:\proj\jeeves\data\jeeves.db
```

如果路径不对，说明 `config.py` 里的 `parents[N]` 数字与实际目录层级不匹配。见 [../architecture/data-files.md](../architecture/data-files.md#env-路径必须绝对)。

**这是本项目最隐蔽的一类问题**，所以两个路径必须在启动日志里打出来。

### 对话一直转圈然后失败

按顺序查：

1. 有没有绑定 chat 位模型？→ `GET /api/meta` 看 `has_chat_model`
2. 系统代理干扰？→ 确认 `JEEVES_LLM__TRUST_ENV=false`。开着 Clash 时长生成会被掐断，实测走代理 32s 失败、绕过 252s 成功
3. 超时太短？→ `JEEVES_LLM__REQUEST_TIMEOUT=300`。推理模型可能 200s+ 不吐字节
4. 日志里的 `X-Request-Id` 对应的完整记录

### 前端流式没反应但后端在跑

查这三个，都是实际会犯的错：

1. 前端用了 `EventSource`？→ 必须用 `fetch`，`EventSource` 只能发 GET
2. 前端发的是 GET？→ 必须 POST。**这里最容易错**
3. 控制台有 `[sse] 未处理的事件` 警告？→ 后端加了事件前端没跟上

### "database is locked"

SQLite 写锁冲突。检查：

1. `PRAGMA journal_mode=WAL` 有没有生效
2. 有没有长事务包住整个 run（应该逐条消息独立短事务）
3. 是不是开了两个后端进程

### 中文流式输出偶发乱码

`TextDecoder.decode()` 没传 `{ stream: true }`。见 [../architecture/frontend-sse.md](../architecture/frontend-sse.md#三个必须注意的细节)。

### Docker 沙箱不生效

前端顶部应该有常驻警示条说明降级原因。检查 `docker info` 能否执行。

## 关键依赖

`pyproject.toml` 的直接依赖钉精确版本（`==`），不用 `>=`。

```toml
[project]
requires-python = ">=3.11"
dependencies = [
    "fastapi==0.115.6",
    "uvicorn[standard]==0.34.0",
    "python-multipart==0.0.20",
    "pydantic==2.10.5",
    "pydantic-settings==2.7.1",
    "sqlalchemy[asyncio]==2.0.37",
    "aiosqlite==0.20.0",
    "alembic==1.14.0",
    "langgraph==0.2.62",
    "httpx==0.28.1",
    "tiktoken==0.8.0",
    "cryptography==44.0.0",
    "structlog==24.4.0",
    "pyyaml==6.0.2",
]

[project.optional-dependencies]
docker = ["docker==7.1.0"]
mcp = ["mcp==1.9.4"]
search = ["tavily-python==0.5.0", "ddgs==9.14.4"]
cron = ["croniter==6.2.4", "tzdata==2026.2; sys_platform == 'win32'"]
web = ["markdownify==0.14.1", "beautifulsoup4==4.12.3", "readability-lxml==0.8.4.1"]
dev = ["pytest==8.3.4", "pytest-asyncio==0.25.2", "ruff==0.9.2", "mypy==1.14.1", "types-PyYAML==6.0.12.20250822"]
```

### 为什么钉精确版本

个人项目最怕"上周还能跑，今天装完就挂了"。范围版本下 `uv sync` 在新机器上会拉到更新的依赖，而这个更新可能有破坏性变更（LangGraph 在 0.2.x 内就改过 API）。

`uv.lock` 提交进 git。精确版本 + 锁文件双重保险。

### 可选依赖必须明确报错

```python
try:
    import docker
except ImportError:
    raise SandboxError(
        "Docker 沙箱需要额外依赖。运行：uv sync --extra docker"
    ) from None
```

**不静默降级。** 用户配了 `JEEVES_SANDBOX__BACKEND=docker` 就是想要隔离，静默用本地执行等于骗他。

（注意：这与"检测不到 Docker 守护进程时降级"是两回事。缺依赖是配置错误，应报错；守护进程不可用是环境问题，降级但发 `sandbox_fallback` 事件并常驻警示。）

## 工具链

```bash
uv run ruff check backend           # lint
uv run ruff check --fix backend     # lint 自动修复
uv run mypy backend                 # 类型检查
uv run pytest backend/tests -q      # 测试

cd frontend
npm run lint
npm run typecheck
npm run build                       # 构建也是一种类型检查
```

提交前跑：`ruff check && mypy && pytest`（后端）+ `npm run typecheck && npm test`（前端）。

前端测试用 vitest + jsdom（`npm test`）。目前只覆盖语音输入 —— 那部分的分支几乎全是"出错时怎么办"（不支持、权限被拒、服务自己断开、组件卸载），而这些在 Chrome 上手工点是点不出来的。UI 渲染本身不测，投入产出比太低。

不配 pre-commit hook。个人项目里 hook 经常在赶时间时被 `--no-verify` 绕过，然后就形同虚设。改为在 `scripts/check.ps1` 里串起来手动跑。
