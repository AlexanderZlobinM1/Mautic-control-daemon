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
INSTANCE_STATE_RELATIVE_PATH = Path(".mcd") / "state" / "instance-state.json"
INSTANCE_STATE_SCHEMA = "mcd-instance-state-profile-v1"
INSTANCE_STATE_VERSION = 1
INSTANCE_STATE_PROFILES = frozenset({"active", "artifact", "backup", "quarantine", "cluster", "unknown"})

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


def instance_state_path(root: str | Path) -> Path:
    """Return the only MCD-owned local instance-state evidence path."""
    return Path(root) / INSTANCE_STATE_RELATIVE_PATH


def _normalize_state_domain(value: object) -> str:
    return str(value or "").strip().lower().rstrip(".")


def _validate_state_payload(raw: object, root: str | Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("instance state must be an object")
    if raw.get("schema") != INSTANCE_STATE_SCHEMA:
        raise ValueError("instance state schema is invalid")
    if raw.get("version") != INSTANCE_STATE_VERSION:
        raise ValueError("instance state version is unsupported")
    profile = str(raw.get("profile") or "").strip().lower()
    if profile not in INSTANCE_STATE_PROFILES:
        raise ValueError("instance state profile is unknown")
    root_value = str(raw.get("root") or "").strip()
    root_path = Path(root).resolve(strict=False)
    if root_value and Path(root_value).resolve(strict=False) != root_path:
        raise ValueError("instance state root does not match the selected root")
    uid = str(raw.get("instance_uid") or "").strip()
    if uid:
        uid = validate_mcc_instance_uid(uid)
    code = str(raw.get("instance_code") or "").strip()
    if code:
        code = validate_instance_code(code)
    domain = _normalize_state_domain(raw.get("primary_domain"))
    ownership_confirmed = raw.get("ownership_confirmed")
    if not isinstance(ownership_confirmed, bool):
        raise ValueError("instance state ownership confirmation is invalid")
    cluster_id = str(raw.get("cluster_id") or "").strip()
    if profile == "cluster" and (not cluster_id or not ownership_confirmed):
        raise ValueError("cluster instance state is incomplete")
    return {
        "schema": INSTANCE_STATE_SCHEMA,
        "version": INSTANCE_STATE_VERSION,
        "profile": profile,
        "instance_uid": uid,
        "instance_code": code,
        "primary_domain": domain,
        "root": str(root_path),
        "ownership_confirmed": ownership_confirmed,
        "cluster_id": cluster_id,
    }


def read_instance_state(root: str | Path) -> dict[str, Any] | None:
    """Read versioned local state; invalid present evidence raises fail-closed."""
    path = instance_state_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("instance state evidence is unreadable") from exc
    return _validate_state_payload(raw, root)


def write_instance_state(
    root: str | Path,
    *,
    profile: str,
    instance_uid: str | None = None,
    instance_code: str | None = None,
    primary_domain: str | None = None,
    ownership_confirmed: bool = False,
    cluster_id: str | None = None,
) -> dict[str, Any]:
    """Persist one versioned local state profile for fallback/evidence use."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError(f"instance root is not a directory: {root_path}")
    payload = _validate_state_payload(
        {
            "schema": INSTANCE_STATE_SCHEMA,
            "version": INSTANCE_STATE_VERSION,
            "profile": profile,
            "instance_uid": instance_uid or "",
            "instance_code": instance_code or "",
            "primary_domain": primary_domain or "",
            "root": str(root_path.resolve()),
            "ownership_confirmed": ownership_confirmed,
            "cluster_id": cluster_id or "",
        },
        root_path,
    )
    path = instance_state_path(root_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return payload


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
