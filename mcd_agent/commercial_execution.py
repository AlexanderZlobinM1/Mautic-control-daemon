from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from mcd_agent.commercial_portal import CONTRACT_VERSION, CommercialState, canonical_json, verify_mcc_artifact, verify_signed_job


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    def run(self, job: Mapping[str, Any], *, portal_keys: Mapping[str, str], publisher_keys: Mapping[str, str], artifact: bytes, context: dict[str, Any] | None = None, mcd_contract: str = "1.5.200", capabilities: set[str] | None = None) -> dict[str, Any]:
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
        available = set(capabilities or set())
        required = scenario.get("required_capabilities", [])
        if not isinstance(required, list) or any(not isinstance(item, str) or item not in available for item in required):
            raise ValueError("commercial_scenario_capability_rejected")
        matrix = scenario.get("compatibility", {})
        runtime_context = context or {}
        if not isinstance(matrix, dict):
            raise ValueError("commercial_scenario_compatibility_rejected")
        for key in ("platform", "runtime"):
            allowed = matrix.get(key)
            if allowed and str(runtime_context.get(key) or "") not in {str(value) for value in allowed}:
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
                self._save(job_id, {"completed": completed, "results": results, "status": "running"})
        except Exception:
            for stage_id in reversed(completed):
                rollback = self.rollback_handlers.get(stage_id)
                if rollback:
                    rollback({"stage_id": stage_id, "context": ctx})
            self._save(job_id, {"completed": completed, "results": results, "status": "failed"})
            raise
        payload = {"schema": "portal-mcd-receipt-v1", "contract_version": CONTRACT_VERSION, "job_id": job_id, "correlation_id": job_id, "idempotency_key": str(job["job"]["idempotency_key"]), "installation_id": str(job["job"]["installation_id"]), "sequence": len(results), "status": "succeeded", "progress": {"completed": len(results), "total": len(scenario["stages"]), "message": "Scenario complete"}, "artifact": {"revision": str(package["revision"]), "sha256": str(package["sha256"]), "size_bytes": int(package["size_bytes"]), "media_type": str(package["media_type"])}, "started_at": _utc(), "completed_at": _utc(), "result": {"executor": "mcd-commercial-scenario", "stages": results}, "error": None}
        self._save(job_id, {"completed": completed, "results": results, "status": "succeeded"})
        return payload

    def _load(self, job_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.state.path) as db:
            row = db.execute("SELECT payload FROM commercial_scenario_runs WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None
