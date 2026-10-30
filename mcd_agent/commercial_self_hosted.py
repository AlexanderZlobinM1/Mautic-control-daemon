"""Generic Portal self-hosted MCD v2 execution boundary.

This module deliberately knows only the v2 wire contract and a bounded external
runner process.  It contains no installer, OS, Mautic, scenario, or primitive
recipes.  Those belong to the versioned external artifact and its Portal-owned
runner.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from mcd_agent.commercial_detector import read_normalized_applicability_facts, read_normalized_local_facts
from mcd_agent.scenario_launcher import (
    LEGACY_SCENARIO_CONTEXT_SCHEMA,
    SCENARIO_CONTEXT_SCHEMA,
    SCENARIO_OUTPUT_SCHEMAS,
    build_scenario_context,
    encode_request_frame,
)
from mcd_agent.scenario_artifact_store import ScenarioArtifactStore, ScenarioArtifactStoreError
from mcd_agent.commercial_portal import (
    PORTAL_MCD_ADOPT_CONTRACT_VERSION,
    SELF_HOSTED_MCD_JOB_SCHEMA,
    SELF_HOSTED_MCD_CONTRACT_VERSIONS,
    SELF_HOSTED_MCD_CONTRACT_VERSION,
    SELF_HOSTED_MCD_STAGE_RECEIPT_SCHEMA,
    SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA,
    SELF_HOSTED_MCD_STAGE_IDS,
    SELF_HOSTED_MCD_STAGE_STATUSES,
    SELF_HOSTED_MCD_TERMINAL_STATUSES,
    SELF_HOSTED_MCD_ROLLBACK_STATUSES,
    PORTAL_ARTIFACT_TRUST_UPDATE_SCHEMA,
    CommercialState,
    CommercialPortalError,
    STANDALONE_SETTINGS_SNAPSHOT_SCHEMA,
    build_standalone_settings_snapshot,
    canonical_json,
    download_external_artifact,
    standalone_settings_etag,
    verify_self_hosted_external_artifact,
    verify_self_hosted_mcd_job,
    portal_artifact_trust_keys,
    validate_portal_artifact_trust_update,
)
from mcd_agent.commercial_generic import (
    COMMAND_JOB_SCHEMA,
    STAGE_RECEIPT_SCHEMA as GENERIC_STAGE_RECEIPT_SCHEMA,
    TERMINAL_RECEIPT_SCHEMA as GENERIC_TERMINAL_RECEIPT_SCHEMA,
    validate_capability_inventory,
    validate_generic_command_job,
    validate_generic_stage_receipt,
    validate_generic_terminal_receipt,
)


_RUNS_TABLE = "commercial_self_hosted_runs"
_PENDING_KEY = "commercial_self_hosted_pending_receipts"
_PENDING_DELIVERY_KEY = "commercial_self_hosted_pending_delivery_v1"
_TRANSPORT_KEY = "commercial_portal_transport_v1"
_ADOPTED_KEY = "commercial_portal_adopted_installations_v1"
_MAX_RECEIPT_RESULT_BYTES = 16 * 1024
_MAX_PROCESS_OUTPUT_BYTES = 64 * 1024
_DEFAULT_LONG_POLL_SECONDS = 25
_DEFAULT_BACKOFF_BASE_SECONDS = 1.0
_DEFAULT_BACKOFF_MAX_SECONDS = 300.0
_PORTAL_NOOP_SCHEMA = "portal-mcd-command-noop-v1"
_PORTAL_NOOP_RECEIPT_SCHEMA = "portal-mcd-command-noop-receipt-v1"
_MAX_RUNNER_OUTPUT_BYTES = 256 * 1024
_MAX_RUNNER_FRAME_BYTES = 64 * 1024
_MAX_RUNNER_PROGRESS_FRAMES = 256
_MAX_RUNNER_STDERR_BYTES = 16 * 1024
_MAX_RUNNER_DIAGNOSTIC_PREVIEW_BYTES = 2048
_RUNNER_EVIDENCE_SCHEMA = "mcd-self-hosted-runner-evidence-v1"
_GENERIC_INVENTORY_KEY = "commercial_portal_generic_capability_inventory_v1"
_GENERIC_RUNS_KEY = "commercial_portal_generic_runs_v1"


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class RunnerExecutionError(ValueError):
    """A bounded, secret-free diagnostic for the external runner boundary."""

    def __init__(self, code: str, *, evidence: Mapping[str, Any] | None = None, result: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.code = str(code)
        self.evidence = dict(evidence or {})
        self.result = dict(result or {}) if isinstance(result, Mapping) else None


class RunnerTimeoutError(TimeoutError):
    """A bounded timeout diagnostic that remains compatible with TimeoutError."""

    def __init__(self, code: str, *, evidence: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.code = str(code)
        self.evidence = dict(evidence or {})


def _runner_error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return _error_code(error)


def default_scenario_runner_argv() -> list[str]:
    """Return the immutable runner shipped in the MCD virtualenv."""
    executable = os.path.join(os.path.dirname(sys.executable), "mcd-scenario-runner")
    return [executable]


class _PortalTransportError(RuntimeError):
    """A delivery transport failure, kept separate from local job rejection."""


def _artifact_download_failure(error: BaseException) -> RunnerExecutionError:
    """Keep artifact failures typed without exposing URLs or transport data."""
    cause = error.__cause__ if isinstance(error, _PortalTransportError) and error.__cause__ is not None else error
    evidence: dict[str, Any] = {"stage": "artifact_download"}
    if isinstance(cause, CommercialPortalError):
        code = str(cause.code)[:120]
        if cause.http_status is not None:
            failure_class = "http"
            evidence["http_status"] = cause.http_status
        elif "size_mismatch" in code or "integrity" in code or "digest" in code:
            failure_class = "digest_or_size"
        else:
            failure_class = "transport"
        evidence.update({"failure_class": failure_class, "cause_code": code})
    elif isinstance(cause, ValueError):
        evidence.update({"failure_class": "pre_request_validation", "cause_code": _error_code(cause)})
    else:
        evidence.update({"failure_class": "transport", "cause_code": type(cause).__name__[:80] or "unknown"})
    return RunnerExecutionError("self_hosted_mcd_artifact_unavailable", evidence=evidence)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _secret_free_result(value: object) -> object:
    """Keep runner evidence bounded and reject secret-shaped fields."""
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, child in value.items():
            text = str(key)
            if any(token in text.lower() for token in ("password", "secret", "token", "private", "credential", "key")):
                continue
            out[text[:96]] = _secret_free_result(child)
        return out
    if isinstance(value, list):
        return [_secret_free_result(item) for item in value[:64]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)[:1024] if isinstance(value, str) else value
    return str(value)[:1024]


def _contains_secret_shape(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if any(token in str(key).lower() for token in ("secret", "password", "token", "private", "credential", "access_key")):
                return True
            if _contains_secret_shape(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_secret_shape(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(token in lowered for token in ("password=", "token=", "secret=", "private_key=", "credential="))
    return False


def _redacted_stderr_preview(stderr: bytes) -> str:
    """Keep only a small diagnostic preview; never persist raw child stderr."""
    if not stderr:
        return ""
    text = stderr[:_MAX_RUNNER_DIAGNOSTIC_PREVIEW_BYTES].decode("utf-8", errors="replace")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", text)
    text = re.sub(
        r"(?i)(secret|password|token|private[ _-]?key|credential|access[ _-]?key)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", "<redacted-key>", text, flags=re.DOTALL)
    # A runner must not be able to turn its artifact or context into durable
    # receipt data by echoing arbitrary multiline output.  Keep only bounded,
    # printable diagnostic text and mark truncation explicitly below.
    text = "".join(char if char in "\t\n\r" or 32 <= ord(char) < 127 else "?" for char in text)
    return text[:_MAX_RUNNER_DIAGNOSTIC_PREVIEW_BYTES]


def _best_effort_progress(stdout: bytes) -> list[dict[str, Any]]:
    """Recover only already-valid progress fields from a malformed stream."""
    frames: list[dict[str, Any]] = []
    offset = 0
    while offset + 4 <= len(stdout) and len(frames) < _MAX_RUNNER_PROGRESS_FRAMES:
        frame_size = struct.unpack("!I", stdout[offset : offset + 4])[0]
        offset += 4
        if frame_size <= 0 or frame_size > _MAX_RUNNER_FRAME_BYTES or frame_size > len(stdout) - offset:
            break
        raw = stdout[offset : offset + frame_size]
        offset += frame_size
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            break
        if not isinstance(value, Mapping) or value.get("frame_type") != "progress" or value.get("status") not in {"running", "waiting"}:
            break
        sequence = value.get("sequence")
        message_code = value.get("message_code")
        if not _nonnegative_int(sequence) or sequence != len(frames) + 1 or not isinstance(message_code, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", message_code):
            break
        item: dict[str, Any] = {"sequence": sequence, "status": value["status"], "message_code": message_code}
        percent = value.get("percent")
        if _nonnegative_int(percent) and percent <= 100:
            item["percent"] = percent
        frames.append(item)
    return frames


def _runner_evidence(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    admission_stage: str,
    exit_code: int | None = None,
    signal_number: int | None = None,
    timed_out: bool = False,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    progress: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema": _RUNNER_EVIDENCE_SCHEMA,
        "admission_stage": str(admission_stage)[:64],
        "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        "signal": signal_number if isinstance(signal_number, int) and not isinstance(signal_number, bool) else None,
        "timed_out": bool(timed_out),
        "stdout_bytes": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stdout_truncated": bool(stdout_truncated),
        "stderr_bytes": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stderr_truncated": bool(stderr_truncated),
        "stderr_preview": _redacted_stderr_preview(stderr),
    }
    if progress:
        evidence["progress"] = [dict(item) for item in progress[:_MAX_RUNNER_PROGRESS_FRAMES]]
    return evidence


def _bounded_result(value: object) -> object:
    safe = _secret_free_result(value)
    try:
        encoded = canonical_json(safe)
    except Exception:
        return {"status": "completed"}
    if len(encoded) > _MAX_RECEIPT_RESULT_BYTES:
        return {"status": "completed", "evidence": "bounded"}
    return safe


def _error_code(error: BaseException) -> str:
    # Preserve bounded contract error codes in terminal evidence.  The
    # verifier raises typed ValueError messages for operation-level failures;
    # collapsing those to "ValueError" hides the actionable fail-closed
    # reason from Portal.  Only the MCD-owned namespace is exposed; all other
    # exceptions remain class names and cannot leak arbitrary text.
    message = str(error).strip()
    if isinstance(error, ValueError) and message.startswith("self_hosted_mcd_") and len(message) <= 80 and all(char.isalnum() or char == "_" for char in message):
        return message
    return (type(error).__name__ or "scenario_execution_failed")[:80]


def _delivery_job(command: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap the Portal delivery envelope without validating its contents."""
    nested = command.get("job") if command.get("schema") == "portal-mcd-job-delivery-v1" else None
    return dict(nested) if isinstance(nested, Mapping) else dict(command)


def _verified_shape_for_terminal(command: Mapping[str, Any], *, expected_agent_id: str, expected_installation_id: str, expected_commercial_key_id: str, expected_host_fingerprint: str) -> dict[str, Any] | None:
    """Recover only binding fields needed to close an accepted delivery.

    This is not an admission path and never authorizes execution. It exists so
    an expired, malformed, or locally disabled leased command can still receive
    a bounded typed terminal receipt when Portal supplied enough binding
    material to identify it.
    """
    job = _delivery_job(command)
    required = ("job_id", "request_id", "account_id", "commercial_key_id", "agent_id", "installation_id", "host_fingerprint", "scenario", "applicability")
    if any(not str(job.get(key) or "").strip() for key in required[:7]):
        return None
    if any(str(job.get(key) or "") != expected for key, expected in (("agent_id", expected_agent_id), ("installation_id", expected_installation_id), ("commercial_key_id", expected_commercial_key_id), ("host_fingerprint", expected_host_fingerprint))):
        return None
    scenario = job.get("scenario")
    applicability = job.get("applicability")
    facts_digest = applicability.get("facts_digest") if isinstance(applicability, Mapping) else None
    if not isinstance(scenario, Mapping) or not isinstance(facts_digest, str) or not facts_digest:
        return None
    contract_version = str(job.get("contract_version") or SELF_HOSTED_MCD_CONTRACT_VERSION)
    if contract_version not in SELF_HOSTED_MCD_CONTRACT_VERSIONS:
        contract_version = SELF_HOSTED_MCD_CONTRACT_VERSION
    return {"job": job, "contract_version": contract_version, "scenario": dict(scenario), "facts": dict(applicability.get("facts") or {}) if isinstance(applicability, Mapping) and isinstance(applicability.get("facts"), Mapping) else {}, "facts_digest": facts_digest}


def _failed_terminal_for_delivery(command: Mapping[str, Any], *, error: BaseException, sequence: int, expected_agent_id: str, expected_installation_id: str, expected_commercial_key_id: str, expected_host_fingerprint: str) -> dict[str, Any] | None:
    verified = _verified_shape_for_terminal(command, expected_agent_id=expected_agent_id, expected_installation_id=expected_installation_id, expected_commercial_key_id=expected_commercial_key_id, expected_host_fingerprint=expected_host_fingerprint)
    if verified is None:
        return None
    try:
        return _failed_terminal_receipt(verified, error=error, sequence=sequence, started_at=_utc(), completed=0, total=1)
    except (TypeError, ValueError, KeyError):
        return None


def _is_recovery_command(command: Mapping[str, Any]) -> bool:
    """Identify Portal's same-job recovery delivery without changing its identity."""
    return "_retry_" in str(command.get("command_id") or "")


def _binding_from_verified(verified: Mapping[str, Any]) -> dict[str, Any]:
    job = verified["job"]
    return {
        "job_id": str(job["job_id"]),
        "request_id": str(job["request_id"]),
        "account_id": str(job["account_id"]),
        "commercial_key_id": str(job["commercial_key_id"]),
        "agent_id": str(job["agent_id"]),
        "installation_id": str(job["installation_id"]),
        "host_fingerprint": str(job["host_fingerprint"]),
        "scenario": dict(verified["scenario"]),
        "facts_digest": str(job["applicability"]["facts_digest"]),
    }


def _validate_binding(receipt: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    for key in ("job_id", "request_id", "account_id", "commercial_key_id", "agent_id", "installation_id", "host_fingerprint", "facts_digest"):
        if receipt.get(key) != binding.get(key):
            raise ValueError("self_hosted_mcd_receipt_binding_mismatch")
    if receipt.get("scenario") != binding.get("scenario"):
        raise ValueError("self_hosted_mcd_receipt_scenario_mismatch")


def validate_self_hosted_stage_receipt(receipt: Mapping[str, Any], *, binding: Mapping[str, Any]) -> None:
    if receipt.get("schema") != SELF_HOSTED_MCD_STAGE_RECEIPT_SCHEMA or receipt.get("contract_version") not in SELF_HOSTED_MCD_CONTRACT_VERSIONS:
        raise ValueError("self_hosted_mcd_stage_receipt_invalid")
    if receipt.get("stage_id") not in SELF_HOSTED_MCD_STAGE_IDS or receipt.get("status") not in SELF_HOSTED_MCD_STAGE_STATUSES:
        raise ValueError("self_hosted_mcd_stage_receipt_invalid")
    if isinstance(receipt.get("sequence"), bool) or not isinstance(receipt.get("sequence"), int) or receipt["sequence"] < 1:
        raise ValueError("self_hosted_mcd_stage_receipt_invalid")
    if not isinstance(receipt.get("idempotency_key"), str) or not receipt["idempotency_key"].strip():
        raise ValueError("self_hosted_mcd_stage_receipt_invalid")
    _validate_binding(receipt, binding)
    for key in ("observed_at",):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            raise ValueError("self_hosted_mcd_stage_receipt_invalid")


def validate_self_hosted_terminal_receipt(receipt: Mapping[str, Any], *, binding: Mapping[str, Any]) -> None:
    if receipt.get("schema") != SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA or receipt.get("contract_version") not in SELF_HOSTED_MCD_CONTRACT_VERSIONS:
        raise ValueError("self_hosted_mcd_terminal_receipt_invalid")
    if receipt.get("status") not in SELF_HOSTED_MCD_TERMINAL_STATUSES or receipt.get("rollback") not in SELF_HOSTED_MCD_ROLLBACK_STATUSES:
        raise ValueError("self_hosted_mcd_terminal_receipt_invalid")
    if isinstance(receipt.get("sequence"), bool) or not isinstance(receipt.get("sequence"), int) or receipt["sequence"] < 1:
        raise ValueError("self_hosted_mcd_terminal_receipt_invalid")
    if not isinstance(receipt.get("idempotency_key"), str) or not receipt["idempotency_key"].strip():
        raise ValueError("self_hosted_mcd_terminal_receipt_invalid")
    _validate_binding(receipt, binding)
    for key in ("observed_at", "started_at", "completed_at"):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            raise ValueError("self_hosted_mcd_terminal_receipt_invalid")
    if receipt["status"] == "failed" and not isinstance(receipt.get("error"), Mapping):
        raise ValueError("self_hosted_mcd_terminal_receipt_error_required")


def _failed_terminal_receipt(verified: Mapping[str, Any], *, error: BaseException, sequence: int, started_at: str, completed: int = 0, total: int = 1, rollback: str = "not_attempted", runner_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    binding = _binding_from_verified(verified)
    contract_version = str(verified.get("contract_version") or SELF_HOSTED_MCD_CONTRACT_VERSION)
    if contract_version not in SELF_HOSTED_MCD_CONTRACT_VERSIONS:
        contract_version = SELF_HOSTED_MCD_CONTRACT_VERSION
    runner_result = getattr(error, "result", None)
    if isinstance(runner_result, Mapping):
        completed = runner_result.get("completed", completed)
        total = runner_result.get("total", total)
        rollback = runner_result.get("rollback", rollback)
    result_payload: dict[str, Any] = {
        "executor": "mcd-self-hosted-external",
        "completed": max(0, int(completed)) if isinstance(completed, int) and not isinstance(completed, bool) else 0,
        "total": max(1, int(total)) if isinstance(total, int) and not isinstance(total, bool) else 1,
        "rollback": rollback if rollback in SELF_HOSTED_MCD_ROLLBACK_STATUSES else "not_attempted",
    }
    if isinstance(runner_result, Mapping):
        result_payload["runner"] = _bounded_result(dict(runner_result))
    result = {
        **binding,
        "schema": SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA,
        "contract_version": contract_version,
        "sequence": max(1, int(sequence)),
        "idempotency_key": f"{binding['job_id']}:terminal:{max(1, int(sequence))}",
        "status": "failed",
        "observed_at": _utc(),
        "started_at": started_at,
        "completed_at": _utc(),
        "result": result_payload,
        "error": {"code": "self_hosted_mcd_execution_failed", "type": _runner_error_code(error)},
        "rollback": rollback if rollback in SELF_HOSTED_MCD_ROLLBACK_STATUSES else "not_attempted",
    }
    evidence = runner_evidence
    if evidence is None and isinstance(error, RunnerExecutionError):
        evidence = error.evidence
    if isinstance(evidence, Mapping):
        result["runner_evidence"] = _bounded_result(dict(evidence))
        result["error"] = {**result["error"], "runner_evidence": _bounded_result(dict(evidence))}
    validate_self_hosted_terminal_receipt(result, binding=binding)
    return result


class ExternalProcessPolicy:
    """Run the single bundled MCD scenario launcher with no shell or secrets.

    The launcher receives a framed artifact/context request on stdin.  It is
    the only MCD-owned execution path in this boundary; scenario policy remains
    in the versioned delivered artifact.
    """

    def __init__(self, argv: list[str] | tuple[str, ...] | None, *, timeout_sec: int = 1800):
        self.argv = tuple(str(item) for item in (argv or ()))
        self.timeout_sec = max(1, min(3600, int(timeout_sec)))
        if self.argv:
            if not os.path.isabs(self.argv[0]) or not os.path.isfile(self.argv[0]) or not os.access(self.argv[0], os.X_OK):
                raise ValueError("self_hosted_mcd_runner_not_allowlisted")
            if any(not item or "\x00" in item for item in self.argv):
                raise ValueError("self_hosted_mcd_runner_argv_invalid")

    def run(
        self,
        *,
        artifact: bytes,
        job: Mapping[str, Any],
        facts: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.argv:
            raise ValueError("self_hosted_mcd_runner_unconfigured")
        env = {"LC_ALL": "C", "LANG": "C"}
        scenario = dict(job["scenario"])
        scenario_pin = job.get("scenario_artifact") if isinstance(job.get("scenario_artifact"), Mapping) else {}
        media_type = str(scenario_pin.get("artifact_media_type") or "")
        if media_type not in {"application/x-sh", "application/x-elf"}:
            raise RunnerExecutionError(
                "self_hosted_mcd_runner_artifact_media_type_invalid",
                evidence=_runner_evidence(admission_stage="artifact_metadata"),
            )
        scenario["artifact_revision"] = str(scenario.get("artifact_revision") or scenario_pin.get("artifact_revision") or scenario.get("revision") or "")
        scenario["artifact_digest"] = str(scenario.get("artifact_digest") or scenario_pin.get("artifact_digest") or "")
        if context is None:
            context_job = dict(job)
            context_job["scenario"] = scenario
            payload: Mapping[str, Any] = build_scenario_context(context_job, context_schema=LEGACY_SCENARIO_CONTEXT_SCHEMA)
        else:
            payload = dict(context)
        try:
            framed = encode_request_frame(artifact, payload)
        except ValueError as exc:
            raise RunnerExecutionError(
                _runner_error_code(exc),
                evidence=_runner_evidence(admission_stage="request_frame"),
            ) from exc
        try:
            completed = self._run_bounded([*self.argv, "--mcd-scenario-apply"], framed, env)
        except (RunnerExecutionError, RunnerTimeoutError):
            raise
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        signal_number = -completed.returncode if completed.returncode is not None and completed.returncode < 0 else None
        evidence = _runner_evidence(
            stdout=stdout,
            stderr=stderr,
            admission_stage="completed",
            exit_code=completed.returncode,
            signal_number=signal_number,
            progress=_best_effort_progress(stdout),
        )
        if completed.returncode < 0:
            raise RunnerExecutionError(
                f"self_hosted_mcd_runner_signal_{-completed.returncode}",
                evidence={**evidence, "admission_stage": "signal"},
            )
        try:
            frames = self._decode_output_frames(stdout)
        except ValueError as exc:
            if completed.returncode != 0:
                code = self._child_error_code(stdout)
                raise RunnerExecutionError(code or "self_hosted_mcd_runner_failed", evidence=evidence) from exc
            raise RunnerExecutionError(
                _runner_error_code(exc),
                evidence={
                    **evidence,
                    "admission_stage": "output_decode",
                    "progress": _best_effort_progress(stdout),
                },
            ) from exc
        if not frames:
            raise RunnerExecutionError("self_hosted_mcd_runner_output_missing", evidence=evidence)
        parsed = frames[-1]
        if completed.returncode != 0:
            result = _bounded_result(parsed)
            raise RunnerExecutionError(
                str(parsed.get("error_code") or "self_hosted_mcd_runner_failed"),
                evidence={**evidence, "admission_stage": "result_decode", "runner_result": result},
                result=result,
            )
        if str(parsed.get("status") or "") not in {"succeeded", "completed", "ok"}:
            raise RunnerExecutionError(
                "self_hosted_mcd_runner_failed",
                evidence={**evidence, "admission_stage": "result_decode", "progress": _best_effort_progress(stdout)},
            )
        evidence["progress"] = _best_effort_progress(stdout)
        return {
            "status": "succeeded",
            "runner": _bounded_result(parsed),
            "binding": hashlib.sha256(canonical_json(payload)).hexdigest(),
            "runner_evidence": evidence,
        }

    @staticmethod
    def _child_error_code(stdout: bytes) -> str | None:
        try:
            frames = ExternalProcessPolicy._decode_output_frames(stdout)
        except ValueError:
            return None
        code = frames[-1].get("error_code") if frames else None
        if isinstance(code, str) and code.startswith("self_hosted_mcd_runner_") and len(code) <= 96 and all(char.isalnum() or char == "_" for char in code):
            return code
        return None

    @staticmethod
    def _decode_output_frames(stdout: bytes) -> list[dict[str, Any]]:
        if len(stdout) > _MAX_RUNNER_OUTPUT_BYTES:
            raise ValueError("self_hosted_mcd_runner_output_too_large")
        frames: list[dict[str, Any]] = []
        offset = 0
        terminal_seen = False
        while offset < len(stdout):
            if len(stdout) - offset < 4:
                raise ValueError("self_hosted_mcd_runner_output_invalid")
            frame_size = struct.unpack("!I", stdout[offset : offset + 4])[0]
            offset += 4
            if frame_size <= 0 or frame_size > _MAX_RUNNER_FRAME_BYTES or frame_size > len(stdout) - offset:
                raise ValueError("self_hosted_mcd_runner_output_frame_invalid")
            try:
                raw_frame = stdout[offset : offset + frame_size]
                frame = json.loads(raw_frame.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("self_hosted_mcd_runner_output_invalid") from exc
            if not isinstance(frame, Mapping):
                raise ValueError("self_hosted_mcd_runner_output_invalid")
            if canonical_json(frame) != raw_frame:
                raise ValueError("self_hosted_mcd_runner_output_not_canonical")
            schema = str(frame.get("schema") or "")
            if schema not in SCENARIO_OUTPUT_SCHEMAS or frame.get("contract_version") != "1.0":
                raise ValueError("self_hosted_mcd_runner_output_schema_invalid")
            if _contains_secret_shape(frame):
                raise ValueError("self_hosted_mcd_runner_output_secret_detected")
            status = str(frame.get("status") or "")
            if status in {"running", "waiting"}:
                allowed = {"schema", "contract_version", "frame_type", "sequence", "status", "message_code", "percent"}
                if frame.get("frame_type") != "progress" or set(frame) - allowed or not _nonnegative_int(frame.get("sequence")) or frame.get("sequence") != len(frames) + 1 or not isinstance(frame.get("message_code"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", frame["message_code"]) or ("percent" in frame and (not _nonnegative_int(frame["percent"]) or not 0 <= frame["percent"] <= 100)):
                    raise ValueError("self_hosted_mcd_runner_output_progress_invalid")
                if terminal_seen:
                    raise ValueError("self_hosted_mcd_runner_output_terminal_order_invalid")
            elif status in {"succeeded", "failed"}:
                allowed = {"schema", "contract_version", "frame_type", "status", "exit_code", "completed", "total", "rollback", "error_code"}
                if terminal_seen or frame.get("frame_type") != "result" or set(frame) - allowed or not all(_nonnegative_int(frame.get(key)) for key in ("exit_code", "completed", "total")) or frame.get("total") < 1 or frame.get("completed") > frame.get("total") or frame.get("rollback") not in {"not_attempted", "not_started", "succeeded", "failed"}:
                    raise ValueError("self_hosted_mcd_runner_output_terminal_invalid")
                if status == "succeeded" and frame.get("exit_code") != 0 or status == "failed" and frame.get("exit_code") == 0:
                    raise ValueError("self_hosted_mcd_runner_output_exit_status_invalid")
                if status == "failed" and (not isinstance(frame.get("error_code"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", frame["error_code"])):
                    raise ValueError("self_hosted_mcd_runner_output_result_invalid")
                if "error_code" in frame and (not isinstance(frame["error_code"], str) or len(frame["error_code"]) > 128):
                    raise ValueError("self_hosted_mcd_runner_output_result_invalid")
                terminal_seen = True
            else:
                raise ValueError("self_hosted_mcd_runner_output_status_invalid")
            frames.append(dict(frame))
            if sum(1 for item in frames if item.get("frame_type") == "progress") > _MAX_RUNNER_PROGRESS_FRAMES:
                raise ValueError("self_hosted_mcd_runner_progress_too_large")
            offset += frame_size
        if not terminal_seen:
            raise ValueError("self_hosted_mcd_runner_output_terminal_missing")
        return frames

    def _run_bounded(self, argv: list[str], framed: bytes, env: Mapping[str, str]) -> subprocess.CompletedProcess[bytes]:
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=dict(env),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ValueError("self_hosted_mcd_runner_not_allowlisted") from exc
        except PermissionError as exc:
            raise ValueError("self_hosted_mcd_runner_permissions") from exc
        selector = selectors.DefaultSelector()
        stdout = bytearray()
        stderr = bytearray()
        input_offset = 0
        stdin_registered = False
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                selector.register(stream, selectors.EVENT_READ, label)
            if framed:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                stdin_registered = True
            else:
                process.stdin.close()
            deadline = time.monotonic() + self.timeout_sec
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process_group(process)
                    evidence = _runner_evidence(
                        stdout=bytes(stdout), stderr=bytes(stderr), admission_stage="timeout",
                        exit_code=process.returncode, timed_out=True,
                    )
                    raise RunnerTimeoutError("self_hosted_mcd_runner_timeout", evidence=evidence)
                for key, mask in selector.select(min(remaining, 0.25)):
                    label = key.data
                    fileobj = key.fileobj
                    if label == "stdin" and mask & selectors.EVENT_WRITE:
                        try:
                            written = os.write(fileobj.fileno(), framed[input_offset : input_offset + 64 * 1024])
                        except BrokenPipeError as exc:
                            self._terminate_process_group(process)
                            evidence = _runner_evidence(
                                stdout=bytes(stdout), stderr=bytes(stderr), admission_stage="stdin",
                                exit_code=process.returncode,
                            )
                            raise RunnerExecutionError("self_hosted_mcd_runner_broken_pipe", evidence=evidence) from exc
                        input_offset += written
                        if input_offset >= len(framed):
                            selector.unregister(fileobj)
                            stdin_registered = False
                            fileobj.close()
                        continue
                    if label not in {"stdout", "stderr"} or not mask & selectors.EVENT_READ:
                        continue
                    chunk = os.read(fileobj.fileno(), (_MAX_RUNNER_OUTPUT_BYTES if label == "stdout" else _MAX_RUNNER_STDERR_BYTES) + 1)
                    if not chunk:
                        selector.unregister(fileobj)
                        continue
                    target = stdout if label == "stdout" else stderr
                    limit = _MAX_RUNNER_OUTPUT_BYTES if label == "stdout" else _MAX_RUNNER_STDERR_BYTES
                    if len(target) + len(chunk) > limit:
                        target.extend(chunk[: max(0, limit - len(target))])
                        self._terminate_process_group(process)
                        evidence = _runner_evidence(
                            stdout=bytes(stdout), stderr=bytes(stderr),
                            admission_stage=f"{label}_capture",
                            exit_code=process.returncode,
                            stdout_truncated=label == "stdout",
                            stderr_truncated=label == "stderr",
                        )
                        code = "self_hosted_mcd_runner_output_too_large" if label == "stdout" else "self_hosted_mcd_runner_stderr_too_large"
                        raise RunnerExecutionError(code, evidence=evidence)
                    target.extend(chunk)
            process.wait(timeout=5)
            completed = subprocess.CompletedProcess(argv, process.returncode, bytes(stdout), bytes(stderr))
            completed.stdout_truncated = False  # type: ignore[attr-defined]
            completed.stderr_truncated = False  # type: ignore[attr-defined]
            return completed
        except BrokenPipeError as exc:
            self._terminate_process_group(process)
            evidence = _runner_evidence(
                stdout=bytes(stdout), stderr=bytes(stderr), admission_stage="stdin",
                exit_code=process.returncode,
            )
            raise RunnerExecutionError("self_hosted_mcd_runner_broken_pipe", evidence=evidence) from exc
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_group(process)
            evidence = _runner_evidence(
                stdout=bytes(stdout), stderr=bytes(stderr), admission_stage="wait",
                exit_code=process.returncode, timed_out=True,
            )
            raise RunnerTimeoutError("self_hosted_mcd_runner_timeout", evidence=evidence) from exc
        finally:
            try:
                selector.close()
            except Exception:
                pass
            try:
                if stdin_registered and process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
            except OSError:
                pass
            for stream in (process.stdout, process.stderr):
                try:
                    if stream is not None and not stream.closed:
                        stream.close()
                except OSError:
                    pass

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


class ScenarioExecutor:
    """Generic local scenario executor shared by every delivery transport.

    The surrounding worker may be Portal-backed today, but this class owns no
    connection, account, commercial-key or provider identity.  Those values
    are admission/delivery bindings and never enter the local artifact frame.
    """
    def __init__(self, state: CommercialState, *, runner: Callable[..., Any] | None = None, facts_reader: Callable[..., Mapping[str, Any]] | None = None, artifact_store: ScenarioArtifactStore | None = None):
        self.state = state
        self.runner = runner
        self.facts_reader = facts_reader or read_normalized_local_facts
        self.artifact_store = artifact_store or ScenarioArtifactStore(Path(self.state.path).resolve().parent / "scenario-artifacts")
        with sqlite3.connect(self.state.path) as db:
            db.execute(f"CREATE TABLE IF NOT EXISTS {_RUNS_TABLE} (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def _load(self, job_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.state.path) as db:
            row = db.execute(f"SELECT payload FROM {_RUNS_TABLE} WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def durable_terminal(self, *, job_id: str, execution_key: str) -> dict[str, Any] | None:
        """Return one exact terminal from the worker's durable execution history."""
        prior = self._load(job_id)
        executions = prior.get("executions") if isinstance(prior, Mapping) else None
        saved = executions.get(execution_key) if isinstance(executions, Mapping) else None
        terminal = saved.get("terminal") if isinstance(saved, Mapping) else None
        return dict(terminal) if isinstance(terminal, Mapping) else None

    def _save(self, job_id: str, payload: Mapping[str, Any]) -> None:
        with sqlite3.connect(self.state.path) as db:
            db.execute(f"INSERT INTO {_RUNS_TABLE}(job_id,payload) VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload", (job_id, json.dumps(payload, sort_keys=True)))

    def _local_facts(self, expected: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        target_kind = str(expected.get("target_kind") or context.get("target_kind") or "existing_server")
        capabilities = list(context.get("local_facts_capabilities") or ["package_manager.query"])
        observed = read_normalized_applicability_facts(
            target_kind=target_kind,
            capabilities=capabilities,
            facts_reader=self.facts_reader,
        )
        observed.pop("detector_schema", None)
        observed["capabilities"] = sorted(set(str(item) for item in observed.get("capabilities", [])))
        normalized_expected = dict(expected)
        normalized_expected["capabilities"] = sorted(set(str(item) for item in expected.get("capabilities", [])))
        # Legacy 2.0/early 2.1 jobs may omit the additive host-classification
        # coordinate; when present, it is part of the exact revalidation.
        for key in ("classification_reason", "classification_evidence"):
            if key not in normalized_expected:
                observed.pop(key, None)
        if observed != normalized_expected:
            raise ValueError("self_hosted_mcd_applicability_facts_mismatch")
        return observed

    def run(self, job: Mapping[str, Any], *, portal_keys: Mapping[str, str], publisher_keys: Mapping[str, Any], context: Mapping[str, Any], expected_agent_id: str, expected_installation_id: str, expected_commercial_key_id: str, expected_host_fingerprint: str, bootstrap_artifact: bytes | None, scenario_artifact: bytes, receipt_sink: Callable[[dict[str, Any]], Any] | None = None, execution_key: str | None = None) -> dict[str, Any]:
        verified = verify_self_hosted_mcd_job(job, {}, expected_agent_id=expected_agent_id, expected_installation_id=expected_installation_id, expected_commercial_key_id=expected_commercial_key_id, expected_host_fingerprint=expected_host_fingerprint)
        binding = _binding_from_verified(verified)
        job_id = binding["job_id"]
        job_digest = hashlib.sha256(canonical_json(dict(job))).hexdigest()
        # The worker normally supplies command_id:delivery_cursor.  Derive it
        # again from the verified envelope as a safety net: a valid Portal
        # delivery must never fall back to the legacy job-only identity merely
        # because a cursor arrived as a JSON string or an older caller omitted
        # the optional helper argument.  A distinct recovery command must be
        # allowed to execute once even when its job_id is unchanged.
        if not execution_key:
            command_id = str(job.get("command_id") or "").strip()
            raw_cursor = job.get("delivery_cursor")
            try:
                delivery_cursor = int(raw_cursor) if raw_cursor is not None and not isinstance(raw_cursor, bool) else 0
            except (TypeError, ValueError):
                delivery_cursor = 0
            if command_id and delivery_cursor > 0:
                execution_key = f"{command_id}:{delivery_cursor}"
            else:
                execution_key = f"job:{job_digest}"
        execution_key = str(execution_key).strip()
        if not execution_key or len(execution_key) > 512:
            raise ValueError("self_hosted_mcd_execution_key_invalid")
        prior = self._load(job_id)
        if isinstance(prior, dict):
            executions = prior.get("executions")
            if isinstance(executions, Mapping):
                saved = executions.get(execution_key)
                if isinstance(saved, Mapping) and saved.get("job_digest") == job_digest and isinstance(saved.get("terminal"), Mapping):
                    # The signed job envelope is still revalidated above, but
                    # a same-command replay must not depend on today's
                    # artifact trust set or execute the runner twice.
                    return dict(saved["terminal"])
            elif (
                "_retry_" not in execution_key
                and prior.get("job_digest") == job_digest
                and isinstance(prior.get("terminal"), Mapping)
            ):
                # Migrate a pre-execution-key record only for the original
                # command. A distinct recovery command must execute once.
                return dict(prior["terminal"])
        started_at = _utc()
        prior_sequence = 0
        if isinstance(prior, Mapping):
            try:
                prior_sequence = max(0, int(prior.get("sequence") or 0))
            except (TypeError, ValueError):
                prior_sequence = 0
        is_recovery = "_retry_" in str(job.get("command_id") or "") or "_retry_" in execution_key
        # Stage and terminal receipts for a distinct recovery continue the
        # job's monotonic receipt sequence.  Starting again at 1 makes the
        # Portal see the recovery terminal as the old terminal and return a
        # receipt conflict even though the command identity is new.
        sequence = prior_sequence if is_recovery else 0
        runner_evidence: Mapping[str, Any] | None = None

        def emit(stage_id: str, status: str, result: object = None, error: Mapping[str, Any] | None = None) -> bool:
            nonlocal sequence
            sequence += 1
            receipt = {**binding, "schema": SELF_HOSTED_MCD_STAGE_RECEIPT_SCHEMA, "contract_version": str(verified.get("contract_version") or SELF_HOSTED_MCD_CONTRACT_VERSION), "stage_id": stage_id, "sequence": sequence, "status": status, "idempotency_key": f"{binding['job_id']}:{stage_id}:{sequence}", "observed_at": _utc()}
            if result is not None:
                receipt["result"] = _bounded_result(result)
            if error is not None:
                error_payload: dict[str, Any] = {
                    "code": str(error.get("code") or "self_hosted_mcd_stage_failed")[:120]
                }
                details = error.get("details")
                if isinstance(details, Mapping):
                    error_payload["details"] = _bounded_result(details)
                receipt["error"] = error_payload
            validate_self_hosted_stage_receipt(receipt, binding=binding)
            if receipt_sink:
                return receipt_sink(receipt) is not False
            return True

        try:
            # Admission failures after the signed envelope is verified must
            # enter the same durable terminal path as runner failures.  In
            # particular, a recovery command must not be cursor-acknowledged
            # from the worker's outer exception handler while losing its
            # command execution identity and terminal history.
            if verified["bootstrap_artifact"] is not None:
                if bootstrap_artifact is None:
                    raise ValueError("self_hosted_mcd_bootstrap_artifact_unavailable")
                verify_self_hosted_external_artifact(verified["bootstrap_artifact"], bootstrap_artifact, publisher_keys, kind="bootstrap")
            verify_self_hosted_external_artifact(verified["scenario_artifact"], scenario_artifact, publisher_keys, kind="scenario")
            facts = self._local_facts(verified["facts"], context)
            # Validate the exact frame/context compatibility before activation;
            # this only serializes and validates the request and never starts
            # the runner.
            try:
                context_job = dict(verified["job"])
                context_scenario = dict(context_job.get("scenario") or {})
                context_scenario.update(
                    {
                        "artifact_revision": verified["scenario_artifact"]["artifact_revision"],
                        "artifact_digest": verified["scenario_artifact"]["artifact_digest"],
                    }
                )
                context_job["scenario"] = context_scenario
                encode_request_frame(
                    scenario_artifact,
                    build_scenario_context(context_job, context_schema=LEGACY_SCENARIO_CONTEXT_SCHEMA),
                )
            except (ValueError, TypeError) as exc:
                raise ScenarioArtifactStoreError("scenario_store_context_incompatible") from exc
            delivery = self.artifact_store.deliver(
                pin=verified["scenario_artifact"],
                artifact=scenario_artifact,
                verify=lambda pin, payload: verify_self_hosted_external_artifact(
                    pin, payload, publisher_keys, kind="scenario"
                ),
                binding={
                    "job_id": binding["job_id"],
                    "request_id": binding["request_id"],
                    "idempotency_key": binding["job_id"] + ":artifact:" + str(verified["scenario_artifact"]["artifact_revision"]),
                    "scenario": dict(binding["scenario"]),
                },
            )
            delivery_receipt_emitted = emit(
                "scenario_dispatch",
                "succeeded",
                {"status": "accepted", "stage_selection": verified.get("stage_selection"), "artifact_delivery": delivery["receipt"]},
            )
            active_artifact = Path(delivery["artifact_path"]).read_bytes()
            if delivery_receipt_emitted:
                self.artifact_store.acknowledge(delivery["receipt"])
            if self.runner is None:
                raise ValueError("self_hosted_mcd_runner_unconfigured")
            result = self.runner(artifact=active_artifact, job=verified["job"], facts=facts)
            execution_result = dict(result) if isinstance(result, Mapping) else {"value": result}
            execution_result["stage_selection"] = verified.get("stage_selection")
            emit("execution", "succeeded", execution_result)
            terminal = {**binding, "schema": SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA, "contract_version": str(verified.get("contract_version") or SELF_HOSTED_MCD_CONTRACT_VERSION), "sequence": sequence + 1, "idempotency_key": f"{binding['job_id']}:terminal:{sequence + 1}", "status": "succeeded", "observed_at": _utc(), "started_at": started_at, "completed_at": _utc(), "result": _bounded_result(execution_result), "rollback": "not_attempted"}
            validate_self_hosted_terminal_receipt(terminal, binding=binding)
        except Exception as exc:
            evidence = getattr(exc, "evidence", None)
            details = evidence if isinstance(evidence, Mapping) else None
            runner_evidence = details
            emit(
                "execution",
                "failed",
                result={"runner_evidence": details} if details is not None else None,
                error={"code": "self_hosted_mcd_execution_failed", "details": details} if details is not None else {"code": "self_hosted_mcd_execution_failed"},
            )
            terminal = _failed_terminal_receipt(
                verified,
                error=exc,
                sequence=sequence + 1,
                started_at=started_at,
                completed=0,
                total=1,
                runner_evidence=details,
            )
        executions: dict[str, Any] = {}
        if isinstance(prior, Mapping) and isinstance(prior.get("executions"), Mapping):
            executions.update({str(key): dict(value) for key, value in prior["executions"].items() if isinstance(value, Mapping)})
        executions[execution_key] = {"job_digest": job_digest, "terminal": terminal}
        # Keep a bounded history so a sequence of guarded Portal recovery
        # commands remains restart-safe without growing the local state.
        if len(executions) > 8:
            for key in list(executions)[:-8]:
                executions.pop(key, None)
        saved = {
            "job_digest": job_digest,
            "execution_key": execution_key,
            "executions": executions,
            "terminal": terminal,
            "status": terminal["status"],
            "sequence": terminal["sequence"],
        }
        if isinstance(runner_evidence, Mapping):
            saved["runner_evidence"] = _bounded_result(runner_evidence)
        self._save(job_id, saved)
        if receipt_sink:
            receipt_sink(terminal)
        return terminal


SelfHostedScenarioExecutor = ScenarioExecutor


class SelfHostedPortalWorker:
    """Durable outbound-only v2 Portal poll/verify/execute/receipt loop.

    This worker never binds or listens on a host socket.  Every Portal
    interaction is initiated by the worker through the supplied client.  The
    cursor, pending delivery and transport backoff are persisted before the
    worker returns, so an outage or daemon restart cannot acknowledge a
    command before its receipt has been delivered.
    """

    def __init__(self, client: Any, executor: ScenarioExecutor, *, installation_id: str | list[str], portal_keys: Mapping[str, str], publisher_keys: Mapping[str, Any], context: Mapping[str, Any], commercial_key_id: str, agent_id: str, host_fingerprint: str, poll_wait_seconds: int = _DEFAULT_LONG_POLL_SECONDS, backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS, backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS, clock: Callable[[], float] | None = None, inventory_provider: Callable[[str], Mapping[str, Any]] | None = None, runtime_command_handler: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None, capability_inventory_provider: Callable[[str], Mapping[str, Any]] | None = None, generic_command_handler: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None):
        self.client = client
        self.executor = executor
        self.installation_ids = [installation_id] if isinstance(installation_id, str) else [str(value) for value in installation_id]
        self.installation_ids = [value for value in self.installation_ids if value]
        if not self.installation_ids or not commercial_key_id or not agent_id or not host_fingerprint:
            raise ValueError("self_hosted_mcd_identity_missing")
        self.portal_keys = dict(portal_keys)
        self.publisher_keys = dict(publisher_keys)
        self.context = dict(context)
        self.commercial_key_id = commercial_key_id
        self.agent_id = agent_id
        self.host_fingerprint = host_fingerprint
        self.poll_wait_seconds = max(0, min(30, int(poll_wait_seconds)))
        self.backoff_base_seconds = max(0.1, float(backoff_base_seconds))
        self.backoff_max_seconds = max(self.backoff_base_seconds, float(backoff_max_seconds))
        self._clock = clock or time.time
        self.inventory_provider = inventory_provider
        self.runtime_command_handler = runtime_command_handler
        self.capability_inventory_provider = capability_inventory_provider
        self.generic_command_handler = generic_command_handler
        self._active_installation_id = self.installation_ids[0]
        # A daemon restart is also the supported recovery point for a stale
        # Portal projection. Publish one complete inventory on the first
        # outbound cycle even when the durable content hash is unchanged;
        # subsequent cycles remain hash/idempotency gated.
        self._publish_inventory_on_startup = True

    def _pending(self) -> list[dict[str, Any]]:
        raw = self.executor.state.get(_PENDING_KEY)
        return [dict(item) for item in raw] if isinstance(raw, list) and all(isinstance(item, Mapping) for item in raw) else []

    def _queue(self, receipt: Mapping[str, Any]) -> None:
        pending = self._pending()
        identity = str(receipt.get("idempotency_key") or "")
        if not any(str(item.get("idempotency_key") or "") == identity for item in pending):
            pending.append(dict(receipt))
        self.executor.state.put(_PENDING_KEY, pending)

    def requeue_durable_terminal_receipt(
        self,
        *,
        job_id: str,
        command_id: str,
        delivery_cursor: int,
        sequence: int,
        idempotency_key: str,
        payload_sha256: str,
    ) -> dict[str, Any]:
        """Requeue one exact terminal already persisted by this worker."""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("self_hosted_mcd_recovery_job_invalid")
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("self_hosted_mcd_recovery_command_invalid")
        if not isinstance(delivery_cursor, int) or isinstance(delivery_cursor, bool) or delivery_cursor < 1:
            raise ValueError("self_hosted_mcd_recovery_cursor_invalid")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ValueError("self_hosted_mcd_recovery_sequence_invalid")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("self_hosted_mcd_recovery_idempotency_invalid")
        if not isinstance(payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
            raise ValueError("self_hosted_mcd_recovery_payload_digest_invalid")
        execution_key = f"{command_id}:{delivery_cursor}"
        loader = getattr(self.executor, "durable_terminal", None)
        terminal = loader(job_id=job_id, execution_key=execution_key) if callable(loader) else None
        if not isinstance(terminal, Mapping):
            raise ValueError("self_hosted_mcd_recovery_terminal_missing")
        terminal = dict(terminal)
        binding = {key: terminal.get(key) for key in ("job_id", "request_id", "account_id", "commercial_key_id", "agent_id", "installation_id", "host_fingerprint", "facts_digest", "scenario")}
        try:
            validate_self_hosted_terminal_receipt(terminal, binding=binding)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("self_hosted_mcd_recovery_terminal_invalid") from exc
        if (
            terminal.get("job_id") != job_id
            or terminal.get("sequence") != sequence
            or terminal.get("idempotency_key") != idempotency_key
            or terminal.get("agent_id") != self.agent_id
            or terminal.get("installation_id") != self._active_installation_id
            or terminal.get("commercial_key_id") != self.commercial_key_id
            or terminal.get("host_fingerprint") != self.host_fingerprint
        ):
            raise ValueError("self_hosted_mcd_recovery_binding_mismatch")
        digest = hashlib.sha256(canonical_json(terminal)).hexdigest()
        if digest != payload_sha256:
            raise ValueError("self_hosted_mcd_recovery_payload_mismatch")
        pending = self._pending()
        if pending:
            if any(str(item.get("idempotency_key") or "") != idempotency_key for item in pending):
                raise ValueError("self_hosted_mcd_recovery_queue_busy")
            return {"status": "already_queued", "job_id": job_id, "sequence": sequence, "idempotency_key": idempotency_key, "payload_sha256": digest}
        self._queue(terminal)
        return {"status": "queued", "job_id": job_id, "sequence": sequence, "idempotency_key": idempotency_key, "payload_sha256": digest}

    def _send_or_queue(self, receipt: dict[str, Any]) -> bool:
        if self._pending():
            self._queue(receipt)
            return False
        try:
            self._send_receipt(receipt)
            self._transport_succeeded()
            return True
        except Exception:
            self._transport_failed("receipt_delivery")
            self._queue(receipt)
            return False

    def _normalize_pending_recovery_receipt(self) -> None:
        """Rebase an unsent retry terminal to the next job sequence.

        Portal recovery is a continuation of the same job.  If the original
        terminal was already recorded, a retry terminal must use the next
        sequence/idempotency key; otherwise Portal correctly rejects it as a
        receipt conflict.  Re-signing is intentionally deferred to
        ``PortalClient.send_receipt`` after this durable rewrite.
        """
        pending_delivery = self._pending_delivery()
        pending = self._pending()
        if not pending or not pending_delivery:
            return
        commands = self._commands_from_delivery(
            pending_delivery.get("job") if isinstance(pending_delivery.get("job"), Mapping) else {}
        )
        if not commands or not _is_recovery_command(commands[0]):
            return
        receipt = pending[0]
        if receipt.get("schema") != SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA:
            return
        try:
            sequence = int(receipt.get("sequence") or 0)
        except (TypeError, ValueError):
            return
        if sequence < 1:
            return
        rebased = dict(receipt)
        next_sequence = sequence + 1
        rebased["sequence"] = next_sequence
        identity = str(rebased.get("idempotency_key") or "")
        prefix = identity.rsplit(":terminal:", 1)[0] if ":terminal:" in identity else str(rebased.get("job_id") or "")
        rebased["idempotency_key"] = f"{prefix}:terminal:{next_sequence}"
        rebased.pop("agent_signature", None)
        pending[0] = rebased
        self.executor.state.put(_PENDING_KEY, pending)

    def _drain(self) -> bool:
        pending = self._pending()
        drained_terminal = False
        while pending:
            try:
                self._send_receipt(pending[0])
            except CommercialPortalError:
                # Any non-2xx response, including a typed sequence regression,
                # is not an acknowledgement of this exact idempotency key.
                # Keep the durable receipt and delivery cursor unchanged until
                # Portal explicitly accepts this receipt.
                self.executor.state.put(_PENDING_KEY, pending)
                self._transport_failed("receipt_delivery")
                return False
            except Exception:
                self._transport_failed("receipt_delivery")
                self.executor.state.put(_PENDING_KEY, pending)
                return False
            else:
                self._transport_succeeded()
                acknowledge_pending = getattr(getattr(self.executor, "artifact_store", None), "acknowledge_pending", None)
                if callable(acknowledge_pending):
                    acknowledge_pending()
                drained_terminal = pending[0].get("schema") == SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA
                pending.pop(0)
                self.executor.state.put(_PENDING_KEY, pending)
        if drained_terminal:
            saved = self._pending_delivery()
            if saved:
                self._finish_command(cursor_next=str(saved.get("cursor_next") or saved.get("cursor") or ""))
        return True

    def _send_receipt(self, receipt: Mapping[str, Any]) -> Any:
        try:
            return self.client.send_receipt(dict(receipt), installation_id=self._active_installation_id)
        except TypeError:
            # Preserve the narrow fake/legacy client interface used by the
            # corporate v2 tests; the real PortalClient takes installation_id.
            return self.client.send_receipt(dict(receipt))

    def _transport_state(self) -> dict[str, Any]:
        value = self.executor.state.get(_TRANSPORT_KEY)
        return dict(value) if isinstance(value, Mapping) else {}

    def _transport_succeeded(self) -> None:
        current = self._transport_state()
        current.update({"failures": 0, "retry_after": 0.0, "last_success_at": float(self._clock()), "last_error": None})
        self.executor.state.put(_TRANSPORT_KEY, current)

    def _transport_failed(self, operation: str) -> float:
        current = self._transport_state()
        failures = max(0, int(current.get("failures") or 0)) + 1
        delay = min(self.backoff_max_seconds, self.backoff_base_seconds * (2 ** min(failures - 1, 12)))
        retry_after = float(self._clock()) + delay
        current.update({"failures": failures, "retry_after": retry_after, "last_error": str(operation)[:80]})
        self.executor.state.put(_TRANSPORT_KEY, current)
        return retry_after

    def _backoff_active(self) -> bool:
        transport_retry = float(self._transport_state().get("retry_after") or 0.0)
        server_retry = float(self.executor.state.get("commercial_portal_server_retry_after") or 0.0)
        return max(transport_retry, server_retry) > float(self._clock())

    def _outbound(self, operation: str, call: Callable[[], Any]) -> Any:
        try:
            value = call()
        except CommercialPortalError as exc:
            retry_after = self._transport_failed(f"{operation}:{exc.code}")
            raise _PortalTransportError(f"{operation}:{exc.code}:retry_after={retry_after:.3f}") from exc
        except Exception as exc:
            retry_after = self._transport_failed(operation)
            raise _PortalTransportError(f"{operation}:retry_after={retry_after:.3f}") from exc
        self._transport_succeeded()
        return value

    def _pending_delivery(self) -> dict[str, Any] | None:
        value = self.executor.state.get(_PENDING_DELIVERY_KEY)
        return dict(value) if isinstance(value, Mapping) else None

    @staticmethod
    def _commands_from_delivery(job: Mapping[str, Any]) -> list[dict[str, Any]]:
        commands = job.get("commands")
        if isinstance(commands, list):
            return [dict(item) for item in commands if isinstance(item, Mapping)]
        return [dict(job)]

    def _remember_delivery(self, *, installation_id: str, cursor: str, cursor_next: str, job: Mapping[str, Any]) -> None:
        self.executor.state.put(_PENDING_DELIVERY_KEY, {"installation_id": installation_id, "cursor": cursor, "cursor_next": cursor_next, "job": dict(job)})

    def _finish_delivery(self, *, cursor_next: str) -> None:
        self.executor.state.put("commercial_portal_cursor", cursor_next)
        self.executor.state.put(_PENDING_DELIVERY_KEY, None)

    def _finish_command(self, *, cursor_next: str) -> None:
        pending = self._pending_delivery()
        if not pending:
            self._finish_delivery(cursor_next=cursor_next)
            return
        commands = self._commands_from_delivery(pending.get("job") if isinstance(pending.get("job"), Mapping) else {})
        if commands:
            commands.pop(0)
        if commands:
            pending["job"] = {"commands": commands}
            self.executor.state.put(_PENDING_DELIVERY_KEY, pending)
        else:
            self._finish_delivery(cursor_next=cursor_next)

    def _minimal_inventory(self, installation_id: str) -> dict[str, Any]:
        values = self.context.get("runtime_overrides") if isinstance(self.context.get("runtime_overrides"), Mapping) else {}
        inventory_revision = getattr(self.executor.state, "inventory_revision", None)
        revision = int(inventory_revision(installation_id)) if callable(inventory_revision) else int(self.executor.state.get("commercial_inventory_revision") or 0)
        sync = {"desired_state_sync_protocol": 3, "pending_intent": self.executor.state.get("commercial_pending_sync_intent"), "conflict": self.executor.state.get("commercial_runtime_conflict")}
        settings = build_standalone_settings_snapshot(values, revision=revision, sync=sync)
        binding = {"agent_id": self.agent_id, "installation_id": installation_id, "host_fingerprint": self.host_fingerprint, "commercial_key_id": self.commercial_key_id}
        return {"schema": "portal-mcd-inventory-snapshot-v1", "contract_version": "1.0", "request_id": f"inventory-{int(self._clock() * 1000)}", "binding": binding, "snapshot_revision": str(self.executor.state.get("commercial_inventory_revision") or revision), "observed_at": _utc(), "complete": True, "host": {"mcd_version": str(self.context.get("mcd_version") or "runtime"), "profile": str(self.context.get("profile") or "commercial"), "service_status": "active", "scheduler_paused": bool(self.context.get("scheduler_paused", False)), "settings": settings}, "instances": []}

    def _inventory(self, installation_id: str) -> dict[str, Any]:
        if self.inventory_provider is not None:
            value = self.inventory_provider(installation_id)
            if not isinstance(value, Mapping):
                raise ValueError("commercial_inventory_provider_invalid")
            inventory = dict(value)
        else:
            inventory = self._minimal_inventory(installation_id)
        host = inventory.get("host") if isinstance(inventory.get("host"), Mapping) else None
        settings = host.get("settings") if isinstance(host, Mapping) and isinstance(host.get("settings"), Mapping) else None
        values = settings.get("values") if isinstance(settings, Mapping) and isinstance(settings.get("values"), Mapping) else None
        etag = str(settings.get("etag") or "") if isinstance(settings, Mapping) else ""
        if values is None or etag != standalone_settings_etag(values):
            raise ValueError("commercial_standalone_settings_etag_invalid")
        # Legacy callers may still provide the retired agent signature. Never
        # let it enter the durable inventory hash or outbound envelope.
        inventory.pop("agent_signature", None)
        prepare = getattr(self.executor.state, "prepare_inventory_snapshot", None)
        if not callable(prepare):
            raise ValueError("commercial_inventory_state_unavailable")
        inventory, record = prepare(installation_id=installation_id, inventory=inventory)
        durable_hash = str(record.get("durable_hash") or "")
        revision = int(record.get("snapshot_revision") or 0)
        # Keep request identity stable for retries of the same durable
        # revision, while excluding it from the durable content hash.
        inventory["request_id"] = f"inventory-{revision}-{durable_hash[7:]}"
        # The inventory snapshot and noop consumer must use durable values
        # across restart; do not recompute a second settings hash in the worker.
        self.executor.state.put(
            "commercial_standalone_settings_snapshot_v1",
            {"schema": STANDALONE_SETTINGS_SNAPSHOT_SCHEMA, **dict(settings)},
        )
        self.executor.state.put("commercial_standalone_etag", etag)
        return inventory

    def _ensure_adopted(self, installation_id: str) -> bool:
        if not callable(getattr(self.client, "adopt", None)):
            return True
        adopted = self.executor.state.get(_ADOPTED_KEY)
        adopted = dict(adopted) if isinstance(adopted, Mapping) else {}
        if adopted.get(installation_id):
            return True
        inventory = self._inventory(installation_id)
        try:
            self._outbound("adopt", lambda: self.client.adopt(installation_id=installation_id, inventory=inventory))
        except _PortalTransportError:
            return False
        acknowledge = getattr(self.executor.state, "acknowledge_inventory", None)
        if callable(acknowledge):
            acknowledge(installation_id=installation_id, inventory=inventory)
        adopted[installation_id] = {"request_id": inventory.get("request_id"), "snapshot_revision": inventory.get("snapshot_revision"), "adopted_at": _utc()}
        self.executor.state.put(_ADOPTED_KEY, adopted)
        return True

    def _publish_capabilities(self, installation_id: str) -> bool:
        """Publish only the MCD primitives explicitly declared by the host."""
        if self.capability_inventory_provider is None or not callable(getattr(self.client, "send_capability_inventory", None)):
            return True
        raw = self.capability_inventory_provider(installation_id)
        if not isinstance(raw, Mapping):
            raise ValueError("commercial_capability_inventory_provider_invalid")
        inventory = validate_capability_inventory(raw)
        if inventory["installation"]["installation_id"] != str(installation_id):
            raise ValueError("commercial_capability_inventory_binding_mismatch")
        sent = self.executor.state.get(_GENERIC_INVENTORY_KEY)
        sent = dict(sent) if isinstance(sent, Mapping) else {}
        prior = sent.get(str(installation_id))
        if isinstance(prior, Mapping) and prior.get("inventory_sha256") == inventory["inventory_sha256"]:
            return True
        try:
            self._outbound("capability_inventory", lambda: self.client.send_capability_inventory(installation_id=installation_id, inventory=inventory))
        except _PortalTransportError:
            return False
        sent[str(installation_id)] = {"inventory_sha256": inventory["inventory_sha256"], "request_id": inventory["request_id"], "observed_at": inventory["observed_at"]}
        self.executor.state.put(_GENERIC_INVENTORY_KEY, sent)
        self.executor.state.put("commercial_portal_generic_capabilities_v1", {str(installation_id): inventory})
        return True

    def _publish_inventory(self, installation_id: str) -> bool:
        if not callable(getattr(self.client, "send_inventory", None)) or self.inventory_provider is None:
            return True
        inventory = self._inventory(installation_id)
        state = self.executor.state.get("commercial_inventory_state_v1")
        installations = state.get("installations") if isinstance(state, Mapping) else None
        record = installations.get(str(installation_id)) if isinstance(installations, Mapping) else None
        pinned = record.get("last_published") if isinstance(record, Mapping) else None
        durable_hash = str(record.get("durable_hash") or "") if isinstance(record, Mapping) else ""
        try:
            revision = int(record.get("snapshot_revision") or 0) if isinstance(record, Mapping) else 0
            pinned_revision = int(pinned.get("snapshot_revision") or -1) if isinstance(pinned, Mapping) else -1
        except (TypeError, ValueError):
            raise ValueError("commercial_inventory_state_invalid")
        if not self._publish_inventory_on_startup and isinstance(pinned, Mapping) and str(pinned.get("durable_hash") or "") == durable_hash and pinned_revision == revision:
            return True
        try:
            self._outbound("inventory", lambda: self._send_inventory(installation_id, inventory))
        except _PortalTransportError:
            return False
        acknowledge = getattr(self.executor.state, "acknowledge_inventory", None)
        if callable(acknowledge):
            acknowledge(installation_id=installation_id, inventory=inventory)
        self._publish_inventory_on_startup = False
        return True

    def _send_inventory(self, installation_id: str, inventory: Mapping[str, Any]) -> Any:
        """Use stable inventory idempotency while retaining narrow test clients."""
        try:
            return self.client.send_inventory(
                installation_id=installation_id,
                inventory=dict(inventory),
                request_id=str(inventory.get("request_id") or ""),
                idempotency_key=str(inventory.get("request_id") or ""),
            )
        except TypeError:
            return self.client.send_inventory(installation_id=installation_id, inventory=dict(inventory))

    @staticmethod
    def _valid_etag(value: object) -> bool:
        text = str(value or "")
        return len(text) == 71 and text.startswith("sha256:") and all(char in "0123456789abcdef" for char in text[7:])

    def _noop_failure(self, command: Mapping[str, Any], *, cursor_next: str, reason: str, expected: str = "", actual: str = "", expires_at: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": reason}
        if expected:
            payload["expected_standalone_etag"] = expected
        if actual:
            payload["actual_standalone_etag"] = actual
        if expires_at:
            payload["expires_at"] = expires_at
        receipt = self._command_receipt(command, status="failed", payload=payload)
        if not self._send_or_queue(receipt):
            return {"status": "receipt_pending", "command_id": receipt["command_id"], "reason": reason}
        self._finish_command(cursor_next=cursor_next)
        return {"status": "failed", "command_id": receipt["command_id"], "reason": reason}

    def _run_noop(self, command: Mapping[str, Any], *, cursor_next: str) -> dict[str, Any]:
        expected = str(command.get("expected_standalone_etag") or "")
        durable_snapshot = self.executor.state.get("commercial_standalone_settings_snapshot_v1")
        durable_etag = durable_snapshot.get("etag") if isinstance(durable_snapshot, Mapping) else None
        actual = str(self.context.get("standalone_etag") or self.executor.state.get("commercial_standalone_etag") or durable_etag or "")
        expires_at = str(command.get("expires_at") or "")
        if not expires_at:
            return self._noop_failure(command, cursor_next=cursor_next, reason="commercial_noop_command_invalid", expected=expected, actual=actual)
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return self._noop_failure(command, cursor_next=cursor_next, reason="commercial_noop_command_invalid", expected=expected, actual=actual, expires_at=expires_at)
        if expires <= float(self._clock()):
            return self._noop_failure(command, cursor_next=cursor_next, reason="commercial_noop_command_expired", expected=expected, actual=actual, expires_at=expires_at)
        if not self._valid_etag(expected):
            return self._noop_failure(command, cursor_next=cursor_next, reason="commercial_noop_command_invalid", expected=expected, actual=actual)
        if not self._valid_etag(actual):
            return self._noop_failure(command, cursor_next=cursor_next, reason="commercial_standalone_etag_unavailable", expected=expected, actual=actual)
        if actual != expected:
            return self._noop_failure(command, cursor_next=cursor_next, reason="commercial_standalone_etag_mismatch", expected=expected, actual=actual)
        unchanged = actual == expected
        receipt = {
            "schema": _PORTAL_NOOP_RECEIPT_SCHEMA,
            "contract_version": PORTAL_MCD_ADOPT_CONTRACT_VERSION,
            "command_id": str(command.get("command_id") or ""),
            "delivery_cursor": int(command.get("delivery_cursor") or 0),
            "request_id": str(command.get("request_id") or ""),
            "idempotency_key": str(command.get("idempotency_key") or ""),
            "binding": dict(command.get("binding") or {}),
            "status": "succeeded" if unchanged else "failed",
            "observed_at": _utc(),
            "standalone": {"before_etag": actual, "after_etag": actual, "unchanged": unchanged},
            "connection_status": "active",
        }
        if not receipt["command_id"] or receipt["delivery_cursor"] <= 0 or not receipt["request_id"] or not receipt["idempotency_key"]:
            return {"status": "rejected", "reason": "commercial_noop_command_invalid"}
        if not self._send_or_queue(receipt):
            return {"status": "receipt_pending", "command_id": receipt["command_id"]}
        if self._pending():
            return {"status": "receipt_pending", "command_id": receipt["command_id"]}
        self._finish_command(cursor_next=cursor_next)
        return {"status": receipt["status"], "command_id": receipt["command_id"]}

    def _command_receipt(self, command: Mapping[str, Any], *, status: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        binding = command.get("binding") if isinstance(command.get("binding"), Mapping) else {"agent_id": self.agent_id, "installation_id": self._active_installation_id, "host_fingerprint": self.host_fingerprint, "commercial_key_id": self.commercial_key_id}
        return {"schema": "portal-mcd-command-receipt-v1", "contract_version": "1.0", "command_id": str(command.get("command_id") or command.get("conflict_id") or ""), "delivery_cursor": int(command.get("delivery_cursor") or 0), "request_id": str(command.get("request_id") or command.get("command_id") or ""), "idempotency_key": str(command.get("idempotency_key") or ""), "binding": dict(binding), "status": status if status in {"succeeded", "failed"} else "failed", "payload": dict(payload), "observed_at": _utc()}

    def _run_artifact_trust_update(self, command: Mapping[str, Any], *, cursor_next: str) -> dict[str, Any]:
        try:
            update = validate_portal_artifact_trust_update(command)
            binding = update["binding"]
            expected = {
                "agent_id": self.agent_id,
                "installation_id": self._active_installation_id,
                "host_fingerprint": self.host_fingerprint,
                "commercial_key_id": self.commercial_key_id,
            }
            if any(str(binding.get(key) or "") != str(expected[key]) for key in expected):
                raise ValueError("portal_artifact_trust_update_binding_mismatch")
            issued = datetime.fromisoformat(str(update["issued_at"]).replace("Z", "+00:00")).timestamp()
            expires = datetime.fromisoformat(str(update["expires_at"]).replace("Z", "+00:00")).timestamp()
            now = float(self._clock())
            if expires <= issued or now < issued - 60 or now >= expires:
                raise ValueError("portal_artifact_trust_update_expired_or_not_yet_valid")
            result = self.executor.state.apply_portal_artifact_trust_update(update)
            # The worker may receive the trust update and the next
            # scenario.apply command in the same long-lived process. Refresh
            # the in-memory capability immediately; restart persistence is
            # handled by the daemon factory reading the same durable state.
            self.publisher_keys = portal_artifact_trust_keys(self.executor.state.get("portal_artifact_trust_v1"))
            status = "succeeded"
        except Exception as exc:
            reason = str(exc).strip()
            if not reason.startswith("portal_artifact_trust_") or len(reason) > 120 or not all(char.isalnum() or char in "_" for char in reason):
                reason = "portal_artifact_trust_update_failed"
            result = {"reason": reason}
            status = "failed"
        receipt = self._command_receipt(command, status=status, payload=result)
        if not self._send_or_queue(receipt):
            return {"status": "receipt_pending", "command_id": receipt["command_id"]}
        self._finish_command(cursor_next=cursor_next)
        return {"status": status, "command_id": receipt["command_id"]}

    def _generic_receipt(self, job: Mapping[str, Any], *, terminal: bool, sequence: int, status: str, result: Mapping[str, Any] | None = None, error: Mapping[str, Any] | None = None) -> dict[str, Any]:
        installation = dict(job["installation"])
        payload: dict[str, Any] = {
            "schema": GENERIC_TERMINAL_RECEIPT_SCHEMA if terminal else GENERIC_STAGE_RECEIPT_SCHEMA,
            "contract_version": "1.0",
            "job_id": str(job["job_id"]),
            "request_id": str(job["request_id"]),
            "command_id": str(job["command_id"]),
            "installation": installation,
            "sequence": sequence,
            "status": status,
        }
        if terminal:
            payload.update({"started_at": str(job.get("issued_at") or _utc()), "completed_at": _utc()})
        else:
            payload.update({"stage_id": "execute", "observed_at": _utc()})
        if result is not None:
            payload["result"] = _bounded_result(result)
        if error is not None:
            payload["error"] = {"code": str(error.get("code") or "commercial_generic_command_failed")[:120], "message": str(error.get("message") or "generic command failed")[:1024]}
        if terminal:
            return validate_generic_terminal_receipt(payload)
        return validate_generic_stage_receipt(payload)

    def _terminalize_generic_failure(self, job: Mapping[str, Any], *, cursor_next: str, reason: str) -> dict[str, Any]:
        """Close a leased, valid generic job when admission cannot proceed."""
        job_id = str(job["job_id"])
        digest = hashlib.sha256(canonical_json(dict(job))).hexdigest()
        terminal = self._generic_receipt(
            job,
            terminal=True,
            sequence=1,
            status="failed",
            error={"code": reason, "message": reason},
        )
        runs = self.executor.state.get(_GENERIC_RUNS_KEY)
        runs = dict(runs) if isinstance(runs, Mapping) else {}
        runs[job_id] = {"job_digest": digest, "started_sent": False, "terminal": terminal}
        if len(runs) > 64:
            for key in list(runs)[:-64]:
                runs.pop(key, None)
        self.executor.state.put(_GENERIC_RUNS_KEY, runs)
        if not self._send_or_queue(terminal):
            return {"status": "receipt_pending", "job_id": job_id}
        self._finish_command(cursor_next=cursor_next)
        return {"status": "failed", "job_id": job_id}

    def _run_generic_command(self, command: Mapping[str, Any], *, cursor_next: str) -> dict[str, Any]:
        """Run one advertised generic primitive with durable receipts."""
        inventories = self.executor.state.get("commercial_portal_generic_capabilities_v1")
        inventory_map = inventories if isinstance(inventories, Mapping) else {}
        inventory = inventory_map.get(str(self._active_installation_id))
        try:
            job = validate_generic_command_job(command)
        except Exception as exc:
            return {"status": "rejected", "reason": str(exc)[:120]}
        if job["installation"]["installation_id"] != str(self._active_installation_id):
            return self._terminalize_generic_failure(job, cursor_next=cursor_next, reason="portal_mcd_generic_installation_mismatch")
        if not isinstance(inventory, Mapping):
            return self._terminalize_generic_failure(job, cursor_next=cursor_next, reason="commercial_capability_inventory_unavailable")
        try:
            job = validate_generic_command_job(command, inventory=inventory)
        except Exception as exc:
            return self._terminalize_generic_failure(job, cursor_next=cursor_next, reason=str(exc)[:120])
        try:
            expires = datetime.fromisoformat(str(job["expires_at"]).replace("Z", "+00:00")).timestamp()
            issued = datetime.fromisoformat(str(job["issued_at"]).replace("Z", "+00:00")).timestamp()
            if expires <= issued or float(self._clock()) >= expires:
                raise ValueError("portal_mcd_generic_command_expired")
        except (TypeError, ValueError, OverflowError) as exc:
            reason = str(exc) if str(exc).startswith("portal_mcd_generic_") else "portal_mcd_generic_command_time_invalid"
            terminal = self._generic_receipt(job, terminal=True, sequence=1, status="failed", error={"code": reason, "message": reason})
            if not self._send_or_queue(terminal):
                return {"status": "receipt_pending", "job_id": job["job_id"]}
            self._finish_command(cursor_next=cursor_next)
            return {"status": "failed", "job_id": job["job_id"]}

        job_id = str(job["job_id"])
        digest = hashlib.sha256(canonical_json(job)).hexdigest()
        runs = self.executor.state.get(_GENERIC_RUNS_KEY)
        runs = dict(runs) if isinstance(runs, Mapping) else {}
        saved = runs.get(job_id)
        if isinstance(saved, Mapping) and saved.get("job_digest") != digest:
            return {"status": "rejected", "reason": "portal_mcd_generic_job_reuse_conflict"}
        started_sent = bool(saved.get("started_sent")) if isinstance(saved, Mapping) else False
        if not started_sent:
            started = self._generic_receipt(job, terminal=False, sequence=1, status="started")
            runs[job_id] = {"job_digest": digest, "started_sent": True}
            self.executor.state.put(_GENERIC_RUNS_KEY, runs)
            if not self._send_or_queue(started):
                return {"status": "receipt_pending", "job_id": job_id}
        saved = runs.get(job_id)
        terminal_saved = saved.get("terminal") if isinstance(saved, Mapping) else None
        if not isinstance(terminal_saved, Mapping):
            try:
                if self.generic_command_handler is None:
                    raise ValueError("commercial_generic_command_handler_unconfigured")
                result = self.generic_command_handler(dict(job))
                result = dict(result) if isinstance(result, Mapping) else {"value": result}
                status = "failed" if str(result.get("status") or "").lower() in {"failed", "error", "conflict"} else "succeeded"
                error = {"code": str(result.get("reason") or "commercial_generic_command_failed"), "message": "generic primitive returned failure"} if status == "failed" else None
            except Exception as exc:
                result = None
                status = "failed"
                error = {"code": type(exc).__name__[:120], "message": str(exc)[:1024] or "generic primitive failed"}
            terminal_saved = self._generic_receipt(job, terminal=True, sequence=2, status=status, result=result, error=error)
            runs[job_id] = {"job_digest": digest, "started_sent": True, "terminal": terminal_saved}
            if len(runs) > 64:
                for key in list(runs)[:-64]:
                    runs.pop(key, None)
            self.executor.state.put(_GENERIC_RUNS_KEY, runs)
        if not self._send_or_queue(dict(terminal_saved)):
            return {"status": "receipt_pending", "job_id": job_id}
        self._finish_command(cursor_next=cursor_next)
        return {"status": str(terminal_saved["status"]), "job_id": job_id}

    def _run_runtime_command(self, command: Mapping[str, Any], *, cursor_next: str) -> dict[str, Any]:
        if str(command.get("schema") or "") != "mcc-desired-runtime-sync-v3" or int(command.get("desired_state_sync_protocol") or 0) != 3:
            receipt = self._command_receipt(command, status="failed", payload={"reason": "commercial_command_unsupported"})
            if self._send_or_queue(receipt):
                self._finish_command(cursor_next=cursor_next)
            return {"status": "failed", "reason": "commercial_command_unsupported"}
        if not str(command.get("command_id") or "").strip() or int(command.get("delivery_cursor") or 0) <= 0 or not str(command.get("idempotency_key") or "").strip():
            # Portal must expose the durable command identity in every poll
            # payload. Without it no receipt can be accepted safely; retain
            # the batch for replay instead of acknowledging an unaddressable
            # command.
            return {"status": "blocked", "reason": "commercial_command_identity_missing"}
        try:
            if self.runtime_command_handler is None:
                raise ValueError("commercial_runtime_handler_unconfigured")
            result = self.runtime_command_handler(dict(command))
            result = dict(result) if isinstance(result, Mapping) else {"status": "succeeded", "result": result}
            status = "failed" if str(result.get("status") or "").lower() in {"failed", "error", "conflict"} else "succeeded"
        except Exception as exc:
            result = {"reason": type(exc).__name__[:80]}
            status = "failed"
        receipt = self._command_receipt(command, status=status, payload=result)
        if not self._send_or_queue(receipt):
            return {"status": "receipt_pending", "command_id": receipt["command_id"]}
        self._finish_command(cursor_next=cursor_next)
        return {"status": status, "command_id": receipt["command_id"]}

    def _publish_active_state(self) -> bool:
        if not callable(getattr(self.client, "send_receipt", None)):
            return True
        if self.executor.state.get("commercial_portal_connection_state") == "active":
            return True
        state = {"schema": "portal-mcd-connection-state-v1", "contract_version": "1.0", "event_id": f"active-{int(self._clock() * 1000)}", "request_id": f"active-{self._active_installation_id}", "idempotency_key": f"active:{self._active_installation_id}", "binding": {"agent_id": self.agent_id, "installation_id": self._active_installation_id, "host_fingerprint": self.host_fingerprint, "commercial_key_id": self.commercial_key_id}, "status": "active", "reason": "outbound_cycle", "observed_at": _utc()}
        if not self._send_or_queue(state):
            return False
        self.executor.state.put("commercial_portal_connection_state", "active")
        return True

    def _replay_saved_delivery(self, pending: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        job = pending.get("job")
        installation_id = str(pending.get("installation_id") or "")
        cursor_next = str(pending.get("cursor_next") or pending.get("cursor") or "")
        if not installation_id or not isinstance(job, Mapping):
            self.executor.state.put(_PENDING_DELIVERY_KEY, None)
            return None
        return installation_id, cursor_next, dict(job)

    def _delivery_failure(self, command: Mapping[str, Any], *, cursor_next: str, error: BaseException, sequence: int = 1) -> dict[str, Any]:
        """Close an accepted delivery with a typed terminal receipt.

        Poll/transport failures happen before a delivery exists and are
        retried normally. Once Portal has leased and MCD has durably saved a
        delivery, local rejection, expiry, disabled state, and artifact
        unavailability must not leave that lease silent.
        """
        if _is_recovery_command(command):
            sequence = max(2, int(sequence))
        failed = _failed_terminal_for_delivery(
            command,
            error=error,
            sequence=sequence,
            expected_agent_id=self.agent_id,
            expected_installation_id=self._active_installation_id,
            expected_commercial_key_id=self.commercial_key_id,
            expected_host_fingerprint=self.host_fingerprint,
        )
        if failed is None:
            return {"status": "rejected", "reason": _error_code(error), "job_id": str(_delivery_job(command).get("job_id") or "")}
        delivered = self._send_or_queue(failed)
        if delivered:
            self._finish_command(cursor_next=cursor_next)
        return {"status": "failed" if delivered else "receipt_pending", "job_id": str(failed["job_id"]), **({"receipt_pending": True} if not delivered else {})}

    def _local_delivery_state_error(self) -> ValueError | None:
        state = self.executor.state
        connection_state = str(state.get("commercial_portal_connection_state") or "").strip().lower()
        if bool(state.get("commercial_revoked")) or bool(state.get("commercial_detached")) or connection_state in {"revoked", "detached"}:
            return ValueError("self_hosted_mcd_commercial_state_revoked")
        if bool(state.get("commercial_disabled")) or connection_state in {"disabled", "inactive"}:
            return ValueError("self_hosted_mcd_commercial_state_disabled")
        return None

    def _download_scenario_artifact(self, pin: Mapping[str, Any]) -> bytes:
        """Use authenticated MCC API transport for the catalog SHA-only path."""
        if pin.get("artifact_trust_mode") == "mcc_catalog_sha256":
            downloader = getattr(self.client, "download_artifact", None)
            if not callable(downloader):
                raise ValueError("self_hosted_mcd_authenticated_artifact_transport_unavailable")
            return downloader(
                path=str(pin["artifact_path"]),
                grant=str(pin["artifact_grant"]),
                expected_size=int(pin["artifact_size_bytes"]),
            )
        return download_external_artifact(
            pin["artifact_uri"],
            expected_size=pin["artifact_size_bytes"],
        )

    def run_once(self) -> dict[str, Any]:
        self._normalize_pending_recovery_receipt()
        if not self._drain():
            return {"status": "receipt_pending"}
        pending = self._pending_delivery()
        saved = self._replay_saved_delivery(pending) if pending else None
        if saved:
            active_installation_id, cursor_next, job = saved
            self._active_installation_id = active_installation_id
            cursor = str(pending.get("cursor") or "")
        else:
            if self._backoff_active():
                state = self._transport_state()
                return {"status": "backoff", "retry_after": float(state.get("retry_after") or 0.0)}
            cursor = str(self.executor.state.get("commercial_portal_cursor") or "")
            delivery = None
            active_installation_id = self.installation_ids[0]
            try:
                for active_installation_id in self.installation_ids:
                    self._active_installation_id = active_installation_id
                    if not self._ensure_adopted(active_installation_id) or not self._publish_inventory(active_installation_id):
                        return {"status": "backoff", "retry_after": float(self._transport_state().get("retry_after") or 0.0)}
                    if not self._publish_capabilities(active_installation_id):
                        return {"status": "backoff", "retry_after": float(self._transport_state().get("retry_after") or 0.0)}
                    if callable(getattr(self.client, "adopt", None)) and not self._publish_active_state():
                        return {"status": "receipt_pending"}
                    delivery = self._outbound(
                        "job_poll",
                        lambda active_installation_id=active_installation_id: self.client.pull_job(
                            installation_id=active_installation_id,
                            cursor=cursor,
                            wait_seconds=self.poll_wait_seconds,
                            limit=1,
                        ),
                    )
                    if delivery:
                        break
            except _PortalTransportError:
                return {"status": "backoff", "retry_after": float(self._transport_state().get("retry_after") or 0.0)}
            if not delivery:
                return {"status": "idle"}
            cursor_next = str(delivery.get("cursor", cursor)) if isinstance(delivery, Mapping) else cursor
            if isinstance(delivery, Mapping) and isinstance(delivery.get("commands"), list):
                job = {"commands": [dict(item) for item in delivery["commands"] if isinstance(item, Mapping)]}
            else:
                job = delivery.get("job") if isinstance(delivery, Mapping) and isinstance(delivery.get("job"), Mapping) else delivery
            if not isinstance(job, Mapping):
                return {"status": "rejected", "reason": "self_hosted_mcd_job_delivery_missing"}
            self._remember_delivery(installation_id=active_installation_id, cursor=cursor, cursor_next=cursor_next, job=job)
        commands = self._commands_from_delivery(job)
        if not commands:
            self._finish_delivery(cursor_next=cursor_next)
            return {"status": "idle"}
        command = commands[0]
        if command.get("schema") == _PORTAL_NOOP_SCHEMA:
            return self._run_noop(command, cursor_next=cursor_next)
        if command.get("schema") == "mcc-desired-runtime-sync-v3":
            return self._run_runtime_command(command, cursor_next=cursor_next)
        if command.get("schema") == COMMAND_JOB_SCHEMA:
            return self._run_generic_command(command, cursor_next=cursor_next)
        if command.get("schema") == PORTAL_ARTIFACT_TRUST_UPDATE_SCHEMA:
            # Signing/key trust updates are retired.  Artifact integrity is
            # enforced by the pinned size/SHA-256 check after HTTPS download.
            receipt = self._command_receipt(command, status="failed", payload={"reason": "commercial_artifact_trust_layer_unsupported"})
            if self._send_or_queue(receipt):
                self._finish_command(cursor_next=cursor_next)
            return {"status": "failed", "reason": "commercial_artifact_trust_layer_unsupported"}
        if command.get("schema") not in {None, SELF_HOSTED_MCD_JOB_SCHEMA, "portal-mcd-job-delivery-v1"}:
            receipt = self._command_receipt(command, status="failed", payload={"reason": "commercial_command_unsupported"})
            if self._send_or_queue(receipt):
                self._finish_command(cursor_next=cursor_next)
            return {"status": "failed", "reason": "commercial_command_unsupported"}
        if command.get("schema") == "portal-mcd-job-delivery-v1" and isinstance(command.get("job"), Mapping):
            command = dict(command["job"])
        try:
            verified = verify_self_hosted_mcd_job(command, self.portal_keys, expected_agent_id=self.agent_id, expected_installation_id=active_installation_id, expected_commercial_key_id=self.commercial_key_id, expected_host_fingerprint=self.host_fingerprint)
        except ValueError as exc:
            return self._delivery_failure(command, cursor_next=cursor_next, error=exc)
        local_state_error = self._local_delivery_state_error()
        if local_state_error is not None:
            return self._delivery_failure(command, cursor_next=cursor_next, error=local_state_error)
        try:
            bootstrap_pin = verified.get("bootstrap_artifact")
            scenario_pin = verified["scenario_artifact"]
            try:
                bootstrap = None
                if bootstrap_pin is not None:
                    bootstrap = self._outbound("bootstrap_download", lambda: self._download_scenario_artifact(bootstrap_pin))
                scenario = self._outbound("scenario_download", lambda: self._download_scenario_artifact(scenario_pin))
            except _PortalTransportError as exc:
                return self._delivery_failure(command, cursor_next=cursor_next, error=_artifact_download_failure(exc))
            command_id = str(command.get("command_id") or "").strip()
            delivery_cursor = command.get("delivery_cursor")
            execution_key = ""
            if command_id and isinstance(delivery_cursor, int) and not isinstance(delivery_cursor, bool):
                execution_key = f"{command_id}:{delivery_cursor}"
            terminal = self.executor.run(command, portal_keys=self.portal_keys, publisher_keys=self.publisher_keys, context=self.context, expected_agent_id=self.agent_id, expected_installation_id=str(command["installation_id"]), expected_commercial_key_id=self.commercial_key_id, expected_host_fingerprint=self.host_fingerprint, bootstrap_artifact=bootstrap, scenario_artifact=scenario, receipt_sink=self._send_or_queue, execution_key=execution_key or None)
            if self._pending():
                # Keep the delivery and cursor pending until the next outbound
                # cycle drains every typed receipt in order.  The executor's
                # durable terminal makes the following replay idempotent.
                return {"status": terminal.get("status"), "job_id": str(command["job_id"]), "receipt_pending": True}
            self._finish_command(cursor_next=cursor_next)
            return {"status": terminal.get("status"), "job_id": str(command["job_id"])}
        except Exception as exc:
            failed = _failed_terminal_receipt(verified, error=exc, sequence=1, started_at=_utc(), completed=0, total=1)
            delivered = self._send_or_queue(failed)
            if delivered:
                self._finish_command(cursor_next=cursor_next)
            return {"status": "failed" if delivered else "receipt_pending", "job_id": str(command["job_id"])}

    def run_forever(self, stop_event: Any, *, interval_sec: float = 1.0) -> None:
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                pass
            stop_event.wait(max(0.1, float(interval_sec)))
