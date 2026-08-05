"""
日志配置。

dev 模式输出到 stdout（彩色人类可读），prod 只写文件（JSON 行便于 grep）。

脱敏：REDACT_KEYS 里的字段值一律替换为 ***。
特别注意执行输出（ExecResult.stdout）【不进日志文件】—— 用户可能执行了
env 或 cat config.yaml。执行日志只记命令本身和退出码，输出内容已经在
数据库的 message 里了。
"""

import logging
import logging.handlers
import sys
from typing import Any

import structlog

from app.core.config import settings

REDACT_KEYS = frozenset(
    {
        "api_key",
        "api_key_cipher",
        "authorization",
        "token",
        "password",
        "secret",
        "encryption_key",
    }
)

_REDACTED = "***"


def _redact(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if key.lower() in REDACT_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def setup_logging() -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.app.is_dev:
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
        handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    else:
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
        handlers = [
            logging.handlers.TimedRotatingFileHandler(
                settings.logs_dir / "app.log",
                when="midnight",
                backupCount=14,
                encoding="utf-8",
            )
        ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    for h in handlers:
        h.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(settings.app.log_level.upper())

    # uvicorn 的 access 日志在开发时很吵，且信息量低于我们自己的请求日志
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx 每个请求都打一行 INFO，长对话下会淹没有用信息
    logging.getLogger("httpx").setLevel(logging.WARNING)
