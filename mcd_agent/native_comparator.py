"""Bounded native-vs-MCD comparison lifecycle.

This module records evidence around an externally scheduled native cycle.  It
does not schedule work, render commands, or claim that a native command
processed an object merely because the process returned zero.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Iterator, Mapping
import uuid

SCHEMA = "mcd-native-comparator-receipt-v1"
CAPABILITY = "mcd-native-comparator-v1"
OPERATIONS = frozenset({"segment_update", "campaign_rebuild", "campaign_trigger"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ROOT = re.compile(r"^/[A-Za-z0-9._/-]+$")
_MAX_OBJECTS = 10_000
_MAX_TEXT = 512


class ComparatorError(ValueError):
    """Invalid or unsafe comparator evidence."""


class ComparatorBusy(ComparatorError):
    """An overlapping native cycle or non-quiescent instance was observed."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ComparatorError("evidence_not_canonicalizable") from exc


def evidence_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def shared_root_fence_path(root: str, lock_directory: str = "/run/lock/mcd-tasks") -> Path:
    root = _root(root)
    digest = hashlib.sha256(f"{root}|native-fence".encode("utf-8")).hexdigest()
    return Path(lock_directory) / f"{digest}.lock"


def campaign_root_fence_path(root: str, lock_directory: str = "/run/lock/mcd-tasks") -> Path:
    root = _root(root)
    digest = hashlib.sha256(f"{root}|campaign-root".encode("utf-8")).hexdigest()
    return Path(lock_directory) / f"{digest}.lock"


def _text(value: Any, field: str, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ComparatorError(f"{field}_required")
    if len(result) > _MAX_TEXT:
        raise ComparatorError(f"{field}_too_long")
    return result


def _root(value: Any) -> str:
    result = _text(value, "root")
    if not _ROOT.fullmatch(result) or "//" in result or "/../" in f"{result}/":
        raise ComparatorError("root_not_canonical")
    return result.rstrip("/") or "/"


def _operation(value: Any) -> str:
    result = _text(value, "operation")
    if result == "campaign_update":
        result = "campaign_rebuild"
    if result not in OPERATIONS:
        raise ComparatorError("operation_unsupported")
    return result


def _object_id(value: Any) -> str:
    result = _text(value, "object_id")
    if not _ID.fullmatch(result):
        raise ComparatorError("object_id_invalid")
    return result


def _timestamp(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComparatorError(f"{field}_invalid")
    result = float(value)
    if result < 0:
        raise ComparatorError(f"{field}_invalid")
    return result


def _bounded_ids(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_OBJECTS:
        raise ComparatorError(f"{field}_invalid")
    return [_object_id(item) for item in value]


def _fingerprint(value: Any, field: str) -> str:
    result = _text(value, field)
    if not _HEX64.fullmatch(result):
        raise ComparatorError(f"{field}_invalid")
    return result


def _optional_fingerprint(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    return _fingerprint(value, field)


def _json(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComparatorError(f"{field}_invalid")
    encoded = _canonical(value)
    if len(encoded) > 32_768:
        raise ComparatorError(f"{field}_too_large")
    return json.loads(encoded)


def _normalise_pre(row: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "object_id", "eligible", "due", "input_generation", "watermark",
        "input_fingerprint", "due_since", "obligation_deadline",
        "last_eligible_observed_at", "history_complete_until", "observed_at", "mcd_decision",
        "mcd_start_generation", "mcd_result_generation", "mcd_result_fingerprint", "pending_event_ids",
        "complete",
    }
    if set(row) != required:
        raise ComparatorError("predecision_fields_invalid")
    object_id = _object_id(row["object_id"])
    booleans = ("eligible", "due", "complete")
    if any(type(row[key]) is not bool for key in booleans):
        raise ComparatorError("predecision_boolean_invalid")
    generation = _text(row["input_generation"], "input_generation")
    watermark = _text(row["watermark"], "watermark")
    if len(generation) > _MAX_TEXT or len(watermark) > _MAX_TEXT:
        raise ComparatorError("predecision_generation_invalid")
    decision = _text(row["mcd_decision"], "mcd_decision")
    if decision not in {"started", "completed", "failed", "queued", "not_started", "paused", "unknown"}:
        raise ComparatorError("mcd_decision_invalid")
    for key in ("mcd_start_generation", "mcd_result_generation"):
        if row[key] not in (None, ""):
            _text(row[key], key)
    if row["mcd_result_fingerprint"] not in (None, ""):
        _fingerprint(row["mcd_result_fingerprint"], "mcd_result_fingerprint")
    return {
        "object_id": object_id,
        "eligible": row["eligible"],
        "due": row["due"],
        "input_generation": generation,
        "watermark": watermark,
        "input_fingerprint": _fingerprint(row["input_fingerprint"], "input_fingerprint"),
        "due_since": None if row["due_since"] is None else _timestamp(row["due_since"], "due_since"),
        "obligation_deadline": _timestamp(row["obligation_deadline"], "obligation_deadline"),
        "last_eligible_observed_at": _timestamp(row["last_eligible_observed_at"], "last_eligible_observed_at"),
        "history_complete_until": _timestamp(row["history_complete_until"], "history_complete_until"),
        "observed_at": _timestamp(row["observed_at"], "observed_at"),
        "mcd_decision": decision,
        "mcd_start_generation": None if row["mcd_start_generation"] in (None, "") else str(row["mcd_start_generation"]),
        "mcd_result_generation": None if row["mcd_result_generation"] in (None, "") else str(row["mcd_result_generation"]),
        "mcd_result_fingerprint": None if row["mcd_result_fingerprint"] in (None, "") else str(row["mcd_result_fingerprint"]),
        "pending_event_ids": _bounded_ids(row["pending_event_ids"], "pending_event_ids"),
        "complete": row["complete"],
    }


def _normalise_after(row: Mapping[str, Any]) -> dict[str, Any]:
    required = {"object_id", "source_input_generation", "source_input_fingerprint", "output_fingerprint", "eligible", "due", "observed_at"}
    if set(row) != required:
        raise ComparatorError("after_fields_invalid")
    if type(row["eligible"]) is not bool or type(row["due"]) is not bool:
        raise ComparatorError("after_boolean_invalid")
    return {
        "object_id": _object_id(row["object_id"]),
        "source_input_generation": _text(row["source_input_generation"], "after_source_input_generation"),
        "source_input_fingerprint": _fingerprint(row["source_input_fingerprint"], "after_source_input_fingerprint"),
        "output_fingerprint": _fingerprint(row["output_fingerprint"], "after_output_fingerprint"),
        "eligible": row["eligible"],
        "due": row["due"],
        "observed_at": _timestamp(row["observed_at"], "after_observed_at"),
    }


@dataclass(frozen=True)
class CycleLease:
    cycle_id: str
    lock_path: str
    fd: int


class NativeComparator:
    """Durable evidence store for an externally scheduled native cycle."""

    def __init__(self, database: str, lock_directory: str):
        self.database = str(database)
        self.lock_directory = Path(lock_directory)
        self.lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS native_comparator_cycles (
                  cycle_id TEXT PRIMARY KEY,
                  instance_uid TEXT NOT NULL,
                  root TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  window_start REAL NOT NULL,
                  window_end REAL NOT NULL,
                  pause_start REAL,
                  pause_end REAL,
                  allowlist_sha256 TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  preflight_sha256 TEXT,
                  receipt_json TEXT,
                  receipt_sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS native_comparator_objects (
                  cycle_id TEXT NOT NULL,
                  object_id TEXT NOT NULL,
                  pre_json TEXT NOT NULL,
                  start_json TEXT,
                  result_json TEXT,
                  classification TEXT,
                  reason TEXT,
                  PRIMARY KEY (cycle_id, object_id),
                  FOREIGN KEY (cycle_id) REFERENCES native_comparator_cycles(cycle_id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def assert_shared_fence_available(self, root: str) -> None:
        """Fail closed when the production daemon fence cannot be used."""
        _root(root)
        production_lock_dir = Path("/run/lock/mcd-tasks")
        if self.lock_directory == production_lock_dir:
            flock_bin = Path("/usr/bin/flock")
            if not flock_bin.is_file() or not os.access(flock_bin, os.X_OK):
                raise ComparatorError("shared_fence_flock_unavailable")
        try:
            self.lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = shared_root_fence_path(root, str(self.lock_directory))
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    return
            finally:
                os.close(fd)
        except (OSError, ValueError) as exc:
            raise ComparatorError("shared_fence_unusable") from exc

    def begin_cycle(
        self,
        *,
        cycle_id: str,
        instance_uid: str,
        root: str,
        operation: str,
        window_start: float,
        window_end: float,
        allowlist: list[str],
        pause_start: float | None = None,
        pause_end: float | None = None,
    ) -> str:
        cycle_id = _text(cycle_id, "cycle_id")
        instance_uid = _text(instance_uid, "instance_uid")
        root = _root(root)
        operation = _operation(operation)
        start = _timestamp(window_start, "window_start")
        end = _timestamp(window_end, "window_end")
        if end <= start:
            raise ComparatorError("window_invalid")
        objects = list(dict.fromkeys(_object_id(item) for item in allowlist))
        if len(objects) > _MAX_OBJECTS:
            raise ComparatorError("allowlist_invalid")
        if (pause_start is None) != (pause_end is None):
            raise ComparatorError("pause_window_invalid")
        if pause_start is not None:
            pause_start = _timestamp(pause_start, "pause_start")
            pause_end = _timestamp(pause_end, "pause_end")
            if pause_end <= pause_start:
                raise ComparatorError("pause_window_invalid")
        allowlist_sha = evidence_sha256(objects)
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone():
                raise ComparatorError("cycle_id_reused")
            overlap = conn.execute(
                """SELECT 1 FROM native_comparator_cycles
                   WHERE root=? AND operation=? AND state IN ('created','preflighted','running')
                     AND window_start < ? AND window_end > ?""",
                (root, operation, end, start),
            ).fetchone()
            if overlap:
                raise ComparatorBusy("overlapping_cycle")
            conn.execute(
                """INSERT INTO native_comparator_cycles
                   (cycle_id,instance_uid,root,operation,window_start,window_end,
                    pause_start,pause_end,allowlist_sha256,state,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (cycle_id, instance_uid, root, operation, start, end, pause_start,
                 pause_end, allowlist_sha, "created", now),
            )
        return cycle_id

    def preflight(self, cycle_id: str, observations: list[Mapping[str, Any]]) -> str:
        if not isinstance(observations, list) or len(observations) > _MAX_OBJECTS:
            raise ComparatorError("preflight_observations_invalid")
        rows = [_normalise_pre(item) for item in observations]
        ids = [row["object_id"] for row in rows]
        if len(set(ids)) != len(ids):
            raise ComparatorError("preflight_duplicate_object")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cycle = conn.execute("SELECT * FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle or cycle["state"] != "created":
                raise ComparatorError("preflight_cycle_state_invalid")
            allowlist = conn.execute("SELECT allowlist_sha256 FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()[0]
            if evidence_sha256(ids) != allowlist:
                raise ComparatorError("preflight_allowlist_mismatch")
            for row in rows:
                if row["observed_at"] > cycle["window_start"]:
                    raise ComparatorError("preflight_after_window_start")
                if row["due_since"] is not None and row["due_since"] > row["observed_at"]:
                    raise ComparatorError("due_history_temporal_invalid")
                if row["obligation_deadline"] < (row["due_since"] or row["observed_at"]):
                    raise ComparatorError("obligation_deadline_invalid")
                if row["last_eligible_observed_at"] > row["observed_at"] or row["history_complete_until"] > row["observed_at"]:
                    raise ComparatorError("obligation_history_temporal_invalid")
            for row in rows:
                conn.execute(
                    "INSERT INTO native_comparator_objects(cycle_id,object_id,pre_json) VALUES (?,?,?)",
                    (cycle_id, row["object_id"], _canonical(row).decode("utf-8")),
                )
            digest = evidence_sha256(rows)
            conn.execute(
                "UPDATE native_comparator_cycles SET state='preflighted',preflight_sha256=? WHERE cycle_id=?",
                (digest, cycle_id),
            )
        return digest

    def _lock_path(self, cycle: sqlite3.Row) -> Path:
        if str(cycle["operation"]) in {"campaign_rebuild", "campaign_trigger"}:
            return campaign_root_fence_path(str(cycle["root"]), str(self.lock_directory))
        return shared_root_fence_path(str(cycle["root"]), str(self.lock_directory))

    @contextmanager
    def native_window(self, cycle_id: str, quiescence_probe: Callable[[], bool]) -> Iterator[CycleLease]:
        with self._connect() as conn:
            cycle = conn.execute("SELECT * FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle or cycle["state"] != "preflighted":
                raise ComparatorError("native_window_state_invalid")
            self.assert_shared_fence_available(str(cycle["root"]))
            lock_path = self._lock_path(cycle)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ComparatorBusy("native_lock_busy") from exc
            if not bool(quiescence_probe()) or not bool(quiescence_probe()):
                raise ComparatorBusy("scheduler_not_quiescent")
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE native_comparator_cycles SET state='running' WHERE cycle_id=? AND state='preflighted'", (cycle_id,))
                if conn.total_changes != 1:
                    raise ComparatorError("native_window_claim_lost")
            yield CycleLease(cycle_id=cycle_id, lock_path=str(lock_path), fd=fd)
        except ComparatorBusy:
            with self._connect() as conn:
                conn.execute("UPDATE native_comparator_cycles SET state='blocked' WHERE cycle_id=? AND state='preflighted'", (cycle_id,))
            raise
        except Exception:
            with self._connect() as conn:
                conn.execute("UPDATE native_comparator_cycles SET state='failed' WHERE cycle_id=? AND state IN ('preflighted','running')", (cycle_id,))
            raise
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def record_start(
        self, cycle_id: str, object_id: str, *, run_id: str, pid: int,
        lock_path: str, started_at: float, input_generation: str, input_fingerprint: str,
    ) -> None:
        payload = {
            "run_id": _text(run_id, "run_id"),
            "pid": int(pid),
            "lock_path": _text(lock_path, "lock_path"),
            "started_at": _timestamp(started_at, "started_at"),
            "input_generation": _text(input_generation, "start_input_generation"),
            "input_fingerprint": _fingerprint(input_fingerprint, "start_input_fingerprint"),
        }
        if payload["pid"] <= 0:
            raise ComparatorError("pid_invalid")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT c.state,o.pre_json,o.start_json FROM native_comparator_cycles c JOIN native_comparator_objects o ON o.cycle_id=c.cycle_id WHERE c.cycle_id=? AND o.object_id=?",
                (cycle_id, _object_id(object_id)),
            ).fetchone()
            if not row or row["state"] != "running" or row["start_json"]:
                raise ComparatorError("native_start_invalid")
            pre = json.loads(row["pre_json"])
            if pre["input_generation"] != payload["input_generation"] or pre["input_fingerprint"] != payload["input_fingerprint"]:
                raise ComparatorError("native_start_preimage_mismatch")
            cycle = conn.execute("SELECT window_start,window_end FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if payload["started_at"] < cycle["window_start"] or payload["started_at"] > cycle["window_end"] or payload["started_at"] < pre["observed_at"]:
                raise ComparatorError("native_start_temporal_invalid")
            conn.execute(
                "UPDATE native_comparator_objects SET start_json=? WHERE cycle_id=? AND object_id=?",
                (_canonical(payload).decode("utf-8"), cycle_id, _object_id(object_id)),
            )

    def record_result(
        self, cycle_id: str, object_id: str, *, status: str, return_code: int,
        processed_input_generation: str | None, processed_input_fingerprint: str | None,
        effect_evidence: Mapping[str, Any], finished_at: float, outcome: str,
    ) -> None:
        status = _text(status, "native_status")
        outcome = _text(outcome, "native_outcome")
        if status not in {"ok", "error", "no_op", "long_running_sql"}:
            raise ComparatorError("native_status_invalid")
        if isinstance(return_code, bool) or not isinstance(return_code, int):
            raise ComparatorError("native_return_code_invalid")
        payload = {
            "status": status, "return_code": return_code,
            "processed_input_generation": None if processed_input_generation is None else _text(processed_input_generation, "processed_input_generation"),
            "processed_input_fingerprint": None if processed_input_fingerprint is None else _fingerprint(processed_input_fingerprint, "processed_input_fingerprint"),
            "effect_evidence": _json(effect_evidence, "effect_evidence"),
            "finished_at": _timestamp(finished_at, "finished_at"), "outcome": outcome,
        }
        effect = payload["effect_evidence"]
        if status == "ok" and effect:
            if effect.get("processed_object_id") != _object_id(object_id) or effect.get("observed") is not True:
                raise ComparatorError("native_effect_evidence_invalid")
            _fingerprint(effect.get("artifact_sha256"), "effect_artifact_sha256")
            _text(effect.get("artifact_ref"), "effect_artifact_ref")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT c.state,o.pre_json,o.start_json FROM native_comparator_cycles c JOIN native_comparator_objects o ON o.cycle_id=c.cycle_id WHERE c.cycle_id=? AND o.object_id=?",
                (cycle_id, _object_id(object_id)),
            ).fetchone()
            if not row or row["state"] != "running" or not row["start_json"]:
                raise ComparatorError("native_result_without_start")
            pre = json.loads(row["pre_json"])
            if (payload["processed_input_generation"] is not None and
                    (payload["processed_input_generation"] != pre["input_generation"] or
                     payload["processed_input_fingerprint"] != pre["input_fingerprint"])):
                raise ComparatorError("native_result_preimage_mismatch")
            cycle = conn.execute("SELECT window_end FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            start = json.loads(row["start_json"])
            if payload["finished_at"] < start["started_at"] or payload["finished_at"] > cycle["window_end"]:
                raise ComparatorError("native_result_temporal_invalid")
            conn.execute(
                "UPDATE native_comparator_objects SET result_json=? WHERE cycle_id=? AND object_id=?",
                (_canonical(payload).decode("utf-8"), cycle_id, _object_id(object_id)),
            )

    def abort_cycle(self, cycle_id: str, reason: str) -> None:
        """Close an unfinished cycle without discarding its bounded evidence."""
        reason = _text(reason, "reason")
        with self._connect() as conn:
            row = conn.execute("SELECT state FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if row is None:
                raise ComparatorError("unknown_cycle")
            if row["state"] in {"complete", "failed_audit"}:
                raise ComparatorError("cycle_already_closed")
            conn.execute(
                "UPDATE native_comparator_cycles SET state='failed', receipt_json=? WHERE cycle_id=?",
                (_canonical({"cycle_id": cycle_id, "status": "failed", "reason": reason}).decode("utf-8"), cycle_id),
            )

    def cycle_state(self, cycle_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT state FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise ComparatorError("unknown_cycle")
        return str(row["state"])

    def cycle_context(self, cycle_id: str) -> dict[str, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT instance_uid,root,operation FROM native_comparator_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
        if row is None:
            raise ComparatorError("unknown_cycle")
        return {"instance_uid": str(row["instance_uid"]), "root": str(row["root"]), "operation": str(row["operation"])}

    def finalize(self, cycle_id: str, after: list[Mapping[str, Any]], *, finished_at: float | None = None) -> dict[str, Any]:
        rows = [_normalise_after(item) for item in after]
        after_by_id = {row["object_id"]: row for row in rows}
        if len(after_by_id) != len(rows):
            raise ComparatorError("after_duplicate_object")
        with self._connect() as conn:
            cycle = conn.execute("SELECT * FROM native_comparator_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle or cycle["state"] != "running":
                raise ComparatorError("finalize_cycle_state_invalid")
            objects = conn.execute("SELECT * FROM native_comparator_objects WHERE cycle_id=? ORDER BY object_id", (cycle_id,)).fetchall()
            if {row["object_id"] for row in objects} != set(after_by_id):
                raise ComparatorError("after_allowlist_mismatch")
            output: list[dict[str, Any]] = []
            for row in objects:
                pre = json.loads(row["pre_json"])
                post = after_by_id[row["object_id"]]
                result = json.loads(row["result_json"]) if row["result_json"] else None
                start = json.loads(row["start_json"]) if row["start_json"] else None
                classification, reason = self._classify(cycle, pre, post, start, result)
                evidence = {
                    "object_id": row["object_id"], "classification": classification,
                    "reason": reason, "pre": pre, "after": post,
                    "native_start": start, "native_result": result,
                }
                output.append(evidence)
                conn.execute(
                    "UPDATE native_comparator_objects SET classification=?,reason=? WHERE cycle_id=? AND object_id=?",
                    (classification, reason, cycle_id, row["object_id"]),
                )
            receipt = {
                "schema": SCHEMA, "cycle_id": cycle_id,
                "instance_uid": cycle["instance_uid"], "root": cycle["root"],
                "operation": cycle["operation"], "window_start": cycle["window_start"],
                "window_end": cycle["window_end"], "preflight_sha256": cycle["preflight_sha256"],
                "pause_start": cycle["pause_start"], "pause_end": cycle["pause_end"],
                "status": "complete", "finished_at": _timestamp(finished_at or time.time(), "finished_at"),
                "objects": output,
            }
            receipt["receipt_sha256"] = evidence_sha256(receipt)
            raw = _canonical(receipt).decode("utf-8")
            conn.execute(
                "UPDATE native_comparator_cycles SET state='complete',receipt_json=?,receipt_sha256=? WHERE cycle_id=?",
                (raw, receipt["receipt_sha256"], cycle_id),
            )
            return json.loads(raw)

    @staticmethod
    def _classify(cycle: sqlite3.Row, pre: dict[str, Any], post: dict[str, Any], start: dict[str, Any] | None, result: dict[str, Any] | None) -> tuple[str, str]:
        if not pre["complete"]:
            return "unknown", "preflight_incomplete"
        if cycle["pause_start"] is not None and pre["due_since"] is not None and cycle["pause_start"] <= pre["due_since"] <= cycle["pause_end"]:
            return "unknown", "due_during_comparator_pause"
        if not pre["eligible"] or not pre["due"]:
            return "not_due", "not_eligible_or_not_due_at_preflight"
        if result is None or start is None:
            return "unknown", "native_start_or_result_missing"
        if result["return_code"] != 0 or result["status"] == "error":
            return "native_error", "native_return_nonzero"
        if result["status"] == "no_op":
            return "native_noop", "native_reported_no_op"
        if result["status"] == "long_running_sql":
            return "native_long_running", "native_reported_long_running_sql"
        if pre["due_since"] is None:
            return "unknown", "due_history_missing"
        if pre["obligation_deadline"] > pre["observed_at"]:
            return "unknown", "mcd_obligation_not_yet_due"
        if pre["history_complete_until"] < pre["obligation_deadline"]:
            return "unknown", "mcd_obligation_history_incomplete"
        if (cycle["pause_start"] is not None and
                pre["due_since"] <= cycle["pause_end"] and
                pre["obligation_deadline"] >= cycle["pause_start"]):
            return "unknown", "mcd_obligation_overlaps_comparator_pause"
        if start["input_generation"] != pre["input_generation"] or start["input_fingerprint"] != pre["input_fingerprint"]:
            return "unknown", "native_start_preimage_mismatch"
        if (result["processed_input_generation"] is None or
                result["processed_input_fingerprint"] is None or
                not (result.get("effect_evidence") or {}).get("observed")):
            return "unknown", "native_per_object_effect_evidence_missing"
        if result["processed_input_generation"] != pre["input_generation"] or result["processed_input_fingerprint"] != pre["input_fingerprint"]:
            return "unknown", "native_processed_input_mismatch"
        if (post["source_input_generation"] != pre["input_generation"] or
                post["source_input_fingerprint"] != pre["input_fingerprint"]):
            return "unknown", "input_changed_after_preflight"
        if (pre["mcd_decision"] == "completed" and
                pre["mcd_start_generation"] == pre["input_generation"] and
                pre["mcd_result_generation"] == pre["input_generation"] and
                pre["mcd_result_fingerprint"] == pre["input_fingerprint"]):
            return "mcd_handled", "mcd_obligation_same_generation"
        if pre["mcd_decision"] in {"unknown", "failed"}:
            return "unknown", "mcd_history_incomplete"
        effect = result.get("effect_evidence") or {}
        if effect.get("processed_object_id") != pre["object_id"] or effect.get("observed") is not True:
            return "unknown", "native_effect_evidence_missing"
        return "mcd_missed", "due_preexisting_state_processed_natively"


def missing_cycle_receipt(*, cycle_id: str, instance_uid: str, root: str, operation: str, window_start: float, window_end: float) -> dict[str, Any]:
    cycle_id = _text(cycle_id, "cycle_id")
    receipt = {
        "schema": SCHEMA, "cycle_id": cycle_id, "instance_uid": _text(instance_uid, "instance_uid"),
        "root": _root(root), "operation": _operation(operation),
        "window_start": _timestamp(window_start, "window_start"), "window_end": _timestamp(window_end, "window_end"),
        "status": "failed_audit", "reason": "comparator_cycle_missing", "objects": [],
    }
    if receipt["window_end"] <= receipt["window_start"]:
        raise ComparatorError("window_invalid")
    receipt["receipt_sha256"] = evidence_sha256(receipt)
    return receipt


def validate_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ComparatorError("receipt_schema_invalid")
    if not _HEX64.fullmatch(str(receipt.get("receipt_sha256", ""))):
        raise ComparatorError("receipt_digest_invalid")
    expected = evidence_sha256({key: value for key, value in receipt.items() if key != "receipt_sha256"})
    if expected != receipt["receipt_sha256"]:
        raise ComparatorError("receipt_digest_mismatch")
    for key in ("cycle_id", "instance_uid"):
        _text(receipt.get(key), key)
    _root(receipt.get("root"))
    _operation(receipt.get("operation"))
    if receipt.get("status") not in {"complete", "failed_audit"}:
        raise ComparatorError("receipt_status_invalid")
    if not isinstance(receipt.get("objects"), list) or len(receipt["objects"]) > _MAX_OBJECTS:
        raise ComparatorError("receipt_objects_invalid")
    return json.loads(_canonical(receipt))
