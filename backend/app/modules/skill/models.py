"""
技能的启用状态。

## 为什么用数据库而不是写进 SKILL.md

启用与否是【用户的偏好】，不是技能作者的属性。写进 frontmatter 的话：

  1. 用户关掉一个技能就修改了技能作者的文件。下次升级技能包
     （POST /skills/upload 带 overwrite）这个开关被冲掉。
  2. 技能目录是可以整体删掉重装的，开关状态应该活得比文件长。
  3. zip 上传的技能是第三方内容，往里写东西等于污染它。

## 为什么只存"被关掉的"

表里没有记录 = 启用。这样新装的技能默认是开的 ——
默认关闭会让用户装完发现模型看不见它，而没有任何提示说明原因。

也意味着删掉技能再装回来时开关状态还在（按名字关联），
那是符合直觉的：用户关掉的是"这个技能"，不是"这个文件"。
"""

from __future__ import annotations

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class SkillState(Base, TimestampMixin):
    __tablename__ = "skill_state"
    __table_args__ = (UniqueConstraint("name", name="uq_skill_state_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)

    # 技能名（SKILL.md frontmatter 里的 name，不是目录名）。
    #
    # 用名字而不是路径关联：技能可以被移动目录，而模型看到的一直是名字。
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    # 0 = 用户关掉了。表里没有记录也视为启用 —— 见模块 docstring。
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
