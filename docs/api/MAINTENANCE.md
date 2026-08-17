# API 文档生成与维护指南

本文档说明如何生成、更新和维护 Jeeves API 文档。

---

## 📁 文档结构

```
docs/api/
├── README.md                    # 文档索引（手动维护）
├── API_OVERVIEW.md              # API 总览（手动维护）
├── QUICK_START.md               # 快速上手（手动维护）
├── MAINTENANCE.md               # 本文件
├── routes_chat.md               # 会话与对话（自动生成）
├── routes_config.md             # 配置管理（自动生成）
├── routes_cron.md               # 定时任务（自动生成）
├── routes_files.md              # 文件访问（自动生成）
├── routes_models.md             # 模型管理（自动生成）
├── agent_router.md              # 智能体（自动生成）
└── memory_router.md             # 记忆系统（自动生成）
```

---

## 🔧 生成文档

### 自动生成

运行脚本自动提取所有后端路由：

```bash
# 从项目根目录运行
python scripts/generate_api_docs.py
```

**输出**:
- 7 个模块的详细端点文档
- 更新 `README.md` 中的端点统计

**生成规则**:
- 从 Python 装饰器中提取：`@router.get()`, `@router.post()` 等
- 解析 `summary` 参数作为端点描述
- 解析 `response_model` 参数作为响应类型
- 提取路径参数（`{param_name}`）

### 手动更新

以下文档需要手动维护：

1. **README.md** - 文档索引和快速链接
2. **API_OVERVIEW.md** - 数据模型定义、错误处理、SSE 示例
3. **QUICK_START.md** - 示例代码、集成指南

---

## 📝 添加新端点

### 后端步骤

1. **定义路由** (`backend/app/api/routes_*.py`):

```python
@router.get(
    "/example",
    response_model=ExampleResponse,
    summary="示例端点"
)
async def get_example():
    """
    这是一个示例端点。
    
    详细说明可以写在这里（多行）。
    """
    return {"data": "example"}
```

2. **定义 Schema** (`backend/app/api/schemas.py`):

```python
class ExampleResponse(BaseModel):
    data: str
    timestamp: int | None = None
```

3. **重新生成文档**:

```bash
python scripts/generate_api_docs.py
```

### 前端步骤

1. **添加 TypeScript 类型** (前端项目):

```typescript
interface ExampleResponse {
  data: string;
  timestamp?: number;
}
```

2. **添加 API 方法**:

```typescript
class JeevesAPI {
  async getExample(): Promise<ExampleResponse> {
    const response = await fetch(`${this.base}/example`);
    return response.json();
  }
}
```

3. **更新文档** (如果需要特殊说明):

编辑 `docs/api/QUICK_START.md` 或 `API_OVERVIEW.md`。

---

## 🔄 更新现有端点

### 修改后端

1. 修改路由装饰器或函数签名
2. 更新 `summary` 参数或 docstring
3. 更新 `response_model`

### 重新生成文档

```bash
python scripts/generate_api_docs.py
```

**注意**: 自动生成会**覆盖**以下文件：
- `routes_chat.md`
- `routes_config.md`
- `routes_cron.md`
- `routes_files.md`
- `routes_models.md`
- `agent_router.md`
- `memory_router.md`

**不会覆盖**:
- `README.md`
- `API_OVERVIEW.md`
- `QUICK_START.md`
- `MAINTENANCE.md`

---

## 📊 文档检查清单

### 添加新模块时

- [ ] 在 `scripts/generate_api_docs.py` 的 `ROUTE_FILES` 中添加条目
- [ ] 运行生成脚本
- [ ] 在 `README.md` 中添加模块链接
- [ ] 在 `API_OVERVIEW.md` 中添加模块说明
- [ ] 如果有特殊用法，在 `QUICK_START.md` 中添加示例

### 更新端点时

- [ ] 修改后端路由
- [ ] 运行生成脚本
- [ ] 检查生成的文档是否正确
- [ ] 如果有 breaking changes，更新 `API_OVERVIEW.md` 的版本历史

### 发布前检查

- [ ] 所有端点都有 `summary` 参数
- [ ] 所有响应都有 `response_model`
- [ ] 所有 Schema 都有字段说明
- [ ] 运行生成脚本，确保文档最新
- [ ] 手动检查 `README.md` 的链接
- [ ] 验证 `QUICK_START.md` 的示例代码可运行

---

## 🛠️ 脚本维护

### 生成脚本位置

`scripts/generate_api_docs.py`

### 主要功能

1. **extract_endpoints()** - 从 Python 文件提取端点
2. **generate_markdown()** - 生成 Markdown 文档
3. **ROUTE_FILES** - 配置路由文件列表

### 修改生成逻辑

**示例：添加请求参数提取**

```python
def extract_endpoints(file_path: Path) -> list[EndpointInfo]:
    # ... 现有代码 ...
    
    # 新增：提取查询参数
    query_params = []
    for line in function_body:
        if 'Query(' in line:
            param_match = re.search(r'(\w+):\s*.*Query\(', line)
            if param_match:
                query_params.append(param_match.group(1))
    
    # ... 添加到 EndpointInfo ...
```

### 测试生成脚本

```bash
# 测试单个模块
python scripts/generate_api_docs.py

# 检查输出
cat docs/api/routes_chat.md
```

---

## 📚 最佳实践

### 后端代码规范

1. **所有路由必须有 summary**:

```python
@router.get("/users", summary="用户列表")  # ✅ 好
@router.get("/users")                      # ❌ 差
```

2. **使用 response_model**:

```python
@router.get("/users", response_model=UserListResponse)  # ✅ 好
@router.get("/users")                                   # ❌ 差
```

3. **Schema 使用类型注解**:

```python
class UserOut(BaseModel):
    id: str                    # ✅ 好
    name: str | None = None    # ✅ 好，可选字段
    age: int = 0               # ✅ 好，有默认值
```

4. **路径参数使用有意义的名称**:

```python
@router.get("/sessions/{session_id}")     # ✅ 好
@router.get("/sessions/{id}")             # ❌ 差
```

### 文档写作规范

1. **简洁明了**:
   - Summary: 一句话说明功能
   - 详细说明: 参数、返回值、注意事项

2. **使用示例**:
   - 提供真实的请求/响应 JSON
   - 代码示例要能直接运行

3. **保持同步**:
   - 后端修改后立即更新文档
   - 定期检查文档与代码的一致性

---

## 🔍 故障排查

### 问题：生成的文档中端点为 0

**原因**: 装饰器格式不匹配

**解决**:
1. 检查路由文件是否使用 `@router.METHOD(` 格式
2. 确保装饰器在函数定义的上一行
3. 查看脚本输出的错误信息

### 问题：路径参数未提取

**原因**: 路径中没有 `{param}` 格式

**解决**:
1. 使用 `{param_name}` 格式定义路径参数
2. 检查正则表达式是否匹配

### 问题：Response Model 未显示

**原因**: 装饰器中未指定 `response_model`

**解决**:
```python
@router.get("/users", response_model=UserListResponse)
```

---

## 📅 维护计划

### 每次发布前

- 运行文档生成脚本
- 检查所有链接
- 验证示例代码

### 每月

- 审查文档完整性
- 更新过时的示例
- 收集前端开发者反馈

### 每季度

- 重构文档结构
- 添加新的最佳实践
- 更新 TypeScript 类型定义

---

## 🤝 贡献指南

### 改进文档

1. Fork 项目
2. 修改文档
3. 提交 Pull Request

### 报告问题

在 GitHub Issues 中提交，标记为 `documentation`。

---

## 📞 联系方式

- **项目**: Jeeves
- **维护者**: LLMM-dog
- **文档位置**: `docs/api/`
- **生成脚本**: `scripts/generate_api_docs.py`

---

**最后更新**: 2026-08-15  
**文档版本**: v1.0
