# Jeeves

一个跑在你电脑上的 AI 助手。

它可以读文件、写代码、执行命令、记住你的习惯，也能在你不开电脑时按计划自己干活。数据全部留在本地，不需要把工作内容交给别人。

![Jeeves 主界面：一次真实的坦克大战开发对话，左边会话列表，中间是带工具调用的对话流，右边待办看板](docs/assets/chat-main.png)

v0.3.1 测试版。核心功能已经能跑，界面和体验还在持续打磨。遇到问题欢迎提 issue。

---

## 它能做什么

- **长期记忆**：不是把聊天记录塞回提示词。它会从对话里提取你的偏好、经验、工具心得，按作用域存成 Markdown 文件。你可以随时查看、修改、删除。
- **智能体与子任务委派**：可以创建不同角色的智能体，模型也能派内置的 `researcher`、`reviewer` 去分担阅读和审查工作。
- **上下文可见**：输入框上方一直显示上下文占用情况，快满时自动压缩，不用你操心。
- **技能系统**：你可以让它「学一下某个规范」，它会自己写成技能，下次自动用上。
- **文件与命令**：内置文件读写、搜索、Shell 工具，默认本地运行，需要时也能切到 Docker 沙箱。
- **MCP**：支持接入外部工具，但默认不启用——MCP 是第三方代码，成本和安全都由你把控。
- **定时任务**：用 cron 表达式安排任务，到点自动开新会话执行。
- **联网、视觉、语音**：能搜索网页、看图片、听语音输入。
- **追踪**：每次执行的耗时、token、工具调用都能回看，方便知道它到底做了什么。

更多细节见 [文档](docs/README.md)。

---

## 快速开始

### 你需要准备

- [uv](https://docs.astral.sh/uv/getting-started/installation/)：包管理器，会自动处理 Python 环境
- [Node.js 18+](https://nodejs.org/)
- 一个 OpenAI 兼容的 API Key，推荐 [DeepSeek](https://platform.deepseek.com/)

### 1. 下载

```bash
git clone https://github.com/LLMM-dog/JeevesAgent.git jeeves
cd jeeves
```

### 2. 启动

Windows 双击 `start.bat`，首次运行会自动初始化。

macOS / Linux：

```bash
chmod +x start.sh
./start.sh --prod
```

浏览器打开 <http://127.0.0.1:9000>。

想改代码就用开发模式：

```bash
start.bat -Dev      # Windows
./start.sh          # macOS / Linux
```

开发模式前端在 <http://127.0.0.1:5173>。

### 3. 配置模型

进入设置页，点「添加端点」。以 DeepSeek 为例：

```
名称：      deepseek
Base URL：  https://api.deepseek.com/v1
API Key：   sk-你的密钥
模型：      deepseek-chat
```

![设置页的模型面板，按供应商分组管理端点](docs/assets/settings-models.png)

任何 OpenAI 兼容的端点都可以：Kimi、通义、智谱、OpenRouter，或者本地 Ollama。

配置后在「功能位绑定」里选好对话模型，就能开始聊了。

### 4. 试几句

```
读一下 workspace 目录，告诉我里面有什么
```

```
写一个 Python 脚本算斐波那契前 20 项，存成 fib.py，然后运行它
```

```
记住：我习惯用 4 个空格缩进，不喜欢过度封装
```

---

## 文档

- [工具说明](docs/architecture/tools.md)
- [技能系统](docs/architecture/skills.md)
- [记忆系统](docs/architecture/memory.md)
- [上下文管理](docs/architecture/context.md)
- [Agent 主循环](docs/architecture/agent-loop.md)
- [智能体与子任务委派](docs/architecture/agents.md)
- [HTTP 接口与 SSE 事件](docs/api/)

---

## 技术栈

后端：FastAPI + SQLAlchemy 2.0 + SQLite  
前端：React 19 + TypeScript + Tailwind CSS 4 + Zustand + TanStack Query

---

## License

[MIT](LICENSE)
