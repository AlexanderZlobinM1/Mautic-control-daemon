"""Read-only host compatibility evaluation for MCC Add Instance candidates."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

from mcd_agent import __version__

REQUEST_SCHEMA = "mcc-image-compatibility-request-v1"
RESPONSE_SCHEMA = "mcd-image-compatibility-response-v1"
ERROR_SCHEMA = "mcd-image-compatibility-error-v1"
MAX_REQUEST_BYTES = 1_048_576
MAX_CANDIDATES = 64
_DIGEST = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?$")
_MAJOR_MINOR = re.compile(r"^[0-9]+\.[0-9]+$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DATABASE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SUPPORTED_REQUIRES = frozenset(
    {"ipv6_disabled", "nginx", "php_fpm", "composer", "node", "npm", "certbot", "database"}
)
_REQUEST_KEYS = frozenset({"schema", "request_id", "operation", "candidates", "destination"})
_CANDIDATE_KEYS = frozenset({"image_ref", "name", "files_digest", "db_digest", "requirements"})
_DESTINATION_KEYS = frozenset({"domain", "root", "database", "state_key"})
_REQUIREMENT_KEYS = frozenset({"php_min", "db_min", "target_min_free_bytes", "requires"})


class ImageCompatibilityError(ValueError):
    pass


def _exact_keys(value: Any, expected: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ImageCompatibilityError(code)
    return value


def _nonempty_string(value: Any, code: str, *, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > limit:
        raise ImageCompatibilityError(code)
    return value


def _version(value: Any) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(str(value or "").strip())
    return tuple(int(item or 0) for item in match.groups()) if match else None


def validate_request(request: Any) -> dict[str, Any]:
    request = _exact_keys(request, _REQUEST_KEYS, "request_fields_invalid")
    if request["schema"] != REQUEST_SCHEMA:
        raise ImageCompatibilityError("request_schema_invalid")
    if request["operation"] != "add_instance":
        raise ImageCompatibilityError("request_operation_invalid")
    request_id = _nonempty_string(request["request_id"], "request_id_invalid", limit=128)
    if not _IDENTIFIER.fullmatch(request_id):
        raise ImageCompatibilityError("request_id_invalid")

    destination = _exact_keys(request["destination"], _DESTINATION_KEYS, "destination_fields_invalid")
    domain = _nonempty_string(destination["domain"], "destination_domain_invalid", limit=253).lower()
    if destination["domain"] != domain or not _DOMAIN.fullmatch(domain):
        raise ImageCompatibilityError("destination_domain_invalid")
    root = _nonempty_string(destination["root"], "destination_root_invalid", limit=4096)
    pure_root = PurePosixPath(root)
    if not pure_root.is_absolute() or any(part in {"", ".", ".."} for part in pure_root.parts):
        raise ImageCompatibilityError("destination_root_invalid")
    if not _DATABASE.fullmatch(_nonempty_string(destination["database"], "destination_database_invalid", limit=64)):
        raise ImageCompatibilityError("destination_database_invalid")
    if not _IDENTIFIER.fullmatch(_nonempty_string(destination["state_key"], "destination_state_key_invalid", limit=128)):
        raise ImageCompatibilityError("destination_state_key_invalid")

    candidates = request["candidates"]
    if not isinstance(candidates, list) or not candidates or len(candidates) > MAX_CANDIDATES:
        raise ImageCompatibilityError("candidates_invalid")
    seen_refs: set[str] = set()
    for candidate in candidates:
        candidate = _exact_keys(candidate, _CANDIDATE_KEYS, "candidate_fields_invalid")
        image_ref = _nonempty_string(candidate["image_ref"], "candidate_image_ref_invalid")
        _nonempty_string(candidate["name"], "candidate_name_invalid")
        if image_ref in seen_refs:
            raise ImageCompatibilityError("candidate_image_ref_duplicate")
        seen_refs.add(image_ref)
        for field in ("files_digest", "db_digest"):
            if not isinstance(candidate[field], str) or not _DIGEST.fullmatch(candidate[field]):
                raise ImageCompatibilityError(f"candidate_{field}_invalid")
        requirements = candidate["requirements"]
        if not isinstance(requirements, dict) or not set(requirements).issubset(_REQUIREMENT_KEYS):
            raise ImageCompatibilityError("candidate_requirements_invalid")
        if "php_min" in requirements and (
            not isinstance(requirements["php_min"], str)
            or not _MAJOR_MINOR.fullmatch(requirements["php_min"])
        ):
            raise ImageCompatibilityError("candidate_php_min_invalid")
        db_min = requirements.get("db_min", {})
        if not isinstance(db_min, dict) or not set(db_min).issubset({"mysql", "mariadb"}):
            raise ImageCompatibilityError("candidate_db_min_invalid")
        for minimum in db_min.values():
            if (
                not isinstance(minimum, list)
                or len(minimum) != 3
                or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in minimum)
            ):
                raise ImageCompatibilityError("candidate_db_min_invalid")
        if "target_min_free_bytes" in requirements:
            minimum_free = requirements["target_min_free_bytes"]
            if isinstance(minimum_free, bool) or not isinstance(minimum_free, int) or minimum_free < 0:
                raise ImageCompatibilityError("candidate_target_min_free_bytes_invalid")
        required = requirements.get("requires", [])
        if (
            not isinstance(required, list)
            or len(required) != len(set(required))
            or any(not isinstance(token, str) or token not in SUPPORTED_REQUIRES for token in required)
        ):
            raise ImageCompatibilityError("candidate_requires_invalid")
    return request


def _reject_constant(_value: str) -> None:
    raise ImageCompatibilityError("request_json_constant_invalid")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImageCompatibilityError("request_json_duplicate_key")
        result[key] = value
    return result


def load_request_bytes(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ImageCompatibilityError("request_size_invalid")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImageCompatibilityError("request_encoding_invalid") from exc
    try:
        payload = json.loads(decoded, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except ImageCompatibilityError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ImageCompatibilityError("request_json_invalid") from exc
    return validate_request(payload)


def load_request_file(path: str) -> dict[str, Any]:
    target = Path(path)
    try:
        if not target.is_file() or target.is_symlink() or target.stat().st_size > MAX_REQUEST_BYTES:
            raise ImageCompatibilityError("request_file_invalid")
        return load_request_bytes(target.read_bytes())
    except ImageCompatibilityError:
        raise
    except OSError as exc:
        raise ImageCompatibilityError("request_file_unavailable") from exc


def capability() -> dict[str, Any]:
    contract = files("mcd_agent").joinpath("contracts/mcd-image-compatibility-v1.json").read_bytes()
    return {
        "schema": "mcd-image-compatibility-capability-v1",
        "agent_version": __version__,
        "command": "mcd-cli image-compatibility --request-json <file|-> --json",
        "request_schema": REQUEST_SCHEMA,
        "response_schema": RESPONSE_SCHEMA,
        "read_only": True,
        "fresh_probe": True,
        "contract_sha256": hashlib.sha256(contract).hexdigest(),
    }


def _nearest_existing_directory(raw: str) -> Path | None:
    current = Path(raw)
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.is_dir() else None


def _collect_readiness_facts() -> dict[str, Any]:
    """Collect only image-relevant facts so an unrelated probe cannot abort."""
    from mcd_agent.install_readiness import _cmd_version, _ipv6_disabled, _php_versions, _run

    try:
        php = _php_versions()
    except Exception:
        php = {}
    tools: dict[str, dict[str, bool]] = {}
    for name in ("composer", "node", "npm", "certbot"):
        try:
            present, _version_line = _cmd_version(name, ["--version"])
            tools[name] = {"installed": bool(present)}
        except Exception:
            tools[name] = {"installed": None}
    try:
        nginx_present, _nginx_version = _cmd_version("nginx", ["-v"])
        nginx_rc, nginx_state = _run(["systemctl", "is-active", "nginx"], timeout_sec=4)
        nginx = {"installed": bool(nginx_present), "active": nginx_rc == 0 and nginx_state.strip() == "active"}
    except Exception:
        nginx = {"installed": None, "active": None}
    try:
        ipv6_disabled = _ipv6_disabled()
    except Exception:
        ipv6_disabled = None
    return {"ipv6_disabled": ipv6_disabled, "nginx": nginx, "php": php, **tools}


def collect_fresh_facts(destination: dict[str, Any]) -> dict[str, Any]:
    """Probe current host state; no cached compatibility decision is reused."""
    from mcd_agent.install_readiness import _database_version_tuple
    from mcd_agent.mautic_image_install import _mysql_exec

    readiness = _collect_readiness_facts()
    php = readiness.get("php") if isinstance(readiness.get("php"), dict) else {}
    fpm = php.get("fpm") if isinstance(php.get("fpm"), dict) else {}
    active_fpm = sorted(
        (_version(key), key)
        for key, row in fpm.items()
        if _version(key) is not None and isinstance(row, dict) and row.get("active") is True
    )
    php_fpm_version = active_fpm[-1][1] if active_fpm else ""

    database_engine = ""
    database_version = ""
    database_active: bool | None = False
    try:
        database_version = str(_mysql_exec("SELECT VERSION()", timeout_sec=15) or "").strip().splitlines()[-1]
        database_engine = "mariadb" if "mariadb" in database_version.lower() else "mysql"
        database_active = bool(database_version)
    except Exception:
        database_active = None
    version_tuple = _database_version_tuple(database_version, database_engine) if database_version else [0, 0, 0]
    normalized_database_version = ".".join(str(value) for value in version_tuple) if database_active else ""

    target_free_bytes: int | None = None
    target = _nearest_existing_directory(str(destination.get("root") or ""))
    if target is not None:
        try:
            stat = os.statvfs(target)
            target_free_bytes = int(stat.f_bavail) * int(stat.f_frsize)
        except OSError:
            pass

    def installed(name: str) -> bool | None:
        value = readiness.get(name)
        if not isinstance(value, dict) or value.get("installed") is None:
            return None
        return value.get("installed") is True

    nginx = readiness.get("nginx") if isinstance(readiness.get("nginx"), dict) else {}
    return {
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "php_fpm_version": php_fpm_version,
        "database_engine": database_engine,
        "database_version": normalized_database_version,
        "database_active": database_active,
        "target_free_bytes": target_free_bytes,
        "ipv6_disabled": readiness.get("ipv6_disabled"),
        "nginx": (
            None
            if nginx.get("installed") is None or nginx.get("active") is None
            else nginx.get("installed") is True and nginx.get("active") is True
        ),
        "php_fpm": bool(php_fpm_version),
        "composer": installed("composer"),
        "node": installed("node"),
        "npm": installed("npm"),
        "certbot": installed("certbot"),
        "database": database_active,
    }


def _block(code: str, required: Any, observed: Any, source: str, status: str = "unknown") -> dict[str, Any]:
    return {"code": code, "required": required, "observed": observed, "fact_source": source, "status": status}


def evaluate_request(request: dict[str, Any], *, facts: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a strictly validated request against fresh read-only facts."""
    request = validate_request(request)
    compatible_refs: list[str] = []
    results: list[dict[str, Any]] = []
    for candidate in request["candidates"]:
        image_ref = candidate["image_ref"]
        blockers: list[dict[str, Any]] = []
        requirements = candidate["requirements"]
        php_min = _version(requirements.get("php_min")) if "php_min" in requirements else None
        php = _version(facts.get("php_fpm_version"))
        if php_min is not None and (php is None or php < php_min):
            blockers.append(
                _block(
                    "php_fpm_minimum_not_met",
                    requirements["php_min"],
                    facts.get("php_fpm_version"),
                    "php_fpm",
                    "observed" if php else "unknown",
                )
            )
        db_min = requirements.get("db_min", {})
        db_engine = str(facts.get("database_engine") or "").lower()
        db_version = _version(facts.get("database_version"))
        database_required = bool(db_min) or "database" in requirements.get("requires", [])
        if database_required and (
            not db_engine or db_version is None or facts.get("database_active") is not True
        ):
            blockers.append(
                _block(
                    "database_fact_unavailable",
                    db_min,
                    {"engine": db_engine, "version": facts.get("database_version"), "active": facts.get("database_active")},
                    "database",
                )
            )
        elif db_min and db_engine in db_min:
            required_db = tuple(db_min[db_engine])
            if db_version < required_db:
                blockers.append(_block("database_minimum_not_met", list(required_db), list(db_version), "database", "observed"))
        elif db_min:
            blockers.append(_block("database_engine_not_supported", sorted(db_min), db_engine, "database", "observed"))
        free = facts.get("target_free_bytes")
        if "target_min_free_bytes" in requirements and (
            isinstance(free, bool) or not isinstance(free, int) or free < requirements["target_min_free_bytes"]
        ):
            blockers.append(
                _block(
                    "target_free_space_not_met",
                    requirements["target_min_free_bytes"],
                    free,
                    "target",
                    "unknown" if isinstance(free, bool) or not isinstance(free, int) else "observed",
                )
            )
        for token in requirements.get("requires", []):
            if token not in facts:
                blockers.append(_block("required_fact_missing", token, None, token))
            elif facts[token] is not True:
                blockers.append(
                    _block(
                        "required_fact_not_satisfied",
                        token,
                        facts[token],
                        token,
                        "unknown" if facts[token] is None else "observed",
                    )
                )
        status = "compatible" if not blockers else ("unknown" if any(item["status"] == "unknown" for item in blockers) else "blocked")
        is_compatible = status == "compatible"
        if is_compatible:
            compatible_refs.append(image_ref)
        results.append(
            {
                "image_ref": image_ref,
                "files_digest": candidate["files_digest"],
                "db_digest": candidate["db_digest"],
                "status": status,
                "compatible": is_compatible,
                "blockers": blockers,
            }
        )
    all_compatible = bool(results) and len(compatible_refs) == len(results)
    status = "compatible" if all_compatible else (
        "unknown" if any(item["status"] == "unknown" for item in results) else "blocked"
    )
    return {
        "schema": RESPONSE_SCHEMA,
        "request_id": request["request_id"],
        "agent_version": __version__,
        "observed_at": facts.get("observed_at"),
        "status": status,
        "compatible": all_compatible,
        "compatible_image_refs": compatible_refs,
        "candidates": results,
    }


def probe(request: dict[str, Any]) -> dict[str, Any]:
    accepted = validate_request(request)
    return evaluate_request(accepted, facts=collect_fresh_facts(accepted["destination"]))
