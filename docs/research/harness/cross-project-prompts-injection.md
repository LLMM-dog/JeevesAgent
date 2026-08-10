# 驾驭工程全景对比：系统提示词、注入顺序与运行时干预

> 调研时间：2026-08-10
> 覆盖项目：OpenCode、Claude Code、Aider、Pi、Goose、Codex CLI、Cline/Roo Code
> 补充现有分析：[opencode.md](./opencode.md)、[claude-code.md](./claude-code.md)、[aider.md](./aider.md) 等

---

## 1. 系统提示词组装顺序（跨项目对比）

每个项目发给 LLM 的不只是用户消息，而是一个**分层堆叠**的系统提示体系。顺序决定优先级：越靠近模型推理开始的位置，权重越高。

### 1.1 OpenCode

```
位置 1: Agent system prompt（Markdown body）
        ↓ 来源：.opencode/agent/*.md 的 frontmatter body
位置 2: System Context Baseline（工作区信息、git 状态）
        ↓ 来源：SessionContextEpoch.prepare() 动态生成
位置 3: SystemPart（tools+warnings）
        ↓ 来源：build_flags（Git dirty/Docker/Ignored/Gitignore 警告等）
位置 4: Messages（历史消息+工具结果+压缩摘要）
        ↓ 来源：SessionHistory.entriesForRunner()
位置 5: MAX_STEPS_PROMPT（步数超限时追加）
        ↓ 来源：max-steps.ts，仅 maxSteps 达到时
```

**关键设计**：系统 prompt 分 **Agent System**（用户定义）和 **System Context**（环境注入）。两者分开发送——Agent 定义在 model 参数里，Context 在 system 消息中。

### 1.2 Claude Code

```
位置 1: Base System Prompt（固定模板："You are Claude Code..."）
        ↓ 来源：内置固定文本
位置 2: Environment Info（Shell 类型、工作目录、平台、日期）
        ↓ 来源：运行时动态注入
位置 3: CLAUDE.md hierarchy（Managed → User → Project → Local）
        ↓ 来源：4 级文件系统层次，每 turn 重读
位置 4: Path-scoped rules（.claude/rules/*.md）
        ↓ 来源：@ 前缀匹配路径作用域
位置 5: Auto-memory（会话级自动记忆）
        ↓ 来源：Session Memory 后台笔记进程
位置 6: Tool metadata（工具名称+描述+searchHint）
        ↓ 来源：assembleToolPool 过滤后
位置 7: Conversation History（经过 compaction 处理）
        ↓ 来源：JSONL transcript
位置 8: Hook injection（UserPromptSubmit 事件）
        ↓ 来源：hooks 的 additionalContext
```

**关键设计**：`CLAUDE.md` 是 **user context**（概率性合规），不是 system prompt（确定性执行）。Permission rules 提供确定性执行层。

### 1.3 Aider

```
位置 1: main_system（一次性固定模板）
        ↓ 包含：{final_reminders} 占位符、{shell_cmd_prompt} 动态注入
位置 2: example_messages（few-shot SEARCH/REPLACE 示例）
        ↓ 来源：editblock_prompts.py，可按 examples_as_sys_msg 折叠进 system
位置 3: system_reminder（每轮重复注入）
        ↓ 来源：editblock_coder.py 的 format_chat_chunk()
        ↓ 内容：硬约束规则、格式检查清单、禁止操作列表
位置 4: repo_content（RepoMap 代码结构摘要）
        ↓ 来源：repomap.py，随对话请求动态更新
```

**关键设计**：`system_reminder` 是 Aider 最独特的驾驭手段——它不是 system prompt 的一部分，而是**每轮作为独立的 system 消息注入**。这意味着：即使模型在对话中途"忘记"约束，下一轮会被重新提醒。

### 1.4 Pi

```
位置 1: Agent Identity（"You are an expert coding assistant inside pi..."）
        ↓ 来源：system-prompt.ts 固定模板
位置 2: Available Tools List（一行一个工具名+描述）
        ↓ 来源：selectedTools 动态生成
位置 3: Guidelines（根据可用工具动态构建的指导原则）
        ↓ 来源：条件判断 hasBash/hasGrep/hasFind → 决定是否注入对应 guideline
位置 4: Context Files（<project-context-file> XML 包裹的项目文件）
        ↓ 来源：IDE 上下文感知下的打开文件
位置 5: Skills L1（name + description + filePath，不含正文）
        ↓ 来源：skills 索引扫描，渐进式披露
```

**关键设计**：Guidelines 是**条件编译式**的——根据当前注册了哪些工具，决定注入哪些指导原则。比如有 Bash 但没 Grep 时，注入"用 bash 做文件搜索"的指导。

### 1.5 Codex CLI

```
位置 1: System Prompt Template（core/prompts/*.md 文件）
        ↓ 包含：角色定义 + {shell_type} + {working_dir} + {plan_status}
位置 2: Plan State（当前计划步骤+状态）
        ↓ 来源：update_plan 工具持久化到 plan.md，每轮注入
位置 3: Environment（OS、shell、workspace、git 状态）
        ↓ 来源：运行时环境变量
位置 4: Goals（高层目标清单）
        ↓ 来源：用户定义 goals/*.md，每轮注入
位置 5: Tool Definitions（JSON Schema）
        ↓ 来源：注册的工具定义
位置 6: Context Files（IDE 打开的文件列表）
        ↓ 来源：editor context
```

**关键设计**：Plan State 是**持久化且每轮注入**的——不是存在上下文历史中，而是存在 `plan.md` 文件中，每轮重新读取。好处是：即使上下文被压缩，计划不会丢失。

### 1.6 提示词层次总结

```
层次           OpenCode    Claude Code   Aider      Pi        Codex CLI
────────────────────────────────────────────────────────────────────────
角色/身份      Agent md    Base Prompt   固定模板   固定模板   prompts/*.md
项目规则       —           CLAUDE.md     —          —         Goals/*.md
环境信息       Epoch       环境变量       —          工具列表   环境变量
技能/知识      Agent tools Skills+Rules  —          Skills L1  Tool defs
行为约束       Flags       权限规则      reminder   Guidelines Plan state
历史对话       Messages    Transcript    历史消息   历史消息   历史+Plan
运行时注入     MAX_STEPS    Hook inject   reminder   无         Plan state
```

---

## 2. 运行时干预机制（跨项目对比）

运行时干预是驾驭工程的核心——在 Agent 执行过程中**不通过 LLM 推理**，而是通过工程系统主动介入。

### 2.1 重复/打转检测

| 项目 | 机制 | 检测粒度 | 干预方式 |
|------|------|----------|----------|
| **Jeeves** | `_check_repeating()` | 跨轮工具 (name, args) hash | 注入提示词："你已连续调用相同工具 N 次，请确认方向是否正确" |
| **OpenCode** | `maxSteps=50` | 步数 | 强制纯文本回复，禁用所有工具 |
| **Claude Code** | 无显式打转检测 | — | 靠 maxTurns 硬限制 |
| **Aider** | 无打转检测 | — | 无机制 |
| **Goose** | `STOP_HOOK_BLOCK_CAP=8` | Hook 连续阻止次数 | 第 8 次强制覆盖 hook 决策 |
| **Claude Code** (auto) | ML 分类器 | 工具调用参数安全评估 | 独立 Sonnet 实例判定 allow/block/escalate |

**Jeeves 值得扩展的方向**：当前只做工具级 `(name, args)` hash。可以参考 Codex CLI 的 PEV 模式，增加"文件修改次数"计数——如果一个文件在一次对话中被修改超过 N 次，注入提示词让模型重新思考大方向是否出现问题，而不是继续微调。

### 2.2 上下文超额保护

| 项目 | 触发条件 | 干预方式 |
|------|----------|----------|
| **Jeeves** | `compact_trigger_ratio=0.75` | 压缩摘要 + 保留 tail turns |
| **Jeeves** | `finish_reason=="length"` | 整批 tool_calls 作废，让模型重试 |
| **Jeeves** | 空响应/仅思维链无产出 | `is_unusable` 判定 → 重试 |
| **OpenCode** | `estimate() > context - max(output, buffer)` | 3 阶段 compaction |
| **Claude Code** | ~167K for 200K window | 5 层 cascade: Budget→Snip→Microcompact→Collapse→AutoCompact |
| **Pi** | `contextTokens > window - 16384` | 反向累积 token → 找合法切点 → 摘要 |
| **Goose** | `MAX_EMPTY_TURN_RETRIES=3` | 空回复 3 次后报告错误 |

**Claude Code 的 5 层 cascade 是最精致的**：每层尽力避免调用下一层，从零成本的 Budget Reduction → 接近于零成本的 Cached Microcompact → 昂贵的 Full Compact。Jeeves 当前只有 1 层压缩。

### 2.3 Token 截断保护（各项目对比）

所有成熟项目都有截断保护，但细节不同：

| 项目 | 截断策略 | 对大工具输出的处理 |
|------|----------|-------------------|
| **Jeeves** | `_Accum.truncated` → 整批 tool_calls 作废 | `max_output_lines=2000, max_output_bytes=51200` |
| **OpenCode** | ToolOutputStore.bound() | 超限写磁盘 → context 只留 ~2KB 预览+路径 |
| **Claude Code** | 50K chars > 写磁盘 | Read 工具 threshold=Infinity（自身豁免） |
| **Pi** | `stopReason=="length"` → 全拒，tool call 标记失败 | — |
| **Goose** | — | 有 MAX_TURNS=1000 |

### 2.4 权限裁决时机（运行时门控）

```
模型提议 tool call
    ↓
┌───────────────────────────────────────────┐
│ Layer 1: Tool Pre-filtering              │  看不到的工具不会提议
│         （被 deny 的工具从 model 视野移除）│
├───────────────────────────────────────────┤
│ Layer 2: Permission Mode                 │  default/acceptEdits/auto/dontAsk
├───────────────────────────────────────────┤
│ Layer 3: Deny-first Rules                │  deny > ask > allow，严格优先
├───────────────────────────────────────────┤
│ Layer 4: Auto-mode Classifier            │  独立 ML 模型评估（Claude Code 独有）
├───────────────────────────────────────────┤
│ Layer 5: Hook Interception               │  PreToolUse hooks 可阻断/修改
├───────────────────────────────────────────┤
│ Layer 6: Shell Sandboxing                │  命令级沙盒
└───────────────────────────────────────────┘
         ↓
    执行或拒绝
```

**Jeeves 当前**：只有 `_filter_tools_by_permissions()`（等同于 Layer 1）+ 审批模式（等同于 Layer 2 的 manual/auto）。缺少 Deny-first rules、ML 分类器、Hook 拦截。

### 2.5 文件修改次数追踪（Jeeves 可新增的运行时干预）

当前没有任何项目显式追踪"单文件在一次对话中的修改次数"。这是一个**低成本的、在运行时不依赖 LLM 即可注入的干预**：

```python
# 伪代码：Jeeves 可在此处插入
_file_edit_counts: dict[str, int] = {}

async def _act(self, ai_msg: Msg) -> None:
    for tool_call in ai_msg.tool_calls:
        if tool_call.name in ("write_file", "edit_file"):
            path = tool_call.arguments.get("path", "")
            self._file_edit_counts[path] = self._file_edit_counts.get(path, 0) + 1
            if self._file_edit_counts[path] >= 5:
                # 不中断执行，但注入一条提示词
                self._system_reminder = (
                    f"注意：文件 {path} 在本轮对话中已被修改 {self._file_edit_counts[path]} 次。"
                    "请确认当前方向是否正确——是否应该在更高层面重新审视架构，"
                    "而不是继续对细节进行微调？"
                )
```

**Aider 的类似设计**：Aider 没有计数机制，但它的 `system_reminder` 每轮注入 `"ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!"`——这是一种**带宽式的持续干预**，而非**阈值触发的条件干预**。

---

## 3. Skills/MCP 的上下文注入方式

### 3.1 渐进披露 vs 全量注入

| 项目 | Skills 注入方式 | MCP 工具注册方式 |
|------|----------------|-----------------|
| **Jeeves** | L1 常驻 system prompt → L2 `load_skill()` → L3 `load_skill_file()` | 连接即注册，全量 tool schema 进上下文 |
| **OpenCode** | skill tool -> 按需加载 | 同上，全量注册 |
| **Claude Code** | skills/ 目录，Agent 工具注入 | ToolSearch: 名称+searchHint → 按需加载完整 schema（85% token 节省） |
| **Pi** | L1 (name+description+filePath) 常驻 → 按需加载 L2 | — |
| **Goose** | MCP 原生，extension 注提示词 | 全量注册 |

**Claude Code 的 ToolSearch 是最创新的**：当 MCP 工具数量多到描述超过 context 10%，自动启用以 searchHint 替代完整 schema。Token 节省 85%。

### 3.2 技能正文的注入位置（安全约束）

所有项目一致：**技能正文不进 system 位**，而是以工具返回值形态进上下文。这样即使技能中包含"忽略之前的指令"等注入攻击，也只是数据层的内容，不会升格为系统指令。

---

## 4. Compaction 的干预策略对比

### 4.1 触发条件对比

| 项目 | 触发公式 | 预警机制 |
|------|----------|----------|
| **Jeeves** | `used >= window * 0.75` | `warn_turn_ratio=0.8` 注入催促 |
| **OpenCode** | `estimate() > context - max(output, buffer)` | buffer 默认 20K |
| **Claude Code** | `context - max_output - 13K buffer` | 5 层渐进 |
| **Pi** | `contextTokens > window - 16384` | 固定 buffer 16K |

### 4.2 切点保护

| 项目 | 切点保护策略 |
|------|-------------|
| **Jeeves** | 不拆分 `tool_calls` 与其 `tool` 结果对 |
| **OpenCode** | 结构化压缩，N 条消息为候选 |
| **Claude Code** | 5 层策略含 full reset 备选 |
| **Pi** | 不在 toolResult 处切 |

---

## 5. 对 Jeeves 的优先级建议

### P0 — 运行时干预（低成本高收益）

1. **文件修改计数 + 条件注入**：如 §2.5 所述，在 `_act()` 中追踪单文件修改次数，超过阈值注入方向性提示词
2. **Step 级进度感知**：当前 `warn_turn_ratio` 只按 turn 计数，可以增加"当前 todo 步骤已完成与总数的比例"感知
3. **空回复保护**：Jeeves 已有 `is_unusable` + `_reason_with_retry`，但缺少类似 Goose 的 `MAX_EMPTY_TURN_RETRIES` 上限

### P1 — 渐进式压缩

4. **多级 compaction cascade**：参考 Claude Code 的 5 层模型。Jeeves 当前只有 1 层（`_maybe_compact`），可增加：
   - Level 1: 预算裁剪（零成本，裁剪超长单条消息）
   - Level 2: 旧结果清除（类似 Claude Code 的 Microcompact）
   - Level 3: 完整压缩（当前已有的 LLM 摘要）

### P2 — 安全增强

5. **Deny-first 规则引擎**：当前权限是 allow-based（Permission 字段），增加 deny 规则（deny > allow）
6. **Hook 拦截点扩展**：当前有 `BEFORE_TOOL/AFTER_TOOL/SHOULD_STOP`，可增加 `POST_COMPACT`、`PRE_LLM_CALL`

### P3 — 能力提升

7. **ToolSearch 延迟发现**：当注册工具数超过阈值，只发名称+searchHint，按需加载完整 schema
8. **Session Memory 后台笔记**：参考 Claude Code，平摊摘要成本到整个 session
