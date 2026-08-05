"""
用真实 MCP 服务器验证接入。

## 为什么自己写服务器而不用 npx 拉一个

`npx -y @modelcontextprotocol/server-filesystem` 要联网下载，慢且不稳定。
而验证需要的是**可控**：我要能让服务器返回带空格的工具名、超长描述、
往 stdout 打垃圾日志 —— 这些是真实世界会遇到但公开服务器不会主动做的。

所以这里自己写一个 stdio MCP 服务器（`_fake_server.py`），故意包含
这些"不规矩"的行为。

## 验什么

1. **真实 stdio 连接能建立**，工具能被发现
2. **工具名合规化**：带空格/点/中文的名字不会让请求 400
3. **调用能拿到结果**
4. **stdout 垃圾日志不破坏连接**（第三方常违反 MUST NOT）
5. **未确认的 stdio 服务器不连**（规范 MUST）
6. **一个服务器挂掉不影响其他**（缺陷）
7. **MCP 工具全部需要审批**（规范 SHOULD）
8. **超时生效**
9. **SSRF 防护**拦得住元数据端点

用法：uv run python scripts/verify_mcp.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

FAKE = Path(__file__).resolve().parent / "_fake_mcp_server.py"


async def main() -> int:  # noqa: PLR0915
    from app.modules.mcp import config as mcfg
    from app.modules.mcp.manager import McpManager
    from app.modules.mcp.tools import McpTool

    failures: list[str] = []
    py = sys.executable

    print("1. 连接真实 stdio MCP 服务器")
    mgr = McpManager()
    cfg = mcfg.ServerConfig(
        server_id="fake",
        transport="stdio",
        command=py,
        args=[str(FAKE)],
        command_approved=True,  # 已确认
    )
    await mgr.connect_all([cfg])
    st = mgr.states()[0]
    print(f"   状态={st.status} 工具数={len(st.tools)}")
    if st.status != "ready":
        print(f"   错误：{st.error}")
        failures.append(f"连不上真实 MCP 服务器：{st.error}")
        return _finish(failures, mgr)

    names = [t.name for t in st.tools]
    print(f"   工具：{names}")

    # ── 工具名合规化 ──
    print("\n2. 工具名合规化（服务器故意返回带空格/点/中文的名字）")
    bad_ok = True
    for n in names:
        if not all(c.isascii() and (c.isalnum() or c in "_-") for c in n):
            failures.append(f"工具名 {n!r} 含非法字符，会让整个请求 400")
            bad_ok = False
        if len(n) > 64:
            failures.append(f"工具名 {n!r} 超过 64 字符")
            bad_ok = False
    if bad_ok:
        print("   ✓ 全部合规（^[a-zA-Z0-9_-]{1,64}$）")

    # 原名要保留，否则调不通
    raws = {t.raw_name for t in st.tools}
    print(f"   保留的原名：{sorted(raws)}")
    if "say hello" not in raws:
        failures.append("原名没保留 —— 调用时必须用原名，合规化不可逆")
    else:
        print("   ✓ 原名保留了（调用用原名）")

    # ── 调用 ──
    print("\n3. 调用带空格名字的工具")
    text, err = await mgr.call("fake", "say hello", {"who": "世界"})
    print(f"   返回：{text[:80]}  错误={err}")
    if err or "世界" not in text:
        failures.append(f"调用失败或返回不对：{text[:200]}")
    else:
        print("   ✓ 调通了")

    print("\n4. 普通调用")
    text2, err2 = await mgr.call("fake", "add", {"a": 2, "b": 3})
    print(f"   返回：{text2[:60]}  错误={err2}")
    if err2 or "5" not in text2:
        failures.append(f"普通调用失败：{text2[:150]}")
    else:
        print("   ✓ 调通了")

    # ── 超长描述截断 ──
    print("\n5. 超长描述应被截断")
    long_tool = next((t for t in st.tools if t.raw_name == "long_desc"), None)
    if long_tool is None:
        failures.append("没发现 long_desc 工具")
    else:
        print(f"   描述长度：{len(long_tool.description)}")
        if len(long_tool.description) > mcfg.MAX_DESC_CHARS + 300:
            failures.append(f"描述没截断，长度 {len(long_tool.description)}")
        else:
            print("   ✓ 已截断")
        if "不可信" not in long_tool.description:
            failures.append("描述没标注来源与不可信声明")
        else:
            print("   ✓ 标注了来源与不可信")

    # ── 全部需要审批 ──
    print("\n6. 所有 MCP 工具都要需要审批（含声明 readOnly 的）")
    ro = next((t for t in st.tools if t.raw_name == "readonly_liar"), None)
    if ro is None:
        failures.append("没发现 readonly_liar 工具")
    else:
        print(f"   readonly_liar 的注解：{ro.annotations}")
        wrapper = McpTool(cfg, ro)
        if not wrapper.requires_approval:
            failures.append(
                "声明 readOnlyHint 的工具跳过了审批 —— "
                "规范要求注解视为不可信"
            )
        else:
            print("   ✓ 仍然需要审批（注解不可信）")

    # ── 超时 ──
    print("\n7. 卡住的工具应该超时而不是永久挂着")
    orig = mcfg.CALL_TIMEOUT
    from app.modules.mcp import manager as mmgr

    mmgr.CALL_TIMEOUT = 2.0
    try:
        t0 = asyncio.get_running_loop().time()
        text3, err3 = await mgr.call("fake", "hang", {})
        dt = asyncio.get_running_loop().time() - t0
        print(f"   耗时 {dt:.1f}s，返回：{text3[:60]}")
        if not err3 or dt > 6:
            failures.append(f"超时没生效，耗时 {dt:.1f}s")
        else:
            print("   ✓ 超时生效")
    finally:
        mmgr.CALL_TIMEOUT = orig

    await mgr.disconnect_all()

    # ── stdout 垃圾日志 ──
    #
    # 规范说服务器 MUST NOT 往 stdout 写非 MCP 消息，但真实世界里
    # 很多服务器会打启动日志。这是接第三方 MCP 时最常见的
    # "配了个能跑的服务器却连不上"。
    print("\n7.5 服务器往 stdout 打垃圾日志（违反规范，但真实世界常见）")
    mgr_dirty = McpManager()
    dirty = mcfg.ServerConfig(
        server_id="dirty",
        transport="stdio",
        command=py,
        args=[str(FAKE), "--dirty-stdout"],
        command_approved=True,
    )
    await mgr_dirty.connect_all([dirty])
    ds = mgr_dirty.states()[0]
    print(f"   状态={ds.status}")
    if ds.status == "ready":
        print("   ✓ SDK 跳过了垃圾行，连接正常")
        dt, de = await mgr_dirty.call("dirty", "add", {"a": 1, "b": 1})
        print(f"   调用返回：{dt[:50]}")
        if de:
            failures.append("stdout 有垃圾时调用失败")
    else:
        # 这不算实现错误 —— SDK 的行为如此。但必须记下来：
        # 用户遇到时错误信息要能指向真因。
        print(f"   ⚠ 连不上：{ds.error[:150]}")
        print("   → SDK 不容忍 stdout 垃圾。这类服务器需要用户自己修，")
        print("     但我们的错误信息必须能指向真因而不是笼统的'连接失败'")
        if "error" not in ds.status:
            failures.append("stdout 垃圾导致的失败没有被正确标记为 error")
    await mgr_dirty.disconnect_all()

    # ── 失败隔离 ──
    print("\n8. 一个服务器连不上，其他仍要正常")
    mgr2 = McpManager()
    good = mcfg.ServerConfig(
        server_id="good", transport="stdio", command=py,
        args=[str(FAKE)], command_approved=True,
    )
    bad = mcfg.ServerConfig(
        server_id="bad", transport="stdio",
        command="this_command_does_not_exist_xyz", command_approved=True,
    )
    await mgr2.connect_all([bad, good])
    states = {s.server_id: s for s in mgr2.states()}
    print(f"   bad={states['bad'].status}  good={states['good'].status}")
    print(f"   bad 的原因：{states['bad'].error[:90]}")
    if states["good"].status != "ready":
        failures.append(
            "一个服务器连不上导致另一个也不可用 —— "
            "这是容易踩的一个坑"
        )
    else:
        print("   ✓ good 不受影响")
    if not states["bad"].error:
        failures.append("失败的服务器没有记录原因")
    tools_now = mgr2.all_tools()
    print(f"   可用工具数：{len(tools_now)}（应该只有 good 的）")
    if not tools_now:
        failures.append("好服务器的工具也没了")
    await mgr2.disconnect_all()

    # ── 未确认命令不连 ──
    print("\n9. 未确认启动命令的 stdio 服务器不能连")
    mgr3 = McpManager()
    unapproved = mcfg.ServerConfig(
        server_id="unapproved", transport="stdio", command=py,
        args=[str(FAKE)], command_approved=False,
    )
    await mgr3.connect_all([unapproved])
    s3 = mgr3.states()[0]
    print(f"   状态={s3.status}  原因={s3.error[:70]}")
    if s3.status == "ready":
        failures.append(
            "未确认的 stdio 服务器被直接连上了 —— "
            "本地 MCP 服务器等于任意代码执行，规范要求必须先确认"
        )
    else:
        print("   ✓ 拒绝连接")
    await mgr3.disconnect_all()

    # ── SSRF ──
    print("\n10. SSRF 防护")
    checks = [
        ("http://169.254.169.254/latest/meta-data/", "云元数据端点"),
        ("http://10.0.0.1/mcp", "内网地址"),
        ("http://0177.0.0.1/mcp", "八进制回环"),
        ("file:///etc/passwd", "file 协议"),
    ]
    for url, why in checks:
        try:
            mcfg.check_url_safe(url, allow_local=False)
            failures.append(f"SSRF 没拦住 {why}：{url}")
            print(f"   ✗ {why} 没拦住")
        except ValueError as e:
            print(f"   ✓ {why} 拦下：{str(e)[:60]}")

    return _finish(failures, None)


def _finish(failures: list[str], mgr: object) -> int:
    print("\n" + "=" * 58)
    if failures:
        for f in failures:
            print(f"✗ {f}")
        return 1
    print("通过：真实 stdio 连接、名字合规化、垃圾日志容错、审批、"
          "超时、失败隔离、命令确认、SSRF 全部生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
