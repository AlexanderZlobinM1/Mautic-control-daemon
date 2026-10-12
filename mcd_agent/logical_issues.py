from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from mcd_agent.db import MauticDB
from mcd_agent.segment_dependencies import (
    dependent_segment_closure,
    extract_leadlist_filter_segment_ids,
    segment_dependency_maps,
)
from mcd_agent.segment_filter_safety import segment_invalid_filter_issues


LOGICAL_ISSUES_SCHEMA = "mcd-logical-issues-v1"
SUPPORTED_REMEDIATIONS = {"disable_segments"}
TOPOLOGY_TERMINAL_CODES = frozenset({"dependency_cycle", "self_reference"})


def _utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(float(time.time() if ts is None else ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _load_json(raw: object, fallback: object) -> object:
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return fallback
    return parsed


def _positive_ids(values: object) -> list[int]:
    raw = values if isinstance(values, (list, tuple, set)) else []
    out: list[int] = []
    seen: set[int] = set()
    for value in raw:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item > 0 and item not in seen:
            seen.add(item)
            out.append(item)
    return sorted(out)


@dataclass(frozen=True)
class LogicalIssue:
    issue_id: str
    component: str
    entity_type: str
    code: str
    severity: str
    title: str
    reason: str
    entity_ids: list[int]
    blocked_entity_ids: list[int]
    evidence: dict[str, Any]
    recommended_action: str = "disable_segments"

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _strongly_connected_components(graph: dict[int, set[int]]) -> list[set[int]]:
    nodes = set(graph)
    for targets in graph.values():
        nodes.update(targets)
    visited: set[int] = set()
    finish_order: list[int] = []
    for start in sorted(nodes):
        if start in visited:
            continue
        stack: list[tuple[int, bool]] = [(start, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                finish_order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for target in sorted(graph.get(node, set()), reverse=True):
                if target not in visited:
                    stack.append((target, False))

    reversed_graph: dict[int, set[int]] = {node: set() for node in nodes}
    for source, targets in graph.items():
        for target in targets:
            reversed_graph.setdefault(target, set()).add(source)
    components: list[set[int]] = []
    assigned: set[int] = set()
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component: set[int] = set()
        stack = [(start, False)]
        while stack:
            node, _expanded = stack.pop()
            if node in assigned:
                continue
            assigned.add(node)
            component.add(node)
            for target in reversed_graph.get(node, set()):
                if target not in assigned:
                    stack.append((target, False))
        components.append(component)
    return components


def detect_segment_logical_issues(rows: list[dict[str, Any]]) -> list[LogicalIssue]:
    normalized: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            segment_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if segment_id > 0:
            normalized[segment_id] = dict(row)
    if not normalized:
        return []

    all_rows = list(normalized.values())
    all_ids = set(normalized)
    published_ids = {
        segment_id
        for segment_id, row in normalized.items()
        if bool(int(row.get("is_published") or 0))
    }
    children_by_parent, _parents_by_child = segment_dependency_maps(all_rows)
    references = {
        segment_id: extract_leadlist_filter_segment_ids(row.get("filters"))
        for segment_id, row in normalized.items()
    }
    dependency_graph = {
        segment_id: {parent for parent in parents if parent in all_ids and parent != segment_id}
        for segment_id, parents in references.items()
    }
    issues: list[LogicalIssue] = []

    for component in _strongly_connected_components(dependency_graph):
        if len(component) < 2 or not (component & published_ids):
            continue
        descendants = dependent_segment_closure(set(component), children_by_parent)
        blocked = sorted((set(component) | descendants) & published_ids)
        cycle_ids = sorted(component)
        issues.append(
            LogicalIssue(
                issue_id="segment:dependency_cycle:" + "-".join(str(x) for x in cycle_ids),
                component="segments",
                entity_type="segment",
                code="dependency_cycle",
                severity="error",
                title="Circular segment dependency",
                reason=(
                    "Segments reference each other recursively. A native full segment rebuild can stop before "
                    "unrelated segments are reached."
                ),
                entity_ids=cycle_ids,
                blocked_entity_ids=blocked,
                evidence={"cycle_ids": cycle_ids, "affected_published_ids": blocked},
            )
        )

    for segment_id in sorted(published_ids):
        row = normalized[segment_id]
        refs = references.get(segment_id, set())
        if segment_id in refs:
            descendants = dependent_segment_closure({segment_id}, children_by_parent)
            blocked = sorted(({segment_id} | descendants) & published_ids)
            issues.append(
                LogicalIssue(
                    issue_id=f"segment:self_reference:{segment_id}",
                    component="segments",
                    entity_type="segment",
                    code="self_reference",
                    severity="error",
                    title="Segment references itself",
                    reason=f"Segment {segment_id} contains a segment filter that points back to itself.",
                    entity_ids=[segment_id],
                    blocked_entity_ids=blocked,
                    evidence={"segment_id": segment_id, "name": str(row.get("name") or "")},
                )
            )

        missing = sorted(refs - all_ids)
        if missing:
            issues.append(
                LogicalIssue(
                    issue_id=f"segment:missing_dependency:{segment_id}:" + "-".join(str(x) for x in missing),
                    component="segments",
                    entity_type="segment",
                    code="missing_dependency",
                    severity="warning",
                    title="Missing segment dependency",
                    reason=f"Segment {segment_id} references segment IDs that no longer exist: {', '.join(map(str, missing))}.",
                    entity_ids=[segment_id],
                    blocked_entity_ids=[],
                    evidence={"segment_id": segment_id, "missing_ids": missing, "name": str(row.get("name") or "")},
                    recommended_action="",
                )
            )

        unpublished = sorted(
            parent_id
            for parent_id in refs
            if parent_id in all_ids and parent_id not in published_ids and parent_id != segment_id
        )
        if unpublished:
            issues.append(
                LogicalIssue(
                    issue_id=f"segment:unpublished_dependency:{segment_id}:"
                    + "-".join(str(x) for x in unpublished),
                    component="segments",
                    entity_type="segment",
                    code="unpublished_dependency",
                    severity="warning",
                    title="Published segment depends on an unpublished segment",
                    reason=(
                        f"Segment {segment_id} is published but depends on unpublished segment IDs: "
                        f"{', '.join(map(str, unpublished))}."
                    ),
                    entity_ids=[segment_id],
                    blocked_entity_ids=[],
                    evidence={
                        "segment_id": segment_id,
                        "unpublished_dependency_ids": unpublished,
                        "name": str(row.get("name") or ""),
                    },
                    recommended_action="",
                )
            )

    invalid_filters = segment_invalid_filter_issues(
        [normalized[segment_id] for segment_id in sorted(published_ids)]
    )
    for segment_id in sorted(invalid_filters):
        details = [
            {"field": item.field, "value": item.value, "reason": item.reason}
            for item in invalid_filters[segment_id]
        ]
        issues.append(
            LogicalIssue(
                issue_id=f"segment:invalid_filter:{segment_id}",
                component="segments",
                entity_type="segment",
                code="invalid_filter",
                severity="warning",
                title="Possible invalid segment filter",
                reason=(
                    f"MCD detected a possible unsafe filter expression on segment {segment_id}; "
                    "this is a diagnostic hint only and does not replace the Mautic command result."
                ),
                entity_ids=[segment_id],
                blocked_entity_ids=[],
                evidence={"segment_id": segment_id, "filters": details},
                recommended_action="",
            )
        )

    deduped: dict[str, LogicalIssue] = {}
    for issue in issues:
        deduped[issue.issue_id] = issue
    return [deduped[key] for key in sorted(deduped)]


class LogicalIssueStore:
    def __init__(self, path: str, runtime_store: Any | None = None) -> None:
        self.path = path
        self.runtime_store = runtime_store
        self.conn: sqlite3.Connection | None = None
        if runtime_store is not None:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_sync (
              key TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              updated_at REAL NOT NULL
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    @staticmethod
    def _key(root: str) -> str:
        digest = hashlib.sha256(str(root or "").encode("utf-8")).hexdigest()
        return f"logical_issues:v1:{digest}"

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema": LOGICAL_ISSUES_SCHEMA,
            "scan": {"status": "never", "reason": "not_scanned", "scanned_at": None},
            "issues": [],
            "actions": [],
            "blocked_segment_ids": [],
            "totals": {"active": 0, "errors": 0, "warnings": 0},
        }

    def _read(self, root: str) -> dict[str, Any]:
        key = self._key(root)
        if self.runtime_store is not None:
            raw = self.runtime_store.get_runtime_sync(key)
            return dict(raw) if isinstance(raw, dict) else self._empty()
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT payload_json FROM runtime_sync WHERE key=? LIMIT 1", (key,)
        ).fetchone()
        if row is None:
            return self._empty()
        raw = _load_json(row["payload_json"], {})
        return dict(raw) if isinstance(raw, dict) else self._empty()

    def _write(self, root: str, payload: dict[str, Any]) -> None:
        key = self._key(root)
        if self.runtime_store is not None:
            self.runtime_store.put_runtime_sync(key, payload)
            return
        assert self.conn is not None
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO runtime_sync(key,payload_json,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                  payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at
                """,
                (key, _json(payload), time.time()),
            )

    def sync(self, root: str, issues: list[LogicalIssue], *, now_ts: float | None = None) -> None:
        now = float(time.time() if now_ts is None else now_ts)
        state = self._read(root)
        previous = {
            str(item.get("issue_id") or ""): item
            for item in list(state.get("issues") or [])
            if isinstance(item, dict) and str(item.get("issue_id") or "")
        }
        ordered_issues = sorted(
            issues,
            key=lambda item: (0 if item.severity == "error" else 1, item.issue_id),
        )
        active: list[dict[str, Any]] = []
        for issue in ordered_issues[:100]:
            item = issue.payload()
            old = previous.get(issue.issue_id, {})
            item.update(
                {
                    "status": "active",
                    "first_seen_at": str(old.get("first_seen_at") or _utc_iso(now)),
                    "last_seen_at": _utc_iso(now),
                    "resolved_at": None,
                }
            )
            active.append(item)
        state.update(
            {
                "schema": LOGICAL_ISSUES_SCHEMA,
                "scan": {"status": "ok", "reason": "", "scanned_at": _utc_iso(now)},
                "issues": active,
                "actions": list(state.get("actions") or [])[:50],
                "blocked_segment_ids": sorted(
                    {
                        segment_id
                        for issue in ordered_issues
                        for segment_id in _positive_ids(issue.blocked_entity_ids)
                    }
                )[:10000],
                "totals": {
                    "active": len(ordered_issues),
                    "errors": sum(1 for issue in ordered_issues if issue.severity == "error"),
                    "warnings": sum(1 for issue in ordered_issues if issue.severity == "warning"),
                },
            }
        )
        self._write(root, state)

    def record_scan_error(self, root: str, reason: str, *, now_ts: float | None = None) -> None:
        now = float(time.time() if now_ts is None else now_ts)
        state = self._read(root)
        state["scan"] = {
            "status": "error",
            "reason": str(reason or "scan_failed")[:1000],
            "scanned_at": _utc_iso(now),
        }
        self._write(root, state)

    def record_action(
        self,
        *,
        root: str,
        issue_id: str,
        action: str,
        actor: str,
        status: str,
        reason: str,
        before: object,
        after: object,
        now_ts: float | None = None,
    ) -> None:
        now = float(time.time() if now_ts is None else now_ts)
        state = self._read(root)
        actions = list(state.get("actions") or [])
        actions.insert(
            0,
            {
                "issue_id": str(issue_id)[:255],
                "action": str(action)[:64],
                "actor": str(actor or "Emmy Starwell")[:200],
                "status": str(status)[:32],
                "reason": str(reason or "")[:2000],
                "before": before if isinstance(before, list) else [],
                "after": after if isinstance(after, list) else [],
                "created_at": _utc_iso(now),
            },
        )
        state["actions"] = actions[:50]
        self._write(root, state)

    def active_issue(self, root: str, issue_id: str) -> dict[str, Any] | None:
        for item in list(self._read(root).get("issues") or []):
            if isinstance(item, dict) and str(item.get("issue_id") or "") == issue_id:
                return dict(item)
        return None

    def snapshot(self, root: str, *, issue_limit: int = 100, action_limit: int = 50) -> dict[str, Any]:
        state = self._read(root)
        scan = state.get("scan") if isinstance(state.get("scan"), dict) else {}
        issues = [dict(item) for item in list(state.get("issues") or []) if isinstance(item, dict)]
        issues.sort(key=lambda item: (0 if item.get("severity") == "error" else 1, str(item.get("issue_id") or "")))
        issues = issues[: max(1, int(issue_limit))]
        actions = [dict(item) for item in list(state.get("actions") or []) if isinstance(item, dict)][
            : max(1, int(action_limit))
        ]
        errors = sum(1 for issue in issues if issue.get("severity") == "error")
        warnings = sum(1 for issue in issues if issue.get("severity") == "warning")
        totals = state.get("totals") if isinstance(state.get("totals"), dict) else {}
        total_active = max(len(issues), int(totals.get("active") or 0))
        total_errors = max(errors, int(totals.get("errors") or 0))
        total_warnings = max(warnings, int(totals.get("warnings") or 0))
        return {
            "schema": LOGICAL_ISSUES_SCHEMA,
            "status": str(scan.get("status") or "never"),
            "reason": str(scan.get("reason") or ("" if scan.get("status") == "ok" else "not_scanned")),
            "scanned_at": str(scan.get("scanned_at") or "") or None,
            "summary": {
                "active": total_active,
                "errors": total_errors,
                "warnings": total_warnings,
                "truncated": total_active > len(issues),
            },
            "issues": issues,
            "actions": actions,
            "blocked_segment_ids": _positive_ids(state.get("blocked_segment_ids")),
        }


def scan_install_logical_issues(
    cfg: Any,
    install: Any,
    *,
    runtime_store: Any | None = None,
) -> dict[str, Any]:
    root = str(getattr(install, "root", "") or "").strip()
    store = LogicalIssueStore(str(cfg.state_db_path), runtime_store=runtime_store)
    db_cfg = getattr(install, "db", None)
    if not root or db_cfg is None:
        reason = "missing_root_or_db_config"
        if root:
            store.record_scan_error(root, reason)
        snapshot = store.snapshot(root) if root else {
            "schema": LOGICAL_ISSUES_SCHEMA,
            "status": "error",
            "reason": reason,
            "scanned_at": None,
            "summary": {"active": 0, "errors": 0, "warnings": 0},
            "issues": [],
            "actions": [],
        }
        store.close()
        return snapshot
    try:
        rows = MauticDB(db_cfg).fetch_all_segment_filters()
        issues = detect_segment_logical_issues(rows)
        store.sync(root, issues)
    except Exception as exc:
        store.record_scan_error(root, str(exc))
    snapshot = store.snapshot(root)
    store.close()
    return snapshot


def read_logical_issues_snapshot(
    state_db_path: str,
    root: str,
    *,
    runtime_store: Any | None = None,
) -> dict[str, Any]:
    store = LogicalIssueStore(state_db_path, runtime_store=runtime_store)
    try:
        return store.snapshot(root)
    finally:
        store.close()


def prune_logical_issue_snapshots(
    state_db_path: str,
    active_roots: object,
    *,
    runtime_store: Any | None = None,
) -> int:
    roots = active_roots if isinstance(active_roots, (list, tuple, set)) else []
    keep = {LogicalIssueStore._key(str(root)) for root in roots if str(root or "").strip()}
    prefix = "logical_issues:v1:"
    if runtime_store is not None:
        stale = [
            key
            for key, _payload in runtime_store.list_runtime_sync(prefix)
            if str(key).startswith(prefix) and str(key) not in keep
        ]
        if stale:
            runtime_store.delete_runtime_sync(stale)
        return len(stale)

    store = LogicalIssueStore(state_db_path)
    try:
        assert store.conn is not None
        rows = store.conn.execute(
            "SELECT key FROM runtime_sync WHERE key LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
        stale = [str(row["key"]) for row in rows if str(row["key"]) not in keep]
        if stale:
            placeholders = ",".join(["?"] * len(stale))
            with store.conn:
                store.conn.execute(
                    f"DELETE FROM runtime_sync WHERE key IN ({placeholders})",
                    tuple(stale),
                )
        return len(stale)
    finally:
        store.close()


def logical_issue_blocked_segment_ids(snapshot: dict[str, Any]) -> set[int]:
    issues = snapshot.get("issues") if isinstance(snapshot.get("issues"), list) else []
    blocked: set[int] = set(_positive_ids(snapshot.get("blocked_segment_ids")))
    for issue in issues:
        if not isinstance(issue, dict) or str(issue.get("status") or "active") != "active":
            continue
        blocked.update(_positive_ids(issue.get("blocked_entity_ids")))
    return blocked


def logical_issue_terminal_segment_notes(snapshot: dict[str, Any]) -> dict[int, str]:
    """Return explicit data-error notes for topology that cannot be run safely.

    Dependency cycles and self-references are structural topology failures,
    not ordinary Mautic command results.  They must not remain in an admitted
    cycle as indefinitely blocked work.  Filter-safety hints deliberately do
    not appear here: the normal profile-bound Mautic command remains the
    source of truth for those expressions.
    """

    issues = snapshot.get("issues") if isinstance(snapshot.get("issues"), list) else []
    notes: dict[int, str] = {}
    for issue in issues:
        if not isinstance(issue, dict) or str(issue.get("status") or "active") != "active":
            continue
        code = str(issue.get("code") or "").strip().lower()
        if code not in TOPOLOGY_TERMINAL_CODES:
            continue
        note = f"data_error:{code}"
        for segment_id in _positive_ids(issue.get("blocked_entity_ids")):
            notes.setdefault(segment_id, note)

    # ``snapshot`` preserves the aggregate blocked projection beyond the
    # bounded visible issue list.  Current producers only populate it from
    # topology-terminal issues, so retain those IDs with a generic explicit
    # data-error note when their detailed issue is outside the display limit.
    for segment_id in _positive_ids(snapshot.get("blocked_segment_ids")):
        notes.setdefault(segment_id, "data_error:logical_issue")
    return notes


def _segment_audit_rows(value: object) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    for row in rows[:500]:
        if not isinstance(row, dict):
            continue
        description = str(row.get("description") or "")
        out.append(
            {
                "id": int(row.get("id") or 0),
                "name": str(row.get("name") or "")[:255],
                "is_published": int(row.get("is_published") or 0),
                "date_modified": str(row.get("date_modified") or "")[:64],
                "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
            }
        )
    return out


def remediate_logical_issues(
    cfg: Any,
    install: Any,
    *,
    targets: list[dict[str, Any]],
    action: str,
    actor: str,
    runtime_store: Any | None = None,
) -> dict[str, Any]:
    root = str(getattr(install, "root", "") or "").strip()
    db_cfg = getattr(install, "db", None)
    action_clean = str(action or "").strip().lower()
    if action_clean not in SUPPORTED_REMEDIATIONS:
        raise ValueError(f"unsupported logical issue remediation: {action_clean}")
    if not root or db_cfg is None:
        raise ValueError("instance database configuration is unavailable")
    if not isinstance(targets, list) or not targets:
        raise ValueError("at least one logical issue remediation target is required")
    if len(targets) > 100:
        raise ValueError("logical issue remediation is limited to 100 targets")

    preflight = scan_install_logical_issues(cfg, install, runtime_store=runtime_store)
    if str(preflight.get("status") or "") != "ok":
        raise RuntimeError(
            "logical issue remediation requires a successful current scan: "
            + str(preflight.get("reason") or "scan_failed")
        )
    active_issues = {
        str(item.get("issue_id") or "").strip(): item
        for item in list(preflight.get("issues") or [])
        if isinstance(item, dict) and str(item.get("issue_id") or "").strip()
    }
    selected_by_issue: dict[str, list[int]] = {}
    issue_reasons: dict[str, str] = {}
    segment_issue_ids: dict[int, list[str]] = {}
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("logical issue remediation target must be an object")
        issue_id_clean = str(target.get("issue_id") or "").strip()
        issue = active_issues.get(issue_id_clean)
        if issue is None:
            raise ValueError(f"logical issue is no longer active: {issue_id_clean}")
        allowed_ids = _positive_ids(issue.get("blocked_entity_ids"))
        requested_raw = target.get("segment_ids")
        requested_ids = allowed_ids if requested_raw is None else _positive_ids(requested_raw)
        if not requested_ids:
            raise ValueError(f"logical issue has no selected published segments: {issue_id_clean}")
        invalid_ids = sorted(set(requested_ids) - set(allowed_ids))
        if invalid_ids:
            raise ValueError(
                f"segments are not part of the current logical issue {issue_id_clean}: "
                + ",".join(map(str, invalid_ids))
            )
        selected = sorted(set(selected_by_issue.get(issue_id_clean, [])) | set(requested_ids))
        selected_by_issue[issue_id_clean] = selected
        issue_reasons[issue_id_clean] = f"{issue.get('title')}: {issue.get('reason')}"
        for segment_id in requested_ids:
            owners = segment_issue_ids.setdefault(segment_id, [])
            if issue_id_clean not in owners:
                owners.append(issue_id_clean)

    if sum(len(segment_ids) for segment_ids in selected_by_issue.values()) > 500:
        raise ValueError("logical issue remediation is limited to 500 segment selections")

    blocked_ids = sorted(segment_issue_ids)
    if not blocked_ids:
        raise ValueError("logical issue remediation selected no published segments")
    segment_contexts: dict[int, dict[str, str]] = {}
    for segment_id, issue_ids in segment_issue_ids.items():
        segment_contexts[segment_id] = {
            "issue_id": ",".join(issue_ids),
            "reason": " | ".join(issue_reasons[issue_id] for issue_id in issue_ids),
        }
    issue_ids = sorted(selected_by_issue)
    batch_issue_id = issue_ids[0] if len(issue_ids) == 1 else "batch:" + hashlib.sha256(
        _json({issue_id: selected_by_issue[issue_id] for issue_id in issue_ids}).encode("utf-8")
    ).hexdigest()[:16]
    batch_reason = " | ".join(issue_reasons[issue_id] for issue_id in issue_ids)
    store = LogicalIssueStore(str(cfg.state_db_path), runtime_store=runtime_store)
    before: object = []
    after: object = []
    status = "error"
    try:
        result = MauticDB(db_cfg).disable_segments(
            blocked_ids,
            issue_id=batch_issue_id,
            reason=batch_reason,
            actor=str(actor or "Emmy Starwell"),
            segment_contexts=segment_contexts,
        )
        before = _segment_audit_rows(result.get("before"))
        after = _segment_audit_rows(result.get("after"))
        status = "success"
        for issue_id_clean in issue_ids:
            selected = set(selected_by_issue[issue_id_clean])
            store.record_action(
                root=root,
                issue_id=issue_id_clean,
                action=action_clean,
                actor=actor,
                status=status,
                reason=issue_reasons[issue_id_clean],
                before=[row for row in before if int(row.get("id") or 0) in selected],
                after=[row for row in after if int(row.get("id") or 0) in selected],
            )
    except Exception as exc:
        for issue_id_clean in issue_ids:
            store.record_action(
                root=root,
                issue_id=issue_id_clean,
                action=action_clean,
                actor=actor,
                status="error",
                reason=str(exc),
                before=[],
                after=[],
            )
        store.close()
        raise

    store.close()
    snapshot = scan_install_logical_issues(cfg, install, runtime_store=runtime_store)
    return {
        "status": status,
        "root": root,
        "issue_id": issue_ids[0] if len(issue_ids) == 1 else "",
        "issue_ids": issue_ids,
        "action": action_clean,
        "disabled_segment_ids": [
            int(row.get("id") or 0)
            for row in (after if isinstance(after, list) else [])
            if isinstance(row, dict) and not bool(int(row.get("is_published") or 0))
        ],
        "before": before,
        "after": after,
        "snapshot": snapshot,
    }


def remediate_logical_issue(
    cfg: Any,
    install: Any,
    *,
    issue_id: str,
    action: str,
    actor: str,
    segment_ids: list[int] | None = None,
    runtime_store: Any | None = None,
) -> dict[str, Any]:
    return remediate_logical_issues(
        cfg,
        install,
        targets=[{"issue_id": issue_id, "segment_ids": segment_ids}],
        action=action,
        actor=actor,
        runtime_store=runtime_store,
    )
