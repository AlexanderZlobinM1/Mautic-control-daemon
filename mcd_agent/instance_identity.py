"""MCC-owned instance identity accepted and persisted by MCD.

MCD deliberately does not mint new MCC identities.  It only validates the
identity supplied by MCC, keeps it beside the installation and returns the
observed values in operation receipts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


INSTANCE_IDENTITY_MARKER = ".mcd-instance-identity.json"
INSTANCE_IDENTITY_SCHEMA = "mcd-instance-identity-v1"

# Crockford Base32, lower-case canonical form.  I, L, O and U are omitted;
# the agreed example ``7k4m2`` is valid in this alphabet.
_INSTANCE_CODE_RE = re.compile(r"^[0-9a-hj-km-np-tv-z]{5}$")
_INSTANCE_UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


def validate_instance_code(value: object) -> str:
    code = str(value or "").strip()
    if not _INSTANCE_CODE_RE.fullmatch(code):
        raise ValueError("instance_code must be a five-character lower-case Crockford Base32 code")
    return code


def validate_mcc_instance_uid(value: object) -> str:
    uid = str(value or "").strip()
    if not _INSTANCE_UID_RE.fullmatch(uid) or "@" in uid:
        raise ValueError("instance_uid must be a bare MCC UID without host qualification")
    return uid


def _marker_path(root: str | Path) -> Path:
    return Path(root) / INSTANCE_IDENTITY_MARKER


def read_instance_identity(root: str | Path) -> dict[str, str] | None:
    """Read a valid local identity marker; missing/invalid legacy markers are ignored."""
    path = _marker_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != INSTANCE_IDENTITY_SCHEMA:
        return None
    try:
        uid = validate_mcc_instance_uid(raw.get("instance_uid"))
        code = validate_instance_code(raw.get("instance_code"))
    except ValueError:
        return None
    root_value = str(raw.get("root") or "").strip()
    if root_value and Path(root_value).resolve() != Path(root).resolve():
        return None
    domain = str(raw.get("primary_domain") or "").strip().lower().rstrip(".")
    return {
        "instance_uid": uid,
        "instance_code": code,
        "primary_domain": domain,
        "root": str(Path(root).resolve()),
    }


def write_instance_identity(
    root: str | Path,
    *,
    instance_uid: str,
    instance_code: str,
    primary_domain: str | None = None,
) -> dict[str, str]:
    """Persist the MCC identity atomically and return the actual marker state."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError(f"instance root is not a directory: {root_path}")
    uid = validate_mcc_instance_uid(instance_uid)
    code = validate_instance_code(instance_code)
    domain = str(primary_domain or "").strip().lower().rstrip(".")
    payload: dict[str, Any] = {
        "schema": INSTANCE_IDENTITY_SCHEMA,
        "instance_uid": uid,
        "instance_code": code,
        "primary_domain": domain,
        "root": str(root_path.resolve()),
        "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    marker = _marker_path(root_path)
    tmp = marker.with_name(f".{marker.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(marker)
    return {
        "instance_uid": uid,
        "instance_code": code,
        "primary_domain": domain,
        "root": str(root_path.resolve()),
    }


def identity_readback(
    root: str | Path,
    *,
    instance_uid: str | None = None,
    instance_code: str | None = None,
    primary_domain: str | None = None,
) -> dict[str, str]:
    """Return observed identity, using explicit values only as a receipt fallback."""
    observed = read_instance_identity(root) or {}
    uid = observed.get("instance_uid") or (validate_mcc_instance_uid(instance_uid) if instance_uid else "")
    code = observed.get("instance_code") or (validate_instance_code(instance_code) if instance_code else "")
    domain = observed.get("primary_domain") or str(primary_domain or "").strip().lower().rstrip(".")
    return {
        "instance_uid": uid,
        "instance_code": code,
        "primary_domain": domain,
        "root": str(Path(root).resolve()),
    }


def instance_path_names(domain: str, code: str) -> dict[str, str]:
    """Build only the MCC-selected names; never invent a code."""
    clean_domain = str(domain or "").strip().lower().rstrip(".")
    clean_code = validate_instance_code(code)
    return {
        "webroot": f"/var/www/{clean_domain}_{clean_code}/public_html",
        "db_name": f"db_{clean_code}",
        "db_user": f"usr_{clean_code}",
    }
