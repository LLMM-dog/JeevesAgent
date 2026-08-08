# Cline & Roo Code 驾驭工程调研

> Cline: VS Code 内 AI 编码 Agent 扩展
> Roo Code: Cline 的分支，增加 Cloud Agents 和模式自定义
> Stars: 20K+ | 语言: TypeScript | 定位：IDE 内 Agent 扩展

## 1. 项目概览

Cline 和 Roo Code 代表了 IDE 内 Agent 扩展这一品类。与独立的 CLI Agent（如 Claude Code）不同，它们深度集成在 VS Code 中，利用 IDE 的 LSP、文件系统、终端等原生能力。

Roo Code 是 Cline 的活跃分支（Cline 已于 2026 年归档），增加了云 Agent 和更灵活的模式系统。

## 2. 架构定位

```
┌──────────── VS Code ────────────┐
│  Cline / Roo Code 扩展          │
│  ├── Agent Loop (ReAct)         │
│  ├── Tool System                │
│  ├── MCP Client                 │
│  └── Permission UI              │
├─────────────────────────────────┤
│  VS Code API                    │
│  ├── LSP (符号、诊断、补全)     │
│  ├── Terminal                   │
│  ├── File System                │
│  └── Editor (diff、selection)   │
└─────────────────────────────────┘
```

**与 CLI Agent 的本质区别**：
- CLI Agent：独立进程，用自己的工具抽象操作文件
- IDE Agent：通过 VS Code API 操作，利用 IDE 已有的 LSP、Terminal、Editor 能力
- 用户交互更自然：在编辑器中查看 diff、点击批准/拒绝

## 3 工具系统

### Cline

- 文件读写、搜索、编辑
- Shell 命令执行（VS Code Terminal）
- MCP 工具集成
- Browser（通过 Playwright）
- LSP 集成（利用 VS Code 已有能力）

### Roo Code 增强

- 同上所有工具
- **Cloud Agents**：任务委派到云端独立运行
- **模式特定工具**：不同模式可用不同工具集

## 4 权限模型

### Cline — 逐步批准

Cline 的核心理念是**结构化生成 + 完全可审计**：
- 每步操作需用户确认
- 完整操作历史可追溯
- 适合企业级开发（可见性和可重现性优先于速度）

### Roo Code — 模式级权限

| 模式 | 权限 |
|------|------|
| **Architect** | 只读 + 规划 |
| **Code** | 全访问 |
| **Debug** | 执行 + 读取 |
| **Ask** | 只读 |
| **Custom** | 完全自定义 |

每个模式保存"sticky models"——Architect 模式绑定强推理模型，Code 模式绑定快速模型。

## 5 多 Agent 编排

### Roo Code Cloud Agents

这是 Roo Code 最大的架构创新：

```
用户分配任务 → Agent 在云端独立工作
  │
  ├── Web 界面查看进度
  ├── GitHub Issue/PR 触发
  └── 完成后自动通知
```

- **并行开发**：多个 Cloud Agent 同时处理不同任务
- **后台工作**：不占用本地 IDE
- **跨会话**：Agent 可以在用户离线时继续工作

### Cline Checkpoints

Cline 支持检查点（checkpoints）——代码变更前 snapshot 文件状态，可回退。

## 6 MCP 集成

- **Cline MCP Marketplace**：精选 MCP Server 目录，一键安装
- **Roo Code**：支持 MCP，但无 Marketplace，需手动配置
- 两者都使用 MCP 协议扩展工具能力

## 7 Context 处理

- 两者都是**文件感知** Agent：读取仓库结构、检查相关文件、进行多步修改
- 支持 OpenRouter、Ollama、LM Studio 等本地模型
- 多 Provider 灵活性（Cline 明确列出支持的所有 Provider）

## 8 与 CLI Agent 的关键差异

| 维度 | Cline/Roo Code (IDE) | Claude Code/OpenCode (CLI) |
|------|---------------------|---------------------------|
| 运行环境 | VS Code 扩展宿主 | 独立进程 |
| 文件操作 | VS Code API (原生 diff 查看) | 自有工具抽象 |
| LSP | 利用 VS Code 已有 LSP | 需自建 LSP 客户端 |
| 终端 | VS Code Terminal | PTY/伪终端 |
| 用户交互 | 编辑器内审批 | 终端内审批 |
| 离线能力 | 依赖 VS Code | 独立运行 |
| 云端 Agent | Roo Code 支持 | OpenCode 不支持 |

## 9 对 Jeeves 的启示

| 维度 | Cline/Roo Code 的做法 | Jeeves 可借鉴 |
|------|----------------------|--------------|
| IDE 集成 | 通过 VS Code API 利用原生能力 | Jeeves 定位为桌面 App，非 IDE 插件 |
| 模式系统 | 每种模式独立配置+权限+模型 | **可参考**：设计不同模式/角色的 Agent |
| Cloud Agents | 任务委派到云端独立运行 | 已有 delegate_task，可增强远程执行 |
| 逐步审批 | 每步操作可见+可审查 | Jeeves 已有审查流程 ✅ |
| Checkpoints | 代码变更前 snapshot | 可考虑实现类似机制 |
| MCP Marketplace | 精选 MCP Server 目录 | 可参考生态建设思路 |
