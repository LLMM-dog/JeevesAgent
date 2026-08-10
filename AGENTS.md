# AGENTS.md

## 常用命令

```bash
# 安装全部依赖（含可选组）
uv sync --extra dev --extra mcp --extra search --extra web --extra cron

# 后端
uv run ruff check backend scripts       # lint（阻断 CI）
uv run ruff check --fix backend scripts # lint 自动修复
uv run mypy backend                     # 类型检查（有存量告警，不阻断）
uv run pytest backend/tests -q          # 全部测试（~1250 个）
uv run pytest backend/tests/path/test_x.py::test_name  # 单个测试
uv run python backend/app/main.py       # 直接启动后端

# 前端
cd frontend && npm ci              # 装依赖（锁版本，CI 用）
cd frontend && npm test            # vitest run（30 个测试）
cd frontend && npx tsc --noEmit   # 类型检查

# 启动（开发模式 → http://127.0.0.1:5173）
start.bat -Dev            # Windows
./start.sh                # macOS/Linux

# 数据库
uv run alembic revision --autogenerate -m "描述"  # 生成迁移
uv run alembic upgrade head                        # 手动跑迁移（启动时会自动跑）
```

## 测试必须设置的环境变量

后端配置层在 import 时就需要解密密钥，没有它测试连 `pytest` 都启不动：

```bash
$env:JEEVES_SECURITY__ENCRYPTION_KEY="xr9i7PEx3aHu2bTacB0tEG1VnCmkvlHJu2fZ-7XQDWs="  # PowerShell
export JEEVES_SECURITY__ENCRYPTION_KEY="xr9i7PEx3aHu2bTacB0tEG1VnCmkvlHJu2fZ-7XQDWs=" # bash
```

## 架构要点

- **后端入口**: `backend/app/main.py` — `create_app()` 建 FastAPI 实例，lifespan 里跑日志初始化 → 配置校验 → 自动迁移 → 注册工具 → MCP 连接 → 定时任务。
- **代理循环已切为纯 while**: 虽然依赖里有 LangGraph，但 agent loop 不再用 LangGraph 的 StateGraph，在 `backend/app/modules/agent/loop.py` 里手动实现。
- **配置集中在 `backend/app/core/config.py`**：pydantic-settings，所有 env var 必须带 `JEEVES_` 前缀（不带的会被本机无关环境变量覆盖，已踩过坑）。模块路径从 `PROJECT_ROOT = Path(__file__).resolve().parents[3]` 推导。
- **包结构**: `backend/app/core/`（基础）→ `backend/app/infra/`（LLM/DB/沙箱端口）→ `backend/app/modules/`（业务模块）。不要往 core 里放业务逻辑。

## 依赖钉死 + lockfile

`pyproject.toml` 里所有依赖都是精确版本（`==`，不是 `>=`），配合提交 `uv.lock`。加了新依赖之后必须同时更新 `uv.lock`（`uv sync` 会自动做）。不要引入范围版本。

## Alembic 迁移注意事项

- `script_location = backend/migrations`，`prepend_sys_path = backend`
- SQLite 不支持 ALTER/DROP COLUMN → env.py 里 `render_as_batch=True`
- 迁移文件名带时间戳（`%Y%m%d_%H%M_rev_slug.py`），autogenerate 自动处理
- 迁移文件被 ruff/mypy 豁免（`per-file-ignores`）
- 启动时 `_run_migrations(embedded=True)` 跳过 `fileConfig`，避免覆盖 structlog
- 新建迁移后必须同时 import 对应模块到 `env.py`，否则 autogenerate 会误删未注册的表
- 加列必须带 `server_default`（SQLite 要求），`downgrade()` 必须实现

## 代码风格

- **中文注释和文档，英文的代码标识符和命令**
- 行长 120（ruff），非默认的 88。原因：SQLAlchemy 列定义和 FastAPI Depends() 天然长
- ruff 规则: `E, F, I, N, W, UP, B, ASYNC`，不启用 `E501`（由行长 120 覆盖）
- mypy: `strict = true`
- FastAPI 路由文件允许 `B008`（参数默认值里调 `Depends()` 是框架惯例）
- 禁止在 async 函数里调阻塞 I/O（ASYNC 规则组），用 aiosqlite 而非 sqlite3

## .gitignore 重点

以下**不在仓库**，agent 不应读它们的内容：`.env`、`.env.verify`、`data/`、`workspace/`、`personas/SOUL.md`、`personas/USER.md`、`personas/AGENTS.md`、`config/*.yaml`（除 `*.example.yaml`）。MCP 配置里有 token，不能进仓库。

### 人格文件说明

`personas/AGENTS.md` 是**给 Jeeves agent 自己的行为规则**（运行时注入系统提示词），不是给本仓库开发者看的。它被 gitignore 掉了。仓库里只有 `personas/*.example.md`，首次启动时复制到 `.md` 版。
