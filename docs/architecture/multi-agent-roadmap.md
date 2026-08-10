# 多智能体路线图

> 最后更新：2026-08-10
> 状态：阶段 1-2 已完成，阶段 3-5 规划中

---

## 0. 设计前提

### 智能体是第一公民

不是"主程序 + 插件"模型。主智能体本身也是一个 AgentDefinition —— 用户在会话中选择哪个智能体来对话，所选智能体就是"主智能体"。子智能体只是一个工具，它可以委派给其他已定义的智能体。

```
智能体定义 (AgentDefinition)
├── 主智能体 ← 用户在对话页选择的那个
│   └── 可以委派给其他智能体 (subagent 工具)
└── 工具型智能体 ← 被委派的子智能体
    └── 只能执行被分配的子任务，不继承父会话历史
```

### 验证增强是智能体的属性

每个 AgentDefinition 自带 `verification_enabled` / `strict_mode` 开关。开启后在主智能体完成每个 Todo 步骤时，自动唤醒一个轻量验证智能体检查成果。验证智能体是系统内置的，不需要用户定义——但它的验证规则（skills）会自我进化。

```
智能体 A (verification_enabled=true)
  │
  ├── 步骤 N 完成
  ├── 验证智能体自动唤醒
  │     ├── pass   → 继续步骤 N+1
  │     ├── suggest → 注入改进建议（不阻止）
  │     └── fail   → 注入反馈 + strict_mode 时阻止继续
  │
  └── 验证智能体自我进化
        └── 发现重复模式 → 自动创建验证 skill
```

---

## 已完成的阶段

### 阶段 1：智能体定义与 CRUD ✓

**已完成**（`backend/app/modules/agent/models.py`，migration `20260808_2242`）

- `agent_defs` 表：name、description、system_prompt、model_id、skills、MCP、权限、验证增强开关
- `agent_service.py`：CRUD + 默认智能体种子
- Agent API：`/api/agents` 全套端点
- 前端智能体管理页（`/settings/agents`）

### 阶段 2：单智能体链路打通 ✓

**已完成**

- `POST /api/chat` 接受 `agent_id`，加载对应 AgentDefinition
- 权限过滤：`_filter_tools_by_permissions()` 根据智能体权限移除工具
- 智能体绑定的模型优先，未绑定时回退到全局默认
- 智能体自带的 skills 和 MCP 连接生效
- 前端对话页智能体选择器

---

## 规划中的阶段

### 阶段 3：验证增强

**目标**：每个智能体可独立开启成果检查，验证智能体自动在 Todo 步骤完成后运行。

**改动点**：

| 层 | 内容 | 改动量 |
|----|------|--------|
| 验证智能体核心 | 轻量 AgentLoop，专用 system prompt，只读工具集 | ~80 行 |
| Todo 钩子 | `AFTER_TODO_COMPLETE` 钩子，检测步骤完成 → 唤醒验证智能体 | ~30 行 |
| 裁定反馈 | pass/suggest/fail → 注入 Loop 消息或当前轮重做 | ~40 行 |
| 验证 skill 进化 | 验证智能体检测到重复失败模式 → 调用 `manage_asset` 创建验证 skill | ~60 行 |
| 前端 | 验证结果卡片（折叠在步骤下方）、strict_mode 拦截提示 | ~80 行 |

**验证智能体的设计约束**：

- 只读工具集：`read_file`、`grep`、`glob`、`load_skill`、`todo_read`。绝不能写文件或执行命令
- 不继承父会话完整历史：只拿"当前步骤描述 + 最近执行记录"
- 每次调用成本可控：一次 LLM 调用，用智能体绑定的模型（或专门的轻量模型）
- evolution 动作不调 LLM：只是文件写入

**自我进化流程**：

```
发现同一模式下第 3 次失败
  → manage_asset(action="create", kind="skill",
      name="check-file-writes",
      content="验证规则：当步骤声明 write_file 完成后，
               用 read_file 读取确认文件非空...")
  → 后续验证自动加载此 rule
```

### 阶段 4：多智能体编排

**目标**：将已定义的智能体组合成工作流或并行协作。

#### 4.1 编排模式

| 模式 | 说明 | 使用场景 |
|------|------|----------|
| **Workflow**（顺序） | Agent A → Agent B → Agent C，固定顺序 | 审查报告：研究员读资料→分析师写报告→审查员检查 |
| **MoA**（并行+汇总） | 多个智能体并行处理同一输入，最后汇总 | 代码审查：安全审查员+性能审查员+风格审查员同时查，审查员汇总 |
| **Router**（路由） | 根据输入特征路由到最合适的智能体 | 用户提问时自动选最合适的智能体回答 |
| **Debate**（辩论） | 两个智能体互辩，由第三个裁判裁定 | 架构决策、安全风险评估 |

#### 4.2 数据模型

```sql
-- 编排模板
CREATE TABLE orchestration_templates (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    mode     TEXT NOT NULL,  -- workflow | moa | router | debate
    config   TEXT NOT NULL,  -- JSON: 各模式的配置
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

Workflow 配置示例：

```json
{
  "steps": [
    {"agent_id": "adf_researcher", "label": "调研阶段"},
    {"agent_id": "adf_writer",     "label": "写作阶段"},
    {"agent_id": "adf_reviewer",   "label": "审查阶段"}
  ],
  "pass_context": true
}
```

MoA 配置示例：

```json
{
  "members": ["adf_security", "adf_perf", "adf_style"],
  "aggregator": "adf_reviewer",
  "parallel_limit": 3
}
```

#### 4.3 执行引擎

编排不是一个"特殊类型的智能体"——它是独立的执行引擎 `OrchestrationRunner`：

```python
class OrchestrationRunner:
    def __init__(self, template: OrchestrationTemplate, context: str):
        ...

    async def run(self) -> OrchestrationResult:
        if self.template.mode == "workflow":
            return await self._run_workflow()
        elif self.template.mode == "moa":
            return await self._run_moa()
        ...
```

每个成员智能体本质上就是一个 AgentLoop 调用——复用现有的 loop、工具注册、权限过滤、上下文管理。

#### 4.4 与现有 subagent 的关系

| | subagent 工具 | 编排 |
| --- | --- | --- |
| 触发方式 | 主智能体主动调用（模型决策） | 用户选择编排模板后执行 |
| 智能体选择 | 模型按 spec description 选 | 固定配置 |
| 结果流向 | 返回给主智能体继续推理 | 最终汇总返回给用户 |
| 适用场景 | 主智能体运行中的临时委派 | 固定的多步/并行审查流程 |

**两者共存**，互不替代。subagent 解决"模型自己觉得该委派"的场景；编排解决"用户想强制执行一个固定多智能体流程"的场景。

### 阶段 5：智能体自管理与技能进化

**目标**：智能体可以自己创建、修改自己的 skills，验证智能体的验证规则也能沉淀为可共享的 skill。

#### 5.1 Skills 目录按智能体隔离

```
skills/
├── code-review/              ← 系统 skill，所有智能体可用
├── security-audit/           ← 系统 skill
├── adf_default/              ← 智能体「默认助手」的私有 skill
│   └── my-workflow/
│       └── SKILL.md
├── adf_researcher/           ← 智能体「研究员」的私有 skill
│   └── deep-search-pattern/
│       └── SKILL.md
└── verification/             ← 全局验证 skill（可跨智能体共享）
    ├── SKILL.md              ← 验证智能体的基础行为
    ├── check-file-not-empty.md
    └── require-test-evidence.md
```

#### 5.2 技能管理工具的智能体路由

当智能体调 `manage_asset(action="create", kind="skill", ...)` 时，自动创建到 `skills/<agent_name>/` 目录下。智能体只能修改/删除自己的 skills，不能动系统 skill 和其他智能体的 skill。

#### 5.3 验证 skill 全局共享

验证智能体创建的验证规则存在 `skills/verification/` 下，任何智能体的验证智能体都能加载它们。一个智能体踩过的坑可以保护所有智能体。

---

## 实施优先级

| 优先级 | 阶段 | 原因 |
|--------|------|------|
| **P0** | 阶段 3：验证增强 | 每个智能体的质量保障基础设施。单智能体也能用，不需要等编排 |
| **P1** | 阶段 4.2+4.3：Workflow + MoA 执行引擎 | 最直接的编排价值——固定的审查/调研流程 |
| **P1** | 阶段 5：Skills 按智能体隔离 | 智能体自管理能力的基础 |
| **P2** | 阶段 4.2：Router + Debate 模式 | 高级编排模式，Workflow/MoA 覆盖 80% 场景 |
| **P2** | 阶段 5：验证 skill 全局共享 | 依赖阶段 3 和阶段 5.1 完成 |

---

## 设计约束（不会改变）

1. **AgentLoop 保持不变**。主智能体、子智能体、验证智能体、编排成员——全部复用同一个循环引擎。不会引入 LangGraph 的 StateGraph 或 LangChain 的 AgentExecutor。
2. **subagent 保持为工具**。不会变成独立的编排层。模型自己决定什么时候委派。
3. **智能体记忆线隔离不会变**。`message.agent_name` 的隔离机制是基础，所有新模式都继承它。
4. **白名单安全模型不会变**。新增工具不会自动泄露给子智能体或验证智能体。
