"""
异常体系。

注意：工具执行中抛出的异常【不走这套体系】—— 它们被 ToolRegistry.execute()
捕获转成错误文本给模型。见 docs/01-architecture/tools.md#异常处理
"""

from typing import Any


class AppError(Exception):
    """所有业务异常的基类。code 与 docs/03-api/conventions.md 的错误码清单一致。"""

    status_code = 500
    code = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        hint: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class PathDeniedError(AppError):
    """路径白名单/拒止锚拦截。403 专用于路径拒绝，不用于鉴权（本项目无鉴权）。"""

    status_code = 403
    code = "path_denied"


class SandboxError(AppError):
    status_code = 500
    code = "sandbox_error"


class ProviderError(AppError):
    """上游 LLM / MCP 失败。"""

    status_code = 502
    code = "upstream_error"


class NoModelBoundError(AppError):
    status_code = 400
    code = "no_model_bound"

    def __init__(self, purpose: str = "chat") -> None:
        super().__init__(
            f"未配置 {purpose} 功能位的模型",
            hint="请到设置页添加供应商并绑定模型",
        )


class EncryptionNotConfiguredError(AppError):
    status_code = 500
    code = "encryption_not_configured"
