"""Catalog-only runtime and operator lifecycle entry points."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from mcd_agent.install_type import detect_install_type
from mcd_agent.mautic_patch_resolution import resolve_plan, report_evidence, _host_identity
from mcd_agent.mautic_patch_plan_v3 import execute
from mcd_agent.mautic_version_cache import read_mautic_version_evidence_read_only, confirmed_mautic_major


def _save_local_receipt(root: Path, run_id: str, receipt: dict[str, Any]) -> Path:
    directory = root / ".mcd" / "patch-plan-v3"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{run_id}.evidence.json"
    fd, temporary = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def run(config: Any, install: Any, *, phase: str, operation: str = "apply",
        trigger: str = "daemon_reconcile", run_id: str | None = None) -> dict[str, Any]:
    root = str(install.root)
    if config is None:
        return {"status": "error", "root": root, "reason": "catalog_resolution_required"}
    if operation == "rollback" and not run_id:
        return {"status": "error", "root": root, "reason": "rollback_run_id_required"}
    started = datetime.now(timezone.utc).isoformat()
    try:
        if operation == "rollback":
            from mcd_agent.mautic_patch_stage import application_root
            from mcd_agent.mautic_patch_plan_v3 import _load_ledger
            ledger_path = application_root(root) / ".mcd/patch-plan-v3" / (str(run_id) + ".json")
            if ledger_path.exists():
                ledger = _load_ledger(ledger_path)
                if ledger.get("phase") != phase or ledger.get("trigger") not in {"daemon_reconcile", "operator_action", "plugin_reload_failure"}:
                    raise RuntimeError("rollback_phase_context_mismatch")
                trigger = ledger["trigger"]
        version = read_mautic_version_evidence_read_only(root).get("version")
        major = confirmed_mautic_major(root, getattr(config, "php_bin", "php"),
                                      console_path=getattr(install, "console_path", None),
                                      local_php_path=getattr(install, "local_php_path", None),
                                      run_as_user=getattr(config, "mautic_run_as_user", "www-data"))
        if major == 6 and phase in {"before_plugin_reload", "legacy_runtime_plugin_repair"} and operation == "apply":
            if getattr(config, "mautic6_core_patch_policy", "required") != "required":
                return {"status": "skip", "root": root, "reason": "policy_off"}
            if version is None and not getattr(config, "mautic6_core_patch_apply_if_version_unknown", False):
                return {"status": "skip", "root": root, "reason": "unknown_version_policy_off"}
        install_type = detect_install_type(root)
        pre_reload = trigger == "daemon_reconcile" and phase == "before_plugin_reload"
        public_source_configured = bool(
            str(getattr(config, "plugins_repo_base_url", "") or "").strip()
            and str(getattr(config, "plugins_manifest_path_template", "") or "").strip()
        )
        public_pre_reload = pre_reload and public_source_configured
        if public_pre_reload:
            from mcd_agent.mautic_patch_public_catalog import resolve_public_pre_reload_plan
            resolved = resolve_public_pre_reload_plan(
                config,
                install,
                trigger=trigger,
                phase=phase,
                operation=operation,
                observed_version=version,
                observed_major=major,
                install_type=install_type,
                run_id=run_id,
            )
        else:
            if pre_reload and not getattr(config, "mcc_url", None):
                return {"status": "skip", "root": root, "reason": "public_patch_catalog_not_configured"}
            if not getattr(config, "mcc_url", None):
                return {"status": "error", "root": root, "reason": "catalog_resolution_required"}
            resolved = resolve_plan(config, install, trigger=trigger, phase=phase,
                                    operation=operation, observed_version=version, observed_major=major,
                                    install_type=install_type, run_id=run_id)
        if resolved["status"] != "selected":
            return {"status": "skip" if resolved["status"] == "noop" else "error",
                    "root": root, "reason": resolved.get("reason", "catalog_not_selected")}
        plan = resolved["plan"]
        from mcd_agent.mautic_patch_stage import application_root
        from mcd_agent.mautic_patch_fact_binding import needs_facts, bound_provider
        app_root = application_root(root)
        provider = bound_provider(config, root) if needs_facts(plan) else None
        admission = execute(str(app_root), dict(plan, operation="verify"), observed_major=major,
                            facts_provider=provider) if provider is not None and operation == "apply" else {}
        result = execute(str(app_root), plan, observed_major=major, facts_provider=provider,
                         accepted_facts=admission.get("facts_receipt"))
        metadata_warnings: list[str] = []
        try:
            host_name, hostname = _host_identity(config)
        except Exception as exc:
            host_name, hostname = "", ""
            metadata_warnings.append("host_identity:" + type(exc).__name__)
        evidence = {"schema": "mcd-mautic-patch-evidence-v1", "host_id": resolved.get("host_id", ""),
                    "instance_uid": install.instance_uid, "catalog_revision": resolved["catalog_revision"],
                    "catalog_sha256": resolved["catalog_sha256"], "registry_commit": plan["registry_commit"],
                    "registry_sha256": plan["registry_sha256"], "plan_sha256": resolved["plan_sha256"],
                    "trigger": trigger, "phase": phase, "run_id": plan["run_id"], "operation": operation,
                    "started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(),
                    "status": result["status"], "selected": result.get("selected", []),
                    "records": [{"id": row["id"], "execution_kind": next(record["execution_kind"] for record in plan["patches"] if record["id"] == row["id"]),
                                 "decision": row["decision"], "gates": row.get("gates", []),
                                 "before_sha256": row.get("before_sha256"), "after_sha256": row.get("after_sha256")}
                                for row in result.get("patches", [])],
                    "rollback": {"available": bool(result.get("rollback_available")),
                                 "attempted": bool(result.get("rollback_attempted")),
                                 "succeeded": bool(result.get("rollback_succeeded"))}}
        from mcd_agent import __version__
        evidence["agent_version"] = __version__
        if result.get("facts_receipt") is not None:
            evidence["facts_receipt"] = result["facts_receipt"]
        receipt = {
            "schema": "mcd-mautic-patch-local-receipt-v1",
            "status": result["status"],
            "plan_sha256": resolved["plan_sha256"],
            "evidence": evidence,
            "management_delivery": {"status": "not_configured"},
        }
        receipt_path: Path | None = None
        receipt_persistence: dict[str, Any] = {"status": "saved"}
        try:
            receipt_path = _save_local_receipt(app_root, str(plan["run_id"]), receipt)
        except Exception as exc:
            logging.error("[%s] patch execution succeeded but local receipt persistence failed: %s", root, exc)
            receipt_persistence = {"status": "failed", "reason": type(exc).__name__, "execution_completed": True}
        delivery: dict[str, Any] = {"status": "not_configured"}
        if getattr(config, "mcc_url", None) and getattr(config, "mcc_token", None):
            try:
                accepted = report_evidence(
                    config,
                    instance_uid=install.instance_uid,
                    mcc_host_name=host_name,
                    hostname=hostname,
                    host_id=str(resolved.get("host_id", "") or ""),
                    catalog_revision=resolved["catalog_revision"],
                    catalog_sha256=resolved["catalog_sha256"],
                    plan_sha256=resolved["plan_sha256"],
                    trigger=trigger,
                    phase=phase,
                    run_id=plan["run_id"],
                    evidence=evidence,
                )
                delivery = {"status": "accepted", "evidence_id": accepted.get("evidence_id")}
            except Exception as exc:
                logging.warning("[%s] patch evidence delivery deferred: %s", root, exc)
                delivery = {"status": "deferred", "reason": type(exc).__name__}
            receipt["management_delivery"] = delivery
            if receipt_path is not None:
                try:
                    _save_local_receipt(app_root, str(plan["run_id"]), receipt)
                except Exception as exc:
                    logging.error("[%s] patch delivery status persistence failed after completed execution: %s", root, exc)
                    metadata_warnings.append("delivery_receipt_update:" + type(exc).__name__)
        return dict(
            result,
            root=root,
            evidence=evidence,
            receipt_path=str(receipt_path) if receipt_path is not None else "",
            receipt_persistence=receipt_persistence,
            management_delivery=delivery,
            metadata_warnings=metadata_warnings,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "error", "root": root, "reason": str(exc)}


def reconcile(config: Any, installs: list[Any], *, phase: str) -> list[dict[str, Any]]:
    pause = Path(getattr(config, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause"))
    results = []
    for install in installs:
        if pause.exists():
            break
        results.append(run(config, install, phase=phase))
    return results
