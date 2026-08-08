# Hermes Agent 驾驭工程深度自分析

> 基于 `NousResearch/hermes-agent` main 分支源码分析
> 核心文件: `run_agent.py`, `agent/prompt_builder.py`, `AGENTS.md`

## 1. System Prompt 构建 — prompt_builder.py 源码级

### 分层注入架构

```python
# agent/prompt_builder.py: build_system_prompt()
# System prompt 组装顺序（严格分层）：

Layer 1: DEFAULT_AGENT_IDENTITY
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research.
     You are helpful, knowledgeable, and direct..."

Layer 2: HERMES_AGENT_HELP_GUIDANCE
    "documentation at https://hermes-agent.nousresearch.com/docs is your
     authoritative reference... Load the `hermes-agent` skill..."

Layer 3: MEMORY_GUIDANCE
    "You have persistent memory across sessions. Save durable facts... 
     Write memories as declarative facts, not instructions to yourself..."

Layer 4: SESSION_SEARCH_GUIDANCE
    "When the user references something from a past conversation... 
     use session_search to recall it..."

Layer 5: SKILLS_GUIDANCE (含 Skill Safety Rule)
    "After completing a complex task (5+ tool calls)... save as skill...
     If [SKILL_PRUNED] → reload with skill_view()..."

Layer 6: Memory entries (user profile + agent notes) — 每 turn 注入

Layer 7: Skills index (从磁盘扫描，匹配 environment/platform 的)
    → build_skills_system_prompt()

Layer 8: Context files (按优先级发现，first-match-wins):
    1. .hermes.md / HERMES.md  (walk to git root)
    2. AGENTS.md / agents.md   (cwd only)
    3. CLAUDE.md / claude.md   (cwd only)
    4. .cursorrules / .cursor/rules/*.mdc (cwd only)

Layer 9: SOUL.md (从 $HERMES_HOME, 独立于 project context)

Layer 10: Environment hints (OS, home, cwd, shell, terminal backend)

Layer 11: Ephemeral prompts (budget warnings, context pressure, etc.)
```

### 硬编码的威胁扫描

```python
# agent/prompt_builder.py: _scan_context_content()
# 所有 context files 在注入 system prompt 前经过扫描：
from tools.threat_patterns import scan_for_threats as _scan_for_threats

def _scan_context_content(content: str, filename: str) -> str:
    # 使用 "context" scope: 检测经典 injection + promptware/C2 + role-play hijack
    # 不检测: SSH backdoor, persistence, exfil-URL（对 cloned repo 太激进）
    findings = _scan_for_threats(content, scope="context")
    if findings:
        return f"[BLOCKED: {filename} contained potential prompt injection...]"
    return content
```

### Context File 发现链的硬约束

```python
# agent/prompt_builder.py: build_context_files_prompt()

# 1. .hermes.md: 从 cwd 向上走到 git root
def _find_hermes_md(cwd: Path) -> Optional[Path]:
    stop_at = _find_git_root(cwd)  # .git 目录为界
    for directory in [current, *current.parents]:
        for name in (".hermes.md", "HERMES.md"):
            if (directory / name).is_file():
                return directory / name
        if stop_at and directory == stop_at:
            break  # 不越过 git root

# 2. AGENTS.md: cwd only，不向上搜索
# 3. CLAUDE.md: cwd only，不向上搜索
# 4. .cursorrules: cwd only

# 关键：first-match-wins — 只加载一种 context 类型！
# 找到 .hermes.md 就不再检查 AGENTS.md
```

### 硬约束：不加载 Hermes 安装树的 Context

```python
# 防止：backend 自己 spawn 到 Hermes 安装目录时，
# 把 Hermes 自己的 AGENTS.md 当作项目 context 加载
if cwd_is_fallback and not allow_install_tree_fallback and _is_install_tree(cwd_path):
    logger.warning("skipping project-context discovery: fell back to Hermes install tree")
    project_context = ""
```

### Context 文件大小截断

```python
# 默认上限：20,000 字符
# 超出部分：head + tail 截断，中间用 [...truncated...] 标记
# 可通过 config context_file_max_chars 或动态计算覆盖
```

## 2. Agent Loop — run_agent.py 源码级

### 纯同步 while 循环

```python
# run_agent.py: AIAgent.run_conversation()
# 完全同步 — 无 async/await（工具调用通过 ThreadPoolExecutor 并行化）

def run_conversation(self, user_message: str) -> dict:
    # 1. Generate task_id
    # 2. Append user message to conversation history
    # 3. Build system prompt (prompt_builder.py)
    # 4. Check preflight compression (>50% context)
    # 5. Build API messages:
    #    - chat_completions: OpenAI 格式
    #    - codex_responses: 转换成 Responses API
    #    - anthropic_messages: 通过 anthropic_adapter.py
    # 6. Inject ephemeral prompt layers
    # 7. Apply provider-specific settings
    
    while api_call_count < self.max_iterations and self.iteration_budget.remaining > 0:
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tool_schemas
        )
        if response.tool_calls:
            for tool_call in response.tool_calls:
                result = handle_function_call(tool_call.name, tool_call.args, task_id)
                messages.append(tool_result_message(result))
                api_call_count += 1
        else:
            return response.content
```

### 硬约束：Ephemeral Scaffolding Protection

```python
# run_agent.py: _EPHEMERAL_SCAFFOLDING_FLAGS
# 标记为"不可持久化"的内部恢复消息类型：
_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_empty_recovery_synthetic",    # 空回复恢复
    "_empty_terminal_sentinel",      # 终端空 sentinel
    "_thinking_prefill",            # 思考预填充
    "_verification_stop_synthetic",  # 验证停止
    "_pre_verify_synthetic",        # 预验证
    "_kanban_stop_synthetic",       # kanban 停止
    "_dropped_toolcall_nudge",     # 丢弃的 tool call nudge
)

# 这些消息只用于驱动重试循环，绝不持久化到 SQLite/JSONL
# 否则恢复的会话会回放这些内部消息
```

### Max Iterations（防无限循环）

```python
# agent.iteration_budget.IterationBudget
# 默认 max_iterations = 90（可通过 config 调整）
# 同时有 token 和 时间的 budget 控制
```

### Preflight Compression

```python
# Step 4: Check preflight compression (>50% context)
# 在每次 LLM 调用前检查 context 用量
# 超过 50% 阈值时触发预压缩
```

### Tool Call 并行化

```python
# 8 个工作线程的 ThreadPoolExecutor
_MAX_TOOL_WORKERS = 8

# 工具调用分批并行执行：
# _should_parallelize_tool_batch() 判断哪些 tool calls 可以并行
# 基于文件路径重叠检测：写同一个文件的 tool calls 不能并行
```

## 3. Skills 系统 — 渐进式加载

### 硬约束：Skill Safety Rule

```python
SKILLS_GUIDANCE = """
## Skill Safety Rule
1. **UNAVAILABLE** — If [SKILL_PRUNED], the skill content was lost in compression
2. **RELOAD** — Before any skill-dependent action, re-check with skill_view()
3. **WAIT** — If a skill is loading, wait for reload confirmation
4. **DEDUP** — After reloading, ignore remaining [SKILL_PRUNED] markers for same skill
"""
```

### Skills Index 构建

```python
# agent/prompt_builder.py: build_skills_system_prompt()
# 扫描 $HERMES_HOME/skills/ 目录
# 过滤：排除 EXCLUDED_SKILL_DIRS，匹配 platform/environment
# 只注入 name + description + file_path（不注入内容！）
# 模型需要时通过 skill_view() 加载完整内容
```

### Curator 系统

```python
# 自动维护 agent 创建的 skills
# - 追踪使用频率
# - 标记闲置 skills 为 stale
# - Archive（最大破坏性操作），绝不 delete
# - Pinned skills 豁免所有自动转换
# - Consolidation pass 默认关闭（curator.consolidate: false）
```

## 4. Memory 系统

### 双层存储 + 状态机

```
User Profile (target='user')  ← 1,375 char 上限
    → 身份、偏好、风格、角色

Agent Notes (target='memory') ← 2,200 char 上限
    → 环境事实、约定、工具特性、教训

Provider: 可插拔 (built-in, Honcho, Mem0, ...)
```

### 硬编码指南

```python
# MEMORY_GUIDANCE 中的关键规则：
# "Save durable facts using the memory tool"
# "Write memories as declarative facts, not instructions to yourself"
#   ✓ 'User prefers concise responses'
#   ✗ 'Always respond concisely' (命令式会被重复读取为指令)
# "Do NOT save task progress, PR numbers, commit SHAs..."
# "If a fact will be stale in a week, it does not belong in memory"
```

### 原子批量操作

```python
# memory(operations=[...])
# 一次调用 = 原子操作
# 只在 FINAL 结果检查字符限制
# 可先 delete 再 add 在同一操作中腾出空间
```

## 5. 安全硬约束

### API Key 管理

```python
# Fernet 加密存储
# 密文带 v1: 前缀
# 只回显尾 4 位
# 任何接口不返回明文
```

### Secret Redaction

```python
# security.redact_secrets: 默认 ON
# 工具输出（terminal stdout, read_file, web content, subagent 摘要）
# 在进入 conversation context 和 logs 前扫描
# 扫描字符串：API keys, tokens, secrets
# LLM 不可在运行时关闭（import 时快照）
```

### 命令审批

```python
# approvals.mode: smart (默认)
# - smart: 辅助 LLM 评估破坏性命令
# - manual: 始终提示
# - off: 跳过所有 = --yolo

# 注意：YOLO 不关闭 secret redaction，两者独立
```

## 6. 进程管理

### 子 Agent 委派

```python
# delegate_task: 批量并发 max 3，实时日志，context 隔离
# 子 Agent 不可透明信任——必须验证结果
# max_spawn_depth = 1（防递归爆炸）
# 不持久——父进程退出 = 子 Agent 丢失
```

### 后台进程

```python
# terminal(background=True, notify_on_complete=True)
# 不同于 delegate_task：完全独立进程，全工具访问
# 用于长时间任务（小时/天级别）
```

## 7. 对 Jeeves 的核心启示

| Hermes 做法 | 优点 | Jeeves 当前状态 |
|------------|------|----------------|
| **Ephemeral Scaffolding** | 内部恢复消息不持久化 → 恢复时不污染 transcript | ❌ 缺失 |
| **Threat Scanner on Context** | 所有 context files 过扫描 → 防 prompt injection | ❌ 缺失 |
| **First-Match-Wins Context** | 只加载一种 context 类型 → 避免冗余 | ✅ 类似 |
| **Skills 渐进式** | 索引进 prompt，内容不进 → 低 context 开销 | ✅ 已实现 |
| **Skill Safety Rule** | [SKILL_PRUNED] 检测 + reload 流程 | ❌ 缺失 |
| **Memory 声明式规则** | 防止命令式记忆被重复读取为指令 | ✅ 已有规则 |
| **原子 Memory 操作** | add+delete 在同一操作中 | ✅ 已实现 |
| **Secret Redaction** | import 时快照，LLM 不可关闭 | ❌ 缺失 |
| **Preflight Compression** | >50% 时预压缩 | ❌ 缺失 |
| **Install Tree Guard** | 不加载 Hermes 自己的 AGENTS.md | N/A（Jeeves 不会有此问题） |
