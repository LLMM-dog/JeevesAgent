"""
MCP 服务器配置加载。从 `config/mcp_servers.yaml` 读。

## 为什么用文件而不是数据库

MCP 配置里有 token（`Authorization: Bearer xxx`），而本项目的数据库
不加密存这类字段（只有 provider 的 api_key 走加密）。放文件里并加
`.gitignore` 更直接。

文档（`docs/01-architecture/mcp.md:13`）也说明了这个分层意图：将来要改成
存表，只需换配置层的来源，协议层和接线层不动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from app.core.config import PROJECT_ROOT
from app.modules.mcp.config import ServerConfig

log = structlog.get_logger(__name__)

CONFIG_PATH = PROJECT_ROOT / "config" / "mcp_servers.yaml"

# 文档里写了四种 transport，但规范只定义两种标准传输
# （stdio + Streamable HTTP），sse 是已被取代的旧传输、websocket 属于
# custom transport。
#
# 这里支持三种：stdio / http（= streamable_http）/ sse（兼容老服务器）。
# websocket 不做 —— 官方 SDK 1.9.4 里它需要额外依赖，而且规范没把它
# 列为标准传输，实际能遇到的服务器极少。
_ALIASES = {
    "stdio": "stdio",
    "http": "http",
    "streamable_http": "http",
    "streamablehttp": "http",
    "sse": "sse",
}


def _one(raw: dict[str, Any]) -> ServerConfig:
    sid = str(raw.get("server_id") or "").strip()
    t_raw = str(raw.get("transport") or "stdio").strip().lower()
    t = _ALIASES.get(t_raw)
    if t is None:
        raise ValueError(
            f"不认识的 transport {t_raw!r}。支持 stdio / streamable_http / sse"
            "（websocket 未实现：规范未将其列为标准传输）"
        )

    cfg = ServerConfig(
        server_id=sid,
        transport=t,  # type: ignore[arg-type]
        enabled=bool(raw.get("enabled", True)),
        command=str(raw.get("command") or ""),
        args=[str(a) for a in (raw.get("args") or [])],
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        cwd=str(raw.get("cwd") or ""),
        url=str(raw.get("url") or ""),
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        command_approved=bool(raw.get("command_approved", False)),
    )
    cfg.validate()
    return cfg


def load_configs(path: Path | None = None) -> tuple[list[ServerConfig], list[str]]:
    """
    读配置，返回 (配置列表, 错误列表)。

    ## 逐条独立降级

    一个服务器配错（transport 写错、stdio 缺 command）不影响其他条目。
    错误收集起来回给前端显示 —— 静默跳过的话用户看不到自己配的服务器
    去哪了。
    """
    p = path or CONFIG_PATH
    if not p.is_file():
        return [], []

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        return [], [f"读取 {p.name} 失败：{e}"]

    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [f"{p.name} 顶层必须是列表（一个服务器一项）"]

    out: list[ServerConfig] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"第 {i + 1} 项不是映射，已跳过")
            continue
        try:
            cfg = _one(item)
        except ValueError as e:
            sid = item.get("server_id") or f"#{i + 1}"
            errors.append(f"服务器 {sid}：{e}")
            continue
        if cfg.server_id in seen:
            # server_id 会成为工具名前缀，重复会导致工具名冲突
            errors.append(f"server_id {cfg.server_id} 重复，已跳过后面那个")
            continue
        seen.add(cfg.server_id)
        out.append(cfg)

    log.info("mcp_config_loaded", count=len(out), errors=len(errors))
    return out, errors


def estimate_tokens(cfg_tools: list[Any]) -> int:
    """
    估算一组工具定义的 token 数。

    ## 为什么要暴露这个数字

    MCP 工具定义是**常驻上下文成本** —— 每一轮请求都要带上全部工具的
    名字、描述、入参 schema。配 5 个服务器共 60 个工具，可能就是
    上万 token，每轮都烧。

    用户看不到这个数字的话，会觉得"多开几个 MCP 没坏处"。

    估算用字符数 / 3（中英混合的粗略比例），不调 tiktoken ——
    这个数字只需要量级正确，而 tiktoken 对每个工具都编码一次太慢。
    """
    total = 0
    for t in cfg_tools:
        total += len(getattr(t, "raw_name", "")) + len(getattr(t, "description", ""))
        schema = getattr(t, "input_schema", None)
        if schema:
            import json

            try:
                total += len(json.dumps(schema, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
    return total // 3
