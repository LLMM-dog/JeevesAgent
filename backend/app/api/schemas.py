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
    private_mode: bool
    amnesia_mode: bool
    vision_mode: bool


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
    pinned: bool | None = None
    approval_mode: Literal["manual", "auto"] | None = None
    private_mode: bool | None = None
    amnesia_mode: bool | None = None
    vision_mode: bool | None = None


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
    artifact_kind: str | None
    artifact_path: str | None
    run_id: str | None
    span_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: int


class MessageListResponse(BaseModel):
    items: list[MessageOut]


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


# ─────────────────────────── provider ───────────────────────────


class ProbeRequest(BaseModel):
    base_url: str = Field(min_length=1)
    api_key: str = ""


class ProbedModelOut(BaseModel):
    model_id: str
    context_window: int
    window_source: str
    looks_non_chat: bool


class ProbeResponse(BaseModel):
    # 要回显 —— 用户填的可能被规范化了（补了 /v1），让他看到实际会用哪个地址
    normalized_base_url: str
    models: list[ProbedModelOut]


class CreateProviderModel(BaseModel):
    model_id: str
    display_name: str = ""
    context_window: int | None = None


class CreateProviderRequest(BaseModel):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    models: list[CreateProviderModel] = Field(default_factory=list)


class ProviderOut(BaseModel):
    id: str
    name: str
    base_url: str
    # 永远只返回尾 4 位，无明文
    key_hint: str
    enabled: bool
    model_count: int
    last_probe_at: int | None
    created_at: int


class ProviderListResponse(BaseModel):
    items: list[ProviderOut]


class ModelOut(BaseModel):
    id: str
    provider_id: str
    model_id: str
    display_name: str
    context_window: int
    window_source: str
    supports_vision: str
    supports_tools: str
    enabled: bool


class ModelListResponse(BaseModel):
    items: list[ModelOut]


class BindingOut(BaseModel):
    id: str
    agent_name: str
    purpose: str
    model_pk: str
    # join 出来给前端显示"DeepSeek / deepseek-chat"，省一次往返
    model_id: str
    provider_name: str


class BindingListResponse(BaseModel):
    items: list[BindingOut]


class SetBindingRequest(BaseModel):
    agent_name: str = ""
    purpose: Literal["chat", "vision", "title", "compact", "embedding"]
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
    skill_count: int
    macro_count: int
    mcp_tool_count: int
    tool_names: list[str]
