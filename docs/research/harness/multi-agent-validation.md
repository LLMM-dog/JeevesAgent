# Jeeves 多智能体架构设计 — 主流项目一致性验证

> **验证日期**：2026-08-08
> **对比项目**：OpenCode（194K+ Stars）、Claude Code（Anthropic）、Hermes Agent（Nous Research）、Goose（15K+ Stars）
> **Jeeves 设计版本**：v2（2026-08-08）
> **设计文档来源**：`docs/architecture/multi-agent.md`、`multi-agent-schema.md`、`verification-agent.md`

---

## 总体结论

Jeeves 的多智能体架构有 **3 项与主流一致**、**1 项超越主流（但伴随风险）**、**1 项缺乏先例（需谨慎推进）**。核心 AgentDefinition 设计正确，但记忆三层隔离和智能体自管理 skills 是超前设计，建议简化起步。验证智能体作为每智能体属性是正确的，但 API 设计存在遗留不一致。

---

## 1. AgentDefinition 作为独立实体 — ✅ 一致

### Jeeves 设计

```
AgentDefinition = name + description + system_prompt + model_id
                + skill_names + mcp_servers + permissions[5]
                + verification_enabled + strict_mode
```

存入 `agent_defs` 表（SQLite），支持 CRUD API，前端管理页。

### 对比项目做法

| 项目 | Agent 定义方式 | 核心字段 | 存储位置 |
|------|---------------|----------|----------|
| **OpenCode** | JSON 配置 / `.md` 文件（YAML frontmatter） | `name`, `description`, `mode`(primary/subagent), `model`, `prompt`, `permission`(deny/ask/allow ruleset), `tools`, `temperature`, `steps`, `color`, `hidden` | `opencode.json` 或 `.opencode/agents/*.md` |
| **Claude Code** | `.claude/agents/*.md`（YAML frontmatter） | `name`, `description`, `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `skills`, `mcpServers`, `hooks`, `background`, `isolation` | `.claude/agents/*.md` 或 `~/.claude/agents/*.md` |
| **Hermes Agent** | ❌ 无 AgentDefinition 概念 | 单 Agent 系统，skills/profile 级别配置 | N/A |
| **Goose** | ❌ 无 AgentDefinition 概念 | 单 Agent struct，通过 Recipe + Extension 区分能力 | N/A |

### 一致性判断

**完全一致**。Jeeves 的 AgentDefinition 设计模式与 OpenCode 和 Claude Code 高度一致：
- Agent = 身份（name, description）+ 行为（system_prompt）+ 模型绑定（model）+ 能力（skills, MCP）+ 权限（permissions）
- 这是业界经过验证的标准范式

### 细微差异（非问题）

| 维度 | Jeeves | OpenCode / Claude Code |
|------|--------|----------------------|
| 存储方式 | SQLite 数据库表 | 文件系统（.md / .json） |
| `mode` 字段 | 无（通过群组成员关系隐式区分） | 显式 `primary` / `subagent` |
| `hidden` 字段 | 无 | 有（内部 agent 不显示在 UI） |
| `steps`/`maxTurns` | 无 | 有（最大迭代步数保护） |

**分析**：
- **数据库 vs 文件**：Jeeves 作为 Web 应用选数据库是正确的（需动态 CRUD、引用完整性、前端查询）。OpenCode/Claude Code 是 CLI 工具，文件系统更自然。
- **mode 字段缺失**：Jeeves 通过"群组中的 role（worker/aggregator）"隐式表达主/子关系。这在实际使用中可能不如显式 `mode` 直观——用户会问"为什么 Plan Agent 不能直接对话？"建议至少加上注释或在文档中明确说明。

### 建议

1. **增加 `hidden` 字段**：验证智能体、compaction 智能体等内部 agent 不应出现在用户选择器中。OpenCode 的内置 agent（compaction、title、summary）全部 `hidden: true`。
2. **增加 `steps`/`max_turns` 字段**：每个 agent 应该能限制最大迭代次数，防止无限循环。Claude Code 的子 agent `maxTurns`、OpenCode 的 `steps`、Goose 的 `DEFAULT_MAX_TURNS=1000` 都是此模式。这是 **安全刚需**，不是可选功能。

---

## 2. 每智能体独立 Memory 隔离 — ⚠️ 超前但伴随风险

### Jeeves 设计

```
三层隔离：
├── target="user"     (agent_id=NULL) — 跨智能体共享
├── target="agent"    (agent_id="adf_xxx") — 智能体跨会话积累
└── target="session"  (agent_id="adf_xxx", session_id="ses_xxx") — 每会话独立
```

验证智能体另有独立记忆：`agent_id="adf_xxx__verification"`。

### 对比项目做法

| 项目 | Memory 机制 | 是否按 Agent 隔离 |
|------|------------|-------------------|
| **OpenCode** | Session-based 消息持久化（SQLite）。子 agent 创建 child session，有独立的消息历史。无跨 session 的持久记忆。 | ❌ 无跨会话记忆 |
| **Claude Code** | CLAUDE.md 层级（Managed/User/Project/Local）+ auto-memory（写入全局文件）。子 agent 有独立的 context window + sidechain JSONL。无 agent 级持久记忆。 | ❌ 全局记忆 |
| **Hermes Agent** | User Profile (1375 char) + Agent Notes (2200 char)。两个都是全局的，不按 agent 隔离。 | ❌ 全局记忆 |
| **Goose** | Session-based 持久化。Recipe 提供持久化上下文。无 agent 级记忆。 | ❌ 无跨会话记忆 |

### 一致性判断

**超越主流项目**。四个参考项目中，**没有一个**实现 per-agent 持久化记忆隔离。Jeeves 的三层记忆设计是更先进的——但这也意味着它是**未经实践验证的**。

### 风险评估

| 风险 | 严重度 | 说明 |
|------|--------|------|
| **记忆碎片化** | 中 | 不同 agent 各自学习，无法共享经验。"代码审查员"学到的项目结构，"研究员"完全不知道——这违反用户直觉 |
| **查询复杂度** | 低 | `agent_id IN ('', 'current_agent_id')` 查询需要正确编写索引 |
| **验证智能体的 hacky ID** | 中 | `"adf_xxx__verification"` 用双下划线后缀在同一个 `agent_id` 字段区分验证记忆。这不规范——应该用独立字段或嵌套 agent 关系 |
| **存量数据兼容** | 低 | `agent_id ''` = 全局共享，设计正确 |

### 最严重的不一致

**Claude Code 的 auto-memory 是全局的（写入 CLAUDE.md 文件），而 Jeeves 把它按 agent 隔离了。** 这不是技术问题，而是哲学分歧：Claude Code 认为"关于项目的知识应该全局共享"，Jeeves 认为"不同 agent 应该记住不同的东西"。

这两种哲学都有道理，但 Claude Code 的选择反映了**更务实的工程设计**——全局记忆更简单、更透明、不会碎片化。Agent 级的记忆差异通过 CLAUDE.md 的层级（Project vs User vs Managed）来实现，而非通过 agent_id 过滤。

### 建议

1. **先实现两层**：`target="user"`（全局 profile）+ `target="session"`（会话隔离）。生产环境运行一段时间后，**如果确实需要 agent 级记忆**，再加第三层。
2. **如果一定要三层**：验证智能体的 agent_id 不要用 `__verification` 后缀 hack。要么让验证智能体成为独立 AgentDefinition（有自己的 id），要么 memory 表加 `memory_scope` 字段（user/agent/session/verification）。
3. **考虑 Claude Code 模式**：用 Markdown 文件（如 `skills/agent_name/AGENT_MEMORY.md`）存储 agent 跨会话记忆，而不是数据库行。更透明、可手动编辑、不会被隐藏。

---

## 3. 验证智能体作为每智能体属性（非全局）— ✅ 正确，但 API 设计有遗留问题

### Jeeves 设计

`verification_enabled` 是 `agent_defs` 表的每行属性。每个智能体独立决定是否开启验证。全局配置已从 v1 废弃。

### 对比项目做法

| 项目 | 验证/检查机制 | 是否按 Agent |
|------|-------------|-------------|
| **OpenCode** | ❌ 无内置验证智能体 | N/A |
| **Claude Code** | Auto-mode Classifier（独立 Sonnet 实例评估工具安全性） | 全局（auto 模式） |
| **Hermes Agent** | ❌ 无内置验证智能体。Secret Redaction + Command Approval 是全局的 | N/A |
| **Goose** | ToolInspectionManager（SecurityInspector + EgressInspector + AdversaryInspector + PermissionInspector + RepetitionInspector） | 全局 |

### 一致性判断

**Jeeves 设计是正确的**。虽然四个参考项目都没有"验证智能体"概念，但 Claude Code 的 YoloClassifier 和 Goose 的 AdversaryInspector 暗示了一个关键事实：**安全检查器应该与被检查的对象绑定**。

Jeeves 的设计比 "全局一个验证器检查所有 agent" 更合理，因为：
1. 不同 agent 有不同的错误模式（审查员漏测试 vs 研究员编造引用）
2. 验证规则需要针对 agent 定制（检查"文件是否真的写入"只适用于有 write 权限的 agent）
3. 用户对"代码审查员"和"研究员"的验证期望不同

**Claude Code 的 Auto-mode Classifier 虽然是全局的，但它面对的是"单一 Claude 实例"——Claude Code 的用户不会同时使用多个 agent。Jeeves 是多 agent 系统，全局验证器反而不合适。**

### 严重问题：API 设计遗留不一致

**`docs/api/endpoints-agents.md` 中存在未删除的 v1 API**：

```
GET  /api/verification   ← 获取全局验证配置（v1 遗留！）
PUT  /api/verification   ← 更新全局验证配置（v1 遗留！）
```

这两个端点与 v2 的设计（`verification_enabled` 是每智能体属性）**直接冲突**。如果 `/api/verification` 端点上线，会出现：
- 用户通过 API 设置"全局验证开"
- 但只有勾选了 `verification_enabled` 的智能体才真正启用
- 两种配置来源造成混乱

### 建议

1. **删除 `/api/verification` 和 `PUT /api/verification` 端点**。验证配置完全通过 `PATCH /api/agents/{id}` 的 `verification_enabled` 和 `strict_mode` 字段操作。**这是必须立即修复的不一致**。
2. 如果需要"查看所有智能体的验证状态"视图，应通过 `GET /api/agents?verification_enabled=true` 过滤实现，而非独立的 verification 端点。

---

## 4. 单智能体和群组共用同一个 Chat 接口 — ✅ 合理但缺少动态编排

### Jeeves 设计

```
POST /api/chat
  agent_id: "adf_xxx"        ← 单智能体模式
  agent_group_id: "agp_xxx"  ← 群组模式
  (互斥)
```

### 对比项目做法

| 项目 | 多 Agent 调用方式 |
|------|------------------|
| **OpenCode** | `@agent_name` 在对话中提及 + Task 工具自主调用。无预定义群组概念 |
| **Claude Code** | `Agent(type="Explore", prompt="...")` 工具调用。无预定义群组概念 |
| **Hermes Agent** | `delegate_task` 工具，`max_spawn_depth=1`。无群组概念 |
| **Goose** | ❌ 无多 Agent 概念 |

### 一致性判断

**API 设计合理**。共用 endpoint + 互斥参数是简洁的 REST 设计。但 Jeeves 与主流项目在**多 Agent 编排模型**上存在根本分歧：

| 维度 | Jeeves | OpenCode / Claude Code |
|------|--------|----------------------|
| 编排模型 | **预定义群组**（MOA / Workflow） | **动态编排**（Agent 自主决定何时调用哪个子 Agent） |
| 群组组成 | 用户在管理页预设成员和角色 | 无群组概念，Agent 按需 @mention 或 Task 调用 |
| 执行模式 | 固定的 MOA（并行+汇总）或 Workflow（顺序） | Agent 自主决定并行/串行，无固定模式 |
| 灵活性 | 低（两种模式） | 高（Agent 根据任务动态调整） |

### 风险分析

预定义群组模式存在三个问题：

1. **不够灵活**：MOA 和 Workflow 两种模式无法覆盖所有多 Agent 场景。例如：
   - 树形编排：Agent A 调用 B 和 C，B 又调用 D
   - 条件分支：如果代码审查通过则继续，否则自动修复
   - 反馈循环：审查员发现问题 → 修复员修改 → 审查员再次检查
   
   这在 Claude Code/OpenCode 中由 Agent 自主决策，在 Jeeves 中需要新的群组类型。

2. **用户认知负担**：用户需要预先想好"我要用哪个群组"，而不是直接描述任务。OpenCode 的用户只说 `@general review this code`，系统自动处理。

3. **未利用 LLM 的编排能力**：现代 LLM（尤其 Claude Sonnet 4）已经能很好地自主决定何时调用子 Agent。预定义群组相当于把编排权从 LLM 手里拿回来交给了用户——这是一种倒退。

### 建议

**分层实现**：

```
Phase 1 (当前设计): 预定义群组（MOA + Workflow）
  → 用户明确知道要并行审查，选"代码审查组"

Phase 2 (建议添加): Agent 动态编排
  → 用户对"默认助手"说 "审查安全和性能"
  → Agent 自主调 spawn_task(agent="安全检查员") + spawn_task(agent="性能分析员")
  → 并行执行 + 汇总

Phase 3 (终极): 混合模式
  → 群组作为快捷方式（保存常用组合）
  → Agent 在群组执行中也可以动态调额外的子 Agent
```

Claude Code 的子 Agent 调用语法 `Agent(type="Explore", prompt="...")` 非常优雅——Agent 不需要知道"群组"概念，只需要知道"有哪些 Agent 可用以及它们的 description"。**建议 Jeeves 的默认智能体自动获得调用其他智能体的能力**（受 `permission_subagent` 控制）。

---

## 5. 智能体自管理 Skills — ⚠️ 缺乏先例，高风险

### Jeeves 设计

```
skills/
├── code-review/              ← 系统 skill（全局共享，Agent 不可修改）
├── <agent_name>/             ← Agent 私有 skill 目录
│   ├── SKILL.md              ← Agent 通过 skill_manage 创建/修改
│   └── verification/         ← 验证智能体的 skill 目录
│       └── SKILL.md          ← 验证智能体自我进化的规则
```

Agent 调用 `skill_manage(action="create", name="x")` 自动路由到 `skills/<agent_name>/x/SKILL.md`。

### 对比项目做法

| 项目 | Skills 管理机制 | Agent 是否可自管理 |
|------|----------------|-------------------|
| **OpenCode** | Agent 的 `prompt` 字段指向 Markdown 文件。无 self-modification | ❌ |
| **Claude Code** | 子 Agent 的 `skills` 字段预加载 skills 到 context。无 self-modification | ❌ |
| **Hermes Agent** | `skill_manage` 工具，Agent 可在 `$HERMES_HOME/skills/` 下创建/修改 skills。Curator 系统管理闲置。**Profile 级别，非 per-agent** | ✅ Agent 可创建 skills，但是全局的 |
| **Goose** | SkillOperation 加载预定义 recipes。无 self-modification | ❌ |

### 一致性判断

**Jeeves 的 agent-scoped skills 是 Hermes Agent 的全局 skills 的变体**——有先例（Hermes Agent），但是否应该按 agent 隔离**无先例**。

Hermes Agent 允许 Agent 通过 `skill_manage` 创建 skills，但它是 profile 级别的——所有 skills 在一个目录下，所有对话共享。Jeeves 把它按 agent 隔离了。

### 风险评估

| 风险 | 严重度 | 说明 |
|------|--------|------|
| **Skills 污染** | 🔴 高 | Agent 创建的 skill 可能含有错误指令、prompt injection、或过时信息。一旦写入文件系统，后续会话都会加载 |
| **自进化失控** | 🔴 高 | 验证智能体发现一个 false positive 模式，创建了严格的验证规则 → 后续所有类似操作都被误判 |
| **中文目录名** | 🟡 中 | `skills/代码审查员/` 作为文件系统路径名——跨平台兼容性差、URL 编码问题、windows 路径长度限制 |
| **Skill 爆炸** | 🟡 中 | Agent 每次对话都创建新 skill → 目录膨胀 → 加载慢 → context 开销大 |
| **缺乏审查** | 🔴 高 | 没有任何 human-in-the-loop。Agent 静默创建/修改 skills，用户完全不知道 |

### Hermes Agent 的解决之道（Jeeves 缺失的）

Hermes Agent 有 **Curator 系统**来管理 Agent 创建的 skills：
- 追踪使用频率
- 标记闲置 skills 为 stale
- Archive（不 delete，可恢复）
- **Pinned skills** 豁免所有自动转换
- Consolidation pass（默认关闭）
- `[SKILL_PRUNED]` 检测机制——skills 被压缩后强制重新加载

Jeeves 的设计中 **完全没有这些安全机制**。一个 Agent 创建了错误的 skill 后，会永久影响后续所有对话。

### 建议

1. **Phase 1（安全默认）**：Agent 只能通过 `skill_manage` **读取** skills，不能创建/修改/删除。Agent 可以调 `skill_view()` 加载 skill，但 `create/patch/delete` 返回错误。
2. **Phase 2（用户主导）**：Agent 可以**提议**创建 skill（生成 SKILL.md 内容），但需要用户在 UI 中**审批**后才写入文件系统。类似 Claude Code 的 `permission.ask` 模式。
3. **Phase 3（有条件自治）**：引入 Hermes 式的 Curator 系统——pinned skills 保护、staleness 追踪、archive 机制——之后才开放 Agent 自管理。
4. **目录命名**：使用 agent_id（如 `skills/adf_7bK2mQ9xR4Lp/`）而非 agent_name（`skills/代码审查员/`）。

---

## 综合评估矩阵

| 设计决策 | OpenCode | Claude Code | Hermes Agent | Goose | 一致性 | 风险 |
|----------|----------|-------------|-------------|-------|--------|------|
| 1. AgentDefinition 独立实体 | ✅ 一致 | ✅ 一致 | N/A | N/A | 🟢 高 | 🟢 低 |
| 2. 每 Agent 独立 memory | ❌ 无此设计 | ❌ 无此设计 | ❌ 全局 | ❌ 无 | 🔴 无先例 | 🟡 中 |
| 3. 验证=每 Agent 属性 | N/A | N/A (全局) | N/A | N/A (全局) | 🟢 设计正确 | 🟢 低 |
| 4. 共用 Chat + 预定义群组 | 不同模式 | 不同模式 | N/A | N/A | 🟡 部分一致 | 🟡 中 |
| 5. Agent 自管理 skills | ❌ | ❌ | ✅ (全局) | ❌ | 🟡 有相关先例 | 🔴 高 |

---

## 优先级修正建议

原 Jeeves 实施顺序：

| 原阶段 | 内容 | 修正建议 |
|--------|------|---------|
| 1 | AgentDefinition CRUD + 权限过滤 | ✅ **保持**。加 `hidden`、`steps`/`max_turns` 字段 |
| 2 | 对话页切换 + 记忆隔离 | ⚠️ **简化**。先做两层记忆（user + session），agent 级记忆推迟 |
| 3 | Agent 自管理 skills | 🔴 **推迟**。改为 Agent 只读 skills + 用户审批创建 |
| 4 | 验证增强 | ⚠️ **拆分**。先做 per-agent 开关（正确），自我进化推迟 |
| 5 | 群组/工作流 | ⚠️ **补充**。加 Agent 动态编排（`spawn_task` 工具） |

---

## 立即需要修复的问题

1. 🔴 **删除 `/api/verification` v1 遗留端点**（`docs/api/endpoints-agents.md` 第 157-188 行）——与 v2 per-agent 设计冲突
2. 🔴 **Agent self-skill 管理不应在 Phase 1 开放**——没有 curator、没有审批、没有 pinned，风险极高
3. 🟡 **验证智能体的 `__verification` ID hack**——用独立字段或独立 AgentDefinition
4. 🟡 **中文目录名作为文件系统路径**——`skills/代码审查员/` → `skills/{agent_id}/`
5. 🟢 **`agent_defs` 表缺 `hidden` 和 `max_turns`/`steps` 字段**——所有主流项目都有的安全机制
6. 🟢 **预定义群组模式不够灵活**——建议规划 Phase 2 Agent 动态编排

---

## 附录：各项目 Agent 定义对比

### OpenCode Agent 定义（JSON）

```json
{
  "agent": {
    "code-reviewer": {
      "description": "Reviews code for best practices and potential issues",
      "mode": "subagent",
      "model": "anthropic/claude-sonnet-4-20250514",
      "prompt": "{file:./prompts/review.txt}",
      "permission": { "edit": "deny", "bash": "deny" }
    }
  }
}
```

### Claude Code 子 Agent 定义（Markdown）

```markdown
---
name: code-reviewer
description: Reviews code for quality and best practices. Use proactively.
tools: Read, Glob, Grep
model: sonnet
permissionMode: acceptEdits
maxTurns: 10
skills: [typescript-best-practices]
---
You are a code reviewer. When invoked, analyze the code...
```

### Jeeves AgentDefinition（数据库记录）

```json
{
  "id": "adf_7bK2mQ9xR4Lp",
  "name": "代码审查员",
  "description": "审查代码质量、安全性和架构",
  "system_prompt": "你是资深代码审查员...",
  "model_id": "anthropic/claude-sonnet-4",
  "skill_names": ["code-review"],
  "mcp_servers": [],
  "permission_read": true,
  "permission_write": false,
  "permission_shell": false,
  "permission_network": false,
  "permission_subagent": false,
  "verification_enabled": false,
  "strict_mode": false
}
```
