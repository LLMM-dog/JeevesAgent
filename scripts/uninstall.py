"""
卸载：清掉项目文件夹【带不走】的东西。

## 为什么需要这个脚本

项目自己的数据全在文件夹内（`data/`、`workspace/`、`.venv/`、
`node_modules/`），删文件夹就没了。审计确认过：没有写家目录、
没有注册表、没有计划任务、没有浏览器 localStorage。

但有三类东西删文件夹带不走：

1. **Docker 容器** —— 项目自己创建的（`jeeves-*`）。用户不知道它们
   存在，而它们会一直占内存。这是最该清的一项。
2. **Docker 镜像** —— `python:3.12-slim`，约 130MB。用户手动 pull 的，
   所以默认不删（可能别的项目也在用），只提示。
3. **包管理器缓存** —— uv 和 npm 在用户目录下的缓存。工具自身行为，
   默认不删（删了别的项目要重新下载），只提示。

## 用法

    uv run python scripts/uninstall.py          # 只清项目自己创建的
    uv run python scripts/uninstall.py --all    # 连缓存和镜像一起
    uv run python scripts/uninstall.py --dry-run

清完之后删掉整个项目文件夹即可。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTAINER_PREFIX = "jeeves-"
TAILSCALE_DIR = ROOT / ".tailscale"
IMAGE = "python:3.12-slim"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def sh(args: list[str], timeout: float = 60) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 输出)。找不到命令返回 (127, '')。"""
    exe = shutil.which(args[0])
    if exe is None:
        return 127, ""
    try:
        r = subprocess.run(
            [exe, *args[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def ask_yes(prompt: str, *, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        got = input(f"  {prompt} {hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        say()
        return False
    if not got:
        return default
    return got in ("y", "yes", "是")


# ───────────────────────── 1. Tailscale 便携版 ─────────────────────────


def _kill_tailscaled() -> None:
    """停掉项目内的 tailscaled（便携版，socket/state 都在 .tailscale/）。"""
    if sys.platform == "win32":
        ps_cmd = (
            'Get-CimInstance Win32_Process -Filter "Name=\'tailscaled.exe\'" | '
            + f"Where-Object {{ $_.CommandLine -like '*{ROOT}*' }} | "
            + "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        code, _ = sh(["powershell", "-NoProfile", "-Command", ps_cmd], timeout=30)
        if code == 0:
            say("   ✓ 已停止 tailscaled 进程")
        else:
            say("   （没有在跑的 tailscaled）")
    else:
        code, _ = sh(["pkill", "-f", str(TAILSCALE_DIR)], timeout=30)
        say("   ✓ 已停止 tailscaled 进程" if code == 0 else "   （没有在跑的 tailscaled）")


def clean_tailscale(dry: bool) -> None:
    """
    清理便携版 Tailscale。

    便携版用 --tun=userspace-networking（纯用户态），不装内核 TUN 驱动、
    不写注册表、不装服务 —— 所以停进程 + 删 .tailscale/ 就彻底干净。
    """
    say("1. Tailscale 便携版")
    if not TAILSCALE_DIR.exists():
        say("   没有便携版（未安装或已清理）")
        return

    # 找便携 CLI
    exe = None
    for name in ("tailscale.exe", "tailscale"):
        cand = TAILSCALE_DIR / "bin" / name
        if cand.exists():
            exe = cand
            break

    if exe is not None:
        sock = TAILSCALE_DIR / "tailscaled.sock"
        if dry:
            say(f"   [dry-run] 会执行：{exe.name} --socket {sock.name} logout")
        else:
            code, _ = sh([str(exe), "--socket", str(sock), "logout"], timeout=30)
            say("   ✓ 已从 Tailscale 账号移除该节点" if code == 0 else "   （节点未登录或已离线，跳过）")

    if dry:
        say(f"   [dry-run] 会停止 tailscaled 并删除 {TAILSCALE_DIR}")
    else:
        _kill_tailscaled()
        shutil.rmtree(TAILSCALE_DIR, ignore_errors=True)
        say("   ✓ 已删除项目内 .tailscale/（二进制、登录状态、socket）")


def clean_system_tailscale(dry: bool) -> None:
    """卸载【系统版】Tailscale（官方安装的）。

    tailscale uninstall 会：登出该节点、停服务、卸载服务、删 TUN 驱动。
    便携版（.tailscale/）不在这里处理，见 clean_tailscale。
    """
    exe = shutil.which("tailscale")
    if exe is None and sys.platform == "win32":
        for p in (r"C:\Program Files\Tailscale\tailscale.exe",):
            if Path(p).exists():
                exe = p
                break
    if exe is None:
        return

    code, _ = sh([exe, "version"], timeout=15)
    if code != 0:
        return  # 不是官方系统版，或不可用

    say()
    say("1b. 系统版 Tailscale")
    if dry:
        say(f"   [dry-run] 会执行：{exe} uninstall")
        return
    code, out = sh([exe, "uninstall"], timeout=120)
    say("   ✓ 已卸载系统版 Tailscale" if code == 0 else f"   ⚠ 卸载返回非零（{out.strip()[:150]}），可用 winget uninstall Tailscale.Tailscale 再试")

# ───────────────────────── 1. Docker 容器 ─────────────────────────


def clean_containers(dry: bool) -> int:
    """
    删项目自己创建的容器。

    这是唯一【必须】清的一项：容器是项目创建的，用户不知道它们存在，
    而且删了项目文件夹之后就再也没有东西会去清理它们
    （启动时的 cleanup_orphans 依赖"下次启动"，而已经没有下次了）。
    """
    say("2. Docker 容器（项目创建的）")
    code, out = sh(["docker", "ps", "-aq", "--filter", f"name={CONTAINER_PREFIX}"])
    if code == 127:
        say("   跳过：没装 Docker")
        return 0
    if code != 0:
        say("   跳过：Docker 守护进程没在跑")
        return 0

    ids = [x.strip() for x in out.splitlines() if x.strip()]
    if not ids:
        say("   没有遗留容器")
        return 0

    say(f"   找到 {len(ids)} 个 {CONTAINER_PREFIX}* 容器")
    if dry:
        say(f"   [dry-run] 会执行：docker rm -f {' '.join(i[:12] for i in ids)}")
        return len(ids)

    code, out = sh(["docker", "rm", "-f", *ids], timeout=120)
    if code == 0:
        say(f"   ✓ 已删除 {len(ids)} 个")
        return len(ids)
    say(f"   ✗ 删除失败：{out.strip()[:150]}")
    return 0


# ───────────────────────── 2. Docker 镜像 ─────────────────────────


def clean_image(dry: bool, force: bool) -> None:
    """
    删镜像。默认不删 —— 别的项目可能也在用 python:3.12-slim。
    """
    say()
    say("3. Docker 镜像")
    code, out = sh(["docker", "images", "-q", IMAGE])
    if code == 127 or code != 0:
        say("   跳过：Docker 不可用")
        return
    if not out.strip():
        say(f"   本地没有 {IMAGE}")
        return

    say(f"   本地有 {IMAGE}（约 130MB）")
    if not force:
        say("   保留 —— 别的项目可能也在用它")
        say(f"   要删：docker rmi {IMAGE}")
        return
    if dry:
        say(f"   [dry-run] 会执行：docker rmi {IMAGE}")
        return
    code, out = sh(["docker", "rmi", IMAGE], timeout=120)
    say(f"   ✓ 已删除 {IMAGE}" if code == 0 else "   ✗ 失败（可能有容器在用）")


# ───────────────────────── 3. 包管理器缓存 ─────────────────────────


def clean_caches(dry: bool, force: bool) -> None:
    """
    清 uv / npm 缓存。默认不清 —— 这是工具的全局缓存，
    清了别的项目也要重新下载。
    """
    say()
    say("4. 包管理器缓存（uv / npm）")
    if not force:
        say("   保留 —— 这是全局缓存，清了别的项目要重新下载")
        say("   要清：uv cache clean  且  npm cache clean --force")
        return

    for name, args in (("uv", ["uv", "cache", "clean"]), ("npm", ["npm", "cache", "clean", "--force"])):
        if dry:
            say(f"   [dry-run] 会执行：{' '.join(args)}")
            continue
        code, _ = sh(args, timeout=180)
        say(f"   ✓ {name} 缓存已清" if code == 0 else f"   ⚠ {name} 缓存清理失败（不影响卸载）")


# ───────────────────────── 4. 残留进程 ─────────────────────────


def check_processes() -> None:
    """
    检查有没有还在跑的后端。

    进程随终端走（启动脚本不注册服务），但被 kill -9 或断电后
    会留下孤儿进程占着端口。
    """
    say()
    say("5. 残留进程")
    try:
        sys.path.insert(0, str(ROOT / "backend"))
        from app.core.config import settings

        port = settings.port
    except Exception:
        port = 9000

    if sys.platform == "win32":
        code, out = sh(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                "-ErrorAction SilentlyContinue).OwningProcess",
            ]
        )
    else:
        code, out = sh(["lsof", "-ti", f":{port}"])

    pids = [x.strip() for x in out.splitlines() if x.strip().isdigit()]
    if pids:
        say(f"   ⚠ 端口 {port} 仍被占用（PID {', '.join(pids)}）")
        say("   先停掉它：关掉运行 start 的终端，或结束该进程")
    else:
        say(f"   端口 {port} 没有残留")


# ───────────────────────── 5. 项目外的文件 ─────────────────────────


def warn_outside_files() -> None:
    """
    提示 agent 可能写到项目外的文件。

    这是唯一【无法自动清理】的一项：路径白名单默认只含项目内目录，
    但用户可以在设置页把任意目录加进来，加了之后文件工具就能写进去。
    而 run_shell 压根不受白名单约束 —— 它能做的事没有上界。
    """
    say()
    say("6. agent 可能写到项目外的文件")
    say("   默认只能写项目内的 workspace/。但两种情况会写到外面：")
    say("     · 你在设置页把别的目录加进了路径白名单")
    say("     · agent 用 run_shell 执行过命令（shell 不受白名单约束）")

    db = ROOT / "data" / "jeeves.db"
    if db.is_file():
        say(f"   删项目前想查的话：{db} 里有完整的执行记录")
    say("   这一项【无法自动清理】—— 只有你知道 agent 动过什么")


def main() -> int:
    ap = argparse.ArgumentParser(description="清掉项目文件夹带不走的东西")
    ap.add_argument("--all", action="store_true", help="连 Docker 镜像和包缓存一起清")
    ap.add_argument("--dry-run", action="store_true", help="只打印会做什么")
    ap.add_argument("-y", "--yes", action="store_true", help="不问直接做")
    args = ap.parse_args()

    say("Jeeves 卸载")
    say(f"项目目录：{ROOT}")
    say()
    say("项目自己的数据（data/ workspace/ .venv/ node_modules/）都在这个")
    say("文件夹里，删掉文件夹就没了。这个脚本清的是【带不走的部分】。")
    say()
    if args.dry_run:
        say("--dry-run：只看不做")
        say()
    elif not args.yes and not ask_yes("继续？", default=True):
        say("  取消")
        return 1
    say()

    clean_tailscale(args.dry_run)
    clean_system_tailscale(args.dry_run)
    clean_containers(args.dry_run)
    clean_image(args.dry_run, args.all)
    clean_caches(args.dry_run, args.all)
    check_processes()
    warn_outside_files()

    say()
    say("─" * 52)
    say("完成。现在可以删掉整个项目文件夹了：")
    say(f"  {ROOT}")
    say()
    say("已确认不会留在别处的：注册表、计划任务、系统服务、PATH、")
    say("桌面快捷方式、浏览器 localStorage、Tailscale 内核驱动 —— 项目压根没写这些。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
