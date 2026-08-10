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

import re
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

def set_enabled(server_id: str, enabled: bool, path: Path | None = None) -> bool:
    """
    改 yaml 里某个服务器的 enabled，返回是否找到了它。

    ## 为什么是逐行文本编辑，不是 load + dump

    mcp_servers.yaml 是用户手写并且要继续手写的文件。
    yaml.safe_dump 会丢掉全部注释、重排键顺序、把中文转成 \\uXXXX ——
    点一次开关就把用户写的注释全删了。

    逐行编辑只碰 enabled 那一行，其余字节原样不动。这比任何
    round-trip 库都保守，而且不需要新增依赖（项目只有 pyyaml，
    它没有保留注释的能力）。

    ## 为什么不用数据库存这个状态

    manager 已经在读 cfg.enabled。存到别处就有两个真源 ——
    用户手工编辑 yaml 和界面点开关会互相打脸。

    ## 无法识别格式时返回 False

    宁可告诉用户"没找到这个服务器"，也不要在看不懂的格式上乱写。
    那个文件里可能有 token。
    """
    p = path or CONFIG_PATH
    if not p.is_file():
        return False
    return _set_enabled_textual(p, server_id, enabled)


def _block_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """找到 YAML 列表里每个顶层项的起止行号。"""
    starts = [i for i, ln in enumerate(lines) if re.match(r"^\s*-\s", ln)]
    if not starts:
        return []
    out: list[tuple[int, int]] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(lines)
        out.append((s, e))
    return out


def _find_block(lines: list[str], server_id: str) -> tuple[int, int] | None:
    """找指定 server_id 的块起止行号，没找到返回 None。"""
    want = re.compile(
        rf"^\s*(?:-\s+)?server_id:\s*['\"]?{re.escape(server_id)}['\"]?\s*(?:#.*)?$"
    )
    for s, e in _block_ranges(lines):
        if any(want.match(lines[j]) for j in range(s, e)):
            return s, e
    return None


def _indent_of_block(lines: list[str], s: int, e: int) -> str:
    """推块的缩进。"""
    for j in range(s + 1, e):
        m = re.match(r"^(\s+)\S", lines[j])
        if m:
            return m.group(1)
    lead = re.match(r"^(\s*)-", lines[s])
    return (lead.group(1) if lead else "") + "  "


def add_server(cfg: ServerConfig, path: Path | None = None) -> None:
    """把服务器追加到 mcp_servers.yaml 末尾。"""
    import yaml as _yaml

    p = path or CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)

    d: dict[str, Any] = {"server_id": cfg.server_id, "transport": cfg.transport}
    if cfg.transport == "http":
        if cfg.url:
            d["url"] = cfg.url
        if cfg.headers:
            d["headers"] = dict(cfg.headers)
    else:
        if cfg.command:
            d["command"] = cfg.command
        if cfg.args:
            d["args"] = list(cfg.args)
        if cfg.env:
            d["env"] = dict(cfg.env)
        if cfg.cwd:
            d["cwd"] = cfg.cwd
    if not cfg.enabled:
        d["enabled"] = False
    if cfg.transport == "stdio" and cfg.command_approved:
        d["command_approved"] = True

    block = _yaml.dump(
        [d], default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    # pyyaml dump 列表会以 "- " 开头，去掉首行的 "[" 包装
    if block.startswith("- "):
        new_lines = block.rstrip("\n").splitlines()
    else:
        # pyyaml 可能输出成 ---\n- ...，去掉文档头
        new_lines = block.rstrip("\n").splitlines()
        # 跳过文档头
        new_lines = [ln for ln in new_lines if not ln.startswith("---")]

    existing = ""
    if p.is_file():
        existing = p.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"

    p.write_text(existing + "\n".join(new_lines) + "\n", encoding="utf-8", newline="\n")


def update_server(
    server_id: str, updates: dict[str, Any], path: Path | None = None
) -> bool:
    """逐行编辑更新服务器字段，返回是否找到。"""
    p = path or CONFIG_PATH
    if not p.is_file():
        return False

    lines = p.read_text(encoding="utf-8").splitlines()
    block = _find_block(lines, server_id)
    if block is None:
        return False

    s, e = block
    indent = _indent_of_block(lines, s, e)

    # 字段到 YAML 行的映射
    replacements: dict[str, str] = {}
    for key, val in updates.items():
        if val is None:
            continue
        if isinstance(val, bool):
            replacements[key] = "true" if val else "false"
        elif isinstance(val, list):
            # 简单列表：放到一行
            import json as _json
            replacements[key] = _json.dumps(val, ensure_ascii=False)
        elif isinstance(val, dict):
            # 字典：简单展开（嵌套不深）
            import json as _json
            replacements[key] = _json.dumps(val, ensure_ascii=False)
        else:
            replacements[key] = str(val)

    # 逐行替换/插入
    changed: set[str] = set()
    for j in range(s, e):
        for key, val_str in replacements.items():
            if key in changed:
                continue
            m = re.match(rf"^(\s*){re.escape(key)}:\s*.*$", lines[j])
            if m:
                lines[j] = f"{m.group(1)}{key}: {val_str}"
                changed.add(key)
                break

    # 未找到的键：插入到块末尾
    insert_at = e
    for key in replacements:
        if key not in changed:
            lines.insert(insert_at, f"{indent}{key}: {replacements[key]}")
            insert_at += 1

    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return True


def remove_server(
    server_id: str, path: Path | None = None
) -> bool:
    """从 YAML 里删除服务器块，返回是否找到。"""
    p = path or CONFIG_PATH
    if not p.is_file():
        return False

    lines = p.read_text(encoding="utf-8").splitlines()
    block = _find_block(lines, server_id)
    if block is None:
        return False

    s, e = block
    # 删掉块，也删掉前面的空白行
    del_start = s
    while del_start > 0 and not lines[del_start - 1].strip():
        del_start -= 1
    del lines[del_start:e]

    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return True


def _set_enabled_textual(path: Path, server_id: str, enabled: bool) -> bool:
    """
    只改目标块里的 enabled 那一行。

    ## 为什么按缩进判断块边界

    顶层是列表，每项以 "- " 开头。下一个同级 "- " 就是下一个服务器 ——
    在此之前的行都属于当前项。这个判断对本项目的配置格式够用，
    而且看不懂的格式会返回 False 而不是写坏文件。

    ## 实测踩到的两个坑

    键名是 `server_id` 不是 `id`。照 "- id:" 写正则的话永远匹配不上，
    而返回值是 False —— 看起来像"配置里没这个服务器"，
    完全不指向"我的正则写错了"。

    行尾注释（`- server_id: github    # 备注`）会让 `$` 锚点失配。
    yaml 里行尾注释很常见，用 `$` 收尾等于要求用户不写注释。
    """
    lines = path.read_text(encoding="utf-8").splitlines()

    # 块的起始行号
    block_starts = [
        i for i, ln in enumerate(lines) if re.match(r"^\s*-\s", ln)
    ]
    if not block_starts:
        return False

    def block_range(idx: int) -> tuple[int, int]:
        s = block_starts[idx]
        e = block_starts[idx + 1] if idx + 1 < len(block_starts) else len(lines)
        return s, e

    # 找 server_id 匹配的块。
    # 值后面允许跟空白和 # 注释 —— 不允许的话有注释的行就匹配不上。
    want = re.compile(
        rf"^\s*(?:-\s+)?server_id:\s*['\"]?{re.escape(server_id)}['\"]?\s*(?:#.*)?$"
    )
    target = -1
    for i in range(len(block_starts)):
        s, e = block_range(i)
        if any(want.match(lines[j]) for j in range(s, e)):
            target = i
            break
    if target < 0:
        return False

    s, e = block_range(target)
    flag = "true" if enabled else "false"

    for j in range(s, e):
        m = re.match(r"^(\s*)enabled:\s*.*$", lines[j])
        if m:
            lines[j] = f"{m.group(1)}enabled: {flag}"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            return True

    # 块里没有 enabled 行 —— 插一行。
    #
    # 缩进照块内其它键对齐。用固定两格的话遇到四格缩进的配置会
    # 生成不合法的 yaml，而那时整个文件都读不了了。
    indent = "  "
    for j in range(s + 1, e):
        m = re.match(r"^(\s+)\S", lines[j])
        if m:
            indent = m.group(1)
            break
    else:
        # 单行块（所有键都在 "- " 那一行之后没有续行）——
        # 照 "- " 的位置推缩进
        lead = re.match(r"^(\s*)-\s", lines[s])
        if lead:
            indent = lead.group(1) + "  "

    lines.insert(s + 1, f"{indent}enabled: {flag}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return True
