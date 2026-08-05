"""
API Key 加解密。

只存密文 + key_hint（尾 4 位），没有明文列。任何接口都不返回明文。

密文带 "v1:" 版本前缀：将来换加密算法时能识别旧密文并平滑迁移。
没有版本前缀的话，换算法就得全表重新加密且无法回滚。
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.exceptions import EncryptionNotConfiguredError

_PREFIX = "v1:"


def _fernet() -> Fernet:
    key = settings.security.encryption_key.strip()
    if not key:
        # 这个错误正常不该出现 —— main.py 的启动期校验会先拒绝启动。
        # 留在这里是为了让直接调用 crypto 的测试/脚本也能得到明确提示。
        raise EncryptionNotConfiguredError(
            "未配置加密密钥",
            hint=(
                "在 .env 里设置 SECURITY__ENCRYPTION_KEY。生成方式："
                'uv run python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ),
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise EncryptionNotConfiguredError(
            "加密密钥格式不合法",
            hint="必须是 Fernet.generate_key() 生成的 44 字符 base64 串",
        ) from e


def encrypt(plain: str) -> str:
    return _PREFIX + _fernet().encrypt(plain.encode()).decode()


def decrypt(cipher: str) -> str:
    if not cipher.startswith(_PREFIX):
        raise EncryptionNotConfiguredError(
            "密文格式无法识别",
            hint="该记录可能是用不同版本的加密方案写入的，请重新填写 API Key",
        )
    try:
        return _fernet().decrypt(cipher[len(_PREFIX) :].encode()).decode()
    except InvalidToken as e:
        # 最常见的原因：换过 ENCRYPTION_KEY。必须把这个可能性写进提示,
        # 否则用户只会看到"解密失败"而想不到是密钥变了。
        raise EncryptionNotConfiguredError(
            "API Key 解密失败",
            hint="加密密钥可能已变更。请到设置页重新填写该供应商的 API Key",
        ) from e


def key_hint(plain: str) -> str:
    """尾 4 位，供用户辨认是哪个 Key。"""
    return plain[-4:] if len(plain) >= 4 else "****"
