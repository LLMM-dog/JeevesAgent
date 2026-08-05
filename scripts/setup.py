"""
一键初始化。

## 为什么值得有

手动搭建有六件事要做对：装两套依赖、生成 .env、生成 ENCRYPTION_KEY、
配模型、写人设。任何一步漏掉或写错，表现都是"启动失败"或"配置没生效"，
而错误信息通常不指向真因。

最典型的是 ENCRYPTION_KEY：让用户手填的话，他会填一个短字符串，
然后遇到 "Fernet key must be 32 url-safe base64-encoded bytes" ——
完全不知道怎么办。

## 复用而不重写

第 5 步（配模型）调的是 `provider.service.probe_models` / `create_provider`
—— 和 Web 设置页【同一个函数】。

各写一遍的话，两处的 URL 规范化规则、错误处理会逐渐分叉，
出现"在 setup 里能连上但 Web 里连不上"这种最难查的问题。

用法：
    python scripts/setup.py            # 完整六步
    python scripts/setup.py --no-deps  # 跳过装依赖（已装过时更快）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"

MIN_PY = (3, 11)
MIN_NODE = 20

# 步骤总数。写死而不是 len(steps) —— 步骤是顺序执行的函数调用，
# 不是列表，而进度提示"3/6"对用户判断"还要多久"有用。
# 默认只有 4 步。模型和个人信息挪到界面里配 —— 见 main() 里的说明。
#
# 交互模式（--interactive）会把它改成 6。不改的话输出是 [5/4]，
# 看起来像程序坏了。
TOTAL = 4


class AbortError(Exception):
    """用户主动中止或前置条件不满足。"""


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, title: str) -> None:
    say()
    say(f"[{n}/{TOTAL}] {title}")
    say("-" * 52)


def ask(prompt: str, default: str = "") -> str:
    """
    读一行输入。

    Ctrl-C / Ctrl-D 要当成"用户想退出"而不是崩掉 ——
    崩掉会留下半完成的状态（比如装了依赖但没生成 .env）。
    """
    hint = f"（回车用 {default}）" if default else ""
    try:
        v = input(f"  {prompt}{hint}: ").strip()
    except (KeyboardInterrupt, EOFError) as e:
        raise AbortError("已取消") from e
    return v or default


def ask_yes(prompt: str, *, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    v = ask(f"{prompt} [{d}]").lower()
    if not v:
        return default
    return v in ("y", "yes", "是")


def run(cmd: list[str], cwd: Path, what: str) -> bool:
    """
    跑一条命令。失败返回 False 而不抛 ——
    装依赖失败不该让整个 setup 退出，用户可能只是网络问题，
    后面的步骤（生成 .env）仍然值得做完。
    """
    say(f"  $ {' '.join(cmd)}")
    # 【Windows 上必须先把命令名解析成全路径】。
    #
    # npm 是 npm.cmd（批处理），不是 .exe。subprocess 不带 shell=True 时
    # 走 CreateProcess，而它【不会执行批处理文件】——
    # 传裸 "npm" 直接抛 FileNotFoundError：
    #
    #   >>> subprocess.run(["npm", "--version"])
    #   FileNotFoundError: [WinError 2] 系统找不到指定的文件
    #   >>> subprocess.run([shutil.which("npm"), "--version"])
    #   0  12.0.2
    #
    # 而 shutil.which("npm") 是能找到的 —— 所以第 1 步的环境检查显示
    # "✓ Node v22"，第 3 步却报"找不到 npm"，看起来自相矛盾。
    #
    # 实测后果：Windows 上每次 setup 都静默跳过前端依赖安装，
    # 而用户直到 start 起不来才发现，那时错误已经指向别处
    # （vite 找不到、页面白屏）。
    #
    # 不用 shell=True：那会让参数里的空格和特殊字符走 shell 解析，
    # 项目路径带空格时会被拆断。
    exe = shutil.which(cmd[0])
    if exe is None:
        say(f"  ✗ 找不到 {cmd[0]}")
        return False
    try:
        r = subprocess.run([exe, *cmd[1:]], cwd=str(cwd), check=False)
    except OSError as e:
        say(f"  ✗ 无法执行 {cmd[0]}：{e}")
        return False
    if r.returncode != 0:
        say(f"  ✗ {what}失败（退出码 {r.returncode}）")
        return False
    say(f"  ✓ {what}完成")
    return True


# ─────────────────────────── 1. 环境检查 ───────────────────────────


def step1_check() -> None:
    step(1, "检查运行环境")

    if sys.version_info < MIN_PY:
        raise AbortError(
            f"Python 版本太低：{sys.version_info.major}.{sys.version_info.minor}，"
            f"需要 {MIN_PY[0]}.{MIN_PY[1]}+"
        )
    say(f"  ✓ Python {sys.version_info.major}.{sys.version_info.minor}")

    # uv 是必需的：项目用 pyproject + uv.lock 管依赖
    if shutil.which("uv") is None:
        raise AbortError(
            "找不到 uv。安装：\n"
            "    Windows: powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"\n"
            "    其它:    curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
    say("  ✓ uv 已安装")

    # Node 只影响前端。没有的话后端仍然能跑（只是没界面），
    # 所以是警告而不是致命错误
    node = shutil.which("node")
    if node is None:
        say("  ⚠ 找不到 node，前端将无法构建（后端仍可用）")
        return
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, check=False, timeout=20
        ).stdout.strip()
        major = int(re.sub(r"^v", "", out).split(".")[0])
        if major < MIN_NODE:
            say(f"  ⚠ Node {out} 偏低，建议 {MIN_NODE}+")
        else:
            say(f"  ✓ Node {out}")
    except (ValueError, IndexError, subprocess.SubprocessError):
        say("  ⚠ 无法确定 Node 版本")


# ─────────────────────────── 2-3. 依赖 ───────────────────────────


def step2_backend_deps(skip: bool) -> None:
    step(2, "安装后端依赖")
    if skip:
        say("  （--no-deps，跳过）")
        return
    # 带 dev 是因为个人项目里跑测试是常规操作，
    # 分开装会让人第一次跑 pytest 时报 "找不到 pytest"。
    #
    # 另外带上 mcp / search / web / cron 四个可选组。
    #
    # 【为什么默认装它们】：只装 dev 的话，联网搜索、网页正文提取、
    # 定时任务、MCP 全都不注册 —— 而这些是文档里介绍过的功能。
    # 用户按快速开始跑完，发现"说好的联网搜索呢"，
    # 而工具列表里就是没有，也没有任何提示说"少装了一个组"。
    #
    # 这四个都是纯 Python 包、体积小、无系统依赖，装上没有代价。
    #
    # docker 组【不默认装】：它需要本机跑着 Docker 守护进程，
    # 装了 SDK 但没有守护进程只会让沙箱探活多绕一圈。
    # 要用 Docker 沙箱的人自己 uv sync --extra docker。
    run(
        [
            "uv",
            "sync",
            "--extra",
            "dev",
            "--extra",
            "mcp",
            "--extra",
            "search",
            "--extra",
            "web",
            "--extra",
            "cron",
        ],
        ROOT,
        "后端依赖",
    )


def step3_frontend_deps(skip: bool) -> None:
    step(3, "安装前端依赖")
    if skip:
        say("  （--no-deps，跳过）")
        return
    if shutil.which("npm") is None:
        say("  ⚠ 找不到 npm，跳过")
        return
    if (FRONTEND / "node_modules").is_dir():
        say("  node_modules 已存在")
        if not ask_yes("重新安装？", default=False):
            return
    run(["npm", "install"], FRONTEND, "前端依赖")


# ─────────────────────────── 4. .env ───────────────────────────


def gen_key() -> str:
    """
    生成 Fernet 密钥。

    【必须自动生成】—— 让用户手填的话他会填一个短字符串，然后遇到
    "Fernet key must be 32 url-safe base64-encoded bytes"，
    而这个错误信息完全不指向"你应该用 Fernet.generate_key()"。
    """
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def step4_env() -> Path:
    step(4, "生成配置文件 .env")

    env = ROOT / ".env"
    example = ROOT / ".env.example"

    if env.is_file():
        say(f"  {env.name} 已存在")
        txt = env.read_text(encoding="utf-8")
        m = re.search(r"^JEEVES_SECURITY__ENCRYPTION_KEY=(.*)$", txt, re.M)
        if m and m.group(1).strip():
            say("  ✓ ENCRYPTION_KEY 已配置，保持不动")
            return env
        # 有 .env 但 key 是空的 —— 这是最常见的"启动就报错"状态
        say("  ⚠ ENCRYPTION_KEY 为空，补一个")
        key = gen_key()
        if m:
            txt = re.sub(
                r"^JEEVES_SECURITY__ENCRYPTION_KEY=.*$",
                f"JEEVES_SECURITY__ENCRYPTION_KEY={key}",
                txt,
                count=1,
                flags=re.M,
            )
        else:
            txt = txt.rstrip() + f"\nJEEVES_SECURITY__ENCRYPTION_KEY={key}\n"
        env.write_text(txt, encoding="utf-8", newline="\n")
        say("  ✓ 已补上")
        _warn_backup_key()
        return env

    if not example.is_file():
        raise AbortError(f"缺少模板 {example.name}，无法生成 .env")

    txt = example.read_text(encoding="utf-8")
    key = gen_key()
    if re.search(r"^JEEVES_SECURITY__ENCRYPTION_KEY=", txt, re.M):
        txt = re.sub(
            r"^JEEVES_SECURITY__ENCRYPTION_KEY=.*$",
            f"JEEVES_SECURITY__ENCRYPTION_KEY={key}",
            txt,
            count=1,
            flags=re.M,
        )
    else:
        txt = txt.rstrip() + f"\nJEEVES_SECURITY__ENCRYPTION_KEY={key}\n"

    env.write_text(txt, encoding="utf-8", newline="\n")
    say(f"  ✓ 已从 {example.name} 生成 {env.name}")
    say("  ✓ 已自动生成 ENCRYPTION_KEY")
    _warn_backup_key()
    return env


def _warn_backup_key() -> None:
    say()
    say("  ⚠ 请备份 .env 里的 JEEVES_SECURITY__ENCRYPTION_KEY")
    say("    它丢了的话，已存的 API Key 全部无法解密（只能重新填一遍）")


# ─────────────────────────── 5. 模型 ───────────────────────────


async def _probe_and_save(base_url: str, api_key: str) -> bool:
    """
    探测模型并落库。

    ## 为什么调 service 而不自己发请求

    setup 和 Web 设置页必须用【同一个函数】。各写一遍的话，两处的 URL
    规范化规则（比如自动补 /v1）、错误处理会逐渐分叉，出现"在 setup 里
    能连上但 Web 里连不上"这种最难查的问题。
    """
    from app.infra.db.session import get_sessionmaker, init_db
    from app.infra.llm.openai_compat import OpenAICompatAdapter
    from app.modules.provider import service

    await init_db()
    llm = OpenAICompatAdapter()
    try:
        say("  正在拉取模型列表…")
        normalized, models = await service.probe_models(llm, base_url, api_key)
    except Exception as e:  # noqa: BLE001
        say(f"  ✗ 连接失败：{type(e).__name__}: {str(e)[:200]}")
        say("    检查 Base URL 和 Key。常见问题：URL 少了 /v1、Key 复制时带了空格")
        return False

    if normalized.rstrip("/") != base_url.rstrip("/"):
        # 规范化后的地址要回显 —— 用户填的被改过，让他知道实际用哪个
        say(f"  地址已规范化为：{normalized}")

    chat_models = [m for m in models if not m.looks_non_chat]
    say(f"  ✓ 拿到 {len(models)} 个模型（其中 {len(chat_models)} 个像对话模型）")

    show = models[:30]
    for i, m in enumerate(show, 1):
        tag = "" if not m.looks_non_chat else "  [可能非对话]"
        say(f"    {i:2}. {m.model_id}  窗口 {m.context_window}{tag}")
    if len(models) > len(show):
        say(f"    …还有 {len(models) - len(show)} 个未显示")

    say()
    pick = ask("选一个用于对话的模型（填序号）", "1")
    try:
        idx = int(pick) - 1
        chosen = show[idx]
    except (ValueError, IndexError):
        say(f"  ✗ 序号无效：{pick}")
        return False

    name = ask("给这个供应商起个名字", "默认")
    sm = get_sessionmaker()
    async with sm() as db:
        prov = await service.create_provider(
            db,
            name=name,
            base_url=normalized,
            api_key=api_key,
            models=[{"model_id": chosen.model_id, "context_window": chosen.context_window}],
        )
        mods = await service.list_models(db, prov.id)
        pk = mods[0].id
        # 三个用途都绑同一个模型。
        #
        # 不绑 title/compact 的话，第一次对话会在生成标题时失败 ——
        # 而错误信息是"没有可用模型"，用户刚配过模型会很困惑。
        for purpose in ("chat", "title", "compact"):
            await service.set_binding(db, purpose=purpose, model_pk=pk)

    say(f"  ✓ 已保存并绑定 {chosen.model_id}（对话/标题/压缩）")
    return True


def step5_model() -> None:
    step(5, "配置模型")
    say("  需要一个 OpenAI 兼容的 API 端点。")
    say("  也可以跳过，之后在设置页里配。")
    say()

    if not ask_yes("现在配置？"):
        say("  （跳过。启动后打开 http://127.0.0.1:9000 → 设置 → 添加供应商）")
        return

    base_url = ask("Base URL（如 https://api.openai.com/v1）")
    if not base_url:
        say("  （没填，跳过）")
        return
    api_key = ask("API Key")
    if not api_key:
        say("  （没填，跳过）")
        return

    # 必须让子进程能 import backend 包
    sys.path.insert(0, str(BACKEND))
    import asyncio

    try:
        ok = asyncio.run(_probe_and_save(base_url, api_key))
    except AbortError:
        raise
    except Exception as e:  # noqa: BLE001
        say(f"  ✗ 配置失败：{type(e).__name__}: {str(e)[:200]}")
        ok = False
    if not ok:
        say("  可以稍后在设置页里重试")


# ─────────────────────────── 6. 人设 ───────────────────────────


def step6_persona() -> None:
    step(6, "填写你的信息")

    personas = ROOT / "personas"
    personas.mkdir(exist_ok=True)
    user_md = personas / "USER.md"

    if user_md.is_file() and user_md.read_text(encoding="utf-8").strip():
        say(f"  {user_md.name} 已有内容，保持不动")
        say("  （想改的话直接编辑这个文件，或在设置页里改）")
        return

    say("  这些信息会进入系统提示词，让模型知道该怎么称呼你、按什么风格回答。")
    say("  可以留空，之后再改。")
    say()

    name = ask("怎么称呼你", "")
    role = ask("你的身份/职业（如：Python 后端开发）", "")
    prefer = ask("回答偏好（如：简洁、多给代码、少解释）", "")

    if not any((name, role, prefer)):
        say("  （都没填，跳过）")
        return

    lines = ["# 用户信息", ""]
    if name:
        lines.append(f"- 称呼：{name}")
    if role:
        lines.append(f"- 身份：{role}")
    if prefer:
        lines.append(f"- 回答偏好：{prefer}")
    lines.append("")
    lines.append("<!-- 这个文件会进入系统提示词。可以随时编辑，或在设置页里改。 -->")

    user_md.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    say(f"  ✓ 已写入 {user_md.relative_to(ROOT)}")


# ─────────────────────────── 收尾 ───────────────────────────


def outro() -> None:
    r"""
    收尾提示。

    ## 为什么要和 README 对齐

    这段话是用户装完之后唯一的指引。它和 README 说的不一致时，
    用户会照这里做 —— 而这里写着旧的启动方式和旧端口的话，
    第一步就走错。

    改过一次：原来说 `.\start.ps1` 和 5173，而现在主入口是双击
    start.bat（生产模式，9000）。5173 是 vite 的开发端口，
    只在 -Dev 模式下才有。
    """
    say()
    say("=" * 52)
    say("装好了。启动：")
    say()
    if os.name == "nt":
        say("  双击 start.bat")
    else:
        say("  chmod +x start.sh    # 只需第一次")
        say("  ./start.sh --prod")
    say()
    say("然后浏览器打开 http://127.0.0.1:9000")
    say()
    say("第一次进去要做两件事：")
    say("  1. 设置 → 模型：填一个 OpenAI 兼容端点，选要用的模型")
    say("  2. 对话页输入框上方：选这次对话的工作目录")
    say()
    say("要改代码的话用开发模式（前端热更新，端口 5173）：")
    if os.name == "nt":
        say("  start.bat -Dev")
    else:
        say("  ./start.sh")


def main() -> int:
    ap = argparse.ArgumentParser(description="Jeeves 一键初始化")
    ap.add_argument("--no-deps", action="store_true", help="跳过装依赖")
    ap.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="额外在命令行里配模型和个人信息（默认不问，去设置页配更方便）",
    )
    args = ap.parse_args()

    say("Jeeves 初始化")
    say(f"项目目录：{ROOT}")

    try:
        step1_check()
        step2_backend_deps(args.no_deps)
        step3_frontend_deps(args.no_deps)
        step4_env()
        # 【默认不进交互】。
        #
        # 原来这里必问模型配置和个人信息。三个问题的代价：
        #
        #   - 双击 setup.bat 的人以为是"装依赖"，结果卡在一个
        #     要填 Base URL 和 API Key 的提示上
        #   - 命令行里填 API Key 会进 shell 历史
        #   - 同样的事在设置页里做更好：能看到探测到的模型列表、
        #     能改、能删、填错了有明确报错
        #
        # 装完直接启动，在界面里配。要命令行配的加 --interactive。
        if args.interactive:
            # 多两步，总数要跟着变
            global TOTAL
            TOTAL = 6
            step5_model()
            step6_persona()
    except AbortError as e:
        say()
        say(f"中止：{e}")
        return 1

    outro()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
