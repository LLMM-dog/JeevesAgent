# 多智能体系统架构设计 v2

> 状态：规划中 | 日期：2026-08-08
> v2 变更：验证增强改为每智能体属性、智能体可自管理 skills、记忆隔离

---

## 1. 概念模型

### 1.1 智能体定义 (AgentDefinition)

```
AgentDefinition
├── 身份
│   ├── name            # "代码审查员"
│   ├── description     # "审查代码质量、安全性和架构"
│   └── avatar          # 可选图标
├── 行为
│   ├── system_prompt   # 自定义提示词
│   └── model_id        # 绑定的模型
├── 能力
│   ├── skill_names[]   # 加载的技能列表（系统 skill + 自建 skill）
│   └── mcp_servers[]   # 连接的 MCP 服务器
├── 权限
│   ├── read: bool      # 读文件
│   ├── write: bool     # 写文件
│   ├── shell: bool     # 执行命令
│   ├── network: bool   # 联网搜索
│   └── subagent: bool  # 能否生成子智能体
├── 验证增强  ←── 每智能体独立开关
│   ├── verification_enabled: bool  # 默认 false
│   └── strict_mode: bool           # true=卡住不通过不让继续
└── 元数据
    ├── created_at / updated_at
    └── skills_dir  # 该智能体的私有 skill 目录（自动派生）
```

**每个智能体都有独立的 skills 目录**：`skills/<agent_name>/`。系统 skills 全局共享，智能体自建的 skills 存在自己的目录里。智能体可以通过 `skill_manage` 工具对自己的 skills 进行增删改查。

### 1.2 验证智能体 — 每智能体一个

不是全局共享的配置——每个智能体定义自带 `verification_enabled` 属性。开启后在创建 AgentLoop 时自动挂载一个内置的轻量验证智能体：

```
智能体「代码审查员」(verification_enabled=true)
  │
  ├── 主 AgentLoop (代码审查员)
  │     todo_write → 完成步骤 2
  │
  ├── 验证智能体被唤醒
  │     system_prompt: "你是代码审查员的成果检查员..."
  │     使用智能体绑定的模型（或辅助模型）
  │     有自己的 skills: skills/代码审查员/verification/
  │
  └── 主 AgentLoop 继续步骤 3
```

**验证智能体会进化**：每次验证通过/失败的经验可以沉淀为 skill。比如发现"模型经常声称修改了文件但实际没改"，验证智能体就会创建一条 skill 规则："检查 write_file 的返回值确认文件真的被写入"。

### 1.3 智能体群组 (AgentGroup)

不变。MOA（并行+汇总）和 Workflow（顺序执行）两种模式。群组里每个成员是一个 AgentDefinition。

---

## 2. Skills 自管理

### 2.1 目录结构

```
skills/
├── code-review/              ← 系统 skill，所有智能体可用
├── security-audit/           ← 系统 skill
├── 代码审查员/               ← 智能体「代码审查员」的私有 skill 目录
│   ├── SKILL.md              ← 智能体自己创建的 skill
│   └── verification/         ← 验证智能体的 skill 目录
│       ├── check-file-writes.md   ← 验证规则：检查文件是否真的被写入
│       └── detect-empty-results.md
└── 研究员/                   ← 智能体「研究员」的私有 skill 目录
    └── SKILL.md
```

### 2.2 智能体调用 skill_manage 时

```python
# 智能体调 skill_manage 创建 skill 时，自动路由到自己的目录
skill_manage(action="create", name="my-skill", content="...")
  → 实际创建在 skills/<agent_name>/my-skill/SKILL.md

skill_manage(action="patch", name="my-skill", ...)
  → 只能修改自己的 skill，不能改系统 skill

skill_manage(action="delete", name="my-skill")
  → 只能删自己的 skill
```

### 2.3 验证智能体的自我进化

验证智能体也是通过 `skill_manage` 进化——但它的 skill 目录是 `skills/<agent_name>/verification/`。示例进化流程：

```
验证智能体第 5 次发现同一个模式：
  "Agent 声称 write_file 完成，但文件内容为空"

→ 验证智能体调 skill_manage:
    action="create"
    name="check-file-writes"
    content="验证规则：当 Agent 声明 write_file 完成时，
             必须检查该文件是否真的被写入且内容非空。
             用 read_file 读取文件的前 5 行确认。"

→ 后续验证时自动加载此 skill，检查更严格
```

---

## 3. 记忆隔离

### 3.1 三层隔离

```
memory 表
├── target="user"          ← 用户 profile（跨智能体共享）
│   session_id: NULL
│   agent_id: NULL
│
├── target="agent"         ← 智能体记忆（每智能体独立）
│   session_id: NULL
│   agent_id: "adf_代码审查员"
│
└── target="session"       ← 会话记忆（每智能体、每会话独立）
    session_id: "ses_xxx"
    agent_id: "adf_代码审查员"
```

`agent_memory` 是该智能体跨会话积累的经验。"代码审查员"在被多次使用后，会记住"这个项目的测试框架是 pytest"、"用户偏好严格的类型检查"等。

`session_memory` 只在当前会话有效。切换智能体时，会话记忆也切换——用户和"代码审查员"的对话记忆不会污染"研究员"。

### 3.2 验证智能体也有独立记忆

```python
# 验证智能体的记忆条目示例
memory(
    target="agent",
    agent_id="adf_代码审查员_verification",  # 独立 ID
    content="该智能体常犯的错误：声称修改了文件但未验证。已强化检查规则。"
)
```

---

## 4. 执行模型

### 4.1 单智能体

```
用户选择智能体 → 创建 AgentLoop
  ├── system_prompt ← AgentDefinition.system_prompt
  ├── tools         ← AgentDefinition.permissions 过滤
  ├── model         ← AgentDefinition.model_id
  ├── skills        ← AgentDefinition.skill_names + 该智能体的私有 skills
  ├── memory(target="agent") ← agent_id 过滤
  └── memory(target="session") ← agent_id + session_id 过滤
```

### 4.2 单智能体 + 验证增强

```
用户选择智能体(verification_enabled=true) → 创建 AgentLoop

主 Agent 执行 todo 步骤:
  │
  ├── todo_write 标记步骤 N completed
  │
  ├── 验证智能体被唤醒（独立的轻量 AgentLoop）
  │     system_prompt: 验证模板
  │     model: 智能体绑定的模型或辅助模型
  │     skills: skills/<agent_name>/verification/ （自我进化的规则）
  │     memory: agent_id=验证智能体独立 ID
  │     输入: 步骤描述 + 执行记录 + 当前 todo 状态
  │
  ├── 验证通过 → 主 Agent 继续步骤 N+1
  ├── 建议改进 → 反馈注入主 Agent（不阻止）
  └── 未通过 → 反馈注入主 Agent + strict_mode 时阻止
```

---

## 5. 数据库 Schema 变更

```sql
-- agent_defs 新增字段
ALTER TABLE agent_defs ADD COLUMN verification_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_defs ADD COLUMN strict_mode INTEGER NOT NULL DEFAULT 0;

-- memory 表新增 agent_id
ALTER TABLE memory ADD COLUMN agent_id TEXT;
-- 已有数据：agent_id=NULL 表示跨智能体共享（user profile）

-- sessions 表新增 agent_id
ALTER TABLE sessions ADD COLUMN agent_id TEXT;
-- 记录本轮对话使用哪个智能体，恢复时复用
```

---

## 6. 实施优先级

| 阶段 | 内容 | 理由 |
|------|------|------|
| **1** | 智能体定义 CRUD + 权限过滤 | 基础——创建和选择智能体 |
| **2** | 对话页切换智能体 + 记忆隔离 | 用户能使用自定义智能体，记忆不污染 |
| **3** | 智能体自管理 skills | Agent 可创建/修改自己的私有 skill |
| **4** | 验证增强 | 每智能体可选，验证智能体独立运行 + 自我进化 |
| **5** | 群组/工作流 | 多智能体编排 |
