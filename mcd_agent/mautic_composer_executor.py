"""Typed external Composer-upgrade executor for the MCD scenario boundary.

The external scenario supplies an immutable, typed step plan.  MCD owns the
host admission fence, command construction, task drain, backup, patch-plan
execution, terminal receipt and rollback.  The request never carries shell,
argv, sudo or arbitrary SQL.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from mcd_agent import mautic_upgrade as upgrade
from mcd_agent.config import AgentConfig


SCHEMA = "mcd-mautic-composer-executor-v1"
CONTRACT_VERSION = "1.0"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_PHASES = frozenset(
    {
        "post_source_install",
        "before_campaign_trigger",
        "before_campaign_execution",
        "before_background_imports",
        "before_cache_warmup",
        "before_asset_generation",
        "preflight_frontend_assets",
        "before_doctrine_migrations",
    }
)
REQUIRED_STEPS = (
    "composer_json_update",
    "composer_update",
    "cache_clear",
    "mautic_finish",
    "doctrine_migrate",
    "verify",
)
FORBIDDEN_WORDS = (
    "shell",
    "script",
    "argv",
    "executable",
    "sudo",
    "password",
    "token",
    "secret",
    "private_key",
    "credential",
    "arbitrary_sql",
)


class ComposerExecutorError(RuntimeError):
    pass


def contract() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "command": "mcd-cli mautic-composer-executor apply --request-file REQUEST --request-sha256 SHA256 --json",
        "execution": "one MCD process holds the exclusive dispatch fence for the complete request",
        "request": {
            "required": [
                "schema",
                "contract_version",
                "run_id",
                "instance_uid",
                "root",
                "source_version",
                "target_version",
                "steps",
                "patch_plan",
                "backup",
                "cron_policy",
            ],
            "steps": {
                "required": list(REQUIRED_STEPS),
                "allowed": [*REQUIRED_STEPS, "patch_phase"],
                "order": "composer_json_update, composer_update, patch phases, cache_clear, patch phases, mautic_finish, doctrine_migrate, verify",
            },
            "patch_plan": {
                "required": ["phases"],
                "when_nonempty": ["plan_file", "plan_sha256", "run_id"],
                "phases": sorted(SAFE_PHASES),
            },
            "backup": {"required": True, "implementation": "MCD-owned immutable pre-mutation archive"},
            "cron_policy": "unchanged",
        },
        "prohibited_request_fields": list(FORBIDDEN_WORDS),
        "receipt": {
            "schema": "mcd-mautic-composer-executor-receipt-v1",
            "terminal_status": ["success", "error", "rolled_back", "rollback_failed"],
            "required": [
                "run_id",
                "instance_uid",
                "root",
                "source_version",
                "target_version",
                "steps",
                "maintenance_guard",
                "rollback",
            ],
        },
        "failure_policy": "fail closed; restore the MCD-owned backup and patch ledger when mutation has started",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _reject_forbidden(value: Any, path: str = "request") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(word in key_text for word in FORBIDDEN_WORDS):
                raise ComposerExecutorError(f"forbidden_request_field:{path}.{key}")
            _reject_forbidden(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden(item, f"{path}[{index}]")


def _require_string(value: Any, name: str, *, safe_id: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComposerExecutorError(f"request_field_invalid:{name}")
    result = value.strip()
    if safe_id and not SAFE_ID.fullmatch(result):
        raise ComposerExecutorError(f"request_id_invalid:{name}")
    return result


def _parse_version(value: Any, name: str) -> tuple[int, int, int]:
    text = _require_string(value, name)
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", text)
    if not match:
        raise ComposerExecutorError(f"version_invalid:{name}")
    return tuple(int(match.group(index)) for index in range(1, 4))  # type: ignore[return-value]


def _validate_steps(steps: Any, patch: Mapping[str, Any]) -> None:
    if not isinstance(steps, list) or not steps:
        raise ComposerExecutorError("steps_invalid")
    names: list[str] = []
    phase_rows: list[tuple[int, str]] = []
    for index, row in enumerate(steps):
        if isinstance(row, str):
            kind = row
            item: Mapping[str, Any] = {}
        elif isinstance(row, Mapping):
            kind = _require_string(row.get("kind"), f"steps[{index}].kind")
            item = row
        else:
            raise ComposerExecutorError(f"step_invalid:{index}")
        if kind not in (*REQUIRED_STEPS, "patch_phase"):
            raise ComposerExecutorError(f"step_kind_unsupported:{kind}")
        names.append(kind)
        if kind == "patch_phase":
            phase = _require_string(item.get("phase"), f"steps[{index}].phase")
            if phase not in SAFE_PHASES:
                raise ComposerExecutorError(f"patch_phase_unsupported:{phase}")
            phase_rows.append((index, phase))
    for required in REQUIRED_STEPS:
        if names.count(required) != 1:
            raise ComposerExecutorError(f"step_count_invalid:{required}")
    if any(names.index(REQUIRED_STEPS[index]) > names.index(REQUIRED_STEPS[index + 1]) for index in range(len(REQUIRED_STEPS) - 1)):
        raise ComposerExecutorError("step_order_invalid")
    phases = patch.get("phases")
    if not isinstance(phases, list) or any(not isinstance(item, str) or item not in SAFE_PHASES for item in phases):
        raise ComposerExecutorError("patch_phases_invalid")
    declared = [phase for _, phase in phase_rows]
    if declared != phases or len(set(declared)) != len(declared):
        raise ComposerExecutorError("patch_phase_binding_invalid")
    composer_index = names.index("composer_update")
    cache_index = names.index("cache_clear")
    finish_index = names.index("mautic_finish")
    migration_index = names.index("doctrine_migrate")
    verify_index = names.index("verify")
    for index, phase in phase_rows:
        if phase in {"post_source_install", "before_campaign_trigger", "before_campaign_execution", "before_background_imports"}:
            if not composer_index < index < cache_index:
                raise ComposerExecutorError(f"patch_phase_position_invalid:{phase}")
        else:
            if not cache_index < index < finish_index:
                raise ComposerExecutorError(f"patch_phase_position_invalid:{phase}")
    if not cache_index < finish_index < migration_index < verify_index:
        raise ComposerExecutorError("step_order_invalid")


def validate_request(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ComposerExecutorError("request_not_object")
    request = dict(raw)
    _reject_forbidden(request)
    if request.get("schema") != SCHEMA or request.get("contract_version") != CONTRACT_VERSION:
        raise ComposerExecutorError("contract_invalid")
    run_id = _require_string(request.get("run_id"), "run_id", safe_id=True)
    instance_uid = _require_string(request.get("instance_uid"), "instance_uid", safe_id=True)
    root = _require_string(request.get("root"), "root")
    if not Path(root).is_absolute() or "\x00" in root:
        raise ComposerExecutorError("root_invalid")
    source_version = _require_string(request.get("source_version"), "source_version")
    target_version = _require_string(request.get("target_version"), "target_version")
    if _parse_version(target_version, "target_version") <= _parse_version(source_version, "source_version"):
        raise ComposerExecutorError("target_not_newer")
    patch = request.get("patch_plan")
    if not isinstance(patch, Mapping):
        raise ComposerExecutorError("patch_plan_invalid")
    phases = patch.get("phases")
    if not isinstance(phases, list) or any(not isinstance(item, str) for item in phases):
        raise ComposerExecutorError("patch_plan.phases_invalid")
    if phases:
        plan_file = _require_string(patch.get("plan_file"), "patch_plan.plan_file")
        if not Path(plan_file).is_absolute() or not Path(plan_file).is_file() or Path(plan_file).is_symlink():
            raise ComposerExecutorError("patch_plan_file_invalid")
        plan_sha256 = _require_string(patch.get("plan_sha256"), "patch_plan.plan_sha256")
        if not _is_digest(plan_sha256):
            raise ComposerExecutorError("patch_plan_sha256_invalid")
        patch_run_id = _require_string(patch.get("run_id"), "patch_plan.run_id", safe_id=True)
    else:
        plan_file = ""
        plan_sha256 = ""
        patch_run_id = ""
    backup = request.get("backup")
    if not isinstance(backup, Mapping) or backup.get("required") is not True:
        raise ComposerExecutorError("backup_required")
    if request.get("cron_policy") != "unchanged":
        raise ComposerExecutorError("cron_policy_invalid")
    _validate_steps(request.get("steps"), patch)
    normalized = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "instance_uid": instance_uid,
        "root": root,
        "source_version": source_version,
        "target_version": target_version,
        "steps": request["steps"],
        "patch_plan": {
            "plan_file": plan_file,
            "plan_sha256": plan_sha256,
            "run_id": patch_run_id,
            "phases": list(phases),
        },
        "backup": {"required": True},
        "cron_policy": "unchanged",
    }
    evidence = request.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, Mapping):
            raise ComposerExecutorError("evidence_invalid")
        normalized["evidence"] = {
            str(key): _require_string(value, f"evidence.{key}", safe_id=True)
            for key, value in evidence.items()
        }
    return normalized


def load_request_file(path: str, expected_sha256: str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file() or candidate.is_symlink():
        raise ComposerExecutorError("request_file_invalid")
    stat = candidate.stat()
    if stat.st_mode & 0o077:
        raise ComposerExecutorError("request_file_permissions_invalid")
    raw_bytes = candidate.read_bytes()
    if len(raw_bytes) > MAX_REQUEST_BYTES or not _is_digest(expected_sha256) or _digest_bytes(raw_bytes) != expected_sha256:
        raise ComposerExecutorError("request_file_digest_invalid")
    try:
        value = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComposerExecutorError("request_json_invalid") from exc
    return validate_request(value)


def _receipt_path(config: AgentConfig, run_id: str) -> Path:
    pause = Path(str(getattr(config, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause")))
    path = pause.parent / "composer-executor" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(receipt), ensure_ascii=True, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _read_terminal_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ComposerExecutorError("terminal_receipt_invalid")
    if isinstance(value, dict) and value.get("status") in {"success", "error", "rolled_back", "rollback_failed"}:
        return value
    raise ComposerExecutorError("terminal_receipt_nonterminal")


def _run_composer_update(config: AgentConfig, project_root: str, current: str, target: str) -> list[str]:
    cjson = Path(project_root) / "composer.json"
    if not cjson.is_file():
        raise ComposerExecutorError("composer_json_missing")
    composer_bin = upgrade._resolve_composer_bin(config.php_bin)
    upgrade._ensure_node20()
    upgrade._ensure_www_data_composer_cache()
    composer_version = upgrade._command_version_line([composer_bin, "-V", "--no-interaction", "--no-ansi"], cwd=project_root, as_www_data=True)
    readiness = upgrade.composer_readiness_from_observation(composer_bin, composer_version)
    if readiness.get("status") not in {"reused", "success"}:
        raise ComposerExecutorError("composer_readiness_failed")
    upgrade._command_version_line(["node", "-v"], cwd=project_root)
    upgrade._command_version_line(["npm", "-v"], cwd=project_root)
    from mcd_agent.composer_permissions import prepare_composer_paths
    prepare_composer_paths(project_root)
    text = cjson.read_text(encoding="utf-8")
    updated, changes = upgrade._replace_version_tokens(text, current, target)
    updated, constraint_changes = upgrade._normalize_mautic7_composer_constraints(updated, target)
    if changes + constraint_changes > 0 and updated != text:
        cjson.write_text(updated, encoding="utf-8")
    deferred = ("post-autoload-dump", "post-update-cmd")
    upgrade._run(
        ["env", "COMPOSER_SKIP_SCRIPTS=" + ",".join(deferred), composer_bin, *upgrade._composer_update_args()],
        cwd=project_root,
        as_www_data=True,
    )
    scripts = json.loads(cjson.read_text(encoding="utf-8")).get("scripts", {})
    ran: list[str] = []
    for event in deferred:
        if event in scripts:
            upgrade._run([composer_bin, "run-script", "--no-interaction", event], cwd=project_root, as_www_data=True)
            ran.append(event)
    return ran


def _apply_patch_steps(
    config: AgentConfig,
    root: str,
    patch: Mapping[str, Any],
    phases: list[str],
    source_version: str,
    target_version: str,
) -> list[dict[str, Any]]:
    from mcd_agent.mautic_patch_plan import atomic_preflight, execute, parse_plan, validate_upgrade_plan
    from mcd_agent.mautic_patch_fact_binding import bound_provider, needs_facts
    from mcd_agent.mautic_patch_resolution import read_plan_file

    raw_plan = read_plan_file(str(patch["plan_file"]), str(patch["plan_sha256"]))
    plan = validate_upgrade_plan(raw_plan, source_version, target_version, "composer")
    patch_plan = parse_plan(raw_plan)
    if patch_plan.get("run_id") != patch["run_id"]:
        raise ComposerExecutorError("patch_plan_run_id_mismatch")
    provider = bound_provider(config, root) if needs_facts(plan) else None
    options: dict[str, Any] = {}
    if provider is not None:
        options["facts_provider"] = provider
    admission = atomic_preflight(upgrade.application_root(upgrade._resolve_composer_project_root(root)), raw_plan, str(patch["run_id"]), **options)
    if admission.get("status") != "success":
        raise ComposerExecutorError("patch_plan_preflight_failed")
    if provider is not None:
        options["accepted_facts"] = admission.get("facts_receipt")
    outcomes: list[dict[str, Any]] = []
    for phase in phases:
        result = execute(
            upgrade.application_root(upgrade._resolve_composer_project_root(root)),
            raw_plan,
            phase,
            str(patch["run_id"]),
            "apply",
            **options,
        )
        if result.get("status") != "success":
            raise ComposerExecutorError(f"patch_phase_failed:{phase}")
        outcomes.extend(item for item in result.get("patches", []) if isinstance(item, dict))
        if provider is not None:
            options["accepted_facts"] = result.get("facts_receipt", options.get("accepted_facts"))
    return outcomes


def execute_request(
    config: AgentConfig,
    request: Mapping[str, Any],
    *,
    request_sha256: str = "",
) -> tuple[int, dict[str, Any]]:
    normalized = validate_request(request)
    receipt_file = _receipt_path(config, normalized["run_id"])
    previous = _read_terminal_receipt(receipt_file)
    if previous is not None:
        return (0 if previous.get("status") == "success" else 1), previous
    inst = upgrade._pick_install_record(config, normalized["root"])
    if str(inst.instance_uid or "") != normalized["instance_uid"] or str(inst.root) != normalized["root"]:
        raise ComposerExecutorError("instance_binding_mismatch")
    current = upgrade._read_current_version(inst.root, inst.console_path, config.php_bin, config.mautic_run_as_user)
    if current != normalized["source_version"]:
        raise ComposerExecutorError("source_version_mismatch")
    project_root = upgrade._resolve_composer_project_root(inst.root)
    started = _now()
    guard = None
    backup_path: Path | None = None
    mutation_started = False
    patch_outcomes: list[dict[str, Any]] = []
    steps_completed: list[str] = []
    rollback = {"available": False, "attempted": False, "succeeded": False}
    maintenance = {"cron_restored": False, "scheduler_resumed": False}
    try:
        guard = upgrade._enter_upgrade_maintenance(config, manage_system_cron=False)
        backup_path = upgrade._backup_install(inst.root)
        rollback["available"] = True
        for row in normalized["steps"]:
            kind = row if isinstance(row, str) else row["kind"]
            if kind == "composer_json_update":
                cjson = Path(project_root) / "composer.json"
                text = cjson.read_text(encoding="utf-8")
                updated, changes = upgrade._replace_version_tokens(text, normalized["source_version"], normalized["target_version"])
                updated, constraint_changes = upgrade._normalize_mautic7_composer_constraints(updated, normalized["target_version"])
                if changes + constraint_changes > 0 and updated != text:
                    cjson.write_text(updated, encoding="utf-8")
                mutation_started = True
            elif kind == "composer_update":
                _run_composer_update(config, project_root, normalized["source_version"], normalized["target_version"])
                mutation_started = True
            elif kind == "patch_phase":
                patch_outcomes.extend(
                    _apply_patch_steps(
                        config,
                        inst.root,
                        normalized["patch_plan"],
                        [row["phase"]],
                        normalized["source_version"],
                        normalized["target_version"],
                    )
                )
                mutation_started = True
            elif kind == "cache_clear":
                upgrade._clear_prod_cache_with_fallback(project_root, inst.console_path, config.php_bin)
                mutation_started = True
            elif kind == "mautic_finish":
                upgrade._run([config.php_bin, inst.console_path, "mautic:update:apply", "--finish"], cwd=inst.root, as_www_data=True)
                mutation_started = True
            elif kind == "doctrine_migrate":
                migration_cmd = upgrade._doctrine_migrate_command(project_root, inst.console_path, config.php_bin)
                upgrade._run_doctrine_migrate_with_reconcile(project_root, inst.console_path, config.php_bin, migration_cmd)
                upgrade._verify_or_reconcile_doctrine_migrations(project_root, inst.console_path, config.php_bin)
                mutation_started = True
            elif kind == "verify":
                final_version = upgrade._read_current_version(inst.root, inst.console_path, config.php_bin, config.mautic_run_as_user)
                if final_version != normalized["target_version"]:
                    raise ComposerExecutorError("target_version_check_failed")
                upgrade._post_upgrade_verify(config, inst)
                upgrade._write_upgrade_version_cache(inst.root, final_version)
                steps_completed.append(kind)
                continue
            steps_completed.append(kind)
        maintenance = upgrade._exit_upgrade_maintenance(config, guard)
        guard = None
        if not maintenance.get("scheduler_resumed") or not maintenance.get("cron_restored"):
            raise ComposerExecutorError("maintenance_restore_failed")
        receipt = {
            "schema": "mcd-mautic-composer-executor-receipt-v1",
            "status": "success",
            "started_at": started,
            "completed_at": _now(),
            "run_id": normalized["run_id"],
            "instance_uid": normalized["instance_uid"],
            "root": normalized["root"],
            "source_version": normalized["source_version"],
            "target_version": normalized["target_version"],
            "steps": steps_completed,
            "patch_outcomes": patch_outcomes,
            "maintenance_guard": maintenance,
            "rollback": rollback,
            "backup_path": str(backup_path or ""),
            "request_sha256": request_sha256,
            "evidence": normalized.get("evidence", {}),
        }
        _write_receipt(receipt_file, receipt)
        return 0, receipt
    except Exception as exc:
        if mutation_started and backup_path is not None:
            rollback["attempted"] = True
            try:
                upgrade._restore_prepared_target_backup(inst.root, backup_path)
                rollback["succeeded"] = True
            except Exception:
                rollback["succeeded"] = False
        if guard is not None:
            try:
                maintenance = upgrade._exit_upgrade_maintenance(config, guard)
            except Exception:
                maintenance = {"cron_restored": False, "scheduler_resumed": False}
        status = "rolled_back" if rollback["attempted"] and rollback["succeeded"] else "rollback_failed" if rollback["attempted"] else "error"
        receipt = {
            "schema": "mcd-mautic-composer-executor-receipt-v1",
            "status": status,
            "started_at": started,
            "completed_at": _now(),
            "run_id": normalized["run_id"],
            "instance_uid": normalized["instance_uid"],
            "root": normalized["root"],
            "source_version": normalized["source_version"],
            "target_version": normalized["target_version"],
            "steps": steps_completed,
            "patch_outcomes": patch_outcomes,
            "maintenance_guard": maintenance,
            "rollback": rollback,
            "backup_path": str(backup_path or ""),
            "request_sha256": request_sha256,
            "error": {"type": type(exc).__name__, "code": str(exc)},
            "evidence": normalized.get("evidence", {}),
        }
        _write_receipt(receipt_file, receipt)
        return 1, receipt
