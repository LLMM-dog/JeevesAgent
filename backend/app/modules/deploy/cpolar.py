"""
cpolar 内网穿透（公网暴露主推方案，大陆服务器、免装客户端即可访问）。

## 为什么主推 cpolar

Tailscale 的控制面/中继/边缘节点都在海外，大陆直连不稳（手机不挂代理
打不开 funnel）。cpolar 服务器在国内，公网 URL 大陆直接可达，
访问方浏览器输入 URL 即可，无需装任何东西。

## 便携化

- 二进制下载到项目目录 .cpolar/（随项目走，删除即干净）
- authtoken 写进 cpolar 默认配置（~/.cpolar/cpolar.yml），这是账号凭证，
  与项目文件分离，删除项目不影响 cpolar 账号
"""

import asyncio
import platform
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.core.config import PROJECT_ROOT

log = structlog.get_logger(__name__)

_DIR = PROJECT_ROOT / ".cpolar"
_BIN_DIR = _DIR / "bin"
_DL_DIR = _DIR / "download"
_LOG_FILE = _DIR / "cpolar.log"

# cpolar 官网下载地址模板（版本号可在此调整）。
_VERSION = "3.3.12"
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
    platform.machine().lower(), "amd64"
)

_proc: asyncio.subprocess.Process | None = None
_tunnel_url: str = ""


def _exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def _system_bin() -> str | None:
    """系统已装的 cpolar（优先用）。"""
    return shutil.which("cpolar")


def _bundled_bin() -> Path | None:
    p = _BIN_DIR / _exe("cpolar")
    return p if p.exists() else None


def _bin() -> str | None:
    s = _system_bin()
    if s:
        return s
    b = _bundled_bin()
    return str(b) if b else None


def _download_url() -> str:
    if sys.platform == "win32":
        return f"https://www.cpolar.com/static/downloads/releases/{_VERSION}/cpolar-stable-windows-{_ARCH}.zip"
    if sys.platform == "darwin":
        return f"https://www.cpolar.com/static/downloads/releases/{_VERSION}/cpolar-stable-darwin-{_ARCH}.zip"
    return f"https://www.cpolar.com/static/downloads/releases/{_VERSION}/cpolar-stable-linux-{_ARCH}.zip"


async def _run_cmd(args: list[str], timeout_s: float = 8.0) -> tuple[int, str]:
    """跑任意命令，超时保留已输出内容。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError:
        return (127, f"找不到命令: {args[0]}")
    except Exception as e:  # noqa: BLE001
        return (1, f"执行失败: {e}")
    assert proc.stdout is not None
    buf = bytearray()
    try:
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=timeout_s)
            if not chunk:
                break
            buf += chunk
    except TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return (124, buf.decode("utf-8", errors="replace").strip() or "命令超时")
    await proc.wait()
    return (proc.returncode or 0, buf.decode("utf-8", errors="replace").strip())


async def _run(args: list[str], timeout_s: float = 8.0) -> tuple[int, str]:
    exe = _bin()
    if exe is None:
        return (127, "cpolar 未安装")
    return await _run_cmd([exe, *args], timeout_s)


async def install() -> tuple[bool, str]:
    """下载 cpolar 客户端到项目 .cpolar/ 并解压。"""
    if _bin() is not None:
        return True, "cpolar 已就绪"
    _DL_DIR.mkdir(parents=True, exist_ok=True)
    url = _download_url()
    archive = _DL_DIR / Path(url).name
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=180) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                with archive.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
    except Exception as e:  # noqa: BLE001
        return False, f"下载失败：{e}"
    _BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            for m in zf.namelist():
                if m.endswith(_exe("cpolar")) or Path(m).name == _exe("cpolar"):
                    dest = _BIN_DIR / _exe("cpolar")
                    dest.write_bytes(zf.read(m))
                    if sys.platform != "win32":
                        dest.chmod(0o755)
                    break
    except Exception as e:  # noqa: BLE001
        return False, f"解压失败：{e}"
    if _bundled_bin() is not None:
        return True, "cpolar 已安装到项目 .cpolar/"
    return False, "解压后未找到 cpolar 可执行文件"


async def set_authtoken(token: str) -> tuple[bool, str]:
    """配置账号 token（cpolar 注册后控制台拿到）。"""
    token = token.strip()
    if len(token) < 8:
        return False, "authtoken 太短，请检查后重填"
    code, out = await _run(["authtoken", token], timeout_s=20)
    return (code == 0), out


def _extract_url(text: str) -> str:
    """从 cpolar 输出里抓公网 URL。

    兼容各种版本/域名格式：https://xxx.r1.cpolar.top、
    https://xxx.cpolar.top、https://xxx.cpolar.cn 等，优先 https。
    """
    m = re.search(r"https://[a-zA-Z0-9\-]+\.(?:[a-z0-9]+\.)?cpolar\.(?:top|cn|io)", text)
    if m:
        return m.group(0)
    m = re.search(r"http://[a-zA-Z0-9\-]+\.(?:[a-z0-9]+\.)?cpolar\.(?:top|cn|io)", text)
    return m.group(0) if m else ""


async def start_http(port: int) -> tuple[bool, str]:
    """后台开启 HTTP 隧道，返回公网 URL。"""
    global _proc, _tunnel_url
    if _proc is not None and _proc.returncode is None:
        return True, _tunnel_url or "隧道已在运行"
    exe = _bin()
    if exe is None:
        return False, "cpolar 未安装"
    _tunnel_url = ""
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logf = _LOG_FILE.open("ab")
    _proc = await asyncio.create_subprocess_exec(
        # -log stdout：cpolar 默认 -log none，URL 不往 stdout 输出，
        # 会导致解析不到公网地址。显式让它输出到 stdout。
        exe, "http", str(port), "-log", "stdout",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout = _proc.stdout
    assert stdout is not None

    async def _collect() -> None:
        global _tunnel_url
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                logf.write(line)
                url = _extract_url(line.decode("utf-8", errors="replace"))
                if url:
                    _tunnel_url = url
        except Exception:  # noqa: BLE001
            pass

    asyncio.get_running_loop().create_task(_collect())
    # 等 URL 出现（cpolar 启动 + 建隧道需要几秒）
    for _ in range(60):
        await asyncio.sleep(0.25)
        if _tunnel_url:
            break
        if _proc.returncode is not None:
            break
    if _tunnel_url:
        return True, _tunnel_url
    return False, "隧道启动失败（请确认 authtoken 已配置）"


async def stop() -> tuple[bool, str]:
    global _proc, _tunnel_url
    if _proc is not None and _proc.returncode is None:
        _proc.terminate()
    _proc = None
    _tunnel_url = ""
    return True, ""


def _authtoken_configured() -> bool:
    """token 是否已持久保存（~/.cpolar/cpolar.yml）。"""
    p = Path.home() / ".cpolar" / "cpolar.yml"
    try:
        return "authtoken:" in p.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return False


async def status() -> dict[str, Any]:
    """cpolar 状态：安装 / token / 隧道 / URL。"""
    running = _proc is not None and _proc.returncode is None
    return {
        "installed": _bin() is not None,
        "authtoken_configured": _authtoken_configured(),
        "running": running,
        "url": _tunnel_url,
    }
