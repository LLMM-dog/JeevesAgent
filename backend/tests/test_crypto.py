"""
加密/解密往返测试、版本前缀测试、密钥提示测试。
"""

from __future__ import annotations

import pytest
from app.core.crypto import decrypt, encrypt, key_hint
from app.core.exceptions import EncryptionNotConfiguredError


class TestEncryptDecryptRoundtrip:
    def test_roundtrip_ascii(self) -> None:
        plain = "sk-test-key-abcdef1234567890"
        cipher = encrypt(plain)
        assert cipher.startswith("v1:")
        assert decrypt(cipher) == plain

    def test_roundtrip_chinese(self) -> None:
        plain = "密钥包含中文-测试数据"
        cipher = encrypt(plain)
        assert decrypt(cipher) == plain

    def test_roundtrip_special_chars(self) -> None:
        plain = "!@#$%^&*()_+-=[]{}|;':\",.<>/?"
        cipher = encrypt(plain)
        assert decrypt(cipher) == plain

    def test_roundtrip_empty_string(self) -> None:
        plain = ""
        cipher = encrypt(plain)
        assert decrypt(cipher) == plain

    def test_roundtrip_long_string(self) -> None:
        plain = "A" * 10000
        cipher = encrypt(plain)
        assert decrypt(cipher) == plain

    def test_roundtrip_unicode(self) -> None:
        """包含 emoji 和 Unicode 的字符串。"""
        plain = "密码 🔑 测试 🧪 — 中文/English/日本語"
        cipher = encrypt(plain)
        assert decrypt(cipher) == plain

    def test_different_plain_produces_different_cipher(self) -> None:
        c1 = encrypt("hello")
        c2 = encrypt("world")
        assert c1 != c2

    def test_same_plain_produces_different_cipher(self) -> None:
        """每次加密产生不同密文（Fernet 使用随机 IV）。"""
        c1 = encrypt("hello")
        c2 = encrypt("hello")
        assert c1 != c2
        assert decrypt(c1) == decrypt(c2) == "hello"


class TestCipherFormat:
    def test_cipher_has_version_prefix(self) -> None:
        cipher = encrypt("test")
        assert cipher.startswith("v1:")

    def test_cipher_length_exceeds_plain(self) -> None:
        plain = "short"
        cipher = encrypt(plain)
        assert len(cipher) > len(plain)

    def test_cipher_is_alphanumeric_safe(self) -> None:
        """密文不包含换行或控制字符。"""
        cipher = encrypt("data")
        assert "\n" not in cipher
        assert "\r" not in cipher
        assert "\x00" not in cipher


class TestDecryptError:
    def test_decrypt_requires_v1_prefix(self) -> None:
        with pytest.raises(EncryptionNotConfiguredError):
            decrypt("no-prefix-data")

    def test_decrypt_old_version_rejected(self) -> None:
        with pytest.raises(EncryptionNotConfiguredError):
            decrypt("v0:some-old-cipher")

    def test_decrypt_corrupted_cipher_fails(self) -> None:
        plain = "valid-key"
        cipher = encrypt(plain)
        corrupted = cipher[:-5] + "XXXXX"
        with pytest.raises(EncryptionNotConfiguredError):
            decrypt(corrupted)

    def test_decrypt_truncated_cipher_fails(self) -> None:
        plain = "valid-key"
        cipher = encrypt(plain)
        truncated = cipher[:len(cipher) // 2]
        with pytest.raises(EncryptionNotConfiguredError):
            decrypt(truncated)

    def test_decrypt_with_wrong_prefix_fails(self) -> None:
        plain = "valid-key"
        cipher = encrypt(plain)
        tampered = "v2:" + cipher[3:]
        with pytest.raises(EncryptionNotConfiguredError):
            decrypt(tampered)


class TestKeyHint:
    def test_key_hint_returns_last_four(self) -> None:
        assert key_hint("sk-abcdef1234ghij") == "ghij"

    def test_key_hint_short_key_returns_stars(self) -> None:
        """短于 4 位时返回 ****。"""
        assert key_hint("abc") == "****"
        assert key_hint("") == "****"
        assert key_hint("x") == "****"

    def test_key_hint_exactly_four(self) -> None:
        assert key_hint("abcd") == "abcd"
