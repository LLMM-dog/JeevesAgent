# 内置验证智能体设计 v2

> v2 变更：从全局配置 → 每智能体独立属性。支持自我进化。

---

## 核心变更

| v1（旧） | v2（新） |
|----------|----------|
| 全局配置，一个开关影响所有智能体 | 每个智能体独立开关 `verification_enabled` |
| 验证提示词全局共享 | 验证智能体有自己的 system_prompt + skills |
| 不会进化 | 通过 skill_manage 沉淀经验，逐步变强 |
| 无记忆 | 独立 memory，记住该智能体常犯的错误 |

---

## 触发时机

```
智能体 A（verification_enabled=true）
  │
  ├── todo_write → 标记步骤 N 为 completed
  │
  ├── 验证智能体被唤醒
  │     ┌─────────────────────────────────────────┐
  │     │ 输入:                                    │
  │     │  - 步骤描述 (todo step)                  │
  │     │  - 执行记录 (本轮 tool calls + results)   │
  │     │  - 当前 todo 完整状态                     │
  │     │  - 该智能体历史 memory（常见错误模式）     │
  │     │  - 该智能体的 verification skills         │
  │     │                                          │
  │     │ 输出: { verdict, reason, feedback }      │
  │     └─────────────────────────────────────────┘
  │
  ├── pass → 继续步骤 N+1
  ├── suggest → 注入反馈（不阻止）
  └── fail → 注入反馈 + strict_mode 时阻止
```

---

## 验证智能体的 System Prompt

```markdown
你是成果检查员。你的任务是验证智能体「{agent_name}」完成的每一步是否真的完成。

## 你的记忆

你会收到该智能体历史上的常见错误模式。重点关注它是否重复犯同样的错误。

## 判断标准

- ✅ pass：步骤产出物已生成、有证据支持、不依赖缺失信息
- ⚠️ suggest：基本完成但有明显改进空间
- ❌ fail：声称完成但没有证据、关键操作失败被忽略、产出物实际不存在

## 输出格式

{ "verdict": "pass", "reason": "...", "feedback": null }

只输出 JSON。
```

---

## 自我进化流程

```
第 3 次验证时:
  验证智能体发现 Agent 又犯了"声称 write_file 但文件为空"
  → 检查自己的 memory: 这个模式已出现 3 次
  → 达到阈值 → 调 skill_manage 创建规则:

  skill_manage(
    action="create",
    name="check-file-not-empty",
    category="verification",
    content="""
    验证规则：当步骤涉及 write_file 时：
    1. 检查 write_file 的返回值确认操作成功
    2. 用 read_file 读取该文件的前 5 行确认非空
    3. 如果文件为空或写入失败，判定为 fail
    """
  )
  → 写入 skills/<agent_name>/verification/check-file-not-empty/SKILL.md

第 4 次及以后验证时:
  → 验证智能体自动加载此 skill
  → 检查更严格，不再漏过空文件
```

---

## 与 AgentLoop 的集成

```python
# chat_service.py

async def run_with_verification(loop, agent_def):
    verifier = None
    if agent_def.verification_enabled:
        verifier = VerificationAgent(
            agent_def=agent_def,
            model=resolve_model(agent_def.model_id),
        )

    while True:
        result = await loop.run_one_turn()

        # 检查是否完成了 todo 步骤
        completed_step = _detect_completed_todo_step(result)
        if completed_step and verifier:
            verdict = await verifier.verify(
                step=completed_step,
                execution_log=_get_turn_log(loop),
                todo_state=_get_todo_state(loop),
            )

            if verdict.verdict == "fail" and agent_def.strict_mode:
                loop._append(Msg(
                    role="user",
                    content=f"[验证] 步骤未通过：{verdict.feedback}",
                    ephemeral=True,
                ))
                continue  # 重新执行当前步骤
            elif verdict.verdict in ("fail", "suggest"):
                loop._append(Msg(
                    role="user",
                    content=f"[验证] {verdict.feedback}",
                    ephemeral=True,
                ))
                # suggest 不阻止，继续

        if not result.has_tool_calls:
            break
```

---

## 成本模型

- 验证智能体每完成一个 todo 步骤调用一次 LLM（不是每轮）
- 使用智能体绑定的模型（或辅助模型，通常更便宜）
- 5 步任务 = 额外 5 次 LLM 调用
- `verification_enabled=false`（默认）：零开销
