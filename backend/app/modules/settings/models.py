"""
用户可调的运行时设置。

## 为什么需要它

`core/config.py` 的值来自环境变量，改一次要重启。而记忆的超参数
（截断长度、迭代上限、相似度阈值）属于「用户按自己的模型调」的东西 ——
换个小窗口模型就要调截断，那不该需要改 .env 再重启。

## 为什么是 key-value 表而不是给每个设置一列

设置会持续增加，每加一个就要一次迁移。而这些值的共同点是
【都是标量且都有代码里的默认值】—— 表里只存"用户改过的那些"，
没有的行回落到 config.py 的默认值。

这也让"恢复默认"变成删行，而不是要记住默认值是多少。
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class AppSetting(Base, TimestampMixin):
    __tablename__ = "app_setting"

    # 点分路径，与 config.py 的结构对应："memory.keep_recent_turns"。
    #
    # 用点分字符串而非嵌套：SQL 里查一个具体设置是 WHERE key = ?，
    # 而嵌套结构要么存 JSON（改一个值要读改写整块），
    # 要么建多张表（过度设计）。
    key: Mapped[str] = mapped_column(String(128), primary_key=True)

    # 值一律存字符串，读取时按目标类型转。
    #
    # 不用 JSON 列：SQLite 的 JSON 支持依赖编译选项，而这些值都是标量，
    # 字符串 + 转换足够。转换失败时回落默认值（见 settings_service）。
    value: Mapped[str] = mapped_column(Text, nullable=False)
