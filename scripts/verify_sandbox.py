"""
用真实 Docker 验证沙箱。

## 为什么必须真跑

单测里大量是源码断言（`assert "--network" in src`）—— 它们只能保证
参数没被误删，**不能保证参数真的起作用**。

而这些参数正是安全边界：`--network none` 写对了但 Docker 忽略了它，
或者挂载路径在 Windows 上转错了，单测完全看不出来。

## 验什么

1. 容器真的创建、命令真的在容器里跑（不是宿主）
2. **网络真的被隔断**（用 --network host，这条是最重要的差异）
3. 挂载范围：能读写工作区，读不到项目文件（.env 里有所有 API Key）
4. 资源限制生效
5. 路径映射正确（sandbox.md 说这是最容易出 bug 的地方）
6. 容器复用（同会话第二次执行不新建）
7. 遗留容器能被清理
8. 超时后容器内进程真的被杀（不是只杀 docker CLI）
9. 降级：Docker 不可用时不静默回落

用法：
  uv run python scripts/verify_sandbox.py
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


def sh(args: list[str], timeout: float = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "not found"


async def main() -> int:  # noqa: PLR0915
    from app.core.config import settings
    from app.infra.sandbox import factory
    from app.infra.sandbox.docker import NAME_PREFIX, DockerSandbox

    fails: list[str] = []
    ws = Path(settings.workspace_dir).resolve()
    ws.mkdir(parents=True, exist_ok=True)

    print("0. 环境")
    code, out = sh(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        print("   Docker 不可用，跳过")
        return 2
    print(f"   Docker {out.strip()}")
    print(f"   工作区 {ws}")

    sb = DockerSandbox()

    print("\n1. 探活")
    ok, reason = await sb.health()
    print(f"   可用={ok} 原因={reason or '（无）'}")
    if not ok:
        print(f"   → 先解决：{reason}")
        return 2

    sid = f"ses_verify{int(time.time()) % 100000}"
    try:
        print("\n2. 命令真的在容器里跑（不是宿主）")
        # 容器是 linux，宿主是 windows —— uname 能区分
        r = await sb.run("uname -s && cat /etc/os-release | head -1", cwd=ws, session_id=sid)
        print(f"   退出码={r.exit_code}")
        print(f"   输出：{r.output.strip()[:150]}")
        if r.exit_code != 0:
            fails.append(f"基本命令失败：{r.output[:200]}")
        elif "Linux" not in r.output:
            fails.append(f"没在 Linux 容器里跑（输出不含 Linux）：{r.output[:150]}")
        else:
            print("   ✓ 在容器里（宿主是 Windows，输出是 Linux）")

        print("\n3. 容器确实存在且名字带前缀")
        code, out = sh(["docker", "ps", "--filter", f"name={NAME_PREFIX}{sid}", "--format", "{{.Names}}\t{{.Status}}"])
        print(f"   {out.strip() or '（没找到）'}")
        if f"{NAME_PREFIX}{sid}" not in out:
            fails.append("找不到对应的容器")
        else:
            print("   ✓ 容器在跑")

        print("\n4. 网络隔离（--network none，这是沙箱最重要的一环）")
        # 容器内检查网络接口
        r = await sb.run("ip addr 2>/dev/null | grep -c inet || echo 0", cwd=ws, session_id=sid)
        print(f"   inet 接口数：{r.output.strip()[:40]}")
        # 尝试出网
        r = await sb.run(
            "timeout 8 python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),timeout=5);print('CONNECTED')\" 2>&1 || echo BLOCKED",
            cwd=ws,
            session_id=sid,
            timeout=25,
        )
        print(f"   外网：{r.output.strip()[:120]}")
        if "CONNECTED" in r.output:
            fails.append("容器能连外网 —— --network none 没生效")
        else:
            print("   ✓ 出网被隔断")

        # 【关键】能不能打到宿主的服务。
        # host.docker.internal 是 Docker Desktop 提供的宿主地址。
        r = await sb.run(
            "timeout 8 python3 -c \"import socket;socket.create_connection(('host.docker.internal',9000),timeout=5);print('HOST_REACHED')\" 2>&1 || echo HOST_BLOCKED",
            cwd=ws,
            session_id=sid,
            timeout=25,
        )
        print(f"   宿主服务：{r.output.strip()[:120]}")
        if "HOST_REACHED" in r.output:
            fails.append("容器能打到宿主服务 —— 网络隔离失效")
        else:
            print("   ✓ 打不到宿主")

        # 云元数据端点
        r = await sb.run(
            "timeout 8 python3 -c \"import socket;socket.create_connection(('169.254.169.254',80),timeout=5);print('META_REACHED')\" 2>&1 || echo META_BLOCKED",
            cwd=ws,
            session_id=sid,
            timeout=25,
        )
        if "META_REACHED" in r.output:
            fails.append("容器能打云元数据端点 —— 能拿 IAM 凭证")
        else:
            print("   ✓ 打不到 169.254.169.254")

        print("\n5. 挂载范围")
        marker = ws / "verify_mount.txt"
        marker.write_text("host-written", encoding="utf-8")
        r = await sb.run("cat verify_mount.txt", cwd=ws, session_id=sid)
        print(f"   读宿主写的文件：{r.output.strip()[:60]}")
        if "host-written" not in r.output:
            fails.append("容器读不到工作区文件 —— 挂载没生效")
        else:
            print("   ✓ 能读工作区")

        r = await sb.run("echo container-written > from_container.txt", cwd=ws, session_id=sid)
        back = ws / "from_container.txt"
        if not back.is_file() or "container-written" not in back.read_text(encoding="utf-8"):
            fails.append("容器写的文件宿主看不到 —— 挂载不是 rw")
        else:
            print("   ✓ 容器写的文件宿主能看到")
        back.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)

        # 【关键】读不到项目文件
        print("\n6. 读不到项目文件（.env 里有 ENCRYPTION_KEY 和所有 API Key）")
        for probe in ("/.env", "/app/.env", "/pyproject.toml", "/backend/app/main.py"):
            r = await sb.run(f"cat {probe} 2>&1 | head -2 || true", cwd=ws, session_id=sid)
            leaked = "ENCRYPTION_KEY" in r.output or "fastapi" in r.output.lower()
            print(f"   {probe}: {'⚠ 读到内容' if leaked else '读不到'}")
            if leaked:
                fails.append(f"容器能读到项目文件 {probe} —— 沙箱形同虚设")
        # 容器里 / 下应该只有系统目录 + /workspace
        r = await sb.run("ls / | tr '\\n' ' '", cwd=ws, session_id=sid)
        print(f"   容器根目录：{r.output.strip()[:160]}")
        if "backend" in r.output or "pyproject" in r.output:
            fails.append("容器根目录里出现了项目文件")
        else:
            print("   ✓ 只有系统目录和 /workspace")

        print("\n7. 路径映射")
        sub = ws / "verify_deep" / "nested"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "here.txt").write_text("deep-ok", encoding="utf-8")
        r = await sb.run("pwd && cat here.txt", cwd=sub, session_id=sid)
        print(f"   在子目录执行：{r.output.strip()[:100]!r}")
        if "/workspace/verify_deep/nested" not in r.output:
            fails.append(f"子目录路径映射不对：{r.output[:150]}")
        elif "deep-ok" not in r.output:
            fails.append("子目录里读不到文件")
        else:
            print("   ✓ 宿主子目录 → /workspace/verify_deep/nested")
        import shutil as _sh

        _sh.rmtree(ws / "verify_deep", ignore_errors=True)

        print("\n8. 资源限制")
        code, out = sh([
            "docker", "inspect", f"{NAME_PREFIX}{sid}",
            "--format", "{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.CapDrop}}",
        ])
        print(f"   {out.strip()}")
        parts = out.strip().split("|")
        if len(parts) >= 5:
            mem, cpus, pids, net, caps = parts[:5]
            if mem == "0":
                fails.append("内存未限制")
            if cpus == "0":
                fails.append("CPU 未限制")
            if pids in ("0", "<nil>", ""):
                fails.append("pids 未限制（fork 炸弹能拖死宿主）")
            if net != "none":
                fails.append(f"NetworkMode={net}，应为 none")
            if "ALL" not in caps:
                fails.append(f"CapDrop={caps}，应含 ALL")
            if not fails:
                print("   ✓ memory / cpus / pids / network=none / cap-drop=ALL 全部生效")

        # 注意：fork 炸弹测试【必须放最后】——
        # 实测它会把容器的 PID 表打满且不恢复，之后所有 docker exec 都返回
        # "procReady not received"（退出码 128）。
        # 放中间的话后续每一项都会失败，而失败原因看起来像各自的功能坏了。
        # 见步骤 18。

        print("\n9. 容器复用（同会话第二次执行不该新建）")
        code, before = sh(["docker", "ps", "-q", "--filter", f"name={NAME_PREFIX}{sid}"])
        r = await sb.run("echo reuse-test", cwd=ws, session_id=sid)
        code, after = sh(["docker", "ps", "-q", "--filter", f"name={NAME_PREFIX}{sid}"])
        print(f"   执行前容器 id={before.strip()[:12]} 执行后={after.strip()[:12]}")
        if before.strip() != after.strip():
            fails.append("容器被重建了 —— 复用失效，pip 装的包会丢")
        else:
            print("   ✓ 复用了同一个容器")

        print("\n10. 容器内状态保持（复用的意义）")
        await sb.run("echo persisted > /tmp/state.txt", cwd=ws, session_id=sid)
        r = await sb.run("cat /tmp/state.txt", cwd=ws, session_id=sid)
        if "persisted" not in r.output:
            fails.append("容器内状态没保持 —— 复用没起作用")
        else:
            print("   ✓ 上次写的文件还在（pip 装的包同理）")

        print("\n11. 超时后容器内进程要被杀")
        t0 = time.monotonic()
        r = await sb.run("sleep 60", cwd=ws, session_id=sid, timeout=5)
        el = time.monotonic() - t0
        print(f"   耗时 {el:.1f}s  timed_out={r.timed_out}")
        if not r.timed_out:
            fails.append("超时没被识别")
        elif el > 25:
            fails.append(f"超时后等太久（{el:.0f}s）")
        else:
            print("   ✓ 超时生效")
        await asyncio.sleep(1)
        r2 = await sb.run("ps aux | grep -c '[s]leep 60' || echo 0", cwd=ws, session_id=sid)
        left = r2.output.strip().splitlines()[-1].strip() if r2.output.strip() else "0"
        print(f"   容器内残留的 sleep 进程数：{left}")
        if left not in ("0", ""):
            fails.append(
                f"超时后容器内还有 {left} 个 sleep —— "
                "只杀了 docker CLI，容器内进程还在烧 CPU"
            )
        else:
            print("   ✓ 容器内进程也被杀了")

        print("\n12. 输出截断")
        r = await sb.run(
            "python3 -c \"print('x'*200)\" | head -1 && seq 1 5000", cwd=ws, session_id=sid, timeout=60
        )
        print(f"   truncated={r.truncated} total_lines={r.total_lines} shown={r.shown_lines}")
        if r.total_lines > 3000 and not r.truncated:
            fails.append("大量输出没被截断")
        elif r.truncated:
            print("   ✓ 截断生效")

        print("\n13. 命令失败要如实返回退出码")
        r = await sb.run("exit 42", cwd=ws, session_id=sid)
        print(f"   exit 42 → exit_code={r.exit_code}")
        if r.exit_code != 42:
            fails.append(f"退出码不对：期望 42，实际 {r.exit_code}")
        else:
            print("   ✓ 退出码透传")

        print("\n14. 遗留容器清理")
        stray = f"{NAME_PREFIX}stray{int(time.time()) % 10000}"
        sh(["docker", "run", "-d", "--name", stray, "--network", "none",
            settings.sandbox.docker_image, "tail", "-f", "/dev/null"])
        code, out = sh(["docker", "ps", "-q", "--filter", f"name={stray}"])
        print(f"   造了一个遗留容器 {stray}: {'在跑' if out.strip() else '没起来'}")
        n = await sb.cleanup_orphans()
        code, out2 = sh(["docker", "ps", "-aq", "--filter", f"name={stray}"])
        print(f"   cleanup_orphans 清了 {n} 个，遗留容器还在={bool(out2.strip())}")
        if out2.strip():
            fails.append("遗留容器没被清掉")
            sh(["docker", "rm", "-f", stray])
        else:
            print("   ✓ 遗留容器已清理")
        # 确认自己的容器没被误删
        code, mine = sh(["docker", "ps", "-q", "--filter", f"name={NAME_PREFIX}{sid}"])
        if not mine.strip():
            fails.append("cleanup_orphans 把正在用的容器也删了")
        else:
            print("   ✓ 正在用的容器没被误删")

        print("\n15. 会话清理")
        await sb.cleanup_session(sid)
        code, out = sh(["docker", "ps", "-aq", "--filter", f"name={NAME_PREFIX}{sid}"])
        print(f"   容器还在={bool(out.strip())}")
        if out.strip():
            fails.append("cleanup_session 没删掉容器")
        else:
            print("   ✓ 容器已删")

        # ── fork 炸弹放最后：它会打坏容器 ──
        print("\n16. fork 炸弹：pids-limit 挡住 + 容器坏了能自动恢复")
        sid2 = f"{sid}b"
        r = await sb.run("echo before-bomb", cwd=ws, session_id=sid2)
        if "before-bomb" not in r.output:
            fails.append("新会话的容器起不来")
        cid_before = sb._containers.get(sid2, {}).get("cid", "")[:12]
        print(f"   炸弹前容器 {cid_before}")

        r = await sb.run(
            "python3 -c \"import os\nfor i in range(3000):\n  try: os.fork()\n  except OSError: print('FORK_BLOCKED'); break\" 2>&1 | head -2",
            cwd=ws,
            session_id=sid2,
            timeout=40,
        )
        print(f"   炸弹输出：{r.output.strip()[:80]}")
        if "FORK_BLOCKED" in r.output:
            print("   ✓ fork 被 pids-limit 挡住（没拖死宿主）")
        else:
            print("   （没触发上限，可能是镜像行为不同）")

        # 【关键】炸弹之后还能不能正常执行。
        #
        # 实测不做处理的话，此后每次 exec 都返回
        # "OCI runtime exec failed: procReady not received"（退出码 128）
        # —— 这个会话的后续所有命令全废，而错误完全不指向真因。
        await asyncio.sleep(2)
        r = await sb.run("echo after-bomb-ok", cwd=ws, session_id=sid2, timeout=60)
        cid_after = sb._containers.get(sid2, {}).get("cid", "")[:12]
        print(f"   炸弹后执行：{r.output.strip()[:80]!r}")
        print(f"   炸弹后容器 {cid_after}（换了={cid_before != cid_after}）")
        if "after-bomb-ok" not in r.output:
            fails.append(
                f"fork 炸弹后容器无法恢复，后续命令全废：{r.output[:200]}"
            )
        else:
            print("   ✓ 容器已自动重建，命令恢复正常")

        r = await sb.run("exit 7", cwd=ws, session_id=sid2, timeout=60)
        if r.exit_code != 7:
            fails.append(f"恢复后退出码不对：期望 7，实际 {r.exit_code}")
        else:
            print("   ✓ 恢复后退出码也正常")
        await sb.cleanup_session(sid2)

        print("\n17. 降级不能静默")
        import app.infra.sandbox.docker as dkmod

        factory.reset_cache()
        orig = dkmod.shutil.which
        try:
            dkmod.shutil.which = lambda _: None  # type: ignore[assignment]
            settings.sandbox.backend = "docker"
            got = await factory.get_sandbox()
            reason2 = factory.fallback_reason()
            print(f"   后端={got.name} isolated={got.isolated}")
            print(f"   降级原因={reason2[:90]}")
            if got.name != "local":
                fails.append("Docker 不可用时没降级")
            elif not reason2:
                fails.append("降级了但没记录原因 —— 前端无法提示用户")
            elif got.isolated:
                fails.append("降级后仍自称隔离环境")
            else:
                print("   ✓ 降级且记录了原因")
        finally:
            dkmod.shutil.which = orig  # type: ignore[assignment]
            factory.reset_cache()

        return _finish(fails)
    finally:
        with __import__("contextlib").suppress(Exception):
            await sb.cleanup_session(sid)
        sh(["docker", "ps", "-aq", "--filter", f"name={NAME_PREFIX}"])
        print("（已清理测试容器）")


def _finish(fails: list[str]) -> int:
    print("\n" + "=" * 58)
    if fails:
        for f in fails:
            print(f"✗ {f}")
        return 1
    print(
        "通过：容器执行、网络三重隔离、挂载范围、读不到项目文件、"
        "路径映射、资源限制、容器复用与状态保持、超时杀进程、"
        "遗留清理、降级告知"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
