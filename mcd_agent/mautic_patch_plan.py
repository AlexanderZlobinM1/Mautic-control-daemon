"""Generic catalog-plan dispatch; the embedded static v1 catalog is retired."""
from __future__ import annotations
import json
from pathlib import Path
import re
from typing import Any

PLAN_SCHEMA = "mcd-mautic-patch-plan-v3"
PLAN_V2_SCHEMA = "mcd-mautic-patch-plan-v2"
PREFLIGHT_SCHEMA = "mcd-mautic-patch-preflight-v1"
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class PatchPlanError(RuntimeError):
    pass


def contract() -> dict[str, Any]:
    from mcd_agent.mautic_patch_resolution import _contract
    from mcd_agent.mautic_patch_plan_v2 import capability
    result = dict(_contract())
    result.update(capabilities=[PREFLIGHT_SCHEMA, "mautic-patch-plan-v2", capability()["evidence_schema"], "mautic-patch-plan-v3"], patch_plan_v2=capability())
    return result


def parse_plan(raw: str) -> dict[str, Any]:
    try:
        plan = json.loads(raw)
        if isinstance(plan, dict) and plan.get("schema") == PLAN_V2_SCHEMA:
            from mcd_agent.mautic_patch_plan_v2 import parse_plan as parse_v2
            return parse_v2(raw)
        if isinstance(plan, dict) and plan.get("schema") == PLAN_SCHEMA:
            from mcd_agent.mautic_patch_plan_v3 import _validate_plan
            _validate_plan(plan)
            return plan
        raise PatchPlanError("static_or_unknown_patch_catalog_rejected")
    except (TypeError, ValueError, RuntimeError) as exc:
        raise PatchPlanError(str(exc) or "invalid_plan_json") from exc


def validate_upgrade_plan(raw: str, source_version: str, target_version: str, install_type: str) -> dict[str, Any]:
    plan = parse_plan(raw)
    for field, expected in (("source_version", source_version), ("target_version", target_version), ("install_type", install_type)):
        if plan[field] != expected:
            raise PatchPlanError(field + "_mismatch")
    return plan


def rejected_preflight(run_id: str | None, reason: str) -> dict[str, Any]:
    return {"schema": PREFLIGHT_SCHEMA, "operation": "patch_preflight", "run_id": run_id or "", "plan_sha256": None,
            "snapshot_id": None, "resolved_source_root": None, "upgrade_started": False,
            "selected": [], "applied": [], "status": "error", "reason": reason, "phases": [], "verification": {},
            "rollback_attempted": False, "rollback_succeeded": False, "hard_incident": False,
            "rollback_reason": None, "pre_patch_hashes": {}, "post_patch_hashes": {}, "restore_hashes": {}, "restored": []}


def _target_version(root: Path) -> str | None:
    lock_versions: set[str] = set()
    metadata_versions: set[str] = set()
    for prefix in ("", "docroot/", "public/"):
        lock = root / (prefix + "composer.lock")
        metadata_paths = (
            root / (prefix + "app/release_metadata.json"),
            root / (prefix + "app/bundles/CoreBundle/release_metadata.json"),
        )
        try:
            if lock.is_file():
                for package in json.loads(lock.read_text(encoding="utf-8"))["packages"]:
                    if package.get("name") in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
                        version = str(package.get("version", "")).strip().removeprefix("v")
                        if version:
                            lock_versions.add(version)
            for metadata in metadata_paths:
                if metadata.is_file():
                    version = str(json.loads(metadata.read_text(encoding="utf-8"))["version"]).strip().removeprefix("v")
                    if version:
                        metadata_versions.add(version)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise PatchPlanError("invalid_version_metadata") from exc

    # Official ZIP releases carry the authoritative version in the root
    # app/release_metadata.json. Composer's 6.0.0-dev package marker is a
    # build-time placeholder and must not make an otherwise exact ZIP look
    # inconsistent. Stable lock/metadata conflicts remain fail-closed.
    if metadata_versions:
        if len(metadata_versions) != 1:
            return None
        authoritative = next(iter(metadata_versions))
        stable_lock_versions = {
            version
            for version in lock_versions
            if not re.search(r"(?i)(?:^|[-@])dev(?:$|[-.])", version)
        }
        if stable_lock_versions and stable_lock_versions != {authoritative}:
            return None
        return authoritative
    return lock_versions.pop() if len(lock_versions) == 1 else None


def _dispatch(root_value: str, raw_plan: str, run_id: str, operation: str, phase: str | None = None, **facts_options):
    plan = parse_plan(raw_plan)
    if plan["run_id"] != run_id:
        raise PatchPlanError("run_id_argument_mismatch")
    try:
        if plan["schema"] == PLAN_V2_SCHEMA:
            from mcd_agent import mautic_patch_plan_v2 as engine
            if operation == "patch_preflight":
                return engine.atomic_preflight(root_value, raw_plan)
            if operation == "rollback":
                return engine.rollback(root_value, raw_plan)
            if plan["operation"] != operation or plan["phase"] != phase:
                raise PatchPlanError("v2_invocation_argument_mismatch")
            return engine.execute(root_value, raw_plan)
        from mcd_agent import mautic_patch_plan_v3 as engine
        if operation == "patch_preflight":
            return engine.atomic_preflight(root_value, plan, **facts_options)
        if plan["operation"] != operation:
            raise PatchPlanError("v3_invocation_argument_mismatch")
        return engine.execute(root_value, plan, phase=phase, **facts_options)
    except RuntimeError as exc:
        raise PatchPlanError(str(exc)) from exc


def atomic_preflight(root_value: str, raw_plan: str, run_id: str, **facts_options):
    return _dispatch(root_value, raw_plan, run_id, "patch_preflight", **facts_options)


def rollback(root_value: str, raw_plan: str, run_id: str, **facts_options):
    return _dispatch(root_value, raw_plan, run_id, "rollback", **facts_options)


def execute(root_value: str, raw_plan: str, phase: str, run_id: str, operation: str = "apply", **facts_options):
    return _dispatch(root_value, raw_plan, run_id, operation, phase or None, **facts_options)
