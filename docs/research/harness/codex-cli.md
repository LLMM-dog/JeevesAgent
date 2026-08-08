# Codex CLI 驾驭工程深度分析

> **项目**: [openai/codex](https://github.com/openai/codex) | **Stars**: 104K+ | **语言**: Rust (97.4%) | **定位**: OpenAI 官方编码 Agent CLI
> **分析日期**: 2026-08-08 | **分析版本**: v0.144.0

---

## 目录

1. [项目概览](#1-项目概览)
2. [PEV 架构 (Plan-Execute-Verify)](#2-pev-架构-plan-execute-verify)
3. [编辑系统](#3-编辑系统)
4. [权限系统演进](#4-权限系统演进)
5. [Hooks 系统](#5-hooks-系统)
6. [Goals 系统](#6-goals-系统)
7. [沙箱执行](#7-沙箱执行)
8. [Chrome Extension 集成](#8-chrome-extension-集成)
9. [Remote Control & Mobile](#9-remote-control--mobile)
10. [模型路由](#10-模型路由)
11. [App Server 架构](#11-app-server-架构)
12. [核心引擎: Codex Core](#12-核心引擎-codex-core)
13. [Harness Engineering 实践](#13-harness-engineering-实践)
14. [Jeeves 可借鉴清单](#14-jeeves-可借鉴清单)

---

## 1. 项目概览

Codex CLI 是 OpenAI 官方开源的终端编码 Agent，发布仅 5 个月即突破 10 万 GitHub Stars。它不仅是一个 CLI 工具，更是一套完整的 **Agent 驾驭工程 (Harness Engineering)** 参考实现——OpenAI 团队自己就是用它在一个空仓库里生成了 100 万行代码，3 人团队在 5 个月内合并了约 1500 个 PR（平均 3.5 PR/工程师/天），**零行人工手写代码**。

### 核心架构层次

```
codex-rs/
├── core/           # Agent 核心逻辑 + 提示词模板
├── cli/            # 终端界面 (TUI) + Vim 模式
├── app-server/     # JSON-RPC API 服务层 (跨表面统一接口)
├── exec/           # 命令执行 + 沙箱加固
├── mcp-server/     # MCP 工具服务器 + 插件市场
├── protocol/       # 类型协议定义
└── security/       # 安全相关
```

### 多表面统一 Harness

Codex 的架构设计核心是一个哲学：**同一套 Harness 驱动所有体验**。CLI、VS Code 扩展、Web App、macOS Desktop App、ChatGPT 移动端，底层都共享 `codex-rs/core` 中的 Agent Loop 和工具执行逻辑。App Server 通过 JSON-RPC over stdio 协议暴露这个 Harness，让所有客户端都能复用同一个 Agent 引擎。

---

## 2. PEV 架构 (Plan-Execute-Verify)

### 2.1 核心概念

Codex CLI 采用 **Plan-Execute-Verify (PEV)** 三阶段分离架构，这是 OpenAI 在 Harness Engineering 实践中的核心设计模式。

```
┌──────────────────────────────────────────────────────────────────┐
│                      PEV Agent Loop                              │
│                                                                  │
│  ┌─────────┐     ┌──────────────┐     ┌──────────────┐          │
│  │  PLAN   │ ──▶ │   EXECUTE    │ ──▶ │   VERIFY     │ ──▶ 完成  │
│  │ 制定计划 │     │  执行工具调用  │     │  验证结果     │    │     │
│  └─────────┘     └──────────────┘     └──────────────┘    │     │
│       ▲                                       │            │     │
│       └──────────── 验证失败，更新计划 ─────────┘            │     │
│                                                              │     │
│  update_plan    shell/apply_patch     test/lint/build        │     │
│  工具显式化      等工具执行          自动验证循环             │     │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 Plan 阶段

**`update_plan` 工具**是 PEV 的核心枢纽。它的设计非常克制：

- 模型通过 `update_plan` 将任务分解为 1-句子步骤（每个不超过 5-7 个词）
- 每步有明确状态: `pending` | `in_progress` | `completed`
- **始终只有 1 个步骤处于 `in_progress`**——强制线性执行
- 计划变更必须提供 `explanation` 理由
- 复杂任务需要明确的可验证检查点

**来源**: `codex-rs/core/prompt.md` 中的 Planning 指令：

```
When steps have been completed, use update_plan to mark each finished
step as completed and the next step you are working on as in_progress.
There should always be exactly one in_progress step until everything
is done. You can mark multiple items as complete in a single update_plan
call.
```

### 2.3 Execute 阶段

Execute 阶段使用两个核心工具：
- **`shell`**: 执行终端命令（PTY 模拟，支持交互式进程）
- **`apply_patch`**: 文件编辑（详见 §3 编辑系统）
- **`update_plan`**: 每个步骤完成后，标记进度

关键设计：**计划状态变化本身就是工具调用**，对用户完全可见。

### 2.4 Verify 阶段

Verify 不是独立的工具，而是 Agent 的内建行为模式：

1. **执行后自动验证**: 运行 `cargo test`、`npm test`、linter
2. **失败后自动修复**: 模型观察测试输出，自动修正
3. **循环直到通过**: Agent 持续迭代直到所有检查点通过
4. **环境可观测性**: Chrome DevTools、日志查询 (LogQL)、指标查询 (PromQL) 都可以作为验证手段

**OpenAI 的实践**: "We regularly see single Codex runs work on a single task for upwards of six hours (often while the humans are sleeping)."

### 2.5 与传统 ReAct Loop 的对比

| 维度 | ReAct Loop | Codex PEV |
|------|-----------|-----------|
| **推理粒度** | 每步推理-行动交替 | 先完整规划，再分段执行 |
| **用户可见性** | 用户看到推理链 | 用户看到结构化计划和进度 |
| **进度追踪** | 无显式进度 | `update_plan` 提供实时进度 |
| **失败恢复** | 重试当前步骤 | 更新计划 → 重新执行 → 验证 |
| **上下文污染** | 每步推理都在上下文中 | 中间的推理和验证分散在多轮 |
| **单次推理负担** | 重（需要同时推理和规划） | 轻（规划已经显式化） |

### 2.6 PEV 减少单次推理负担的机制

```
传统方案: 模型一次回答 = 理解需求 + 规划步骤 + 生成代码 + 考虑边界 + 验证正确性
PEV 方案:  Plan 阶段 → 只做规划
            Execute → 只做当前步骤
            Verify  → 只检查结果
```

**设计优点**:
- 将"推理深度"分摊到多个轻量推理调用上
- 计划作为"外部记忆"保存在 `update_plan` 中，不占用上下文
- 验证失败时，模型只需分析失败原因而非重新理解整个任务

### 2.7 对 Jeeves 的启示

1. **引入显式计划工具**: 类似 `update_plan`，让模型将任务分解为可追踪的步骤
2. **计划状态可视化**: 步骤状态变化本身就是有价值的反馈
3. **验证闭环**: 不依赖模型"说自己做完了"，而是通过测试/构建结果验证
4. **单步骤推进**: 始终只有一个活跃步骤，避免上下文碎片化

---

## 3. 编辑系统

### 3.1 `apply_patch` 工具：自定义 Diff 格式

Codex CLI 的编辑系统设计是其最大的工程亮点之一。它不使用传统的 search-replace，而是设计了一种 **精简的、面向文件的 Diff 格式**，称为 "V4A Patch Format"。

### 3.2 格式语法

```
*** Begin Patch
*** Add File: path/to/new_file.py
+新文件内容行1
+新文件内容行2
*** Update File: path/to/existing.py
*** Move to: path/to/renamed.py
@@ class MyClass
 保留的上下文行
-删除的行
+新增的行
 保留的上下文行
*** Delete File: path/to/obsolete.txt
*** End Patch
```

完整语法定义（来自 `prompt_with_apply_patch_instructions.md`）：

```
Patch       := Begin { FileOp } End
Begin       := "*** Begin Patch" NEWLINE
End         := "*** End Patch" NEWLINE
FileOp      := AddFile | DeleteFile | UpdateFile
AddFile     := "*** Add File: " path NEWLINE { "+" line NEWLINE }
DeleteFile  := "*** Delete File: " path NEWLINE
UpdateFile  := "*** Update File: " path NEWLINE [ MoveTo ] { Hunk }
MoveTo      := "*** Move to: " newPath NEWLINE
Hunk        := "@@" [ header ] NEWLINE { HunkLine } [ "*** End of File" NEWLINE ]
HunkLine    := (" " | "-" | "+") text NEWLINE
```

### 3.3 关键设计约束

1. **文件引用必须是相对路径，绝对路径禁用**
2. **默认上下文 3 行**: 每个修改前后各显示 3 行作为定位上下文
3. **`@@` 定位符**: 当 3 行上下文不够唯一时，用 `@@ class ClassName` 或 `@@ def method()` 来定位
4. **多级 `@@` 嵌套**: 当代码块重复过多时，可以用多个 `@@` 跳转到正确上下文
5. **单次调用可包含多个文件操作**: Add + Update + Delete 都在一个 patch 中

### 3.4 与 OpenCode `search-replace` 的差异

| 维度 | Codex `apply_patch` | OpenCode `edit` |
|------|---------------------|-----------------|
| **编辑语义** | Unified Diff (增删改统一) | search-replace (查找替换) |
| **批量操作** | 单次调用多文件操作 | 每次调用一个文件 |
| **创建文件** | `Add File` header | 单独 `write` 工具 |
| **删除文件** | `Delete File` header | 单独 `bash rm` |
| **重命名** | `Move to` 内联指令 | 需要两次操作 |
| **git 原生性** | 接近 git diff 格式 | 更像 IDE 查找替换 |
| **模型训练** | 模型专门训练此格式 | 泛用 pattern |

### 3.5 为什么选择 Unified Diff 风格

从源码和 OpenAI Cookbook 中可以推断的设计理由：

1. **模型专门训练**: OpenAI 在 `gpt-5.2-codex`、`gpt-5.3-codex`、`gpt-5.5` 等模型上专门微调了 `apply_patch` 格式
2. **结构化解析**: 格式有明确的语法规则，可以安全解析和应用
3. **原子性操作**: 整个 patch 作为单一 shell 命令执行，要么全部成功要么全部失败
4. **可审查性**: Patch 格式天然可读，适合 diff review
5. **减少工具调用次数**: 一个 patch 可以跨多个文件，减少 API 往返

### 3.6 使用边界（来自 system prompt）

```
- 使用 apply_patch 进行单文件编辑
- 对于自动生成的文件 (如 package.json)、lint/format 命令的输出，不适用 apply_patch
- 对于全局字符串替换等脚本化操作，用 shell 工具而不是 apply_patch
```

### 3.7 对 Jeeves 的启示

1. **设计专用编辑格式**: 与其让模型生成"第 X 行改成 Y"，不如定义结构化 patch 格式
2. **单次操作多文件**: 显著减少 API 往返次数
3. **上下文定位机制**: `@@` 层级定位比纯行号更鲁棒
4. **原子性**: 整个 patch 作为原子操作，避免部分成功部分失败的状态

---

## 4. 权限系统演进

### 4.1 早期 `--full-auto` 标志的问题

Codex CLI 最初的权限模型使用简单的 CLI 标志：

```
# 早期方式（已被权限 Profile 替代）
codex --full-auto "create the fanciest todo-list app"
codex --sandbox workspace-write --ask-for-approval on-request
```

问题：
- **二进制选择**: 要么全部批准，要么全部拒绝
- **不透明**: 用户不知道什么操作被允许、什么被阻止
- **配置不可重用**: 每次运行都要指定标志
- **安全风险**: `--full-auto` 意味着完全信任，网络和文件系统无限制

### 4.2 当前权限架构（三轴模型）

Codex CLI v0.128+ 引入了 **三层权限架构**：

```
┌──────────────────────────────────────────────────────────┐
│                   Codex 权限系统                          │
│                                                          │
│  Layer 1: Sandbox Mode (沙箱模式)                        │
│  ├── read-only: 只能读取，不能修改                        │
│  ├── workspace-write: 默认。可读写工作区，禁止网络         │
│  └── danger-full-access: 无限制（仅用于一次性容器/VM）     │
│                                                          │
│  Layer 2: Approval Policy (审批策略)                     │
│  ├── untrusted: 每次操作都需要审批                        │
│  ├── on-request: 超出沙箱范围时审批（推荐）               │
│  ├── on-failure: 失败时审批                               │
│  └── never: 从不审批（需结合沙箱使用）                    │
│                                                          │
│  Layer 3: Network Access (网络访问)                      │
│  └── 仅在 workspace-write 模式下可配置                    │
└──────────────────────────────────────────────────────────┘
```

### 4.3 Permission Profiles（权限配置文件）

v0.128+ 引入了 **Named Permission Profiles**，将沙箱 + 审批 + 网络组合为可命名的策略：

```toml
# ~/.codex/config.toml

# 默认权限策略（日常开发推荐）
approval_policy = "on-request"
sandbox_mode = "workspace-write"

# 命名 Profile: 网络化开发
[profiles.networked]
approval_policy = "never"
sandbox_mode = "workspace-write"
[profiles.networked.sandbox_workspace_write]
network_access = true

# 命名 Profile: 完全自主（仅用于 CI/VM）
[profiles.yolo]
approval_policy = "never"
sandbox_mode = "danger-full-access"

# 内置预设 Profile（Beta）
# :read-only, :workspace, :danger-full-access
default_permissions = ":workspace"
```

使用方式：
```bash
codex --profile networked "install dependencies and run tests"
codex -p yolo "refactor the entire auth module"  # 仅在 VM 中使用!
```

### 4.4 细粒度控制

在 `workspace-write` 模式下，可以进一步细化：

```toml
[sandbox_workspace_write]
network_access = false
writable_roots = ["./src", "./tests", "./docs"]  # 只能写这些目录

# 命令前缀白名单
# npm test, pytest 允许; docker, curl 阻止
```

### 4.5 平台级沙箱强制

Codex 的沙箱不是提示词级别的"请求"，而是操作系统级别的强制：

| 平台 | 沙箱机制 |
|------|---------|
| macOS | Apple Seatbelt sandbox |
| Linux | Docker + 防火墙规则 |
| Windows | Windows Sandbox / WSL2 |

### 4.6 与 Claude Code 权限系统的对比

| 维度 | Codex CLI | Claude Code |
|------|-----------|-------------|
| **权限层次** | 3 层 (沙箱/审批/网络) | 7 层 (预过滤→拒绝→6模式→分类器→Shell沙箱→非恢复→Hook拦截) |
| **审批粒度** | 命令级 | 命令级 + 文件级 + 路径规则 |
| **自动分类** | 无 | Auto Mode 分类器（独立模型） |
| **平台强制** | OS 级 sandbox | 无 OS 级沙箱 |
| **Profile 系统** | 命名 Profile + 继承 | 模式 + deny-first 规则 |
| **复杂度** | 简洁，易理解 | 非常复杂，功能强大 |

**Codex 的设计哲学**: 用环境约束替代审批——"sandboxing is the guarantee"。

### 4.7 演进中的问题

从 GitHub Issues 可见当前权限系统的痛点：
- **配置兼容性问题** (#29100): CLI vs App vs Remote 不同入口的权限行为不一致
- **Profile 切换 Bug** (#23958): 会话中途权限模式自动切换
- **UI 与实际不一致** (#29503): 选择 "Full access" 但实际沙箱模式仍是 `workspace-write`
- **`danger-full-access` vs `danger-no-sandbox`** (#28715): 命名混乱导致的可用性问题

### 4.8 对 Jeeves 的启示

1. **三级沙箱模型**: read-only / workspace-write / full-access 是最小可行安全模型
2. **环境约束 > 审批提示**: OS 级别的沙箱比"请确认是否运行 rm -rf /"可靠得多
3. **命名 Profile 降低认知负担**: `--profile daily` 比记住三个标志的组合更友好
4. **细粒度目录控制**: `writable_roots` 让用户精确控制 Agent 的写入范围
5. **预留配置不一致的陷阱**: 多入口（CLI/IDE/远程）的权限一致性是关键

---

## 5. Hooks 系统

### 5.1 概述

Codex Hooks 是一个**确定性脚本注入框架**，在 Agent 生命周期关键节点自动执行用户定义的脚本。v0.130.0 (2026-05-08) GA 发布。

核心哲学：**"Hooks are not prompts. You can't ask the model to 'always remember to check X' and rely on that. A hook doesn't ask — it runs."**

### 5.2 生命周期事件

Codex 提供 **11 个生命周期钩子点**：

```
会话级别:
├── SessionStart    # 会话/子代理启动时
├── SessionEnd      # 主线程结束时（子代理不触发）
└── SubagentStart   # 子代理启动时

Turn 级别:
├── UserPromptSubmit # 用户提交提示时
├── PreToolUse       # 工具调用前
├── PermissionRequest # 权限请求时
├── PostToolUse      # 工具调用后
├── PreCompact       # 上下文压缩前 ⭐
├── PostCompact      # 上下文压缩后 ⭐
├── SubagentStop     # 子代理停止时
└── Stop             # 代理停止时
```

### 5.3 配置方式

```json
// hooks.json（用户级、项目级或插件内）
{
  "hooks": [
    {
      "event": "PostToolUse",
      "command": "python scripts/format-check.py",
      "description": "Run linter after every tool use"
    },
    {
      "event": "PreCompact",
      "command": "bash scripts/save-context.sh",
      "description": "Save context state before compaction"
    },
    {
      "event": "PostCompact",
      "command": "node scripts/reload-goal-state.js",
      "description": "Restore goal state after compaction"
    }
  ]
}
```

也可以在 `config.toml` 中内联定义：

```toml
[hooks.PostToolUse]
command = "bash scripts/audit-log.sh"
```

### 5.4 Plugin-Bundled Hooks

插件可以捆绑 hooks，分发给所有团队成员：

```json
// .codex-plugin/plugin.json
{
  "name": "security-scanner",
  "hooks": "./hooks/hooks.json"
}
```

这支持团队级安全策略：安全团队维护一个包含 hooks 的插件（密钥扫描、审计日志、合规检查），通过插件市场分发给所有开发者。

### 5.5 Pre/Post Compaction Hooks 的关键作用

**`PreCompact`**: 在上下文被压缩前执行，用于：
- 保存 Goal 状态
- 记录当前上下文摘要
- 将关键信息写入外部存储

**`PostCompact`**: 压缩完成后执行，用于：
- 恢复 Goal 状态到新上下文
- 重新注入项目特定指令
- 验证压缩后 Agent 行为一致性

**实际价值**: 在长时间运行的 Goal 工作流中（可能跨越多次 compaction），这些 hooks 确保 Agent 不会因为上下文压缩而"丢失记忆"。

### 5.6 Hook 发现机制

Codex 从多个来源发现 hooks（优先级由低到高）：
1. 插件内置 hooks
2. 项目级 `hooks.json`
3. 用户级 `~/.codex/hooks.json`
4. 管理级 managed hooks
5. `config.toml` 内联 hooks

### 5.7 与 OpenCode Hooks 的对比

| 维度 | Codex CLI | OpenCode |
|------|-----------|----------|
| **Hook 数量** | 11 个生命周期事件 | 20+ 个 hook 点 |
| **Hook 类型** | 命令执行 (shell scripts) | Pipeline pattern (函数) |
| **Pre/Post Compaction** | 原生支持 ⭐ | 不内置 |
| **Plugin 分发** | 通过插件市场分发 hooks | 通过 agent 定义分发 |
| **实现方式** | 进程外脚本执行 | PluginContext 内 JS/TS |

### 5.8 对 Jeeves 的启示

1. **Pre/Post Compaction hooks 是关键功能**: 长对话中最脆弱的环节就是上下文压缩
2. **Hooks 应该是确定性的**: 不走 LLM，直接执行脚本，确保可靠
3. **插件化分发**: Hooks 作为团队安全策略的载体，通过插件统一管理
4. **生命周期全覆盖**: 从 Session 到 Tool 到 Compaction 到 Stop，覆盖完整的 Agent 生命周期

---

## 6. Goals 系统

### 6.1 概述

`/goal` 命令（v0.128.0+, v0.133.0 起默认启用）是 Codex CLI 的**持久化跨会话工作流系统**。它将 Codex 从"单轮问答工具"转变为"长时间运行的异步任务执行器"。

### 6.2 核心设计

```
┌─────────────────────────────────────────────────────────────┐
│                     /goal 生命周期                           │
│                                                             │
│  用户定义目标                                               │
│  /goal "Produce the strongest evidence-backed               │
│         reproduction of the paper..."                       │
│       │                                                     │
│       ▼                                                     │
│  Codex 调查 + 制定计划 (update_plan)                        │
│       │                                                     │
│       ▼                                                     │
│  执行 → 验证 → 修复 → 循环...                               │
│       │         │                                           │
│       │    ┌────┘                                           │
│       ▼    ▼                                                │
│  检查完成条件:                                              │
│  - 测试全部通过? → 完成                                     │
│  - 指标达标? → 完成                                         │
│  - 明确受阻? → 报告并暂停                                   │
│  - 预算耗尽? → 停止                                         │
│                                                             │
│  控制命令:                                                  │
│  /goal pause    # 暂停目标                                  │
│  /goal resume   # 恢复目标                                  │
│  /goal clear    # 清除目标                                  │
└─────────────────────────────────────────────────────────────┘
```

### 6.3 关键特性

1. **持久化状态**: v0.133.0 起，Goal 状态有专用存储，跨 CLI 重启存活
2. **完成合约**: Goal 不是"永远运行"，而是定义明确的可验证完成条件
3. **进度追踪**: 通过 `update_plan` 在整个 Goal 生命周期中追踪进度
4. **预算管理**: Goal 受配额/预算限制，防止无限消耗
5. **生命周期控制**: create → pause → resume → clear → complete

### 6.4 使用模式

```bash
# 定义目标
/goal Refactor the auth module to use the new token format.
       All existing tests must pass. No API changes.

# Codex 自动:
# 1. 调查 auth 模块
# 2. 制定计划 (update_plan)
# 3. 逐步执行重构
# 4. 运行测试验证
# 5. 检查 API 兼容性
# 6. 完成或报告受阻

# 用户可以在任何时候:
/goal pause    # "等一下,我要先看看进展"
/goal resume   # "继续"
/goal clear    # "放弃这个目标"
```

### 6.5 与 AGENTS.md 的配合

Goal 定义"意图"（这次要达成什么），`AGENTS.md` 定义"边界"（每个会话都应该遵守的规则）：

- **Goal** = 当前会话的持久目标
- **AGENTS.md** = 每个会话都应该为真的条件（默认模型、验证代码片段、什么时候升级到更强模型）

### 6.6 Goals vs 普通 Prompt

| 维度 | 普通 Prompt | /goal |
|------|-----------|-------|
| **执行轮次** | 2-3 轮 | 持续到完成条件满足 |
| **状态持久化** | 无 | 跨会话持久化 |
| **完成判断** | 模型感觉"做完了" | 用户定义的证据标准 |
| **失败处理** | 用户介入 | 自动重试 + 报告受阻 |
| **上下文管理** | 单会话内 | 跨 compaction 保持 |

### 6.7 OpenAI 的官方使用建议

来自 `developers.openai.com/cookbook/examples/codex/using_goals_in_codex`：

- ✅ 适合: 调试复杂 bug、大规模重构、数据迁移、论文复现、多轮 PR 审查
- ❌ 不适合: 单行编辑、简单解释、短代码审查、模糊的"让它更好"
- ⚠️ 关键: 必须定义**可验证的完成条件**——"所有测试通过"、"API 无变化"

### 6.8 与 Hermes Cronjob 的对比

| 维度 | Codex Goals | Hermes Cronjob |
|------|-------------|----------------|
| **触发方式** | 用户 `/goal` 命令 | 定时触发 |
| **执行持续性** | 持续运行直到完成 | 按 cron 表达式触发 |
| **状态管理** | 内置生命周期 (pause/resume/clear) | 基于日记和工具结果 |
| **适用场景** | "重构认证模块" | "每天检查文档新鲜度" |
| **共通点** | 都是跨会话的持久化工作流 | |

### 6.9 对 Jeeves 的启示

1. **持久化目标状态**: Goal 不是"一次性任务"，而是有生命周期的合约
2. **证据驱动的完成条件**: "所有测试通过"比"我感觉做好了"可靠得多
3. **预算和配额管理**: 长时间运行的成本控制是生产级 Agent 的必备能力
4. **Goal + 边界文件模式**: 用 AGENTS.md 提供每个会话都要遵守的约束，Goal 定义当前会话的特定目标

---

## 7. 沙箱执行

### 7.1 核心哲学

Codex 的沙箱设计体现了一个关键洞察：**"Autonomy requires boundaries. The more you want an agent to do on its own, the tighter the environment needs to be."**

这不是 "tool-based agents"（给模型一组工具），而是 **"environment-based agents"**（给模型一个有边界的运行环境）。

### 7.2 双表面双层沙箱

#### Cloud Sandbox（云沙箱）

```
┌─────────────────────────────────────────┐
│           Codex Cloud Sandbox            │
│                                          │
│  ┌────────────────────────────────┐     │
│  │    隔离的 MicroVM              │     │
│  │    - 独立文件系统              │     │
│  │    - 独立进程空间              │     │
│  │    - 网络访问: 故意限制        │     │
│  │    - 不能 pip install          │     │
│  │    - 不能调外部 API            │     │
│  │    - 不能外泄代码              │     │
│  └────────────────────────────────┘     │
│                                          │
│  输出: 只有 PR 或 Diff                   │
│  你的实际文件: 永远不被触碰              │
└─────────────────────────────────────────┘
```

- 每个任务一个隔离容器
- 从 GitHub 克隆仓库到沙箱
- 在沙箱内完成所有工作
- 输出 PR/diff，用户审查后合并
- 任务完成后环境销毁

#### Local Sandbox（本地沙箱）

在 CLI/IDE 中运行时，沙箱在**操作系统级别**强制实施：

| 平台 | 机制 |
|------|------|
| macOS | Apple Seatbelt sandbox |
| Linux | 内核命名空间 + 防火墙 |
| Windows | Windows Sandbox / WSL2 |

**本地沙箱的关键设计**:
- 不是提示词级别的"建议"，而是 OS 内核强制执行
- `workspace-write` 模式下 Agent 不能访问工作区外的文件
- 网络默认关闭（除非显式配置 `network_access = true`）

### 7.3 三层沙箱模式详解

```
read-only          workspace-write        danger-full-access
─────────          ───────────────        ──────────────────
能看不能改          默认模式               完全无限制
                   - 读写工作区文件        - 访问整个文件系统
探索代码库          - 运行常规命令          - 无限制网络访问
代码审查            - Git 操作             - 仅用于一次性 VM/CI
回答问题            - 包管理器操作          - 绝对不要在日常
                    - 测试运行器            开发机使用
                    网络: 默认关闭
```

### 7.4 环境生命周期管理

```
Cloud 模式:
  创建 → 克隆仓库 → 执行任务 → 生成 PR → 销毁环境

Local 模式:
  配置沙箱 → 启动 Agent → 执行任务 → 沙箱持续存在
  （环境跟随开发机器生命周期）

Git Worktree 模式 (OpenAI 内部):
  每个 PR/变更创建一个 Git worktree
  → Agent 在隔离的 worktree 中工作
  → 自带 ephemeral 观测栈 (logs, metrics, traces)
  → 任务完成后 worktree 销毁
```

### 7.5 OpenAI 内部实践

来自 Harness Engineering 博客：

- **每个 Git worktree 独立启动应用实例**，Agent 可以驱动和验证
- **Chrome DevTools Protocol 接入 Agent 运行时**，支持 DOM snapshots、截图、导航
- **临时观测栈**: LogQL (日志) + PromQL (指标) + TraceQL (追踪)
- 支持 "ensure service startup completes in under 800ms" 这类精确的验证指令

### 7.6 对 Jeeves 的启示

1. **OS 级沙箱 > 提示词级"请勿"**: 安全的唯一保证是内核强制
2. **环境即边界**: 给 Agent 一个受限的运行环境，而不是一串受限的工具
3. **Cloud + Local 双模式**: 安全敏感任务用 Cloud，快速迭代用 Local
4. **Git Worktree 隔离**: 每个任务一个 worktree，互不干扰
5. **可观测性作为验证**: 日志/指标/追踪应该对 Agent 可读

---

## 8. Chrome Extension 集成

### ⭐ 这是 Codex CLI 的独特创新

### 8.1 核心问题

传统 AI Agent 面临一个瓶颈：**无法访问需要登录的 Web 应用**。Headless 浏览器没有用户的 session cookie，无法操作 Gmail、Salesforce、LinkedIn、内部 Grafana 面板等需要认证的服务。

### 8.2 Codex Chrome Extension 解决方案

2026-05-07 发布（v0.130.0），Codex Chrome Extension 让 Agent 可以**直接操作用户的实际 Chrome 浏览器**：

```
┌──────────────────────────────────────────────────────────┐
│              Codex Chrome Extension 架构                  │
│                                                          │
│  用户 Chrome 浏览器                                       │
│  ┌─────────────────────────────────────┐                │
│  │  Codex Chrome Extension             │                │
│  │  - 访问已登录的 session cookie      │                │
│  │  - 并行操作多个标签页               │                │
│  │  - 后台静默工作                     │                │
│  │  - 不接管整个浏览器                 │                │
│  └──────────────┬──────────────────────┘                │
│                 │ 通信                                    │
│  ┌──────────────▼──────────────────────┐                │
│  │  Codex App Server (本地)            │                │
│  │  - Agent 通过 App Server 发送指令    │                │
│  │  - Extension 返回 DOM snapshots     │                │
│  └─────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────┘
```

### 8.3 三层浏览器策略

Codex 采用**三层浏览器策略**，根据任务需要自动选择：

```
Layer 1: Plugins (专用集成)
  → GitHub API、Slack API、Figma API ...
  → 最快、最可靠，但需要专门开发

Layer 2: Chrome Extension (认证会话) ⭐
  → LinkedIn、Salesforce、Gmail、内部工具
  → 利用用户已有的登录状态
  → per-site 权限确认

Layer 3: In-App Browser (本地开发)
  → localhost、本地 dev server、公开页面
  → 不需要认证的场景
  → 沙箱化浏览器，不接触用户 Chrome profile
```

### 8.4 安全设计

- **Per-site 确认**: 首次访问一个新域名时，需要用户明确批准
- **Allowlist/Blocklist**: 用户可管理允许和阻止的站点列表
- **标签页状态可视化**: 通过 tab icon 显示 Agent 正在操作的标签页
- **不接管浏览器**: 后台静默工作，用户可以继续正常浏览
- **高风险权限手动审批**: 浏览器历史访问等高风险功能需要每次会话手动授权

### 8.5 使用方式

```bash
# Codex 自动根据任务选择浏览器层
@Chrome open Salesforce and update the opportunity status to "Closed Won"

# Codex 自动:
# 1. 通过 Chrome Extension 在你的已登录 Salesforce 中操作
# 2. 读取当前页面状态
# 3. 执行更新操作
# 4. 验证结果
```

### 8.6 对 Jeeves 的启示

1. **认证会话是 Agent 浏览器能力的关键瓶颈**: Headless 浏览器无法替代
2. **三层策略**: 专用 Plugin > 认证浏览器 Extension > 沙箱浏览器
3. **安全粒度**: per-site 权限 + allowlist/blocklist
4. **这个能力是 Jeeves 可以差异化竞争的方向**: 如果 Jeeves 能提供一个类似机制，将大幅提升实用性

---

## 9. Remote Control & Mobile

### ⭐ 这是 Codex CLI 的独特创新

### 9.1 核心概念

Codex Remote Control 让用户可以从手机（ChatGPT App）或另一台电脑控制运行在桌面机器上的 Codex Agent，执行编码任务、审查输出、指导执行、批准操作。

### 9.2 架构

```
┌──────────────────────────────────────────────────────────┐
│                 Codex Remote Control 架构                 │
│                                                          │
│  手机 (ChatGPT App)                                       │
│  ┌──────────────────────────────┐                       │
│  │  查看活跃 Threads            │                       │
│  │  发送新指令                  │                       │
│  │  审查 Diff 输出              │                       │
│  │  批准/拒绝操作               │                       │
│  │  访问项目文件                │                       │
│  └──────────┬───────────────────┘                       │
│             │ HTTPS/WebSocket                             │
│             ▼                                            │
│  桌面主机 (Codex 运行在这里)                              │
│  ┌──────────────────────────────┐                       │
│  │  ChatGPT Desktop App         │                       │
│  │  ┌────────────────────────┐  │                       │
│  │  │ Codex App Server       │  │                       │
│  │  │ (remote-control 模式)   │  │                       │
│  │  │ - 管理 Agent 生命周期   │  │                       │
│  │  │ - 推送实时上下文到手机  │  │                       │
│  │  │ - 双向消息传递          │  │                       │
│  │  └────────────────────────┘  │                       │
│  └──────────────────────────────┘                       │
│                                                          │
│  所有计算都在桌面主机上执行                               │
│  手机只是遥控器                                          │
└──────────────────────────────────────────────────────────┘
```

### 9.3 关键特性

1. **所有计算在桌面执行**: 手机只是遥控器，不消耗手机资源
2. **完整功能访问**: 文件、Shell、凭证、插件、MCP 服务器、沙箱设置
3. **实时推送**: 桌面 Codex 将活动线程上下文实时推送到手机
4. **批准流程**: 手机端可以审批 Agent 的操作请求

### 9.4 CLI 命令

```bash
# 启动 Remote Control daemon
codex remote-control start

# 前台模式（显示状态）
codex remote-control

# 查看状态
codex remote-control status

# 停止
codex remote-control stop

# 低级 API（供自定义客户端）
codex app-server --listen
```

### 9.5 三种 Remote 模式

| 模式 | 使用场景 | 命令 |
|------|---------|------|
| **ChatGPT Mobile** | 手机控制桌面 | ChatGPT App → Settings → Remote Control → 扫描 QR 码 |
| **Remote Control Daemon** | 无头服务器操作 | `codex remote-control start` |
| **App Server Listener** | 自定义客户端/IDE | `codex app-server --listen` |

### 9.6 移动端体验

```
ChatGPT App 中的 Codex 界面:
├── 查看活跃 Threads (桌面正在运行的任务)
├── 发送新指令
├── 实时查看 Agent 输出
├── 审查 Diff
├── 批准/拒绝工具调用
└── 访问项目文件
```

### 9.7 SSH 远程连接

除了 ChatGPT App 方式，Codex 也支持通过 SSH 连接远程机器：

```bash
# 从 ChatGPT App → Settings → Connections → Remote
# 通过 SSH 连接到运行 Codex 的服务器
# 在无 GUI 的 headless 服务器上运行 Codex
```

### 9.8 对 Jeeves 的启示

1. **移动端是 Agent 的杀手级应用场景**: 检查长时间运行的任务、批准关键操作
2. **计算在本地，控制在远端**: 不需要在手机上运行 Agent
3. **双向通信协议**: JSON-RPC over stdio/WebSocket 是关键基础设施
4. **App Server 作为统一入口**: 所有远程能力通过同一个 App Server 协议暴露
5. **Jeeves 可以考虑类似的远程控制能力**: 特别是对于需要在开发机上长时间运行的任务

---

## 10. 模型路由

### 10.1 模型矩阵

Codex CLI 支持多层次的模型选择：

| 模型 | 定位 | 适用场景 | 特性 |
|------|------|---------|------|
| **GPT-5.5** | 推荐默认模型 | 大多数编码任务 | 最新前沿模型，实现、重构、调试、测试 |
| **GPT-5.4** | 旗舰编码模型 | 复杂推理 + 工具使用 | 被 GPT-5.5 逐步取代，但仍广泛使用 |
| **GPT-5.4-mini** | 快速/高效 | 子代理、轻量任务 | 成本低、速度快 |
| **GPT-5.3-Codex-Spark** | 实时迭代 | Pro 用户专享 | 1000+ tok/sec, Cerebras 硬件加速 |
| **GPT-5.6 Sol** | 细节和打磨 | 需要极致质量的场景 | 最新模型系列 |
| **GPT-5.6 Terra** | 日常主力 | 替代 GPT-5.4 | GPT-5.6 系列的工作马 |
| **GPT-5.6 Luna** | 清晰可重复 | 简单明确的任务 | 成本最低的 GPT-5.6 |

### 10.2 路由逻辑

Codex CLI 本身**不做自动模型路由**（模型选择是显式配置的），但存在一些隐式路由机制：

1. **用户显式配置**: `config.toml` 中 `model = "gpt-5.5"`
2. **Profile 覆盖**: 通过 `--profile` 或 `--model` CLI 参数切换
3. **OpenAI 服务端路由**: 当选择 "Auto" 时，服务端根据任务复杂度在模型间路由
4. **后端子代理路由**: Codex 可能在内部使用轻量模型处理子任务

### 10.3 实际使用观察

根据社区反馈（来自 OpenAI 论坛）：
- 用户配置 `model = "gpt-5.5"` 但 Analytics 显示大部分 turns 使用了 GPT-5.4
- 原因可能是：**容量限制下的自动回退**、**账号配额策略**、**任务类型自动路由**
- 这说明 Codex 存在不被文档化的隐式路由行为

### 10.4 配置方式

```toml
# ~/.codex/config.toml
model = "gpt-5.5"  # 默认模型

[profiles.fast]
model = "gpt-5.4-mini"
model_reasoning_effort = "minimal"

[profiles.deep]
model = "gpt-5.5"
model_reasoning_effort = "high"
```

### 10.5 对 Jeeves 的启示

1. **多模型分层**: 不同任务复杂度对应不同模型
2. **显式配置 + 智能回退**: 用户指定偏好，系统在容量不足时自动降级
3. **Profile 绑定模型**: 通过 Profile 将模型选择与权限/沙箱策略绑定
4. **Jeeves 应该支持模型路由**: 轻量任务用便宜模型，复杂重构用强模型

---

## 11. App Server 架构

### 11.1 设计初衷

Codex CLI 最初只是 TUI。当需要驱动 VS Code 扩展时，团队需要一个方式复用同一套 Agent 循环：

> "We needed a way to use the same harness so as to drive the same agent loop from an IDE UI without re-implementing it."

结果就是 **App Server**: 一个 client-friendly、双向 JSON-RPC over stdio API。

### 11.2 架构组件

```
┌──────────────────────────────────────────────────────────┐
│                App Server 进程                            │
│                                                          │
│  ┌────────────────┐     ┌─────────────────────┐         │
│  │  stdio reader   │────▶│ Codex msg processor │         │
│  │  (JSON-RPC 入口) │     │ (请求路由/事件转换)  │         │
│  └────────────────┘     └─────────┬───────────┘         │
│                                   │                      │
│                         ┌─────────▼───────────┐         │
│                         │  Thread Manager     │         │
│                         │  (线程生命周期管理)  │         │
│                         └─────────┬───────────┘         │
│                                   │                      │
│                    ┌──────────────┼──────────────┐      │
│                    ▼              ▼              ▼      │
│                Core Thread   Core Thread    Core Thread  │
│                (Thread 1)    (Thread 2)     (Thread 3)   │
│                每个对应一个会话                            │
└──────────────────────────────────────────────────────────┘
```

### 11.3 三项对话原语

App Server 将 Agent 交互抽象为三个核心原语：

| 原语 | 定义 | 生命周期 |
|------|------|---------|
| **Item** | 原子输入/输出单元（用户消息、Agent 消息、工具执行、审批请求、diff） | `item/started` → `item/*/delta` → `item/completed` |
| **Turn** | 一次用户输入触发的完整 Agent 工作单元 | `turn/started` → items... → `turn/completed` |
| **Thread** | 持久化的会话容器，包含多个 Turns | `thread/started` → turns... → 可恢复/归档 |

### 11.4 协议细节

- **传输层**: JSON-RPC over stdio (JSONL 格式)
- **双向通信**: 客户端发送请求，服务端推送通知，服务端也可发起请求（如审批）
- **审批流程**: 服务端发送审批请求 → 暂停 Turn → 等待客户端 allow/deny → 继续
- **流式更新**: Agent 消息通过 `agentMessage/delta` 事件流式推送

### 11.5 客户端支持

App Server 协议已被多种语言的客户端实现：
- Go, Python, TypeScript, Swift, Kotlin
- 自动生成: `codex app-server generate-ts` (TypeScript)
- 自动生成: `codex app-server generate-json-schema` (JSON Schema → 任意语言)

### 11.6 对 Jeeves 的启示

1. **Harness 与 UI 分离**: Agent 逻辑与前端解耦，通过协议通信
2. **Item/Turn/Thread 三层抽象**: 天然支持多会话、多轮交互
3. **双向协议**: 不仅客户端请求服务端，服务端也可以主动请求客户端（如审批）
4. **流式通信**: 不只是响应式，而是持续推送状态更新

---

## 12. 核心引擎: Codex Core

### 12.1 位置与角色

`codex-rs/core/` 是整个 Codex 生态的"引擎舱"：

- **既是库也是运行时**: 可以被编译为库供其他 Rust 代码调用，也可以作为独立运行时
- **管理一个 Codex Thread 的完整生命周期**: 创建、运行、持久化
- **包含 Agent Loop**: 模型推理 → 工具调用 → 结果反馈的核心循环

### 12.2 Prompt 构建流程

来自 `codex-rs/core/src/codex.rs` 的大致流程：

```
1. developer 角色消息: 沙箱权限说明
   (从 protocol/src/prompts/permissions/ 加载 Markdown 片段)

2. (可选) developer_instructions: 用户 config.toml 中的 developer_instructions

3. user 角色消息: 项目文档
   AGENTS.override.md → AGENTS.md → project_doc_fallback_filenames
   优先级: 更具体的文件排在后面（覆盖前面的）

4. user 角色消息: 环境上下文
   <environment_context>
     <cwd>/path/to/project</cwd>
     <shell>zsh</shell>
   </environment_context>

5. user 角色消息: 用户的实际输入
```

### 12.3 Prompt 缓存优化

Codex 在 prompt 构建中极度注重缓存命中率：

- **静态内容在前**: instructions、tools 等不变内容放在 prompt 前面
- **动态内容在后**: 用户消息等可变内容放在末尾
- **避免修改已有消息**: 权限变更时追加新 `developer` 消息而不修改旧的
- **MCP 工具排序 Bug (#2611)**: 工具列表排序不一致导致缓存失效，已修复
- **MCP 工具列表动态变化**: `notifications/tools/list_changed` 可在对话中触发，需特殊处理

### 12.4 Auto Compaction

```rust
// 大致逻辑 (codex-rs/core/src/codex.rs)
if token_count > auto_compact_limit {
    // 使用 Responses API 的 /responses/compact 端点
    // 返回 type=compaction 的 item (含 encrypted_content)
    // 替代原来的 input，释放上下文窗口
}
```

**关键设计**:
- `encrypted_content` 保留模型的潜在理解（不仅仅是文本摘要）
- `auto_compact_limit` 可配置（在 `config.toml` 中）
- 早期实现是手动 `/compact` 命令 + 模型摘要；现在是自动 + 专用 API

### 12.5 模型特定指令

每个支持的模型有独立的提示词文件:
- `gpt-5.2-codex_prompt.md` — 编辑约束、前端任务、展示规则
- 未来模型会有各自的提示词变体

---

## 13. Harness Engineering 实践

### 13.1 OpenAI 的核心理念

来自 Harness Engineering 博客的核心洞察：

1. **Humans steer. Agents execute.**: 人类设计环境、指定意图、构建反馈循环
2. **No manually-written code**: 自始至终，零行人工手写代码
3. **Repository knowledge as system of record**: 仓库本地文档是唯一真相来源
4. **Agent legibility is the goal**: 代码库首先要对 Agent 可读
5. **Progressive disclosure**: AGENTS.md 是目录，不是百科全书

### 13.2 仓库知识库结构

```
AGENTS.md                   # 简短的"目录"（约 100 行）
ARCHITECTURE.md             # 顶层架构地图
docs/
├── design-docs/            # 设计文档（含 verification status + core beliefs）
│   ├── index.md
│   └── core-beliefs.md
├── exec-plans/             # 执行计划（第一等 artifacts）
│   ├── active/
│   ├── completed/
│   └── tech-debt-tracker.md
├── generated/              # 自动生成的文档（如 db-schema.md）
├── product-specs/          # 产品规格
├── references/             # LLM 友好的参考资料
├── QUALITY_SCORE.md        # 各领域质量评分
├── RELIABILITY.md
└── SECURITY.md
```

### 13.3 关键工作流

1. **Ralph Wiggum Loop**: Codex 审查自己的变更 → 请求其他 Agent 审查 → 响应反馈 → 循环直到所有 Agent 审查者满意
2. **Doc-gardening Agent**: 定期扫描过期文档并自动提交修复 PR
3. **CI 强制执行**: 专用 linter 和 CI 任务验证知识库结构正确性

---

## 14. Jeeves 可借鉴清单

### P0（立即实施）

| # | 借鉴点 | 来源 | 实施建议 |
|---|--------|------|---------|
| 1 | **PEV 计划工具** | §2 | 实现类似 `update_plan` 的结构化进度追踪工具，包含 pending/in_progress/completed 状态 |
| 2 | **结构化编辑格式** | §3 | 设计 Jeeves 专用的 patch 格式，支持单次调用多文件操作 |
| 3 | **三级沙箱 + 审批策略** | §4 | read-only / workspace-write / full-access + on-request / never |

### P1（短期规划）

| # | 借鉴点 | 来源 | 实施建议 |
|---|--------|------|---------|
| 4 | **Hooks 系统** | §5 | 至少实现 PreToolUse/PostToolUse + PreCompact/PostCompact hooks |
| 5 | **持久化 Goals** | §6 | 类似 `/goal` 的跨会话工作流，含 pause/resume/clear 生命周期 |
| 6 | **App Server 协议** | §11 | Harness 与 UI 分离，通过 JSON-RPC 通信 |

### P2（中期规划）

| # | 借鉴点 | 来源 | 实施建议 |
|---|--------|------|---------|
| 7 | **权限 Profiles** | §4 | 命名策略 + 继承 + 多来源合并 |
| 8 | **OS 级沙箱集成** | §7 | macOS Seatbelt / Linux Namespaces / Windows Sandbox |
| 9 | **Prompt Caching 优化** | §12 | 静态内容在前、动态内容在后、避免修改已有消息 |
| 10 | **模型路由** | §10 | 轻量任务用便宜模型、复杂任务用强模型 |

### P3（长期愿景）

| # | 借鉴点 | 来源 | 实施建议 |
|---|--------|------|---------|
| 11 | **浏览器 Extension** | §8 | 认证会话的浏览器集成（Codex 独有创新） |
| 12 | **Remote Control** | §9 | 移动端控制 + 无头操作（Codex 独有创新） |
| 13 | **Cloud Sandbox** | §7 | 每任务隔离的云执行环境 |
| 14 | **Git Worktree 隔离** | §7 | 每任务独立 worktree + ephemeral 观测栈 |

---

## 附录: 参考资料

1. [Harness Engineering: Leveraging Codex in an agent-first world](https://openai.com/index/harness-engineering) — OpenAI 工程博客
2. [Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop) — Agent Loop 深度剖析
3. [Unlocking the Codex harness: how we built the App Server](https://openai.com/index/unlocking-the-codex-harness) — App Server 架构
4. [Using Goals in Codex](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex) — Goals 官方 Cookbook
5. [Codex CLI GitHub Repository](https://github.com/openai/codex) — 源代码 (Rust)
6. [Codex CLI Prompt Instructions](https://github.com/openai/codex/blob/main/codex-rs/core/prompt_with_apply_patch_instructions.md) — apply_patch 格式定义
7. [OpenCode vs Codex CLI (August 2026)](https://www.morphllm.com/comparisons/opencode-vs-codex) — MorphLLM 对比分析
8. [Top Agent Harnesses: Claude Code vs Codex](https://www.aimultiple.com/agent-harness) — AIMultiple 对比
9. [OpenAI Codex Sandboxing](https://cobusgreyling.medium.com/openai-codex-sandboxing-53fbcf61ed40) — 沙箱机制详解
10. [Codex Hooks](https://learn.chatgpt.com/docs/hooks) — 官方 Hooks 文档
11. [Codex Chrome Extension](https://www.verdent.ai/guides/codex-chrome-extension-explained) — Chrome 扩展详解
12. [Codex Remote Connections](https://learn.chatgpt.com/docs/remote-connections) — Remote Control 官方文档
