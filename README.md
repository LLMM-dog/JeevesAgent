# Jeeves

跑在自己电脑上的 AI 助手。不是又一个聊天窗口 —— 它能读写你的文件、执行命令、跨会话记住你的偏好、连 MCP 工具、按 cron 定时干活。

Python 后端（FastAPI + LangGraph）+ React 前端，单机运行，数据全留在本地。

---

## 快速开始

### 前置要求

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** —— Python 包管理器，会自己装 Python 3.11+
- **[Node.js 18+](https://nodejs.org/)** —— 前端需要
- **一个 OpenAI 兼容的 API Key** —— 推荐 DeepSeek（便宜、国内直连），见下面第 3 步

### 1. 拿代码

```bash
git clone <你的仓库地址> jeeves
cd jeeves
```

### 2. 一键初始化

```bash
uv run python scripts/setup.py
```

Windows 也可以直接双击 `setup.bat`。

| 步骤 | 内容 | 需要你操作吗 |
| --- | --- | --- |
| 1/6 | 检查 uv / Node | 自动 |
| 2/6 | 装后端依赖（含 MCP、联网搜索、定时任务） | 自动 |
| 3/6 | 装前端依赖 | 自动 |
| 4/6 | 生成 `.env` 和加密密钥 | 自动 |
| 5/6 | 配模型 | **可跳过**，之后在设置页配更方便 |
| 6/6 | 设定助手人设和你的称呼 | 可跳过 |

### 3. 启动

**Windows** —— 双击 `start.bat`。

**macOS / Linux**：

```bash
chmod +x start.sh    # 只需第一次
./start.sh --prod
```

然后浏览器打开 **http://127.0.0.1:9000**。

就一个地址、一个进程 —— 前端已经构建好，由后端直接伺服。源码没改动时会跳过构建，所以之后每次启动都是秒开。

<details>
<summary>要改代码的话用开发模式</summary>

开发模式起两个进程：后端带 `--reload`，前端跑 vite dev server（改代码即时生效）。

```
start.bat -Dev          # Windows
./start.sh              # macOS / Linux
```

这时候访问 **http://127.0.0.1:5173**（vite 的地址，不是 9000）。

也可以直接用 PowerShell 脚本，它默认就是开发模式：

```powershell
.\start.ps1                  # 开发
.\start.ps1 -Prod            # 生产
.\start.ps1 -BackendOnly     # 只起后端
.\start.ps1 -Port 9500       # 换端口
```

`start.ps1` 需要执行策略允许运行脚本。被拦了就先跑一次 `Set-ExecutionPolicy Bypass -Scope Process -Force`，或者干脆用 `start.bat`（它内部已经带了 `-ExecutionPolicy Bypass`）。

</details>

### 4. 配模型

进去先到 **设置 → 添加供应商**。以 DeepSeek 为例：

```
名称：      deepseek
Base URL：  https://api.deepseek.com/v1
API Key：   sk-你的密钥
模型：      deepseek-chat
```

去 [platform.deepseek.com](https://platform.deepseek.com/) 注册就能拿到 Key。任何 OpenAI 兼容端点都行 —— Kimi、通义、智谱、OpenRouter、本地的 Ollama 都是。

填完在 **功能位绑定** 里把「对话模型」选上就能聊了。

### 5. 试几句

先随便聊一句确认通了，然后试试它真正能干的事：

```
读一下 workspace 目录，告诉我里面有什么
```

```
写一个 Python 脚本算斐波那契数列前 20 项，存成 fib.py，然后运行它
```

> 文件路径都是**相对工作区根目录**的，所以直接说 `fib.py` 就好。
> 说成 `workspace/fib.py` 的话会变成 `workspace/workspace/fib.py` —— 多套一层。

```
记住：我习惯用 4 个空格缩进，不喜欢过度封装
```

第三句会写进长期记忆 —— 换个新会话再问"我的代码偏好是什么"，它还记得。记住的每一条都能在 **设置 → 记忆** 里看到和删掉。

执行命令默认需要你点确认。想让它自己跑就在输入框上方把审批模式切成「自动」。

### 起不来的话

| 现象 | 原因 |
| --- | --- |
| 双击 `start.bat` 窗口一闪就没了 | 不会发生 —— 它有 `pause`。如果真闪掉了，用 cmd 手动跑一次看报错 |
| `无法加载文件 start.ps1` | 你直接跑了 `.ps1`。用 `start.bat`，或先 `Set-ExecutionPolicy Bypass -Scope Process -Force` |
| `Permission denied: ./start.sh` | 忘了 `chmod +x start.sh` |
| `端口被占用` | 上次的进程没退干净。脚本会问你要不要清理，选 y |
| 打开 9000 是白屏 | 前端没构建。`cd frontend && npm install && npm run build` |
| `ENCRYPTION_KEY 缺失` | `.env` 没生成。重跑 `setup.bat` |
| 对话报"未配置模型" | 去设置页添加供应商，并确认「功能位绑定」里对话模型选了 |

### 起不来的话

| 现象 | 原因 |
| --- | --- |
| `端口被占用` | 上次的进程没退干净。`start.ps1` 会问你要不要清理，选 y |
| 页面白屏 | 前端依赖没装上。`cd frontend && npm install` |
| `ENCRYPTION_KEY 缺失` | `.env` 没生成。重跑 `uv run python scripts/setup.py` |
| 对话报"未配置模型" | 去设置页添加供应商，并确认"功能位绑定"里对话模型选了 |

---

## 它能做什么

**文件与命令** —— 读写文件、glob/grep、执行 shell 和 Python。文件操作受路径白名单约束，命令执行默认需要你点确认。

**记忆** —— 跨会话记住事情。哪些该记由模型判断，你可以在设置页看到和删除每一条。

**技能与宏** —— 把常用流程写成 Markdown 放进 `skills/`，模型按需加载。`macros/` 里是可以 `!名字` 直接调用的提示词模板。

**子智能体** —— 派一个独立上下文的子任务，跑完只把结论带回主对话。适合"翻一遍这个目录再总结"这类会污染上下文的活。

**MCP** —— 连外部 MCP 服务器，把它们的工具混进工具集。默认不启用任何服务器。

**定时任务** —— cron 表达式，到点自动开一个会话干活。支持时区，服务没运行时错过的窗口有记录。

**联网搜索** —— 默认关闭（开了之后你的搜索词会发给第三方）。设置页里一键开，不用改配置重启。

**语音输入** —— 点麦克风说话。浏览器不支持时按钮不显示。

**追踪** —— 每轮对话的耗时、token、工具调用都能展开看。

一共 21 个内置工具，完整清单见 [docs/01-architecture/tools.md](docs/01-architecture/tools.md)。

---

## 数据放在哪

全部在项目文件夹内，没有一个字节写到别处：

```
data/jeeves.db      对话、记忆、定时任务、追踪（SQLite）
data/uploads/       你上传的图片
data/logs/          日志
workspace/          agent 的工作目录
.env                配置和加密后的 API Key
```

所有路径都锚定在项目根，**环境变量也改不到外面去**（它们是代码里的属性，不是可配置项）。

已确认**不会**碰的地方：注册表、计划任务、系统服务、PATH、桌面快捷方式、协议关联、浏览器 localStorage、家目录（`~` / `%APPDATA%` / `Library/Application Support`）。

完整清单在 [local-files.yaml](local-files.yaml) —— 哪个文件是谁写的、升级时要不要保留、卸载会不会被删掉，都在那里。它跟着代码维护，所以不会像文档那样过时。

---

## 卸载

```bash
uv run python scripts/uninstall.py
```

然后删掉整个项目文件夹。

这个脚本清的是**删文件夹带不走的东西** —— 主要是 Docker 容器（如果你开了 Docker 沙箱，那些 `jeeves-*` 容器会一直留着占内存）。

加 `--all` 会连 `python:3.12-slim` 镜像和 uv/npm 缓存一起清。默认不清，因为别的项目可能也在用。

**有一样东西脚本清不了**：如果你在设置页把项目外的目录加进了白名单，或者 agent 用 `run_shell` 执行过命令，它可能在别处写过文件。这个只有你自己知道 —— 删项目前想查的话，`data/jeeves.db` 里有完整的执行记录。

---

## 安全边界

个人单机项目，取舍写在明面上：

**默认只绑 `127.0.0.1`，没有鉴权。** 绑到 `0.0.0.0` 之前必须先加鉴权，否则同网段任何人都能用你的模型和文件。启动时如果检测到绑了非本地地址会持续警告。

**`run_shell` 能做的事没有上界。** 路径白名单管不到它 —— 一条命令可以 `curl | sh`。默认需要人工确认每条命令。要真隔离就配 Docker 沙箱（`--network none` + 资源限制 + 只挂工作区），见 [docs/01-architecture/sandbox.md](docs/01-architecture/sandbox.md)。

**定时任务里的 agent 是自动批准的。** 触发时没人在旁边点确认，所以强制 auto 审批。创建界面上有提示。

**`.env` 里的 API Key 是加密存的，但密钥就在同一个文件里。** 这挡的是"截图/日志泄露"，不挡"拿到文件系统访问权"。

细节见 [docs/01-architecture/security.md](docs/01-architecture/security.md)。

---

## 进阶

大部分东西不用改配置，设置页里能动态调。要改的话 `.env` 里所有可选项都有注释（`.env.example` 是带说明的完整版）。

```bash
uv sync --extra docker    # Docker 沙箱（需要本机跑着 Docker）
uv run pytest             # 914 个后端测试
cd frontend && npm test   # 前端测试
```

`scripts/verify_*.py` 是各功能的真实环境验证脚本（要真实模型 API），比单测更接近实际使用。用法见各脚本开头的 docstring。

---

## 文档

[docs/README.md](docs/README.md) 是索引。按你想做的事找：

**想用起来**

- [docs/01-architecture/tools.md](docs/01-architecture/tools.md) —— 21 个内置工具都能干什么
- [docs/01-architecture/skills.md](docs/01-architecture/skills.md) —— 怎么写技能扩展它
- [docs/01-architecture/security.md](docs/01-architecture/security.md) —— 安全边界和审批机制

**想改代码**

- [docs/01-architecture/agent-loop.md](docs/01-architecture/agent-loop.md) —— agent 主循环怎么转
- [docs/03-api/conventions.md](docs/03-api/conventions.md) —— API 约定
- [docs/05-dev/setup.md](docs/05-dev/setup.md) —— 开发环境
- [docs/05-dev/testing.md](docs/05-dev/testing.md) —— 测试怎么写

---

## 技术栈

后端 FastAPI + LangGraph + SQLAlchemy 2.0 + SQLite，前端 React 19 + TypeScript + Tailwind 4 + Zustand + TanStack Query。

87 个后端源文件，914 个后端测试 + 30 个前端测试。

---

## 协议

[MIT](LICENSE)。随便用、随便改、随便分发，出问题自己担着。
