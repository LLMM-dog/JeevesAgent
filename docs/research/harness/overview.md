# 驾驭工程对比总览（深度版）

> 基于源码分析的跨项目对比

## 驾驭工程核心实现对比

### 1. 提示词设计策略

| 项目 | System Prompt 设计 | 硬编码注入方式 | 动态性 |
|------|-------------------|---------------|--------|
| **Pi** | 字符串模板：`You are an expert coding assistant inside pi` | guidelines 按工具可用性动态构建；context files 用 `<project-context-file>` XML 包裹 | 中等 |
| **OpenCode** | Markdown frontmatter 驱动：每个 agent 一个 .md 文件 | tools/permission 在 frontmatter 声明，body 是 system prompt | 高（换文件 = 换 Agent） |
| **Claude Code** | CLAUDE.md 每 turn 重读 + hooks 注入 | deny-first rules + auto-mode classifier 评估 | 高（hooks 动态注入） |
| **Aider** | 固定模板 + system_reminder 每轮注入 | `ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!` 硬约束 | 低（模板固定） |
| **Goose** | PromptManager 分层管理：base + extension + project | MCP extensions 动态注入提示词 | 高 |
| **Hermes** | 分层构建：SOUL.md → project context → memory → skills | 每 turn 注入 memory；skills 按需 skill_view() 加载 | 高（渐进式） |

### 2. 硬编码约束对比

| 约束类型 | Pi | OpenCode | Claude Code | Aider | Goose | Hermes |
|----------|----|-----------|-------------|-------|-------|--------|
| **Token 截断保护** | ✅ stopReason=="length" → 全拒 | ❓ | ✅ 98% 触发压缩 | ❌ | ✅ MAX_TURNS | ✅ _Accum.truncated + _act 整批作废 |
| **角色交替** | ✅ 硬抛异常 | ✅ MessageV2 校验 | ✅ 内置 | ✅ 系统层 | ✅ | ✅ compact 切点保护 |
| **空回复保护** | ❓ | ✅ | ❓ | ❓ | ✅ MAX_EMPTY_TURN_RETRIES=3 | ✅ is_unusable + _reason_with_retry 自动重试 |
| **无限循环保护** | ❓ | ✅ maxSteps=50 | ✅ compaction | ❓ | ✅ MAX_TURNS=1000 + STOP_HOOK_BLOCK_CAP=8 | ✅ max_turns=90 |
| **工具执行前拦截** | ✅ beforeToolCall(block/terminate) | ✅ tool.execute.before | ✅ PreToolUse hook | ❌ | ✅ ToolApprovalOperation | ✅ BEFORE_TOOL + AFTER_TOOL hooks |
| **安全扫描** | ❌ | ❓ | ❓ | ❌ | ✅ SecurityInspector 三层 | ✅ threat-pattern scanner |
| **死循环 hook 保护** | ❌ | ❌ | ❌ | ❌ | ✅ STOP_HOOK_BLOCK_CAP=8 | ❌ |
| **Compaction 切点保护** | ✅ 不在 toolResult 处切 | ✅ 结构化压缩 | ✅ | N/A(RepoMap) | ✅ StateMachine 编排 | ✅ 不拆 tool_calls |

### 3. 记忆/状态管理

| 项目 | 存储后端 | 持久化策略 | 注入时机 |
|------|----------|-----------|----------|
| **Pi** | Entries (session log) | JSONL 文件 | 通过 session load |
| **OpenCode** | SQLite (per-project) | Drizzle ORM | Database.effect 保证一致 |
| **Claude Code** | 本地文件 | CLAUDE.md 每 turn 重读 | 每 turn 注入 |
| **Aider** | Git commits | `.aider.chat.history.md` | 通过 chat files |
| **Goose** | SessionManager | conversation 持久化 | session resume |
| **Hermes** | SQLite (state.db) + FTS5 | user profile + agent notes 双存储 | 每 turn 自动注入 |

### 4. 可扩展性机制

| 项目 | Skills | Plugins | Hooks | MCP | 自定义工具 |
|------|--------|---------|-------|-----|-----------|
| **Pi** | ✅ (SKILL.md, 渐进式) | ✅ Extensions | ✅ 6 个钩子点 | ❌ | ✅ |
| **OpenCode** | ✅ (skill tool) | ✅ 20+ 钩子 | ✅ 流水线模式 | ✅ | ✅ .opencode/tools/ |
| **Claude Code** | ✅ (.claude/skills/) | ✅ Hooks 系统 | ✅ 全生命周期 | ✅ 原生 | ✅ MCP tools |
| **Aider** | ❌ | ❌ | ❌ | ❌ | ❌(fork repo) |
| **Goose** | ❌ | ✅ ExtensionManager | ✅ HookManager | ✅ 原生唯一扩展 | ✅ MCP tools |
| **Hermes** | ✅ (渐进式+Curator) | ✅ plugins/ | ❌ | ✅ MCP client | ✅ tools/registry |

### 5. Compaction 算法

| 项目 | 触发条件 | 切点策略 | 摘要生成 |
|------|----------|----------|----------|
| **Pi** | `contextTokens > window - 16384` | 反向累积 token → 找最近合法切点 | 专用 summarization prompt + file ops 追踪 |
| **OpenCode** | 3 阶段：Selection→Generation→Replacement | N 条消息为候选 | Subagent 生成结构化摘要 |
| **Claude Code** | token 约 98% 时 | 5 层策略（含 full reset 备选） | 总结 + CLAUDE.md 锚点保护 |
| **Hermes** | compaction.threshold (默认 0.50) | 不拆 tool_calls+tool 结果对 | Compact 命令 4 种模式 |

## 从源码中提炼的 10 个硬约束最佳实践

1. **Pi**: Token 截断时绝不执行 tool call——全部标记为失败，让模型重试
2. **OpenCode**: Plan Agent 的只读保护在代码层硬编码，配置覆盖无效
3. **Claude Code**: Auto Mode 分类器用独立模型实例，且不看到 Agent prose 输出
4. **Aider**: `ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!` 每轮注入 system_reminder
5. **Goose**: Stop hook 连续阻止 8 次后强制覆盖，防止 hook 死循环
6. **Pi**: Roles 交替硬抛异常，绝不从 assistant 角色继续
7. **Goose**: 空回复最多重试 3 次后报告错误，不静默失败
8. **Hermes**: 远程 terminal 时抑制 host 信息，防止 agent 尝试触碰无法访问的主机
9. **Hermes**: Secret redaction 在 import 时快照，LLM 不可在运行时关闭
10. **Pi**: Skills 只注入 name+description+filePath 到 system prompt，不注入内容（渐进式）
