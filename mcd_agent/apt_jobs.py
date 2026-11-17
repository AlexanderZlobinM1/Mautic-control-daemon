from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ENQUEUE_SCHEMA = "mcd-apt-job-enqueue-result-v1"
STATUS_SCHEMA = "mcd-apt-job-status-result-v1"
CANCEL_SCHEMA = "mcd-apt-job-cancel-result-v1"
RECORD_SCHEMA = "mcd-apt-job-record-v1"
TERMINAL_SCHEMA = "mcd-apt-operation-result-v1"
RUNTIME_PREFIX = "apt_upgrade_job:"
CAPABILITY = "mcd-apt-job-v1"
TERMINAL_PREFIX = "MCD_APT_OPERATION_RESULT="

_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class AptJobError(ValueError):
    pass


def capability() -> dict[str, Any]:
    return {
        "schema": "mcd-apt-capabilities-v1",
        "version": "1.0",
        "features": [CAPABILITY],
    }


def validate_operation_id(operation_id: str) -> str:
    operation = str(operation_id or "").strip()
    if not _OPERATION_RE.fullmatch(operation):
        raise AptJobError("apt_operation_id_invalid")
    return operation


def canonical_request(*, operation_id: str) -> dict[str, str]:
    return {
        "operation": "dist_upgrade",
        "operation_id": validate_operation_id(operation_id),
    }


def request_sha256(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def runtime_key(operation_id: str) -> str:
    digest = hashlib.sha256(validate_operation_id(operation_id).encode("utf-8")).hexdigest()
    return RUNTIME_PREFIX + digest


def host_task_root(state_db_path: str) -> str:
    return str(Path(state_db_path).resolve().parent)


def output_paths(state_db_path: str, operation_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(validate_operation_id(operation_id).encode("utf-8")).hexdigest()
    directory = Path(state_db_path).resolve().parent / "apt-jobs"
    return str(directory / f"{digest}.stdout"), str(directory / f"{digest}.stderr")


def with_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    canonical = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    result["receipt_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def terminal_result(operation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    operation = validate_operation_id(operation_id)
    status = "success" if str(payload.get("status") or "").strip().lower() == "ok" else "failed"
    return with_receipt({
        "schema": TERMINAL_SCHEMA,
        "terminal": True,
        "status": status,
        "operation_id": operation,
        "job_id": operation,
        "result": dict(payload),
    })


def terminal_from_output(output: str) -> dict[str, Any] | None:
    for raw in reversed(str(output or "").splitlines()):
        line = raw.strip()
        if not line.startswith(TERMINAL_PREFIX):
            continue
        try:
            payload = json.loads(line[len(TERMINAL_PREFIX):])
        except (TypeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("schema") == TERMINAL_SCHEMA
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


def status_result(store: object, record: dict[str, Any], *, mcc_enabled: bool) -> dict[str, Any]:
    operation_id = str(record.get("operation_id") or "")
    request_id = int(record.get("request_id") or 0)
    request_status = str(getattr(store, "get_manual_request_status")(request_id) or "unknown").strip().lower()
    terminal = terminal_from_output(
        read_tail(str(record.get("stdout_path") or ""), limit=256 * 1024)
    )
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
    result: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "status": public_status,
        "operation_id": operation_id,
        "job_id": operation_id,
        "request_id": request_id,
        "request_sha256": str(record.get("request_sha256") or ""),
        "notification_delivery_status": "pending" if mcc_enabled else "disabled",
    }
    if isinstance(terminal, dict):
        result["terminal_result"] = terminal
    elif public_status == "failed":
        result["error_tail"] = read_tail(str(record.get("stderr_path") or ""))
    return with_receipt(result)


def recent_statuses(store: object, *, mcc_enabled: bool, limit: int = 50) -> list[dict[str, Any]]:
    records = [
        dict(payload)
        for _key, payload in getattr(store, "list_runtime_sync")(RUNTIME_PREFIX)
        if isinstance(payload, dict) and payload.get("schema") == RECORD_SCHEMA
    ]
    records.sort(key=lambda item: float(item.get("created_at") or 0.0), reverse=True)
    return [
        status_result(store, record, mcc_enabled=mcc_enabled)
        for record in records[: max(1, int(limit))]
    ]
