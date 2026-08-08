# AI Agent 结果评估工程：端到端验证、自评失真、代码质量与观测追踪

> **研究日期**：2026-08-08
> **覆盖项目**：OpenCode、Claude Code、Aider、Codex CLI、Goose
> **方法论**：深入源码阅读 + 官方博客 + 社区逆向分析，提取具体实现而非概念描述

---

## 目录

1. [端到端验证：Agent 如何证明自己的产出真的能用](#1-端到端验证agent-如何证明自己的产出真的能用)
   - 1.1 [Codex CLI：PEV 架构 + 完整观测栈验证](#11-codex-clipev-架构--完整观测栈验证)
   - 1.2 [Aider：自动 Lint + 自动测试 + 反馈循环](#12-aider自动-lint--自动测试--反馈循环)
   - 1.3 [Claude Code：Hook-driven 验证注入](#13-claude-codehook-driven-验证注入)
   - 1.4 [OpenCode：LSP 诊断 + 工具输出验证](#14-opencodelsp-诊断--工具输出验证)
   - 1.5 [Goose：RetryManager 的 success_checks 验证](#15-gooseretrymanager-的-success_checks-验证)
2. [自评失真：模型给自己的结果打高分的解决方案](#2-自评失真模型给自己的结果打高分的解决方案)
   - 2.1 [Claude Code：独立 Auto-mode Classifier（最强方案）](#21-claude-code独立-auto-mode-classifier最强方案)
   - 2.2 [Codex CLI：Ralph Wiggum Loop 多 Agent 交叉审查](#22-codex-cliralph-wiggum-loop-多-agent-交叉审查)
   - 2.3 [Goose：AdversaryInspector 的 Fail-Open 设计](#23-gooseadversaryinspector-的-fail-open-设计)
   - 2.4 [Aider：精确匹配强约束（宁可报错不猜测）](#24-aider精确匹配强约束宁可报错不猜测)
3. [代码质量评估：耦合性、架构、规范性](#3-代码质量评估耦合性架构规范性)
   - 3.1 [Codex CLI：自定义 Linter 强制层级架构依赖方向](#31-codex-cli自定义-linter-强制层级架构依赖方向)
   - 3.2 [Aider：每轮自动 Lint + Git Commit 可逆性保证](#32-aider每轮自动-lint--git-commit-可逆性保证)
   - 3.3 [Claude Code：LSP 工具提供语言服务器诊断](#33-claude-codelsp-工具提供语言服务器诊断)
   - 3.4 [OpenCode：LSP 集成在 Read 工具中](#34-opencodelsp-集成在-read-工具中)
4. [观测与追踪：Agent 行为如何被记录和审查](#4-观测与追踪agent-行为如何被记录和审查)
   - 4.1 [OpenCode：Event Sourcing + SQLite（最强审计方案）](#41-opencodeevent-sourcing--sqlite最强审计方案)
   - 4.2 [Claude Code：JSONL 转录 + Sidechain + Session Memory](#42-claude-codejsonl-转录--sidechain--session-memory)
   - 4.3 [Codex CLI：Chrome DevTools + 临时观测栈](#43-codex-clichrome-devtools--临时观测栈)
   - 4.4 [Goose：5 层 Inspector 链 + OpenTelemetry](#44-goose5-层-inspector-链--opentelemetry)
   - 4.5 [Aider：Git 归因 + Commit Hash 追踪](#45-aidergit-归因--commit-hash-追踪)
5. [跨项目对比矩阵](#5-跨项目对比矩阵)
6. [核心启示](#6-核心启示)

---

## 1. 端到端验证：Agent 如何证明自己的产出真的能用

### 1.1 Codex CLI：PEV 架构 + 完整观测栈验证

Codex CLI 的验证能力是所有项目中最系统化的。其 PEV 架构的 **Verify 阶段**包含多层验证：

#### 核心：PEV（Plan-Execute-Verify）三阶段分离

```
┌─────────┐     ┌──────────────┐     ┌──────────────┐
│  PLAN   │ ──▶ │   EXECUTE    │ ──▶ │   VERIFY     │ ──▶ 完成
│ 制定计划 │     │  执行工具调用  │     │  验证结果     │    │
└─────────┘     └──────────────┘     └──────────────┘    │
     ▲                                       │            │
     └──────── 验证失败，更新计划 ─────────────┘            │
```

**关键设计**（`codex-rs/core/prompt.md`）：

```markdown
When steps have been completed, use update_plan to mark each finished
step as completed and the next step you are working on as in_progress.
There should always be exactly one in_progress step until everything
is done.
```

每个步骤完成后自动进入验证循环：
1. 运行 `cargo test`、`npm test`、linter
2. 观察测试输出
3. 失败时自动修正
4. 循环直到所有检查点通过

#### Chrome DevTools Protocol 验证 UI 路径

OpenAI 博客原文（`harness-engineering`）：

> We made the app bootable per git worktree, so Codex could launch and drive one instance per change. We also wired the Chrome DevTools Protocol into the agent runtime and created skills for working with DOM snapshots, screenshots, and navigation. This enabled Codex to reproduce bugs, validate fixes, and reason about UI behavior directly.

完整流程：
1. Agent 选择目标 UI 路径
2. 触发前截取 DOM 快照
3. 触发 UI 路径
4. 触发后截取 DOM 快照
5. 观察运行时事件（CDP）
6. 对比前后状态 → 应用修复 → 重启应用 → 循环验证

#### 完整观测栈：LogQL + PromQL + TraceQL

```yaml
验证指令示例:
  "ensure service startup completes in under 800ms"
  "no span in these four critical user journeys exceeds two seconds"
```

架构：应用 → Vector（日志/指标/追踪收集器）→ Victoria Logs/Metrics/Traces → Agent 通过 LogQL/PromQL/TraceQL 查询 → 关联、推理 → 修复 → 重启 → 重新验证

#### Ralph Wiggum Loop（多 Agent 交叉验证）

```python
# 概念伪代码
while True:
    result = codex.implement(feature)
    reviews = []
    reviews.append(codex.review_own_changes(result))
    reviews.append(request_agent_review("architecture-agent", result))
    reviews.append(request_agent_review("security-agent", result))
    reviews.append(any_human_feedback())
    
    if all(review.is_satisfied for review in reviews):
        break
    
    codex.respond_to_feedback(reviews)
```

OpenAI 团队实际观察："We regularly see single Codex runs work on a single task for upwards of six hours (often while the humans are sleeping)."

---

### 1.2 Aider：自动 Lint + 自动测试 + 反馈循环

Aider 的验证是**每轮自动执行 + 用户确认 + 自动修复**的模式。

#### 配置方式

```python
# aider/coders/base_coder.py:105-107
class Coder:
    auto_lint = True
    auto_test = False  # 默认关闭，需用户显式配置
    test_cmd = None     # 如 "python -m pytest"
```

```bash
# 用户通过命令行配置
aider --lint-cmd "ruff check --fix" --test-cmd "python -m pytest -x"
```

#### 执行流程（base_coder.py:1590-1624）

```python
# aider/coders/base_coder.py:1599-1623
if edited and self.auto_lint:
    lint_errors = self.lint_edited(edited)         # 1. 自动 lint
    self.auto_commit(edited, context="Ran the linter")  # 2. lint 后提交
    self.lint_outcome = not lint_errors
    if lint_errors:
        ok = self.io.confirm_ask("Attempt to fix lint errors?")  # 3. 用户确认
        if ok:
            self.reflected_message = lint_errors   # 4. 将错误反馈给模型
            return  # 5. 进入下一轮修复循环

shared_output = self.run_shell_commands()           # 6. 运行 shell 命令

if edited and self.auto_test:
    test_errors = self.commands.cmd_test(self.test_cmd)  # 7. 自动测试
    self.test_outcome = not test_errors
    if test_errors:
        ok = self.io.confirm_ask("Attempt to fix test errors?")  # 8. 用户确认
        if ok:
            self.reflected_message = test_errors   # 9. 错误反馈给模型
            return  # 10. 进入下一轮修复循环
```

#### lint_edited 实现（base_coder.py:1681-1696）

```python
def lint_edited(self, fnames):
    res = ""
    for fname in fnames:
        if not fname:
            continue
        errors = self.linter.lint(self.abs_root_path(fname))
        if errors:
            res += "\n" + errors + "\n"
    if res:
        self.io.tool_warning(res)
    return res
```

#### cmd_test 实现（commands.py:993-1011）

```python
def cmd_test(self, args):
    """Run a shell command and add the output to the chat on non-zero exit code"""
    if not args and self.coder.test_cmd:
        args = self.coder.test_cmd
    if not args:
        return
    if not callable(args):
        return self.cmd_run(args, True)  # add_on_nonzero_exit=True
    # callable case: 用户传递了 lambda
    errors = args()
    if not errors:
        return
    self.io.tool_output(errors)
    return errors
```

**关键设计**：`cmd_run(args, add_on_nonzero_exit=True)`——只有测试失败（exit code ≠ 0）时才将输出添加到对话中。这让模型只看到"需要修复的东西"，而不是被成功的测试输出淹没。

#### 反馈循环机制

```
编辑文件 → 自动 lint → 有错误?
  ├── 是 → 用户确认 → 错误作为 new_message 反馈给模型 → 模型修复 → 重新 lint
  └── 否 → 自动测试 → 有错误?
        ├── 是 → 用户确认 → 错误反馈 → 模型修复 → 重新测试
        └── 否 → 完成
```

---

### 1.3 Claude Code：Hook-driven 验证注入

Claude Code 通过 **PostToolUse hooks** 注入验证结果，通过 **Stop hook** 强制 agent 继续工作直到验证通过。

#### PostToolUse Hook 示例

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [{
          "type": "command",
          "command": "npx prettier --write ${CLAUDE_PROJECT_DIR}/src/**/*.ts"
        }]
      },
      {
        "matcher": "Bash",
        "hooks": [{
          "type": "command",
          "command": "npm test 2>&1",
          "timeout": 30
        }]
      }
    ]
  }
}
```

#### Stop Hook 强制继续验证

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "prompt",
        "prompt": "Did the agent complete all requested tasks? Check test results and file changes.",
        "model": "haiku"
      }]
    }]
  }
}
```

**Stop Hook 返回 exit code 2 的行为**：强制 agent 继续工作而非停止。结合 Prompt Hook 类型，可以用 LLM 评估 agent 是否真正完成了任务——这就是一种**非自评**的验证方式。

#### 异步 Hook 运行测试（不阻塞 Agent Loop）

Claude Code 支持异步 command hook——测试在后台运行，不阻塞 agent：

```json
{
  "PostToolUse": [{
    "matcher": "Write",
    "hooks": [{
      "type": "command",
      "command": "python -m pytest -x --tb=short",
      "async": true
    }]
  }]
}
```

---

### 1.4 OpenCode：LSP 诊断 + 工具输出验证

OpenCode 利用 VSCode LSP 集成，在工具的 read 操作中自动包含语言服务器诊断信息。

#### LSP 集成原理

OpenCode 复用了 VS Code 已有的 LSP 能力。Read 工具的输出中会附带 LSP 诊断：

```
读取文件时返回:
├── 文件内容
├── LSP Diagnostics:
│   ├── Line 42: Warning: variable 'x' is declared but never used
│   ├── Line 78: Error: Type 'string' is not assignable to type 'number'
└── ...
```

这使得 agent 在**读取代码时就感知到类型错误和警告**，无需单独运行编译。

#### Lint 工具

OpenCode 的 lint 工具通过 hook 或命令执行：

```typescript
// 通过 PostToolUse hook 跑 lint
"tool.execute.after": (input, output) => {
  // 可在工具执行后自动运行 lint
  // output = { title, output, metadata }
  // 可以修改 output
}
```

#### Session Replay / Trace 机制（事件溯源）

OpenCode 使用 **Event Sourcing（事件溯源）**，所有状态变化都是事件：

```typescript
// 所有 SSE 事件都被持久化
用户输入 ──→ Event("user.prompted") ──→ Projector ──→ SQLite 投影表
LLM 响应 ──→ Event("text.delta")   ──→ Projector ──→ SessionMessageTable
工具执行 ──→ Event("tool.success") ──→ Projector ──→ SessionMessageTable
```

SQLite 表结构：
```sql
-- SessionMessageTable: session_id, id, type, data (JSON), seq
-- Cursor-based pagination by seq
```

这使得整个 session 可以**重放**——你可以回到任意时间点查看 agent 的思考过程、工具调用和输出。

---

### 1.5 Goose：RetryManager 的 success_checks 验证

Goose 的 `RetryManager` 支持定义**成功检查条件**：

```rust
// retry.rs:42-47
pub struct RetryManager {
    attempts: Arc<Mutex<u32>>,
    repetition_inspector: Option<Arc<Mutex<Option<RepetitionInspector>>>>,
}
```

配置格式（Recipe 中定义）：
```yaml
retry:
  max_retries: 3
  checks:
    - "curl -s http://localhost:3000/health | grep -q ok"
    - "python -m pytest tests/ -x"
  on_failure: "echo 'Report failure details'"
```

**`RetryOperation`** 在 StateMachine 管道中作为第 13 个操作，检查 goal/grind/retry 状态。工具执行后自动重置消息历史到初始状态并重试。

---

## 2. 自评失真：模型给自己的结果打高分的解决方案

> **核心问题**：模型在评估自己生成的代码时倾向于给出偏高评价。这是所有 agent 系统面临的根本性挑战。

### 2.1 Claude Code：独立 Auto-mode Classifier（最强方案）⭐⭐

这是业界**唯一的独立 ML 安全评估器**。不用主模型评估自己的行为，而是用**独立的 Sonnet 4.6 实例**。

#### 架构（yoloClassifier.ts）

```typescript
class YoloClassifier {
    async evaluate(toolCall, context):
        // 1. 加载 classifier 的专用 system prompt
        //    （独立的安全评估 prompt，不含 agent prose）
        prompt = loadClassifierPrompt()
        
        // 2. 两阶段评估：
        //    Phase 1: Fast-filter（模式匹配 + 启发式规则）
        if fastFilterPasses(toolCall):
            return "allow"
        
        //    Phase 2: Chain-of-thought（完整 LLM 推理）
        result = race(
            classifierModel.evaluate(toolCall, context),
            precomputedTimeout,
        )
        
        return result // "allow" | "block" | "escalate"
}
```

关键设计决策：
1. **Classifier 看不到 agent 的思考过程**——只看到工具调用参数
2. **两阶段**：快速过滤（零成本）+ LLM 评估（回退）
3. **与超时 race**：不阻塞 agent loop
4. **ArXiv 研究数据**（Sonnet 4.6, 2026-04）：
   - 安全率从 43.8% 提升到 59.4%
   - 偶尔对合法操作过度阻止

这不是概念——这是**实际部署的系统**，独立于主模型运行。

#### Anthropic 的三 Agent Harness（Planner/Generator/Evaluator）

Anthropic 的研究论文中描述了**三 Agent 分离实验**：
- **Planner**：制定计划
- **Generator**：生成代码（无法评估）
- **Evaluator**：独立评估（无法生成代码）

Claude Code 通过子 Agent 系统实现了类似的分离：
```markdown
Plan Agent (只读) → Explore Agent (只读搜索) → Build Agent (完整工具) → 交叉验证
```

关键是让**不同 Agent 角色之间交叉验证**，而非让同一个 Agent 自我评分。

---

### 2.2 Codex CLI：Ralph Wiggum Loop 多 Agent 交叉审查

OpenAI 博客原文：

> We instruct Codex to review its own changes locally, request additional specific agent reviews both locally and in the cloud, respond to any human or agent given feedback, and iterate in a loop until all agent reviewers are satisfied.

```python
# 概念伪代码
def ralph_wiggum_loop(task):
    while True:
        # 1. 执行
        result = agent.implement(task)
        
        # 2. 交叉审查（不是自评）
        reviews = []
        reviews.append(agent.self_review_local(result))     # 本地自审
        reviews.append(dispatch_to_cloud_agents(result))    # 云端多 Agent 审
        reviews.append(collect_human_feedback(result))      # 人类审查
        
        # 3. 只有全部审查通过才停止
        if all_satisfied(reviews):
            return result
        
        # 4. 响应反馈并迭代
        agent.respond_to_feedback(reviews)
```

**关键洞察**：Codex 将自己的变更交给**其他 Agent 实例**审查——不是同一个 model 自评，而是多个 agent 交叉验证。审查意见成为反馈信号。

---

### 2.3 Goose：AdversaryInspector 的 Fail-Open 设计

Goose 使用了**独立的安全审查器链**，而非让主模型自我评估。

#### AdversaryInspector（第3层）

```rust
// adversary_inspector.rs:56-78
/// 通过 ~/.config/goose/adversary.md 激活
/// 文件格式：
///   tools: shell, computercontroller__automation_script
///   ---
///   BLOCK if the command exfiltrates data or is destructive.
///   ALLOW normal development operations.
///
/// 设计原则：失败时 "fail-open"（允许通过），不阻塞正常工作
```

Fail-open 设计（adversary_inspector.rs:469-488）：
```rust
Err(e) => {
    // LLM 调用失败 → fail-open（允许通过）
    tracing::warn!(security.action = "ALLOW", "adversary review: error (fail-open)");
    results.push(InspectionResult {
        action: InspectionAction::Allow,
        reason: format!("Adversary error (fail-open): {}", e),
        confidence: 0.0,
    });
}
```

**核心设计**：
1. AdversaryInspector 是一个**独立的安全检查器**，不是主模型的一部分
2. 使用用户自定义的 `adversary.md` 规则文件
3. Fail-open：审查器出错时不阻止用户工作
4. 与 SecurityInspector（正则匹配）+ EgressInspector（网络检测）+ PermissionInspector + RepetitionInspector 组成 5 层检查链

#### 决策合并规则

```rust
// tool_inspection.rs - 多个 Inspector 的结果取最严格的：
// 任何一个返回 Deny → 拒绝
// 任何一个返回 RequireApproval → 至少需要确认
// 全部 Allow → 允许
```

这本质上是**多方交叉验证**——不同 inspector 从不同角度评估同一操作，减少单一评估源的偏差。

---

### 2.4 Aider：精确匹配强约束（宁可报错不猜测）

Aider 解决自评失真的方式很独特：**不让模型自我评估**，而是用**确定性机制**验证输出。

#### SEARCH/REPLACE 精确匹配

```python
# editblock_coder.py - replace_most_similar_chunk 的尝试顺序：
# 1. Perfect match（精确逐行匹配）— 第一优先级
# 2. Leading whitespace 容错
# 3. 去除首行空行
# 4. '...' 省略号支持
# 5. Fuzzy matching（已在当前版本中 disabled！）
```

**设计哲学**：精确匹配失败时，不默默猜测，而是**报错反馈**：

```python
# 失败时返回精确错误信息
res = f"# {len(failed)} SEARCH/REPLACE blocks failed to match!\n"
for edit in failed:
    res += f"""
## SearchReplaceNoExactMatch: This SEARCH block failed to exactly match lines in {path}
<<<<<<< SEARCH
{original}=======
{updated}>>>>>>> REPLACE
"""
    # 提供 "Did you mean..." 建议
    did_you_mean = find_similar_lines(original, content)
    if did_you_mean:
        res += f"Did you mean to match some of these actual lines from {path}?\n..."
```

这是**机械验证替代模型自我评估**的典范——成功/失败由精确的文本匹配决定，而非模型的"感觉"。

---

## 3. 代码质量评估：耦合性、架构、规范性

### 3.1 Codex CLI：自定义 Linter 强制层级架构依赖方向 ⭐⭐

这是整个调研中最亮眼的代码质量保障实践。

OpenAI 博客原文（harness-engineering）：

> We built the application around a rigid architectural model. Each business domain is divided into a fixed set of layers, with strictly validated dependency directions and a limited set of permissible edges. **These constraints are enforced mechanically via custom linters** (Codex-generated, of course!) and structural tests.

架构规则：
```
每个业务域（如 App Settings）内：
  Types → Config → Repo → Service → Runtime → UI
                      ↑
             Providers（跨领域切面的唯一入口）
              - auth, connectors, telemetry, feature flags

规则：
  - 代码只能沿着此方向依赖（前向依赖）
  - 跨领域关注点只能通过 Providers 进入
  - 其他依赖被自定义 linter 机械拒绝
  - linter 的错误消息包含"修复指令"注入 agent context
```

**关键洞察**：自定义 linter 的错误消息被设计为 **agent-readable**：

```python
# 概念伪代码：自定义架构 linter
def enforce_architecture(file_path):
    imports = parse_imports(file_path)
    layer = determine_layer(file_path)
    
    for imp in imports:
        target_layer = determine_layer(imp.target)
        # 违反依赖方向
        if not is_valid_direction(layer, target_layer):
            yield {
                "error": f"ARCHITECTURE VIOLATION: {file_path} (layer={layer}) imports from {imp.target} (layer={target_layer})",
                "fix": f"Move the imported logic to {imp.target} into a Provider interface and inject it through the domain's Provider entry point.",
                "example": "// See docs/design-docs/core-beliefs.md#dependency-rules for the correct pattern"
            }
```

其他机械规则（"Taste Invariants"）：
- 结构化日志的静态强制
- Schema 和类型的命名约定
- 文件大小限制
- 平台特定可靠性要求
- **"Parse, don't validate"**——强制在边界处解析数据形状

OpenAI 还运行**周期性的背景 Codex 任务**（"garbage collection"）：
- 扫描偏离模式的代码
- 更新质量评分
- 打开有针对性的重构 PR
- 大多数能在 1 分钟内审查完毕并自动合并

---

### 3.2 Aider：每轮自动 Lint + Git Commit 可逆性保证

Aider 的代码质量保障是**轻量级但实用的**：

#### 自动 Lint

```python
# base_coder.py:105-107
class Coder:
    auto_lint = True
    auto_test = False
    test_cmd = None
```

配置：
```bash
aider --lint-cmd "ruff check --fix: ruff format --check"
```

每轮编辑后自动执行：
1. `apply_updates()` 应用文件编辑
2. `auto_commit(edited)` 自动 Git 提交
3. `lint_edited(edited)` Lint 检查
4. 有 lint 错误 → 询问用户 → 将错误反馈给模型修复
5. `auto_commit(edited, context="Ran the linter")` Lint 修复后再次提交

#### Git 可逆性

每轮自动 Git commit 是**最安全的质量保障**——任何质量问题都可以 `/undo`：

```python
# commands.py:731-751
def raw_cmd_undo(self, args):
    # 5 层安全检查：
    # 1. 最后 commit 必须是 aider 做的
    # 2. 不能是 merge commit（>1 parent）
    # 3. 文件不能有未提交的修改
    # 4. 文件在上一个 commit 中必须存在
    # 5. 不能已推送到 origin
    
    # 执行：逐个文件 checkout HEAD~1 + soft reset
    for file_path in changed_files_last_commit:
        self.coder.repo.repo.git.checkout("HEAD~1", file_path)
    self.coder.repo.repo.git.reset("--soft", "HEAD~1")
```

---

### 3.3 Claude Code：LSP 工具提供语言服务器诊断

Claude Code 的工具列表中包含 **LSP 工具**：

```
核心文件操作:
| LSP | ✅ | 语言服务器协议（代码导航）|
```

LSP 工具提供：
- 代码导航（定义跳转、引用查找）
- **类型错误和警告**（编译时）
- 代码补全上下文

这使 agent 能在不运行完整构建的情况下获取类型检查反馈。

#### 自定义 Code Reviewer 子 Agent

```markdown
---
name: code-reviewer
description: Reviews code for quality and best practices. Use proactively.
tools: Read, Glob, Grep
model: sonnet
permissionMode: acceptEdits
---
You are a code reviewer. When invoked, analyze the code and provide 
specific, actionable feedback on quality, security, and best practices.
```

---

### 3.4 OpenCode：LSP 集成在 Read 工具中

OpenCode 作为 IDE Agent（VS Code 扩展），天然利用了 IDE 的 LSP：

- Read 工具输出中包含当前文件的 LSP 诊断（类型错误、编译警告）
- Agent 在**读取代码的阶段就能看到质量问题**
- 不需要单独的工具调用来跑类型检查

这是"**将质量问题暴露在最早可能的时刻**"的设计——agent 在理解代码时就知道了问题所在。

---

## 4. 观测与追踪：Agent 行为如何被记录和审查

### 4.1 OpenCode：Event Sourcing + SQLite（最强审计方案）

OpenCode 的 Session 管理采用**完整的事件溯源（Event Sourcing）**：

```
用户输入 ──→ Event("user.prompted") ──→ Projector ──→ SQLite 投影表
LLM 响应 ──→ Event("text.delta")   ──→ Projector ──→ SessionMessageTable
工具执行 ──→ Event("tool.success") ──→ Projector ──→ SessionMessageTable
```

#### SQLite 存储结构

```sql
-- SessionTable: session_id, project_id, directory, title, agent, model, cost, tokens
-- SessionMessageTable: session_id, id, type, data (JSON), seq
-- SessionInputTable: session_id, id, prompt, delivery, admitted_seq, promoted_seq
-- SessionContextEpochTable: session_id, baseline, snapshot, baseline_seq
-- PermissionTable: id, project_id, action, resource
```

#### 全量可重放

```typescript
// packages/core/src/session.ts
export interface Interface {
    messages: (input: { sessionID, limit?, cursor? }) => Effect<Message[]>
    context: (sessionID) => Effect<Message[]>
    revert: {
        stage: (input: { sessionID, messageID, files? }) => Effect<Revert.State>
        commit: (sessionID) => Effect<void>
    }
}
```

- **Cursor 分页**：基于 seq 的前后翻页
- **Revert 三步**：Stage → Review → Commit
- **每项目独立 SQLite**：隔离 + 无跨项目泄漏

---

### 4.2 Claude Code：JSONL 转录 + Sidechain + Session Memory

#### JSONL 转录

每个 agent 实例（包括子 agent）都写自己的 `.jsonl` 转录文件：

```jsonl
{"type":"user","content":"分析 auth 模块","timestamp":"..."}
{"type":"assistant","content":"","tool_calls":[{"name":"Grep","args":{"pattern":"auth*"}}]}
{"type":"tool_result","tool_call_id":"...","content":"..."}
{"type":"assistant","content":"auth 模块有 3 个端点..."}
```

#### Sidechain Transcripts（子 Agent 独立转录）

```
Parent Session:
  .claude/transcripts/session-123.jsonl

Subagent (auth):
  .claude/transcripts/session-123.auth-explore.jsonl  ← 独立文件

Subagent (db):
  .claude/transcripts/session-123.db-explore.jsonl    ← 独立文件

多实例协调使用 POSIX flock()——零外部依赖
```

#### Session Memory 后台笔记

Session Memory 是一个**后台 fork agent**，在整个 session 期间持续维护结构化笔记：

```markdown
# Session Title
_A short and distinctive 5-10 word descriptive title..._

# Current State
_What is actively being worked on right now?..._

# Task specification
_What did the user ask to build?..._

# Files and Functions
_What are the important files?..._

# Errors & Corrections
_Errors encountered and how they were fixed..._

# Worklog
_Step by step, what was attempted, done?..._
```

这提供了**完整的工作记录**，即使原始对话已被 compaction 压缩。

---

### 4.3 Codex CLI：Chrome DevTools + 临时观测栈

Codex CLI 的观测能力体现在**运行时可观测性**，而非事后审计。

#### Chrome DevTools Protocol（UI 验证）

```
Agent → CDP:
  ├── Page.navigate(url)          # 导航
  ├── Runtime.evaluate(script)    # 执行 JS
  ├── DOMSnapshot.captureSnapshot()  # 快照
  ├── Page.captureScreenshot()    # 截图
  └── Runtime.consoleAPICalled    # 监听事件
```

> "Record a video demonstrating the failure" → "Implement a fix" → "Record a second video demonstrating the resolution"

#### 临时观测栈（Ephemeral Observability Stack）

每个 Git worktree 有独立的临时观测栈：
```
Git Worktree
├── 应用实例（隔离启动）
├── 日志 → Vector → Victoria Logs（LogQL 查询）
├── 指标 → Vector → Victoria Metrics（PromQL 查询）
└── 追踪 → Vector → Victoria Traces（TraceQL 查询）

任务完成后 worktree + 观测栈全部销毁
```

#### App Server 的 Item/Turn/Thread 三层抽象

```json
// Item — 原子 I/O 单元
{"type":"item/started","item":{"type":"agent_message"}}
{"type":"item/*/delta","delta":"..."}
{"type":"item/completed","item":{...}}

// Turn — 一次用户输入触发的完整 Agent 工作单元
{"type":"turn/started"}
// ... items ...
{"type":"turn/completed"}

// Thread — 持久化会话容器
{"type":"thread/started","thread_id":"..."}
```

这些事件通过 JSON-RPC over stdio 推送，客户端可以订阅和录制。

---

### 4.4 Goose：5 层 Inspector 链 + OpenTelemetry

#### tool_inspection.rs：5 个 Inspector 的决策级联

```rust
// agent.rs:697-724
fn create_tool_inspection_manager(...) -> ToolInspectionManager {
    let mut manager = ToolInspectionManager::new();
    manager.add_inspector(Box::new(SecurityInspector::new()));       // 第1层
    manager.add_inspector(Box::new(EgressInspector::new()));         // 第2层
    manager.add_inspector(Box::new(AdversaryInspector::new(...)));   // 第3层
    manager.add_inspector(Box::new(PermissionInspector::new(...)));  // 第4层
    manager.add_inspector(Box::new(RepetitionInspector::new(None))); // 第5层
    manager
}
```

#### RepetitionInspector（第5层）— 重复调用检测

```rust
// retry.rs:42-47
pub struct RetryManager {
    attempts: Arc<Mutex<u32>>,
    repetition_inspector: Option<Arc<Mutex<Option<RepetitionInspector>>>>,
}
```

RepetitionInspector 检测 agent 是否陷入了重复调用同一工具的循环。这是**行为模式检测**，不是内容检测。

#### Usage 追踪（usage.rs）

```rust
// usage.rs:9-61
fn attach_to_last_assistant(effects: &mut [StateEffect], usage: &ProviderUsage) {
    // 从后往前找 effects 中最后一个非错误的 assistant AppendMessage
    let message = effects.iter_mut().rev().find_map(|effect| match effect {
        StateEffect::AppendMessage(message)
            if message.role == Assistant && message.error_kind().is_none() => Some(message),
        _ => None,
    })?;
    message.metadata.usage = Some(Box::new(MessageUsage::from_provider_usage(usage, false)));
}
```

Usage 数据用于：
1. Compaction 触发：`session.usage.total_tokens` vs `context_limit * threshold`
2. UI 展示：`AgentEvent::MessageUsage` 实时推送给前端
3. 成本估算：`canonical::maybe_get_canonical_model` 查找定价
4. OpenTelemetry 追踪：`gen_ai.usage.input_tokens/output_tokens` span 属性

---

### 4.5 Aider：Git 归因 + Commit Hash 追踪

Aider 的追踪是**Git 原生的**：

```bash
# 三种归因策略
git commit --author="UserName (aider)"
git commit --committer="UserName (aider)"
# Commit 尾部：
# Co-authored-by: aider (gpt-4o) <aider@aider.chat>
```

```python
# aider_commit_hashes 集合追踪所有 aider 做的 commit
if last_commit_hash not in self.coder.aider_commit_hashes:
    self.io.tool_error("The last commit was not made by aider...")
    return
```

成本追踪：
```python
# 每轮显示
# Tokens: 2,341 input / 456 output | Cost: $0.023
self.total_cost += cost
```

---

## 5. 跨项目对比矩阵

| 维度 | OpenCode | Claude Code | Aider | Codex CLI | Goose |
|------|----------|-------------|-------|-----------|-------|
| **端到端验证** | LSP 诊断 + lint hook | PostToolUse hook + Stop hook 强制继续 | 自动 lint + 自动测试 + 反馈循环 | PEV + CDP UI 截图 + LogQL/PromQL | RetryManager success_checks |
| **自评失真方案** | 权限规则机械判定 | ⭐ 独立 Auto-mode Classifier + 子 Agent 交叉验证 | 精确匹配机械验证 | Ralph Wiggum Loop 多 Agent 交叉审查 | 5 层 Inspector 链 + AdversaryInspector |
| **代码质量** | LSP 在 Read 时注入诊断 | LSP 工具 + 子 Agent Code Review | 自动 lint + Git 可逆性 | ⭐⭐ 自定义 Linter 强制层级架构 + "Taste Invariants" | Hook 扩展（BeforeShellExecution 等） |
| **观测/追踪** | ⭐ Event Sourcing + SQLite 全量重放 | JSONL 转录 + Sidechain + Session Memory 笔记 | Git 归因 + commit hash 追踪 | CDP + 临时观测栈 + Item/Turn/Thread | OpenTelemetry + 5 层 Inspector 链 |
| **调试工具** | Session Replay（Cursor 分页） | Sidechain 独立回放 | `/undo` 回退 | 视频录制验证（CDP 截图） | RepetitionInspector 死循环检测 |

---

## 6. 核心启示

### 6.1 验证闭环的三个层次

```
Level 1: 机械验证（成本最低，确定性最高）
  → Aider 的精确匹配、Codex 的自定义 linter、Goose 的正则 Inspector

Level 2: 工具辅助验证（中等成本）
  → PostToolUse hook 跑测试、LSP 诊断、LogQL 查询

Level 3: 独立 Agent 评估（成本最高，适合复杂判断）
  → Claude Code 的独立 Classifier、Codex 的 Ralph Wiggum Loop
```

### 6.2 解决自评失真的三条铁律

1. **不让评估者生成代码**（Claude Code Auto-mode Classifier）
2. **机械验证优先于模型判断**（Aider 精确匹配、Codex 架构 Linter）
3. **多方交叉验证 > 自我评分**（Codex Ralph Wiggum Loop、Goose 5 层 Inspector）

### 6.3 代码质量保障的机械论

- **Codex CLI 的层级架构 Linter** 是最值得借鉴的设计——将架构规则编码为机器可执行的检查，错误消息包含修复指令
- **Aider 的 Git 可逆性** 是最低成本的质量保障——任何操作可撤销
- **LSP 集成** 让 agent 最早感知问题

### 6.4 观测追踪的数据结构选择

- **Event Sourcing + SQLite**（OpenCode）：最强审计能力，支持重放和时间旅行
- **JSONL 转录 + Sidechain**（Claude Code）：简单实用，子 Agent 转录独立
- **Item/Turn/Thread 三层抽象**（Codex CLI）：统一协议，多客户端共享
- **OpenTelemetry**（Goose）：标准化追踪，与现有监控体系集成

### 6.5 最值得借鉴的 5 个设计

| 排名 | 设计 | 来源 | 理由 |
|------|------|------|------|
| 1 | 自定义架构 Linter 强制依赖方向 | Codex CLI | 将架构规则从"文档建议"升级为"机械强制"，错误消息注入修复指令 |
| 2 | 独立 Auto-mode Classifier（非主模型自评） | Claude Code | 唯一将"反自评失真"实现为独立系统的项目 |
| 3 | Event Sourcing 全量 Session 重放 | OpenCode | 最完整的审计方案，支持时间旅行调试 |
| 4 | Ralph Wiggum Loop 多 Agent 交叉审查 | Codex CLI | 将"审查"本身也 Agent 化，审查者比实施者多 |
| 5 | 每轮自动 Git Commit + 精确匹配 | Aider | 最低成本的确定性验证，任何操作可逆 |
