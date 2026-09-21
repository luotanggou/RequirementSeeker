"""使用数据集级秘密生成稳定且类型域隔离的伪 ID。"""

import hashlib
import hmac
import os
from typing import Literal, Self


class SecretConfigurationError(ValueError):
    """秘密配置错误；消息不得包含秘密值。"""


class IdentifierPseudonymizer:
    """只在内存中保存秘密，不提供原始 ID 到伪 ID 的映射。"""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise SecretConfigurationError("dataset_secret_too_short")
        self._secret = secret

    @classmethod
    def from_environment(cls, variable_name: str) -> Self:
        """按变量名读取秘密；异常只包含变量名和固定错误码。"""

        value = os.environ.get(variable_name)
        if value is None:
            raise SecretConfigurationError(f"dataset_secret_missing:{variable_name}")
        return cls(value.encode("utf-8"))

    def _make(self, kind: Literal["video", "author", "comment"], raw_id: str) -> str:
        payload = f"{kind}\0{raw_id}".encode()
        digest = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()[:32]
        return f"{kind}_{digest}"

    def video(self, raw_id: str) -> str:
        return self._make("video", raw_id)

    def author(self, raw_id: str) -> str:
        return self._make("author", raw_id)

    def comment(self, raw_id: str) -> str:
        return self._make("comment", raw_id)

    def __repr__(self) -> str:
        return "IdentifierPseudonymizer(<redacted>)"
