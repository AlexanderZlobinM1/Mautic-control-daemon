from __future__ import annotations
import hashlib, re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from mcd_agent.commercial_portal import canonical_json

SCHEMA = "mcd-docker-runtime-capability-v1"
_PHP_RE = re.compile(r"PHP\s+(\d+\.\d+(?:\.\d+)?)", re.I)

def observe_docker_runtime(*, image_ref: str, image_digest: str, adapter: str, container: str, runner: Callable[[list[str]], str], now: datetime | None = None) -> dict[str, Any]:
    if not str(image_digest).startswith("sha256:") or not str(image_ref) or not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", str(adapter)):
        raise ValueError("docker_runtime_identity_invalid")
    cli = runner(["docker", "exec", container, "/usr/bin/php", "-v"])
    fpm = runner(["docker", "exec", container, "/usr/sbin/php-fpm", "-v"])
    cli_match, fpm_match = _PHP_RE.search(cli), _PHP_RE.search(fpm)
    if not cli_match or not fpm_match:
        raise ValueError("docker_runtime_php_observation_missing")
    checked = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result: dict[str, Any] = {"schema": SCHEMA, "contract_version": "1.0", "runtime": "docker", "image_ref": str(image_ref), "image_digest": str(image_digest), "adapter": str(adapter), "container": str(container), "php_cli_version": cli_match.group(1), "php_fpm_version": fpm_match.group(1), "capabilities": ["php.cli", "php.fpm"], "checked_at": checked}
    result["observation_sha256"] = "sha256:" + hashlib.sha256(canonical_json(result)).hexdigest()
    return result

def validate_docker_runtime_capability(value: Mapping[str, Any], *, expected_image_digest: str, expected_adapter: str, max_age_seconds: int = 900, now: datetime | None = None) -> dict[str, Any]:
    if value.get("schema") != SCHEMA or value.get("contract_version") != "1.0" or value.get("runtime") != "docker":
        raise ValueError("docker_runtime_capability_schema_invalid")
    if value.get("image_digest") != expected_image_digest or value.get("adapter") != expected_adapter or not value.get("php_cli_version") or not value.get("php_fpm_version"):
        raise ValueError("docker_runtime_capability_binding_invalid")
    try:
        checked = datetime.fromisoformat(str(value["checked_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("docker_runtime_capability_timestamp_invalid") from exc
    current = now or datetime.now(timezone.utc)
    if checked > current or (current - checked).total_seconds() > max(1, int(max_age_seconds)):
        raise ValueError("docker_runtime_capability_stale")
    unsigned = dict(value)
    digest = unsigned.pop("observation_sha256", None)
    if digest != "sha256:" + hashlib.sha256(canonical_json(unsigned)).hexdigest():
        raise ValueError("docker_runtime_capability_digest_invalid")
    return dict(value)
