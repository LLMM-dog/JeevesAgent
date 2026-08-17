# 测试数据

## 为什么这个目录必须进 git

`.gitignore` 里有 `data/`，而那条规则匹配**任意层级**的 `data` 目录 —— 包括这里。所以 `.gitignore` 有一条显式的取反规则（`!backend/tests/data/**`）。

不加取反规则会静默丢数据：fixture 写好后 `git status` 里看不见，重建工作区时消失，而对应的测试报 `FileNotFoundError` —— 那个错误完全不指向 gitignore。这个坑已经踩过一次。

## 会话对话数据

`sessions/<session_id>/messages.jsonl` — 每行一条消息，字段与 `message` 表的列一一对应（`seq` / `role` / `agent_name` / `content` / `tool_calls` / `tool_call_id` / `tool_name` / `tool_display` / `is_error`）。

`tool_calls` 是 **JSON 字符串**而非对象，与数据库里的存法一致。反序列化路径因此和生产完全相同（`session/repo.py:row_to_msg`），不会出现"测试里能解析、生产里不能"。

### ses_first_memory — 首次提取

一次完整的编码会话，25 条消息。设计成能产出每一类记忆：

| 该产出 | 依据（消息 seq） |
| --- | --- |
| `preferences` | 4（配置用 dataclass）、10（布尔默认值先确认）、14（ruff 强制）、22（pytest -x --tb=short） |
| `events` | 5-6（加 --verbose）、11-12（修 verbose 默认值）、19-20（修 tmpdir） |
| `experiences` | 18-21（pytest 7+ 用 tmp_path 替代 tmpdir） |
| `trajectories` | 整个会话（目标 → 步骤 → 一次返工 → 完成） |
| `tool_notes` | edit_file ×3、run_shell ×3、read_file ×1 |
| `profile` | 全程（个人项目、Python、在意工具链一致性） |

**验证点**：所有记忆都是新建（`created=True` / `version=1`），没有任何 merge 发生。

### ses_accumulated — 已有记忆后的再次提取

同一个智能体的后续会话，21 条消息。它**故意与已有记忆冲突**，用来验证 merge 而非追加：

| 场景 | 依据 | 期望 |
| --- | --- | --- |
| **偏好被改写** | seq 0：pytest 从 `-x --tb=short` 改成 `-q` | 旧值消失，不是两条并存 |
| **偏好被确认不变** | seq 15：ruff 规则不变 | 内容不变 → 幂等，`version` 不递增 |
| **计数器累加** | run_shell ×3、edit_file ×2 | `total_calls` 在旧值上累加 |
| **新增实体** | seq 13：小明接手 billing | 新建，且只在本会话可见 |
| **失败被记录** | seq 6：`FrozenInstanceError`（`is_error=1`） | 进 trajectory 的过程，或成为 experience 的 Reflect |
| **新经验** | seq 7-12：frozen dataclass 配 `dataclasses.replace` | 新建一条 experience |

**核心验证点**：`pytest 参数` 这条偏好在提取后**必须只有一个值**。如果 `-x --tb=short` 和 `-q` 同时留在文件里，说明 `patch` 退化成了追加 —— 那正是重写前的 bug。

## 怎么跑

不在 tmp 里断言，而是复制到真实的 `data/memory/` 观察文件变化：

```bash
uv run python scripts/memory_playground.py --scenario first
uv run python scripts/memory_playground.py --scenario accumulated
uv run python scripts/memory_playground.py --scenario both --keep
```

`--keep` 保留产物供人工检查。不加则跑完清理。详见脚本自身的 docstring。
