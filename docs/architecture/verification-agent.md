# 内置验证智能体设计 v3

> v2→v3 变更：验证智能体通过 skills（非 memory 表）实现自我进化。Skills 可跨智能体共享。

---

## 核心设计

验证智能体是**独立的 AgentLoop 实例**，系统内置。每完成一个 todo 步骤后自动唤醒。

### 自我进化机制

验证智能体不是往数据库写记忆来进化，而是调用 `skill_manage` 创建**验证 skill**。这些 skill 是文件，可跨会话、跨智能体共享：

```
skills/verification/
├── check-file-not-empty.md      ← 验证规则：确认文件真的被写入
├── require-test-evidence.md     ← 验证规则：代码修改后必须跑测试
├── detect-empty-results.md      ← 验证规则：检测空结果模式
└── SKILL.md                     ← 验证智能体的基础行为
```

### 进化触发条件

```
同一模式出现 3 次 → 创建验证 skill

例：
  第 1 次: Agent 声称 write_file 完成，文件为空 → 标记为 fail
  第 2 次: 同样模式 → 标记为 fail，记录
  第 3 次: 同样模式 → 触发进化 →
    skill_manage(action="create", name="check-file-not-empty",
      category="verification",
      content="验证规则：当步骤涉及 write_file 时...")

  第 4 次及以后: 自动加载此 skill → 检查更严格
```

### 验证智能体的 System Prompt

```markdown
你是成果检查员。检查智能体完成的每一步是否真的完成。

## 你的验证规则

你会自动加载 `skills/verification/` 下的所有验证 skill。
每个 skill 定义了特定场景下的验证标准。严格遵循它们。

## 验证模式

当检测到同一种错误模式反复出现时，使用 skill_manage 创建新的验证规则。
新规则应该：
1. 描述具体的问题模式
2. 定义自动检测该模式的方法
3. 说明判定 pass/fail 的标准

## 输出格式

{ "verdict": "pass"|"suggest"|"fail", "reason": "...", "feedback": "..." }
只输出 JSON。
```

---

## 触发时机

```
智能体 (verification_enabled=true)
  │
  ├── todo_write → 标记步骤 N 为 completed
  │
  ├── 验证智能体被唤醒
  │     system_prompt: 验证模板
  │     model: 智能体绑定的模型或辅助模型
  │     skills: skills/verification/ 下所有 skill
  │     输入: 步骤描述 + 执行记录 + 当前 todo 状态
  │
  ├── pass → 继续步骤 N+1
  ├── suggest → 注入反馈（不阻止）
  └── fail → 注入反馈 + strict_mode 时阻止
```

---

## 与 AgentLoop 的集成

```python
async def run_with_verification(loop, agent_def):
    verifier = None
    if agent_def.verification_enabled:
        verifier = VerificationAgent(
            agent_def=agent_def,
            model=resolve_model(agent_def.model_id),
        )
        # 加载验证 skills
        verifier.load_skills("skills/verification/")

    while True:
        result = await loop.run_one_turn()
        completed_step = _detect_completed_todo_step(result)
        if completed_step and verifier:
            verdict = await verifier.verify(step=completed_step, ...)
            if verdict.verdict == "fail" and agent_def.strict_mode:
                loop._append(Msg(role="user", content=f"[验证] {verdict.feedback}", ephemeral=True))
                continue

        if not result.has_tool_calls:
            break
```

---

## 成本模型

- 每完成一个 todo 步骤调用一次 LLM
- 使用智能体绑定的模型（或辅助模型）
- 5 步任务 = 额外 5 次 LLM 调用
- `verification_enabled=false`：零开销
- 进化动作（skill_manage）不调 LLM，只是文件写入
