"""
请求/响应模型。

字段名 snake_case，与数据库列和前端 TypeScript interface 完全一致 ——
不做 camelCase 转换。转换层是跨层 bug 的温床：嵌套结构误转、
后端加字段前端静默丢失、Network 面板字段名和代码对不上。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    hint: str | None = None


# ─────────────────────────── session ───────────────────────────


class SessionBrief(BaseModel):
    id: str
    title: str
    workspace_id: str
    pinned: bool
    message_count: int
    last_message_at: int
    created_at: int


class SessionDetail(SessionBrief):
    approval_mode: str
    # 空串 = 未设置。前端据此显示"选择工作目录"的提示
    work_dir: str = ""
    # 空串表示跟随功能位绑定
    model_pk: str = ""
    # 这次对话实际生效的模型窗口。
    #
    # 【必须带上】—— 前端要用它算上下文占用比例。没有的话只能猜一个
    # 默认值 32K，而实际可能是 128K，进度条会虚高四倍，
    # 用户以为快满了。
    context_window: int = 0
    private_mode: bool
    amnesia_mode: bool
    vision_mode: bool
    stream_enabled: bool = True
    agent_id: str = ""


class SessionListResponse(BaseModel):
    items: list[SessionBrief]
    total: int
    page: int
    size: int
    pages: int


class CreateSessionRequest(BaseModel):
    title: str = ""
    workspace_id: str | None = None


class PatchSessionRequest(BaseModel):
    title: str | None = None
    # 这次对话的工作目录。传空串表示清除。
    #
    # 用 str 而不是 Path：Path 在 JSON 里没有对应类型，
    # pydantic 会把 Windows 反斜杠当转义符处理。
    work_dir: str | None = None
    # 这次对话用哪个模型。传空串 = 回到默认绑定
    model_pk: str | None = None
    pinned: bool | None = None
    approval_mode: Literal["manual", "auto"] | None = None
    private_mode: bool | None = None
    amnesia_mode: bool | None = None
    vision_mode: bool | None = None
    stream_enabled: bool | None = None
    agent_id: str | None = None


class MessageOut(BaseModel):
    id: str
    seq: int
    role: str
    agent_name: str
    content: str
    reasoning: str | None
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None
    tool_name: str | None
    tool_display: dict[str, Any] | None
    is_error: bool
    refs: list[dict[str, Any]] | None
    attachments: list[str] | None
    run_id: str | None
    span_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: int


class MessageListResponse(BaseModel):
    items: list[MessageOut]
    # 归档水位线：seq <= watermark 的消息已被记忆归档，不再发回 LLM。
    # -1 表示无归档。前端据此跳过已归档消息，恢复"归档后的上下文占用"。
    watermark: int = -1


# ─────────────────────────── chat ───────────────────────────


class ChatRequest(BaseModel):
    session_id: str
    content: str = Field(min_length=1)
    # 七种引用类型见 docs/03-api/endpoints-chat.md
    refs: list[dict[str, Any]] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)
    # 图片的 data URL 列表。
    #
    # 直接传 data URL 而不是先上传拿 id 再引用 —— 图片最终要以 base64
    # 进 LLM 请求，存文件再读回来编码是多绕一圈。前端粘贴即得 data URL。
    #
    # 服务端仍然会校验（魔数 + 大小 + 数量），只信前端校验等于没校验。
    images: list[str] = Field(default_factory=list)
    agent_id: str = ""  # 选择的智能体，空串=默认


class CancelResponse(BaseModel):
    run_id: str
    status: str


class ApproveRequest(BaseModel):
    call_id: str
    approved: bool


class AnswerRequest(BaseModel):
    call_id: str
    answer: str | None = None
    selected: list[str] | None = None


# ─────────────────────────── endpoint ───────────────────────────


class ProbeRequest(BaseModel):
    base_url: str = Field(min_length=1)
    api_key: str = ""


class ProbedModelOut(BaseModel):
    model_id: str
    context_window: int
    window_source: str
    looks_non_chat: bool
    model_type: str


class ProbeResponse(BaseModel):
    # 要回显 —— 用户填的可能被规范化了（补了 /v1），让他看到实际会用哪个地址
    normalized_base_url: str
    # 从地址推断的分组名。用户不填名字，前端回显它让用户知道会归到哪个分组
    suggested_name: str
    models: list[ProbedModelOut]


class CreateEndpointModel(BaseModel):
    model_id: str
    display_name: str = ""
    context_window: int | None = None
    model_type: str = ""
    supports_vision: str = "unknown"  # "true" | "false" | "unknown"
    supports_tools: str = "unknown"   # "true" | "false" | "unknown"


class CreateEndpointRequest(BaseModel):
    # 允许留空 —— 空则由后端从 base_url 推断分组名（"添加模型"是纯自动分组）
    name: str = ""
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    models: list[CreateEndpointModel] = Field(default_factory=list)


class EndpointOut(BaseModel):
    id: str
    name: str
    base_url: str
    # 永远只返回尾 4 位，无明文
    key_hint: str
    enabled: bool
    model_count: int
    last_probe_at: int | None
    created_at: int


class EndpointListResponse(BaseModel):
    items: list[EndpointOut]


class ModelOut(BaseModel):
    id: str
    endpoint_id: str
    # 端点名。对话页下拉显示 "端点 / 模型"。
    # 只有 endpoint_id 的话前端还得再查一次
    endpoint_name: str = ""
    model_id: str
    display_name: str
    context_window: int
    window_source: str
    supports_vision: str
    supports_tools: str
    # 模型类型（chat / reasoning / embedding / ...），前端用图标显示
    model_type: str = "chat"
    # 禁用的模型不出现在对话页快捷切换菜单里，但配置保留
    enabled: bool
    # 被绑定到的功能位（purpose），如 ["chat", "memory"]。前端据此显示
    # "这个模型被配置为对话、记忆提取"，并在禁用时弹提醒。
    bindings: list[str] = []
    price_in_per_1m: float | None = None
    price_out_per_1m: float | None = None


class ModelCreate(BaseModel):
    """往已有模型组下加一个模型。不用重建端点。"""

    endpoint_id: str
    model_id: str = Field(..., min_length=1)
    display_name: str = ""
    context_window: int = Field(32768, ge=1024)
    model_type: str = ""


class ModelPatch(BaseModel):
    enabled: bool | None = None
    display_name: str | None = None
    context_window: int | None = None
    # 移动模型到另一个分组（拖动改分组）。绑定引用的是 model_pk 不受影响。
    endpoint_id: str | None = None
    model_type: str | None = None
    price_in_per_1m: float | None = None
    price_out_per_1m: float | None = None
    supports_vision: str | None = None  # "true" | "false" | "unknown"
    supports_tools: str | None = None   # "true" | "false" | "unknown"


class UpdateEndpointRequest(BaseModel):
    """改分组名字 / 地址 / Key。三个都可不传，传了才改。"""

    name: str | None = None
    base_url: str | None = None
    # 空串 = 保持原 Key。前端永远拿不到明文 Key，编辑时输入框是空的。
    api_key: str | None = None


class ModelListResponse(BaseModel):
    items: list[ModelOut]


class VisionVerifyRequest(BaseModel):
    model_pk: str


class BindingOut(BaseModel):
    id: str
    agent_name: str
    purpose: str
    model_pk: str
    # join 出来给前端显示"DeepSeek / deepseek-chat"，省一次往返
    model_id: str
    endpoint_name: str


class BindingListResponse(BaseModel):
    items: list[BindingOut]


class SetBindingRequest(BaseModel):
    agent_name: str = ""
    purpose: Literal["chat", "vision", "title", "compact", "embedding", "memory", "memory_rerank"]
    model_pk: str


# ─────────────────────────── todo ───────────────────────────


class TodoOut(BaseModel):
    id: str
    content: str
    status: str
    priority: str
    order_index: int
    archived_at: int | None
    created_at: int


class TodoListResponse(BaseModel):
    items: list[TodoOut]
    stats: dict[str, int]


class PatchTodoRequest(BaseModel):
    content: str | None = None
    status: Literal["pending", "in_progress", "completed", "cancelled"] | None = None
    priority: Literal["high", "medium", "low"] | None = None
    order_index: int | None = None


# ─────────────────────────── auth ───────────────────────────


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class LoginResponse(BaseModel):
    authenticated: bool = True
    username: str
    is_admin: bool
    # 会话 token。cookie 已经自动带上；这里再给一份是为了
    # 命令行 / 脚本场景（curl -H "Authorization: Bearer <token>"）。
    # 注意：这是原始 token 唯一一次出现在响应里。
    token: str = ""


class AuthMeResponse(BaseModel):
    # 鉴权是否开启。false = 本机无鉴权模式。
    auth_enabled: bool
    # 当前请求是否已通过鉴权（auth_enabled=False 时恒为 False）。
    authenticated: bool
    username: str = ""
    is_admin: bool = False
    session_ttl_days: int = 0


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    is_admin: bool
    enabled: bool
    created_at: int
    last_login_at: int


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_-]{2,32}$")
    password: str = Field(..., min_length=8, max_length=128)
    is_admin: bool = False


class UserPatchRequest(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=128)
    is_admin: bool | None = None
    enabled: bool | None = None


# ─────────────────────────── deploy ───────────────────────────


class DeployStatusResponse(BaseModel):
    """远程访问的整体状态。"""

    host: str
    port: int
    is_localhost: bool
    auth_enabled: bool
    # 当前请求是否走 https（反代/隧道转发时由 X-Forwarded-Proto 决定）。
    https: bool = False


class DeploySettingsUpdate(BaseModel):
    values: dict[str, Any]


class EnableAuthRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=8, max_length=128)


class EnableAuthResponse(BaseModel):
    username: str
    token: str


class TailscaleRequest(BaseModel):
    port: int = Field(default=9000, ge=1, le=65535)


class CpolarRequest(BaseModel):
    port: int = Field(default=9000, ge=1, le=65535)


class CpolarAuthtokenRequest(BaseModel):
    token: str = Field(..., min_length=8, max_length=256)


class CpolarActionResponse(BaseModel):
    ok: bool
    detail: str = ""
    status: dict = Field(default_factory=dict)


class TailscaleActionResponse(BaseModel):
    ok: bool
    detail: str = ""
    status: dict = Field(default_factory=dict)


# ─────────────────────────── meta ───────────────────────────


class MetaResponse(BaseModel):
    version: str
    sandbox_backend: str
    sandbox_docker_available: bool
    # 配了 docker 但降级到本地执行的原因。空表示没降级。
    #
    # 【必须暴露给前端】—— 用户配了 docker 就是想要隔离，
    # 静默用本地执行等于骗他：他会以为命令跑在容器里，
    # 于是放心地让 agent 执行危险操作。
    #
    # 前端据此在会话内【持续显示】提示条（不是一闪而过的 toast）——
    # 用户需要一直知道当前不是隔离环境。
    sandbox_fallback_reason: str = ""
    # 当前后端是否真隔离。local 后端为 false ——
    # PathGuard 限制了文件范围、白名单限制了命令，但命令跑起来就是
    # 宿主进程：能访问网络、能读环境变量、资源不受限。
    sandbox_isolated: bool = False
    websearch_backend: str
    has_chat_model: bool
    # false 时前端显示无鉴权警示条
    host_is_localhost: bool
    # 远程访问鉴权是否开启。true 时前端显示登录页。
    auth_enabled: bool = False
    # 远程访问鉴权是否开启。true 时前端显示登录页。
    auth_enabled: bool = False
    skill_count: int
    mcp_tool_count: int
    tool_names: list[str]


class WhitelistItem(BaseModel):
    """一条路径白名单。"""

    id: str
    path: str
    can_write: bool
    note: str = ""
    builtin: bool = False
    # None = 全局条目（所有会话都生效）
    session_id: str | None = None
    # 路径当前是否真实存在。
    #
    # 必须告诉用户 —— 目录被移走或删掉时，白名单还在但工具会失败，
    # 而错误信息只说"路径不在白名单内"，指向完全错误的方向。
    exists: bool = True


class WhitelistCreate(BaseModel):
    path: str = Field(..., min_length=1)
    can_write: bool = False
    note: str = ""


class WhitelistPatch(BaseModel):
    can_write: bool | None = None
    note: str | None = None


class BrowseEntry(BaseModel):
    name: str
    path: str
    is_dir: bool


class BrowseResult(BaseModel):
    """目录浏览结果，供工作目录选择器用。"""

    path: str
    parent: str | None = None
    entries: list[BrowseEntry] = Field(default_factory=list)
    # 常用起点（盘符 / 家目录 / 项目目录），让用户不用手打路径
    roots: list[BrowseEntry] = Field(default_factory=list)


class SkillToggle(BaseModel):
    enabled: bool


class McpToggle(BaseModel):
    enabled: bool


class McpServerCreate(BaseModel):
    """添加 MCP 服务器的请求体。"""

    server_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$")
    transport: str = Field(default="http", pattern=r"^(http|stdio)$")
    # http 模式
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    # stdio 模式
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    # 通用
    enabled: bool = True


class McpServerUpdate(BaseModel):
    """修改 MCP 服务器的请求体。只传要改的字段。"""

    transport: str | None = Field(default=None, pattern=r"^(http|stdio)$")
    url: str | None = None
    headers: dict[str, str] | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled: bool | None = None
