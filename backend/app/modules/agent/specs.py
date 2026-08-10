"""
智能体注册表：用 md + frontmatter 定义子智能体。

## 为什么用配置文件而不是让 LLM 现编

走的是相反的路 —— `assign_sub_assistant` 让主 LLM 在调用时传
`system_prompt`。结果是**仓库里找不到一个成型
的子代理资产**：每次委派的质量都在赌主模型当场的措辞，无法复用、无法
版本管理、无法 code review。

子代理的提示词是需要反复调优的东西。写在文件里才能 git diff。

有实现用 frontmatter 配了若干可直接
复用的 agent 定义。本项目照这条路。

## 工具集用白名单，不用黑名单

用黑名单 `forbiden_for_sub_agent`—— 任何**新增**
的危险工具默认对子代理开放，每加一个都要记得登记。

白名单默认安全：新增工具不会自动泄漏给子代理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.core.config import settings
from app.modules.skill.loader import Diagnostic, parse_frontmatter

log = structlog.get_logger(__name__)

# 不声明 tools 时给的保守默认。
#
# 关键是【不给全集】。同类实现.md 没写 tools 字段就拿到全集，
# 成了潜在的无限递归口子 —— 而很多实现没有深度防护。
DEFAULT_TOOLS: tuple[str, ...] = ("read_file", "list_dir", "glob", "grep")

# 子代理永远拿不到的工具，无论白名单里写了什么。
#
# 这是递归防护的【第一道】：模型看不到的工具不会去调，
# 比调了被拒更省一轮。第二道是 ContextVar 深度计数，见 subagent.py。
NEVER_FOR_SUBAGENT: frozenset[str] = frozenset({"subagent"})

MAX_DEPTH = 2


@dataclass(frozen=True)
class AgentSpec:
    name: str
    # 给主智能体看的"什么时候派这个子智能体"。
    # 它是主模型选择子代理的唯一依据，写"什么时候用"而不是"这是什么"。
    description: str
    prompt: str
    tools: tuple[str, ...] = DEFAULT_TOOLS
    # per-agent 模型。侦察类任务用便宜模型读 20 个文件，成本是主模型的
    # 十分之一。同类实现=Haiku / planner=Sonnet 是现成的最佳实践；
    # 和 都强制继承父模型，等于用最贵的模型去 grep。
    # per-agent 模型不在这里配，走 model_binding 表的 agent_name 字段
    #（在设置页绑定）。
    #
    # 理由：模型是【部署期】决定的，和"这台机器上配了哪些端点"绑在一起；
    # 而 spec 是【设计期】的资产，要能跨机器复用。把 model_id 写进 md 会让
    # 定义无法在另一台机器上直接用 —— 那台机器上未必有同名模型。
    #
    # resolve(agent_name=...) 的顺序是
    #   (agent_name, purpose) → ("", purpose) → ("", "chat")
    # 没单独绑就自动用全局默认。
    max_turns: int = 12
    source: str = "builtin"

    def allowed_tools(self, available: list[str]) -> list[str]:
        """白名单交上实际可用的工具集，再剔除永不下发的。"""
        wanted = set(self.tools) - NEVER_FOR_SUBAGENT
        return [t for t in available if t in wanted]


@dataclass
class AgentRegistry:
    specs: dict[str, AgentSpec] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def get(self, name: str) -> AgentSpec | None:
        return self.specs.get(name)

    def names(self) -> list[str]:
        return sorted(self.specs)

    def catalog(self) -> list[tuple[str, str]]:
        """给 subagent 工具描述用的 (name, description)。"""
        return sorted((s.name, s.description) for s in self.specs.values())


# ---------------------------------------------------------------------------
# 内置子智能体
#
# 只放两个。多了反而让主模型难选 —— 委派本身是有成本的决策，
# 候选太多它会花轮次在"该派谁"上。
# ---------------------------------------------------------------------------

_RESEARCHER_PROMPT = """\
你是调研型子智能体。任务是读大量材料后给出**结论**，不是转述材料。

## 你的输出会给一个没看过这些材料的人

这是最重要的一条。不要写"如上所述"、"根据前面的文件"——对方没有前面。
每个结论都要自带依据：说清是在哪个文件的哪一部分看到的。

## 工作方式

1. 先用 glob / grep 定位范围，不要一上来就逐个读文件
2. 读的时候记下文件路径和关键行号
3. 给结论时按"结论 → 依据"组织，不要按"我读了什么"组织

## 输出约束

- 只回答被指派的任务，不附加未被要求的建议
- 不要在结尾提问
- 不要提议下一步动作，除非任务里明确要求
- 结论优先，细节其次。对方要的是判断，不是原始材料

## 你没有写入和执行权限

这是刻意的。如果任务需要改文件或跑命令，直接说明这一点，不要绕路尝试。
"""

_REVIEWER_PROMPT = """\
你是代码审查型子智能体。任务是找出**具体问题**，不是给泛泛的评价。

## 什么算有效的审查意见

有效：指出某个文件某行的具体问题，说明什么情况下会出错，以及后果。
无效："建议增强错误处理"、"代码可以更简洁"、"注意性能"。

如果读完没发现真问题，就说没发现——**编造问题比漏掉问题更糟**，
因为它会让对方浪费时间去改不存在的缺陷。

## 优先看什么

按严重程度排序，不要按文件顺序：

1. 会导致数据损坏或丢失的
2. 安全问题（路径穿越、注入、凭据泄漏）
3. 并发与取消路径上的问题（这类平时不出错，出错时极难排查）
4. 错误处理缺失，尤其是"静默失败"
5. 边界条件

## 输出约束

- 每条意见给出 `文件:行号`
- 说清"什么情况下会触发"，不只说"这里有问题"
- 只回答被指派的任务，不附加未被要求的建议
- 不要在结尾提问

## 你没有写入权限

只报告问题，不要改。改动由派你来的智能体决定。
"""


BUILTIN_SPECS: dict[str, AgentSpec] = {
    "researcher": AgentSpec(
        name="researcher",
        description=(
            "调研型子智能体。当需要读大量文件或搜索后给出结论时派它 —— "
            "它读完只回结论，中间材料不进你的上下文。"
            "无写入、无执行权限。"
        ),
        prompt=_RESEARCHER_PROMPT,
        tools=("read_file", "list_dir", "glob", "grep", "load_skill", "load_skill_file"),
        max_turns=16,
    ),
    "reviewer": AgentSpec(
        name="reviewer",
        description=(
            "代码审查型子智能体。当需要对一批改动或某个模块做审查时派它。"
            "它只报告具体问题（带文件:行号），不改代码。无写入权限。"
        ),
        prompt=_REVIEWER_PROMPT,
        tools=("read_file", "list_dir", "glob", "grep"),
        max_turns=14,
    ),
}


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def load_specs(root: Path | None = None) -> AgentRegistry:
    """
    内置 spec 加用户自定义的（`agents/*.md`）。

    用户定义同名时**覆盖内置** —— 内置的只是默认值，用户想改 researcher
    的提示词应该能直接改，不用换个名字。

    和技能加载一样：每份文件独立降级，一个坏定义不影响其它。
    """
    reg = AgentRegistry(specs=dict(BUILTIN_SPECS))
    base = root if root is not None else settings.agents_dir
    if not base.is_dir():
        return reg

    for f in sorted(base.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            reg.diagnostics.append(Diagnostic("warning", f"读取失败：{e}", str(f)))
            continue

        meta, body = parse_frontmatter(text)
        desc = meta.get("description")
        if not desc or not str(desc).strip():
            # description 是主模型选择子代理的唯一依据，没有它这个定义没用
            reg.diagnostics.append(
                Diagnostic("warning", "缺 description，跳过", str(f))
            )
            continue
        if not body.strip():
            # 正文就是 system prompt，空的话子代理没有人格 ——
            # 子代理就是这样，它会像跟人聊天一样回答父代理
            reg.diagnostics.append(Diagnostic("warning", "正文为空，跳过", str(f)))
            continue

        name = _one_line(meta.get("name") or f.stem)
        raw_tools = meta.get("tools")
        if isinstance(raw_tools, str):
            # 支持空格或逗号分隔的一行写法
            tools = tuple(t for t in raw_tools.replace(",", " ").split() if t)
        elif isinstance(raw_tools, list):
            tools = tuple(str(t) for t in raw_tools)
        else:
            tools = DEFAULT_TOOLS

        try:
            max_turns = int(meta.get("max_turns") or 12)
        except (TypeError, ValueError):
            max_turns = 12

        reg.specs[name] = AgentSpec(
            name=name,
            description=_one_line(desc),
            prompt=body,
            tools=tools or DEFAULT_TOOLS,
            max_turns=max(1, min(max_turns, 40)),
            source=str(f),
        )

    log.info("agent_specs_loaded", count=len(reg.specs), names=reg.names())
    for d in reg.diagnostics:
        log.warning("agent_spec_diagnostic", msg=d.message, path=d.path)
    return reg


_registry: AgentRegistry | None = None


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = load_specs()
    return _registry


def reload() -> AgentRegistry:
    global _registry
    _registry = load_specs()
    return _registry


def reset() -> None:
    global _registry
    _registry = None
