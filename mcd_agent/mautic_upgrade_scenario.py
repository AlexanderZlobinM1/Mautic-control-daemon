"""MCD-owned typed Mautic upgrade scenario adapter.

The scenario is external data, while execution remains the existing MCD
``mautic-upgrade`` operation.  This module only verifies the immutable
scenario/patch bindings, materialises short-lived typed context files when
needed, and calls the same ``run_upgrade_apply`` implementation used by the
CLI.  It is intentionally limited to the 1.7 commercial capability.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from mcd_agent.commercial_portal import canonical_json
from mcd_agent.instance_uid import canonical_instance_uid
from mcd_agent.inventory import InstanceInventory
from mcd_agent.mautic_version_cache import read_mautic_version_evidence_read_only
from mcd_agent.scenario_artifact_store import ScenarioArtifactStore
from mcd_agent.scenario_sync import local_scenario


SCENARIO_SCHEMA = "mcd-mautic-upgrade-scenario-v1"
CONTRACT_VERSION = "1.0"
CAPABILITY_ID = "mautic_upgrade"
COMMAND_ID = "apply"
INPUT_SCHEMA_REF = "portal://mcd/input/mautic-upgrade-apply-v1"
_SHA_RE = re.compile(r"sha256:[0-9a-f]{64}")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")


class MauticUpgradeScenarioError(ValueError):
    """Bounded, secret-free scenario admission failure."""


def _text(value: Any, code: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MauticUpgradeScenarioError(code)
    return value.strip()


def _identifier(value: Any, code: str, maximum: int = 256) -> str:
    result = _text(value, code, maximum)
    if not _ID_RE.fullmatch(result):
        raise MauticUpgradeScenarioError(code)
    return result


def _digest(value: Any, code: str) -> str:
    result = _text(value, code, 71).lower()
    if not _SHA_RE.fullmatch(result):
        raise MauticUpgradeScenarioError(code)
    return result


def _version(value: Any, code: str) -> str:
    result = _text(value, code, 96)
    if not _VERSION_RE.fullmatch(result):
        raise MauticUpgradeScenarioError(code)
    return result


def _mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MauticUpgradeScenarioError(code)
    return dict(value)


def _canonical_sha(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(dict(value))).hexdigest()


def _scenario_content_sha(value: Mapping[str, Any]) -> str:
    content = dict(value)
    content.pop("scenario_digest", None)
    return _canonical_sha(content)


def _artifact_pin(value: Any, *, allow_local: bool = False) -> dict[str, Any]:
    pin = _mapping(value, "mautic_upgrade_scenario_artifact_invalid")
    required = {
        "artifact_path", "artifact_grant", "artifact_digest", "artifact_size_bytes",
        "artifact_media_type", "artifact_revision", "artifact_trust_mode",
        "artifact_transport", "trusted_catalog",
    }
    if set(pin) != required:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_invalid")
    digest = _digest(pin["artifact_digest"], "mautic_upgrade_scenario_artifact_invalid")
    if not _text(pin["artifact_path"], "mautic_upgrade_scenario_artifact_invalid"):
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_invalid")
    if not _text(pin["artifact_grant"], "mautic_upgrade_scenario_artifact_invalid"):
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_invalid")
    if type(pin["artifact_size_bytes"]) is not int or not 1 <= pin["artifact_size_bytes"] <= 16 * 1024 * 1024:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_invalid")
    if pin["artifact_media_type"] != "application/json":
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_media_type_invalid")
    revision = _identifier(pin["artifact_revision"], "mautic_upgrade_scenario_artifact_invalid")
    local_pin = pin["artifact_trust_mode"] == "local_file" and pin["artifact_transport"] == "local_file"
    if local_pin and not allow_local:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_transport_invalid")
    if not local_pin and (pin["artifact_trust_mode"] != "mcc_catalog_sha256" or pin["artifact_transport"] != "mcc_authenticated_api"):
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_transport_invalid")
    catalog = _mapping(pin["trusted_catalog"], "mautic_upgrade_scenario_catalog_invalid")
    if (
        set(catalog) != {"source", "transport", "status", "revision", "artifact_digest"}
        or catalog["source"] != ("local" if local_pin else "mcc")
        or catalog["transport"] != ("local_file" if local_pin else "mcc_authenticated_api")
        or catalog["status"] != "active"
        or catalog["revision"] != revision
        or catalog["artifact_digest"] != digest
    ):
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_catalog_invalid")
    return {
        **pin,
        "artifact_digest": digest,
        "artifact_revision": revision,
    }


def _scenario_store(config: Any, instance_uid: str = "") -> ScenarioArtifactStore:
    state_path = Path(str(getattr(config, "state_db_path", "") or "")).expanduser()
    if not state_path.is_absolute() or not state_path.name:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_state_path_invalid")
    root = state_path.resolve().parent / "scenario-artifacts"
    if instance_uid:
        root = root / _identifier(instance_uid, "mautic_upgrade_scenario_instance_invalid")
    return ScenarioArtifactStore(root)


def _install_for_uid(config: Any, instance_uid: str) -> Any:
    """Resolve the stable UID from the current registered local inventory.

    A manually registered instance is already an authoritative current MCD
    instance. Re-running filesystem discovery here can legitimately omit it
    (for example, before vhost/domain discovery) and must not invalidate the
    standard UID-only command path.
    """
    state_path = Path(str(getattr(config, "state_db_path", "") or "")).expanduser()
    if not state_path.is_absolute() or not state_path.name or not state_path.is_file():
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_state_path_invalid")
    inventory = InstanceInventory(str(state_path))
    try:
        installs = inventory.list_instances()
    finally:
        inventory.conn.close()
    matches = [
        item
        for item in installs
        if canonical_instance_uid(str(getattr(item, "instance_uid", ""))) == instance_uid
    ]
    if len(matches) != 1:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_instance_not_current")
    return matches[0]


def _resolve_scenario(
    config: Any,
    *,
    instance_uid: str,
    application_root: str,
    resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    allow_local: bool = False,
) -> tuple[str, str, dict[str, Any], str]:
    evidence = read_mautic_version_evidence_read_only(application_root)
    source_version = str(evidence.get("version") or "").strip()
    if not source_version or str(evidence.get("source") or "") != "static_metadata":
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_source_version_unavailable")
    # Corporate MCC delivers executable scenario packages through the normal
    # state-push control response.  They are host-wide catalog objects, not
    # per-command JSON envelopes and therefore do not use the old per-instance
    # ScenarioArtifactStore.
    runtime_item = local_scenario(config, "mautic-upgrade")
    if runtime_item is not None:
        return (
            str(runtime_item["id"]),
            str(runtime_item["version"]),
            dict(runtime_item["pin"]),
            source_version,
        )
    local_store = _scenario_store(config, instance_uid)
    local = local_store.read_current()
    local_result: tuple[str, str, dict[str, Any], str] | None = None
    if local is not None:
        active, raw = local
        try:
            document = json.loads(raw.decode("utf-8"))
            if not isinstance(document, Mapping):
                raise ValueError("document")
            local_id = _identifier(document.get("scenario_id"), "mautic_upgrade_scenario_local_invalid")
            local_revision = _identifier(document.get("scenario_revision"), "mautic_upgrade_scenario_local_invalid")
            if document.get("instance_uid") != instance_uid or document.get("application_root") != application_root:
                raise MauticUpgradeScenarioError("mautic_upgrade_scenario_local_binding_mismatch")
            digest = _digest(active.get("digest"), "mautic_upgrade_scenario_local_invalid")
            local_pin = {
                "artifact_path": str(local_store.objects / digest[7:]),
                "artifact_grant": "local",
                "artifact_digest": digest,
                "artifact_size_bytes": int(active["size_bytes"]),
                "artifact_media_type": "application/json",
                "artifact_revision": local_revision,
                "artifact_trust_mode": "local_file",
                "artifact_transport": "local_file",
                "trusted_catalog": {
                    "source": "local",
                    "transport": "local_file",
                    "status": "active",
                    "revision": local_revision,
                    "artifact_digest": digest,
                },
            }
            local_result = (local_id, local_revision, local_pin, source_version)
        except (MauticUpgradeScenarioError, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            local_result = None

    if resolver is None:
        if local_result is not None:
            return local_result
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_resolver_unavailable")
    try:
        result = resolver(
            {
                "operation": "mautic-upgrade",
                "instance_uid": instance_uid,
                "source_version": source_version,
            }
        )
    except Exception:
        if local_result is not None:
            return local_result
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_resolve_failed")
    resolved = _mapping(result, "mautic_upgrade_scenario_resolution_invalid")
    if resolved.get("status") not in {"matched", "succeeded"}:
        if local_result is not None:
            return local_result
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_not_available")
    scenario_id = _identifier(resolved.get("scenario_id"), "mautic_upgrade_scenario_resolution_invalid")
    scenario_revision = _identifier(resolved.get("scenario_revision"), "mautic_upgrade_scenario_resolution_invalid")
    pin = _artifact_pin(resolved.get("scenario_artifact"), allow_local=allow_local)
    return scenario_id, scenario_revision, pin, source_version


def _latest_upgrade_target(config: Any, source_version: str) -> str:
    from mcd_agent.mautic_upgrade import _latest_same_branch

    target = _latest_same_branch(config, source_version)
    if not target:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_target_unavailable")
    return str(target)


def _external_scenario_context(
    *,
    config: Any,
    install: Any,
    instance_uid: str,
    scenario_id: str,
    scenario_revision: str,
    artifact_digest: str,
    source_version: str,
    target_version: str,
    install_type: str,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build only observed host/application facts for the generic runner."""
    from mcd_agent.commercial_detector import (
        applicability_facts_digest,
        read_normalized_applicability_facts,
    )

    capabilities = ["package_manager.query"]
    facts = read_normalized_applicability_facts(
        target_kind="existing_server",
        capabilities=capabilities,
    )
    php_bin = str(getattr(install, "runtime_php_bin", "") or getattr(config, "php_bin", "") or "php")
    run_as_user = str(getattr(install, "runtime_user", "") or getattr(config, "mautic_run_as_user", "") or "www-data")
    composer = str(getattr(config, "composer_bin", "") or shutil.which("composer") or "/usr/local/bin/composer")
    php_version = "8.4"
    match = re.search(r"php(?:/|)(\d+\.\d+)", php_bin)
    if match:
        php_version = match.group(1)
    job_id = f"mautic-upgrade-scenario-{run_id}"
    binding = {
        "job_id": job_id,
        "request_id": job_id,
        "command_id": "apply",
        "idempotency_key": job_id,
    }
    scenario_binding = {
        "scenario_id": scenario_id,
        "revision": scenario_revision,
        "artifact_revision": scenario_revision,
        "artifact_digest": artifact_digest,
    }
    target_label = target_version or "scenario"
    parameters = {
        "run_id": run_id,
        "transition_id": f"mautic-{source_version}-to-{target_label}-{install_type}",
        "instance_uid": instance_uid,
        "root": str(install.root),
        "console_path": str(install.console_path),
        "php_bin": php_bin,
        "run_as_user": run_as_user,
        "source_version": source_version,
        "target_version": target_version,
        "install_type": install_type,
        "composer_bin": composer,
        "runtime_php": php_version,
    }
    context = {
        "schema": "mcd-scenario-executable-context-v1",
        "contract_version": "1.0",
        "job": binding,
        "scenario": scenario_binding,
        "applicability": {"facts_digest": applicability_facts_digest(facts)},
        "parameters": parameters,
        "secret_references": [],
    }
    return context, facts


def _run_external_scenario(
    *,
    config: Any,
    install: Any,
    instance_uid: str,
    scenario_id: str,
    scenario_revision: str,
    pin: Mapping[str, Any],
    source_version: str,
    job: Mapping[str, Any],
    inventory_rescan: Callable[[], Any] | None,
) -> Mapping[str, Any]:
    """Run an MCC-delivered executable scenario through the shared MCD runner."""
    # The executable scenario is authoritative for its own upgrade steps.  A
    # target is needed only to resolve the optional MCC patch plan; a missing
    # MCC connection/catalog must not prevent a verified local scenario from
    # running offline.
    target_version = ""
    if str(getattr(config, "mcc_url", "") or "").strip():
        try:
            target_version = _latest_upgrade_target(config, source_version)
        except MauticUpgradeScenarioError:
            target_version = ""
    from mcd_agent.install_type import detect_install_type
    from mcd_agent.commercial_self_hosted import ExternalProcessPolicy, default_scenario_runner_argv

    install_type = detect_install_type(str(install.root))
    selected = (
        _resolve_patch_plan(
            config,
            install,
            source=source_version,
            target=target_version,
            mode=install_type,
        )
        if target_version
        else None
    )
    patch_cache = None
    patch = None
    if selected is not None:
        plan = _mapping(selected.get("plan"), "mautic_upgrade_scenario_patch_plan_invalid")
        patch = {
            "catalog_revision": str(selected.get("catalog_revision") or ""),
            "catalog_sha256": "sha256:" + str(selected.get("catalog_sha256") or ""),
            "plan_sha256": "sha256:" + str(selected.get("plan_sha256") or ""),
            "run_id": str(plan.get("run_id") or ""),
            "plan": plan,
        }
        patch_cache = _stage_patch_plan(config, instance_uid, {"plan": plan})
    run_id = str(job.get("job_id") or job.get("command_id") or "scenario")
    context, facts = _external_scenario_context(
        config=config,
        install=install,
        instance_uid=instance_uid,
        scenario_id=scenario_id,
        scenario_revision=scenario_revision,
        artifact_digest=str(pin["artifact_digest"]),
        source_version=source_version,
        target_version=target_version,
        install_type=install_type,
        run_id=run_id,
    )
    runner = ExternalProcessPolicy(
        default_scenario_runner_argv(),
        timeout_sec=int(getattr(config, "command_timeout_sec", 1800) or 1800),
    )
    try:
        runner_result = runner.run(
            artifact=Path(str(pin["artifact_path"])).read_bytes(),
            job={
                "job_id": context["job"]["job_id"],
                "scenario": context["scenario"],
                "scenario_artifact": dict(pin),
            },
            facts=facts,
            context=context,
        )
    except Exception as exc:
        reason = str(getattr(exc, "code", "mautic_upgrade_scenario_runner_failed"))
        return {"status": "failed", "reason": reason, "scenario": {"id": scenario_id, "revision": scenario_revision, "artifact_digest": pin["artifact_digest"]}}
    patch_result: Mapping[str, Any] = {"status": "not_available"}
    if patch is not None:
        from mcd_agent.mautic_patch_plan_v3 import atomic_preflight, execute as execute_patch_plan
        from mcd_agent.mautic_patch_fact_binding import bound_provider, needs_facts

        plan = patch["plan"]
        root = str(install.root)
        facts_provider = bound_provider(config, root) if needs_facts(plan) else None
        preflight = atomic_preflight(
            root,
            plan,
            facts_provider=facts_provider,
        )
        accepted_facts = preflight.get("facts_receipt")
        patch_result = execute_patch_plan(
            root,
            plan,
            phase="post_source_install",
            observed_major=int(source_version.split(".", 1)[0]),
            facts_provider=facts_provider,
            accepted_facts=accepted_facts,
        )
        if patch_result.get("status") != "success":
            return {"status": "failed", "reason": "mautic_upgrade_scenario_patch_apply_failed", "scenario": {"id": scenario_id, "revision": scenario_revision}, "runner": runner_result, "patch": patch_result}
    final_version = read_mautic_version_evidence_read_only(str(install.root))
    inventory = inventory_rescan() if inventory_rescan is not None else {"status": "not_configured"}
    return {
        "status": "succeeded",
        "scenario": {"id": scenario_id, "revision": scenario_revision, "artifact_digest": pin["artifact_digest"]},
        "instance_uid": instance_uid,
        "application_root": str(install.root),
        "runner": runner_result,
        "patch": {"status": "staged_and_applied" if patch is not None else "not_available", "cache": patch_cache, "result": patch_result},
        "cache_clear": {"status": "scenario_owned"},
        "final_version": final_version,
        "inventory_rescan": inventory,
    }


def load_scenario_artifact(
    config: Any,
    pin: Mapping[str, Any],
    *,
    scenario_id: str,
    scenario_revision: str,
    idempotency_key: str,
    downloader: Callable[[Mapping[str, Any]], bytes] | None,
    instance_uid: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the exact local scenario or fetch/activate the exact MCC pin."""
    store = _scenario_store(config, instance_uid)
    digest = str(pin["artifact_digest"])
    revision = str(pin["artifact_revision"])
    raw = store.read_active(revision=revision, digest=digest)
    delivery: dict[str, Any] = {
        "status": "local_cache_hit",
        "revision": revision,
        "digest": digest,
        "size_bytes": int(pin["artifact_size_bytes"]),
        "idempotent": True,
    }
    if raw is None:
        if downloader is None:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_downloader_unavailable")
        try:
            raw = downloader(pin)
        except Exception as exc:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_download_failed") from exc
        if not isinstance(raw, bytes):
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_download_invalid")
        activated = store.deliver(
            pin=pin,
            artifact=raw,
            verify=lambda candidate, _payload: {
                "artifact_revision": candidate["artifact_revision"],
                "artifact_digest": candidate["artifact_digest"],
            },
            binding={
                "scenario_id": scenario_id,
                "scenario_revision": scenario_revision,
                "idempotency_key": idempotency_key,
            },
        )
        delivery = dict(activated["receipt"])
        raw = Path(str(activated["artifact_path"])).read_bytes()
    if len(raw) != int(pin["artifact_size_bytes"]):
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_size_mismatch")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_artifact_sha256_mismatch")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_document_invalid") from exc
    if not isinstance(document, dict):
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_document_invalid")
    return document, delivery


def validate_scenario_document(
    document: Mapping[str, Any],
    *,
    pin: Mapping[str, Any],
    scenario_id: str,
    scenario_revision: str,
    instance_uid: str,
    application_root: str,
) -> dict[str, Any]:
    value = dict(document)
    required = {"schema", "contract_version", "scenario_id", "scenario_revision", "scenario_digest", "instance_uid", "application_root", "source_version", "target_version", "cli", "post"}
    if not required.issubset(value) or set(value) - required - {"context", "patch"} or value["schema"] != SCENARIO_SCHEMA or value["contract_version"] != CONTRACT_VERSION:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_schema_invalid")
    if value["scenario_id"] != scenario_id or value["scenario_revision"] != scenario_revision:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_binding_mismatch")
    if value["scenario_digest"] != _scenario_content_sha(value):
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_content_digest_mismatch")
    if value["instance_uid"] != instance_uid or value["application_root"] != application_root:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_instance_binding_mismatch")
    source = _version(value["source_version"], "mautic_upgrade_scenario_source_version_invalid")
    target = _version(value["target_version"], "mautic_upgrade_scenario_target_version_invalid")
    cli = _mapping(value["cli"], "mautic_upgrade_scenario_cli_invalid")
    cli_required = {"mode", "yes", "backup", "with_system_upgrade", "allow_minor", "allow_major", "allow_release_transition", "mcc_preflighted_single_instance"}
    if set(cli) != cli_required or cli["mode"] not in {"auto", "zip", "composer"}:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_cli_invalid")
    if any(type(cli[key]) is not bool for key in cli_required - {"mode"}) or cli["yes"] is not True or cli["mcc_preflighted_single_instance"] is not True:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_cli_invalid")
    patch_value = value.get("patch")
    patch = None
    if patch_value is not None:
        patch = _mapping(patch_value, "mautic_upgrade_scenario_patch_invalid")
        patch_required = {"catalog_revision", "catalog_sha256", "plan_sha256", "run_id", "plan"}
        if set(patch) != patch_required:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_invalid")
        catalog_revision = _identifier(patch["catalog_revision"], "mautic_upgrade_scenario_patch_invalid")
        catalog_sha = _digest(patch["catalog_sha256"], "mautic_upgrade_scenario_patch_invalid")
        run_id = _identifier(patch["run_id"], "mautic_upgrade_scenario_patch_invalid", 128)
        plan = _mapping(patch["plan"], "mautic_upgrade_scenario_patch_invalid")
        plan_sha = _digest(patch["plan_sha256"], "mautic_upgrade_scenario_patch_invalid")
        if plan_sha != _canonical_sha(plan) or plan.get("run_id") != run_id:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_binding_mismatch")
        if plan.get("source_version") != source or plan.get("target_version") != target:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_version_mismatch")
        try:
            from mcd_agent.install_type import detect_install_type
            from mcd_agent.mautic_patch_plan import validate_upgrade_plan
            validate_upgrade_plan(
                json.dumps(plan, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                source,
                target,
                (detect_install_type(application_root) if cli["mode"] == "auto" else str(cli["mode"])),
            )
        except Exception as exc:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_plan_invalid") from exc
    post = _mapping(value["post"], "mautic_upgrade_scenario_post_invalid")
    if set(post) != {"cache_clear", "inventory_rescan"} or post["cache_clear"] is not True or post["inventory_rescan"] is not True:
        raise MauticUpgradeScenarioError("mautic_upgrade_scenario_post_invalid")
    return {
        **value,
        "source_version": source,
        "target_version": target,
        "cli": cli,
        **({"patch": {
            **patch,
            "catalog_revision": catalog_revision,
            "catalog_sha256": catalog_sha,
            "plan_sha256": plan_sha,
            "run_id": run_id,
            "plan": plan,
        }} if patch is not None else {}),
    }


def _safe_projection(
    value: Mapping[str, Any],
    *,
    root: str,
    instance_uid: str,
    artifact_digest: str,
) -> dict[str, Any]:
    cli = dict(value["cli"])
    projection = {
        "scenario_id": value["scenario_id"],
        "scenario_revision": value["scenario_revision"],
        "scenario_digest": value["scenario_digest"],
        "artifact_digest": artifact_digest,
        "root": root,
        "instance_uid": instance_uid,
        "source_version": value["source_version"],
        "target_version": value["target_version"],
        **cli,
    }
    patch = value.get("patch")
    if isinstance(patch, Mapping):
        projection.update({
            "patch_catalog_revision": patch["catalog_revision"],
            "patch_catalog_sha256": patch["catalog_sha256"],
            "patch_plan_sha256": patch["plan_sha256"],
            "patch_run_id": patch["run_id"],
        })
    else:
        projection["patch_source"] = "mcd-resolved-before-apply"
    return projection


def _write_json_context(directory: Path, name: str, value: Mapping[str, Any]) -> str:
    path = directory / name
    raw = canonical_json(dict(value)) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return str(path)


def _resolve_patch_plan(config: Any, install: Any, *, source: str, target: str, mode: str) -> Mapping[str, Any] | None:
    """Ask the existing MCD patch resolver for the optional current plan."""
    if not str(getattr(config, "mcc_url", "") or "").strip():
        return None
    try:
        from mcd_agent.mautic_patch_resolution import resolve_plan
        from mcd_agent.install_type import detect_install_type
        selected_mode = detect_install_type(install.root) if mode == "auto" else mode
        selected = resolve_plan(
            config,
            install,
            trigger="upgrade_lifecycle",
            phase="dependency_update_preflight",
            operation="apply",
            observed_version=source,
            observed_major=int(source.split(".", 1)[0]),
            target_version=target,
            install_type=selected_mode,
        )
        return selected if selected.get("status") == "selected" else None
    except Exception:
        # A missing MCC connection means the verified local scenario remains
        # authoritative; the upgrade itself still follows that scenario.
        return None


def _stage_patch_plan(config: Any, instance_uid: str, selected: Mapping[str, Any]) -> dict[str, Any]:
    """Materialise and re-check all selected patch bytes before apply."""
    plan = _mapping(selected.get("plan"), "mautic_upgrade_scenario_patch_plan_invalid")
    run_id = _identifier(plan.get("run_id"), "mautic_upgrade_scenario_patch_plan_invalid", 128)
    state_path = Path(str(getattr(config, "state_db_path", "") or "")).expanduser().resolve()
    root = state_path.parent / "patches" / _identifier(instance_uid, "mautic_upgrade_scenario_instance_invalid") / run_id
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    files: list[dict[str, Any]] = []
    for payload in plan.get("payloads", []) if isinstance(plan.get("payloads"), list) else []:
        item = _mapping(payload, "mautic_upgrade_scenario_patch_payload_invalid")
        relative = _text(item.get("path"), "mautic_upgrade_scenario_patch_payload_invalid", 512)
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_payload_path_invalid")
        encoded = item.get("content_base64")
        expected = str(item.get("sha256") or "").lower()
        if not isinstance(encoded, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_payload_invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_payload_invalid") from exc
        if hashlib.sha256(content).hexdigest() != expected:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_payload_sha256_mismatch")
        destination = (root / relative).resolve()
        if root not in destination.parents:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_patch_payload_path_invalid")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + ".tmp")
        with temporary.open("wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        files.append({"path": str(destination), "sha256": expected, "size_bytes": len(content)})
    return {
        "status": "staged",
        "root": str(root),
        "run_id": run_id,
        "files": files,
    }


def execute(
    job: Mapping[str, Any],
    *,
    config: Any,
    scenario_downloader: Callable[[Mapping[str, Any]], bytes] | None,
    scenario_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    inventory_rescan: Callable[[], Any] | None,
) -> Mapping[str, Any]:
    value = _mapping(job.get("input"), "mautic_upgrade_scenario_input_invalid")
    required = {"instance_uid"}
    if set(value) != required:
        return {"status": "failed", "reason": "mautic_upgrade_scenario_input_invalid"}
    instance_uid = _identifier(value["instance_uid"], "mautic_upgrade_scenario_input_invalid")
    try:
        install = _install_for_uid(config, instance_uid)
        application_root = str(install.root)
        scenario_id, scenario_revision, pin, _source_version = _resolve_scenario(
            config,
            instance_uid=instance_uid,
            application_root=application_root,
            resolver=scenario_resolver,
            allow_local=bool(job.get("_local_scenario")),
        )
        if str(pin.get("artifact_media_type") or "") in {"application/x-sh", "application/x-elf"}:
            return _run_external_scenario(
                config=config,
                install=install,
                instance_uid=instance_uid,
                scenario_id=scenario_id,
                scenario_revision=scenario_revision,
                pin=pin,
                source_version=_source_version,
                job=job,
                inventory_rescan=inventory_rescan,
            )
        document, delivery = load_scenario_artifact(
            config,
            pin,
            scenario_id=scenario_id,
            scenario_revision=scenario_revision,
            idempotency_key=str(job.get("job_id") or job.get("command_id") or "scenario"),
            downloader=scenario_downloader,
            instance_uid=instance_uid,
        )
        scenario = validate_scenario_document(
            document,
            pin=pin,
            scenario_id=scenario_id,
            scenario_revision=scenario_revision,
            instance_uid=instance_uid,
            application_root=application_root,
        )
        selected_patch = scenario.get("patch")
        patch_cache: dict[str, Any] | None = None
        fresh_patch = _resolve_patch_plan(
            config,
            install,
            source=str(scenario["source_version"]),
            target=str(scenario["target_version"]),
            mode=str(scenario["cli"]["mode"]),
        )
        if fresh_patch is not None:
            selected_patch = {
                "catalog_revision": fresh_patch.get("catalog_revision"),
                "catalog_sha256": "sha256:" + str(fresh_patch.get("catalog_sha256") or ""),
                "plan_sha256": "sha256:" + str(fresh_patch.get("plan_sha256") or ""),
                "run_id": str((_mapping(fresh_patch.get("plan"), "mautic_upgrade_scenario_patch_plan_invalid")).get("run_id") or ""),
                "plan": _mapping(fresh_patch.get("plan"), "mautic_upgrade_scenario_patch_plan_invalid"),
            }
        if isinstance(selected_patch, Mapping):
            patch_cache = _stage_patch_plan(config, instance_uid, {
                "plan": selected_patch["plan"],
            })
        if isinstance(selected_patch, Mapping):
            scenario = {**scenario, "patch": dict(selected_patch)}
        from mcd_agent.install_type import detect_install_type
        effective_mode = detect_install_type(application_root) if scenario["cli"]["mode"] == "auto" else scenario["cli"]["mode"]
        projection = _safe_projection(
            scenario,
            root=application_root,
            instance_uid=instance_uid,
            artifact_digest=pin["artifact_digest"],
        )
        projection["effective_mode"] = effective_mode
        print("MCD_MAUTIC_UPGRADE_SCENARIO_PARAMETERS=" + json.dumps(projection, ensure_ascii=True, sort_keys=True, separators=(",", ":")), flush=True)
        cli = scenario["cli"]
        patch = scenario.get("patch")
        plan_json = json.dumps(patch["plan"], ensure_ascii=True, sort_keys=True, separators=(",", ":")) if isinstance(patch, Mapping) else None
        upgrade_kwargs: dict[str, Any] = {
            "config": config,
            "root": application_root,
            "mode": effective_mode,
            "yes": True,
            "do_backup": cli["backup"],
            "with_system_upgrade": cli["with_system_upgrade"],
            "target_override": scenario["target_version"],
            "allow_minor": cli["allow_minor"],
            "allow_major": cli["allow_major"],
            "allow_release_transition": cli["allow_release_transition"],
            "mcc_preflighted_single_instance": True,
            "patch_plan_json": plan_json,
            "patch_run_id": patch["run_id"] if isinstance(patch, Mapping) else None,
            "operation_id": str(job.get("job_id") or job.get("command_id") or "mautic-upgrade-scenario"),
        }
        optional = scenario.get("context")
        context = _mapping(optional, "mautic_upgrade_scenario_context_invalid") if optional is not None else {}
        allowed_context = {"repair_plan_json", "prepared_target_json", "repair_auth_context", "patch_backup_context", "mcc_release_authorization_context"}
        if set(context) - allowed_context:
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_context_invalid")
        upgrade_kwargs["repair_plan_json"] = context.get("repair_plan_json") or None
        upgrade_kwargs["prepared_target_json"] = context.get("prepared_target_json") or None
        if context.get("repair_auth_context") is not None or context.get("patch_backup_context") is not None or context.get("mcc_release_authorization_context") is not None:
            base = Path(str(getattr(config, "state_db_path"))).resolve().parent
            with tempfile.TemporaryDirectory(prefix="mcd-upgrade-scenario-", dir=base) as temporary:
                directory = Path(temporary)
                if context.get("repair_auth_context") is not None:
                    upgrade_kwargs["repair_auth_context_file"] = _write_json_context(directory, "repair-auth.json", _mapping(context["repair_auth_context"], "mautic_upgrade_scenario_context_invalid"))
                if context.get("patch_backup_context") is not None:
                    upgrade_kwargs["patch_backup_context_file"] = _write_json_context(directory, "patch-backup.json", _mapping(context["patch_backup_context"], "mautic_upgrade_scenario_context_invalid"))
                if context.get("mcc_release_authorization_context") is not None:
                    release_path = _write_json_context(directory, "release-auth.json", _mapping(context["mcc_release_authorization_context"], "mautic_upgrade_scenario_context_invalid"))
                    upgrade_kwargs["mcc_release_authorization_context_file"] = release_path
                    upgrade_kwargs["mcc_release_authorization_context_sha256"] = hashlib.sha256(Path(release_path).read_bytes()).hexdigest()
                from mcd_agent.mautic_upgrade import run_upgrade_apply
                rc = run_upgrade_apply(**upgrade_kwargs)
        else:
            from mcd_agent.mautic_upgrade import run_upgrade_apply
            rc = run_upgrade_apply(**upgrade_kwargs)
        if rc != 0:
            return {"status": "failed", "reason": "mautic_upgrade_scenario_failed", "scenario": {"id": scenario_id, "revision": scenario_revision, "digest": pin["artifact_digest"]}, "cli_parameters": projection}
        from mcd_agent.mautic_version_cache import read_mautic_version_evidence_read_only
        final_version = read_mautic_version_evidence_read_only(application_root)
        inventory = inventory_rescan() if inventory_rescan is not None else {"status": "not_configured"}
        return {
            "status": "succeeded",
            "scenario": {
                "id": scenario_id,
                "revision": scenario_revision,
                "digest": scenario["scenario_digest"],
                "artifact_digest": pin["artifact_digest"],
                "delivery": delivery,
            },
            "instance_uid": instance_uid,
            "application_root": application_root,
            "cli_parameters": projection,
            "patch": {
                "status": "staged" if isinstance(patch, Mapping) else "not_available",
                **({
                    "catalog_revision": patch["catalog_revision"],
                    "catalog_sha256": patch["catalog_sha256"],
                    "plan_sha256": patch["plan_sha256"],
                    "run_id": patch["run_id"],
                } if isinstance(patch, Mapping) else {}),
                "cache": patch_cache,
                "selected": [
                    {"id": row.get("id"), "payload_path": row.get("payload_path"), "payload_sha256": next((item.get("sha256") for item in patch["plan"].get("payloads", []) if item.get("path") == row.get("payload_path")), None)}
                    for row in (patch["plan"].get("patches", []) if isinstance(patch, Mapping) else []) if isinstance(row, Mapping)
                ],
            },
            "cache_clear": {"status": "succeeded", "owner": "mcd-cli-mautic-upgrade"},
            "final_version": final_version,
            "inventory_rescan": inventory,
        }
    except MauticUpgradeScenarioError as exc:
        return {"status": "failed", "reason": str(exc)}
    except Exception:
        return {"status": "failed", "reason": "mautic_upgrade_scenario_execution_failed"}


def execute_local_file(
    config: Any,
    *,
    scenario_file: str,
    expected_sha256: str,
    root: str | None = None,
    inventory_rescan: Callable[[], Any] | None = None,
) -> Mapping[str, Any]:
    """Run one locally staged scenario through the same MCD execution path."""
    path = Path(str(scenario_file or ""))
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        return {"status": "failed", "reason": "mautic_upgrade_scenario_file_invalid"}
    expected = str(expected_sha256 or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected):
        expected = "sha256:" + expected
    if not _SHA_RE.fullmatch(expected):
        return {"status": "failed", "reason": "mautic_upgrade_scenario_sha256_invalid"}
    try:
        raw = path.read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != expected:
            return {"status": "failed", "reason": "mautic_upgrade_scenario_sha256_mismatch"}
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, Mapping):
            raise MauticUpgradeScenarioError("mautic_upgrade_scenario_document_invalid")
        scenario_id = _identifier(document.get("scenario_id"), "mautic_upgrade_scenario_document_invalid")
        scenario_revision = _identifier(document.get("scenario_revision"), "mautic_upgrade_scenario_document_invalid")
        instance_uid = _identifier(document.get("instance_uid"), "mautic_upgrade_scenario_document_invalid")
        application_root = _text(document.get("application_root"), "mautic_upgrade_scenario_document_invalid", 4096)
        if root and str(root) != application_root:
            return {"status": "failed", "reason": "mautic_upgrade_scenario_root_mismatch"}
        pin = {
            "artifact_path": str(path),
            "artifact_grant": "local",
            "artifact_digest": expected,
            "artifact_size_bytes": len(raw),
            "artifact_media_type": "application/json",
            "artifact_revision": scenario_revision,
            "artifact_trust_mode": "local_file",
            "artifact_transport": "local_file",
            "trusted_catalog": {
                "source": "local",
                "transport": "local_file",
                "status": "active",
                "revision": scenario_revision,
                "artifact_digest": expected,
            },
        }
        return execute(
            {
                "job_id": "cli-mautic-upgrade-scenario-" + scenario_revision,
                "command_id": "apply",
                "_local_scenario": True,
                "input": {
                    "instance_uid": instance_uid,
                },
            },
            config=config,
            scenario_downloader=lambda _pin: raw,
            scenario_resolver=lambda _request: {
                "status": "matched",
                "scenario_id": scenario_id,
                "scenario_revision": scenario_revision,
                "scenario_artifact": pin,
            },
            inventory_rescan=inventory_rescan,
        )
    except MauticUpgradeScenarioError as exc:
        return {"status": "failed", "reason": str(exc)}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "mautic_upgrade_scenario_file_invalid"}

