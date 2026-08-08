# Jeeves

跑在自己电脑上的 AI 助手。不是又一个聊天窗口 —— 它能读写你的文件、执行命令、跨会话记住你的偏好、连 MCP 工具、按 cron 定时干活。

Python 后端（FastAPI，基于 LangGraph 组件）+ React 前端，单机运行，数据全留在本地。

> **v0.1 测试版。** 核心功能都能跑（1251 个后端测试 + 30 个前端测试，关键路径还用真实模型验证过），但界面和交互还在调整，遇到问题欢迎提 issue。

---

## 为什么用它

市面上的本地 AI 助手不少。这个项目的取舍集中在四件事上。

### 1. 上下文是可见、可控的

大多数工具把上下文当黑盒。这里输入框上方一直有一条占用条，分三段显示：

```
工具定义 4,298 (3.3%)   系统提示词 1,722 (1.3%)   对话内容 8,940 (6.8%)
共 14,960 / 131,072 (11.4%)
```

**百分比按窗口算，不是分项互比** —— 你要知道的是"还剩多少空间粘代码"，而不是"工具占了已用部分的 70%"。后者会让人误以为窗口快满了。

固定开销（工具定义 + 系统提示词）在发消息之前就显示出来。这两项每一轮都重发，看到具体数字才知道该关掉哪个 MCP 服务器。

上下文快满时会自动压缩，你也可以让它主动压缩 —— 模型知道"调研阶段结束了、几十条工具输出已经没用了",而阈值只看总量。压缩目标是当前模型窗口的 20%,所以 8K 窗口和 128K 窗口的行为不一样。

### 2. 每个消耗 token 的东西都能单独关掉

技能、MCP 服务器都有开关，关掉的不进系统提示词。开关旁边就写着它占多少 token:

```
filesystem   stdio   已启用   12 个工具 · ~3,140 token   [开关]
github       http    已关闭   —                          [开关]
```

关掉之后上下文条上的数字立刻变小。这不是摆设 —— 一个用不到的 MCP 服务器可能每轮烧掉几千 token。

### 3. 它能改自己的能力

技能是一个目录：`SKILL.md` 定义"什么时候用",`references/` 放细节，还可以带脚本。模型能自己建技能、加附件、重新扫描索引 —— 你说"学一下这个规范",它就写成技能存下来，下次自动想起来用。

宏是可复用的流程模板，输入 `!` 就能引用。同样可以让模型帮你建。

`skills/` 和 `macros/` 在可写白名单里，所以模型用的是它已经熟练的 `write_file` / `edit_file`,不是一套专门的 API。

### 4. 数据真的在本地

全部在项目文件夹内，没有一个字节写到别处。已确认**不碰**：注册表、计划任务、系统服务、PATH、桌面快捷方式、协议关联、浏览器 localStorage、家目录。

完整清单在 [local-files.yaml](local-files.yaml) —— 哪个文件是谁写的、升级要不要保留、卸载会不会删，都在那里。它跟着代码维护，所以不会像文档那样过时。

---

## 快速开始

### 前置要求

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** —— Python 包管理器，会自己装 Python 3.11+
- **[Node.js 18+](https://nodejs.org/)** —— 前端需要
- **一个 OpenAI 兼容的 API Key** —— 推荐 DeepSeek（便宜、国内直连）,见下面第 4 步

### 1. 拿代码

```bash
git clone <你的仓库地址> jeeves
cd jeeves
```

### 2. 一键初始化

```bash
uv run python scripts/setup.py
```

双击 `start.bat`（首次运行会自动初始化项目，不需要单独跑 setup）。

| 步骤 | 内容 | 需要你操作吗 |
| --- | --- | --- |
| 1/6 | 检查 uv / Node | 自动 |
| 2/6 | 装后端依赖（含 MCP、联网搜索、定时任务） | 自动 |
| 3/6 | 装前端依赖 | 自动 |
| 4/6 | 生成 `.env` 和加密密钥 | 自动 |
| 5/6 | 配模型 | **可跳过**,之后在设置页配更方便 |
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

开发模式起两个进程：后端带 `--reload`,前端跑 vite dev server（改代码即时生效）。

```
start.bat -Dev          # Windows
./start.sh              # macOS / Linux
```

这时候访问 **http://127.0.0.1:5173**（vite 的地址，不是 9000）。

也可以手动控制运行模式：

```bash
start.bat -Dev          # Windows 开发模式（改代码即时生效）
start.bat -BackendOnly  # Windows 只起后端
./start.sh              # macOS / Linux 开发模式
./start.sh --backend-only
```

`start.bat` 内部已带 `-ExecutionPolicy Bypass`，不需要自己配 PowerShell 策略。

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

> 建议把 `title`（起标题）和 `compact`（压缩上下文）也绑上。不绑会回落到对话模型 —— 能用，但起标题这种小事用便宜模型更划算。

### 5. 试几句

先随便聊一句确认通了，然后试试它真正能干的事：

```
读一下 workspace 目录，告诉我里面有什么
```

```
写一个 Python 脚本算斐波那契数列前 20 项，存成 fib.py,然后运行它
```

> 文件路径都是**相对工作区根目录**的，所以直接说 `fib.py` 就好。
> 说成 `workspace/fib.py` 的话会变成 `workspace/workspace/fib.py` —— 多套一层。

```
记住：我习惯用 4 个空格缩进，不喜欢过度封装
```

第三句会写进长期记忆 —— 换个新会话再问"我的代码偏好是什么",它还记得。记住的每一条都能在 **设置 → 记忆** 里看到和删掉。

执行命令默认需要你点确认。想让它自己跑就在输入框上方把审批模式切成「自动」。

### 起不来的话

| 现象 | 原因 |
| --- | --- |
| 双击 `start.bat` 窗口一闪就没了 | 不会发生 —— 它有 `pause`。如果真闪掉了，用 cmd 手动跑一次看报错 |
| `无法加载文件 start.ps1` | 你直接跑了 `.ps1`。用 `start.bat` |
| `Permission denied: ./start.sh` | 忘了 `chmod +x start.sh` |
| `端口被占用` | 上次的进程没退干净。脚本会问你要不要清理，选 y |
| 打开 9000 是白屏 | 前端没构建。`cd frontend && npm install && npm run build` |
| `ENCRYPTION_KEY 缺失` | `.env` 没生成。重跑 `start.bat`（会自动重新初始化） |
| 对话报"未配置模型" | 去设置页添加供应商，并确认「功能位绑定」里对话模型选了 |

---

## 它能做什么

**文件与命令** —— 读写文件、glob/grep、执行 shell 和 Python。文件操作受路径白名单约束，命令执行默认需要你点确认。

**记忆** —— 跨会话记住事情。哪些该记由模型判断，你可以在设置页看到和删除每一条。两个开关分别控制读写：私密模式（这轮不写记忆）、失忆模式（这轮不读记忆）。

**技能与宏** —— 技能是目录（`SKILL.md` + 可选的 `references/`、脚本）,模型按需三级加载。宏是 `!名字` 直接引用的流程模板。两者都能在设置页管理，也能让模型自己建。

**上下文管理** —— 占用条按窗口比例分段显示，固定开销常驻可见。快满时自动压缩，也可以让模型主动压缩。压缩目标是窗口的 20%。

**子智能体** —— 派一个独立上下文的子任务，跑完只把结论带回主对话。实测同一个"读 6 个文件提取结论"的任务，父上下文从 8399 降到 5489 token。

**MCP** —— 连外部 MCP 服务器，把它们的工具混进工具集。每个服务器有开关，关掉的不占上下文。默认不启用任何服务器。

**定时任务** —— cron 表达式，到点自动开一个会话干活。支持时区，服务没运行时错过的窗口有记录。

**联网搜索** —— 默认关闭（开了之后你的搜索词会发给第三方）。设置页里一键开，不用改配置重启。

**语音输入** —— 点麦克风说话。浏览器不支持时按钮不显示。

**追踪** —— 每轮对话的耗时、token、工具调用都能展开看。按会话分层，不用在一堆 run 里找。

20 个内置工具（配了搜索后端是 21 个）,完整清单见 [docs/architecture/tools.md](docs/architecture/tools.md)。

---

## 数据放在哪

```
data/jeeves.db      对话、记忆、定时任务、追踪（SQLite）
data/uploads/       你上传的图片
data/logs/          日志
workspace/          agent 的工作目录
skills/             技能（模型可写）
macros/             宏（模型可写）
personas/           人格设定
.env                配置和加密后的 API Key
```

所有路径都锚定在项目根,**环境变量也改不到外面去**（它们是代码里的属性，不是可配置项）。

---

## 更新

```bash
git pull
uv sync                      # 有新依赖时
cd frontend && npm install   # 同上
```

然后重新启动。**数据库迁移在启动时自动跑**,不需要手动执行任何命令。

### 你的东西都会留下

这些文件不被 git 跟踪,`git pull` 碰不到它们：

| | |
|---|---|
| `data/jeeves.db` | 对话历史、记忆、定时任务、追踪 |
| `.env` | 配置和加密后的 API Key |
| `personas/*.md` | 性格设定、你的自述、行为规则 |
| `workspace/` | agent 的工作目录 |
| `config/mcp_servers.yaml` | MCP 服务器配置 |

仓库里只有 `personas/*.example.md`。首次启动时复制成 `.md`,**已存在的绝不覆盖** —— 所以升级不会重置你改过的人格设定。

### 数据库怎么保证不丢

迁移只加不删：加表、加列、加索引。有测试盯着这件事 —— 任何一个迁移里出现 `drop_table('message')` 或往 `message` / `session` 表 `drop_column`,测试就会失败。

往已有表加列时必须带 `server_default`,否则已有行满足不了 `NOT NULL`,SQLite 会在迁移中途失败。这一条也有测试。

每个迁移都实现了 `downgrade()`。升级出问题时可以退回去：

```bash
uv run alembic downgrade -1    # 退一步
uv run alembic current         # 看当前版本
```

### 升级前想更保险

```bash
# Windows
Copy-Item data\jeeves.db data\jeeves.db.bak
# macOS / Linux
cp data/jeeves.db data/jeeves.db.bak
```

出问题就把 `.bak` 改回来。数据库是单个文件，没有别的状态。

### 如果 pull 报 AGENTS.md 冲突

只会发生一次，在从 2026-08-05 之前的版本升级时：

```
error: Your local changes to the following files would be overwritten by merge:
        personas/AGENTS.md
```

原因是那个版本里 `personas/AGENTS.md` 还被 git 跟踪，而新版本把它改成了「只跟踪 `.example.md`」——git 不肯删掉一个你改过的文件。

你的行为规则不会丢，先挪出来再拉：

```bash
# Windows
Move-Item personas\AGENTS.md personas\AGENTS.md.mine
git pull
# 想恢复自己的版本：
Move-Item -Force personas\AGENTS.md.mine personas\AGENTS.md
```

```bash
# macOS / Linux
mv personas/AGENTS.md personas/AGENTS.md.mine
git pull
mv personas/AGENTS.md.mine personas/AGENTS.md
```

拉完之后这个文件就不再被跟踪了，以后改它不会再冲突。不挪也可以直接 `git checkout -- personas/AGENTS.md` 丢掉本地修改，但那会把你写的规则一起丢掉。

> **`.env` 里的 `ENCRYPTION_KEY` 要单独备份。** 它丢了的话已存的 API Key 全部无法解密（只能重填一遍）—— 而对话历史不受影响，那部分没有加密。

---

## 卸载

```bash
uv run python scripts/uninstall.py
```

然后删掉整个项目文件夹。

这个脚本清的是**删文件夹带不走的东西** —— 主要是 Docker 容器（如果你开了 Docker 沙箱，那些 `jeeves-*` 容器会一直留着占内存）。

加 `--all` 会连 `python:3.12-slim` 镜像和 uv/npm 缓存一起清。默认不清，因为别的项目可能也在用。

**有一样东西脚本清不了**：如果你在设置页把项目外的目录加进了白名单，或者 agent 用 `run_shell` 执行过命令，它可能在别处写过文件。这个只有你自己知道 —— 删项目前想查的话,`data/jeeves.db` 里有完整的执行记录。

---

## 安全边界

个人单机项目，取舍写在明面上：

**默认只绑 `127.0.0.1`,没有鉴权。** 绑到 `0.0.0.0` 之前必须先加鉴权，否则同网段任何人都能用你的模型和文件。启动时如果检测到绑了非本地地址会持续警告。

**`run_shell` 能做的事没有上界。** 路径白名单管不到它 —— 一条命令可以 `curl | sh`。默认需要人工确认每条命令。要真隔离就配 Docker 沙箱（`--network none` + 资源限制 + 只挂工作区）,见 [docs/architecture/sandbox.md](docs/architecture/sandbox.md)。

**`skills/` 和 `macros/` 对模型可写。** 这是为了让它能自己建技能。硬拒止清单（`.env`、`*.pem`、`credentials` 等）优先级高于白名单，所以这不等于放开敏感文件。

**定时任务里的 agent 是自动批准的。** 触发时没人在旁边点确认，所以强制 auto 审批。创建界面上有提示。

**`.env` 里的 API Key 是加密存的，但密钥就在同一个文件里。** 这挡的是"截图/日志泄露",不挡"拿到文件系统访问权"。

细节见 [docs/architecture/security.md](docs/architecture/security.md)。

---

## 进阶

大部分东西不用改配置，设置页里能动态调。要改的话 `.env` 里所有可选项都有注释（`.env.example` 是带说明的完整版）。

```bash
uv sync --extra docker    # Docker 沙箱（需要本机跑着 Docker）
uv run pytest             # 1251 个后端测试
cd frontend && npm test   # 30 个前端测试
```

`scripts/verify_*.py` 是各功能的真实环境验证脚本（要真实模型 API）,比单测更接近实际使用。用法见各脚本开头的 docstring。

---

## 文档

[docs/README.md](docs/README.md) 是索引。按你想做的事找：

**想用起来**

- [docs/architecture/tools.md](docs/architecture/tools.md) —— 20 个内置工具都能干什么
- [docs/architecture/skills.md](docs/architecture/skills.md) —— 怎么写技能扩展它
- [docs/architecture/context.md](docs/architecture/context.md) —— 上下文怎么算、怎么压缩
- [docs/architecture/security.md](docs/architecture/security.md) —— 安全边界和审批机制

**想改代码**

- [docs/architecture/agent-loop.md](docs/architecture/agent-loop.md) —— agent 主循环怎么转
- [docs/api/conventions.md](docs/api/conventions.md) —— API 约定
- [docs/development/setup.md](docs/development/setup.md) —— 开发环境
- [docs/development/testing.md](docs/development/testing.md) —— 测试怎么写

---

## 技术栈

后端 FastAPI + SQLAlchemy 2.0 + SQLite（LangGraph 组件保留，agent loop 已切换为纯 while 循环），前端 React 19 + TypeScript + Tailwind 4 + Zustand + TanStack Query。

94 个后端源文件、38 个前端源文件，1251 个后端测试 + 30 个前端测试。

---

## 协议

[MIT](LICENSE)。随便用、随便改、随便分发，出问题自己担着。
