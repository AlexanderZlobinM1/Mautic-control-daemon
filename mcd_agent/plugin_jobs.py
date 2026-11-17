from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ENQUEUE_SCHEMA = "mcd-plugin-job-enqueue-result-v1"
STATUS_SCHEMA = "mcd-plugin-job-status-result-v1"
CANCEL_SCHEMA = "mcd-plugin-job-cancel-result-v1"
RECORD_SCHEMA = "mcd-plugin-job-record-v1"
RUNTIME_PREFIX = "plugin_apply_job:"
CAPABILITY = "mcd-plugin-job-v1"

_UID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{2,127}$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class PluginJobError(ValueError):
    pass


def capability() -> dict[str, Any]:
    return {
        "schema": "mcd-plugin-capabilities-v1",
        "version": "1.0",
        "features": [
            CAPABILITY,
            "mcd-plugin-auto-update-policy-v1",
            "mcd-plugin-catalog-digest-v1",
        ],
    }


def canonical_request(
    *,
    instance_uid: str,
    plugin_uids: list[str],
    package_uids: list[str],
    action: str,
    operation_id: str,
) -> dict[str, Any]:
    instance = str(instance_uid or "").strip()
    operation = str(operation_id or "").strip()
    normalized_action = str(action or "").strip().lower()
    plugins = [str(value or "").strip().lower() for value in plugin_uids]
    packages = [str(value or "").strip().lower() for value in package_uids]
    if not instance or "@" in instance or not _INSTANCE_RE.fullmatch(instance):
        raise PluginJobError("plugin_instance_uid_must_be_bare")
    if not _OPERATION_RE.fullmatch(operation):
        raise PluginJobError("plugin_operation_id_invalid")
    if normalized_action not in {"auto", "install", "update", "reinstall", "remove", "purge"}:
        raise PluginJobError("plugin_action_invalid")
    if not plugins or any(not _UID_RE.fullmatch(value) for value in plugins):
        raise PluginJobError("plugin_uid_invalid")
    if any(not _UID_RE.fullmatch(value) for value in packages):
        raise PluginJobError("package_uid_invalid")
    if packages and len(packages) != len(plugins):
        raise PluginJobError("plugin_package_pair_count_mismatch")
    pairs = [
        {
            "plugin_uid": plugin,
            **({"package_uid": packages[index]} if packages else {}),
        }
        for index, plugin in enumerate(plugins)
    ]
    if len({json.dumps(pair, sort_keys=True) for pair in pairs}) != len(pairs):
        raise PluginJobError("plugin_selection_duplicate")
    return {
        "instance_uid": instance,
        "selections": pairs,
        "action": normalized_action,
        "operation_id": operation,
    }


def request_sha256(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def runtime_key(operation_id: str) -> str:
    digest = hashlib.sha256(str(operation_id).encode("utf-8")).hexdigest()
    return RUNTIME_PREFIX + digest


def output_paths(state_db_path: str, operation_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(str(operation_id).encode("utf-8")).hexdigest()
    directory = Path(state_db_path).resolve().parent / "plugin-jobs"
    return str(directory / f"{digest}.stdout"), str(directory / f"{digest}.stderr")


def terminal_from_output(output: str) -> dict[str, Any] | None:
    prefix = "MCD_PLUGIN_OPERATION_RESULT="
    for raw in reversed(str(output or "").splitlines()):
        line = raw.strip()
        if not line.startswith(prefix):
            continue
        try:
            payload = json.loads(line[len(prefix):])
        except (TypeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("schema") == "mcd-plugin-operation-result-v2"
            and payload.get("terminal") is True
        ):
            return payload
    return None


def read_tail(path: str, limit: int = 4000) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data[-max(1, int(limit)):].decode("utf-8", errors="replace")


def with_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    canonical = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    result["receipt_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def status_result(store: object, record: dict[str, Any], *, mcc_enabled: bool) -> dict[str, Any]:
    operation_id = str(record.get("operation_id") or "")
    request_id = int(record.get("request_id") or 0)
    request_status = str(getattr(store, "get_manual_request_status")(request_id) or "unknown").strip().lower()
    stdout = read_tail(str(record.get("stdout_path") or ""), limit=256 * 1024)
    terminal = terminal_from_output(stdout)
    public_status = {
        "pending": "queued",
        "launched": "running",
        "done": "succeeded",
        "failed": "failed",
        "timeout": "failed",
        "lost": "failed",
        "skipped": "failed",
        "cancelled": "cancelled",
    }.get(request_status, request_status or "unknown")
    if isinstance(terminal, dict):
        public_status = "succeeded" if terminal.get("status") == "success" else "failed"
    notification = "pending" if mcc_enabled else "disabled"
    if isinstance(terminal, dict) and terminal.get("inventory_sync_status"):
        notification = str(terminal.get("inventory_sync_status"))
    payload: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "status": public_status,
        "operation_id": operation_id,
        "job_id": operation_id,
        "request_id": request_id,
        "request_sha256": str(record.get("request_sha256") or ""),
        "notification_delivery_status": notification,
    }
    if isinstance(terminal, dict):
        payload["terminal_result"] = terminal
    elif public_status == "failed":
        payload["error_tail"] = read_tail(str(record.get("stderr_path") or ""))
    return with_receipt(payload)


def recent_statuses(store: object, *, mcc_enabled: bool, limit: int = 50) -> list[dict[str, Any]]:
    rows = getattr(store, "list_runtime_sync")(RUNTIME_PREFIX)
    records = [
        dict(payload)
        for _key, payload in rows
        if isinstance(payload, dict) and payload.get("schema") == RECORD_SCHEMA
    ]
    records.sort(key=lambda item: float(item.get("created_at") or 0.0), reverse=True)
    return [
        status_result(store, record, mcc_enabled=mcc_enabled)
        for record in records[: max(1, int(limit))]
    ]
