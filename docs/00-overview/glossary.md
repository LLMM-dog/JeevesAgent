# 术语表

统一用词。文档、代码标识符、UI 文案三处必须一致，避免同一个东西有三个名字。

## 核心对象

| 术语 | 代码标识 | 含义 |
| --- | --- | --- |
| 会话 | `session` | 一条对话线。**不叫 conversation，不叫 chat。** |
| 消息 | `message` | 会话里的一条记录。角色见下表 |
| 运行 | `run` | 一次"用户发言 → 模型给出最终答复"的完整过程，可含多轮工具调用。取消的粒度是 run |
| 跨度 | `span` | run 内的一个执行单元（一次 LLM 调用、一次工具调用、一个子智能体）。前端气泡树的节点 |
| 轮次 | `turn` | 一次 reason → act 往返。一个 run 含 1~N 个 turn |
| 工作区 | `workspace` | 一个本地目录。会话归属工作区，是文件工具的默认根 |
| 智能体 | `agent` | 一个"角色 + 提示词 + 工具集"的组合，由 `AgentSpec` 定义 |
| 子智能体 | `subagent` | 以工具形式调用的独立子会话 |
| 工具 | `tool` | 模型可调用的函数。**不叫 function，不叫 action** |
| 产物 | `artifact` | 模型生成的完整交付物（代码文件、文档）。有专门的 message 角色，不参与压缩 |
| 技能 | `skill` | `skills/<name>/` 目录下的能力包，含 `SKILL.md` |
| 宏 | `macro` | `macros/<name>/MACRO.md`，技能的轻量派生，纯流程无脚本 |
| 供应商 | `provider` | 一个 OpenAI 兼容端点（base_url + api_key） |
| 功能位 | `purpose` | 模型的用途槽位：`chat` / `vision` / `title` / `compact` / `embedding` |
| 沙箱 | `sandbox` | 代码执行环境的抽象，有本地和 Docker 两个实现 |
| 任务项 | `todo` | Todo 清单里的一条。**不叫 task**（task 在 asyncio 里已有含义） |
| 记忆 | `memory` | 跨会话的长期记忆条目 |

## message.role 取值

只有这六个，不再增加。

| role | 含义 | 是否进 LLM 上下文 | 是否参与压缩 |
| --- | --- | --- | --- |
| `user` | 用户发言 | 是 | 是 |
| `assistant` | 模型回复（含 `tool_calls`） | 是 | 是 |
| `tool` | 工具执行结果 | 是 | 是（但切点不能拆开它与对应的 assistant） |
| `system` | 系统提示词 | 是 | 否（永不压缩） |
| `summary` | 压缩摘要。**以 `user` 角色发给 LLM**，`role` 字段仅用于本地区分 | 是 | 否 |
| `artifact` | 产物。每个 `(session_id, agent_name)` 只保留最新一版 | 是（钉在末尾） | 否 |

## 模式开关

四个正交开关，可任意组合。

| 术语 | 代码标识 | 开启后 |
| --- | --- | --- |
| 检查模式 / 自动模式 | `approval_mode` = `manual` / `auto` | `manual`：执行类工具需人工确认。**默认 manual** |
| 私密模式 | `private_mode` | 本会话内容**不写入**长期记忆 |
| 失忆模式 | `amnesia_mode` | 本会话**不读取**长期记忆 |
| 视觉模式 | `vision_mode` | 引用的图片以 base64 多模态注入。需模型通过 vision 核验 |

## 技能三级披露

| 级别 | 内容 | 存放 | 何时进上下文 |
| --- | --- | --- | --- |
| L1 | `name` + `description` | `SKILL.md` frontmatter | **常驻**系统提示词 |
| L2 | `SKILL.md` 正文 | `SKILL.md` | 模型调 `load_skill` 时 |
| L3 | `references/` 等附属文件 | 技能目录内 | 模型调 `load_skill_file` 时 |

实测比例约 1:81（L1 合计 2KB 覆盖 171KB 能力），这是这套分级存在的全部理由。

## 易混淆的区分

**run 与 session**：一个 session 含多个 run。取消只取消当前 run，session 继续存在。

**span 与 turn**：turn 是"reason→act 往返"的计数概念；span 是"可展示的执行节点"，比 turn 细（一个 turn 里 3 个并行工具调用 = 3 个 span）。

**tool 与 skill**：tool 是代码，模型直接调用；skill 是文本指令包，模型通过 `load_skill` 这个 tool 来读。**技能不是工具**，它是喂给模型的知识。

**artifact 与 attachment**：artifact 是模型产出的，attachment 是用户上传的。

**compact 与 summary**：compact 是动作（压缩），summary 是产物（摘要消息）。

**memory 与 message**：message 属于某个 session；memory 跨 session，是被提炼过的。

## 命名约定

- 数据库表名单数、无前缀：`session` 而非 `sessions` 或 `t_session`
- 外键：`<表名>_id`，如 `session_id`
- 布尔字段：`is_` / `has_` 前缀，或直接形容词（`pinned`、`deleted`）
- 时间字段：`_at` 结尾，UTC 毫秒整数
- 枚举字段存字符串而非整数，便于直接读库排查
- 事件名：小写下划线，`<对象>_<动作>`，如 `tool_start`、`agent_end`

## 后来补充的区分

### 主动压缩 vs 被动压缩

同一个 `compaction.compact()`，两条触发路径：

| | 触发者 | 时机 |
| --- | --- | --- |
| 被动 | 阈值 | 涨到窗口的 `compact_trigger_ratio`（0.75） |
| 主动 | 模型调 `compact_context` | 它判断某个阶段结束了 |

阈值只看总量，不知道"调研阶段已经结束、几十条工具输出已经没用了"。主动压缩让模型在自然的段落边界上压。

主动那条走 `urgent=True` 不看阈值 —— 模型判断该压时上下文可能只用了 40%，走正常路径会直接返回什么都不做，而工具会报告"已压缩"（一个静默的谎）。

### 技能开关 vs 模式开关

两者作用域不同，别混：

| | 存哪 | 作用域 |
| --- | --- | --- |
| 模式开关（审批 / 视觉 / 私密 / 失忆） | `session` 表 | 会话级 |
| 技能开关 | `skill_state` 表 | 全局 |
| MCP 服务器开关 | `config/mcp_servers.yaml` | 全局 |

技能和 MCP 是"这台机器上装了什么"，不该按会话分。而私密模式这类是"这次对话怎么进行"。
