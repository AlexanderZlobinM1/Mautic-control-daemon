from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet


def _derive_fernet_key(raw: str) -> bytes:
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecretStore:
    def __init__(self, *, key_path: str, env_master_key: str | None = "MCD_MASTER_KEY") -> None:
        self.key_path = Path(key_path)
        self.env_master_key = env_master_key
        self._fernet = Fernet(self._load_key())

    def _load_key(self) -> bytes:
        env_name = (self.env_master_key or "").strip()
        if env_name:
            env_value = os.environ.get(env_name, "").strip()
            if env_value:
                return _derive_fernet_key(env_value)

        if self.key_path.exists():
            raw = self.key_path.read_bytes().strip()
            if raw:
                return raw

        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self.key_path.write_bytes(key + b"\n")
        try:
            os.chmod(self.key_path, 0o600)
        except Exception:
            pass
        return key

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")

