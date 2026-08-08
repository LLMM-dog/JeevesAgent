# 多智能体系统 — 实施步骤

> 每步可独立提交。标注了改动量和影响范围。

---

## 阶段 1：数据层 — 以智能体为单位

### 1.1 创建 agent_defs 表

**文件**：`backend/alembic/versions/xxx_add_agent_defs.py` + `backend/app/modules/agent/models.py`

```sql
CREATE TABLE agent_defs (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    avatar          TEXT,
    system_prompt   TEXT NOT NULL DEFAULT '',
    model_id        TEXT,
    skill_names     TEXT NOT NULL DEFAULT '[]',
    mcp_servers     TEXT NOT NULL DEFAULT '[]',
    permission_read      INTEGER NOT NULL DEFAULT 1,
    permission_write     INTEGER NOT NULL DEFAULT 0,
    permission_shell     INTEGER NOT NULL DEFAULT 0,
    permission_network   INTEGER NOT NULL DEFAULT 0,
    permission_subagent  INTEGER NOT NULL DEFAULT 0,
    verification_enabled INTEGER NOT NULL DEFAULT 0,
    strict_mode          INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    deleted_at      INTEGER
);
```

**改动量**：~30 行（Alembic migration）+ ~40 行（SQLAlchemy model）

---

### 1.2 session 表加 agent_id

```sql
ALTER TABLE session ADD COLUMN agent_id TEXT DEFAULT '';
```

**影响**：
- `Session` model 加字段
- 已有数据 `agent_id=''` 表示使用默认智能体（向后兼容）

**改动量**：~5 行（migration）+ 1 行（model）+ 1 行（创建会话时默认空串）

---

### 1.3 memory 表加 agent_id

```sql
ALTER TABLE memory ADD COLUMN agent_id TEXT DEFAULT '';
```

**影响**：
- `Memory` model 加字段
- 已有数据 `agent_id=''` 表示跨智能体共享（向后兼容）
- 召回查询加 `AND agent_id IN ('', '<当前agent_id>')` 过滤

**改动量**：~5 行（migration）+ 1 行（model）+ 召回查询改 1 行

---

## 阶段 2：业务层 — 智能体 CRUD

### 2.1 agent_service.py

```
agent_service.py
├── create_agent(data) → AgentDefinition
├── get_agent(agent_id) → AgentDefinition
├── list_agents(include_deleted=False) → list[AgentDefinition]
├── update_agent(agent_id, patch) → AgentDefinition
├── delete_agent(agent_id) → None  (软删除，检查群组引用)
└── get_default_agent() → AgentDefinition  (系统内置，不可删除)
```

**改动量**：~80 行

---

### 2.2 权限过滤函数

```python
# tool_registry 或 tools/base.py
def filter_tools_by_permissions(
    tools: list[Tool], agent_def: AgentDefinition
) -> list[Tool]:
    """根据智能体权限过滤可用工具。"""
    perms = {
        "read_file": agent_def.permission_read,
        "grep": agent_def.permission_read,
        "glob": agent_def.permission_read,
        "list_dir": agent_def.permission_read,
        "write_file": agent_def.permission_write,
        "edit_file": agent_def.permission_write,
        "run_shell": agent_def.permission_shell,
        "run_python": agent_def.permission_shell,
        "web_search": agent_def.permission_network,
        "web_fetch": agent_def.permission_network,
        "delegate_task": agent_def.permission_subagent,
    }
    return [t for t in tools if perms.get(t.name, True)]
```

**改动量**：~25 行

---

## 阶段 3：接口层

### 3.1 Agent API

```
GET    /api/agents          → list_agents
POST   /api/agents          → create_agent
GET    /api/agents/{id}     → get_agent
PATCH  /api/agents/{id}     → update_agent
DELETE /api/agents/{id}     → delete_agent
```

**改动量**：~60 行（FastAPI router）

---

### 3.2 系统内置默认智能体

应用首次启动时，`agent_defs` 表为空 → 自动创建一条默认智能体：

```python
# 迁移或启动逻辑
if not await agent_service.count():
    await agent_service.create(AgentDefinition(
        name="默认助手",
        description="通用任务执行",
        system_prompt="",  # 使用系统默认
        permission_read=True,
        permission_write=True,
        permission_shell=True,
        permission_network=True,
        permission_subagent=True,
    ))
```

---

## 阶段 4：链路改造 — chat_service 接受 agent_id

### 4.1 POST /api/chat 加 agent_id 参数

```python
# chat_service.py — 改动点
async def handle_chat(
    session_id: str,
    message: str,
    agent_id: str = "",  # 新增
):
    # 解析智能体
    agent_def = await agent_service.get(agent_id) if agent_id else await agent_service.get_default()

    # 创建 AgentLoop 时用智能体配置
    loop = AgentLoop(
        ...
        system_prompt=agent_def.system_prompt or current_system_prompt,
        registry=filter_tools_by_permissions(registry, agent_def),
    )

    # 加载智能体的 skills
    for skill_name in agent_def.skill_names:
        loop.skills.append(load_skill(skill_name))

    # 记录到 session
    await repo.set_agent_id(session_id, agent_def.id)
```

**改动量**：~30 行

---

### 4.2 Memory 查询加 agent_id 过滤

```python
# memory_service.py — 查询时加过滤
async def recall(db, theme, agent_id="", limit=5):
    stmt = (
        select(Memory)
        .where(Memory.archived_at.is_(None))
        .where(Memory.theme == theme)
        .where(Memory.agent_id.in_(["", agent_id]))  # 共享 + 本智能体
        .order_by(Memory.hit.desc())
        .limit(limit)
    )
```

**改动量**：~5 行（查询条件）

---

### 4.3 Skills 路由到智能体目录

```python
# skill_manage 工具执行时
def resolve_skill_path(agent_name: str, skill_name: str) -> Path:
    base = Path("skills") / agent_name
    return base / skill_name / "SKILL.md"
```

**改动量**：~10 行（路径解析）

---

## 阶段 5：前端

### 5.1 智能体管理页 /settings/agents

- 列表页：显示所有智能体，标记默认
- 编辑弹窗：名称、描述、提示词、模型、权限、验证增强开关
- 新建/删除

### 5.2 对话页智能体选择器

- 输入框上方加下拉选择
- 切换智能体时后端新建会话或在当前会话切换

---

## 全部改动量估算

| 层 | 行数 |
|----|------|
| 数据库 migration | ~40 |
| SQLAlchemy models | ~45 |
| agent_service | ~80 |
| 权限过滤 | ~25 |
| API router | ~60 |
| chat_service 改动 | ~30 |
| memory 查询改动 | ~5 |
| skills 路径改动 | ~10 |
| 启动默认智能体 | ~15 |
| **后端总计** | **~310 行** |
| 前端 | ~200 行 |

---

## 实施顺序

```
阶段 1 (数据层)
├── 1.1 建 agent_defs 表
├── 1.2 session 加 agent_id
└── 1.3 memory 加 agent_id

阶段 2 (业务层)
├── 2.1 agent_service CRUD
└── 2.2 权限过滤函数

阶段 3 (接口层)
├── 3.1 Agent API
└── 3.2 默认智能体

阶段 4 (链路改造)
├── 4.1 chat_service 接受 agent_id
├── 4.2 memory 按 agent_id 过滤
└── 4.3 skills 路由

阶段 5 (前端)
├── 5.1 智能体管理页
└── 5.2 对话页选择器
```
