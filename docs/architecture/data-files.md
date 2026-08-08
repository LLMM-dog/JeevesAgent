# 运行时文件布局

## 完整目录

```
jeeves/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── core/
│   │   ├── infra/
│   │   ├── modules/
│   │   └── prompts/              # 提示词 .md，随代码走 git
│   ├── migrations/               # alembic
│   ├── tests/
│   └── alembic.ini
├── frontend/
│   ├── src/
│   ├── index.html
│   ├── package.json
│   └── vite.config.ts
├── docs/                         # 本文档
├── scripts/
│   ├── setup.py                  # 一键初始化
│   ├── start.sh                  # macOS / Linux 启动入口
│   └── start.bat                 # Windows 双击即用（首次自动初始化）
│
├── skills/                       # 技能包（git 管，可选择性提交）
│   └── <skill-name>/
│       ├── SKILL.md
│       ├── references/
│       ├── scripts/
│       └── assets/
├── macros/                       # 宏（git 管）
│   └── <macro-name>/MACRO.md
├── personas/                     # 人设
│   ├── SOUL.example.md           # 提交
│   ├── USER.example.md           # 提交
│   ├── AGENTS.md                 # 提交（行为规则，属于项目资产）
│   ├── SOUL.md                   # gitignore
│   └── USER.md                   # gitignore
│
├── config/                       # 运行时配置，全部 gitignore
│   ├── mcp_servers.yaml
│   └── mcp_servers.example.yaml  # 这个提交
│
├── workspace/                    # 默认工作区，gitignore
│   └── .jeeves/                  # agent 的临时目录
│       └── tmp/                  # run_python 的临时脚本
│
├── data/                         # 全部 gitignore
│   ├── jeeves.db
│   ├── jeeves.db-wal
│   ├── jeeves.db-shm
│   ├── uploads/<YYYYMM>/
│   └── logs/
│
├── .env                          # gitignore
├── .env.example                  # 提交，注释详细
├── pyproject.toml
├── uv.lock
└── .gitignore
```

## 什么进 git，什么不进

判据：**换一台机器 clone 下来，能不能跑起来 + 会不会泄露隐私。**

| 路径 | git | 理由 |
| --- | --- | --- |
| `skills/` | 进 | 技能是项目资产。下载的第三方技能可自行决定是否提交 |
| `macros/` | 进 | 个人工作流，想同步就提交 |
| `personas/AGENTS.md` | 进 | 行为规则是项目设计的一部分 |
| `personas/SOUL.md` `USER.md` | **不进** | 含个人信息 |
| `personas/*.example.md` | 进 | 新环境的初始模板 |
| `config/mcp_servers.yaml` | **不进** | 含 token |
| `config/*.example.yaml` | 进 | 模板 |
| `workspace/` | **不进** | 工作数据 |
| `data/` | **不进** | 数据库、上传文件、日志 |
| `.env` | **不进** | 密钥 |
| `.env.example` | 进 | 且注释要详细到能照着填 |
| `backend/app/prompts/` | 进 | 提示词是代码的一部分 |

## 为什么提示词在 backend/app/prompts/ 而人设在根目录 personas/

两者性质不同：

- `prompts/` 是**项目内置的**系统提示词（压缩提示词、标题生成提示词、各智能体的基础人格），随代码演进，用户一般不改。放包内，用 `importlib.resources` 或相对 `__file__` 读取。
- `personas/` 是**用户的**，在线可编辑，改了立即生效。放项目根，便于用户直接用编辑器打开。

数据库里的 prompt 覆盖机制：**不做。** 这类表只在多用户场景下有意义（管理员改提示词且需审计）。个人项目直接改文件 + git diff 就够了，加一层覆盖反而让"当前生效的是哪个版本"变模糊。

## .env 结构

用 `env_nested_delimiter="__"`，两个下划线表示嵌套：

```ini
# 服务
JEEVES_APP__HOST=127.0.0.1
JEEVES_APP__PORT=9000
JEEVES_APP__LOG_LEVEL=INFO

# 安全（缺失则拒绝启动）
JEEVES_SECURITY__ENCRYPTION_KEY=

# LLM 请求参数（与具体模型无关的操作参数）
JEEVES_LLM__REQUEST_TIMEOUT=300
JEEVES_LLM__MAX_RETRIES=3
JEEVES_LLM__TRUST_ENV=false

# Agent
JEEVES_AGENT__MAX_TURNS=40
JEEVES_AGENT__COMPACT_TRIGGER_RATIO=0.75
JEEVES_AGENT__KEEP_TAIL_TURNS=4
JEEVES_AGENT__MAX_SUBAGENT_DEPTH=3

# 沙箱
JEEVES_SANDBOX__BACKEND=local
JEEVES_SANDBOX__TIMEOUT_DEFAULT=120
JEEVES_SANDBOX__MAX_OUTPUT_CHARS=30000
JEEVES_SANDBOX__DOCKER_IMAGE=python:3.12-slim
JEEVES_SANDBOX__DOCKER_NETWORK=none

# 搜索（可选）
JEEVES_WEBSEARCH__BACKEND=none
JEEVES_WEBSEARCH__TAVILY_API_KEY=
```

**模型凭证不在 .env。** 它们加密存 `provider` 表，运行时解密。`.env` 里只有与具体模型无关的操作参数。

理由：模型配置需要在 Web 界面上增删改（探测模型列表、切换绑定），存 `.env` 就得让服务改自己的配置文件再重载，很别扭。

## .env 路径必须绝对

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 绝对路径。相对路径 ".env" 会按进程 cwd 解析：
        # 从项目根启动能读到，从 backend/ 启动读不到，从任何其它目录启动也读不到。
        # 读不到时全部回落默认值，表现为"ENCRYPTION_KEY 缺失拒绝启动"
        # 或"连不上模型" —— 而真正的原因是"配置根本没加载"。
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_nested_delimiter="__",
        extra="ignore",
    )
```

`parents[3]`：从 `backend/app/core/config.py` 上溯到项目根。改动目录层级时**必须同步改这个数字**，所以在启动日志里打出实际加载的 `.env` 路径 —— 一眼能看出对不对。

## .jeeves/ 临时目录

`workspace/.jeeves/tmp/` 存 `run_python` 生成的临时脚本。

放工作区内而非系统 temp 的原因：

1. Docker 沙箱下系统 temp 不在挂载范围
2. 脚本里可能有相对路径 `open("data.csv")`，工作区内才能找到

需要在三处排除它：

- `.gitignore`
- `glob` / `grep` 工具的默认搜索范围
- `list_dir` 的默认输出（除非显式指定该路径）

否则 agent 会在自己的临时文件里搜到自己刚写的代码，产生混乱。

启动时清理超过 24 小时的临时文件。

## 日志

```
data/logs/
  app.log              # 按天滚动，保留 14 天
  app.log.2026-08-01
```

structlog + 标准 logging 的 `TimedRotatingFileHandler`。

开发时同时输出到 stdout（彩色、人类可读），生产/后台运行时只写文件（JSON 行格式，便于 grep）。

**执行输出不进日志文件。** 见 [../architecture/security.md](../architecture/security.md#日志脱敏)。

## 首次启动的自动初始化

`lifespan` 启动时按顺序检查并创建：

```
1. data/ data/uploads/ data/logs/ 目录
2. workspace/ 目录 + workspace/.jeeves/tmp/
3. skills/ macros/ config/ 目录
4. 跑 alembic upgrade head
5. 若 workspace 表为空 → 建默认工作区记录
6. 若 path_whitelist 表为空 → 插入两条 builtin 记录
7. 若 personas/SOUL.md 不存在 → 从 SOUL.example.md 复制
8. 若 config/mcp_servers.yaml 不存在 → 从 example 复制
9. 清理 workspace/.jeeves/tmp/ 下超过 24h 的文件
10. 清理遗留的 Docker 沙箱容器
```

全部幂等，可重复执行。这样 `git clone` + 填 `.env` + 启动就能用，不需要手动建目录。
