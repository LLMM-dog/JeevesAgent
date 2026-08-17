# 记忆系统

Jeeves 的记忆系统。设计上对照 OpenViking 的实现（`openviking/session/memory/`），采纳它的「YAML 定义记忆类型 + Markdown 文件存储 + 字段级 merge + SEARCH/REPLACE patch」这套核心，但**隔离模型完全不同**：OpenViking 是「单智能体 + 多 peer 用户」，Jeeves 是「单用户 + 多智能体」。

本文档是设计真源。代码与本文冲突时先改文档。

| 文档 | 内容 |
| --- | --- |
| 本文 | 分层与隔离、存储形态、merge 语义、接口契约 |
| [memory-schema.md](memory-schema.md) | 记忆类型 YAML 的完整字段说明 + 内置类型清单 |

提取（commit/extract）、召回（recall/search）、归档（archive）均已实现。归档水位线把已提取的消息移出上下文，用 `.overview.md` 摘要替代。

## 三个层次的隔离

记忆有三种作用域。**这是整个系统最重要的一个决定**，其它设计都从它推导。

| 作用域 | 谁能看到 | 内置类型 | 目录 |
| --- | --- | --- | --- |
| `global` | 所有智能体、所有会话 | `profile`（用户画像） | `data/memory/global/` |
| `agent` | 单个智能体的全部会话 | `soul` / `identity` / `preferences` / `experiences` / `trajectories` / `tool_notes` / `skill_notes` | `data/memory/agents/<agent_id>/` |
| `session` | 单个会话内（属于会话本身，不按智能体隔离） | `events` / `entities` | `data/memory/sessions/<session_id>/` |

### 为什么用户画像是全局的，其它都不是

用户只有一个。「LLMM-dog 是个人开发者、用 Python、讨厌过度设计」这件事对每个智能体都成立。让每个智能体各自积累一份等于重复劳动，而且会各自跑偏——A 学到「他喜欢简洁」，B 学到「他喜欢完整」，两份都有出处但对不上。

**唯一的全局可写记忆是 `profile`。** 这个限制是刻意的：全局记忆是所有智能体的共享可写状态，多一种就多一处「A 的错误结论污染 B」的风险。

反过来，`soul`（我的性格）和 `identity`（我是谁）必须是智能体级——那正是「不同智能体」这个概念的全部内容。一个严厉的代码审查员和一个耐心的教学助手，它们对同一个用户的观察不同，也应该不同。

> OpenViking 把 `profile` / `soul` / `identity` 都放在 `user/{user_space}/memories/` 下同一层（见 `profile.yaml:17`），因为它只有一个 agent，「用户的」和「我的」不需要分。Jeeves 必须分。

### 为什么事件和实体只在会话内生效

`events`（这次做了什么）和 `entities`（这次谈到的人/物/项目）是**对话的痕迹**，不是知识。

一次会话里「决定用 ruff 替换 flake8」是事件；这个决定值得长期记住的部分（「他偏好单一工具链」）应该由提取流程升格成 `preferences`，而不是让原始事件跨会话漂移。

不做会话隔离的后果很具体：会话 A 里的实体卡片「张三 = 后端同事」会在会话 B（完全无关的话题）里被召回并注入提示词，模型于是开始提张三。这不是记性好，是串台。

**升格是提取阶段的职责**，不是存储层的。存储层只负责让 `session` 域的记忆物理上不可能被别的会话读到。

## 智能体之间的相互认知（peer）

OpenViking 的 peer 是「我观察到的另一个用户」。它的 peer 有个容易忽略的双重含义（guide 里点明了，`memory_isolation_handler.py:29` 的 `peer_user_space` 印证）：

```
写入时：peer 是「被观察的对象」——Alice 观察 Bob，写 alice/peers/bob/
召回时：peer 是「观察者视角」——以 Bob 的视角搜 bob/memories/
```

Jeeves 没有多用户，但**未来会有多智能体协作**，那时「A 对 B 的认知」是真实需求：A 委派任务给 B，A 需要记住「B 擅长读代码但爱漏边界情况」。

所以 peer 在 Jeeves 里被重新定义为：**智能体 A 视角下的智能体 B**，并且**只保留「被观察对象」这一个含义**。

```
data/memory/agents/<agent_id>/peers/<peer_agent_id>/
```

两条规则：

1. **归属明确**：`agents/A/peers/B/` 是「**A 认为的** B」，不是 B 的自述。B 读不到它——那会变成「你的同事觉得你不行」这种破坏性反馈。
2. **和 agent 域同构**：peer 目录下用同一批 schema（`peer_enabled: true` 的那些），不为 peer 单独发明类型。这一点照抄 OpenViking（`memory_isolation_handler.py:146` `render_schema_directories`）。

只保留一个含义的理由：OpenViking 那个双重语义在 `calculate_memory_uris` 里制造了 60 行分支（`memory_isolation_handler.py:196-260`），要同时处理 `ranges` 推导目标、`peer_id` 字段显式指定、以及「消息是谁说的」三条来源。Jeeves 的记忆归属由调用方的 `MemoryScope` 直接决定，不从消息内容反推。

当前只有单智能体在跑，peer 目录不会被创建。接口层完整支持它，代价只是 `MemoryScope` 多一个字段——比之后再加便宜得多。

## 存储形态：消息进 SQL，记忆进文件

这个选择被反复问到，结论是**两者都要，各管一段**。

| | 消息（message 表） | 记忆（Markdown 文件） |
| --- | --- | --- |
| 存哪 | SQLite | `data/memory/**/*.md` |
| 形态 | 原始、逐条、不可变 | 提炼、结构化、持续合并 |
| 主要操作 | 按 `(session_id, seq)` 顺序读 | 语义搜、字段级 merge |
| 变更频率 | 只追加 | 反复重写同一文件 |
| 谁读 | 组装上下文的代码 | 模型（召回或直接读文件） |

### 消息为什么不搬去文件

OpenViking 把消息存成 `messages.jsonl`，因为它自带一个 git 版控文件系统（VikingFS）——文件就是它的数据库。Jeeves 没有那个，SQLite 已经在跑，而且消息表承担了三件文件干不了的事：

- `Index("ix_message_seq", session_id, seq, unique=True)` 保证顺序唯一。jsonl 靠行号，并发追加会错。
- `role=artifact` 的部分唯一索引（每个 `(session, agent)` 只留最新一版，见 `session/models.py:133`）。文件系统要自己实现。
- `ON DELETE CASCADE`。删会话时消息自动清掉，不留孤儿文件。

把消息搬进文件是纯亏损。**记忆不进 SQL 的理由则相反**：

1. **字段级 merge 需要结构，一个 `content` 列装不下。** 「经验」有 Situation/Approach/Reflect 三段，各段合并策略不同。要在 SQL 里做就得建一堆列或塞 JSON，塞 JSON 等于把文件塞进数据库。
2. **记忆要能被人直接读和改。** 个人项目，我会去 `data/memory/agents/xxx/soul.md` 看它对我的印象跑偏成什么样，顺手改掉。`sqlite3` 打开一个 blob 列做不到。
3. **git 可版控。** 记忆目录能 diff、能回滚。「上周它还知道我用 uv，怎么忘了」这种问题只有 diff 能答。
4. **模型能用现成工具读。** `read_file` / `grep` 已经存在且模型会用。记忆是 Markdown 意味着**不必为「让模型看自己的记忆」新造一套工具**。

### 但索引进 SQL

文件适合存内容，不适合查。所以有一张薄索引表 `memory_index`，只存元数据不存正文：

```
uri（主键）| scope | agent_id | session_id | peer_agent_id | memory_type
title | version | active_count | created_at | updated_at | content_hash
embedding_model | embedding_dim
```

职责就四件：

- **列举与筛选**（「这个智能体有哪些经验」）不必 rglob 整个目录再逐个解 frontmatter
- **热度统计**（`active_count` 每次召回命中 +1）需要频繁小写入。OpenViking 把它存在向量库记录里（`vec_index.py` 的 `VectorRecord.active_count`）；写进 Markdown frontmatter 会让 git diff 全是计数器噪音
- **删会话时**按 `session_id` 找出该清理的文件
- **嵌入模型漂移检测**：`embedding_model` / `embedding_dim` 与当前绑定不一致 → 标记需重建，而不是静默算出错误的相似度

索引可以随时从文件重建（`rebuild_index()`）。**文件是真源，索引是缓存**——这条不能反，冲突时永远相信文件。

## 目录结构

```
data/memory/
├── global/
│   └── profile.md                       # 唯一的全局记忆
│
├── agents/
│   └── <agent_id>/
│       ├── soul.md                      # 我的性格（单文件）
│       ├── identity.md                  # 我是谁（单文件）
│       ├── preferences/<topic>.md
│       ├── experiences/<name>.md
│       ├── trajectories/<name>.md
│       ├── tool_notes/<tool_name>.md
│       ├── skill_notes/<skill_name>.md
│       │
│       ├── peers/<peer_agent_id>/       # 多智能体阶段才会出现
│       │   ├── identity.md
│       │   └── experiences/<name>.md
│       │
│       └── sessions/<session_id>/
│           ├── events/<YYYY>/<MM>/<DD>/<event_name>.md
│           └── entities/<category>/<name>.md
│
└── .index/                              # 可重建的缓存，不进 git
```

每个记忆目录下可以有一个 `.overview.md`（目录索引，由系统按 `overview_template` 生成）。以 `.` 开头，因此不会被当成记忆项。

`events` 按日期分层照抄 OpenViking（`events.yaml:90`）：跨会话看同一个智能体的事件时日期是最自然的浏览维度，而且「清理三个月前的事件」变成删目录。`entities` 按类别（`people` / `project` / `product`）分层，理由同上——查「有哪些人」比查「有哪些实体」常见。

## 记忆项的文件格式

YAML frontmatter 存字段，正文由 `content_template` 渲染。

```markdown
---
memory_type: experiences
title: pytest_asyncio 取消挂起的修法
scope: agent
agent_id: adf_7bK2mQ9xR4Lp
version: 3
created_at: 1786432100000
updated_at: 1786694312000
source_extraction_id: ext_a1b2c3
last_update_trace_id: trc_9f8e7d
tags: [pytest, asyncio]
---

## Situation
- 测试用 asyncio.wait_for 包了一个自己 catch 了 Exception 的协程

## Approach
- 检查被包裹协程里的 except 子句是否覆盖 CancelledError
- 若覆盖，改成先 `except asyncio.CancelledError: raise`

## Reflect
- 绝不用裸 except 或 except BaseException 包 await
```

frontmatter 里的字段分两类：

- **schema 声明的业务字段**（`title` / `tags` / 各类型自己的字段）：由提取流程产出，按 `merge_op` 合并
- **系统字段**（`memory_type` / `scope` / `agent_id` / `session_id` / `version` / `created_at` / `updated_at` / `source_extraction_id` / `last_update_trace_id`）：由存储层写入，LLM 不许碰

**系统字段必须显式保留。** 更新一个记忆时，旧文件里 schema 没声明的字段要拷回新 metadata，否则每次 update 都静默丢掉它们。OpenViking 在 `memory_updater.py:1101-1105` 专门做了这件事并写了注释说明原因；这是个容易漏的坑，因为丢了不报错，只是历史溯源信息消失。

`source_extraction_id`（一次提取一个）与 `last_update_trace_id`（一次请求）分开存：前者让 diff 能追溯「是哪次提取改的」，后者对应 trace 系统。

### 为什么用 frontmatter 而不是 HTML 注释

OpenViking 用 `<!-- MEMORY_FIELDS {...json...} -->` 放在文件**末尾**（`memory_file_utils.py:112`）。改成 YAML frontmatter：

1. **本项目已经在用 frontmatter**（`skills/*/SKILL.md`、`agents/*.md`），有现成的 `parse_frontmatter()` 和相应约定。再引入第二套元数据格式没有收益。
2. **frontmatter 在文件头**。`head -20` 就能看清一个记忆是什么，不用翻到底。
3. **编辑器和 GitHub 原生渲染 frontmatter**，HTML 注释里的 JSON 只是一坨。

代价：YAML 对多行字符串的转义比 JSON 麻烦。但业务字段的长文本本来就应该进正文（由 `content_template` 渲染），frontmatter 里只放标量和短列表。

## 字段合并：patch 是 SEARCH/REPLACE，不是追加

四种 `merge_op`，语义与 OpenViking 一致（`merge_op/base.py:96`）：

| merge_op | 行为 | 用在 |
| --- | --- | --- |
| `immutable` | 首次写入后永不改。已有值时忽略新值 | `event_name` / `category` / `name` |
| `replace` | 全量覆盖。**空值不覆盖** | `summary` / `goal` / `outcome` |
| `patch` | 字符串走 SEARCH/REPLACE 块；非字符串等同 replace | `content`（profile / entities / soul） |
| `sum` | 数值累加 | 计数器 |

**`patch` 的语义是这一节的重点，因为最容易做错。**

Jeeves 现有实现（`merge_op.py:71` `_patch`）是「字符串追加，重复则跳过」。那是个阉割版，有两个致命问题：

1. **只增不减。** `profile.md` 会无限膨胀，而「用户去年用 flake8，今年换 ruff」这种事实修正做不到——旧句子永远留着，模型于是同时看到两个矛盾的事实。
2. **去重靠子串包含。** 稍微改一个字就判定为「新内容」，于是同一件事以七八种措辞并存。

OpenViking 的做法（`merge_op/patch.py`）：LLM 输出 SEARCH/REPLACE 块，系统做定位替换。

```json
{
  "content": {
    "blocks": [
      {"search": "- 代码风格：flake8 + black", "replace": "- 代码风格：ruff（2026-08 换）"}
    ]
  }
}
```

配套的三个细节，缺一个就不能用：

- **`search` 必须是原文里唯一的最小片段**（通常 2-4 行）。整段贴进去会让匹配变脆。
- **无原文时不做匹配。** `current_value is None` 时直接取第一个 block 的 `replace` 当内容（`patch.py:56-58` `_extract_replace_when_no_original`）。漏掉这条，新建文件永远写不进内容——因为 search 匹配不上空串。
- **`search` 为空的块要过滤掉。** 有原文时空 search 是非法的（空串能匹配任意位置），OpenViking 在 `patch.py:70` 显式过滤而不是报错，因为 LLM 偶尔会输出空 search 表示「追加」。

模糊匹配：OpenViking 有一个 48KB 的 `patch_handler.py`，做 Levenshtein 相似度 + 行窗口滑动 + 标记序列校验，容忍 LLM 抄错缩进。**Jeeves 第一版只做精确匹配 + 缩进归一化**（strip 每行尾空白后比对），匹配失败时把失败信息回给 LLM 让它重试一次。理由：模糊匹配的风险是**改错地方且不报错**，而精确匹配失败是显式的、可重试的。个人项目的记忆量下，多一轮重试比静默改错便宜。

留了扩展点：`merge.py` 的 `PatchOp` 接受一个 `matcher` 参数，之后要上模糊匹配只加一个 matcher 实现。

### patch 的输出契约由 (type, merge_op) 推导

LLM 该输出什么形状，不是手写在 prompt 里，而是从字段定义算出来（`merge_op/patch.py:27` `get_output_schema_type`）：

| type | merge_op | LLM 输出 |
| --- | --- | --- |
| string | patch | `{"blocks": [{"search": ..., "replace": ...}]}` |
| string | replace / immutable | 裸字符串 |
| int / float | sum / replace | 裸数字 |

这让「加一个字段」只需改 YAML，不需要动 prompt。实现上是从 registry 动态生成一个 Pydantic model（OpenViking 的 `schema_model_generator.py`），Jeeves 同样做，但生成的是 JSON Schema dict 而非 Pydantic 类——因为本项目的 LLM 调用走 `LLMPort.stream_chat` 传原始 dict，不需要 Pydantic 那层。

## 可自定义：记忆类型、提取模型、嵌入模型

「高度自定义」在本项目的具体含义是这三件事，都不改代码。

### 记忆类型可自定义

内置类型定义在 `backend/app/modules/memory/schemas/*.yaml`（随代码走 git）。用户在 `config/memory/*.yaml` 放自己的定义：

- **同名整体覆盖**内置（不做字段级部分覆盖——那会让「最终生效的定义是什么」难以推断）
- **新名新增**一种记忆类型
- `enabled: false` 关掉一个内置类型

加载顺序与 `agents/*.md` 覆盖 `BUILTIN_SPECS` 的做法一致，用户对这个模式已经熟悉。OpenViking 的 registry 也是这个结构（`memory_type_registry.py:42` `_load_schemas`：内置 → 实验性 → 自定义，后者 `replace=True`）。

**一个坏 YAML 不影响其它。** OpenViking 在 `load_from_directory` 里 catch 每个文件（`memory_type_registry.py:174`），但它的 `_load_schemas` 在整体加载数为 0 时 raise。Jeeves 同样：单文件失败记 diagnostic 跳过，全部失败则启动期报错——因为那说明包装坏了，静默跑起来会让所有记忆写入无声失败。

加载时校验模板（对应 OpenViking 的 `validate_uri_template`，`utils/uri.py:87`）：`filename_template` 里引用的变量必须在 `fields` 里声明，否则渲染时才炸，而那时错误信息指向 Jinja 内部。

### 提取模型与嵌入模型可自定义

走已有的 `model_binding` 表功能位机制，不新造配置：

| 功能位 | 用途 |
| --- | --- |
| `embedding` | 记忆向量化与召回 query 嵌入（已存在，见 `endpoint/service.py:25`） |
| `memory`（新增） | 记忆提取用的模型。未绑定时回落 `compact` 再回落 `chat` |

提取用单独功能位而非复用 `chat`：提取是后台批处理，可以用便宜模型；但它的输出要严格符合 JSON schema，可能需要一个专门擅长这个的模型。让用户能分开选。

嵌入模型已经能自定义（`Embedder` 优先读 `embedding` 绑定，回落本地 sentence-transformers）。**有一个坑要修**：换嵌入模型后维度变化，旧向量全部失效。`memory_index` 记 `embedding_model` 与 `embedding_dim`，不一致时标记需重建而不是继续算相似度。

## 模块内的文件分工

```
backend/app/modules/memory/
├── schema.py         # MemoryTypeSchema / MemoryField / 枚举
├── registry.py       # 类型注册表：内置 + 用户覆盖，懒加载单例
├── models.py         # MemoryScope / MemoryItem / WriteOp 等数据类
├── models_db.py      # memory_index 表
├── layout.py         # scope → 目录路径的唯一换算处
├── merge.py          # merge_op 实现（含 SEARCH/REPLACE matcher）
├── render.py         # frontmatter 读写 + 模板渲染
├── store.py          # MemoryStore Protocol
├── file_store.py     # 文件实现
├── index.py          # memory_index 读写
├── service.py        # 对外唯一入口
└── schemas/*.yaml    # 内置记忆类型
```

单文件 500 行上限照旧。`service.py` 是唯一对外入口——agent loop、路由、提取流程只 import 它，不直接碰 `file_store`。

对比 OpenViking 的 `memory_updater.py` 是 64KB / `streaming_memory_updater.py` 72KB / `extract_loop.py` 38KB。它们那么大是因为把提取编排、并发合并、向量化、链接图、resource 引用同步全塞进了同一层。Jeeves 把提取（LLM 编排）、存储（文件读写）、索引（SQL）分成三个互不 import 的层。

## 提取管线

```
message 表
  → extract_input.prepare    截断：按轮保留、超长截头尾、超预算丢最早
  → prefetch.prefetch        预取已有记忆 + 分配 page_id
  → extract_loop.ExtractLoop ReAct 循环（工具调用 / 三种修复重试）
  → commit._to_write_ops     page_id → 字段
  → service.write_many       合并写入
  → service.write_diff       痕迹落盘
```

### 截断的三条规则

| 规则 | 为什么 |
| --- | --- |
| 末尾 N 轮不参与 | 正在进行的对话总结出来是「他开始做 X」而不是「做完了 X」 |
| 超长消息保留**头尾** | 工具结果的结论在结尾（`3 passed` / `error:`），只留开头会切掉结论 |
| 超预算丢**最早**的 | 较新的内容更可能还没被提取过；最早的很可能上次已提取 |

不复用上下文压缩的 `fit_to_budget`：压缩要「让对话能继续」所以保留最近的，提取要「从已结束的部分学习」所以要的恰好是被压缩丢弃的那部分。方向相反，共用会让其中一个语义被带歪。

### 预取：按三层分别取

**这是我们自己的架构要求，不能照抄 OpenViking。** 它一次提取只涉及一个 `user_space`；我们要同时覆盖三层，各从自己的目录读：

| 层 | 目录 | 例子 |
| --- | --- | --- |
| global | `global/` | profile |
| agent | `agents/<A>/` | preferences、experiences、tool_notes |
| session | `sessions/<S>/` | entities |

`layout.type_dir()` 按每个 schema 自己的 `scope` 解析目录，所以一次 `prefetch(session_scope)` 会分别读到三层。漏掉任一层都会让模型看不到已有记忆而新建重复的。

**渲染时必须标注层级**，因为模型光看「已有的 entities」无法知道两件事：

- session 级的东西下次会话看不到 → 该记进 agent 级的记成了会话级，等于没记
- 预取只含**本会话**的 session 级记忆 → 模型会把「没看到」当成「从没记过」

所以每组标题带上「本智能体，跨会话长期有效」/「仅本次会话，其他会话看不到」/「全局，所有智能体共享」。

### 两种预取模式

对齐 OpenViking 的 `eager_prefetch`（`memory_config.py:58`）：

| 模式 | 预取 | 工具 | 适用 |
| --- | --- | --- | --- |
| eager（默认） | 每类 top-N 的完整正文 | 不给 | 记忆少，省一轮调用 |
| lazy | 只给标题 + page_id 索引 | `list` / `read` / `search` | 记忆多到装不下窗口 |

**eager 也有上限**（`prefetch_topn` 每类 + `prefetch_max_chars` 总量）。原来 eager 不限量，实测一个有 120 条偏好的智能体（用半年就会有）让预取吃掉 13572 token，把对话挤出窗口——而对话才是提取的原料。加上限后同样数据降到 1340 token。

OpenViking 的 eager 也只读搜索结果的 top-N（`session_extract_context_provider.py:571`），从来不是「读全部」。区别是它用向量搜索排序，我们用 `updated_at` 倒序——最近改过的最可能与当前对话相关，而且不必在提取路径上多一次嵌入调用。

超总量时从尾部类型逐条弹出，**不整类丢弃**：某类完全不可见会让模型把它当「从没记过」而新建重复的。丢弃条数记进 `dropped` 和日志——静默丢弃会让「模型为什么没改那条记忆」变成无法排查的问题。

lazy 模式下 `read_uris` 保持为空——那个集合的语义是「模型已看过正文」，而 lazy 只给了标题。填进去会让 refetch 检查失效，模型就能在没读正文的情况下 patch，而那必然匹配失败。

**工具参数用 `page_id` 而非 `uri`**：模型会抄错长路径，抄错的后果是静默读到空内容而不是报错。

### 循环的四条分支

| 分支 | 触发 | 处理 | 上限 |
| --- | --- | --- | --- |
| 工具调用 | 模型返回 tool_calls | 并行执行、拼回消息 | 每次 +1 预算 |
| 格式错误 | 输出不是合法 JSON | 回格式纠正 | 1 次 |
| patch 打不上 | SEARCH 匹配失败 | **回真实原文** | 1 次 |
| refetch | page_id 无效（幻觉） | 提示先读 | 1 次 |

每种情况都 `max_iterations += 1`：那三种不是「模型不听话」，是信息不足，不该占用正常预算（`extract_loop.py:276` 同样这么做）。

三个必须的连带处理：

- **未知工具名 → 下一轮收回工具**。模型持续调不存在的工具会耗尽预算。
- **最后一轮强制关闭工具**，否则它可能一直探索而永不产出结果。
- **patch 预检在写入前**。写入是串行的，第 3 条失败时前 2 条已落盘，那时重试会重复应用。

失败两次就当「没有记忆要写」而非硬失败——提取失败不该让用户已完成的对话回滚。

### supersedes：经验的泛化

经验会逐步变宽。先记「pytest 挂住时查 CancelledError」，后来发现更普适的是「任何 await 被裸 except 包住都会挂」。第二条完全覆盖第一条，但**名字不同**所以 upsert 不会合并。

`supersedes` 字段让新经验声明它取代了谁，提取后删掉旧的。不处理的话两条并存，召回时一条窄一条宽，而窄的那条会误导（让模型以为只有 pytest 场景才需要检查）。

删除有痕迹（`deleted_content` 存全文），需要时能从 diff 找回。自引用和指向不存在的目标都容错——前者忽略（否则这次提取白做），后者只记警告（模型可能记错名字）。

### 向量化

写入后对 `written + edited` 算向量。**只增类型（events / trajectories）同样要算**——预取时跳过它们和向量化跳过它们是两件事：

| | 目的 | add_only 类型 |
| --- | --- | --- |
| 预取 | 让模型改已有记忆而不是新建重复的 | 跳过（不会被改，回顾是浪费） |
| 向量化 | 让记忆以后能被召回 | **不跳过** |

混淆这两件事会让只增类型永远无法被语义召回。OpenViking 同样对全部 written + edited 向量化（`memory_updater.py:1352`），只排除 `.overview.md` / `.abstract.md`。我排除 overview 的理由相同：它是其他记忆标题的拼接，命中一个目录索引对召回毫无价值，而且它会与被它索引的记忆竞争相似度。

向量走 `embedding_template` 而不是正文。trajectories 的模板只用 `retrieval_anchor`——那是「检索文本与执行文本分开」这条设计的落点。

**向量存 float32 BLOB，不进记忆文件。** 1024 维存 JSON 约 12KB、存 BLOB 是 4KB。不进文件是因为它是派生数据、不可读、换模型就失效——写进 frontmatter 会让 git diff 出现 4KB 乱码。

不引 sqlite-vss：需要编译安装，破坏「clone 下来能跑」。几千条记忆用 Python 算余弦足够（实测 3000 条 1024 维约 25ms，而一次 LLM 调用是几十秒）。

### 搜索范围：按我们的三层隔离

**这里和 OpenViking 不同。** 它的目录是 `viking://user/{{ user_space }}/memories/...`，隔离维度是用户和 peer（多租户 SaaS），搜索时按 `user_space` 拼路径。Jeeves 是 global / agent / session 三层，所以按 scope 筛索引行：

| 查询 scope | 可见范围 |
| --- | --- |
| 给了 session_id | global + 该 agent + 该 session |
| 只给 agent_id | global + 该 agent（**不含任何会话**） |
| 都不给 | 只有 global |

会话级记忆对其他会话不可见——否则 A 会话的临时上下文会污染 B。

**peer 视角也参与筛选**：`agents/A/peers/B/` 是「A 眼中的 B」，与 A 自己的记忆是两套东西。不按 `peer_agent_id` 筛的话两个方向都错——普通查询会把「A 眼中的 B」混进 A 自己的记忆，peer 视角查询会拿到 A 自己的记忆。peer 目前不会被创建，但筛选条件先做对，因为等它被用起来时这类污染极难发现（结果「看起来合理」）。

### 嵌入模型可切换

用户随时能换嵌入模型。维度一变旧向量全部失效，处理方式是：

1. **旧向量立即停止参与召回**（`search` 里按 `embedding_model` 筛掉）
2. **不自动重算**，由 `POST /api/memory/vectors/rebuild` 手动触发

不自动重算的理由：那可能是几千次 API 调用，用户没同意就烧钱，而且期间召回质量是混乱的（一半新向量一半旧向量）。最坏情况应该是「召回暂时没有语义结果」，而不是「扣了一笔意外费用」。

**为什么必须按 model 筛而不是只看维度**：两个不同模型可能维度相同，那时算余弦会得到一个「看起来合理」的数值，而那个数值毫无意义。这是最难发现的一类 bug——召回照常返回结果，只是结果没有意义。

| 接口 | 作用 |
| --- | --- |
| `GET /api/memory/vectors` | 新鲜度统计（never / model / content / fresh 分开报告） |
| `POST /api/memory/vectors/rebuild` | 一键重算，默认只算失效的 |
| `DELETE /api/memory/vectors` | 清空，让召回干净地回落关键词 |
| `GET /api/memory/search` | 语义搜索，按三层隔离筛范围 |

`embedded_hash` 与 `content_hash` 分开：前者记「向量算的是哪一版内容」。两者不同说明记忆改过但向量没跟上，那时召回用的是旧语义，是个必须能被发现的状态而不是静默错误。

### 真实模型验证

`scripts/verify_memory.py` 跑真实模型（凭证从 `.env.verify` 读）。它和 pytest 的分工不可互换：

- pytest 用假 LLM 验**控制流**：每条分支都能精确触发，可复现
- verify 脚本验**契约能否被真模型满足**：JSON 合法性、page_id 引用、SEARCH 逐字符一致、工具参数格式

实测（deepseek-v4-pro）：eager + lazy 共 28/28 断言通过。单轮 30~135 秒，**推理 token 是正文的 5~10 倍**——所以脚本必须打进度和超时，否则和卡死无法区分。实测首轮 2 分 47 秒时我以为死锁了。

`--embedding` 用真实嵌入模型（BAAI/bge-m3，1024 维）验向量化链路，19/19 通过。它验的是假嵌入证明不了的东西——真实语义排序：查「单元测试应该怎么运行」得到 `testing` 0.573 > `database` 0.47 > `cooking` 0.337。

两个测试自身的坑值得记：

- **隔离断言必须用 `scope: session` 的类型**。我第一版用 `preferences`（它是 `scope: agent`），写它时 `session_id` 被正确忽略，于是「其他会话也能搜到」是对的行为，而断言失败看起来像隔离坏了。
- **`.env.verify` 的模型名要和 base_url 匹配**。填了 OpenAI 的 `text-embedding-3-small` 但指向硅基流动，返回 400 Model does not exist——那不是代码问题。

## 痕迹

记忆是模型自己改的，所以「改了什么」必须可查。三层痕迹：

| 层 | 内容 | 在哪 |
| --- | --- | --- |
| `WriteResult` | 单次写入的 `before` / `after` 全文 | 返回值 |
| `memory_diff` | 一批改动的 adds / updates / deletes + summary | `data/memory/agents/<id>/.trace/<extraction_id>.json` |
| 结构化日志 | `memory_written`：uri、版本、字符数变化 | structlog |

结构对齐 OpenViking 的 `memory_diff.json`（`compressor_v3.py:2019`），包括它一个容易漏的行为：**no-op 不进 `operations`**。一次合并可能报告成功但正文没变（幂等命中），记成 update 会让 diff 里出现 `before == after` 的条目。它在 `compressor_v3.py:307` 显式过滤，我们同样过滤，但把数量记在 `summary.total_unchanged` 里——「提取跑了但什么都没变」和「提取没跑」是两回事，混在一起会掩盖问题。

**日志不打正文。** 记忆里有用户的个人信息，日志会进文件、可能被贴到 issue。正文只在 `memory_diff` 里，那个文件和记忆同域、权限一致。

### 为什么删除要先读一遍

`delete_with_trace` 在删之前读出正文存进 `deleted_content`。多一次读换可追溯性——删掉的记忆**没有别处可查**，不留正文的话「模型把一条重要经验删了」只剩一行 uri，无法判断该不该恢复，也无法恢复。删除是低频操作，成本可接受。

### 痕迹的实际价值

三个真 bug 是靠痕迹发现的，不是靠断言想出来的（见 `tests/test_memory_trace.py`）：

1. **`events` 完全写不进去** —— `'extract_context' is undefined`。它是唯一需要日期路径渲染的类型，此前没有任何测试碰过它。
2. **正文被裸字符串整体顶掉** —— `patch` 字段收到裸字符串时回落成 `replace`，`tool_notes` 的两个小节静默消失。diff 里只显示一次正常的 update，看不出丢了东西。现在会打 warning。
3. **`content_template` 的壳被重复叠加** —— 这个**只有看文件才能发现**。`run_shell.md` 长出两个 `# 工具：run_shell` 标题和两组计数行，version 每涨一次多一层。

第 3 个的根因值得记下来：合并时拿 `body`（渲染结果）当 `current`，而模板会在 `content` 外面套一层壳，于是壳被反复套。修法是 `MemoryItem.raw_content` 单独保存渲染前的原始值。OpenViking 不会遇到这个问题，因为它的 `MemoryFile.content` 始终是原始内容，模板只在 serialize 时套用。

**原始值优先存偏移而不是副本。** frontmatter 里的 `raw_content_span: "起点:长度"` 指向正文里的位置，解析时切片还原。只有模板对 content 做了变换（原始值无法在正文里原样找到）才回落存 `<!-- JEEVES_RAW_CONTENT -->` 副本。

理由是 git diff：存副本时 trajectories 的一个文件 2200 字符里有 33% 是重复内容，每次改动在 diff 里出现两遍——而记忆目录进 git 的全部价值就是 diff 可读。偏移失效的场景是有人手工编辑了文件（这是文件形态的核心卖点，必须支持），那时回落「整个正文当原始值」，代价是模板壳可能被叠加一次，比抛异常好。

### 真实模型暴露的两个坑

**消费错了 chunk 字段名。** `ChunkKind` 的字面量是 `content` / `reasoning` / `tool_call` / `usage` / `done`（`port.py:17`），没有 `text`。我在 verify 脚本里写成 `kind == "text"`，把所有正文丢掉，表现为「模型返回空 → parse_error」——看起来像模型不听话。跑了两轮真实模型才发现。已加测试锁住这组字面量。

**推理内容不能参与解析。** 推理模型单轮输出上万字符 reasoning（实测 21830），那是思考过程，混进正文会让 JSON 解析必然失败。必须只取 `kind == "content"`。

## 接口契约

`service.py` 暴露的操作。读/写/删/向量化/召回/归档均已实现。

```python
# 读
async def get(scope: MemoryScope, memory_type: str, key: str = "") -> MemoryItem | None
async def read_uri(uri: str) -> MemoryItem | None
async def list_items(scope: MemoryScope, memory_type: str = "") -> list[MemoryItem]
async def visible_types(scope: MemoryScope) -> list[MemoryTypeSchema]

# 写
async def write(scope, memory_type, fields, *, extraction_id="") -> WriteResult
async def write_many(ops: list[WriteOp]) -> BatchResult
async def delete_uri(uri: str) -> bool

# 生命周期
async def init_agent(agent_id: str) -> None
async def drop_agent(agent_id: str) -> None
async def drop_session(session_id: str) -> None
async def rebuild_index() -> int
```

五条不变量，实现和测试都围绕它们：

1. **写前重读磁盘。** 每个 URI 在合并前重新读文件，不用调用方传来的旧内容。理由（OpenViking 在 `memory_updater.py:1048` 写明）：同一批操作里可能有多条 patch 打到同一个 URI，后一条必须看到前一条的结果。用缓存会让第二条 patch 的 search 匹配失败。
2. **`write` 幂等于内容。** 合并后正文与业务字段都没变 → 不写盘、`version` 不递增。否则每次 commit 都产生一堆无意义的 version 跳动和 git diff。
3. **scope 不可越界。** `MemoryScope(agent_id="A", session_id="s1")` 拿不到 `A/sessions/s2/` 或 `agents/B/` 下的任何东西。路径拼接只在 `layout.py` 一处发生，越界在那里被挡。
4. **文件是真源。** 读操作在索引与文件冲突时按文件返回，并顺手修索引。
5. **一个坏文件不影响其它。** 解析失败的记忆项跳过并记 warning，不抛异常。列举 100 个记忆时第 37 个 frontmatter 坏了，应该返回 99 个。

### MemoryScope

```python
@dataclass(frozen=True)
class MemoryScope:
    agent_id: str = ""        # 空 = 只访问 global
    session_id: str = ""      # 非空 = 可访问该会话的 session 域
    peer_agent_id: str = ""   # 非空 = 访问 A 眼中的 B
```

frozen 是刻意的：scope 是一次操作的「身份证」，中途被改掉会让越界检查失效。

能访问哪些域由字段组合决定，不需要额外的权限参数：

| agent_id | session_id | 可访问 |
| :---: | :---: | --- |
| 空 | 空 | `global` |
| 有 | 空 | `global` + 该 agent 的 `agent` 域 |
| 有 | 有 | 上面两项 + 该会话的 `session` 域 |
| 有 | 任意 | + peer 域（`peer_agent_id` 非空时） |

这比 OpenViking 的 `MemoryIsolationHandler` 简单得多。它需要 `allow_self` / `allowed_peer_ids` / `allowed_memory_types` 三个维度加上从消息 `peer_id` 反推目标（`_message_target_id`），因为它要处理「一段对话里多个用户说话，记忆各归各人」。Jeeves 的一次对话只有一个用户和一个智能体，归属在调用时就确定了。

## 暂不实现但已留位置的东西

对照 OpenViking 后确认这些是真实需求，只是不属于「接口层」这一步。写在这里避免之后重新发明。

| 机制 | OpenViking 出处 | Jeeves 状态 |
| --- | --- | --- |
| **提取编排**（ReAct 循环、工具调用） | `extract_loop.py` | 已实现，见下方「提取管线」 |
| **page_id** LLM 用整数而非 URI 引用记忆 | `page_id_map.py` | 已实现（`prefetch.PageMap`） |
| **refetch 防覆盖** write 目标已存在但 LLM 没读过 → 补读重试 | `extract_loop.py:729` | 提取阶段实现。这是防止「把已有记忆当新文件覆盖」的唯一屏障 |
| **patch 校验 + 修复重试** SEARCH 匹配不上 → 回错误给 LLM 重试 | `extract_loop.py:782` | 提取阶段实现。第一版只做精确匹配，所以这个更重要 |
| **并发写合并** 同一文件的并发 patch 攒批后二次 LLM 合并 | `streaming_memory_updater.py` | **不实现**。Jeeves 是单用户单进程，一个会话一次 commit。改用文件级 asyncio 锁 |
| **`.overview.md` 目录索引** | `memory_updater.py:1465` | 存储层实现（`overview_template` 已在 schema 里） |
| **向量层级 L0/L1/L2** | `hierarchical_retriever.py` | L0(abstract)/L1(overview)/L2(details) 三层均向量化，`level` 列区分。L2 正文超过上限会截断（取头尾） |
| **hotness 热度混合** | `retrieve/memory_lifecycle.py` | 召回阶段实现。公式照抄：`sigmoid(log1p(active_count)) × exp_decay(age)` |
| **分类型配额召回** events/entities/preferences 各有独立名额 | `memory.py:427` | 召回阶段实现。混在一起搜会让某一类占满名额 |
| **记忆间链接（wiki link）** | `graph_view.py` / `link_merge.py` | **暂不实现**。OpenViking 默认也是关的（`link_enabled: false`）。个人项目的记忆量下收益不明 |
| **agent 进化训练（RL）** | `session/train/` 整个目录 | **不实现**。那是一整套 RL 管线 |
