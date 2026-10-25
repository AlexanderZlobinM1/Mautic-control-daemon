from __future__ import annotations
import base64, hashlib, os, stat
from pathlib import Path
from typing import Any, Mapping
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcd_agent.commercial_portal import canonical_json

def ensure_result_key(path: str) -> tuple[Ed25519PrivateKey, dict[str, str]]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
        if target.is_symlink() or mode != 0o600 or target.stat().st_uid != os.getuid():
            raise ValueError("mcd_result_key_permissions_invalid")
        private = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(target.read_text().strip() + "=" * (-len(target.read_text().strip()) % 4)))
    else:
        private = Ed25519PrivateKey.generate()
        target.write_text(base64.urlsafe_b64encode(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).rstrip(b"=").decode() + "\n")
        os.chmod(target, 0o600)
    public = base64.urlsafe_b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    return private, {"key_id": "mcd-result-" + hashlib.sha256(base64.urlsafe_b64decode(public + "=" * (-len(public) % 4))).hexdigest()[:24], "algorithm": "ed25519", "value": public}

def sign_result(value: Mapping[str, Any], private: Ed25519PrivateKey, key_id: str) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop("signature", None)
    signed = dict(unsigned)
    signed["signature"] = {"algorithm": "ed25519", "key_id": key_id, "value": base64.urlsafe_b64encode(private.sign(canonical_json(unsigned))).rstrip(b"=").decode()}
    return signed
