# AI Coding Agent 状态管理深度研究

> 研究日期：2026-08-08
> 覆盖项目：OpenCode、Aider、Claude Code、Goose
> 数据来源：各项目源码直接阅读（repo-research 目录下的 clone）

---

## 目录

1. [运行时中间记忆](#1-运行时中间记忆)
2. [工作区认知地图](#2-工作区认知地图)
3. [会话状态 vs 持久状态](#3-会话状态-vs-持久状态)
4. [Artifact/产物管理](#4-artifact产物管理)
5. [横向对比矩阵](#5-横向对比矩阵)
6. [对 Jeeves 的启示](#6-对-jeeves-的启示)

---

## 1. 运行时中间记忆

### 1.1 核心问题

Agent 在执行多步任务时会产生大量中间状态——"已经读过的文件"、"当前在做什么"、"下一步计划"、"上次失败的原因"。这些信息需要存在于某处，但不必全部塞进每次 LLM 调用的 context。

### 1.2 OpenCode: TodoWrite 工具 + SQLite 待办表

**机制**：Agent 通过 `todowrite` 工具主动管理自己的待办列表，列表持久化到 SQLite。

**`todowrite` 工具定义**（`packages/core/src/tool/todowrite.ts:14-16`）：
```typescript
export const Input = Schema.Struct({
  todos: Schema.Array(SessionTodo.Info).annotate({
    description: "The updated todo list"
  }),
})
```

**描述**（`:34-35`）：
```
"Create and maintain a structured task list for the current coding session.
Use it to track progress during multi-step work and keep todo statuses current."
```

**执行流程**（`:39-51`）：
```typescript
execute: (input, context) =>
  Effect.gen(function* () {
    // 1. 权限检查
    yield* permission.assert({ action: name, resources: ["*"], ... })
    // 2. 更新 SQLite
    yield* todos.update({ sessionID: context.sessionID, todos: input.todos })
    return { todos: input.todos }
  })
```

**SessionTodo 持久化**（`packages/core/src/session/todo.ts:32-57`）：
```typescript
const update = Effect.fn("SessionTodo.update")(function* (input) {
  yield* db.transaction((tx) =>
    Effect.gen(function* () {
      // 1. 全量删除旧记录
      yield* tx.delete(TodoTable).where(eq(TodoTable.session_id, input.sessionID)).run()
      if (input.todos.length === 0) return
      // 2. 批量插入新记录
      yield* tx.insert(TodoTable).values(
        input.todos.map((todo, position) => ({
          session_id: input.sessionID,
          content: todo.content,
          status: todo.status,       // pending | in_progress | completed | cancelled
          priority: todo.priority,   // high | medium | low
          position,
        })),
      ).run()
    }),
  )
  // 3. 发布事件
  yield* events.publish(Event.Updated, input)
})
```

**Todo 表结构**（`packages/core/src/session/sql.ts:100-117`）：
```sql
CREATE TABLE todo (
  session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  priority TEXT NOT NULL,
  position INTEGER NOT NULL,
  time_created INTEGER NOT NULL DEFAULT (unixepoch()),
  time_updated INTEGER NOT NULL DEFAULT (unixepoch()),
  PRIMARY KEY (session_id, position)
)
```

**关键设计**：
- **Agent 自驱动**：不是系统自动生成，而是 Agent 自己决定何时调用 `todowrite` 更新列表
- **全量替换**：每次更新都先删后插（`DELETE + INSERT`），而非增量修补
- **有序列表**：`position` 字段保证顺序，按 `position ASC` 查询
- **与 session 生命周期绑定**：`ON DELETE CASCADE`——session 删除时 todo 自动清除
- **变化传播**：通过 `EventV2` 发布 `Event.Updated` 事件，让 UI 和其他组件感知变化

### 1.3 Aider: 内存中的运行时状态

Aider 不使用持久化存储来做运行时记忆，而是在 Coder 对象的内存字段中维护。

**关键运行时字段**（`aider/coders/base_coder.py:88-122`）：
```python
class Coder:
    abs_fnames = None              # 已添加到对话的文件路径集合
    abs_read_only_fnames = None    # 只读文件集合
    aider_edited_files = None      # 本轮编辑过的文件集合（set）
    last_aider_commit_hash = None  # 最后一次 auto-commit 的 hash
    aider_commit_hashes = set()    # 所有 aider 产生的 commit hash
    cur_messages = []              # 当前对话轮次的消息列表
    done_messages = []             # 已压缩的历史消息
    commit_before_message = []     # 每轮开始前的 HEAD commit hash 栈
    num_exhausted_context_windows = 0
    num_malformed_responses = 0
    num_reflections = 0            # 当前反射/重试次数
    max_reflections = 3
    reflected_message = None       # 反射信息（如 lint 错误、编辑格式错误）
    lint_outcome = None            # lint 结果
    test_outcome = None            # 测试结果
    shell_commands = []            # 待执行的 shell 命令
    message_cost = 0.0             # 本轮消息的成本
```

**`init_before_message()` — 每轮开始的初始化**（`:864-874`）：
```python
def init_before_message(self):
    self.aider_edited_files = set()       # 重置编辑文件集合
    self.reflected_message = None          # 清除反射信息
    self.num_reflections = 0              # 重置反射次数
    self.lint_outcome = None              # 重置 lint 结果
    self.test_outcome = None              # 重置测试结果
    self.shell_commands = []              # 清空 shell 命令
    self.message_cost = 0                 # 重置成本
    if self.repo:
        self.commit_before_message.append(self.repo.get_head_commit_sha())
```

**关键观察**：
- `cur_messages` 是**跨轮次累积**的——每轮的用户输入和 assistant 回复都追加进去
- `done_messages` 是**被压缩后的历史**——不参与 LLM 请求但保留在内存中
- `reflected_message` 是**本轮重试控制信号**——如果非空，会触发新一轮 LLM 调用（带反馈）
- `commit_before_message` 是一个**栈**——每轮 push 一个 HEAD，用于 `/undo` 追踪
- `aider_edited_files` 是 `set()`——去重后的编辑文件集合

### 1.4 Claude Code: Session Memory 后台笔记

Claude Code 运行时记忆的最独特设计是 **Session Memory**——一个后台 fork agent 在整个会话期间持续维护结构化笔记。

**笔记模板**（来源：社区逆向 + 官方文档）：
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

**工作机制**：
- 后台 fork agent（共享主 session 的 prompt cache prefix）
- 只允许使用 Edit 工具操作笔记文件
- 触发条件：token count + tool call count 双阈值，且最近回复不含 tool call
- 每节有 2000 token 上限，总计 12000 token
- 笔记文件是**运行时记忆的持久化形式**，compaction 时直接作为摘要使用——零额外 LLM 调用

### 1.5 Goose: Conversation 对象内的中间状态

Goose 没有单独的 todo 或 task list 机制。中间状态附着在消息的 `metadata` 字段上。

**Message 结构**（`goose-provider-types/src/conversation.rs`）：
```rust
pub struct Conversation(Vec<Message>);
// Message 包含: id, role, content (Vec<MessageContentBlock>), metadata
```

**Usage 附着在消息上**（`goose/src/agent.rs:330-353`）：
```rust
fn attach_turn_usage(messages, usage, preferred_message_id) {
    // 从后往前找 role=Assistant 的消息
    let message_index = messages.iter().rposition(|msg| msg.role == Assistant)?;
    let message = &mut messages[message_index];
    message.metadata.usage = Some(Box::new(message_usage));
}
```

**Session 级别的 usage 追踪**（`session_manager.rs:61-97`）：
```rust
pub struct Session {
    pub usage: Usage,              // 当前累计
    pub accumulated_usage: Usage,   // 历史累计（跨 compaction）
    pub accumulated_cost: Option<f64>,
    pub message_count: usize,
    pub last_message_at: Option<DateTime<Utc>>,
}
```

**Compaction 时的状态保留**：Goose 的 `compact_messages` 保留最近的用户消息，将其标记为 `agent_only`，而非完全丢弃。

### 1.6 对比总结

| 维度 | OpenCode | Aider | Claude Code | Goose |
|------|----------|-------|-------------|-------|
| Todo/Task 追踪 | `todowrite` 工具 → SQLite TodoTable | 无（系统 prompt 中隐含） | Session Memory 笔记 | 无独立机制 |
| "已读文件"追踪 | FilePart 嵌入消息 | `abs_fnames` 内存 set | Session Memory "Files" 节 | 消息内容中包含 |
| 运行时状态存储 | SQLite（per-project） | 内存（Coder 对象字段） | JSONL + 笔记文件 | Session SQLite（JSON blob） |
| 状态更新方式 | Agent 主动调用工具 | 系统自动维护 | 后台 fork agent | 系统自动 + agent 触发 |
| 反射/重试控制 | `maxSteps` 配置 | `num_reflections` / `reflected_message` | maxTurns 限制 | MAX_TURNS 常量 |

---

## 2. 工作区认知地图

### 2.1 核心问题

Agent 如何"认识"它正在操作的代码库？哪些文件已经读过？文件之间有什么关联？这本质上是 Agent 的"空间感知"问题。

### 2.2 Aider: Chat Files + Repo Map 双层地图

Aider 有最完善的工作区认知体系。

#### 2.2.1 Chat Files — "已读过的文件"

**添加机制**：用户通过 `/add` 命令将文件加入 `abs_fnames`

**`abs_fnames`** 是一个 Python `set`——自动去重，包含所有"已被 Agent 看到内容"的文件绝对路径。

**文件内容注入**（`base_coder.py:789-815`）：
```python
def get_chat_files_messages(self):
    chat_files_messages = []
    if self.abs_fnames:
        files_content = self.gpt_prompts.files_content_prefix
        files_content += self.get_files_content()     # 读取文件内容拼接
        files_reply = self.gpt_prompts.files_content_assistant_reply
    elif self.get_repo_map() and ...:
        files_content = self.gpt_prompts.files_no_full_files_with_repo_map
        files_reply = self.gpt_prompts.files_no_full_files_with_repo_map_reply
    else:
        files_content = self.gpt_prompts.files_no_full_files
        files_reply = "Ok."

    if files_content:
        chat_files_messages += [
            dict(role="user", content=files_content),
            dict(role="assistant", content=files_reply),
        ]
    return chat_files_messages
```

**消息组装顺序**（`format_chat_chunks`, `:1226-1295`）：
```python
chunks.system          # 系统提示词
chunks.examples        # few-shot 示例
chunks.done            # 压缩后的历史
chunks.repo            # repo map
chunks.readonly_files  # 只读文件内容
chunks.chat_files      # ★ 已添加文件的内容
chunks.cur             # 当前轮次消息
chunks.reminder        # 格式提醒
```

**关键设计**：
- chat files 的内容以 `<user>/<assistant>"Ok."` 伪对话形式注入——让 LLM 认为它已经"看到"了文件
- 文件内容在每次 LLM 调用时**重新读取**（`get_files_content()`）——但 SQLite 缓存了 token 计数
- `abs_read_only_fnames` 是只读文件——Agent 可以看到但不能编辑

#### 2.2.2 Repo Map — 代码库的结构化"地图"

Repo Map 是 Aider 最重要的创新之一——它构建一个**符号级代码引用图**并用 PageRank 排序。

**工作流程**：
```
源码文件 → Tree-sitter 解析 → 符号（def + ref）提取
    → PageRank 图（边=引用关系，权重=重要性调整）
    → Token budget 二分查找 → 树状文本输出
```

**PageRank 权重调整**（`repomap.py`）：
- 用户提到的标识符 ×10
- chat 中的文件引用 ×50（大幅强化与当前任务相关的代码）
- 长标识符（≥8 字符且符合命名规范）×10
- 私有符号（`_` 开头）×0.1
- 定义超过 5 次的符号 ×0.1（太通用的符号不重要）

**Token Budget 二分查找**（`repomap.py`）：
```python
middle = min(int(max_map_tokens // 25), num_tags)  # 初始猜测：每个 tag ~25 tokens
while lower_bound <= upper_bound:
    tree = self.to_tree(ranked_tags[:middle])       # 取前 middle 个 tag
    num_tokens = self.token_count(tree)
    if (num_tokens <= max_map_tokens and num_tokens > best_tree_tokens) or pct_err < 0.15:
        best_tree = tree
        if pct_err < 0.15: break                    # 15% 容差内接受
    if num_tokens < max_map_tokens:
        lower_bound = middle + 1
    else:
        upper_bound = middle - 1
```

**缓存策略**：`.aider.tags.cache.v4` 目录，基于 tree-sitter 版本和文件 mtime 做增量缓存。

### 2.3 OpenCode: Context Epoch + FilePart 追踪

#### 2.3.1 Context Epoch — 上下文环境的"检查点"

Context epoch 记录会话的"环境快照"——系统上下文在某个时刻的完整状态。

**SessionContextEpoch 表**（`session/sql.ts:168-176`）：
```sql
CREATE TABLE session_context_epoch (
  session_id TEXT PRIMARY KEY REFERENCES session(id) ON DELETE CASCADE,
  baseline TEXT NOT NULL,
  snapshot TEXT NOT NULL,       -- JSON: SystemContext.Snapshot
  baseline_seq INTEGER NOT NULL
)
```

**初始化与恢复**（`session/context-epoch.ts:40-78`）：
```typescript
const prepareOnce = Effect.fnUntraced(function* (db, events, context, sessionID) {
  const [value, stored, compaction] = yield* Effect.all([context, find(db, sessionID), ...])
  if (!stored) {
    // 首次运行：生成 baseline
    const generation = yield* SystemContext.initialize(value)
    yield* insert(db, sessionID, generation)
    return { baseline: generation.baseline, baselineSeq }
  }
  // 恢复运行：reconcile（检测系统上下文变化）
  const result = replacementSeq
    ? yield* SystemContext.replace(value, snapshot)
    : yield* SystemContext.reconcile(value, snapshot)
  if (result._tag === "Unchanged" || result._tag === "ReplacementBlocked") {
    return { baseline: stored.baseline, baselineSeq: stored.baseline_seq }
  }
  // 上下文有变化 → 更新 epoch 并发布 ContextUpdated 事件
})
```

**设计意图**：
- `baseline` 是文本形式的系统上下文摘要（CLAUDE.md 等规则文件的快照）
- 当规则文件变化时，epoch 检测到不一致，发布 `ContextUpdated` 事件通知 Agent
- 这避免了 Agent 使用过时的规则继续工作

#### 2.3.2 V1 文件追踪：FilePart

在 V1 会话中，"读过文件"以 `FilePart` 嵌入 assistant 消息中：

**Part 类型**（`schema/src/v1/session.ts`）：
- `TextPart`：文本回复
- `ToolPart`：工具调用（含 name, args, state）
- `FilePart`：文件读操作（path, content, source）
- `ReasoningPart`：推理内容
- `PatchPart`：编辑操作
- `CompactionPart`：压缩摘要
- `SnapshotPart`：文件快照

**SessionMessage（V2）** 中也包含 snapshot 引用（`session/message.ts`）：
```typescript
// Assistant 消息带 file snapshot
interface Assistant {
  snapshot?: { start: Snapshot.ID; files?: RelativePath[] }
}
```

### 2.4 Claude Code: CLAUDE.md 层级 + 文件变更追踪

#### 2.4.1 CLAUDE.md 4 级层次

```
/etc/claude-code/CLAUDE.md              # 系统级（企业部署）
~/.claude/CLAUDE.md                     # 用户级
CLAUDE.md / .claude/CLAUDE.md / rules/*.md  # 项目级
CLAUDE.local.md                         # 个人级（gitignored）
```

- 每 turn 重新读取——变更立即生效
- Path-scoped rules 支持目录级别的规则（`.claude/rules/frontend/*.md`）

#### 2.4.2 文件变更快照

每个编辑操作前后都有文件内容 hash：
```
before hash → 编辑 → after hash
```
这使得 revert 时可以精确定位回退目标。

### 2.5 Goose: Session 级别工作目录绑定

Goose 通过 `Session.working_dir` 绑定工作目录，但没有独立的"代码库地图"机制。

```rust
pub struct Session {
    pub working_dir: PathBuf,     // 工作目录
    pub extension_data: ExtensionData,  // 扩展数据（含 MCP 服务器状态）
}
```

MCP 工具通过 `list_tools()` 提供文件系统操作能力，但认知地图完全依赖 LLM 自身从工具输出中构建。

---

## 3. 会话状态 vs 持久状态

### 3.1 核心问题

什么状态应该跨轮次保留？什么应该每轮丢弃？什么应该跨 session 重启保留？

### 3.2 OpenCode: 事件溯源 + SQLite 全持久化

OpenCode 采用**事件溯源（Event Sourcing）**架构——所有状态都从事件流重建。

#### 3.2.1 持久化层次

| 层次 | 存储 | 内容 | 生命周期 |
|------|------|------|----------|
| Session 元数据 | `session` 表 | id, title, agent, model, cost, tokens, revert | session 存活期间 |
| 消息历史 | `session_message` 表 | type + seq + JSON data | session 存活期间 |
| 消息 V1 | `message` + `part` 表 | message + 多个 part | 兼容旧版本 |
| 待处理输入 | `session_input` 表 | prompt + delivery + seq | 处理完即清 |
| Context Epoch | `session_context_epoch` 表 | baseline + snapshot + seq | session 存活期间 |
| Todo | `todo` 表 | content + status + priority + position | session 存活期间 |
| 权限批准 | `permission` 表 | action + resource | 跨 session（按 project_id） |

#### 3.2.2 跨轮次保留

- **消息历史**：全部保留在 SQLite，通过 `seq` 排序
- **Compaction 摘要**：替换旧消息，但保留为 `compaction` 类型消息
- **Todo 列表**：全量替换但跨轮次保留在表中
- **Revert 状态**：`session.revert` JSON 字段，包含快照引用和 diff

#### 3.2.3 每轮丢弃

- LLM streaming 的中间 delta 只存在于内存，不持久化
- `SessionInput.promoted_seq` 处理完后标记为 NULL（不再 pending）

#### 3.2.4 Session Fork

通过 `parent_id` 字段实现：
```sql
parent_id TEXT REFERENCES session(id)
```

Fork 时新 session 插入一条记录，`parent_id` 指向原始 session。这允许：
- 分支探索（实验性修改不污染主会话）
- 会话导航（从子 session 回溯到父 session）

#### 3.2.5 Revert（回退）

**三步流程**（`session/revert.ts`）：

1. **stage**：基于目标 messageID 之后的 snapshot diff 计算还原文件 → 如果文件已被修改则警告
2. **commit**：用户确认后 → 删除目标后的所有消息 → 发布 `RevertEvent.Committed`
3. **clear**：取消 revert 状态，恢复文件到原始状态

```typescript
export const stage = Effect.fn("SessionRevert.stage")(function* (input) {
  const original = input.session.revert?.snapshot
    ? Snapshot.ID.make(input.session.revert.snapshot)
    : yield* snapshot.capture()                       // 先拍快照
  const next = yield* plan({ sessionID, messageID })   // 计算需回退的文件
  const restore = new Map()
  if (input.files !== false)
    for (const [file, tree] of next) restore.set(file, tree)
  yield* snapshot.restore({ files: restore })          // 还原文件
  // 计算 diff 用于 UI 展示
  const files = original
    ? yield* snapshot.diff({ from: original, to: current, paths })
    : []
  return { messageID, snapshot: original, diff, files }
})
```

### 3.3 Aider: Git 即状态 + 内存对话

Aider 的持久化哲学是**"Git 已经是最好的状态管理工具"**。

#### 3.3.1 持久化层次

| 层次 | 存储 | 内容 | 生命周期 |
|------|------|------|----------|
| 文件变更 | Git commits | 每次 auto-commit | 永久（Git 历史） |
| 对话消息 | 内存 `cur_messages` + `done_messages` | 对话内容 | 进程存活期间 |
| 对话摘要 | 内存 + `.aider.chat.history.md` | 可选的文件历史 | 进程存活（除非手动保存） |
| Repo Map 缓存 | `.aider.tags.cache.v4/` | tree-sitter 标签 | 跨 session（mtime 校验） |
| Git undo 栈 | 内存 `commit_before_message` | 每轮开始前的 HEAD hash | 进程存活期间 |

#### 3.3.2 跨轮次保留

- **文件状态**：通过 Git 完全持久化——每个编辑都是一个可追溯的 commit
- **`cur_messages`**：跨轮次累积，包含完整对话历史
- **`done_messages`**：压缩后的摘要，跨轮次保留
- **`aider_edited_files`**：跨轮次累积（`update()` 而非替换）
- **`aider_commit_hashes`**：跨轮次累积，用于 `/undo` 安全检查

#### 3.3.3 每轮重置

通过 `init_before_message()`：
```python
self.aider_edited_files = set()    # 本轮编辑的文件
self.reflected_message = None       # 反射/重试控制
self.num_reflections = 0            # 重试次数
self.lint_outcome = None            # lint 结果
self.test_outcome = None            # 测试结果
self.shell_commands = []            # shell 命令队列
```

#### 3.3.4 Undo 机制

`/undo` 命令（`commands.py:553-655`）：
```python
def raw_cmd_undo(self, args):
    last_commit = self.coder.repo.get_head_commit()
    # 安全检查1：必须是 aider 的 commit
    if last_commit_hash not in self.coder.aider_commit_hashes:
        error("The last commit was not made by aider in this chat session.")
        return
    # 安全检查2：不能有未提交的修改
    for fname in changed_files_last_commit:
        if self.coder.repo.repo.is_dirty(path=fname):
            error(f"The file {fname} has uncommitted changes.")
            return
    # 安全检查3：不能已经 push 到 origin
    if local_head == remote_head:
        error("The last commit has already been pushed.")
        return
    # 执行 revert：逐文件 checkout
    for file_path in changed_files_last_commit:
        self.coder.repo.repo.git.checkout("HEAD~1", file_path)
    # 软重置
    self.coder.repo.repo.git.reset("--soft", "HEAD~1")
```

**关键设计**：
- **git checkout HEAD~1** 而非 `git reset --hard`——逐文件回退，更安全
- **`--soft` reset** 保留 working tree 中的文件状态
- **三重安全检查**：aider commit 验证 + dirty check + push check
- **`aider_commit_hashes`** 作为安全边界——只允许 undo aider 自己的 commit

### 3.4 Claude Code: JSONL 全转录 + Checkpoint

#### 3.4.1 JSONL 会话转录

每行一个 JSON 事件，记录完整的 turn 流水：
```
{"type":"user","content":"...","timestamp":...}
{"type":"assistant","content":"...","tool_calls":[...],"timestamp":...}
{"type":"tool_result","call_id":"...","content":"...","timestamp":...}
```

- 文件路径：`~/.claude/projects/<hash>/<session-id>.jsonl`
- 这是**完整的、不可变的 audit log**

#### 3.4.2 Checkpoint / Snapshot

文件修改前拍快照（hash），修改后比对：
```
edit 操作:
  before: sha256(file_content)
  edit: SEARCH/REPLACE
  after: sha256(file_content)
```

Revert 时直接用 `before` hash 恢复文件。

#### 3.4.3 跨轮次保留

- **完整 JSONL 转录**：永久保留（除非主动清理）
- **CLAUDE.md**：每 turn 重新读取（文件系统的变更立即生效）
- **Auto-memory**：`/memory` 命令写入 `CLAUDE.md` 的特定区块
- **Session Memory 笔记**：作为中间状态被 compaction 消费

### 3.5 Goose: SQLite Session DB + 7 种 Session 类型

#### 3.5.1 持久化架构

```
~/. goose/sessions/
  ├── sessions.db           # 主 SQLite 数据库
  └── exports/              # 导出文件
```

**Session 结构**（`session_manager.rs:61-97`）：
```rust
pub struct Session {
    pub id: String,
    pub working_dir: PathBuf,
    pub name: String,
    pub session_type: SessionType,      // ★ 7 种类型
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub conversation: Option<Conversation>,  // ★ 可选加载
    pub message_count: usize,
    pub usage: Usage,
    pub accumulated_usage: Usage,
    pub parent_session_id: Option<String>,   // ★ fork 来源
    pub project_id: Option<String>,
    pub archived_at: Option<DateTime<Utc>>,
}
```

**Session 类型**：
```rust
pub enum SessionType {
    User,        // 标准用户会话
    Scheduled,   // 定时任务
    SubAgent,    // 子 agent
    Hidden,      // 隐藏会话
    Terminal,    // 终端模式
    Gateway,     // 网关
    Acp,         // Agent Communication Protocol
}
```

#### 3.5.2 跨轮次保留

- **完整 Conversation**：JSON 序列化到 session 行中
- **Usage 累计**：`accumulated_usage` 跨 compaction 保留
- **Extension data**：MCP 服务器的状态/配置
- **Messages 可视性**：`user_visible` / `agent_visible` 标志控制哪些消息对谁可见
- **Compaction 摘要**：旧消息 `agent_visible = false`，新摘要 `agent_only`

#### 3.5.3 每轮丢弃

- LLM streaming chunks 只在内存中
- `tool_result_tx/tool_result_rx` mpsc channel 的内容不持久化
- `steer_queues` 用户中途引导队列不持久化

#### 3.5.4 Session Recovery（恢复）

```rust
pub async fn get_session(&self, id: &str, include_messages: bool) -> Result<Session> {
    self.storage.get_session(id, include_messages).await
}
```

- `include_messages=false`：只加载元数据（列表页面用）
- `include_messages=true`：加载完整 conversation JSON blob
- `fork_session`：通过 `parent_session_id` 链接，复制父 session 的配置和消息

#### 3.5.5 消息截断

```rust
pub async fn truncate_conversation(&self, session_id: &str, timestamp: i64) -> Result<()>
pub async fn truncate_conversation_from_message(&self, session_id: &str, message_id: &str) -> Result<()>
```

支持按时间戳或消息 ID 截断对话——这是 "undo" 的 Goose 版本。

---

## 4. Artifact/产物管理

### 4.1 核心问题

Agent 产生的代码修改、文档、分析结果如何跟踪版本？什么情况下需要保留历史版本？

### 4.2 Aider: Git 作为产物管理基础设施

Aider 把 Git 用到极致——**每个修改都是一次 commit**。

#### 4.2.1 Auto-commit 流程

**`send_message()` 中的完整流程**（`base_coder.py:1585-1623`）：
```python
# 1. 应用编辑
edited = self.apply_updates()
if edited:
    # 2. 记录编辑文件
    self.aider_edited_files.update(edited)
    # 3. 自动提交（包含对话上下文作为 commit message）
    saved_message = self.auto_commit(edited)
    # 4. 将 commit 信息反馈给 LLM
    self.move_back_cur_messages(saved_message)

# 5. Lint 检查 → 如有错误，再做一次 commit（"Ran the linter"）
if edited and self.auto_lint:
    lint_errors = self.lint_edited(edited)
    self.auto_commit(edited, context="Ran the linter")

# 6. 自动测试 → 如有失败，触发反射
if edited and self.auto_test:
    test_errors = self.commands.cmd_test(self.test_cmd)
    if test_errors:
        self.reflected_message = test_errors
```

#### 4.2.2 Commit Message 生成

**`auto_commit()`**（`:2375-2395`）：
```python
def auto_commit(self, edited, context=None):
    if not self.repo or not self.auto_commits or self.dry_run:
        return
    if not context:
        context = self.get_context_from_history(self.cur_messages)
    res = self.repo.commit(fnames=edited, context=context, aider_edits=True, coder=self)
    if res:
        commit_hash, commit_message = res
        # 将 commit hash 和 message 注入 LLM context
        return self.gpt_prompts.files_content_gpt_edits.format(
            hash=commit_hash, message=commit_message,
        )
```

**`get_context_from_history()`**（`:2367-2373`）：
```python
def get_context_from_history(self, history):
    context = ""
    if history:
        for msg in history:
            context += "\n" + msg["role"].upper() + ": " + msg["content"] + "\n"
    return context
```

**关键设计**：
- Commit message 来自最近的对话历史（用户请求 + LLM 回复）
- 这使得每个 commit 都自带语义化的上下文
- `aider_commits` 标记区分 aider 提交和手动提交

#### 4.2.3 Dirty Commit — 保护用户工作

```python
def dirty_commit(self):
    """在 user 自己修改过的文件上做自动提交"""
    if not self.dirty_commits: return
    self.repo.commit(fnames=self.need_commit_before_edits, coder=self)
```

- 在 aider 编辑文件之前，如果文件有用户未提交的修改，先做一个 dirty commit
- 防止 aider 的修改覆盖用户正在做的工作

#### 4.2.4 Undo 即 Git 回退

```python
# 逐文件回退到 HEAD~1
for file_path in changed_files_last_commit:
    self.coder.repo.repo.git.checkout("HEAD~1", file_path)
# 软重置（保留文件状态在 working tree）
self.coder.repo.repo.git.reset("--soft", "HEAD~1")
```

**关键观察**：Aider 不维护自己的产物版本系统——它完全依赖 Git。这让它：
- 天然支持 `git log`/`git diff`/`git show` 查看历史
- 天然支持 `git revert`/`git reset` 回退
- 天然与开发者的 Git workflow 集成

### 4.3 OpenCode: Snapshot 系统 + Revert

#### 4.3.1 Snapshot 机制

OpenCode 使用独立的 snapshot 系统管理文件状态（而非依赖 Git）：

```typescript
// revert.ts
const original = input.session.revert?.snapshot
  ? Snapshot.ID.make(input.session.revert.snapshot)
  : yield* snapshot.capture()  // 文件系统快照

const current = yield* snapshot.capture()
const files = yield* snapshot.diff({
  from: original,
  to: current,
  paths
})
```

**Snapshot 能力**：
- `capture()` — 捕获当前文件系统状态
- `diff({from, to, paths})` — 计算两个快照之间的差异
- `restore({files})` — 恢复文件到指定快照

#### 4.3.2 Revert 的产物追踪

```typescript
export const stage = Effect.fn("SessionRevert.stage")(function* (input) {
  // 1. 计算回退点之后的所有 assistant 消息中的文件变更
  const rows = yield* db.select().from(SessionMessageTable)
    .where(gt(SessionMessageTable.seq, boundary.seq), ...)
  // 2. 从消息中提取 snapshot 引用
  for (const row of rows) {
    if (message.type !== "assistant" || !message.snapshot?.start) continue
    for (const file of message.snapshot.files ?? [])
      files.set(file, Snapshot.ID.make(message.snapshot.start))
  }
  // 3. 执行恢复
  yield* snapshot.restore({ files: restore })
  // 4. 生成 diff 供 UI 展示
  const diff = files.map(f => f.patch).join("").trim()
  return { messageID, snapshot: original, diff, files }
})
```

#### 4.3.3 Compaction 的产物管理

Compaction 也是"产物"——它是对历史对话的摘要产物：

**Compaction 模板**（`session/compaction.ts:16-46`）：
```markdown
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions, assumptions, exact context, or "(none)"]

## Work State
### Completed
- [finished work, verified facts; otherwise "(none)"]
### Active
- [current work, partial changes; otherwise "(none)"]
### Blocked
- [blockers, failing commands; otherwise "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
```

**Compaction 策略**（`:128-159`）：
```typescript
const select = (entries, tokens) => {
  // 从后往前收集消息，直到超过 token 预算
  for (let index = conversation.length - 1; index >= 0; index--) {
    const next = total + Token.estimate(conversation[index])
    if (next > tokens) {
      // 切割消息：head 给 LLM 做摘要，recent 保留在 context 中
      splitPrefix = conversation[index].slice(0, -remaining)
      splitSuffix = conversation[index].slice(-remaining)
      break
    }
    total = next; split = index
  }
  return { head, recent }
}
```

**关键设计**：
- 旧消息的"头"部分送给 LLM 做摘要
- 旧消息的"尾"部分 + 新消息保留在 context 中
- 之前的 compaction 摘要作为"锚点"（anchored summary）传递——新摘要在其基础上更新

### 4.4 Claude Code: Tool Result 回传 + Session Memory

#### 4.4.1 Tool Result 持久化策略

Claude Code 对大 tool result 有磁盘持久化策略：

```python
def maybe_persist_large_result(tool_result, threshold=50000):
    if len(tool_result.content) <= threshold:
        return tool_result       # 小结果直接保留在 context
    filepath = write_to_disk(tool_result.content, tool_result.id)
    preview = tool_result.content[:2000]
    return ToolResult(
        content=f"Output too large ({size}). Saved to: {filepath}\n"
                f"Preview:\n{preview}\n...\n"
    )
```

- Read 工具显式设置 `threshold = Infinity`（豁免持久化）
- 消息级别有 200K chars 聚合预算

#### 4.4.2 Session Memory 作为"活的产物"

Session Memory 笔记是一个**持续更新的产物**——它在整个会话期间被后台 fork agent 增量维护：

```markdown
# Current State
_What is actively being worked on right now?..._

# Worklog
_Step by step, what was attempted, done?..._
```

这个笔记在 compaction 时直接作为摘要使用——它本身就是对工作产物的结构化记录。

### 4.5 Goose: 消息可见性分层 + 导出

#### 4.5.1 消息可见性控制

Goose 使用双层可见性标志：

```
user_visible: bool   # 对用户可见
agent_visible: bool  # 对 Agent（LLM）可见
```

Compaction 后的策略：
- 原始消息：`agent_visible = false`（LLM 不可见，用户仍可见）
- 摘要消息：`agent_visible = true, user_visible = true`

#### 4.5.2 Session 导出

```rust
pub async fn export_session(&self, id: &str) -> Result<String>
pub async fn export_session_markdown(&self, id: &str) -> Result<String> {
    let session = self.get_session(id, true).await?;
    let messages = session.conversation
        .map(|conversation| conversation.user_visible_messages())
        .unwrap_or_default();
    Ok(export_session_to_markdown(messages, &session.name))
}
```

支持 JSON 和 Markdown 两种导出格式——产物可以被外部工具消费。

#### 4.5.3 Session 拷贝

```rust
pub async fn copy_session(&self, session_id: &str, new_name: String) -> Result<Session>
```

完整拷贝一个 session（包括所有消息）——用于分支实验。

---

## 5. 横向对比矩阵

| 维度 | OpenCode | Aider | Claude Code | Goose |
|------|----------|-------|-------------|-------|
| **运行时记忆存储** | SQLite TodoTable | 内存 Coder 字段 | JSONL + 笔记文件 | Session SQLite |
| **TODO 管理** | Agent 主动 `todowrite` 工具 | 系统 prompt 中隐含 | Session Memory 笔记 | 无 |
| **已读文件追踪** | FilePart/Snapshot | `abs_fnames` set | Session Memory "Files" | 消息内容隐式包含 |
| **代码库认知** | Context Epoch | Repo Map (PageRank) | CLAUDE.md 层级 | 无（依赖 MCP 工具） |
| **消息持久化** | SQLite（事件溯源） | 内存（可选文件导出） | JSONL 全转录 | SQLite（JSON blob） |
| **跨轮次保留** | 全部 | 对话 + Git history | 全部 | 全部 |
| **每轮丢弃** | streaming delta | 编辑集合/重试状态 | streaming delta | steering queue |
| **Fork/Branch** | `parent_id` + SQLite | 无原生支持 | Fork agent | `parent_session_id` |
| **回退/Undo** | Snapshot-based Revert | Git checkout + reset | Checkpoint hash | Truncate conversation |
| **产物版本追踪** | Snapshot diff | Git commit history | Before/after hash | 消息可见性 |
| **Compaction** | LLM 结构化摘要 | LLM 摘要（分层递归） | 5层渐进（Session Memory） | 渐进式减量（0%→100%） |
| **会话恢复** | SQLite 加载 + epoch reconcile | 不支持（进程重启=丢失） | JSONL 回放 | SQLite 加载 |

---

## 6. 对 Jeeves 的启示

### 6.1 核心建议

1. **Todo 管理借鉴 OpenCode**：提供 `todowrite` 工具让 Agent 管理自己的任务列表，持久化到数据库。Agent 自主决定何时更新，系统只提供读写接口。

2. **代码库地图借鉴 Aider RepoMap**：实现符号级代码引用图 + PageRank 排序，用 token budget 二分查找控制输出大小。这是提升 Agent"空间感知"能力的最有效手段。

3. **产物管理借鉴 Aider Git 集成**：每个编辑自动 commit，commit message 从对话历史自动生成。Git 是最可靠的版本管理系统，不应重新发明。

4. **会话持久化借鉴 Goose**：SQLite 存储 Session 元数据 + Conversation JSON blob，支持 fork/copy/export/truncate 全生命周期操作。

5. **回退机制借鉴 OpenCode Revert**：Snapshot-based 而非纯 Git-based——适用于非 Git 项目或需要在回退时保留用户中间修改的场景。

6. **Compaction 借鉴 Claude Code Session Memory**：后台维护结构化笔记，平摊摘要成本。这是最创新的设计——避免一次性大量 LLM 调用做 compaction。

### 6.2 最小可行状态模型建议

```python
# Jeeves 最小可行状态模型
class SessionState:
    """跨轮次持久状态"""
    session_id: str
    messages: list[Message]          # 完整对话历史
    todo_items: list[TodoItem]       # Agent 的待办列表
    edited_files: set[str]           # 已编辑的文件
    context_epoch: ContextEpoch      # 环境快照（规则文件 hash）
    usage: TokenUsage                # token 累计

class TurnState:
    """每轮临时状态"""
    pending_tool_calls: list         # 待执行的工具调用
    reflection_count: int            # 重试次数
    lint_outcome: bool | None        # lint 结果
    streaming_delta: str             # 流式增量

class ArtifactState:
    """产物状态"""
    file_snapshots: dict[str, str]   # 文件 hash → 内容
    compaction_summary: str | None   # 压缩摘要
```

### 6.3 优先级排序

| 优先级 | 功能 | 借鉴来源 | 理由 |
|--------|------|----------|------|
| 🔴 P0 | 消息持久化（SQLite/SQLAlchemy） | Goose/OpenCode | 基础能力，没有它无法恢复会话 |
| 🔴 P0 | Git auto-commit | Aider | 产物管理的最佳实践 |
| 🟡 P1 | Todo/Task 追踪 | OpenCode | 提升多步任务的可靠性 |
| 🟡 P1 | Compaction | OpenCode/Goose | 长会话必须 |
| 🟢 P2 | Repo Map | Aider | 大幅提升代码理解质量 |
| 🟢 P2 | Session Fork | OpenCode/Goose | 分支探索能力 |
| 🟢 P2 | Snapshot-based Revert | OpenCode | 安全的回退机制 |
| ⚪ P3 | Session Memory 后台笔记 | Claude Code | 创新但实现复杂 |
