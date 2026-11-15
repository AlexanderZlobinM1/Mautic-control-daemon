"""Unsigned check-only snapshot validation; never execution authority."""
import json
import re
import hashlib
from pathlib import Path

from mcd_agent.composer_target_manifest import validate_artifact
from mcd_agent.mautic_patch_check_only import collect_fixed_signature
from mcd_agent.mautic_patch_resolution import canonical_json_sha256
from mcd_agent.mautic_patch_stage import application_root

FIELDS = {"schema", "job_id", "run_id", "prepared_target_id", "original_patch_plan_sha256",
          "catalog_revision", "catalog_sha256", "target_artifact_sha256", "execution_context",
          "identity_association", "root_mapping", "excluded_records"}
BINDINGS = FIELDS - {"schema", "excluded_records"}


def validate_result(result, *, snapshot):
    copied = {"job_id", "run_id", "prepared_target_id", "original_patch_plan_sha256",
              "catalog_revision", "catalog_sha256", "target_artifact_sha256", "execution_context"}
    fields = copied | {"schema", "verification_plan_sha256", "status", "records"}
    if (not isinstance(result, dict) or set(result) != fields
            or result["schema"] != "mcd-mautic-target-patch-verification-result-v1"
            or result["status"] != "verified"
            or result["verification_plan_sha256"] != canonical_json_sha256(snapshot)
            or any(result[key] != snapshot[key] for key in copied)
            or len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode()) > 1048576):
        raise ValueError("verification_result_binding_invalid")
    rows = result["records"]
    if not isinstance(rows, list) or len(rows) != len(snapshot["excluded_records"]):
        raise ValueError("verification_result_records_invalid")
    from mcd_agent.mautic_patch_resolution import _validate_gates
    for row, original in zip(rows, snapshot["excluded_records"]):
        if (not isinstance(row, dict) or set(row) != {"id", "record_sha256", "decision", "gates"}
                or row["id"] != original["id"] or row["record_sha256"] != original["record_sha256"]
                or row["decision"] != "already"):
            raise ValueError("verification_result_record_binding_invalid")
        record = original["record"]
        _validate_gates(record, set(record["source_paths"]))
        if not isinstance(row["gates"], list) or len(row["gates"]) != len(record["gate"]):
            raise ValueError("verification_result_gates_invalid")
        matched = {"fixed": [], "vulnerable": []}
        for observed, gate in zip(row["gates"], record["gate"]):
            expected_key, actual_key, wanted = {
                "exact_count": ("expected", "actual", gate.get("expected_count")),
                "path_state": ("expected_state", "actual_state", gate.get("expected_state")),
                "sha256": ("expected_sha256", "actual_sha256", gate.get("expected_sha256")),
            }[gate["kind"]]
            observed_fields = {"group", "kind", "path", expected_key, actual_key}
            if gate["kind"] == "exact_count":
                observed_fields.add("needle")
            if (not isinstance(observed, dict)
                    or set(observed) != observed_fields
                    or any(observed[key] != gate[key] for key in ("group", "kind", "path"))
                    or (gate["kind"] == "exact_count" and observed["needle"] != gate["needle"])
                    or type(observed[expected_key]) is not type(wanted) or observed[expected_key] != wanted):
                raise ValueError("verification_result_gate_binding_invalid")
            actual = observed[actual_key]
            if gate["kind"] == "exact_count":
                valid = type(actual) is int and actual >= 0
            elif gate["kind"] == "path_state":
                valid = type(actual) is str and actual in {"absent", "present"}
            else:
                valid = type(actual) is str and re.fullmatch(r"[0-9a-f]{64}", actual)
            if not valid:
                raise ValueError("verification_result_gate_actual_invalid")
            matched[gate["group"]].append(actual == wanted)
        if not all(matched["fixed"]) or all(matched["vulnerable"]):
            raise ValueError("verification_result_fixed_signature_unproven")
    return result


def validate_snapshot(snapshot, *, expected_binding, catalog_bytes, original_patch_ids):
    if not isinstance(snapshot, dict) or set(snapshot) != FIELDS:
        raise ValueError("verification_snapshot_fields_invalid")
    if snapshot["schema"] != "mcc-mautic-target-patch-verification-plan-v1":
        raise ValueError("verification_snapshot_schema_invalid")
    if len(json.dumps(snapshot, ensure_ascii=True, allow_nan=False).encode()) > 1048576:
        raise ValueError("verification_snapshot_too_large")
    if set(expected_binding) != BINDINGS or any(snapshot[k] != expected_binding[k] for k in BINDINGS):
        raise ValueError("verification_snapshot_binding_mismatch")
    context = snapshot["execution_context"]
    if not isinstance(context, dict) or set(context) != {"host_id", "instance_uid", "application_root", "table_prefix"}:
        raise ValueError("verification_context_invalid")
    from mcd_agent.mautic_identity_association import validate_association
    validate_association(snapshot["identity_association"], wire_context=context)
    mapping = snapshot["root_mapping"]
    if (not isinstance(mapping, dict) or set(mapping) != {"schema", "project_root", "application_root_relative", "console_relative_path"}
            or mapping["schema"] != "mcd-mautic-root-mapping-v1"
            or type(mapping["project_root"]) is not str
            or not Path(mapping["project_root"]).is_absolute()
            or str(Path(mapping["project_root"])) != mapping["project_root"]
            or ".." in Path(mapping["project_root"]).parts
            or mapping["application_root_relative"] not in {".", "docroot", "public"}
            or str(Path(mapping["project_root"]) / mapping["application_root_relative"]) != context["application_root"]
            or mapping["console_relative_path"] not in {"bin/console", "docroot/bin/console", "public/bin/console"}):
        raise ValueError("verification_root_mapping_invalid")
    revision = snapshot["catalog_revision"]
    if type(revision) is not str or not revision or len(revision) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in revision):
        raise ValueError("verification_catalog_revision_invalid")
    for key in ("original_patch_plan_sha256", "catalog_sha256", "target_artifact_sha256"):
        if type(snapshot[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", snapshot[key]):
            raise ValueError("verification_digest_invalid")
    if not isinstance(catalog_bytes, bytes) or len(catalog_bytes) > 1048576:
        raise ValueError("verification_catalog_bytes_invalid")
    from mcd_agent.mautic_check_only_inputs import strict_json
    catalog = strict_json(catalog_bytes, max_bytes=1048576)
    if not isinstance(catalog, list):
        raise ValueError("verification_catalog_array_required")
    canonical = json.dumps(catalog, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode() + b"\n"
    if (canonical != catalog_bytes or hashlib.sha256(catalog_bytes).hexdigest() != snapshot["catalog_sha256"]
            or hashlib.sha1(catalog_bytes).hexdigest() != revision):
        raise ValueError("verification_catalog_bytes_binding_mismatch")
    verified_catalog_records = {}
    for record in catalog:
        if not isinstance(record, dict) or type(record.get("id")) is not str or record["id"] in verified_catalog_records:
            raise ValueError("verification_catalog_record_invalid")
        verified_catalog_records[record["id"]] = record
    records = snapshot["excluded_records"]
    if not isinstance(records, list) or not 1 <= len(records) <= 32:
        raise ValueError("verification_records_invalid")
    ids = set()
    previous_id = ""
    for row in records:
        if not isinstance(row, dict) or set(row) != {"id", "record_sha256", "record"}:
            raise ValueError("verification_record_fields_invalid")
        identifier = row["id"]
        if type(identifier) is not str or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,95}", identifier) or identifier in ids:
            raise ValueError("verification_record_id_invalid")
        ids.add(identifier)
        if identifier <= previous_id:
            raise ValueError("verification_record_order_invalid")
        previous_id = identifier
        if identifier in original_patch_ids:
            raise ValueError("verification_original_apply_record_not_excluded")
        record = row["record"]
        if (not isinstance(record, dict) or record.get("id") != identifier
                or canonical_json_sha256(record) != row["record_sha256"]
                or verified_catalog_records.get(identifier) != record):
            raise ValueError("verification_catalog_record_mismatch")
    return canonical_json_sha256(snapshot)


def collect_snapshot(stage, snapshot, *, expected_binding, catalog_bytes, original_patch_ids):
    """Caller supplies independently verified catalog/bindings; no signature claim."""
    digest = validate_snapshot(snapshot, expected_binding=expected_binding,
        catalog_bytes=catalog_bytes, original_patch_ids=original_patch_ids)
    if stage.prepared_receipt.get("prepared_target_id") != snapshot["prepared_target_id"]:
        raise ValueError("verification_prepared_identity_mismatch")
    from mcd_agent.composer_prepared_target import recheck_receipt
    recheck_receipt(stage.prepared_receipt, prepared_root=str(stage.root),
        live_root=snapshot["execution_context"]["application_root"],
        archive_paths=stage.prepared_archive_paths, expected_binding=stage.prepared_binding)
    artifact = stage.prepared_receipt["target_artifact"]
    validate_artifact(artifact, target_version=stage.target_version)
    if canonical_json_sha256(artifact) != snapshot["target_artifact_sha256"]:
        raise ValueError("verification_target_artifact_mismatch")
    prepared_binding = stage.prepared_receipt["binding"]
    for key in ("job_id", "run_id", "execution_context", "identity_association", "root_mapping"):
        if prepared_binding.get(key) != snapshot[key]:
            raise ValueError("verification_prepared_binding_mismatch")
    if prepared_binding.get("patch_plan_sha256") != snapshot["original_patch_plan_sha256"]:
        raise ValueError("verification_original_plan_mismatch")
    root = application_root(stage.root)
    records = []
    for row in snapshot["excluded_records"]:
        observed = collect_fixed_signature(root, record=row["record"],
            expected_record_sha256=row["record_sha256"])
        records.append(dict(id=row["id"], record_sha256=row["record_sha256"],
            decision="already", gates=observed["gates"]))
    result = {key: snapshot[key] for key in ("job_id", "run_id", "prepared_target_id",
        "original_patch_plan_sha256", "catalog_revision", "catalog_sha256",
        "target_artifact_sha256", "execution_context")}
    result.update(schema="mcd-mautic-target-patch-verification-result-v1",
        verification_plan_sha256=digest, status="verified", records=records)
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode()) > 1048576:
        raise ValueError("verification_result_too_large")
    return validate_result(result, snapshot=snapshot)
