"""Catalog-resolved, phase-atomic Mautic patch execution (v3)."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from mcd_agent.mautic_patch_resolution import (
    MauticPatchResolutionError,
    _contract,
    _validate_plan_records,
    canonical_json_sha256,
)


SCHEMA = "mcd-mautic-patch-plan-v3"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIFF_RE = re.compile(rb"^diff --git a/(.+) b/(.+)$", flags=re.MULTILINE)


class PatchPlanV3Error(RuntimeError):
    """A v3 plan could not be safely verified, applied, or rolled back."""

    def __init__(self, reason: str, *, evidence: dict[str, Any] | None = None):
        super().__init__(reason)
        self.evidence = evidence if isinstance(evidence, dict) else None


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _root(value: str) -> Path:
    raw = Path(str(value or ""))
    if not str(value or "").strip() or raw.is_symlink():
        raise PatchPlanV3Error("patch_root_invalid")
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise PatchPlanV3Error("patch_root_unavailable") from exc
    if not root.is_dir():
        raise PatchPlanV3Error("patch_root_not_directory")
    return root


def _relative(raw: Any) -> str:
    value = str(raw or "")
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in parts[0]
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise PatchPlanV3Error("patch_path_unsafe")
    return value


def _file(root: Path, relative: str, *, create_parents: bool = False) -> Path:
    relative = _relative(relative)
    current = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise PatchPlanV3Error("patch_path_symlink")
        if index < len(parts) - 1 and current.exists() and not current.is_dir():
            raise PatchPlanV3Error("patch_path_parent_not_directory")
        if index < len(parts) - 1 and create_parents and not current.exists():
            current.mkdir(mode=0o755)
    try:
        current.relative_to(root)
    except ValueError as exc:
        raise PatchPlanV3Error("patch_path_outside_root") from exc
    return current


def _read(path: Path) -> tuple[bytes | None, os.stat_result | None]:
    if path.is_symlink():
        raise PatchPlanV3Error("patch_path_symlink")
    if not path.exists():
        return None, None
    if not path.is_file():
        raise PatchPlanV3Error("patch_path_not_regular_file")
    try:
        return path.read_bytes(), path.stat()
    except OSError as exc:
        raise PatchPlanV3Error("patch_path_read_failed") from exc


def _gate(root: Path, gate: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    path = _file(root, gate["path"])
    value, _st = _read(path)
    kind = gate["kind"]
    row: dict[str, Any] = {
        "group": gate["group"],
        "kind": kind,
        "path": gate["path"],
    }
    if kind == "path_state":
        actual = "present" if value is not None else "absent"
        row.update(expected_state=gate["expected_state"], actual_state=actual)
        return actual == gate["expected_state"], row
    if kind == "sha256":
        actual_hash = _sha(value) if value is not None else None
        row.update(expected_sha256=gate["expected_sha256"], actual_sha256=actual_hash)
        return actual_hash == gate["expected_sha256"], row
    if kind == "exact_count":
        if value is None:
            actual = 0 if gate.get("allow_missing_path") is True else None
        else:
            actual = value.count(gate["needle"].encode("utf-8"))
        row.update(expected=gate["expected_count"], actual=actual)
        return actual == gate["expected_count"], row
    raise PatchPlanV3Error("patch_gate_kind_unsupported")


def _record_state(root: Path, record: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    groups: dict[str, list[bool]] = {"vulnerable": [], "fixed": []}
    gate_signatures: dict[str, set[str]] = {}
    for item in record["gate"]:
        signature = json.dumps({key: value for key, value in item.items() if key != "group"}, sort_keys=True)
        gate_signatures.setdefault(signature, set()).add(item["group"])
    vulnerable_match = False
    fixed_match = False
    for item in record["gate"]:
        matched, row = _gate(root, item)
        rows.append(row)
        groups[item["group"]].append(matched)
        signature = json.dumps({key: value for key, value in item.items() if key != "group"}, sort_keys=True)
        if matched and gate_signatures[signature] == {"vulnerable"}:
            vulnerable_match = True
        if matched and gate_signatures[signature] == {"fixed"}:
            fixed_match = True
    vulnerable = bool(groups["vulnerable"]) and all(groups["vulnerable"])
    fixed = bool(groups["fixed"]) and all(groups["fixed"])
    # A complete group is eligible only when no gate in the opposite group
    # matches.  Otherwise a partially mixed source could be mistaken for an
    # already-fixed or candidate state, depending on which group is complete.
    if vulnerable and not fixed_match:
        return "candidate", rows
    if fixed and not vulnerable_match:
        return "already", rows
    return "ambiguous", rows


def _patch_paths(payload: bytes, declared: list[str]) -> list[str]:
    try:
        text = payload.decode("utf-8", errors="surrogateescape")
    except Exception as exc:
        raise PatchPlanV3Error("patch_payload_decode_failed") from exc
    if any(token in text for token in ("deleted file mode ", "rename from ", "rename to ", "+++ /dev/null")):
        raise PatchPlanV3Error("patch_delete_or_rename_unsupported")
    pairs = _DIFF_RE.findall(payload)
    if not pairs:
        raise PatchPlanV3Error("patch_diff_missing")
    allowed = set(declared)
    found: list[str] = []
    for old_raw, new_raw in pairs:
        old = old_raw.decode("utf-8", errors="surrogateescape")
        new = new_raw.decode("utf-8", errors="surrogateescape")
        old = _relative(old)
        new = _relative(new)
        if old != new or old not in allowed:
            raise PatchPlanV3Error("patch_diff_path_not_declared")
        found.append(old)
    if len(set(found)) != len(found):
        raise PatchPlanV3Error("patch_diff_duplicate_path")
    for mode in re.findall(rb"^new file mode (\d+)$", payload, flags=re.MULTILINE):
        if mode not in {b"100644", b"100755"}:
            raise PatchPlanV3Error("patch_new_file_mode_unsupported")
    return found


def _payloads(plan: dict[str, Any]) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for row in plan["payloads"]:
        path = _relative(row["path"])
        try:
            content = base64.b64decode(row["content_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise PatchPlanV3Error("patch_payload_invalid_base64") from exc
        if _sha(content) != row["sha256"]:
            raise PatchPlanV3Error("patch_payload_sha256_mismatch")
        result[path] = content
    return result


def _validate_plan(plan: dict[str, Any]) -> str:
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise PatchPlanV3Error("patch_plan_schema_mismatch")
    contract = _contract()
    from mcd_agent.mautic_patch_resolution import _version_tuple
    if plan.get("trigger") not in contract["triggers"] or plan.get("phase") not in contract["phases"]:
        raise PatchPlanV3Error("patch_plan_trigger_phase_invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", str(plan.get("registry_commit", ""))) or not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("registry_sha256", ""))):
        raise PatchPlanV3Error("patch_plan_registry_invalid")
    source, target = plan.get("source_version"), plan.get("target_version")
    source_tuple = _version_tuple(str(source)) if source is not None else None
    target_tuple = _version_tuple(str(target)) if target is not None else None
    if plan.get("trigger") == "upgrade_lifecycle":
        if source_tuple is None or target_tuple is None or source_tuple >= target_tuple:
            raise PatchPlanV3Error("upgrade_plan_version_relation_invalid")
    elif not (source is None and target is None):
        if source_tuple is None or target_tuple is None or source_tuple != target_tuple:
            raise PatchPlanV3Error("remediation_plan_version_relation_invalid")
    try:
        _validate_plan_records(plan, contract, plan["trigger"], plan["phase"])
    except (KeyError, MauticPatchResolutionError) as exc:
        raise PatchPlanV3Error(str(exc) or "patch_plan_invalid") from exc
    if not _RUN_ID_RE.fullmatch(str(plan.get("run_id", ""))):
        raise PatchPlanV3Error("patch_run_id_invalid")
    if plan["operation"] not in {"status", "verify", "apply", "rollback"}:
        raise PatchPlanV3Error("patch_operation_invalid")
    if len(json.dumps(plan, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > int(contract["max_plan_bytes"]):
        raise PatchPlanV3Error("patch_plan_too_large")
    payloads = _payloads(plan)
    if source is None and not all(record.get("allow_unknown_version") is True for record in plan["patches"]):
        raise PatchPlanV3Error("unknown_version_plan_not_authorized")
    if sum(map(len, payloads.values())) > int(contract["max_payload_bytes"]):
        raise PatchPlanV3Error("patch_payload_too_large")
    if any(record["execution_kind"] != "git_patch_v1" for record in plan["patches"]):
        raise PatchPlanV3Error("patch_execution_kind_not_enabled")
    for record in plan["patches"]:
        payload = payloads.get(record.get("payload_path", ""))
        if payload is None:
            raise PatchPlanV3Error("patch_payload_reference_missing")
        _patch_paths(payload, record["source_paths"])
    return canonical_json_sha256(plan)


def _simulate(root: Path, plan: dict[str, Any], phase: str | None = None, facts: dict[str, Any] | None = None) -> tuple[Path, list[dict[str, Any]], dict[str, tuple[bytes | None, int]]]:
    temp = Path(tempfile.mkdtemp(prefix="mcd-patch-v3-"))
    try:
        return _simulate_inner(root, plan, phase, temp, facts)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _simulate_inner(root: Path, plan: dict[str, Any], phase: str | None, temp: Path, facts: dict[str, Any] | None = None) -> tuple[Path, list[dict[str, Any]], dict[str, tuple[bytes | None, int]]]:
    payloads = _payloads(plan)
    records = [record for record in plan["patches"] if phase is None or phase in record["phases"]]
    declared = list(dict.fromkeys(path for record in records for path in record["source_paths"]))
    modes: dict[str, int] = {}
    for relative in declared:
        source = _file(root, relative)
        content, st = _read(source)
        target = _file(temp, relative, create_parents=True)
        modes[relative] = stat.S_IMODE(st.st_mode) if st is not None else 0o644
        if content is not None and st is not None:
            target.write_bytes(content)
            os.chmod(target, stat.S_IMODE(st.st_mode))
    outcomes: list[dict[str, Any]] = []
    final: dict[str, tuple[bytes | None, int]] = {}
    for record in records:
        if record.get("preconditions"):
            if facts is None:
                raise PatchPlanV3Error("typed_facts_provider_required")
            if not facts["records"][record["id"]]["preconditions"]:
                outcomes.append({"id": record["id"], "decision": "skip_condition_not_required", "gates": [],
                                 "observations_sha256": facts["observations_sha256"]})
                continue
        state, gates = _record_state(temp, record)
        if state == "ambiguous":
            shutil.rmtree(temp, ignore_errors=True)
            raise PatchPlanV3Error(
                "patch_source_state_ambiguous",
                evidence={"completed": outcomes, "failure": {"id": record["id"], "gates": gates}},
            )
        if state == "already":
            outcomes.append({"id": record["id"], "decision": "already", "gates": gates})
            continue
        payload = payloads[record["payload_path"]]
        patch_paths = _patch_paths(payload, record["source_paths"])
        check = subprocess.run(
            ["git", "apply", "--binary", "--check"],
            cwd=temp,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if check.returncode != 0:
            shutil.rmtree(temp, ignore_errors=True)
            raise PatchPlanV3Error(
                "patch_apply_check_failed",
                evidence={"completed": outcomes, "failure": {"id": record["id"], "gates": gates}},
            )
        applied = subprocess.run(
            ["git", "apply", "--binary"],
            cwd=temp,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if applied.returncode != 0:
            shutil.rmtree(temp, ignore_errors=True)
            raise PatchPlanV3Error(
                "patch_apply_simulation_failed",
                evidence={"completed": outcomes, "failure": {"id": record["id"], "gates": gates}},
            )
        for relative in patch_paths:
            blocks = list(_DIFF_RE.finditer(payload))
            block = next(payload[match.start():blocks[index + 1].start() if index + 1 < len(blocks) else len(payload)]
                         for index, match in enumerate(blocks) if match.group(1).decode("utf-8", errors="surrogateescape") == relative)
            new_mode = re.search(rb"^new file mode (\d+)$", block, flags=re.MULTILINE)
            changed_mode = re.search(rb"^new mode (\d+)$", block, flags=re.MULTILINE)
            if new_mode:
                modes[relative] = int(new_mode.group(1), 8) & 0o777
            elif changed_mode:
                modes[relative] = int(changed_mode.group(1), 8) & 0o777
        after_state, after_gates = _record_state(temp, record)
        if after_state != "already":
            shutil.rmtree(temp, ignore_errors=True)
            raise PatchPlanV3Error(
                "patch_fixed_gate_failed",
                evidence={
                    "completed": outcomes,
                    "failure": {"id": record["id"], "gates": after_gates},
                },
            )
        outcomes.append({"id": record["id"], "decision": "candidate", "gates": gates})
    for relative in declared:
        path = _file(temp, relative)
        content, _st = _read(path)
        final[relative] = (content, modes[relative])
    return temp, outcomes, final


def _ledger_dir(root: Path) -> Path:
    first = root / ".mcd"
    if first.is_symlink():
        raise PatchPlanV3Error("patch_ledger_path_symlink")
    second = first / "patch-plan-v3"
    if second.is_symlink():
        raise PatchPlanV3Error("patch_ledger_path_symlink")
    first.mkdir(mode=0o700, exist_ok=True)
    second.mkdir(mode=0o700, exist_ok=True)
    return second


def _ledger_path(root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", run_id):
        raise PatchPlanV3Error("patch_run_id_invalid")
    return _ledger_dir(root) / f"{run_id}.json"


def _save_ledger(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _load_ledger(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PatchPlanV3Error("patch_rollback_ledger_missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PatchPlanV3Error("patch_rollback_ledger_invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != "mcd-mautic-patch-ledger-v3":
        raise PatchPlanV3Error("patch_rollback_ledger_invalid")
    return value


def _snapshot(root: Path, relative: str, expected_after: bytes | None) -> dict[str, Any]:
    path = _file(root, relative)
    before, st = _read(path)
    if before is None:
        parent = path.parent
        while parent != root and not parent.exists():
            parent = parent.parent
        pst = parent.stat()
        mode = 0o644
        uid, gid = pst.st_uid, pst.st_gid
    else:
        mode = stat.S_IMODE(st.st_mode)
        uid, gid = st.st_uid, st.st_gid
    return {
        "path": relative,
        "existed": before is not None,
        "before_base64": base64.b64encode(before).decode("ascii") if before is not None else None,
        "before_sha256": _sha(before) if before is not None else None,
        "mode": mode,
        "uid": uid,
        "gid": gid,
        "after_sha256": _sha(expected_after) if expected_after is not None else None,
    }


def _atomic_replace(path: Path, content: bytes, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise PatchPlanV3Error("patch_path_symlink")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.mcd-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        try:
            os.chown(name, uid, gid)
        except PermissionError:
            current = os.stat(name)
            if (current.st_uid, current.st_gid) != (uid, gid):
                raise
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _current_sha(path: Path) -> str | None:
    value, _st = _read(path)
    return _sha(value) if value is not None else None


def _restore_ledger(root: Path, ledger: dict[str, Any], *, allow_pre_state: bool = False) -> dict[str, Any]:
    snapshots = ledger.get("snapshots")
    if not isinstance(snapshots, list):
        raise PatchPlanV3Error("patch_rollback_ledger_invalid")
    resolved: list[tuple[dict[str, Any], Path, str | None]] = []
    for item in snapshots:
        if not isinstance(item, dict) or not _relative(item.get("path")):
            raise PatchPlanV3Error("patch_rollback_ledger_invalid")
        path = _file(root, item["path"])
        actual = _current_sha(path)
        before_sha = item.get("before_sha256")
        after_sha = item.get("after_sha256")
        if actual == before_sha and allow_pre_state:
            resolved.append((item, path, actual))
        elif actual != after_sha:
            raise PatchPlanV3Error("patch_rollback_after_hash_mismatch")
        else:
            resolved.append((item, path, actual))
    restored: list[str] = []
    for item, path, actual in reversed(resolved):
        if actual == item.get("before_sha256") and allow_pre_state:
            continue
        if item.get("existed") is True:
            try:
                content = base64.b64decode(item["before_base64"], validate=True)
            except (ValueError, TypeError, KeyError) as exc:
                raise PatchPlanV3Error("patch_rollback_snapshot_invalid") from exc
            if _sha(content) != item.get("before_sha256"):
                raise PatchPlanV3Error("patch_rollback_snapshot_hash_mismatch")
            _atomic_replace(path, content, int(item["mode"]), int(item["uid"]), int(item["gid"]))
        else:
            if _current_sha(path) != item.get("after_sha256"):
                raise PatchPlanV3Error("patch_rollback_after_hash_mismatch")
            path.unlink()
        restored.append(item["path"])
    ledger["status"] = "rolled_back"
    ledger["restored"] = restored
    return {"status": "success", "restored": restored}


def _legacy_snapshot(root: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    descriptor = record.get("legacy_state")
    if not descriptor:
        return None
    source_rel = record["source_paths"][0]
    source = _file(root, source_rel)
    current, current_st = _read(source)
    if current is None or current_st is None:
        raise PatchPlanV3Error("legacy_state_current_missing")
    backup = _file(root, source_rel + descriptor["backup_suffix"])
    original, backup_st = _read(backup)
    if original is None or backup_st is None:
        if descriptor["kind"] == "backup_file" or not _file(root, source_rel + descriptor["metadata_suffix"]).exists():
            return None
        raise PatchPlanV3Error("legacy_state_backup_missing")
    temp = Path(tempfile.mkdtemp(prefix="mcd-legacy-gate-"))
    try:
        candidate_path = _file(temp, source_rel, create_parents=True)
        candidate_path.write_bytes(original)
        fixed_state, _ = _record_state(root, record)
        vulnerable_state, _ = _record_state(temp, record)
        if fixed_state != "already" or vulnerable_state != "candidate":
            raise PatchPlanV3Error("legacy_state_gates_mismatch")
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    original_sha = _sha(original)
    current_sha = _sha(current)
    if descriptor["kind"] == "backup_file_with_metadata":
        metadata_path = _file(root, source_rel + descriptor["metadata_suffix"])
        raw, _metadata_st = _read(metadata_path)
        try:
            metadata = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (UnicodeDecodeError, ValueError) as exc:
            raise PatchPlanV3Error("legacy_state_metadata_invalid") from exc
        fields = descriptor["metadata_fields"]
        if (
            not isinstance(metadata, dict)
            or metadata.get(fields["marker"]) != descriptor["expected_marker"]
            or metadata.get(fields["path"]) != str(source.resolve())
            or metadata.get(fields["original_sha256"]) != original_sha
            or metadata.get(fields["applied_sha256"]) != current_sha
        ):
            raise PatchPlanV3Error("legacy_state_metadata_mismatch")
    return {
        "path": source_rel,
        "existed": True,
        "before_base64": base64.b64encode(original).decode("ascii"),
        "before_sha256": original_sha,
        "mode": stat.S_IMODE(backup_st.st_mode),
        "uid": backup_st.st_uid,
        "gid": backup_st.st_gid,
        "after_sha256": current_sha,
        "legacy_import": True,
    }


def atomic_preflight(root_value: str, plan_value: dict[str, Any] | str, *, facts_provider=None) -> dict[str, Any]:
    """Validate the entire immutable plan in a disposable source tree."""
    plan = json.loads(plan_value) if isinstance(plan_value, str) else plan_value
    digest = _validate_plan(plan)
    root = _root(root_value)
    facts = _observe_facts(plan, facts_provider, "verify", plan["phase"])
    temp, outcomes, _final = _simulate(root, plan, facts=facts)
    shutil.rmtree(temp, ignore_errors=True)
    return {"schema": "mcd-mautic-patch-preflight-v3", "status": "success",
            "operation": "patch_preflight", "run_id": plan["run_id"],
            "plan_sha256": digest, "selected": [row["id"] for row in outcomes],
            "patches": outcomes, "facts_receipt": facts, "upgrade_started": False, "rollback_attempted": False}


def verify_applied(root_value: str, plan_value: dict[str, Any] | str, *, accepted_facts=None) -> dict[str, Any]:
    plan = json.loads(plan_value) if isinstance(plan_value, str) else plan_value
    digest = _validate_plan(plan)
    root = _root(root_value)
    outcomes = []
    for record in plan["patches"]:
        if record.get("preconditions") and accepted_facts is not None and not accepted_facts["records"][record["id"]]["preconditions"]:
            outcomes.append({"id": record["id"], "decision": "skip_condition_not_required", "gates": []})
            continue
        state, gates = _record_state(root, record)
        if state != "already":
            raise PatchPlanV3Error("patch_final_fixed_gate_failed")
        outcomes.append({"id": record["id"], "decision": "already", "gates": gates})
    return {"status": "success", "operation": "verify_applied", "run_id": plan["run_id"],
            "plan_sha256": digest, "patches": outcomes}


def _execute(root_value: str, plan_value: dict[str, Any] | str, *, phase: str | None = None, observed_major: int | None = None, facts_provider=None, accepted_facts=None) -> dict[str, Any]:
    try:
        plan = json.loads(plan_value) if isinstance(plan_value, str) else plan_value
    except ValueError as exc:
        raise PatchPlanV3Error("patch_plan_invalid_json") from exc
    plan_sha = _validate_plan(plan)
    root = _root(root_value)
    if plan["source_version"] is None and (type(observed_major) is not int or observed_major not in range(1, 100)):
        raise PatchPlanV3Error("unknown_version_requires_independently_confirmed_major")
    selected_phase = phase or plan["phase"]
    facts = _observe_facts(plan, facts_provider, plan["operation"], selected_phase)
    if facts is not None:
        from mcd_agent.mautic_patch_fact_binding import require_receipt
        if plan["operation"] == "apply":
            if accepted_facts is None:
                raise PatchPlanV3Error("typed_facts_admission_required")
            try:
                require_receipt(accepted_facts, facts)
            except ValueError as exc:
                raise PatchPlanV3Error(str(exc)) from exc
    if selected_phase not in _contract()["phases"] or (plan["trigger"] != "upgrade_lifecycle" and selected_phase != plan["phase"]):
        raise PatchPlanV3Error("patch_execution_phase_invalid")
    if plan["trigger"] == "upgrade_lifecycle" and phase is None and plan["operation"] == "apply":
        raise PatchPlanV3Error("upgrade_execution_phase_required")
    records = [record for record in plan["patches"] if selected_phase in record["phases"]]
    if not records and plan["operation"] != "rollback":
        return {"status": "success", "operation": plan["operation"], "run_id": plan["run_id"],
                "plan_sha256": plan_sha, "phase": selected_phase, "selected": [], "patches": [], "rollback_available": False}
    ledger_run_id = plan["run_id"] + ("." + selected_phase if plan["trigger"] == "upgrade_lifecycle" else "")
    operation = plan["operation"]
    if operation == "rollback":
        return _rollback(root_value, plan, phase=phase, facts=facts)
    if operation in {"status", "verify"}:
        temp, outcomes, _final = _simulate(root, plan, selected_phase, facts)
        shutil.rmtree(temp, ignore_errors=True)
        for outcome in outcomes:
            record = next(row for row in plan["patches"] if row["id"] == outcome["id"])
            if outcome["decision"] == "already" and record.get("legacy_state"):
                outcome["legacy_rollback_source_validated"] = _legacy_snapshot(root, record) is not None
        return {
            "schema": "mcd-mautic-patch-preflight-v3",
            "status": "success",
            "operation": operation,
            "run_id": plan["run_id"],
            "plan_sha256": plan_sha,
            "selected": [row["id"] for row in outcomes],
            "patches": outcomes,
            "facts_receipt": facts,
            "rollback_available": False,
        }
    if operation != "apply":
        raise PatchPlanV3Error("patch_operation_unsupported")
    temp, outcomes, final = _simulate(root, plan, selected_phase, facts)
    try:
        records_by_path: dict[str, dict[str, Any]] = {}
        for record in records:
            if any(row["id"] == record["id"] and row["decision"] == "candidate" for row in outcomes):
                for path in _patch_paths(_payloads(plan)[record["payload_path"]], record["source_paths"]):
                    records_by_path[path] = record
        snapshots = []
        for relative in records_by_path:
            expected, _mode = final[relative]
            snapshots.append(_snapshot(root, relative, expected))
        ledger = {
            "schema": "mcd-mautic-patch-ledger-v3",
            "run_id": plan["run_id"],
            "plan_sha256": plan_sha,
            "plan_binding_sha256": canonical_json_sha256(dict(plan, operation="apply")),
            "registry_commit": plan["registry_commit"],
            "registry_sha256": plan["registry_sha256"],
            "trigger": plan["trigger"],
            "phase": selected_phase,
            "operation": "apply",
            "status": "prepared",
            "snapshots": snapshots,
            "facts_receipt": facts,
        }
        if not snapshots:
            # No source changes. Preserve rollback for a catalog-declared legacy state only.
            for record in records:
                if record.get("legacy_state"):
                    imported = _legacy_snapshot(root, record)
                    if imported:
                        snapshots.append(imported)
            ledger["snapshots"] = snapshots
        ledger_path = _ledger_path(root, ledger_run_id)
        if ledger_path.exists():
            existing = _load_ledger(ledger_path)
            if existing.get("plan_sha256") != plan_sha:
                raise PatchPlanV3Error("patch_run_id_reused")
            if existing.get("status") != "applied" or any(_current_sha(_file(root, item["path"])) != item["after_sha256"] for item in existing.get("snapshots", [])):
                raise PatchPlanV3Error("patch_existing_ledger_state_mismatch")
            return {"status": "success", "operation": "apply", "run_id": plan["run_id"], "plan_sha256": plan_sha, "patches": outcomes, "rollback_available": bool(existing.get("snapshots"))}
        _save_ledger(ledger_path, ledger)
        if facts is not None:
            from mcd_agent.mautic_patch_fact_binding import require_receipt
            try:
                require_receipt(facts, _observe_facts(plan, facts_provider, "apply", selected_phase))
            except ValueError as exc:
                raise PatchPlanV3Error(str(exc)) from exc
        for item in snapshots:
            path = _file(root, item["path"], create_parents=True)
            expected_before = item.get("before_sha256")
            if _current_sha(path) != expected_before:
                raise PatchPlanV3Error("patch_source_changed_after_preflight")
        for relative in records_by_path:
            after_bytes, mode = final[relative]
            if after_bytes is None:
                raise PatchPlanV3Error("patch_delete_unsupported")
            item = next(row for row in snapshots if row["path"] == relative)
            path = _file(root, relative, create_parents=True)
            _atomic_replace(path, after_bytes, mode, int(item["uid"]), int(item["gid"]))
        for item in snapshots:
            path = _file(root, item["path"])
            if _current_sha(path) != item["after_sha256"]:
                raise PatchPlanV3Error("patch_after_hash_mismatch")
        ledger["status"] = "applied"
        _save_ledger(ledger_path, ledger)
        for outcome in outcomes:
            record = next(record for record in records if record["id"] == outcome["id"])
            related = [item for item in snapshots if item["path"] in record["source_paths"]]
            outcome["before_sha256"] = {item["path"]: item["before_sha256"] for item in related}
            outcome["after_sha256"] = {item["path"]: item["after_sha256"] for item in related}
            if outcome["decision"] == "candidate":
                outcome["decision"] = "applied"
        return {
            "schema": "mcd-mautic-patch-preflight-v3",
            "status": "success",
            "operation": "apply",
            "run_id": plan["run_id"],
            "plan_sha256": plan_sha,
            "selected": [row["id"] for row in outcomes],
            "patches": outcomes,
            "facts_receipt": facts,
            "rollback_available": bool(snapshots),
            "rollback_attempted": False,
            "rollback_succeeded": True,
        }
    except Exception:
        try:
            path = _ledger_path(root, ledger_run_id)
            if path.exists():
                failed_ledger = _load_ledger(path)
                if failed_ledger.get("status") == "prepared" and failed_ledger.get("plan_sha256") == plan_sha and failed_ledger.get("snapshots"):
                    _restore_ledger(root, failed_ledger, allow_pre_state=True)
                    _save_ledger(path, failed_ledger)
        except Exception as rollback_exc:
            shutil.rmtree(temp, ignore_errors=True)
            raise PatchPlanV3Error(f"patch_apply_failed_rollback_failed:{rollback_exc}")
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _rollback(root_value: str, plan_value: dict[str, Any] | str, *, phase: str | None = None, facts=None) -> dict[str, Any]:
    try:
        plan = json.loads(plan_value) if isinstance(plan_value, str) else plan_value
    except ValueError as exc:
        raise PatchPlanV3Error("patch_plan_invalid_json") from exc
    _validate_plan(plan)
    if plan["operation"] != "rollback":
        raise PatchPlanV3Error("patch_rollback_operation_required")
    root = _root(root_value)
    selected_phase = phase or plan["phase"]
    if facts is not None and any(not row["rollback_preconditions"] for row in facts["records"].values()):
        raise PatchPlanV3Error("patch_rollback_dependent_database_barrier")
    if plan["trigger"] == "upgrade_lifecycle" and phase is None:
        phases = list(dict.fromkeys(item for record in plan["patches"] for item in record["phases"]))
        ledgers = []
        for item in reversed(phases):
            ledger_file = root / ".mcd" / "patch-plan-v3" / (plan["run_id"] + "." + item + ".json")
            if ledger_file.exists():
                ledgers.append((ledger_file, _load_ledger(ledger_file)))
        _preflight_restore(root, plan, ledgers, facts)
        restored = []
        for ledger_file, ledger in ledgers:
            restored.extend(_restore_ledger(root, ledger, allow_pre_state=ledger.get("status") == "rolled_back")["restored"])
            _save_ledger(ledger_file, ledger)
        return {"schema": "mcd-mautic-patch-preflight-v3", "status": "success",
                "operation": "rollback", "run_id": plan["run_id"],
                "plan_sha256": canonical_json_sha256(dict(plan, operation="apply")),
                "restored": restored, "rollback_attempted": bool(restored),
                "rollback_succeeded": True}
    path = _ledger_path(root, plan["run_id"] + ("." + selected_phase if plan["trigger"] == "upgrade_lifecycle" else ""))
    if not path.exists():
        snapshots = []
        for record in plan["patches"]:
            if record.get("legacy_state"):
                imported = _legacy_snapshot(root, record)
                if imported:
                    snapshots.append(imported)
        if not snapshots:
            raise PatchPlanV3Error("patch_rollback_ledger_missing")
        ledger = {
            "schema": "mcd-mautic-patch-ledger-v3",
            "run_id": plan["run_id"],
            "plan_sha256": canonical_json_sha256(plan),
            "plan_binding_sha256": canonical_json_sha256(dict(plan, operation="apply")),
            "registry_commit": plan["registry_commit"],
            "registry_sha256": plan["registry_sha256"],
            "trigger": plan["trigger"],
            "phase": selected_phase,
            "operation": "apply",
            "status": "legacy_imported",
            "snapshots": snapshots,
        }
        _save_ledger(path, ledger)
    else:
        ledger = _load_ledger(path)
    _preflight_restore(root, plan, [(path, ledger)], facts)
    if ledger.get("registry_commit") != plan["registry_commit"] or ledger.get("registry_sha256") != plan["registry_sha256"]:
        raise PatchPlanV3Error("patch_rollback_registry_mismatch")
    if ledger.get("plan_binding_sha256") != canonical_json_sha256(dict(plan, operation="apply")):
        raise PatchPlanV3Error("patch_rollback_plan_mismatch")
    if ledger.get("trigger") != plan["trigger"] or ledger.get("phase") != selected_phase:
        raise PatchPlanV3Error("patch_rollback_context_mismatch")
    records = [record for record in plan["patches"] if selected_phase in record["phases"]]
    for record in records:
        mutated = set(_patch_paths(_payloads(plan)[record["payload_path"]], record["source_paths"]))
        if any(not _gate(root, gate)[0] for gate in record["gate"] if gate["group"] == "fixed" and gate["path"] not in mutated):
            raise PatchPlanV3Error("patch_rollback_identity_gate_mismatch")
    result = _restore_ledger(root, ledger, allow_pre_state=ledger.get("status") == "rolled_back")
    _save_ledger(path, ledger)
    return {
        "schema": "mcd-mautic-patch-preflight-v3",
        "status": "success",
        "operation": "rollback",
        "run_id": plan["run_id"],
        "plan_sha256": canonical_json_sha256(dict(plan, operation="apply")),
        "rollback_attempted": True,
        "rollback_succeeded": True,
        "restored": result["restored"],
    }


def _observe_facts(plan, provider, operation, phase):
    from mcd_agent.mautic_patch_fact_binding import needs_facts
    if not needs_facts(plan):
        return None
    if provider is None:
        raise PatchPlanV3Error("typed_facts_provider_required")
    try:
        return provider(plan, operation, phase)
    except (ValueError, OSError) as exc:
        raise PatchPlanV3Error(str(exc)) from exc


def _preflight_restore(root, plan, ledgers, facts):
    """Verify the complete reverse hash chain and all receipts before writing."""
    from mcd_agent.mautic_patch_fact_binding import require_receipt
    virtual = {}
    mutated = {item["path"] for _path, ledger in ledgers for item in ledger.get("snapshots", [])}
    applied_phases = {ledger.get("phase") for _path, ledger in ledgers}
    for _path, ledger in ledgers:
        if (ledger.get("plan_binding_sha256") != canonical_json_sha256(dict(plan, operation="apply"))
                or ledger.get("run_id") != plan["run_id"] or ledger.get("trigger") != plan["trigger"]):
            raise PatchPlanV3Error("patch_rollback_plan_mismatch")
        if facts is not None:
            try:
                require_receipt(ledger.get("facts_receipt"), facts)
            except ValueError as exc:
                raise PatchPlanV3Error(str(exc)) from exc
        for item in reversed(ledger.get("snapshots", [])):
            path = _file(root, item["path"])
            actual = virtual.get(item["path"], _current_sha(path))
            if ledger.get("status") == "rolled_back" and actual == item["before_sha256"]:
                continue
            if actual != item["after_sha256"]:
                raise PatchPlanV3Error("patch_rollback_after_hash_mismatch")
            if item.get("existed"):
                try:
                    before = base64.b64decode(item["before_base64"], validate=True)
                except (ValueError, TypeError, KeyError) as exc:
                    raise PatchPlanV3Error("patch_rollback_snapshot_invalid") from exc
                if _sha(before) != item["before_sha256"]:
                    raise PatchPlanV3Error("patch_rollback_snapshot_hash_mismatch")
            virtual[item["path"]] = item["before_sha256"]
    for record in plan["patches"]:
        if not applied_phases.intersection(record["phases"]) or not mutated.intersection(record["source_paths"]):
            continue
        if any(not _gate(root, gate)[0] for gate in record["gate"] if gate["group"] == "fixed" and gate["path"] not in mutated):
            raise PatchPlanV3Error("patch_rollback_identity_gate_mismatch")


def execute(root_value: str, plan_value: dict[str, Any] | str, *, phase: str | None = None, observed_major: int | None = None, facts_provider=None, accepted_facts=None) -> dict[str, Any]:
    plan = json.loads(plan_value) if isinstance(plan_value, str) else plan_value
    _validate_plan(plan)
    if plan["operation"] in {"status", "verify"}:
        return _execute(root_value, plan, phase=phase, observed_major=observed_major, facts_provider=facts_provider, accepted_facts=accepted_facts)
    root = _root(root_value)
    path = _ledger_dir(root) / ".execution.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _execute(root_value, plan, phase=phase, observed_major=observed_major, facts_provider=facts_provider, accepted_facts=accepted_facts)
    finally:
        os.close(fd)


def rollback(root_value: str, plan_value: dict[str, Any] | str, *, phase: str | None = None, facts_provider=None) -> dict[str, Any]:
    plan = json.loads(plan_value) if isinstance(plan_value, str) else plan_value
    if plan.get("operation") != "rollback":
        raise PatchPlanV3Error("patch_rollback_operation_required")
    return execute(root_value, plan, phase=phase, facts_provider=facts_provider)
