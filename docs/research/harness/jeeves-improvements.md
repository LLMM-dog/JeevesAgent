# Jeeves 驾驭工程改进建议

> 基于 7 个知名 Agent 项目的源码级调研
> 2026-08-08

## 当前状态评估

Jeeves 的驾驭工程基础上乘：纯 while loop、messages/journal 分离、落库先 DB 后内存、压缩三铁律（真实 usage 触发 + 不拆 tool 配对 + 保留 tail）、三道文件防线、Protocol 而非 ABC 的工具定义、content/display 分离。这些都是多数项目没做好的。

以下建议按**投入产出比**排序，标注参考项目和预期收益。

---

## P0 — 高收益、低成本（本周可做）

### 1. Token 截断保护 ✅ 已实现

**当前**: 已完整实现，无需改动。`_Accum.truncated` 检测 `finish_reason == "length"`，`_act` 整批作废不执行。测试覆盖：`test_loop_guards.py::TestTruncationGuard`。

### 2. 空回复保护 ✅ 已实现

**当前**: 已完整实现。`_Accum.is_unusable` 判据（无正文且无工具调用），区分思维链截断（附带"少想多说"提示）。`_reason_with_retry` 自动重试。测试覆盖：`test_loop_guards.py::TestEmptyResponse`。

### 3. 工具执行前拦截（beforeToolCall） ✅ 已实现

**当前**: 已实现。`ToolRegistry.hooks` 提供 `BEFORE_TOOL` + `AFTER_TOOL` 两个钩子点。测试覆盖：`tests/test_hooks.py`（18 条）。详见 `backend/app/modules/agent/hooks.py`。

---

## P1 — 中收益、中成本（本月可做）

### 4. Context File 威胁扫描

**当前**: 无。`SOUL.md`、`AGENTS.md` 等文件直接注入 system prompt，可被 prompt injection 利用。

**参考**: Hermes 的 `prompt_builder.py:_scan_context_content()` — 所有 context files 在注入前经过 `scan_for_threats(content, scope="context")`，匹配的内容替换为 `[BLOCKED: ...]`。

**建议**: 在 `system_prompt.py` 的 context file 加载后、注入前，加一个正则扫描：
```python
# 检测经典 prompt injection 模式
THREAT_PATTERNS = [
    r"(?i)ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|rules?)",
    r"(?i)you\s+are\s+now\s+(DAN|STAN|jailbroken)",
    # ... 更多模式
]
```

**改动量**: ~30 行 + 一个 pattern 列表。

### 5. 压缩结构化摘要

**当前**: 压缩摘要是自由文本，模型自己决定保留什么。

**参考**: OpenCode 的 compaction.ts — 使用固定模板：`## Objective / ## Current Work State / ## Relevant Files / ## Errors and Blockers / ## Pending Items / ## Next Move`。Goose 的 compaction 用结构化 JSON 输出（`compaction_output.rs`），字段明确：`objectives`, `current_state`, `file_changes`, `errors`。

**建议**: 把压缩 prompt 改为要求模型输出固定结构：
```
## 目标
[一句话]

## 当前进度
[已完成的关键步骤]

## 涉及文件
[文件路径和改动性质]

## 阻碍
[遇到的错误或需要确认的点]

## 下一步
[接下来该做什么]
```

**改动量**: ~20 行（改压缩 prompt 模板）。

### 6. Ephemeral Scaffolding 保护

**当前**: 无。压缩/重试产生的内部消息（如"你的回复为空，请重试"）会被持久化，恢复会话时污染 transcript。

**参考**: Hermes 的 `run_agent.py:_EPHEMERAL_SCAFFOLDING_FLAGS` — 内部恢复消息标记 `_empty_recovery_synthetic` 等 flag，持久化时跳过。

**建议**: 在 `Msg` 类里加一个 `ephemeral: bool = False` 字段。`_persist()` 检查此字段，为 True 时只入 `messages` 不入库。

**改动量**: ~15 行。

---

## P2 — 高收益、高成本（下个版本）

### 7. Agent 角色/模式系统

**当前**: 只有一个主 Agent。子 Agent 通过 `delegate_task` 生成，但缺少预设角色。

**参考**: OpenCode 的 build/plan/explore/scout 体系 — 每个角色有独立的 system prompt、工具权限和默认模型。Plan agent 的只读保护在代码层硬编码（无法被配置覆盖）。

**建议**: 定义 `AgentRole` 枚举：
```python
class AgentRole(Enum):
    BUILD = "build"       # 默认，全工具
    PLAN = "plan"         # 只读，拒绝文件编辑和 shell
    EXPLORE = "explore"   # 只读，快速代码导航
```

每个角色配置独立的 system prompt 前缀和工具白名单。Plan 角色在 `ToolRegistry.execute()` 层硬拒绝 `write_file`/`run_shell` 等写操作。

**改动量**: ~100 行。

### 8. Compaction 增量更新

**当前**: 每次压缩生成全新摘要，覆盖旧的。

**参考**: OpenCode 的 Anchored Summary 模式 — 保留上一轮的摘要作为"锚点"，新摘要增量追加。

**建议**: 压缩 prompt 中加入 `previous_summary`，要求模型在旧摘要基础上追加新信息：
```
## 之前的摘要
{previous_summary}

请在此基础上追加本轮的新信息，不要重复已有的内容。
```

**改动量**: ~30 行。

### 9. Session Fork

**当前**: 无。会话不能分支。

**参考**: OpenCode 的 `Session.fork(messageId)` — 复制到指定消息的历史，创建子会话（保留 parentID 血统）。用户说"如果这条路不通就回到这里"的场景。

**建议**: 复制 `messages` 表中指定 message_id 之前的记录到新 session，新 session 的 `parent_id` 指向原 session。前端显示 fork 关系。

**改动量**: ~80 行 + 前端。

---

## P3 — 长期方向

### 10. 权限分层体系

**当前**: 两档 — manual 和 auto。所有工具同一策略。

**参考**: Claude Code 的 7 层权限：tool pre-filtering → deny-first rules → permission modes → auto-mode classifier → shell sandboxing → non-restoration on resume → hook interception。OpenCode 的 `action × resource` glob 二维匹配 + last-match-wins。

**建议**: 从当前的两档逐步演进到多层：
1. **工具分类**: read/write/shell/network 四个类别
2. **每类可独立设置**: allow/ask/deny
3. **路径匹配**: glob 模式（`*.env` → deny，`workspace/**` → write allow）
4. **持久化批准**: 用户批准的路径在一定时间内不再询问

**改动量**: 大，建议拆成多个渐进 PR。

### 11. 工具延迟发现（MCP）

**当前**: MCP 工具在连接时全量加载到 context。

**参考**: Claude Code 的 ToolSearch — 启动时只加载工具名称，需要时搜索相关工具 schema。实测节省 85% context token（从 ~24,685 → ~3,792 tokens）。

**建议**: MCP Server 连接时只记录工具名和一句话描述，不加载完整 JSON Schema。Agent 使用工具时再动态获取 schema。

### 12. RepoMap 式智能 Context

**当前**: Context 只包含对话历史和 system prompt，不了解代码库结构。

**参考**: Aider 的 Tree-sitter + PageRank — 解析整个仓库构建符号图，只注入最相关的 Top-K 符号和签名（默认 1K tokens）。

**建议**: 可选特性。大项目使用 `search_files` 已经够用。只有在 Agent 频繁"猜不到文件在哪"时才值得投资。

---

## 快速对照表

| 改进项 | 改动量 | 收益 | 优先级 |
|--------|--------|------|--------|
| Token 截断保护 | ~15 行 | 防止执行不完整的 tool call | **P0** |
| beforeToolCall 拦截 | ~20 行 | 注入验证逻辑，不污染工具代码 | **P0** |
| 空回复保护 | ~25 行 | 防止静默失败 | **P0** |
| Context 威胁扫描 | ~30 行 | 防 prompt injection | **P1** |
| 压缩结构化摘要 | ~20 行 | 压缩保真度提升 | **P1** |
| Ephemeral Scaffolding | ~15 行 | 恢复时不污染 transcript | **P1** |
| Agent 角色系统 | ~100 行 | 安全性 + UX | **P2** |
| Compaction 增量更新 | ~30 行 | 长会话上下文质量 | **P2** |
| Session Fork | ~80 行 | 探索性工作流 | **P2** |
| 权限分层 | 大 | 安全性 | **P3** |
| MCP 延迟发现 | 中 | Context 优化 | **P3** |
| RepoMap | 大 | Context 优化 | **P3** |
