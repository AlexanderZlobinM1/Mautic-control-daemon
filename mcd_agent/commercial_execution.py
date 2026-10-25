from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from mcd_agent.commercial_portal import CONTRACT_VERSION, CommercialState, canonical_json, verify_mcc_artifact, verify_signed_job
from mcd_agent.commercial_onboarding import validate_onboarding_artifact
from mcd_agent.commercial_detector import validate_scenario_detector
from mcd_agent.commercial_bootstrap_precheck import validate_portal_preflight_receipt


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_onboarding_stage_receipt(receipt: Mapping[str, Any], *, job_id: str, stage_id: str, sequence: int) -> None:
    if receipt.get("schema") != "mcd-onboarding-stage-receipt-v1":
        raise ValueError("commercial_stage_receipt_schema_invalid")
    if receipt.get("contract_version") != CONTRACT_VERSION or (receipt.get("request_id") or receipt.get("job_id")) != job_id or receipt.get("stage_id") != stage_id:
        raise ValueError("commercial_stage_receipt_binding_invalid")
    if int(receipt.get("sequence", -1)) != int(sequence) or receipt.get("status") not in {"passed", "failed", "running", "waiting", "succeeded"} or not receipt.get("idempotency_key"):
        raise ValueError("commercial_stage_receipt_fields_invalid")


def validate_onboarding_terminal_receipt(receipt: Mapping[str, Any], *, job_id: str) -> None:
    if receipt.get("schema") != "mcd-onboarding-terminal-receipt-v1" or receipt.get("contract_version") != CONTRACT_VERSION or (receipt.get("request_id") or receipt.get("job_id")) != job_id or not receipt.get("idempotency_key"):
        raise ValueError("commercial_terminal_receipt_invalid")
    if receipt.get("status") not in {"succeeded", "failed", "rejected", "expired", "cancelled"}:
        raise ValueError("commercial_terminal_receipt_status_invalid")


class CommercialScenarioExecutor:
    """Generic scenario bridge; concrete stage behavior is injected by MCD."""

    def __init__(self, state: CommercialState, *, stage_handlers: Mapping[str, Callable[[dict[str, Any]], Any]], rollback_handlers: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None):
        self.state = state
        self.stage_handlers = dict(stage_handlers)
        self.rollback_handlers = dict(rollback_handlers or {})
        with sqlite3.connect(self.state.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS commercial_scenario_runs (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def _save(self, job_id: str, payload: dict[str, Any]) -> None:
        with sqlite3.connect(self.state.path) as db:
            db.execute("INSERT INTO commercial_scenario_runs(job_id,payload) VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload", (job_id, json.dumps(payload, sort_keys=True)))

    def run(self, job: Mapping[str, Any], *, portal_keys: Mapping[str, str], publisher_keys: Mapping[str, str], artifact: bytes, context: dict[str, Any] | None = None, mcd_contract: str = "1.5.200", capabilities: set[str] | None = None, receipt_sink: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        verified = verify_signed_job(job, portal_keys)
        package = job["job"]["package"]
        compatibility = verified["applicability"].get("mcd_contract")
        if compatibility and not str(compatibility).startswith(">=1."):
            raise ValueError("commercial_scenario_compatibility_rejected")
        verify_mcc_artifact(job, artifact, publisher_keys)
        try:
            scenario = json.loads(artifact.decode("utf-8"))
        except Exception as exc:
            raise ValueError("commercial_scenario_schema_invalid") from exc
        if scenario.get("schema") not in {"portal-mcd-scenario-v1", "mcc-self-hosted-onboarding-scenario-v1"} or (scenario.get("schema") == "portal-mcd-scenario-v1" and scenario.get("contract_version") != CONTRACT_VERSION) or not isinstance(scenario.get("stages"), list):
            raise ValueError("commercial_scenario_schema_invalid")
        if scenario.get("schema") == "mcc-self-hosted-onboarding-scenario-v1":
            validate_onboarding_artifact(scenario)
        if scenario.get("detector"):
            facts = (context or {}).get("local_facts")
            if not isinstance(facts, dict):
                raise ValueError("commercial_detector_facts_missing")
            validate_scenario_detector(scenario, facts, detector_id=str(scenario["detector"].get("id") or ""), detector_revision=str(scenario["detector"].get("revision") or ""))
        matrix = scenario.get("compatibility", {})
        if not isinstance(matrix, dict):
            raise ValueError("commercial_scenario_compatibility_rejected")
        available = set(capabilities or set())
        required = scenario.get("required_capabilities", matrix.get("mcd_required_capabilities", []))
        if not isinstance(required, list) or any(not isinstance(item, str) or item not in available for item in required):
            raise ValueError("commercial_scenario_capability_rejected")
        runtime_context = context or {}
        for key in ("platform", "runtime"):
            allowed = matrix.get(key)
            if allowed and str(runtime_context.get(key) or "") not in {str(value) for value in allowed}:
                raise ValueError("commercial_scenario_compatibility_rejected")
        allowed_platforms = matrix.get("platforms")
        if allowed_platforms and str(runtime_context.get("platform") or "") not in {str(value) for value in allowed_platforms}:
            raise ValueError("commercial_scenario_compatibility_rejected")
        job_id = str(job["job"]["job_id"])
        prior = self._load(job_id)
        completed = list(prior.get("completed", [])) if prior else []
        results: list[Any] = list(prior.get("results", [])) if prior else []
        ctx = dict(context or {})
        ctx["mcd_contract"] = mcd_contract
        try:
            for stage in scenario["stages"]:
                if not isinstance(stage, dict) or not (stage.get("stage_id") or stage.get("id")) or not (stage.get("handler") or stage.get("kind")):
                    raise ValueError("commercial_scenario_stage_invalid")
                stage_capabilities = stage.get("requires_capabilities", [])
                if not isinstance(stage_capabilities, list) or any(not isinstance(item, str) or item not in available for item in stage_capabilities):
                    raise ValueError("commercial_scenario_capability_rejected")
                stage_id, handler_name = str(stage.get("stage_id") or stage.get("id")), str(stage.get("handler") or stage.get("kind"))
                if stage_id in completed:
                    continue
                handler = self.stage_handlers.get(handler_name)
                if handler is None:
                    raise ValueError("commercial_scenario_unknown_stage")
                result = handler({"stage": stage, "context": ctx})
                completed.append(stage_id)
                results.append({"stage_id": stage_id, "result": result})
                if receipt_sink:
                    stage_receipt = {"schema": "mcd-onboarding-stage-receipt-v1", "contract_version": CONTRACT_VERSION, "request_id": job_id, "job_id": job_id, "stage_id": stage_id, "sequence": len(results), "idempotency_key": f"{job_id}:stage:{len(results)}", "status": "passed", "result": result}
                    validate_onboarding_stage_receipt(stage_receipt, job_id=job_id, stage_id=stage_id, sequence=len(results))
                    receipt_sink(stage_receipt)
                self._save(job_id, {"completed": completed, "results": results, "status": "running"})
        except Exception:
            for stage_id in reversed(completed):
                rollback = self.rollback_handlers.get(stage_id)
                if rollback:
                    rollback({"stage_id": stage_id, "context": ctx})
            self._save(job_id, {"completed": completed, "results": results, "status": "failed"})
            raise
        payload = {"schema": "mcd-onboarding-terminal-receipt-v1", "contract_version": CONTRACT_VERSION, "request_id": job_id, "job_id": job_id, "correlation_id": job_id, "idempotency_key": str(job["job"]["idempotency_key"]), "installation_id": str(job["job"]["installation_id"]), "sequence": len(results), "status": "succeeded", "progress": {"completed": len(results), "total": len(scenario["stages"]), "message": "Scenario complete"}, "artifact": {"revision": str(package["revision"]), "sha256": str(package["sha256"]), "size_bytes": int(package["size_bytes"]), "media_type": str(package["media_type"])}, "started_at": _utc(), "completed_at": _utc(), "result": {"executor": "mcd-commercial-scenario", "stages": results}, "error": None}
        self._save(job_id, {"completed": completed, "results": results, "status": "succeeded"})
        return payload

    def _load(self, job_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.state.path) as db:
            row = db.execute("SELECT payload FROM commercial_scenario_runs WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None


class CommercialPortalWorker:
    """One non-blocking commercial poll/execute/receipt cycle."""

    def __init__(self, client: Any, executor: CommercialScenarioExecutor, *, installation_id: str | list[str], portal_keys: Mapping[str, str], publisher_keys: Mapping[str, str], context: dict[str, Any], capabilities: set[str], preflight_required: bool = False):
        self.client, self.executor = client, executor
        self.installation_ids = [installation_id] if isinstance(installation_id, str) else [str(value) for value in installation_id]
        self.installation_ids = [value for value in self.installation_ids if value]
        if not self.installation_ids:
            raise ValueError("commercial_installation_identity_missing")
        self.portal_keys, self.publisher_keys = dict(portal_keys), dict(publisher_keys)
        self.context, self.capabilities = dict(context), set(capabilities)
        self.preflight_required = bool(preflight_required)

    def run_once(self) -> dict[str, Any]:
        pending = self.executor.state.get("commercial_pending_receipt")
        if isinstance(pending, dict):
            self.client.send_receipt(pending)
            self.executor.state.put("commercial_pending_receipt", None)
        cursor = str(self.executor.state.get("commercial_portal_cursor") or "")
        delivery = None
        active_installation_id = self.installation_ids[0]
        for active_installation_id in self.installation_ids:
            delivery = self.client.pull_job(installation_id=active_installation_id, cursor=cursor, wait_seconds=0, limit=1)
            if delivery:
                break
        if not delivery:
            return {"status": "idle"}
        job = delivery.get("job")
        if not isinstance(job, dict):
            raise ValueError("commercial_job_delivery_missing")
        package = job.get("package") if isinstance(job.get("package"), dict) else {}
        download = package.get("download") if isinstance(package.get("download"), dict) else {}
        try:
            verified = verify_signed_job(delivery, self.portal_keys)
            preflight = job.get("preflight_receipt")
            if self.preflight_required and not isinstance(preflight, dict):
                raise ValueError("portal_preflight_receipt_missing")
            if isinstance(preflight, dict):
                tuple_key = str(job.get("preflight_tuple_key") or job.get("idempotency_key") or "")
                host_fingerprint = str(job.get("preflight_host_fingerprint") or self.context.get("host_fingerprint") or "")
                receipt_meta = validate_portal_preflight_receipt(preflight, portal_keys=self.portal_keys, tuple_key=tuple_key, host_fingerprint=host_fingerprint, request_id=str(job.get("job_id") or ""))
                replay_key = str(job.get("job_id") or "") + ":" + str(preflight.get("checked_at") or "")
                used = self.executor.state.get("commercial_preflight_receipts") or {}
                if replay_key in used:
                    raise ValueError("portal_preflight_receipt_replay")
                used[replay_key] = receipt_meta
                self.executor.state.put("commercial_preflight_receipts", used)
            artifact = self.client.download_artifact(path=str(download["path"]), grant=str(download["grant"]), expected_size=verified["size_bytes"])
            self.client.ack_job(job_id=str(job["job_id"]), installation_id=active_installation_id, status="accepted", reason=None, idempotency_key=str(job["idempotency_key"]))
            terminal = self.executor.run(delivery, portal_keys=self.portal_keys, publisher_keys=self.publisher_keys, artifact=artifact, context=self.context, capabilities=self.capabilities, receipt_sink=self.client.send_receipt)
            terminal["schema"] = "mcd-onboarding-terminal-receipt-v1"
            validate_onboarding_terminal_receipt(terminal, job_id=str(job["job_id"]))
            try:
                self.client.send_receipt(terminal)
            except Exception:
                self.executor.state.put("commercial_pending_receipt", terminal)
                return {"status": "receipt_pending", "job_id": str(job["job_id"])}
            self.executor.state.put("commercial_portal_cursor", delivery.get("cursor", cursor))
            return {"status": "succeeded", "job_id": str(job["job_id"])}
        except Exception as exc:
            self.client.ack_job(job_id=str(job.get("job_id") or ""), installation_id=active_installation_id, status="rejected", reason=str(exc), idempotency_key=str(job.get("idempotency_key") or ""))
            return {"status": "rejected", "reason": str(exc)}

    def run_forever(self, stop_event: Any, *, interval_sec: float = 1.0) -> None:
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                # Portal outage or a malformed delivery must not terminate MCD.
                pass
            stop_event.wait(max(0.1, float(interval_sec)))
