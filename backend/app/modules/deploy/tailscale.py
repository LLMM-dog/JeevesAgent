"""
Tailscale 隧道管理。

## 便携化设计（个人本地项目）

1. 先检测系统是否已装 Tailscale（which + 常见路径），有就直接用系统版；
2. 没有才从官方下载【便携二进制】到项目目录 `.tailscale/`，状态与 socket
   也放在项目目录里 —— 删除整个项目目录 = 彻底干净，不污染系统。

所有 CLI 调用：系统版不带参数，便携版统一加 `--socket <项目>/.tailscale/tailscaled.sock`。
路径用 PROJECT_ROOT 推导（绝对路径），与进程启动目录无关 —— 和项目其它路径同一约定。

## 平台差异

- Linux：官方静态 tarball（tailscale + tailscaled），--tun=userspace-networking 无需 root；
- Windows：官方 MSI 用 msiexec 管理安装提取二进制到项目目录；TUN 驱动是内核驱动，
   首次启动 tailscaled 可能弹一次 UAC（这是 OS 限制）；
- macOS：zip 解压 CLI，系统扩展仍需要授权。
"""

import asyncio
import hashlib
import json
import platform
import re
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Any, cast

import httpx
import structlog

from app.core.config import PROJECT_ROOT

log = structlog.get_logger(__name__)

# ── 项目内便携目录（.gitignore 已忽略） ──
_BUNDLED_DIR = PROJECT_ROOT / ".tailscale"
_BIN_DIR = _BUNDLED_DIR / "bin"
_DL_DIR = _BUNDLED_DIR / "download"
_SOCKET = _BUNDLED_DIR / "tailscaled.sock"
_STATE = _BUNDLED_DIR / "tailscaled.state"

# 正在等待登录授权的子进程 / 后台 tailscaled
_login_proc: asyncio.subprocess.Process | None = None
_login_url: str = ""
_daemon_proc: asyncio.subprocess.Process | None = None
_LOG_FILE = _BUNDLED_DIR / "tailscaled.log"

_ARCH_MAP = {
    "x86_64": "amd64", "amd64": "amd64",
    "aarch64": "arm64", "arm64": "arm64",
    "armv7l": "arm", "armv6l": "arm",
    "i386": "386", "i686": "386",
}


def _exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def _system_bin() -> str | None:
    """系统已装的 tailscale CLI（优先用，绝不碰系统安装）。"""
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "win32":
        for p in (
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        ):
            if Path(p).exists():
                return p
    return None


def _bundled_bin() -> Path | None:
    p = _BIN_DIR / _exe("tailscale")
    return p if p.exists() else None


def _bundled_daemon() -> Path | None:
    p = _BIN_DIR / _exe("tailscaled")
    return p if p.exists() else None


def _bin() -> str | None:
    """当前要用的 tailscale CLI：系统版优先，否则便携版。"""
    s = _system_bin()
    if s:
        return s
    b = _bundled_bin()
    return str(b) if b else None


def _is_bundled() -> bool:
    """当前是否用项目内便携版（决定是否加 --socket）。"""
    return _system_bin() is None and _bundled_bin() is not None


def _socket_args() -> list[str]:
    # Windows 的 --socket 是命名管道（不是文件路径），且默认管道在
    # ProtectedPrefix\Administrators 下、必须由管理员 tailscaled 创建。
    # 所以 Windows 不传 --socket（CLI 自动连默认管道），只有 Linux/macOS
    # 才用项目目录里的 unix socket 文件。
    if _is_bundled() and sys.platform != "win32":
        return ["--socket", str(_SOCKET)]
    return []


async def _run_cmd(args: list[str], timeout_s: float = 8.0) -> tuple[int, str]:
    """跑任意命令，返回 (exit_code, output)。

    超时时保留已输出的内容 —— 像 tailscale serve 会先打印
    "需要启用 Serve + 链接"再阻塞等待，之前超时把这些关键提示吞掉了。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
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
        text = buf.decode("utf-8", errors="replace").strip()
        return (124, text or "命令超时（无输出）")
    await proc.wait()
    return (proc.returncode or 0, buf.decode("utf-8", errors="replace").strip())


async def _run(args: list[str], timeout_s: float = 8.0) -> tuple[int, str]:
    """跑一条 tailscale 命令（自动加 socket）。"""
    exe = _bin()
    if exe is None:
        return (127, "tailscale 未安装")
    return await _run_cmd([exe, *_socket_args(), *args], timeout_s)


def _parse_json(text: str) -> dict[str, Any]:
    """容忍输出噪音，解析失败返回空 dict。"""
    if not text:
        return {}
    try:
        return cast(dict[str, Any], json.loads(text))
    except json.JSONDecodeError:
        idx = text.find("{")
        if idx >= 0:
            try:
                return cast(dict[str, Any], json.loads(text[idx:]))
            except json.JSONDecodeError:
                return {}
        return {}


# ─────────────────────────── 便携版安装 ───────────────────────────


async def _asset_url() -> tuple[str, str]:
    """从官方 JSON 索引取当前平台/架构的包 URL，返回 (url, kind)。kind: tgz|msi|maczip。"""
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        r = await client.get("https://pkgs.tailscale.com/stable/?mode=json")
        r.raise_for_status()
        data = r.json()
    arch = _ARCH_MAP.get(platform.machine().lower(), "amd64")
    if sys.platform == "win32":
        msis = data.get("MSIs") or {}
        name = msis.get("amd64" if arch in ("amd64", "x86_64") else arch) or msis.get("amd64")
        return f"https://pkgs.tailscale.com/stable/{name}", "msi"
    if sys.platform == "darwin":
        zips = data.get("MacZips") or {}
        name = zips.get("universal")
        return f"https://pkgs.tailscale.com/stable/{name}", "maczip"
    tb = data.get("Tarballs") or {}
    name = tb.get(arch) or tb.get("amd64")
    return f"https://pkgs.tailscale.com/stable/{name}", "tgz"


async def _download(url: str, dest: Path) -> tuple[bool, str]:
    """下载到项目目录，并尽量校验 sha256。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
    except Exception as e:  # noqa: BLE001
        return False, f"下载失败：{e}"

    # sha256 校验（拿不到校验文件则跳过，不阻断）
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            r = await client.get(url + ".sha256")
            if r.status_code == 200:
                expect = r.text.strip().split()[0].strip()
                actual = hashlib.sha256(dest.read_bytes()).hexdigest()
                if expect and actual != expect:
                    return False, "校验和不匹配，已放弃（可重试）"
    except Exception:  # noqa: BLE001
        pass
    return True, ""


def _extract_tgz(archive: Path) -> tuple[bool, str]:
    """Linux 静态 tarball：提取 tailscale / tailscaled 到 bin。"""
    _BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():
                name = Path(m.name).name
                if name not in ("tailscale", "tailscaled"):
                    continue
                src = tf.extractfile(m)
                if src is None:
                    continue
                dest = _BIN_DIR / _exe(name)
                dest.write_bytes(src.read())
                if sys.platform != "win32":
                    dest.chmod(0o755)
    except Exception as e:  # noqa: BLE001
        return False, f"解压失败：{e}"
    return True, ""


async def _extract_msi(archive: Path) -> tuple[bool, str]:
    """Windows MSI：msiexec 管理安装提取，再拷二进制到 bin。"""
    extract = _BUNDLED_DIR / "msi_extract"
    code, out = await _run_cmd(
        ["msiexec", "/a", str(archive), "/qn", f"TARGETDIR={extract}"],
        timeout_s=180,
    )
    if code != 0:
        return False, f"MSI 提取失败：{out[:300]}"
    _BIN_DIR.mkdir(parents=True, exist_ok=True)
    # wintun.dll 必须一起复制：它内嵌了 TUN 驱动，tailscaled 创建
    # 虚拟网卡时从同目录加载它（少了它会一直 tstun.New 失败）。
    for name in ("tailscale.exe", "tailscaled.exe", "wintun.dll"):
        for found in extract.rglob(name):
            shutil.copy2(found, _BIN_DIR / name)
            break
    return True, ""


async def ensure_bundled() -> tuple[bool, str]:
    """确保 Tailscale 可用：系统已有直接用；否则下载便携版到项目目录。"""
    if _system_bin():
        return True, "检测到系统已装 Tailscale，直接使用"
    if _bundled_bin() is not None:
        return True, "便携版已就绪（位于项目 .tailscale/，删除项目目录即彻底卸载）"
    try:
        url, kind = await _asset_url()
    except Exception as e:  # noqa: BLE001
        return False, f"获取版本信息失败：{e}"
    archive = _DL_DIR / Path(url).name
    ok, err = await _download(url, archive)
    if not ok:
        return False, err
    if kind == "tgz":
        ok, err = _extract_tgz(archive)
    elif kind == "msi":
        ok, err = await _extract_msi(archive)
    else:
        return False, "macOS 便携版请先用系统安装（brew install --cask tailscale）"
    if not ok:
        return False, err
    if _bundled_bin() is not None:
        return True, "便携版已安装到项目目录 .tailscale/"
    return False, "解压后未找到 tailscale 二进制"


async def _install_windows_official() -> tuple[bool, str]:
    """Windows 上安装官方 Tailscale（系统服务，稳定可靠）。

    不依赖 winget（很多系统没有）：直接下载官方 MSI，
    用 msiexec /qn 静默安装（弹一次 UAC）。装完系统服务自动启动。
    """
    try:
        url, _ = await _asset_url()  # win32 下返回 msi
    except Exception as e:  # noqa: BLE001
        return False, f"获取下载地址失败：{e}"
    archive = _DL_DIR / Path(url).name
    ok, err = await _download(url, archive)
    if not ok:
        return False, err
    # 提权静默安装（-Wait 等它装完再返回）
    ps = (
        "Start-Process -FilePath 'msiexec.exe' "
        + "-ArgumentList '/i','" + str(archive) + "','/qn' "
        + "-Verb RunAs -Wait"
    )
    code, out = await _run_cmd(
        ["powershell", "-NoProfile", "-Command", ps], timeout_s=300
    )
    if code != 0:
        return False, f"安装失败：{out[:300]}"
    if _system_bin() is not None:
        return True, "官方 Tailscale 已安装，页面会自动刷新"
    return True, "官方 Tailscale 安装完成（稍候刷新识别）"


async def install() -> tuple[bool, str]:
    """确保 Tailscale 可用：系统已有直接用；Windows 装官方版，其它平台便携版。"""
    if _system_bin():
        return True, "检测到系统已装 Tailscale，直接使用"
    if sys.platform == "win32":
        return await _install_windows_official()
    return await ensure_bundled()


def _system_proxy() -> str | None:
    """读用户系统代理（Clash 等），返回 http://host:port。

    关键：Clash 通常设置的是 WinINET 代理（浏览器用），而 tailscaled
    走 WinHTTP —— Windows 里这是两套独立配置，导致 tailscaled 不走代理。
    这里读 WinINET 注册表，把代理地址用环境变量传给 tailscaled。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as k:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if not enable:
                return None
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        if not server:
            return None
        host_port = str(server).split(";")[0].strip()
        if host_port.startswith("http") or host_port.startswith("socks"):
            return host_port
        return f"http://{host_port}"
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────── 后台 tailscaled ───────────────────────────


async def _daemon_running() -> bool:
    """判断 tailscaled 是否在跑并响应。

    【不能用 status 的退出码】—— 未登录时 status 返回非零（它反映的是
    登录状态，不是守护进程是否在运行）。改看输出：连不上 daemon 时 CLI
    会打印 "failed to connect to local tailscaled"。
    """
    if _system_bin():
        return True  # 系统版有系统服务
    code, out = await _run_cmd(
        [_bin() or "tailscale", *_socket_args(), "status"], timeout_s=5
    )
    if code == 0:
        return True
    low = out.lower()
    return "failed to connect" not in low and "cannot connect" not in low and bool(out.strip())


_winhttp_synced = False


async def _ensure_winhttp_proxy() -> None:
    """让官方版 tailscaled（系统服务）走用户的 Clash 代理。

    系统服务走 WinHTTP，与浏览器用的 WinINET 是两套代理。用户开着 Clash
    时只设置了 WinINET，WinHTTP 还是直连 —— 导致 DERP 中继（数据平面）
    连不上，serve 的 URL 打不开。这里把 WinINET 代理同步到 WinHTTP。
    """
    global _winhttp_synced
    if _winhttp_synced or sys.platform != "win32":
        return
    _winhttp_synced = True  # 每次进程生命周期只尝试一次
    proxy = _system_proxy()
    if not proxy:
        return
    host_port = proxy.replace("http://", "").replace("https://", "").split("/")[0]
    code, out = await _run_cmd(["netsh", "winhttp", "show", "proxy"], timeout_s=10)
    if code == 0 and host_port in out:
        return  # 已同步，无需再设
    ps = (
        "Start-Process -FilePath 'netsh.exe' "
        + "-ArgumentList 'winhttp','set','proxy','" + host_port + "' "
        + "-Verb RunAs -Wait -WindowStyle Hidden"
    )
    code, out = await _run_cmd(
        ["powershell", "-NoProfile", "-Command", ps], timeout_s=30
    )
    if code != 0:
        log.warning("winhttp_proxy_sync_failed", detail=out[:200])
    else:
        log.info("winhttp_proxy_synced", proxy=host_port)


async def _ensure_daemon() -> tuple[bool, str]:
    """便携版：确保项目内 tailscaled 在跑（socket/state 都在项目目录）。"""
    global _daemon_proc
    if not _is_bundled():
        # 系统版：同步 WinHTTP 代理，否则数据平面（DERP）连不上
        await _ensure_winhttp_proxy()
        return True, ""
    daemon = _bundled_daemon()
    if daemon is None:
        return False, "便携版 tailscaled 未找到，请先点安装"
    if await _daemon_running():
        return True, ""

    _BUNDLED_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # Windows tailscaled 必须管理员运行（默认命名管道在
        # ProtectedPrefix\Administrators 下）。用 PowerShell 提权启动，
        # 弹一次 UAC；不传 --socket，CLI 自动连默认管道。
        # 输出重定向到项目目录，错误不再被吞掉。
        proxy = _system_proxy()
        if proxy:
            # 用 cmd 先把环境变量设好再启动 tailscaled，让它走用户的代理
            run_cmd = (
                "set HTTP_PROXY=" + proxy + "&&set HTTPS_PROXY=" + proxy + "&&\""
                + str(daemon) + "\" --state \""
                + str(_STATE) + "\" --tun=userspace-networking"
            )
            ps = (
                "Start-Process -FilePath 'cmd.exe' "
                + "-ArgumentList '/c','" + run_cmd.replace("'", "''") + "' "
                + "-Verb RunAs -WindowStyle Hidden"
            )
        else:
            ps = (
                "Start-Process -FilePath '" + str(daemon).replace("'", "''") + "' "
                + "-ArgumentList '--state','" + str(_STATE).replace("'", "''") + "','--tun=userspace-networking' "
                + "-Verb RunAs -WindowStyle Hidden "
                + "-RedirectStandardError '" + str(_LOG_FILE).replace("'", "''") + "' "
                + "-RedirectStandardOutput '" + str(_LOG_FILE).replace("'", "''") + "'"
            )
        code, out = await _run_cmd(
            ["powershell", "-NoProfile", "-Command", ps], timeout_s=60
        )
        if code != 0:
            return False, f"tailscaled 启动失败（需要管理员授权）：{out[:200]}"
    else:
        args = [str(daemon), "--socket", str(_SOCKET), "--state", str(_STATE)]
        if sys.platform != "darwin":
            args.append("--tun=userspace-networking")
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            _daemon_proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=_LOG_FILE.open("ab"),
            )
        except Exception as e:  # noqa: BLE001
            return False, f"启动 tailscaled 失败：{e}"

    for _ in range(60):
        await asyncio.sleep(0.25)
        if await _daemon_running():
            return True, ""
    return False, "tailscaled 启动超时（Windows 请在系统弹出的管理员授权窗口点“是”）"


# ─────────────────────────── 状态 / 隧道 ───────────────────────────


def _read_log(tail: int = 40) -> str:
    """读 tailscaled 的错误输出（项目日志 + Windows 系统日志），给用户看。"""
    lines: list[str] = []
    if _LOG_FILE.exists():
        try:
            lines += _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32":
        logdir = Path("C:/ProgramData/Tailscale/Logs")
        try:
            files = sorted(
                logdir.glob("tailscale-service-*.txt"), key=lambda p: p.stat().st_mtime
            )
            if files:
                lines += ["", "--- 系统日志（C:/ProgramData/Tailscale/Logs）---"]
                lines += files[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines)[-4000:]


async def ensure_daemon() -> tuple[bool, str]:
    """公开入口：确保 tailscaled 在跑（Windows 会触发一次 UAC 提权）。"""
    return await _ensure_daemon()


async def get_status() -> dict[str, Any]:
    """聚合状态：安装 / 登录 / serve / funnel / 设备名 / URL。"""
    if _bin() is None:
        return {
            "installed": False,
            "installed_hint": "未检测到 Tailscale（点“安装”下载便携版到项目目录）",
        }
    ok, err = await _ensure_daemon()
    if not ok:
        return {
            "installed": True,
            "backend_state": "daemon_failed",
            "logged_in": False,
            "installed_hint": err or "tailscaled 未能启动",
            "daemon_error": _read_log(),
        }
    code, out = await _run(["status", "--json"])
    st = _parse_json(out)
    backend_state = str(st.get("BackendState", "unknown"))
    self_info = st.get("Self", {}) or {}
    dns_name = str(self_info.get("DNSName", "")).rstrip(".")

    _, serve_out = await _run(["serve", "status", "--json"])
    _, funnel_out = await _run(["funnel", "status", "--json"])

    return {
        "installed": True,
        "bundled": _is_bundled(),
        "backend_state": backend_state,
        "logged_in": backend_state == "Running",
        "device_name": dns_name,
        "ipv4": (self_info.get("TailscaleIPs") or [""])[0],
        "serve": _parse_serve(serve_out, funnel_out),
        # 当前待授权的登录链接（tailscale login 后台抓到的）。
        # 前端轮询这里，不再依赖单次 start_login 的返回时机。
        "login_url": _login_url,
        "daemon_error": _read_log() if backend_state not in ("Running", "NeedsLogin") else "",
    }


def _parse_serve(serve_out: str, funnel_out: str) -> dict[str, Any]:
    s = _parse_json(serve_out)
    f = _parse_json(funnel_out)

    def _serve_on(cfg: dict[str, Any]) -> bool:
        # 实际结构：Handlers 在 "Web" 段（每个站点一条），不是 TCP 段。
        web = cfg.get("Web", {}) or {}
        for site in web.values():
            if site and site.get("Handlers"):
                return True
        # 兼容：TCP 段有 HTTPS=true 也视为已开启
        tcp = cfg.get("TCP", {}) or {}
        for entry in tcp.values():
            if entry and entry.get("HTTPS"):
                return True
        return False

    def _funnel_enabled(cfg: dict[str, Any]) -> bool:
        # funnel 开启的标志是 "AllowFunnel" 段（值为 true），
        # 不是 TCP 段里的 TailscaleFunnel.Enabled。
        allow = cfg.get("AllowFunnel", {}) or {}
        for enabled in allow.values():
            if enabled:
                return True
        return False

    return {
        "serve_on": _serve_on(s),
        "funnel_on": _funnel_enabled(f) or _funnel_enabled(s),
    }


def _serve_enable_url(out: str) -> str:
    """从 serve/funnel 输出里抓「启用」授权链接（/f/serve 或 /f/funnel）。"""
    m = re.search(r"https://login\.tailscale\.com/f/(?:serve|funnel)[^\s]+", out)
    return m.group(0) if m else ""


async def _open_enable_page(url: str) -> None:
    """自动打开浏览器到启用页（一次性授权）。"""
    try:
        import webbrowser

        await asyncio.to_thread(webbrowser.open, url)
    except Exception:  # noqa: BLE001
        pass


async def start_serve(port: int) -> tuple[bool, str]:
    ok, err = await _ensure_daemon()
    if not ok:
        return False, err
    code, out = await _run(["serve", "--bg", str(port)], timeout_s=20)
    if code == 0:
        return True, out
    url = _serve_enable_url(out)
    if url:
        await _open_enable_page(url)
        return False, (
            "需要先启用 Tailscale Serve（已自动打开浏览器，点 Enable 即可），"
            "启用后回来再点一次“开启 serve”。启用链接：" + url
        )
    return False, out or "开启失败"


async def stop_serve() -> tuple[bool, str]:
    ok, err = await _ensure_daemon()
    if not ok:
        return False, err
    code, out = await _run(["serve", "reset"])
    if code != 0:
        code, out = await _run(["serve", "--bg=false", "443"])
    return code == 0, out


async def start_funnel(port: int) -> tuple[bool, str]:
    ok, err = await _ensure_daemon()
    if not ok:
        return False, err
    code, out = await _run(["funnel", "--bg", str(port)], timeout_s=20)
    if code == 0:
        return True, out
    url = _serve_enable_url(out)
    if url:
        await _open_enable_page(url)
        return False, (
            "需要先启用 Tailscale Funnel（已自动打开浏览器，点 Enable 即可），"
            "启用后回来再点一次“开启 funnel”。启用链接：" + url
        )
    return False, out or "开启失败"


async def stop_funnel() -> tuple[bool, str]:
    ok, err = await _ensure_daemon()
    if not ok:
        return False, err
    code, out = await _run(["funnel", "off"])
    return code == 0, out


# ─────────────────────────── 登录 ───────────────────────────


def _extract_login_url(text: str) -> str:
    """从 tailscale login 输出里抓授权 URL。"""
    for m in re.finditer(r"https://login\.tailscale\.com/\S+", text):
        return m.group(0)
    return ""


async def start_login() -> tuple[bool, str]:
    global _login_proc, _login_url
    ok, err = await _ensure_daemon()
    if not ok:
        return False, err
    exe = _bin()
    if exe is None:
        return False, "tailscale 未安装"
    if _login_proc is not None and _login_proc.returncode is None:
        return True, _login_url or "正在等待登录授权…"
    _login_url = ""
    _login_proc = await asyncio.create_subprocess_exec(
        exe, *_socket_args(), "login",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout = _login_proc.stdout
    assert stdout is not None

    async def _collect() -> None:
        global _login_url
        try:
            while True:
                chunk = await stdout.readline()
                if not chunk:
                    break
                url = _extract_login_url(chunk.decode("utf-8", errors="replace"))
                if url:
                    _login_url = url
        except Exception:  # noqa: BLE001
            pass

    asyncio.get_running_loop().create_task(_collect())
    # 多等一会儿：tailscale login 要先连控制服务器（controlplane.tailscale.com），
    # 网络慢时 URL 可能 30 秒甚至更久才出。
    for _ in range(40):
        await asyncio.sleep(0.25)
        if _login_url:
            break
        if _login_proc.returncode is not None:
            break
    if not _login_url:
        if _login_proc.returncode is not None:
            # login 秒退且无 URL：多半是状态文件损坏（反复异常启动导致）。
            return False, "登录进程提前退出且未取得授权链接——多为状态文件损坏，删除项目内 .tailscale/tailscaled.state 后重试"
        return True, "正在连接 Tailscale 服务器获取授权链接（网络慢时约 30s~1min）——稍后点“刷新”，链接会出现在下方"
    return True, _login_url
