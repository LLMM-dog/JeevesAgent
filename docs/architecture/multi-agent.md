# 多智能体系统架构（规划稿）

> 状态：本文是早期规划，**尚未实现**。
>
> 当前实际代码请以 [agents.md](agents.md) 和 [agent_router.md](../api/agent_router.md) 为准。

当前项目已实现：

- 用户自定义智能体（`agent_defs`，设置页 CRUD）
- `subagent` 工具：主智能体可以把子任务委派给内置子智能体
- 内置 `researcher` / `reviewer` 两个子智能体

尚未实现：

- `agent_groups` 显式编排
- 验证增强（verification_enabled / strict_mode）
- Workflow / MoA / Router / Debate 等多智能体编排模式

这些内容仍在路线图中，等实现后再补文档。
