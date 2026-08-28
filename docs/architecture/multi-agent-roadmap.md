# 多智能体路线图

> 状态：阶段 1-2 已实现，阶段 3 之后为规划。

## 已实现

- 用户自定义智能体（`/api/agents` CRUD）
- 对话页选择单个智能体
- `subagent` 工具委派子任务
- 内置 `researcher` / `reviewer`
- 子智能体记忆线隔离

## 尚未实现

- 显式编排：Workflow / MoA / Router / Debate
- `agent_groups` 多智能体组
- 验证增强（每步完成后自动检查）
- 验证技能自动沉淀

实现时再更新对应架构文档。当前实现细节见 [agents.md](agents.md)。
