"""
远程访问鉴权的数据模型。

用户名 + 密码（个人场景），密码用 PBKDF2-SHA256 加盐哈希存储，
会话用不透明随机 token（库里只存哈希，泄库不泄会话）。

## 为什么密码哈希用标准库而不是 passlib/bcrypt

项目依赖全部精确钉版本（pyproject.toml == + uv.lock）。
passlib 已停止维护（多年未发版，与新版 bcrypt 不兼容），
argon2-cffi 需要编译依赖。PBKDF2-SHA256 是标准库方案，
OpenSSL 实现，性能与安全性对个人应用足够（60 万次迭代）。

## 为什么 session token 只存哈希

cookie 值是 32 字节随机 token，库里存它的 SHA-256。
这样即使数据库泄露，攻击者也拿不到有效会话 ——
token 只存在于用户浏览器的 cookie 里。
"""

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "auth_user"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 格式：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 停用的账号立即失效（中间件会同时校验 enabled），不必等会话过期。
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_login_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class AuthSession(Base, TimestampMixin):
    __tablename__ = "auth_session"
    __table_args__ = (
        Index("ix_auth_session_user_id", "user_id"),
        Index("ix_auth_session_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # token 的 SHA-256 hex。原始 token 只下发一次（写进 cookie）。
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(Text, default="", nullable=False)
