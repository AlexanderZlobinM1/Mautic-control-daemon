"""Durable delivery for Mautic upgrade evidence acknowledgements.

The upgrade itself is not replayable after the target tree has been applied.
Evidence delivery therefore has its own small, idempotent outbox.  The
outbox stores the exact request envelope, never a new plan, and is drained in
creation order by the normal MCD heartbeat when the MCC endpoint is
temporarily unavailable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping


OUTBOX_SCHEMA = "mcd-mautic-upgrade-evidence-outbox-v1"
RECEIPT_SCHEMA = "mcd-mautic-upgrade-terminal-receipt-v1"
_TABLE = "mcd_mautic_upgrade_evidence_outbox"
_LEASE_SECONDS = 60.0
_RETRY_DELAYS = (0.0, 1.0, 5.0, 30.0, 300.0)


class UpgradeEvidenceDeliveryError(RuntimeError):
    """Evidence could not be durably acknowledged by MCC."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(str(reason))
        self.reason = str(reason)
        self.retryable = bool(retryable)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def delivery_key(request: Mapping[str, Any]) -> str:
    identity = "\0".join(
        str(request.get(field) or "")
        for field in ("instance_uid", "run_id", "plan_sha256")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _db(path: str) -> sqlite3.Connection:
    raw = str(path or "").strip()
    if not raw:
        raise UpgradeEvidenceDeliveryError("upgrade_evidence_state_db_missing")
    Path(raw).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(raw, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            delivery_key TEXT PRIMARY KEY,
            instance_uid TEXT NOT NULL,
            run_id TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            source_version TEXT NOT NULL,
            target_version TEXT NOT NULL,
            request_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            accepted_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {_TABLE}_due_idx "
        f"ON {_TABLE}(status, next_attempt_at, created_at, delivery_key)"
    )
    db.commit()
    return db


@contextmanager
def _database(path: str):
    db = _db(path)
    try:
        yield db
    finally:
        db.close()


def enqueue(state_db_path: str, request: Mapping[str, Any]) -> str:
    """Persist one immutable request, rejecting a changed retry envelope."""
    required = ("instance_uid", "run_id", "plan_sha256", "source_version", "target_version", "evidence")
    if any(not str(request.get(field) or "").strip() for field in required):
        raise UpgradeEvidenceDeliveryError("upgrade_evidence_identity_missing")
    key = delivery_key(request)
    encoded = _canonical(dict(request))
    now = _utc_now()
    with _database(state_db_path) as db:
        row = db.execute(
            f"SELECT request_json, status FROM {_TABLE} WHERE delivery_key = ?",
            (key,),
        ).fetchone()
        if row is not None:
            if str(row["request_json"]) != encoded:
                raise UpgradeEvidenceDeliveryError("upgrade_evidence_identity_mismatch")
            return key
        db.execute(
            f"""
            INSERT INTO {_TABLE}(
                delivery_key, instance_uid, run_id, plan_sha256,
                source_version, target_version, request_json, status,
                attempt_count, next_attempt_at, lease_until, last_error,
                accepted_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, 0, '', NULL, ?, ?)
            """,
            (
                key,
                str(request["instance_uid"]),
                str(request["run_id"]),
                str(request["plan_sha256"]),
                str(request["source_version"]),
                str(request["target_version"]),
                encoded,
                now,
                now,
            ),
        )
        db.commit()
    return key


def _claim(state_db_path: str, *, force: bool) -> sqlite3.Row | None:
    now = time.time()
    with _database(state_db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            f"""
            SELECT * FROM {_TABLE}
            WHERE status IN ('pending', 'sending')
            ORDER BY created_at ASC, delivery_key ASC
            LIMIT 1
            """,
        ).fetchone()
        if row is None:
            db.commit()
            return None
        pending_due = str(row["status"]) == "pending" and (force or float(row["next_attempt_at"] or 0) <= now)
        sending_expired = str(row["status"]) == "sending" and float(row["lease_until"] or 0) <= now
        if not pending_due and not sending_expired:
            db.commit()
            return None
        attempt = int(row["attempt_count"] or 0) + 1
        db.execute(
            f"UPDATE {_TABLE} SET status='sending', attempt_count=?, lease_until=?, updated_at=? WHERE delivery_key=?",
            (attempt, now + _LEASE_SECONDS, _utc_now(), str(row["delivery_key"])),
        )
        db.commit()
        claimed = {column: row[column] for column in row.keys()}
        claimed.update(attempt_count=attempt, status="sending")
        return claimed  # type: ignore[return-value]


def _finish(state_db_path: str, key: str, *, status: str, error: str = "", response: Mapping[str, Any] | None = None) -> None:
    now = time.time()
    delay = _RETRY_DELAYS[min(4, max(0, int(_attempt_count(state_db_path, key)) - 1))]
    next_at = now + delay if status == "pending" else 0.0
    with _database(state_db_path) as db:
        db.execute(
            f"""
            UPDATE {_TABLE}
            SET status=?, next_attempt_at=?, lease_until=0, last_error=?, accepted_json=?, updated_at=?
            WHERE delivery_key=?
            """,
            (
                status,
                next_at,
                str(error or "")[:1000],
                _canonical(dict(response)) if response is not None else None,
                _utc_now(),
                key,
            ),
        )
        db.commit()


def _attempt_count(state_db_path: str, key: str) -> int:
    with _database(state_db_path) as db:
        row = db.execute(f"SELECT attempt_count FROM {_TABLE} WHERE delivery_key=?", (key,)).fetchone()
    return int(row[0] or 0) if row is not None else 1


def status(state_db_path: str, key: str) -> dict[str, Any]:
    with _database(state_db_path) as db:
        row = db.execute(f"SELECT * FROM {_TABLE} WHERE delivery_key=?", (key,)).fetchone()
    return dict(row) if row is not None else {}


def _retryable(reason: str) -> bool:
    value = str(reason or "").lower()
    if "mcc_patch_evidence_pending" in value:
        return True
    if "mcc_patch_transport_failed" in value or "urlerror" in value or "timeout" in value:
        return True
    if "response_invalid_json" in value or "response_not_object" in value:
        return True
    for code in ("408", "425", "429", "500", "501", "502", "503", "504", "505", "507", "508", "510", "511"):
        if f"mcc_patch_http_{code}" in value:
            return True
    return False


def drain_pending_upgrade_evidence(config: Any, *, max_items: int = 1, force: bool = False) -> list[dict[str, Any]]:
    """Deliver due records in FIFO order; never create or replay an upgrade."""
    state_db_path = str(getattr(config, "state_db_path", "") or "").strip()
    if not state_db_path:
        return []
    from mcd_agent.mautic_patch_resolution import (
        MauticPatchEvidencePending,
        MauticPatchResolutionError,
        report_evidence,
    )

    results: list[dict[str, Any]] = []
    for _ in range(max(1, int(max_items))):
        row = _claim(state_db_path, force=force)
        if row is None:
            break
        key = str(row["delivery_key"])
        try:
            request = json.loads(str(row["request_json"]))
            if not isinstance(request, dict):
                raise UpgradeEvidenceDeliveryError("upgrade_evidence_request_invalid")
            response = report_evidence(
                config,
                instance_uid=str(request["instance_uid"]),
                mcc_host_name=str(request["mcc_host_name"]),
                hostname=str(request["hostname"]),
                host_id=str(request["host_id"]),
                catalog_revision=str(request["catalog_revision"]),
                catalog_sha256=str(request["catalog_sha256"]),
                plan_sha256=str(request["plan_sha256"]),
                trigger=str(request["trigger"]),
                phase=str(request["phase"]),
                run_id=str(request["run_id"]),
                evidence=dict(request["evidence"]),
            )
        except MauticPatchEvidencePending as exc:
            _finish(state_db_path, key, status="pending", error=str(exc))
            results.append({"delivery_key": key, "status": "pending", "reason": str(exc)})
            break
        except (MauticPatchResolutionError, UpgradeEvidenceDeliveryError) as exc:
            retryable = getattr(exc, "retryable", False) or _retryable(str(exc))
            _finish(state_db_path, key, status="pending" if retryable else "blocked", error=str(exc))
            results.append({"delivery_key": key, "status": "pending" if retryable else "blocked", "reason": str(exc)})
            if retryable:
                break
        except Exception as exc:  # network/library failures remain durable and bounded
            _finish(state_db_path, key, status="pending", error=f"{type(exc).__name__}: {exc}")
            results.append({"delivery_key": key, "status": "pending", "reason": type(exc).__name__})
            break
        else:
            _finish(state_db_path, key, status="accepted", response=response)
            results.append({"delivery_key": key, "status": "accepted", "response": response})
    return results


def deliver_upgrade_evidence(config: Any, request: Mapping[str, Any], *, attempts: int = 3) -> dict[str, Any] | None:
    """Persist then synchronously try the exact request before terminal success."""
    state_db_path = str(getattr(config, "state_db_path", "") or "").strip()
    if not state_db_path:
        raise UpgradeEvidenceDeliveryError("upgrade_evidence_state_db_missing")
    key = enqueue(state_db_path, request)
    for index in range(max(1, int(attempts))):
        result = drain_pending_upgrade_evidence(config, max_items=1, force=True)
        current = status(state_db_path, key)
        if str(current.get("status") or "") == "accepted":
            accepted = current.get("accepted_json")
            try:
                return json.loads(str(accepted)) if accepted else None
            except (TypeError, ValueError):
                raise UpgradeEvidenceDeliveryError("upgrade_evidence_ack_invalid")
        if str(current.get("status") or "") == "blocked":
            raise UpgradeEvidenceDeliveryError(str(current.get("last_error") or "upgrade_evidence_blocked"))
        if index + 1 < max(1, int(attempts)):
            delay = _RETRY_DELAYS[min(index + 1, len(_RETRY_DELAYS) - 1)]
            if delay:
                time.sleep(delay)
    return None
