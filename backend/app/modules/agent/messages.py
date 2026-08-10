"""
Agent 内部的消息表示与上下文一致性修复。

这里的 Msg 是【内存表示】，与 DB 的 Message 模型分开：
- DB 模型带 seq / created_at / 外键等持久化关注点
- Msg 只关心"发给 LLM 需要什么"

分开的理由：压缩会重写消息列表（删中段、插摘要），在 ORM 对象上做这个
很容易误触发 flush 把中间态写进库。
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

Role = Literal["user", "assistant", "tool", "system", "summary", "artifact"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"

    def parsed_args(self) -> dict[str, Any]:
        """
        参数解析失败返回空 dict 而非抛异常 —— 模型偶尔会吐出不完整的 JSON,
        这时应该让工具收到空参数并返回"参数不合法"的错误文本让模型自我纠正,
        而不是让整轮对话崩掉。
        """
        try:
            v = json.loads(self.arguments or "{}")
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            log.warning("tool_args_parse_failed", tool=self.name, raw=self.arguments[:200])
            return {}


@dataclass
class Msg:
    role: Role
    content: str = ""
    reasoning: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    agent_name: str = ""
    # 落库后回填，用于事件里引用具体消息
    message_id: str | None = None
    # 图片的 data URL 列表。只有 user 消息会有。
    #
    # 【只在当前这一轮携带】—— 从库里读历史消息时不还原它。
    # 见 to_api 里的说明。
    images: list[str] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        """
        转成 OpenAI 兼容格式。

        summary / artifact 是本地专用角色，发给 LLM 时要映射成标准角色：
        - summary → user。它是【模型对用户内容的转述】，源自用户输入，
          放 system 位等于给注入开了升格通道（用户说"忽略之前的指令"，
          被摘要进去后就变成了系统级指令）。
        - artifact → assistant。它就是模型自己的产出。
        """
        if self.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": self.tool_call_id or "",
                "content": self.content,
            }

        if self.role == "assistant":
            out: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
            if self.tool_calls:
                out["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in self.tool_calls
                ]
                # 思维链【只在带 tool_calls 的轮次】回传。
                #
                # DeepSeek 文档（Thinking Mode / Tool Calls）的规则：
                #   两个 user 消息之间没有工具调用 → reasoning 不必传，传了也会被忽略
                #   两个 user 消息之间有工具调用   → reasoning 必须传，让模型延续思考
                # 文档甚至说带 tools 时不传会 400。
                #
                # 实测这个端点四种组合全返回 200，没有触发 400 —— 文档比实际
                # 行为严格。但"让模型延续上一步的推理"这条对 agent 是实质收益：
                # 一个 tool_call 轮次的 content 往往是空的（真实观测到
                # content=0 字符 / reasoning=306 字符），思维链就是它全部的思考。
                # 丢掉它等于让模型每一步都从头想。
                #
                # 不带 tool_calls 的轮次不传：那种情况上游会忽略，纯浪费 token。
                if self.reasoning and settings.llm.send_reasoning_back:
                    out["reasoning_content"] = self.reasoning
            return out

        if self.role == "artifact":
            return {"role": "assistant", "content": self.content}

        if self.role == "summary":
            return {"role": "user", "content": self.content}

        if self.role == "user" and self.images:
            # 多模态 content。
            #
            # ## 为什么没图片时必须回字符串而不是 [{"type":"text"}]
            #
            # 不是所有 OpenAI 兼容端点都接受数组形式。有些中转站只实现了
            # 字符串分支，收到数组直接 400 或静默丢内容。既然没图片时两种
            # 写法等价，就用兼容性更好的那个 —— 所以这个分支只在有图时进。
            from app.modules.endpoint.vision import ImagePart, decode_data_url

            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            for url in self.images:
                decoded = decode_data_url(url)
                if decoded is None:
                    # 坏的 data URL 直接跳过，不让整轮请求失败。
                    # 图片损坏时用户宁可"这张没看到"，也不要"整段对话报错"。
                    continue
                mime, raw = decoded
                import base64 as _b64

                parts.append(
                    ImagePart(mime=mime, data_b64=_b64.b64encode(raw).decode()).to_api()
                )
            if parts:
                return {"role": "user", "content": parts}

        return {"role": self.role, "content": self.content}


def repair_tool_pairing(msgs: list[Msg]) -> tuple[list[Msg], int]:
    """
    修复 tool 配对，返回 (修复后的列表, 修复动作数)。

    ## 为什么必须有这个函数

    两类不一致都会让 LLM 请求【直接 400】，且错误信息只说"messages 格式不对",
    不指明是哪条：

      1. assistant 带 tool_calls 但缺少对应的 tool 消息  → 补占位
      2. tool 消息前面找不到声明它的 assistant           → 丢弃

    ## 为什么不能只在取消路径里修

    产生原因不只是取消：进程崩溃、断电、手动改库都会产生同样的不一致，
    而它们走不到取消处理代码。只修取消路径的话，任何非正常退出都会留下一个
    【永久坏掉的会话】—— 每次打开都 400，用户完全不知道为什么，只能删掉重开。

    所以这里不做"是否需要修复"的判断，从 DB 组装上下文时无条件跑一遍。

    为此专门写了 _inject_cancel_tool_messages，
    并且踩了 as_node="tools" 的坑（不传会让 aupdate_state 重新评估条件边、
    返回不存在的目的地、KeyError 静默失败）。我们不用 checkpointer、
    从 DB 重组，所以简单得多也可靠得多。
    """
    out: list[Msg] = []
    fixes = 0
    i = 0
    n = len(msgs)

    while i < n:
        m = msgs[i]

        if m.role == "tool":
            # 情况 2：孤立的 tool 消息（前面没有声明它的 assistant）。
            # 走到这里说明它没有被下面的 assistant 分支消费掉。
            fixes += 1
            log.warning(
                "orphan_tool_message_dropped",
                tool_call_id=m.tool_call_id,
                tool=m.tool_name,
            )
            i += 1
            continue

        out.append(m)

        if m.role == "assistant" and m.tool_calls:
            # 收集紧跟其后的 tool 消息
            expected = {tc.id for tc in m.tool_calls}
            j = i + 1
            got: dict[str, Msg] = {}
            while j < n and msgs[j].role == "tool":
                tm = msgs[j]
                if tm.tool_call_id in expected:
                    got[tm.tool_call_id] = tm
                else:
                    # 属于别的 assistant 的 tool 结果，或纯粹的脏数据
                    fixes += 1
                    log.warning("stray_tool_message_dropped", tool_call_id=tm.tool_call_id)
                j += 1

            # 情况 1：按 tool_calls 的原始顺序输出，缺失的补占位。
            # 顺序必须与 tool_calls 一致 —— 有些上游会校验这一点。
            for tc in m.tool_calls:
                existing = got.get(tc.id)
                if existing is not None:
                    out.append(existing)
                else:
                    fixes += 1
                    log.warning("missing_tool_result_filled", tool_call_id=tc.id, tool=tc.name)
                    out.append(
                        Msg(
                            role="tool",
                            content="（该工具调用未完成）",
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            is_error=True,
                            agent_name=m.agent_name,
                        )
                    )
            i = j
            continue

        i += 1

    return out, fixes


def find_missing_tool_calls(msgs: list[Msg]) -> list[ToolCall]:
    """
    找出最后一条 assistant 消息里没有对应 tool 结果的 tool_calls。

    取消处理用这个来补占位并落库 —— 这是修复的第一道，
    repair_tool_pairing 是组装上下文时的兜底第二道。
    """
    last_ai_idx = -1
    for idx in range(len(msgs) - 1, -1, -1):
        if msgs[idx].role == "assistant":
            last_ai_idx = idx
            break
    if last_ai_idx < 0:
        return []

    ai = msgs[last_ai_idx]
    if not ai.tool_calls:
        return []

    answered = {
        m.tool_call_id for m in msgs[last_ai_idx + 1 :] if m.role == "tool" and m.tool_call_id
    }
    return [tc for tc in ai.tool_calls if tc.id not in answered]
