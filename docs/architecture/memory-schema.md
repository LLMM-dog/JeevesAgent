# 记忆类型 Schema

记忆类型用 YAML 定义。一个 YAML 文件驱动四件事：

1. 发给 LLM 的 JSON Schema（告诉它输出什么字段、什么形状）
2. 文件路径（`directory` + `filename_template`）
3. 写入时的字段合并策略（每个字段的 `merge_op`）
4. 最终文件内容（`content_template`）

内置定义在 `backend/app/modules/memory/schemas/*.yaml`，用户覆盖放 `config/memory/*.yaml`。见 [memory.md](memory.md#记忆类型可自定义)。

## 顶层字段

| 字段 | 必填 | 默认 | 说明 |
| --- | :---: | --- | --- |
| `memory_type` | ✅ | — | 类型名。同时是注册表的 key 与覆盖时的匹配依据 |
| `scope` | ✅ | — | `global` / `agent` / `session`。决定文件落在哪一层，见 [memory.md](memory.md#三个层次的隔离) |
| `description` | ✅ | — | 给 LLM 的提取指导。**这是提示词，不是给人看的注释**，写清「什么该记、什么不该记」 |
| `directory` | ✅ | — | 相对 scope 根的目录。空串表示 scope 根本身 |
| `filename_template` | ✅ | — | 文件名模板。含 `{{ }}` 表示多文件类型，不含表示单文件类型 |
| `fields` | ✅ | — | 字段定义列表，见下节 |
| `content_template` |  | 空 | 正文模板（Jinja2）。为空时正文取 `content` 字段的值 |
| `enabled` |  | `true` | `false` 时完全不参与提取与召回 |
| `operation_mode` |  | `upsert` | `upsert` / `add_only` / `update_only` |
| `peer_enabled` |  | `true` | 是否在 peer 目录下也存一份。`scope: session` 的类型该设 `false` |
| `embedding_template` |  | 空 | 向量化文本模板。为空时用正文截断 |
| `overview_template` |  | 空 | 目录级 `.overview.md` 的模板。为空时不生成 |
| `single_file` |  | 自动推导 | 由 `filename_template` 是否含 `{{ }}` 推导，一般不手写 |

### scope 是新增的，不是 OpenViking 的字段

OpenViking 没有 `scope`，它用 `directory` 里的 `{{ user_space }}` 占位符表达隔离（`profile.yaml:17` 的 `viking://user/{{ user_space }}/memories`），由 `MemoryIsolationHandler.render_schema_directories()` 在运行时填入。

Jeeves 改成显式的 `scope` 枚举，理由：

1. **占位符表达不了三层。** `{{ user_space }}` 只有一个变量位，而 Jeeves 需要区分 global / agent / session 三种根，其中 session 只需要 session_id 一个变量（会话记忆平级于 agents，不按智能体隔离）。继续用占位符会变成 `{{ session_root }}/{{ session_id }}/events`，那等于把路径规则写进每个 YAML——改一次目录结构要改所有文件。
2. **越界检查需要一个可判定的声明。** 有了 `scope` 字段，`layout.py` 才能断言「`scope: session` 的类型在没有 session_id 的 MemoryScope 下不可写」。靠解析 `directory` 字符串里有没有 `sessions` 是脆的。
3. **`directory` 变成纯相对路径**，YAML 里不出现任何绝对前缀。

### operation_mode

| 值 | 行为 | 用在 |
| --- | --- | --- |
| `upsert` | 已存在则合并，不存在则新建 | 绝大多数类型 |
| `add_only` | 只新建。目标已存在时改名（追加 `_2`）而不是合并 | `events` / `trajectories` |
| `update_only` | 只更新已存在的。目标不存在时跳过 | 暂无内置类型使用 |

`add_only` 的一个连带效果（guide 里点明，`memory-system-guide.md:236`）：**提取阶段的 prefetch 会跳过 `add_only` 类型**。既然只增不改，回顾已有事件没有意义，只是白烧 token。

### peer_enabled

`true`（默认）时，这个类型在 peer 目录下也会有一份。对应 OpenViking 的 `render_schema_directories()`（`memory_isolation_handler.py:146`）。

`scope: session` 的类型应该设 `false`：会话是「我和用户的这次对话」，不存在「A 眼中 B 的会话事件」这种东西。

## 字段定义

```yaml
fields:
  - name: content
    type: string
    merge_op: patch
    description: |
      用户画像正文。Markdown 格式，只用 H1 + 无序列表。
    init_value: ""
    system: false
```

| 属性 | 必填 | 默认 | 说明 |
| --- | :---: | --- | --- |
| `name` | ✅ | — | 字段名。同时是 LLM 输出 JSON 的 key 与 frontmatter 的 key |
| `type` | | `string` | `string` / `int` / `float` / `bool` |
| `merge_op` | | `replace` | `immutable` / `replace` / `patch` / `sum` |
| `description` | ✅ | — | 给 LLM 的字段说明。**是提示词** |
| `init_value` | | 无 | 初始值。只对单文件类型有效，`init_agent()` 时用它建骨架文件 |
| `system` | | `false` | `true` 时不出现在 LLM 的 JSON Schema 里，由代码填充 |

默认 `merge_op` 取 `replace` 而非 OpenViking 的 `patch`（`memory_type_registry.py:201`）：patch 要求 LLM 输出 SEARCH/REPLACE 结构，对一个短标量字段（`goal` / `outcome`）来说是过重的契约，忘写 `merge_op` 时静默变成 patch 会让模型困惑。replace 是更安全的默认。

### system 字段

`system: true` 的字段由代码填，不给 LLM 看。典型是 `events` 的 `chat_log`——它的值是从 `ranges` 指向的原始消息提取的对话原文。

**让 LLM 填 chat_log 是错的**：它会「凭记忆」重写对话，而重写过的对话就不再是证据了。所以 `ranges`（消息索引范围）由 LLM 输出，`chat_log` 由系统按 ranges 从消息表取原文。这个分工照抄 OpenViking（`events.yaml` 的 `content_template` 调 `extract_context.get_event_content(ranges, ...)`）。

### merge_op 决定 LLM 的输出形状

不是手写在 prompt 里，是从 `(type, merge_op)` 算出来的（对应 OpenViking `merge_op/patch.py:27` `get_output_schema_type`）：

| type | merge_op | LLM 输出 |
| --- | --- | --- |
| string | `patch` | `{"blocks": [{"search": "...", "replace": "..."}]}` |
| string | `replace` / `immutable` | 裸字符串 |
| int / float | `sum` / `replace` | 裸数字 |
| bool | 任意 | 裸布尔 |

`patch` 的完整语义见 [memory.md](memory.md#字段合并patch-是-searchreplace不是追加)。

## 模板

三个模板（`content_template` / `embedding_template` / `overview_template`）都是 Jinja2。

### 可用变量

**所有模板**都能拿到：

- 该记忆的全部字段值（按字段名）
- 系统字段：`version` / `created_at` / `updated_at` / `memory_type`

**`filename_template` / `directory`** 额外能调 `extract_context` 的方法（提取阶段才有）：

| 方法 | 返回 |
| --- | --- |
| `extract_context.get_year(ranges)` | 该消息范围首条消息的年份，如 `2026` |
| `extract_context.get_month(ranges)` | 月份，两位补零 |
| `extract_context.get_day(ranges)` | 日，两位补零 |

所以 `events` 的路径是：

```yaml
directory: "events"
filename_template: "{{ extract_context.get_year(ranges) }}/{{ extract_context.get_month(ranges) }}/{{ extract_context.get_day(ranges) }}/{{ event_name }}.md"
```

**注意**：`filename_template` 里可以带 `/`，它参与目录分层。这是 OpenViking 的做法（`events.yaml:90`），比在 `directory` 里塞变量更清楚——`directory` 是「这类记忆放哪」，`filename_template` 是「这一条放哪」。

**`overview_template`** 额外拿到：

- `items`：目录下所有记忆项，每项有 `file_name` 与 `file_content`（字段字典）
- `directory_name`：当前目录名

```yaml
overview_template: |-
  # 事件索引
  {% for item in items %}
  - [{{ item.file_content.summary|default(item.file_name, true) }}](./{{ item.file_name }})
  {% endfor %}
```

### 模板校验在加载时做

`filename_template` 与 `directory` 里引用的变量必须在 `fields` 里声明（`extract_context` 与系统字段除外），否则加载时就报 diagnostic 并跳过这个类型。

对应 OpenViking 的 `validate_uri_template`（`utils/uri.py:87`）。不校验的后果是渲染时才炸，而那时 Jinja 的错误信息指向模板内部，看不出是哪个 YAML 写错了。

### 渲染失败不能丢内容

`content_template` 渲染抛异常时，回落到「正文 = `content` 字段的原值」而不是写一个空文件。OpenViking 在 `memory_file_utils.py:93` 这么做并记 exception 日志。

理由：模板是格式，内容是信息。格式坏了应该保住信息。

## 内置记忆类型

### global 域

#### profile — 用户画像

**唯一的全局记忆。** 所有智能体共享一份。

| | |
| --- | --- |
| 路径 | `global/profile.md` |
| 单文件 | ✅ |
| operation_mode | `upsert` |
| 关键字段 | `content`（`patch`） |

只记**稳定的**个人属性：职业、技术栈、沟通风格、工作习惯。不记事件、不记临时状态。

可变状态必须带时间戳（照抄 OpenViking `profile.yaml:28` 的规则）：

```markdown
# LLMM-dog
- 职业：个人开发者
- 主力技术栈：Python（FastAPI / SQLAlchemy）
- 代码风格：ruff，行长 120（截至 2026-08）
```

「截至」标记的作用：半年后模型看到这条时知道它可能过时，而不是当成永恒事实。

### agent 域

#### soul — 我的性格

| | |
| --- | --- |
| 路径 | `agents/<agent_id>/soul.md` |
| 单文件 | ✅ |
| peer_enabled | `false` |
| 关键字段 | `content`（`patch`） |

这个智能体的行为倾向、语气、偏好。`peer_enabled: false` 因为「A 眼中 B 的性格」应该走 peer 域的 `identity`，不是 `soul`——`soul` 是自述。

#### identity — 我是谁

| | |
| --- | --- |
| 路径 | `agents/<agent_id>/identity.md`（或 `peers/<peer>/identity.md`） |
| 单文件 | ✅ |
| peer_enabled | `true` |
| 关键字段 | `content`（`patch`） |

角色定位、职责边界、擅长与不擅长。在 peer 目录下时表示「A 认为 B 是什么角色」。

#### preferences — 用户偏好（该智能体视角）

| | |
| --- | --- |
| 路径 | `agents/<agent_id>/preferences/<topic>.md` |
| operation_mode | `upsert` |
| 关键字段 | `topic`（`immutable`）/ `content`（`patch`） |

按主题分文件。**放在 agent 域而非 global**：不同智能体观察到的偏好不同且都对——代码审查员看到「他不接受没有测试的 PR」，教学助手看到「他喜欢先看例子再看原理」。硬要合并成一份全局偏好会互相冲突。

#### experiences — 可复用经验

| | |
| --- | --- |
| 路径 | `agents/<agent_id>/experiences/<name>.md` |
| operation_mode | `upsert` |
| 关键字段 | `experience_name`（`immutable`）/ `content`（`replace`）/ `supersedes`（`replace`） |

三段式结构，照抄 OpenViking（`experiences.yaml:26-46`），因为那套约束是针对「输出要直接注入系统提示词」调过的：

```markdown
## Situation
- 什么情况下这条规则适用（入口条件）

## Approach
- 要做什么。命令式，用 IF/THEN 表达分支

## Reflect
- 绝不做什么。负向约束、边界条件
```

三条关键约束：

- **互斥**：Approach 只放正向步骤，Reflect 只放负向约束，不重复
- **抽象**：去掉具体 ID、人名、原文，让规则可泛用
- **原子**：一条经验只覆盖一个意图。Approach 超过 8 条就该拆

`content` 用 `replace` 而非 `patch`：三段式结构重写比打补丁可靠——经验是要被整体重新审视的，不是增量累积的。这一点与 OpenViking 一致（`experiences.yaml:48`）。

`supersedes` 字段：新经验取代一条更窄的旧经验时填旧的 `experience_name`，系统删旧的并继承其轨迹历史。

#### trajectories — 执行轨迹

| | |
| --- | --- |
| 路径 | `agents/<agent_id>/trajectories/<name>.md` |
| operation_mode | `add_only` |
| 关键字段 | `trajectory_name`（`immutable`）/ `content`（`replace`） |

「这次是怎么做的」的原始记录。`add_only` 因为轨迹是历史，不该被改写。

轨迹是 `experiences` 的原料：`trajectories`（做了什么）→ `experiences`（怎么做好）。提取阶段对每条新轨迹单独提炼一次经验。

#### tool_notes — 工具使用心得

| | |
| --- | --- |
| 路径 | `agents/<agent_id>/tool_notes/<tool_name>.md` |
| 关键字段 | `tool_name`（`immutable`）/ `content`（`patch`）/ `total_calls`（`sum`）/ `fail_count`（`sum`） |

工具是不可变池，用户只能勾选每个智能体启用哪些（见 [memory.md](memory.md#工具与技能不可变工具池-vs-可写技能)）。心得按工具名分文件。

`sum` 字段是计数器的示范用法：`total_calls` / `fail_count` 累加，用来算失败率。

#### skill_notes — 技能使用心得

同 `tool_notes`，按技能名分文件。

技能的可见性规则与工具不同（全池可见 + 按智能体覆盖 + 新建默认对作者可见），见 [memory.md](memory.md#技能全池可见--按智能体可写覆盖--新建默认可见)。

### session 域

#### events — 原子事件

| | |
| --- | --- |
| 路径 | `sessions/<sid>/events/<YYYY>/<MM>/<DD>/<name>.md` |
| operation_mode | `add_only` |
| peer_enabled | `false` |
| 关键字段 | `event_name`（`immutable`）/ `goal` / `summary` / `outcome` / `ranges`（`immutable`）/ `chat_log`（system） |

**原子性是这个类型的全部要求。** OpenViking 的 `events.yaml` 用了 88 行 description 来讲这一件事，正反例各三组，因为 LLM 的默认倾向是「把一段对话总结成一个事件」，而那样的事件无法被检索——它什么都沾一点。

内置定义保留那套正反例（翻成中文）。核心规则：

- 目标、时间锚点、决策、责任人、结果——**任一项变化就是新事件**
- 名字不能以 `_chat` / `_talk` / `_discussion` 结尾，不能是「团队安排」这类大伞
- 名字里不带日期和数字（日期在路径里）

`ranges` + `chat_log` 的分工见上文 [system 字段](#system-字段)。

#### entities — 实体卡片

| | |
| --- | --- |
| 路径 | `sessions/<sid>/entities/<category>/<name>.md` |
| operation_mode | `upsert` |
| peer_enabled | `false` |
| 关键字段 | `category`（`immutable`）/ `name`（`immutable`）/ `content`（`patch`） |

对话里提到的人、组织、项目、产品、概念。结构（照抄 OpenViking `entities.yaml:42`）：一个 H1 显示名 + 一句平实描述，然后 2-4 个 H2 小节，每个 H2 下必须是无序列表。

**为什么限定结构**：实体卡片会被整段注入提示词，段落式的正文在预算紧张时无法「只取前几条」降级，列表可以。

`category` 用 `immutable`：实体的类别定了就不该变。改类别等于换文件路径，那是删旧建新而不是更新。

## 加一个记忆类型

以「用户的项目清单」为例，假设想让智能体记住用户有哪些项目在跑。

`config/memory/projects.yaml`：

```yaml
memory_type: projects
scope: agent
description: |
  用户正在进行的项目。每个项目一个文件。

  什么该记：项目的目标、技术栈、当前阶段、已知阻塞。
  什么不该记：某次具体的代码改动（那是 events）、一次性的问题排查。

directory: "projects"
filename_template: "{{ project_name }}.md"
operation_mode: upsert

content_template: |
  # {{ project_name }}

  {{ content }}

  {% if blocked_on %}
  ## 当前阻塞
  {{ blocked_on }}
  {% endif %}

fields:
  - name: project_name
    type: string
    merge_op: immutable
    description: 项目名。用仓库目录名，小写。

  - name: content
    type: string
    merge_op: patch
    description: |
      项目概况。Markdown 无序列表，包含目标、技术栈、当前阶段。

  - name: blocked_on
    type: string
    merge_op: replace
    description: 当前阻塞的事。没有阻塞时留空。

overview_template: |-
  # 项目清单
  {% for item in items %}
  - [{{ item.file_content.project_name }}](./{{ item.file_name }})
  {% endfor %}
```

放进去就生效，不重启（registry 提供 `reload()`，与 skill / agent spec 的做法一致）。

三个容易踩的点：

1. **`filename_template` 里的 `project_name` 必须在 `fields` 里声明**，否则加载时被拒。
2. **`merge_op: patch` 的字段，LLM 要输出 SEARCH/REPLACE 块**。如果这个字段的内容总是整体重写（像 `experiences.content`），用 `replace` 更省事。
3. **`scope: session` 的类型要显式 `peer_enabled: false`**，否则会在 peer 目录下建出无意义的会话目录。
