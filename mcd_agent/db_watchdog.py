from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from mcd_agent.config import AgentConfig
from mcd_agent.db import MauticDB
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.models import MauticInstall


_DEFAULT_CFG: dict[str, Any] = {
    "enabled": False,
    "interval_sec": 300,
    "observe_only": True,
    "processlist_limit": 500,
    "sample_limit": 25,
    "long_query_sec": 900,
    "orphan_query_sec": 1200,
    "mcd_tmp_segment_query_sec": 1800,
    "kill_mcd_tmp_segment_queries": True,
    "global_rules": [],
    "host_rules": {},
}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[index]
        else:
            out[k] = v
    return out


def _host_aliases(cfg: AgentConfig) -> set[str]:
    ident = resolve_agent_identity(cfg)
    out: set[str] = set()
    for key in ("effective_mcc_host_name", "effective_hostname", "configured_host_name", "local_hostname"):
        raw = str(ident.get(key) or "").strip().lower()
        if raw:
            out.add(raw)
    return out


def _first_host_alias(cfg: AgentConfig) -> str:
    aliases = _host_aliases(cfg)
    if not aliases:
        return "unknown-host"
    return sorted(aliases)[0]


def _normalize_rule_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id", "")).strip() or f"rule_{idx}"
        enabled = bool(row.get("enabled", True))
        action = str(row.get("action", "observe")).strip().lower() or "observe"
        if action not in {"observe", "kill_connection", "kill_query"}:
            action = "observe"
        match_raw = row.get("match")
        match = dict(match_raw) if isinstance(match_raw, dict) else {}
        # Top-level shorthand fields are merged into match block.
        for key in (
            "command_in",
            "command",
            "state_regex",
            "info_regex",
            "user_regex",
            "db_regex",
            "min_time_sec",
        ):
            if key in row and key not in match:
                match[key] = row.get(key)
        out.append(
            {
                "id": rid,
                "enabled": enabled,
                "description": str(row.get("description", "")).strip(),
                "action": action,
                "match": match,
            }
        )
    return out


def _merge_rules(global_rules: list[dict[str, Any]], host_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    idx_by_id: dict[str, int] = {}
    for row in global_rules:
        rid = str(row.get("id", "")).strip()
        if rid and rid not in idx_by_id:
            idx_by_id[rid] = len(out)
        out.append(dict(row))
    for row in host_rules:
        rid = str(row.get("id", "")).strip()
        if rid and rid in idx_by_id:
            out[idx_by_id[rid]] = dict(row)
            continue
        if rid and rid not in idx_by_id:
            idx_by_id[rid] = len(out)
        out.append(dict(row))
    return out


def _host_patch_from_rules(raw: Any, aliases: set[str]) -> dict[str, Any]:
    if isinstance(raw, dict):
        # Exact host mapping: { "<host>": { ...patch... } }
        for alias in aliases:
            row = raw.get(alias) or raw.get(alias.lower()) or raw.get(alias.upper())
            if isinstance(row, dict):
                return dict(row)
        return {}
    if not isinstance(raw, list):
        return {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        host_match_raw = row.get("host_match")
        host_match: list[str] = []
        if isinstance(host_match_raw, str):
            host_match = [host_match_raw.strip().lower()]
        elif isinstance(host_match_raw, list):
            host_match = [str(x).strip().lower() for x in host_match_raw if str(x).strip()]
        host_regex = str(row.get("host_regex", "")).strip()
        matched = False
        if host_match and aliases.intersection(set(host_match)):
            matched = True
        elif host_regex:
            try:
                rx = re.compile(host_regex, flags=re.IGNORECASE)
                for alias in aliases:
                    if rx.search(alias):
                        matched = True
                        break
            except Exception:
                matched = False
        if not matched:
            continue
        patch_raw = row.get("patch")
        if isinstance(patch_raw, dict):
            return dict(patch_raw)
        # Backward-compatible inline row as patch.
        patch = dict(row)
        patch.pop("host_match", None)
        patch.pop("host_regex", None)
        return patch
    return {}


def effective_db_watchdog_config(cfg: AgentConfig) -> dict[str, Any]:
    raw = dict(getattr(cfg, "db_watchdog", {}) or {})
    aliases = _host_aliases(cfg)
    base = _deep_merge(_DEFAULT_CFG, raw if isinstance(raw, dict) else {})
    host_patch = _host_patch_from_rules(base.get("host_rules"), aliases)
    merged = _deep_merge(base, host_patch if isinstance(host_patch, dict) else {})
    global_rules = _normalize_rule_list(merged.get("global_rules"))
    host_rules = _normalize_rule_list(host_patch.get("rules") if isinstance(host_patch, dict) else None)
    # Support old key `rules` as global fallback.
    if not global_rules:
        global_rules = _normalize_rule_list(merged.get("rules"))
    merged["rules"] = _merge_rules(global_rules, host_rules)
    merged["enabled"] = bool(merged.get("enabled", False))
    merged["observe_only"] = bool(merged.get("observe_only", True))
    merged["interval_sec"] = max(30, int(merged.get("interval_sec", 300) or 300))
    merged["processlist_limit"] = max(10, min(5000, int(merged.get("processlist_limit", 500) or 500)))
    merged["sample_limit"] = max(1, min(200, int(merged.get("sample_limit", 25) or 25)))
    merged["long_query_sec"] = max(1, int(merged.get("long_query_sec", 900) or 900))
    merged["orphan_query_sec"] = max(1, int(merged.get("orphan_query_sec", 1200) or 1200))
    merged["mcd_tmp_segment_query_sec"] = max(60, int(merged.get("mcd_tmp_segment_query_sec", 1800) or 1800))
    merged["kill_mcd_tmp_segment_queries"] = bool(merged.get("kill_mcd_tmp_segment_queries", True))
    return merged


def _norm_process_row(row: dict[str, Any]) -> dict[str, Any]:
    def _ival(v: Any) -> int:
        try:
            return int(v or 0)
        except Exception:
            return 0

    info_raw = str(row.get("Info", "") or "").strip()
    info_head = re.sub(r"\s+", " ", info_raw)[:220]
    return {
        "id": _ival(row.get("Id")),
        "user": str(row.get("User", "") or "").strip(),
        "host": str(row.get("Host", "") or "").strip(),
        "db": str(row.get("db", "") or row.get("Db", "") or "").strip(),
        "command": str(row.get("Command", "") or "").strip(),
        "time_sec": _ival(row.get("Time")),
        "state": str(row.get("State", "") or "").strip(),
        "info_head": info_head,
    }


def _command_matches(command: str, command_in: Any, command_eq: Any) -> bool:
    if command_eq is not None and str(command_eq).strip():
        return command.lower() == str(command_eq).strip().lower()
    if isinstance(command_in, str):
        allow = {x.strip().lower() for x in command_in.split(",") if x.strip()}
    elif isinstance(command_in, list):
        allow = {str(x).strip().lower() for x in command_in if str(x).strip()}
    else:
        allow = set()
    if not allow:
        return True
    return command.lower() in allow


def _regex_match(pattern: Any, value: str, *, errors: list[str], rule_id: str, field: str) -> bool:
    raw = str(pattern or "").strip()
    if not raw:
        return True
    try:
        return bool(re.search(raw, value, flags=re.IGNORECASE))
    except Exception as e:
        errors.append(f"{rule_id}:{field}:invalid_regex:{e}")
        return False


def _rule_matches(rule: dict[str, Any], row: dict[str, Any], *, errors: list[str]) -> bool:
    if not bool(rule.get("enabled", True)):
        return False
    match = rule.get("match")
    if not isinstance(match, dict):
        return False
    rid = str(rule.get("id", "rule")).strip() or "rule"
    min_time_sec = int(match.get("min_time_sec", 0) or 0)
    if min_time_sec > 0 and int(row.get("time_sec", 0) or 0) < min_time_sec:
        return False
    command = str(row.get("command", "") or "").strip()
    if not _command_matches(command, match.get("command_in"), match.get("command")):
        return False
    state = str(row.get("state", "") or "")
    info = str(row.get("info_head", "") or "")
    user = str(row.get("user", "") or "")
    db_name = str(row.get("db", "") or "")
    if not _regex_match(match.get("state_regex"), state, errors=errors, rule_id=rid, field="state_regex"):
        return False
    if not _regex_match(match.get("info_regex"), info, errors=errors, rule_id=rid, field="info_regex"):
        return False
    if not _regex_match(match.get("user_regex"), user, errors=errors, rule_id=rid, field="user_regex"):
        return False
    if not _regex_match(match.get("db_regex"), db_name, errors=errors, rule_id=rid, field="db_regex"):
        return False
    return True


def _apply_rule_action(
    db: MauticDB,
    *,
    rule: dict[str, Any],
    row: dict[str, Any],
    observe_only: bool,
) -> dict[str, Any] | None:
    action = str(rule.get("action", "observe") or "observe").strip().lower()
    if action not in {"kill_query", "kill_connection"}:
        return None
    pid = int(row.get("id", 0) or 0)
    rid = str(rule.get("id", "rule")).strip() or "rule"
    event: dict[str, Any] = {
        "rule_id": rid,
        "action": action,
        "pid": pid,
        "time_sec": int(row.get("time_sec", 0) or 0),
        "db": str(row.get("db", "") or ""),
        "user": str(row.get("user", "") or ""),
        "info_head": str(row.get("info_head", "") or ""),
    }
    if observe_only:
        event["status"] = "observe_only"
        return event
    if pid <= 0:
        event["status"] = "skipped"
        event["reason"] = "invalid_pid"
        return event
    if str(row.get("command", "") or "").strip().lower() != "query":
        event["status"] = "skipped"
        event["reason"] = "not_query"
        return event
    try:
        if action == "kill_query":
            db.kill_query(pid)
        else:
            db.kill_connection(pid)
    except Exception as e:
        event["status"] = "error"
        event["reason"] = str(e)[:220]
        return event
    event["status"] = "applied"
    return event


def _is_stale_mcd_tmp_segment_query(row: dict[str, Any], *, min_time_sec: int) -> bool:
    """Match only MCD-owned SQL segment temp-table rebuild queries."""
    if str(row.get("command", "") or "").strip().lower() != "query":
        return False
    if int(row.get("time_sec", 0) or 0) < int(min_time_sec):
        return False
    info = str(row.get("info_head", "") or "").lower()
    return "mcd_tmp_segment_leads" in info and "insert ignore into" in info


def collect_db_watchdog_snapshot(
    *,
    cfg: AgentConfig,
    inst: MauticInstall,
    running_task_types: list[str] | None = None,
) -> dict[str, Any]:
    profile = effective_db_watchdog_config(cfg)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    host_name = _first_host_alias(cfg)
    if not inst.db:
        return {
            "checked_at_utc": now,
            "host": host_name,
            "root": inst.root,
            "enabled": bool(profile.get("enabled", False)),
            "observe_only": bool(profile.get("observe_only", True)),
            "status": "skipped",
            "reason": "instance_has_no_db",
        }

    db = MauticDB(inst.db)
    errors: list[str] = []
    rows_raw: list[dict[str, Any]] = []
    try:
        rows_raw = db.fetch_processlist(limit=int(profile.get("processlist_limit", 500) or 500))
    except Exception as e:
        return {
            "checked_at_utc": now,
            "host": host_name,
            "root": inst.root,
            "enabled": bool(profile.get("enabled", False)),
            "observe_only": bool(profile.get("observe_only", True)),
            "status": "error",
            "reason": f"processlist_failed:{e}",
        }

    rows = [_norm_process_row(r) for r in rows_raw]
    long_query_sec = int(profile.get("long_query_sec", 900) or 900)
    orphan_query_sec = int(profile.get("orphan_query_sec", 1200) or 1200)
    sample_limit = int(profile.get("sample_limit", 25) or 25)
    metadata_lock_waits = [
        r for r in rows if "metadata lock" in str(r.get("state", "")).lower() and str(r.get("command", "")).lower() == "query"
    ]
    long_queries = [r for r in rows if str(r.get("command", "")).lower() == "query" and int(r.get("time_sec", 0) or 0) >= long_query_sec]
    running_types = [str(x).strip() for x in (running_task_types or []) if str(x).strip()]
    running_managed = len(running_types)
    orphan_candidates = [
        r
        for r in rows
        if running_managed == 0
        and str(r.get("command", "")).lower() == "query"
        and int(r.get("time_sec", 0) or 0) >= orphan_query_sec
    ]

    rules = profile.get("rules")
    rules_list = rules if isinstance(rules, list) else []
    hit_counts: dict[str, int] = {}
    rule_samples: list[dict[str, Any]] = []
    rule_actions: list[dict[str, Any]] = []
    observe_only = bool(profile.get("observe_only", True))
    acted_pids: set[int] = set()
    if bool(profile.get("kill_mcd_tmp_segment_queries", True)):
        builtin_rule = {
            "id": "mcd_tmp_segment_leads_stale",
            "action": "kill_query",
            "description": "Kill stale MCD SQL-segment temp-table rebuild queries.",
        }
        builtin_threshold = int(profile.get("mcd_tmp_segment_query_sec", 1800) or 1800)
        for row in rows:
            pid = int(row.get("id", 0) or 0)
            if pid in acted_pids:
                continue
            if not _is_stale_mcd_tmp_segment_query(row, min_time_sec=builtin_threshold):
                continue
            hit_counts["mcd_tmp_segment_leads_stale"] = int(hit_counts.get("mcd_tmp_segment_leads_stale", 0) or 0) + 1
            action_event = _apply_rule_action(db, rule=builtin_rule, row=row, observe_only=False)
            if action_event is not None:
                action_event["builtin"] = True
                action_event["observe_only_overridden"] = observe_only
                rule_actions.append(action_event)
                acted_pids.add(pid)

    for row in rows:
        matched_ids: list[str] = []
        for rule in rules_list:
            if not isinstance(rule, dict):
                continue
            if _rule_matches(rule, row, errors=errors):
                rid = str(rule.get("id", "rule")).strip() or "rule"
                matched_ids.append(rid)
                hit_counts[rid] = int(hit_counts.get(rid, 0) or 0) + 1
                action = str(rule.get("action", "observe") or "observe").strip().lower()
                pid = int(row.get("id", 0) or 0)
                if action in {"kill_query", "kill_connection"} and pid not in acted_pids:
                    action_event = _apply_rule_action(db, rule=rule, row=row, observe_only=observe_only)
                    if action_event is not None:
                        rule_actions.append(action_event)
                        acted_pids.add(pid)
        if matched_ids:
            sample = {
                "pid": int(row.get("id", 0) or 0),
                "time_sec": int(row.get("time_sec", 0) or 0),
                "command": str(row.get("command", "") or ""),
                "state": str(row.get("state", "") or ""),
                "db": str(row.get("db", "") or ""),
                "user": str(row.get("user", "") or ""),
                "info_head": str(row.get("info_head", "") or ""),
                "matched_rules": matched_ids,
            }
            rule_samples.append(sample)

    top_slowest = sorted(
        [r for r in rows if str(r.get("command", "")).lower() == "query"],
        key=lambda x: int(x.get("time_sec", 0) or 0),
        reverse=True,
    )[:sample_limit]
    top_slowest_out = [
        {
            "pid": int(r.get("id", 0) or 0),
            "time_sec": int(r.get("time_sec", 0) or 0),
            "command": str(r.get("command", "") or ""),
            "state": str(r.get("state", "") or ""),
            "db": str(r.get("db", "") or ""),
            "user": str(r.get("user", "") or ""),
            "info_head": str(r.get("info_head", "") or ""),
        }
        for r in top_slowest
    ]

    return {
        "checked_at_utc": now,
        "host": host_name,
        "root": inst.root,
        "enabled": bool(profile.get("enabled", False)),
        "observe_only": bool(profile.get("observe_only", True)),
        "status": "ok",
        "config": {
            "interval_sec": int(profile.get("interval_sec", 300) or 300),
            "long_query_sec": long_query_sec,
            "orphan_query_sec": orphan_query_sec,
            "processlist_limit": int(profile.get("processlist_limit", 500) or 500),
            "sample_limit": sample_limit,
            "rules_count": len(rules_list),
            "global_rules_count": len(_normalize_rule_list(profile.get("global_rules"))),
            "mcd_tmp_segment_query_sec": int(profile.get("mcd_tmp_segment_query_sec", 1800) or 1800),
            "kill_mcd_tmp_segment_queries": bool(profile.get("kill_mcd_tmp_segment_queries", True)),
        },
        "running": {
            "managed_tasks": running_managed,
            "task_types": sorted(set(running_types)),
        },
        "processlist": {
            "total": len(rows),
            "queries": sum(1 for r in rows if str(r.get("command", "")).lower() == "query"),
            "sleep": sum(1 for r in rows if str(r.get("command", "")).lower() == "sleep"),
            "metadata_lock_waits": len(metadata_lock_waits),
            "long_queries": len(long_queries),
            "orphan_candidates": len(orphan_candidates),
            "max_query_time_sec": max([int(r.get("time_sec", 0) or 0) for r in rows], default=0),
            "top_slowest": top_slowest_out,
        },
        "rules": {
            "hit_total": int(sum(hit_counts.values())),
            "hit_counts": hit_counts,
            "samples": rule_samples[:sample_limit],
            "actions": rule_actions[:sample_limit],
        },
        "errors": errors[:50],
    }
