"""Generic Portal self-hosted MCD v2 execution boundary.

This module deliberately knows only the v2 wire contract and a bounded external
runner process.  It contains no installer, OS, Mautic, scenario, or primitive
recipes.  Those belong to the signed external artifact and its Portal-owned
runner.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from mcd_agent.commercial_detector import read_normalized_local_facts
from mcd_agent.commercial_portal import (
    PORTAL_MCD_ADOPT_CONTRACT_VERSION,
    SELF_HOSTED_MCD_JOB_SCHEMA,
    SELF_HOSTED_MCD_CONTRACT_VERSION,
    SELF_HOSTED_MCD_STAGE_RECEIPT_SCHEMA,
    SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA,
    SELF_HOSTED_MCD_STAGE_IDS,
    SELF_HOSTED_MCD_STAGE_STATUSES,
    SELF_HOSTED_MCD_TERMINAL_STATUSES,
    SELF_HOSTED_MCD_ROLLBACK_STATUSES,
    CommercialState,
    canonical_json,
    download_external_artifact,
    verify_self_hosted_external_artifact,
    verify_self_hosted_mcd_job,
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


class _PortalTransportError(RuntimeError):
    """An outbound Portal failure, kept separate from local job rejection."""


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
    return (type(error).__name__ or "scenario_execution_failed")[:80]


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
    if receipt.get("schema") != SELF_HOSTED_MCD_STAGE_RECEIPT_SCHEMA or receipt.get("contract_version") != SELF_HOSTED_MCD_CONTRACT_VERSION:
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
    if receipt.get("schema") != SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA or receipt.get("contract_version") != SELF_HOSTED_MCD_CONTRACT_VERSION:
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


def _failed_terminal_receipt(verified: Mapping[str, Any], *, error: BaseException, sequence: int, started_at: str, completed: int = 0, total: int = 1, rollback: str = "not_attempted") -> dict[str, Any]:
    binding = _binding_from_verified(verified)
    result = {
        **binding,
        "schema": SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA,
        "contract_version": SELF_HOSTED_MCD_CONTRACT_VERSION,
        "sequence": max(1, int(sequence)),
        "idempotency_key": f"{binding['job_id']}:terminal:{max(1, int(sequence))}",
        "status": "failed",
        "observed_at": _utc(),
        "started_at": started_at,
        "completed_at": _utc(),
        "result": {"executor": "mcd-self-hosted-external", "completed": max(0, int(completed)), "total": max(0, int(total))},
        "error": {"code": "self_hosted_mcd_execution_failed", "type": _error_code(error)},
        "rollback": rollback if rollback in SELF_HOSTED_MCD_ROLLBACK_STATUSES else "not_attempted",
    }
    validate_self_hosted_terminal_receipt(result, binding=binding)
    return result


class ExternalProcessPolicy:
    """Run one externally supplied runner with no shell or inherited secrets."""

    def __init__(self, argv: list[str] | tuple[str, ...] | None, *, timeout_sec: int = 1800):
        self.argv = tuple(str(item) for item in (argv or ()))
        self.timeout_sec = max(1, min(3600, int(timeout_sec)))
        if self.argv:
            if not os.path.isabs(self.argv[0]) or not os.path.isfile(self.argv[0]) or not os.access(self.argv[0], os.X_OK):
                raise ValueError("self_hosted_mcd_runner_not_allowlisted")
            if any(not item or "\x00" in item for item in self.argv):
                raise ValueError("self_hosted_mcd_runner_argv_invalid")

    def run(self, *, artifact: bytes, job: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
        if not self.argv:
            raise ValueError("self_hosted_mcd_runner_unconfigured")
        env = {"LC_ALL": "C", "LANG": "C"}
        payload = {"job_id": str(job["job_id"]), "scenario": dict(job["scenario"]), "facts": dict(facts)}
        try:
            completed = subprocess.run(
                [*self.argv, "--mcd-scenario-apply"],
                input=artifact,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=env,
                timeout=self.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("self_hosted_mcd_runner_timeout") from exc
        if completed.returncode != 0:
            raise RuntimeError("self_hosted_mcd_runner_failed")
        stdout = completed.stdout[:_MAX_PROCESS_OUTPUT_BYTES]
        if not stdout:
            return {"status": "succeeded"}
        try:
            parsed = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "succeeded", "evidence": "runner_completed"}
        return {"status": "succeeded", "runner": _bounded_result(parsed), "binding": hashlib.sha256(canonical_json(payload)).hexdigest()}


class SelfHostedScenarioExecutor:
    def __init__(self, state: CommercialState, *, runner: Callable[..., Any] | None = None, facts_reader: Callable[..., Mapping[str, Any]] | None = None):
        self.state = state
        self.runner = runner
        self.facts_reader = facts_reader or read_normalized_local_facts
        with sqlite3.connect(self.state.path) as db:
            db.execute(f"CREATE TABLE IF NOT EXISTS {_RUNS_TABLE} (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def _load(self, job_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.state.path) as db:
            row = db.execute(f"SELECT payload FROM {_RUNS_TABLE} WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, job_id: str, payload: Mapping[str, Any]) -> None:
        with sqlite3.connect(self.state.path) as db:
            db.execute(f"INSERT INTO {_RUNS_TABLE}(job_id,payload) VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload", (job_id, json.dumps(payload, sort_keys=True)))

    def _local_facts(self, expected: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        target_kind = str(expected.get("target_kind") or context.get("target_kind") or "existing_server")
        capabilities = list(context.get("local_facts_capabilities") or ["package_manager.query"])
        observed = dict(self.facts_reader(target_kind=target_kind, capabilities=capabilities))
        observed.pop("detector_schema", None)
        observed["capabilities"] = sorted(set(str(item) for item in observed.get("capabilities", [])))
        normalized_expected = dict(expected)
        normalized_expected["capabilities"] = sorted(set(str(item) for item in expected.get("capabilities", [])))
        if observed != normalized_expected:
            raise ValueError("self_hosted_mcd_applicability_facts_mismatch")
        return observed

    def run(self, job: Mapping[str, Any], *, portal_keys: Mapping[str, str], publisher_keys: Mapping[str, str], context: Mapping[str, Any], expected_agent_id: str, expected_installation_id: str, expected_commercial_key_id: str, expected_host_fingerprint: str, bootstrap_artifact: bytes, scenario_artifact: bytes, receipt_sink: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        verified = verify_self_hosted_mcd_job(job, portal_keys, expected_agent_id=expected_agent_id, expected_installation_id=expected_installation_id, expected_commercial_key_id=expected_commercial_key_id, expected_host_fingerprint=expected_host_fingerprint)
        binding = _binding_from_verified(verified)
        verify_self_hosted_external_artifact(verified["bootstrap_artifact"], bootstrap_artifact, publisher_keys, kind="bootstrap")
        verify_self_hosted_external_artifact(verified["scenario_artifact"], scenario_artifact, publisher_keys, kind="scenario")
        facts = self._local_facts(verified["facts"], context)
        job_id = binding["job_id"]
        prior = self._load(job_id)
        if isinstance(prior, dict) and prior.get("job_digest") == hashlib.sha256(canonical_json(dict(job))).hexdigest() and isinstance(prior.get("terminal"), dict):
            return dict(prior["terminal"])
        started_at = _utc()
        sequence = 0

        def emit(stage_id: str, status: str, result: object = None, error: Mapping[str, Any] | None = None) -> None:
            nonlocal sequence
            sequence += 1
            receipt = {**binding, "schema": SELF_HOSTED_MCD_STAGE_RECEIPT_SCHEMA, "contract_version": SELF_HOSTED_MCD_CONTRACT_VERSION, "stage_id": stage_id, "sequence": sequence, "status": status, "idempotency_key": f"{binding['job_id']}:{stage_id}:{sequence}", "observed_at": _utc()}
            if result is not None:
                receipt["result"] = _bounded_result(result)
            if error is not None:
                receipt["error"] = {"code": str(error.get("code") or "self_hosted_mcd_stage_failed")[:120]}
            validate_self_hosted_stage_receipt(receipt, binding=binding)
            if receipt_sink:
                receipt_sink(receipt)

        try:
            emit("scenario_dispatch", "succeeded", {"status": "accepted"})
            if self.runner is None:
                raise ValueError("self_hosted_mcd_runner_unconfigured")
            result = self.runner(artifact=scenario_artifact, job=verified["job"], facts=facts)
            emit("execution", "succeeded", result)
            terminal = {**binding, "schema": SELF_HOSTED_MCD_TERMINAL_RECEIPT_SCHEMA, "contract_version": SELF_HOSTED_MCD_CONTRACT_VERSION, "sequence": sequence + 1, "idempotency_key": f"{binding['job_id']}:terminal:{sequence + 1}", "status": "succeeded", "observed_at": _utc(), "started_at": started_at, "completed_at": _utc(), "result": _bounded_result(result), "rollback": "not_attempted"}
            validate_self_hosted_terminal_receipt(terminal, binding=binding)
        except Exception as exc:
            emit("execution", "failed", error={"code": "self_hosted_mcd_execution_failed"})
            terminal = _failed_terminal_receipt(verified, error=exc, sequence=sequence + 1, started_at=started_at, completed=0, total=1)
        self._save(job_id, {"job_digest": hashlib.sha256(canonical_json(dict(job))).hexdigest(), "terminal": terminal, "status": terminal["status"], "sequence": terminal["sequence"]})
        if receipt_sink:
            receipt_sink(terminal)
        return terminal


class SelfHostedPortalWorker:
    """Durable outbound-only v2 Portal poll/verify/execute/receipt loop.

    This worker never binds or listens on a host socket.  Every Portal
    interaction is initiated by the worker through the supplied client.  The
    cursor, pending delivery and transport backoff are persisted before the
    worker returns, so an outage or daemon restart cannot acknowledge a
    command before its receipt has been delivered.
    """

    def __init__(self, client: Any, executor: SelfHostedScenarioExecutor, *, installation_id: str | list[str], portal_keys: Mapping[str, str], publisher_keys: Mapping[str, str], context: Mapping[str, Any], commercial_key_id: str, agent_id: str, host_fingerprint: str, poll_wait_seconds: int = _DEFAULT_LONG_POLL_SECONDS, backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS, backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS, clock: Callable[[], float] | None = None, inventory_provider: Callable[[str], Mapping[str, Any]] | None = None, runtime_command_handler: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None):
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
        self._active_installation_id = self.installation_ids[0]

    def _pending(self) -> list[dict[str, Any]]:
        raw = self.executor.state.get(_PENDING_KEY)
        return [dict(item) for item in raw] if isinstance(raw, list) and all(isinstance(item, Mapping) for item in raw) else []

    def _queue(self, receipt: Mapping[str, Any]) -> None:
        pending = self._pending()
        identity = str(receipt.get("idempotency_key") or "")
        if not any(str(item.get("idempotency_key") or "") == identity for item in pending):
            pending.append(dict(receipt))
        self.executor.state.put(_PENDING_KEY, pending)

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

    def _drain(self) -> bool:
        pending = self._pending()
        while pending:
            try:
                self._send_receipt(pending[0])
            except Exception:
                self._transport_failed("receipt_delivery")
                self.executor.state.put(_PENDING_KEY, pending)
                return False
            self._transport_succeeded()
            pending.pop(0)
            self.executor.state.put(_PENDING_KEY, pending)
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
        revision = int(self.executor.state.get("commercial_standalone_settings_revision") or 0)
        sync = {"desired_state_sync_protocol": 3, "pending_intent": self.executor.state.get("commercial_pending_sync_intent"), "conflict": self.executor.state.get("commercial_runtime_conflict")}
        settings = {"values": dict(values), "revision": revision, "etag": "sha256:" + hashlib.sha256(canonical_json(dict(values))).hexdigest(), "sync": sync}
        binding = {"agent_id": self.agent_id, "installation_id": installation_id, "host_fingerprint": self.host_fingerprint, "commercial_key_id": self.commercial_key_id}
        return {"schema": "portal-mcd-inventory-snapshot-v1", "contract_version": "1.0", "request_id": f"inventory-{int(self._clock() * 1000)}", "binding": binding, "snapshot_revision": str(self.executor.state.get("commercial_inventory_revision") or revision), "observed_at": _utc(), "complete": True, "host": {"mcd_version": str(self.context.get("mcd_version") or "runtime"), "profile": str(self.context.get("profile") or "commercial"), "service_status": "active", "scheduler_paused": bool(self.context.get("scheduler_paused", False)), "settings": settings}, "instances": []}

    def _inventory(self, installation_id: str) -> dict[str, Any]:
        if self.inventory_provider is not None:
            value = self.inventory_provider(installation_id)
            if not isinstance(value, Mapping):
                raise ValueError("commercial_inventory_provider_invalid")
            return dict(value)
        return self._minimal_inventory(installation_id)

    def _ensure_adopted(self, installation_id: str) -> bool:
        if not callable(getattr(self.client, "adopt", None)):
            return True
        adopted = self.executor.state.get(_ADOPTED_KEY)
        adopted = dict(adopted) if isinstance(adopted, Mapping) else {}
        if adopted.get(installation_id):
            return True
        inventory = self._inventory(installation_id)
        signer = getattr(self.client, "_agent_signed", None)
        if callable(signer) and not inventory.get("agent_signature"):
            inventory = signer(inventory)
        try:
            self._outbound("adopt", lambda: self.client.adopt(installation_id=installation_id, inventory=inventory))
        except _PortalTransportError:
            return False
        adopted[installation_id] = {"request_id": inventory.get("request_id"), "snapshot_revision": inventory.get("snapshot_revision"), "adopted_at": _utc()}
        self.executor.state.put(_ADOPTED_KEY, adopted)
        return True

    def _publish_inventory(self, installation_id: str) -> bool:
        if not callable(getattr(self.client, "send_inventory", None)) or self.inventory_provider is None:
            return True
        inventory = self._inventory(installation_id)
        signer = getattr(self.client, "_agent_signed", None)
        if callable(signer) and not inventory.get("agent_signature"):
            inventory = signer(inventory)
        digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
        if self.executor.state.get("commercial_inventory_digest") == digest:
            return True
        try:
            self._outbound("inventory", lambda: self.client.send_inventory(installation_id=installation_id, inventory=inventory))
        except _PortalTransportError:
            return False
        self.executor.state.put("commercial_inventory_digest", digest)
        return True

    @staticmethod
    def _valid_etag(value: object) -> bool:
        text = str(value or "")
        return len(text) == 71 and text.startswith("sha256:") and all(char in "0123456789abcdef" for char in text[7:])

    def _run_noop(self, command: Mapping[str, Any], *, cursor_next: str) -> dict[str, Any]:
        expected = str(command.get("expected_standalone_etag") or "")
        actual = str(self.context.get("standalone_etag") or self.executor.state.get("commercial_standalone_etag") or "")
        if not self._valid_etag(expected) or not self._valid_etag(actual):
            return {"status": "rejected", "reason": "commercial_standalone_etag_unavailable"}
        unchanged = actual == expected
        receipt = {
            "schema": _PORTAL_NOOP_RECEIPT_SCHEMA,
            "contract_version": PORTAL_MCD_ADOPT_CONTRACT_VERSION,
            "command_id": str(command.get("command_id") or ""),
            "request_id": str(command.get("request_id") or ""),
            "idempotency_key": str(command.get("idempotency_key") or ""),
            "binding": dict(command.get("binding") or {}),
            "status": "succeeded" if unchanged else "failed",
            "observed_at": _utc(),
            "standalone": {"before_etag": actual, "after_etag": actual, "unchanged": unchanged},
            "connection_status": "active",
        }
        if not receipt["command_id"] or not receipt["request_id"] or not receipt["idempotency_key"]:
            return {"status": "rejected", "reason": "commercial_noop_command_invalid"}
        if not self._send_or_queue(receipt):
            return {"status": "receipt_pending", "command_id": receipt["command_id"]}
        if self._pending():
            return {"status": "receipt_pending", "command_id": receipt["command_id"]}
        self._finish_command(cursor_next=cursor_next)
        return {"status": receipt["status"], "command_id": receipt["command_id"]}

    def _command_receipt(self, command: Mapping[str, Any], *, status: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        binding = command.get("binding") if isinstance(command.get("binding"), Mapping) else {"agent_id": self.agent_id, "installation_id": self._active_installation_id, "host_fingerprint": self.host_fingerprint, "commercial_key_id": self.commercial_key_id}
        return {"schema": "portal-mcd-command-receipt-v1", "contract_version": "1.0", "command_id": str(command.get("command_id") or command.get("conflict_id") or ""), "request_id": str(command.get("request_id") or command.get("command_id") or ""), "idempotency_key": str(command.get("idempotency_key") or ""), "binding": dict(binding), "status": status if status in {"succeeded", "failed"} else "failed", "payload": dict(payload), "observed_at": _utc()}

    def _run_runtime_command(self, command: Mapping[str, Any], *, cursor_next: str) -> dict[str, Any]:
        if str(command.get("schema") or "") != "mcc-desired-runtime-sync-v3" or int(command.get("desired_state_sync_protocol") or 0) != 3:
            receipt = self._command_receipt(command, status="failed", payload={"reason": "commercial_command_unsupported"})
            if self._send_or_queue(receipt):
                self._finish_command(cursor_next=cursor_next)
            return {"status": "failed", "reason": "commercial_command_unsupported"}
        if not str(command.get("command_id") or "").strip() or not str(command.get("idempotency_key") or "").strip():
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

    def run_once(self) -> dict[str, Any]:
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
            return {"status": "rejected", "reason": str(exc)}
        try:
            bootstrap_pin = verified["bootstrap_artifact"]
            scenario_pin = verified["scenario_artifact"]
            bootstrap = self._outbound("bootstrap_download", lambda: download_external_artifact(bootstrap_pin["artifact_uri"], expected_size=bootstrap_pin["artifact_size_bytes"]))
            scenario = self._outbound("scenario_download", lambda: download_external_artifact(scenario_pin["artifact_uri"], expected_size=scenario_pin["artifact_size_bytes"]))
            terminal = self.executor.run(command, portal_keys=self.portal_keys, publisher_keys=self.publisher_keys, context=self.context, expected_agent_id=self.agent_id, expected_installation_id=str(command["installation_id"]), expected_commercial_key_id=self.commercial_key_id, expected_host_fingerprint=self.host_fingerprint, bootstrap_artifact=bootstrap, scenario_artifact=scenario, receipt_sink=self._send_or_queue)
            if self._pending():
                # Keep the delivery and cursor pending until the next outbound
                # cycle drains every signed receipt in order.  The executor's
                # durable terminal makes the following replay idempotent.
                return {"status": terminal.get("status"), "job_id": str(command["job_id"]), "receipt_pending": True}
            self._finish_command(cursor_next=cursor_next)
            return {"status": terminal.get("status"), "job_id": str(command["job_id"])}
        except _PortalTransportError:
            return {"status": "backoff", "job_id": str(command.get("job_id") or ""), "retry_after": float(self._transport_state().get("retry_after") or 0.0)}
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
