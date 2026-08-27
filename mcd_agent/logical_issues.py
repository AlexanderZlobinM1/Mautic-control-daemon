from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
    for value in raw:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item > 0 and item not in out:
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
    index = 0
    indices: dict[int, int] = {}
    lowlinks: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    components: list[set[int]] = []

    def visit(node: int) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph.get(node, set())):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: set[int] = set()
        while stack:
            target = stack.pop()
            on_stack.discard(target)
            component.add(target)
            if target == node:
                break
        components.append(component)

    nodes = set(graph)
    for targets in graph.values():
        nodes.update(targets)
    for node in sorted(nodes):
        if node not in indices:
            visit(node)
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
            descendants = dependent_segment_closure({segment_id}, children_by_parent)
            blocked = sorted(({segment_id} | descendants) & published_ids)
            issues.append(
                LogicalIssue(
                    issue_id=f"segment:missing_dependency:{segment_id}:" + "-".join(str(x) for x in missing),
                    component="segments",
                    entity_type="segment",
                    code="missing_dependency",
                    severity="error",
                    title="Missing segment dependency",
                    reason=f"Segment {segment_id} references segment IDs that no longer exist: {', '.join(map(str, missing))}.",
                    entity_ids=[segment_id],
                    blocked_entity_ids=blocked,
                    evidence={"segment_id": segment_id, "missing_ids": missing, "name": str(row.get("name") or "")},
                )
            )

        unpublished = sorted(
            parent_id
            for parent_id in refs
            if parent_id in all_ids and parent_id not in published_ids and parent_id != segment_id
        )
        if unpublished:
            descendants = dependent_segment_closure({segment_id}, children_by_parent)
            blocked = sorted(({segment_id} | descendants) & published_ids)
            issues.append(
                LogicalIssue(
                    issue_id=f"segment:unpublished_dependency:{segment_id}:"
                    + "-".join(str(x) for x in unpublished),
                    component="segments",
                    entity_type="segment",
                    code="unpublished_dependency",
                    severity="error",
                    title="Published segment depends on an unpublished segment",
                    reason=(
                        f"Segment {segment_id} is published but depends on unpublished segment IDs: "
                        f"{', '.join(map(str, unpublished))}."
                    ),
                    entity_ids=[segment_id],
                    blocked_entity_ids=blocked,
                    evidence={
                        "segment_id": segment_id,
                        "unpublished_dependency_ids": unpublished,
                        "name": str(row.get("name") or ""),
                    },
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
                severity="error",
                title="Invalid segment filter",
                reason=f"Segment {segment_id} contains filter values that Mautic cannot evaluate safely.",
                entity_ids=[segment_id],
                blocked_entity_ids=[segment_id],
                evidence={"segment_id": segment_id, "filters": details},
            )
        )

    deduped: dict[str, LogicalIssue] = {}
    for issue in issues:
        deduped[issue.issue_id] = issue
    return [deduped[key] for key in sorted(deduped)]


class LogicalIssueStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logical_issues (
              issue_id TEXT NOT NULL,
              root TEXT NOT NULL,
              component TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              code TEXT NOT NULL,
              severity TEXT NOT NULL,
              title TEXT NOT NULL,
              reason TEXT NOT NULL,
              entity_ids_json TEXT NOT NULL,
              blocked_entity_ids_json TEXT NOT NULL,
              evidence_json TEXT NOT NULL,
              recommended_action TEXT NOT NULL,
              status TEXT NOT NULL,
              first_seen_at REAL NOT NULL,
              last_seen_at REAL NOT NULL,
              resolved_at REAL,
              PRIMARY KEY(root, issue_id)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_logical_issues_root_status ON logical_issues(root,status,last_seen_at)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logical_issue_actions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              root TEXT NOT NULL,
              issue_id TEXT NOT NULL,
              action TEXT NOT NULL,
              actor TEXT NOT NULL,
              status TEXT NOT NULL,
              reason TEXT NOT NULL,
              before_json TEXT NOT NULL,
              after_json TEXT NOT NULL,
              created_at REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_logical_issue_actions_root_created "
            "ON logical_issue_actions(root,created_at DESC)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logical_issue_scans (
              root TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              reason TEXT NOT NULL,
              scanned_at REAL NOT NULL
            )
            """
        )
        self.conn.commit()

    def sync(self, root: str, issues: list[LogicalIssue], *, now_ts: float | None = None) -> None:
        now = float(time.time() if now_ts is None else now_ts)
        active_ids = {issue.issue_id for issue in issues}
        with self.conn:
            for issue in issues:
                self.conn.execute(
                    """
                    INSERT INTO logical_issues(
                      issue_id,root,component,entity_type,code,severity,title,reason,
                      entity_ids_json,blocked_entity_ids_json,evidence_json,recommended_action,
                      status,first_seen_at,last_seen_at,resolved_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(root,issue_id) DO UPDATE SET
                      component=excluded.component,
                      entity_type=excluded.entity_type,
                      code=excluded.code,
                      severity=excluded.severity,
                      title=excluded.title,
                      reason=excluded.reason,
                      entity_ids_json=excluded.entity_ids_json,
                      blocked_entity_ids_json=excluded.blocked_entity_ids_json,
                      evidence_json=excluded.evidence_json,
                      recommended_action=excluded.recommended_action,
                      status='active',
                      last_seen_at=excluded.last_seen_at,
                      resolved_at=NULL
                    """,
                    (
                        issue.issue_id,
                        root,
                        issue.component,
                        issue.entity_type,
                        issue.code,
                        issue.severity,
                        issue.title,
                        issue.reason,
                        _json(issue.entity_ids),
                        _json(issue.blocked_entity_ids),
                        _json(issue.evidence),
                        issue.recommended_action,
                        "active",
                        now,
                        now,
                    ),
                )
            rows = self.conn.execute(
                "SELECT issue_id FROM logical_issues WHERE root=? AND status='active'", (root,)
            ).fetchall()
            stale = [str(row["issue_id"]) for row in rows if str(row["issue_id"]) not in active_ids]
            for issue_id in stale:
                self.conn.execute(
                    "UPDATE logical_issues SET status='resolved',resolved_at=? WHERE root=? AND issue_id=?",
                    (now, root, issue_id),
                )
            self.conn.execute(
                """
                INSERT INTO logical_issue_scans(root,status,reason,scanned_at) VALUES(?,?,?,?)
                ON CONFLICT(root) DO UPDATE SET status=excluded.status,reason=excluded.reason,scanned_at=excluded.scanned_at
                """,
                (root, "ok", "", now),
            )

    def record_scan_error(self, root: str, reason: str, *, now_ts: float | None = None) -> None:
        now = float(time.time() if now_ts is None else now_ts)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO logical_issue_scans(root,status,reason,scanned_at) VALUES(?,?,?,?)
                ON CONFLICT(root) DO UPDATE SET status=excluded.status,reason=excluded.reason,scanned_at=excluded.scanned_at
                """,
                (root, "error", str(reason or "scan_failed")[:1000], now),
            )

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
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO logical_issue_actions(
                  root,issue_id,action,actor,status,reason,before_json,after_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    root,
                    issue_id,
                    action,
                    str(actor or "Emmy Starwell")[:200],
                    status,
                    str(reason or "")[:2000],
                    _json(before),
                    _json(after),
                    now,
                ),
            )

    def active_issue(self, root: str, issue_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM logical_issues WHERE root=? AND issue_id=? AND status='active'",
            (root, issue_id),
        ).fetchone()
        return self._issue_payload(row) if row is not None else None

    @staticmethod
    def _issue_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "issue_id": str(row["issue_id"]),
            "component": str(row["component"]),
            "entity_type": str(row["entity_type"]),
            "code": str(row["code"]),
            "severity": str(row["severity"]),
            "title": str(row["title"]),
            "reason": str(row["reason"]),
            "entity_ids": _positive_ids(_load_json(row["entity_ids_json"], [])),
            "blocked_entity_ids": _positive_ids(_load_json(row["blocked_entity_ids_json"], [])),
            "evidence": _load_json(row["evidence_json"], {}),
            "recommended_action": str(row["recommended_action"]),
            "status": str(row["status"]),
            "first_seen_at": _utc_iso(float(row["first_seen_at"])),
            "last_seen_at": _utc_iso(float(row["last_seen_at"])),
            "resolved_at": _utc_iso(float(row["resolved_at"])) if row["resolved_at"] is not None else None,
        }

    def snapshot(self, root: str, *, issue_limit: int = 100, action_limit: int = 50) -> dict[str, Any]:
        scan = self.conn.execute(
            "SELECT status,reason,scanned_at FROM logical_issue_scans WHERE root=?", (root,)
        ).fetchone()
        issue_rows = self.conn.execute(
            """
            SELECT * FROM logical_issues
            WHERE root=? AND status='active'
            ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     last_seen_at DESC,issue_id ASC
            LIMIT ?
            """,
            (root, max(1, int(issue_limit))),
        ).fetchall()
        action_rows = self.conn.execute(
            """
            SELECT issue_id,action,actor,status,reason,before_json,after_json,created_at
            FROM logical_issue_actions WHERE root=? ORDER BY created_at DESC,id DESC LIMIT ?
            """,
            (root, max(1, int(action_limit))),
        ).fetchall()
        issues = [self._issue_payload(row) for row in issue_rows]
        actions = [
            {
                "issue_id": str(row["issue_id"]),
                "action": str(row["action"]),
                "actor": str(row["actor"]),
                "status": str(row["status"]),
                "reason": str(row["reason"]),
                "before": _load_json(row["before_json"], []),
                "after": _load_json(row["after_json"], []),
                "created_at": _utc_iso(float(row["created_at"])),
            }
            for row in action_rows
        ]
        errors = sum(1 for issue in issues if issue.get("severity") == "error")
        warnings = sum(1 for issue in issues if issue.get("severity") == "warning")
        return {
            "schema": LOGICAL_ISSUES_SCHEMA,
            "status": str(scan["status"]) if scan is not None else "never",
            "reason": str(scan["reason"]) if scan is not None else "not_scanned",
            "scanned_at": _utc_iso(float(scan["scanned_at"])) if scan is not None else None,
            "summary": {"active": len(issues), "errors": errors, "warnings": warnings},
            "issues": issues,
            "actions": actions,
        }


def scan_install_logical_issues(cfg: Any, install: Any) -> dict[str, Any]:
    root = str(getattr(install, "root", "") or "").strip()
    store = LogicalIssueStore(str(cfg.state_db_path))
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


def read_logical_issues_snapshot(state_db_path: str, root: str) -> dict[str, Any]:
    store = LogicalIssueStore(state_db_path)
    try:
        return store.snapshot(root)
    finally:
        store.close()


def logical_issue_blocked_segment_ids(snapshot: dict[str, Any]) -> set[int]:
    issues = snapshot.get("issues") if isinstance(snapshot.get("issues"), list) else []
    blocked: set[int] = set()
    for issue in issues:
        if not isinstance(issue, dict) or str(issue.get("status") or "active") != "active":
            continue
        blocked.update(_positive_ids(issue.get("blocked_entity_ids")))
    return blocked


def remediate_logical_issue(
    cfg: Any,
    install: Any,
    *,
    issue_id: str,
    action: str,
    actor: str,
) -> dict[str, Any]:
    root = str(getattr(install, "root", "") or "").strip()
    db_cfg = getattr(install, "db", None)
    action_clean = str(action or "").strip().lower()
    issue_id_clean = str(issue_id or "").strip()
    if action_clean not in SUPPORTED_REMEDIATIONS:
        raise ValueError(f"unsupported logical issue remediation: {action_clean}")
    if not root or db_cfg is None:
        raise ValueError("instance database configuration is unavailable")

    scan_install_logical_issues(cfg, install)
    store = LogicalIssueStore(str(cfg.state_db_path))
    issue = store.active_issue(root, issue_id_clean)
    if issue is None:
        store.close()
        raise ValueError("logical issue is no longer active; rescan before remediation")
    blocked_ids = _positive_ids(issue.get("blocked_entity_ids"))
    if not blocked_ids:
        store.close()
        raise ValueError("logical issue has no affected published segments")

    reason = f"{issue.get('title')}: {issue.get('reason')}"
    before: object = []
    after: object = []
    status = "error"
    try:
        result = MauticDB(db_cfg).disable_segments(
            blocked_ids,
            issue_id=issue_id_clean,
            reason=reason,
            actor=str(actor or "Emmy Starwell"),
        )
        before = result.get("before", [])
        after = result.get("after", [])
        status = "success"
        store.record_action(
            root=root,
            issue_id=issue_id_clean,
            action=action_clean,
            actor=actor,
            status=status,
            reason=reason,
            before=before,
            after=after,
        )
    except Exception as exc:
        store.record_action(
            root=root,
            issue_id=issue_id_clean,
            action=action_clean,
            actor=actor,
            status="error",
            reason=str(exc),
            before=before,
            after=after,
        )
        store.close()
        raise

    store.close()
    snapshot = scan_install_logical_issues(cfg, install)
    return {
        "status": status,
        "root": root,
        "issue_id": issue_id_clean,
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
