"""
带类型前缀的 ID 生成。

格式 <前缀>_<base62 12位>，如 ses_7bK2mQ9xR4Lp。

为什么带前缀：日志里看到一个 ID 立刻知道是什么，不用回去查上下文；
跨表误用（把 session_id 传给了要 message_id 的地方）在开发阶段就能看出来。

为什么不用自增整数：前端和 URL 里出现连续整数容易误操作（改个数字就到了别的记录）。

为什么不用 UUID：36 字符太长，日志和调试时占屏。
base62 12 位约 71 bit 随机性，单人项目远够。
"""

import secrets

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_LENGTH = 12


def _rand() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def new_id(prefix: str) -> str:
    return f"{prefix}_{_rand()}"


# 前缀表与 docs/02-data/schema.md 的 ID 规范一致。
def session_id() -> str:
    return new_id("ses")


def message_id() -> str:
    return new_id("msg")


def run_id() -> str:
    return new_id("run")


def span_id() -> str:
    return new_id("spn")


def todo_id() -> str:
    return new_id("todo")


def endpoint_id() -> str:
    return new_id("ept")


def model_id() -> str:
    return new_id("mdl")


def binding_id() -> str:
    return new_id("bnd")


def memory_id() -> str:
    return new_id("mem")


def attachment_id() -> str:
    return new_id("att")


def workspace_id() -> str:
    return new_id("wsp")


def path_id() -> str:
    return new_id("pth")

def cron_task_id() -> str:
    return new_id("crt")


def cron_run_id() -> str:
    return new_id("crr")
