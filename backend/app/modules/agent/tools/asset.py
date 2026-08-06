"""
让模型自己建/改宏和技能。

## 为什么不是"把 skills/ 加进白名单"

那是最省事的方案，但有三个问题：

  1. **frontmatter 要模型自己拼**。少一个 description，条目会被加载器
     静默跳过（只留一条 warning 诊断），而模型以为建好了。这个失败模式
     没有任何反馈能让它发现。
  2. **建完不 reload 等于没建**。索引是进程内单例，不重扫的话新条目要
     等重启才出现 —— 模型和用户都会以为失败。
  3. **白名单是目录级的**。给了写 skills/ 的权限，模型也就能覆盖任何
     已有的技能。而它通常只是想新建一个。

所以走一个受控写入口：名字校验、必填字段校验、默认不覆盖、写完自动
reload。模型拿到的是"成功/失败 + 原因"，而不是一个可能静默失效的
文件写入。

## 为什么一个工具带 action 而不是四个工具

工具定义每轮都进上下文。四个工具（create/update/delete/list）的 schema
加起来 800+ token，而它们的参数几乎完全一样。一个带 action 的工具
只要 300 出头。

同理宏和技能合成一个工具（kind 参数）—— 它们的结构是一样的
（目录 + 带 frontmatter 的 md），分成两套等于把同一份 schema 写两遍。
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.exceptions import AppError
from app.modules.agent.tools.base import ToolContext, ToolResult
from app.modules.skill import authoring
from app.modules.skill import registry as skill_registry
from app.modules.skill.macros import get_index as get_macro_index

log = structlog.get_logger(__name__)


class ManageAssetTool:
    """
    宏和技能的增删改查。

    ## 为什么需要审批

    它写文件，而且写的是【会进后续所有对话上下文】的文件。一个恶意或
    错误的技能描述能影响模型之后的全部行为 —— 比"改一个源码文件"
    影响面更大。

    读操作（list / read）不写任何东西，但审批是工具级的粒度，
    没法只对写操作要求。让 list 也要审批比让 create 不要审批安全。
    """

    name = "manage_asset"
    description = (
        "管理宏（macro）和技能（skill）：新建、修改、删除、查看。"
        "\n\n宏是可复用的流程模板，用户用 ! 引用它。"
        "技能是按需加载的知识包，你用 load_skill 读它。"
        "\n\n什么时候用："
        "\n- 用户说「把这个流程存成宏」「记住这个步骤」"
        "\n- 用户说「学一下这个」并给了一段规范或文档"
        "\n- 用户要改或删已有的宏/技能"
        "\n\n注意 description 字段决定了它【什么时候会被想起来】——"
        "写「当用户要 X 时使用」这种触发条件，不要只写「关于 X 的说明」。"
        "写不好的话这个宏建了也没人用。"
    )
    requires_approval = True
    destructive = True

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "read", "create", "update", "delete"],
                    "description": "要做什么",
                },
                "kind": {
                    "type": "string",
                    "enum": ["macro", "skill"],
                    "description": "宏还是技能",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "名字。只能用字母、数字、中文、连字符、下划线。"
                        "list 时不需要"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "一句话说明【什么时候该用它】。create 和 update 必填。"
                        "这句话会常驻上下文，所以要短且准 —— "
                        "写「当用户要写 git 提交信息时使用」而不是「git 相关」"
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "正文（Markdown）。create 和 update 必填",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词，帮助检索。可选",
                },
            },
            "required": ["action", "kind"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        action = str(kw.get("action", "")).strip()
        kind = str(kw.get("kind", "")).strip()
        name = str(kw.get("name", "")).strip()

        if kind not in ("macro", "skill"):
            return ToolResult(
                content=f"kind 只能是 macro 或 skill，收到 {kind!r}", is_error=True
            )

        try:
            if action == "list":
                return self._list(kind)

            if not name:
                return ToolResult(
                    content=f"{action} 需要 name", is_error=True
                )

            if action == "read":
                desc, body, kwds, _raw = authoring.read_source(kind=kind, name=name)
                return ToolResult(
                    content=(
                        f"# {name}\n\ndescription: {desc}\n"
                        f"keywords: {', '.join(kwds) or '（无）'}\n\n---\n\n{body}"
                    ),
                    display={"kind": kind, "name": name, "description": desc},
                )

            if action == "delete":
                authoring.remove(kind=kind, name=name)
                return ToolResult(
                    content=f"已删除 {kind} {name}",
                    display={"kind": kind, "name": name, "deleted": True},
                )

            if action in ("create", "update"):
                desc = str(kw.get("description", "")).strip()
                body = str(kw.get("body", ""))
                if not desc:
                    return ToolResult(
                        content=(
                            "description 不能为空 —— 没有它的话这个条目会被"
                            "加载器静默跳过，不会有任何报错，而你会以为建好了"
                        ),
                        is_error=True,
                    )
                if not body.strip():
                    return ToolResult(
                        content="body 不能为空", is_error=True
                    )
                raw_kw = kw.get("keywords") or []
                kwds = [str(x) for x in raw_kw] if isinstance(raw_kw, list) else []
                r = authoring.upsert(
                    kind=kind,
                    name=name,
                    description=desc,
                    body=body,
                    keywords=kwds,
                    # update 才允许覆盖。create 撞名时报错 ——
                    # 模型起的名字撞车很常见，默认覆盖会悄悄冲掉
                    # 用户手写的东西。
                    overwrite=(action == "update"),
                )
                verb = "已新建" if r.created else "已更新"
                extra = (
                    "\n\n它现在就能用了（已重新扫描索引，不用重启）。"
                    if r.created
                    else ""
                )
                return ToolResult(
                    content=f"{verb} {kind} {r.name}{extra}",
                    display={
                        "kind": kind,
                        "name": r.name,
                        "created": r.created,
                        "description": desc,
                    },
                )

            return ToolResult(
                content=f"不认识的 action：{action!r}", is_error=True
            )

        except AppError as e:
            # 把业务错误如实转给模型。
            #
            # 【不能吞掉】。"已存在"这类错误里含着模型需要的信息 ——
            # 它应该改用 update 或者换个名字，而不是重试同样的调用。
            return ToolResult(content=f"失败：{e.message}", is_error=True)
        except OSError as e:
            log.warning("manage_asset_io_failed", err=str(e), kind=kind, name=name)
            return ToolResult(content=f"文件操作失败：{e}", is_error=True)

    def _list(self, kind: str) -> ToolResult:
        if kind == "macro":
            idx = get_macro_index()
            items = [
                {"name": m.name, "description": m.description}
                for m in sorted(idx.macros.values(), key=lambda x: x.name)
            ]
        else:
            idx2 = skill_registry.get_index()
            items = [
                {"name": n, "description": d} for n, d in idx2.l1()
            ]
        if not items:
            return ToolResult(
                content=f"还没有任何 {kind}", display={"kind": kind, "items": []}
            )
        lines = [f"- {i['name']}：{i['description']}" for i in items]
        return ToolResult(
            content=f"现有 {len(items)} 个 {kind}：\n" + "\n".join(lines),
            display={"kind": kind, "items": items},
        )
