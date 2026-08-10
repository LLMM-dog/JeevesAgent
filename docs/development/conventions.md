# 代码规范

## Python

### 格式与 lint

ruff 统一格式化 + lint，不用 black/isort/flake8。

```toml
[tool.ruff]
line-length = 120
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP", "B", "ASYNC"]
ignore = ["E501"]     # 行长交给 formatter，lint 不重复报

[tool.ruff.lint.per-file-ignores]
"backend/migrations/*" = ["E", "F", "N", "I", "W"]     # 迁移文件是生成的，不强求
"backend/app/api/*" = ["B008"]     # B008: FastAPI Depends() 是框架惯例
"backend/tests/*" = ["SLF001"]     # 测试访问私有函数是正常的
```

`line-length = 120` 而非 88：SQLAlchemy 的列定义和 FastAPI 的依赖注入天然长，88 会把它们折成难读的多行。

`ASYNC` 规则组很重要——它能抓到"在 async 函数里调了阻塞 I/O"这类问题。

### 类型注解

所有函数签名必须有注解。mypy 配置：

```toml
[tool.mypy]
python_version = "3.11"
strict = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["langgraph.*", "mcp.*"]
ignore_missing_imports = true
```

`strict = true` 起手。放松单条规则可以，但不要整体关掉——之后再收紧的成本远高于一开始就严格。

### 命名

| 对象 | 风格 | 例 |
| --- | --- | --- |
| 模块 / 包 | snake_case | `agent_events.py` |
| 类 | PascalCase | `ToolRegistry` |
| 函数 / 变量 | snake_case | `resolve_model` |
| 常量 | UPPER_SNAKE | `MAX_OUTPUT_CHARS` |
| 私有 | 前缀 `_` | `_current_bus` |
| Protocol | 后缀 `Port` 或无后缀 | `LLMPort` / `Tool` |
| Pydantic 请求 | 后缀 `Request` | `CreateSessionRequest` |
| Pydantic 响应 | 后缀 `Response` 或实体名 | `SessionResponse` / `Session` |
| SQLAlchemy 模型 | 单数实体名 | `Session` / `Message` |

**`Session` 命名冲突要注意**：SQLAlchemy 的 `AsyncSession`、本项目的 `Session` 模型、FastAPI 的依赖。约定：

```python
from sqlalchemy.ext.asyncio import AsyncSession      # 数据库会话，永远叫 AsyncSession
from app.modules.session.models import Session       # 业务会话
```

数据库会话的变量名统一为 `db`，不叫 `session`：

```python
async def get_session(db: AsyncSession, session_id: str) -> Session:
```

### async 规范

- I/O 一律 async。文件读写用 `anyio.Path` 或在 `asyncio.to_thread` 里跑
- **不在 async 函数里调阻塞 I/O**。`ruff` 的 ASYNC 规则会抓，但它抓不到间接调用
- `asyncio.CancelledError` **必须重新抛出**，不能被 `except Exception` 吞掉：

```python
try:
    ...
except asyncio.CancelledError:
    raise                  # 必须。吞掉它会让取消功能失效
except Exception as e:
    ...
```

这条极重要。`except Exception` 在 Python 3.8+ 不会捕获 `CancelledError`（它继承 `BaseException`），但 `except BaseException` 或裸 `except:` 会。**禁止裸 except。**

### 注释写决策，不写代码在做什么

这是最有价值的习惯之一。

```python
# 坏：
# 遍历消息列表
for msg in messages:

# 好：
# 向前扫到 tool 组边界外。切点落在 tool_calls 与其 tool 结果之间时，
# OpenAI 兼容 API 直接返回 400 且不指明原因。
while not _is_group_boundary(messages, cut):
    cut -= 1
```

注释里要留：**踩过的坑、实测数据、为什么不用另一种做法**。

```python
# 实测：本机开 Clash 时同一请求走代理 32s 被掐断，绕过代理 252s 正常完成。
# 代理的空闲连接超时远短于一次长推理，而推理阶段不吐字节，连接看起来就是空闲的。
trust_env=False
```

半年后回来看代码，"为什么"比"是什么"重要得多。

### docstring

模块级 docstring 说明这个模块的职责边界和它与相邻模块的分工。函数级 docstring 只在行为不显然时写。

```python
"""
工具注册表。

与 modules/agent/loop.py 的分工：loop 只管"要不要调工具"，
本模块管"怎么调、失败怎么办"。工具执行的异常绝不向上抛，
一律转成错误文本让模型自我纠正 —— 除了 CancelledError。
"""
```

### 单文件上限

500 行。超了拆。

例外：`core/config.py` 允许更长（配置项集中定义比拆散好找），但每个配置项要有注释说明取值依据。

## TypeScript

### 格式与 lint

用 Biome 或 ESLint + Prettier。选 Biome：一个工具搞定 lint + format，比 ESLint 快得多，配置少。

```json
{
  "formatter": { "indentStyle": "space", "indentWidth": 2, "lineWidth": 100 },
  "linter": { "rules": { "recommended": true } }
}
```

### 命名

| 对象 | 风格 |
| --- | --- |
| 组件文件 | PascalCase（`MessageList.tsx`） |
| 非组件文件 | camelCase（`chatStore.ts`） |
| 组件 | PascalCase |
| hook | `use` 前缀 camelCase |
| 类型 / interface | PascalCase，**不加 `I` 前缀** |
| 常量 | UPPER_SNAKE |

### snake_case 的边界

见 [../architecture/backend.md](../architecture/backend.md#为什么不用-camelcase)。规则复述：

- **来自后端的数据结构：snake_case**（`types/api.ts`、`types/events.ts`）
- **纯前端的东西：camelCase**（组件 props、hook 返回、局部变量）

```typescript
// 后端数据保持原样
const msg: Message = await api.get(`/api/messages/${id}`);
console.log(msg.agent_name, msg.created_at);

// 前端自己的东西用 camelCase
interface MessageItemProps {
  message: Message;
  isStreaming: boolean;
  onRetry: () => void;
}
```

带 `_` 的字段是后端来的——这个视觉提示本身有用。

### 禁止 any

用 `unknown` + 类型收窄。自由结构（`tool_display`）用 `Record<string, unknown>`。

`// @ts-expect-error` 允许，但必须紧跟一行注释说明原因。`// @ts-ignore` 禁止（它不会在错误消失后提醒你删掉）。

### 组件规范

- 函数组件 + hooks，不用 class
- props 用 interface 而非 inline type
- 单文件上限 300 行
- 一个文件一个组件（小的辅助组件可同文件，但不导出）
- 不用 `React.FC`（它对 children 的处理有历史包袱，且妨碍泛型组件）

```typescript
interface Props {
  message: Message;
}

export function UserBubble({ message }: Props) {
  ...
}
```

## Git

### 分支

`main` 单分支。个人项目不需要 git flow。

大改动开临时分支，做完就合并删除。

### 提交信息

```
<type>: <中文描述>

[可选的正文，说明为什么这么改]
```

type 取值：

| type | 用于 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 修 bug |
| `refactor` | 重构，行为不变 |
| `docs` | 只改文档 |
| `chore` | 依赖、配置、脚本 |
| `test` | 测试 |
| `perf` | 性能 |

```
feat: 模型探测支持中转站前缀剥离

中转站返回的模型名带 openai/ 或 accounts/xxx/models/ 前缀，
按原名去上下文窗口映射表里查不到，全部回落到 32K 默认值。
```

**正文里写为什么，不写改了哪些文件**（那个 diff 里有）。

### 不提交的东西

见 [../architecture/data-files.md](../architecture/data-files.md#什么进-git什么不进)。

特别注意：`.env`、`config/mcp_servers.yaml`、`personas/SOUL.md`、`personas/USER.md`、`data/`、`workspace/`。

提交前 `git status` 扫一眼，别用 `git add .`。

## 文档同步

**改代码时必须同步改文档的三种情况**（否则文档失效，本项目最重要的资产就废了）：

| 改了什么 | 必须同步 |
| --- | --- |
| 数据库表结构 | [../architecture/data-schema.md](../architecture/data-schema.md) 或 schema-2.md |
| SSE 事件名或字段 | [../api/sse-events.md](../api/sse-events.md) **+ 前端 switch** |
| 接口路径、请求/响应结构 | [../api/](../api/) 对应文件 |

其余情况（内部实现调整）不强求改文档，但如果推翻了文档里写的某个决策，要把新的理由写进去。

**文档与代码冲突时，先改文档再改代码。** 文档是设计，代码是实现。
