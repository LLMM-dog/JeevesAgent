"""
字段合并策略。

四种 merge_op 的实现。最重要的是 PATCH —— 它对字符串是【SEARCH/REPLACE 块】，
不是追加。

## 为什么不是追加

旧实现（已删）是"字符串追加，重复则跳过"，有两个致命问题：

1. 只增不减。profile.md 无限膨胀，而"用户去年用 flake8，今年换 ruff"这种
   事实修正做不到 —— 旧句子永远留着，模型同时看到两个矛盾的事实。
2. 去重靠子串包含。改一个字就判定为新内容，同一件事以七八种措辞并存。

## 为什么第一版只做精确匹配

OpenViking 有一个 48KB 的 patch_handler.py：Levenshtein 相似度 + 行窗口滑动 +
标记序列校验，容忍 LLM 抄错缩进。

模糊匹配的风险是【改错地方且不报错】，而精确匹配失败是显式的、可重试的。
个人项目的记忆量下，多一轮 LLM 重试比静默改错便宜。

扩展点留在 PatchOp 的 matcher 参数上 —— 要上模糊匹配只加一个 matcher 实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from app.modules.memory.schema import FieldType, MemoryField, MergeOp

log = structlog.get_logger(__name__)


class MergeError(ValueError):
    """
    合并失败。

    ## 为什么抛异常而不是静默保留原值

    调用方（提取流程）需要知道"这个 patch 没打上"才能把失败信息回给 LLM 重试。
    静默保留原值会让 LLM 以为写成功了，下一轮 SEARCH 又基于它想象的内容，
    错误一路放大。

    OpenViking 在 memory_updater.py:1086 catch 了 merge 异常并保留原值，
    但它同时有 _validate_patch_operations 在【写入前】预检（extract_loop.py:782）。
    我们把校验和执行合并在一处，靠异常传递失败。
    """

    def __init__(self, message: str, *, field_name: str = "", search: str = ""):
        super().__init__(message)
        self.field_name = field_name
        self.search = search


@dataclass(frozen=True)
class SearchReplaceBlock:
    """一个 SEARCH/REPLACE 块。search 必须是原文里唯一的最小片段。"""

    search: str
    replace: str


@dataclass(frozen=True)
class StrPatch:
    """字符串补丁。string + merge_op=patch 的字段，LLM 输出这个形状。"""

    blocks: tuple[SearchReplaceBlock, ...] = ()

    @property
    def first_replace(self) -> str:
        return self.blocks[0].replace if self.blocks else ""

    @classmethod
    def from_raw(cls, raw: Any) -> StrPatch | None:
        """
        从 LLM 输出（dict / 已构造对象）解析。不是补丁形状时返回 None，
        由调用方当整体替换处理 —— LLM 偶尔会对 patch 字段直接给一个字符串。
        """
        if isinstance(raw, StrPatch):
            return raw
        if not isinstance(raw, dict) or "blocks" not in raw:
            return None

        blocks: list[SearchReplaceBlock] = []
        for item in raw.get("blocks") or []:
            if isinstance(item, SearchReplaceBlock):
                blocks.append(item)
            elif isinstance(item, dict):
                # replace 缺失时当空串（= 删除匹配内容），这是合法意图。
                # search 缺失则这个块无效，跳过。
                search = item.get("search")
                if search is None:
                    continue
                blocks.append(SearchReplaceBlock(search=str(search), replace=str(item.get("replace") or "")))
        return cls(blocks=tuple(blocks))


class TextMatcher(Protocol):
    """
    在原文里定位 search 片段。返回 (start, end)，找不到返回 None。

    抽出来是为了之后能换成模糊匹配而不动 PatchOp。
    """

    def find(self, content: str, search: str) -> tuple[int, int] | None: ...


class ExactMatcher:
    """
    精确匹配 + 行尾空白归一化。

    ## 为什么归一化行尾空白

    LLM 复制原文时经常丢掉行尾的空格，或者把 CRLF 写成 LF。这两种差异
    对内容毫无意义，但会让精确匹配失败。归一化后比对能挡掉这类假失败，
    而缩进（行首空白）【不归一化】—— 那是 Markdown 列表层级，有语义。

    ## 为什么要求唯一

    search 在原文里出现多次时不猜第一个 —— 那有 50% 概率改错地方。
    直接失败让 LLM 补充上下文重试。
    """

    def find(self, content: str, search: str) -> tuple[int, int] | None:
        idx = content.find(search)
        if idx >= 0:
            if content.find(search, idx + 1) >= 0:
                raise MergeError(
                    f"SEARCH 片段在原文中出现多次，无法定位。请加入更多上下文让它唯一：{_preview(search)}",
                    search=search,
                )
            return idx, idx + len(search)

        return self._find_normalized(content, search)

    def _find_normalized(self, content: str, search: str) -> tuple[int, int] | None:
        """
        按行归一化后再找。命中时返回【原文】里的偏移，不是归一化后的 ——
        替换要作用在原文上。
        """
        search_lines = [ln.rstrip() for ln in search.replace("\r\n", "\n").split("\n")]
        if not search_lines:
            return None

        # 原文按行切分，同时记录每行在原文里的起始偏移。
        offsets: list[int] = []
        pos = 0
        raw_lines: list[str] = []
        for line in content.replace("\r\n", "\n").split("\n"):
            offsets.append(pos)
            raw_lines.append(line)
            pos += len(line) + 1

        norm = [ln.rstrip() for ln in raw_lines]
        n = len(search_lines)
        hits = [i for i in range(len(norm) - n + 1) if norm[i : i + n] == search_lines]
        if not hits:
            return None
        if len(hits) > 1:
            raise MergeError(
                f"SEARCH 片段在原文中出现多次，无法定位。请加入更多上下文让它唯一：{_preview(search)}",
                search=search,
            )

        start_line = hits[0]
        start = offsets[start_line]
        end_line = start_line + n - 1
        end = offsets[end_line] + len(raw_lines[end_line])
        return start, end


def _preview(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


def apply_str_patch(current: str, patch: StrPatch, *, matcher: TextMatcher | None = None) -> str:
    """
    依次应用所有块。

    ## 为什么串行而不是一次性算好所有位置

    每个块都在【上一个块的结果】上定位。这让 LLM 能连续修改相邻内容
    （先删一行、再在同一位置插入），而一次性定位会让第二个块的偏移失效。

    代价是前一个块的 replace 内容可能意外匹配后一个块的 search。实践中
    没遇到 —— search 通常是原文片段，而 replace 是新内容。
    """
    matcher = matcher or ExactMatcher()
    out = current
    for block in patch.blocks:
        # 目标内容已经在原文里 → 这个块已经打过了，跳过。
        #
        # ## 为什么需要这条
        #
        # "把 A 扩写成 A+B" 这类 patch（search="A", replace="A\nB"）
        # 在重复应用时会再匹配一次 A 并再插一份 B。实测：同一个场景连跑两次，
        # tool_notes 里出现了 4 份 "## 常见失败"。
        #
        # 提取流程理论上不该重复输出同一个 patch，但实践中会：
        # 模型对同一段对话重新提取、用户手动重跑、commit 失败后重试。
        # 让重复应用变成 no-op 比要求上游永不重复更可靠。
        #
        # 判据用 replace 而非 search：search 在原文里【本来就存在】
        # （否则匹配不上），拿它判断永远为真。
        if block.replace and block.replace in out:
            log.debug("patch_block_already_applied", preview=_preview(block.replace))
            continue

        if not block.search:
            # 有原文时空 search 是非法的（空串能匹配任意位置）。
            # 不报错而是跳过 —— LLM 偶尔用空 search 表示"追加"，
            # 而那个意图应该由它显式写出 search 上下文来表达。
            # OpenViking 在 merge_op/patch.py:70 也是过滤而非报错。
            log.debug("patch_empty_search_skipped")
            continue

        span = matcher.find(out, block.search)
        if span is None:
            raise MergeError(
                f"SEARCH 片段在原文中找不到：{_preview(block.search)}",
                search=block.search,
            )
        start, end = span
        out = out[:start] + block.replace + out[end:]
    return out


def apply_merge(field: MemoryField, current: Any, incoming: Any) -> Any:
    """
    对一个字段应用它声明的 merge_op。current 为 None 表示新文件。

    返回合并后的值。失败抛 MergeError。
    """
    if field.merge_op is MergeOp.IMMUTABLE:
        # 首次写入后永不改。这是"事件名不该变"这类约束的实现。
        return current if current is not None else incoming

    if field.merge_op is MergeOp.SUM:
        return _apply_sum(current, incoming)

    if field.merge_op is MergeOp.PATCH:
        return _apply_patch(field, current, incoming)

    return _apply_replace(current, incoming)


def _apply_replace(current: Any, incoming: Any) -> Any:
    """
    全量覆盖，但【空值不覆盖】。

    LLM 在不确定一个字段时倾向于输出空串而不是省略它。把空串写进去会
    抹掉已有的有效值 —— 那是一次不可见的信息丢失。

    ## 智能降级 patch → replace

    如果 incoming 是 `{"blocks": [{"search": "", "replace": "..."}]}`（单个
    空 search 的 patch），自动展开成 replace —— 这让 LLM 可以统一用
    patch 语法，而不用记住"某些字段必须用纯字符串"。

    实测：experiences 的 content 声明为 replace，但测试传的是 patch。
    不展开的话模板会渲染成 "{'blocks': [...]}"（字典的 repr）。
    """
    if incoming is None or incoming == "":
        return current

    # 智能降级：单 block + 空 search → 取 replace
    if isinstance(incoming, dict) and "blocks" in incoming:
        blocks = incoming.get("blocks", [])
        if (
            isinstance(blocks, list)
            and len(blocks) == 1
            and isinstance(blocks[0], dict)
            and not blocks[0].get("search")
        ):
            return blocks[0].get("replace", "")

    return incoming


def _apply_sum(current: Any, incoming: Any) -> Any:
    try:
        base = float(current or 0)
        delta = float(incoming or 0)
    except (TypeError, ValueError):
        raise MergeError(f"sum 字段的值不是数字：current={current!r} incoming={incoming!r}") from None
    total = base + delta
    # 计数器不该为负。LLM 偶尔输出负数表示"减少"，但累加语义下那是错的。
    total = max(total, 0.0)
    return int(total) if float(total).is_integer() else total


def _apply_patch(field: MemoryField, current: Any, incoming: Any) -> Any:
    # 非字符串字段的 patch 等同 replace。OpenViking 同（merge_op/patch.py:52）。
    if field.type is not FieldType.STRING:
        return _apply_replace(current, incoming)

    patch = StrPatch.from_raw(incoming)

    # 没有原文时【不做匹配】，直接取第一个块的 replace。
    # 漏掉这条的后果：新建文件永远写不进内容 —— search 匹配不上空串。
    # 对应 OpenViking 的 _extract_replace_when_no_original（patch.py:104）。
    if current is None or current == "":
        if patch is not None:
            return patch.first_replace
        return incoming if isinstance(incoming, str) else ""

    if patch is None:
        # LLM 对 patch 字段直接给了字符串（而不是 SEARCH/REPLACE 块）。
        #
        # 当整体替换处理 —— 拒绝它没有好处，而空值仍然不覆盖。
        #
        # 但【必须留痕】：这会丢掉已有正文里没被新内容覆盖的部分。
        # 实测在试验场里踩到：tool_notes 的"适用场景/参数要点"两节
        # 被一条只写了"常见失败"的裸字符串整体顶掉，而 diff 只显示
        # 一次正常的 update，看不出丢了东西。
        #
        # 不改成报错的理由：报错会让这条记忆完全写不进去，
        # 而部分正文总比没有好。留痕让它可被发现。
        if isinstance(incoming, str) and incoming.strip():
            log.warning(
                "memory_patch_field_got_plain_string",
                field=field.name,
                chars_lost=len(str(current)),
                chars_new=len(incoming),
                hint="patch 字段收到裸字符串，已有正文被整体替换。LLM 应输出 blocks",
            )
        return _apply_replace(current, incoming)

    if not patch.blocks:
        return current

    try:
        return apply_str_patch(str(current), patch)
    except MergeError as e:
        # 补上字段名，让上层的错误信息能指出是哪个字段的 patch 失败。
        raise MergeError(str(e), field_name=field.name, search=e.search) from None
