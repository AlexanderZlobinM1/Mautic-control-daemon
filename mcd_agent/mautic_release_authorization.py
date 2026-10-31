"""Consume MCC-issued private authorization without knowing its signing key."""
from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
import urllib.request
import uuid

SCHEMA = "mcc-mautic-release-authorization-v3"
CONTEXT_SCHEMA = "mcc-mautic-release-authorization-context-v2"
BINDINGS = (
    "job_id", "patch_run_id", "host_id", "instance_uid", "application_root",
    "source_version", "target_version", "source_line", "target_line", "install_type",
    "phase", "operation", "transition_id", "policy_revision", "policy_sha256", "plan_sha256", "transition_requirements",
)
ENVELOPE = ("schema", "issued_at", "expires_at", "nonce", "signature")
MAX_BYTES = 16384


def contract() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("contracts").joinpath("mautic-release-authorization-v3.json").read_text(encoding="utf-8"))


def runtime_capabilities(runtime: str, install_type: str, existing) -> list[str]:
    result = set(existing)
    result.discard(SCHEMA)
    if str(runtime or "host").strip().lower() == "host" and install_type in {"composer", "zip"}:
        result.add(SCHEMA)
    return sorted(result)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("release_context_duplicate_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("release_context_nonfinite")


def read_context(path: str, expected_sha256: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise ValueError("release_context_hash_required")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= MAX_BYTES):
            raise ValueError("release_context_file_unsafe")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_BYTES or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("release_context_file_changed")
        context = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_constant)
        if (not isinstance(context, dict) or set(context) != set(BINDINGS + ENVELOPE)
                or raw != canonical(context) or hashlib.sha256(raw).hexdigest() != expected_sha256):
            raise ValueError("release_context_shape_or_hash_mismatch")
        return context
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("release_context_unavailable") from exc
    finally:
        if fd is not None:
            os.close(fd)


def validate_context(context: dict[str, Any], expected: dict[str, str], now: datetime | None = None) -> None:
    if set(context) != set(BINDINGS + ENVELOPE) or context["schema"] != CONTEXT_SCHEMA:
        raise ValueError("release_context_schema_mismatch")
    if (type(context["policy_revision"]) is not int or context["policy_revision"] < 0
            or any(not isinstance(context[key], str) or not context[key]
                   for key in BINDINGS + ENVELOPE if key not in {"transition_requirements", "policy_revision"})):
        raise ValueError("release_context_types_invalid")
    requirements = context["transition_requirements"]
    flags = {"requires_json_repair", "requires_backup", "system_upgrade_supported", "requires_latest_source"}
    if (not isinstance(requirements, dict)
            or set(requirements) != flags | {"database_compatibility", "minimum_agent_version", "install_types", "phases"}
            or any(type(requirements[key]) is not bool for key in flags)
            or requirements["database_compatibility"] not in ("", "mautic7")
            or not isinstance(requirements["minimum_agent_version"], str)
            or (requirements["minimum_agent_version"] and not re.fullmatch(r"\d+\.\d+\.\d+", requirements["minimum_agent_version"]))
            or any(not isinstance(requirements[key], list) or not requirements[key]
                   or any(not isinstance(item, str) or not item for item in requirements[key])
                   or len(set(requirements[key])) != len(requirements[key]) for key in ("install_types", "phases"))
            or not set(requirements["install_types"]).issubset({"composer", "zip"})
            or requirements["phases"] != ["upgrade"]):
        raise ValueError("release_context_requirements_invalid")
    if any(context.get(key) != value for key, value in expected.items()):
        raise ValueError("release_context_local_binding_mismatch")
    if str(uuid.UUID(context["host_id"])) != context["host_id"]:
        raise ValueError("release_context_host_invalid")
    if any(not re.fullmatch(r"[0-9a-f]{64}", context[key]) for key in ("signature", "policy_sha256", "plan_sha256")):
        raise ValueError("release_context_digest_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{24}", context["nonce"]):
        raise ValueError("release_context_nonce_invalid")
    if not Path(context["application_root"]).is_absolute() or str(Path(context["application_root"])) != context["application_root"] or ".." in Path(context["application_root"]).parts:
        raise ValueError("release_context_root_invalid")
    issued = datetime.fromisoformat(context["issued_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(context["expires_at"].replace("Z", "+00:00"))
    now = now or datetime.now(timezone.utc)
    if (issued.utcoffset() != timezone.utc.utcoffset(issued) or expires.utcoffset() != timezone.utc.utcoffset(expires)
            or not 0 < (expires - issued).total_seconds() <= 1800 or expires <= now
            or (issued - now).total_seconds() > 30):
        raise ValueError("release_context_expired_or_invalid")


def authorize(config: Any, context: dict[str, Any], context_sha256: str, expected: dict[str, str]) -> None:
    validate_context(context, expected)
    if not getattr(config, "mcc_url", "") or not getattr(config, "mcc_token", ""):
        raise RuntimeError("release_transition_live_authorization_required")
    request = mcc_request(
        config.mcc_url.rstrip("/") + "/api/v1/agent/mautic/releases/authorize-transition",
        data=canonical({"authorization_context": context, "authorization_context_sha256": context_sha256}),
        headers={"Authorization": "Bearer " + config.mcc_token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized response")
        result = json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_constant)
        echo = {key: context[key] for key in BINDINGS}
        echo.update(schema=SCHEMA, status="authorized")
        if result != echo:
            raise ValueError("authorization echo mismatch")
    except Exception as exc:
        raise RuntimeError("release_transition_authorization_rejected") from exc
