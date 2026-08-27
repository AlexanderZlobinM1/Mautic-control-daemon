from __future__ import annotations

import json
import re
from typing import Any


_VERSION_RE = re.compile(r"\d+|[A-Za-z]+")
_BUNDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*Bundle(?:Dev)?$")
_PHASE_ALIASES = {
    "any": "any",
    "all": "any",
    "selection": "selection",
    "install": "selection",
    "catalog": "selection",
    "remediation": "remediation",
    "policy": "remediation",
}


def _valid_bundle_name(name: str) -> bool:
    return bool(_BUNDLE_RE.match(str(name or "").strip()))


def _normalize_sender_type(value: str | None) -> str:
    return str(value or "").strip().lower()


def _version_key(value: str | None) -> tuple[Any, ...]:
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    if not text or text in {"-", "none", "null"}:
        return tuple()
    out: list[Any] = []
    for token in _VERSION_RE.findall(text.replace("-", ".")):
        out.append(int(token) if token.isdigit() else token)
    return tuple(out)


def _version_cmp(left: str | None, right: str | None) -> int | None:
    lkey = _version_key(left)
    rkey = _version_key(right)
    if not lkey or not rkey:
        return None
    if lkey < rkey:
        return -1
    if lkey > rkey:
        return 1
    return 0


def normalize_interaction_rules(raw: Any) -> list[dict[str, Any]]:
    value = raw
    if isinstance(value, str):
        text = str(value).strip()
        if not text:
            return []
        value = json.loads(text)
    if not isinstance(value, list):
        return []

    out: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        when = entry.get("when")
        if not isinstance(when, dict):
            when = {}
        phases_raw = entry.get("phase", entry.get("phases", ["any"]))
        if isinstance(phases_raw, str):
            phases_input = [phases_raw]
        elif isinstance(phases_raw, list):
            phases_input = phases_raw
        else:
            phases_input = ["any"]
        phases: list[str] = []
        for raw_phase in phases_input:
            key = _PHASE_ALIASES.get(str(raw_phase or "").strip().lower())
            if key and key not in phases:
                phases.append(key)
        if not phases:
            phases = ["any"]

        selection_conflicts: list[str] = []
        for token in list(entry.get("selection_conflicts") or []):
            bundle = str(token or "").strip()
            if _valid_bundle_name(bundle) and bundle not in selection_conflicts:
                selection_conflicts.append(bundle)

        version_rules: list[dict[str, Any]] = []
        for item in list(when.get("version_rules") or []):
            if not isinstance(item, dict):
                continue
            bundle = str(item.get("bundle", "") or "").strip()
            if not _valid_bundle_name(bundle):
                continue
            rule: dict[str, Any] = {"bundle": bundle}
            if "exists" in item:
                rule["exists"] = bool(item.get("exists"))
            for key in ("equals", "not_equals", "lt", "lte", "gt", "gte"):
                raw_val = item.get(key)
                if isinstance(raw_val, str) and raw_val.strip():
                    rule[key] = raw_val.strip()
            for key in ("equals_any", "not_equals_any", "prefix_in", "not_prefix_in"):
                raw_list = item.get(key)
                if isinstance(raw_list, list):
                    vals = [str(x or "").strip() for x in raw_list if str(x or "").strip()]
                    if vals:
                        rule[key] = vals
            version_rules.append(rule)

        when_out: dict[str, Any] = {}
        sender_types = when.get("sender_types")
        if isinstance(sender_types, list):
            vals = [_normalize_sender_type(x) for x in sender_types if str(x or "").strip()]
            if vals:
                when_out["sender_types"] = vals
        for key in ("installed_bundles_all", "installed_bundles_any", "installed_bundles_none"):
            raw_list = when.get(key)
            if isinstance(raw_list, list):
                vals = [str(x or "").strip() for x in raw_list if _valid_bundle_name(str(x or "").strip())]
                if vals:
                    when_out[key] = vals
        if version_rules:
            when_out["version_rules"] = version_rules
        out.append(
            {
                "name": str(entry.get("name", "") or "").strip(),
                "priority": int(entry.get("priority", 0) or 0),
                "phase": phases,
                "when": when_out,
                "selection_conflicts": selection_conflicts,
            }
        )
    return out


def selection_conflicts_for_rules(
    rules: list[dict[str, Any]],
    *,
    sender_type: str | None,
    installed_plugins: dict[str, str],
) -> set[str]:
    conflicts: set[str] = set()
    for rule in sorted(normalize_interaction_rules(rules), key=lambda x: int(x.get("priority", 0)), reverse=True):
        if not _phase_matches(rule, "selection"):
            continue
        if not _when_matches(rule.get("when"), sender_type=sender_type, installed_plugins=installed_plugins):
            continue
        for bundle in list(rule.get("selection_conflicts") or []):
            if _valid_bundle_name(str(bundle or "").strip()):
                conflicts.add(str(bundle).strip())
    return conflicts


def _phase_matches(rule: dict[str, Any], phase: str) -> bool:
    phases = [str(x or "").strip().lower() for x in list(rule.get("phase") or []) if str(x or "").strip()]
    if not phases:
        return True
    return "any" in phases or str(phase or "").strip().lower() in phases


def _when_matches(
    when: Any,
    *,
    sender_type: str | None,
    installed_plugins: dict[str, str],
) -> bool:
    if not isinstance(when, dict):
        return True
    sender_types = when.get("sender_types")
    if isinstance(sender_types, list) and sender_types:
        allowed = {_normalize_sender_type(x) for x in sender_types if str(x or "").strip()}
        if allowed and _normalize_sender_type(sender_type) not in allowed:
            return False
    installed = {str(k or "").strip(): str(v or "").strip() for k, v in dict(installed_plugins or {}).items() if _valid_bundle_name(str(k or "").strip())}
    for key, mode in (
        ("installed_bundles_all", "all"),
        ("installed_bundles_any", "any"),
        ("installed_bundles_none", "none"),
    ):
        raw_list = when.get(key)
        if not isinstance(raw_list, list) or not raw_list:
            continue
        bundles = [str(x or "").strip() for x in raw_list if _valid_bundle_name(str(x or "").strip())]
        if not bundles:
            continue
        present = [bundle for bundle in bundles if bundle in installed]
        if mode == "all" and len(present) != len(bundles):
            return False
        if mode == "any" and not present:
            return False
        if mode == "none" and present:
            return False
    for raw_rule in list(when.get("version_rules") or []):
        if not isinstance(raw_rule, dict):
            continue
        bundle = str(raw_rule.get("bundle", "") or "").strip()
        if not _valid_bundle_name(bundle):
            continue
        exists = bundle in installed and bool(installed.get(bundle))
        version = str(installed.get(bundle, "") or "").strip()
        if "exists" in raw_rule and exists is not bool(raw_rule.get("exists")):
            return False
        if "equals" in raw_rule and version != str(raw_rule.get("equals") or "").strip():
            return False
        if "not_equals" in raw_rule and version == str(raw_rule.get("not_equals") or "").strip():
            return False
        eq_any = [str(x or "").strip() for x in list(raw_rule.get("equals_any") or []) if str(x or "").strip()]
        if eq_any and version not in eq_any:
            return False
        neq_any = [str(x or "").strip() for x in list(raw_rule.get("not_equals_any") or []) if str(x or "").strip()]
        if neq_any and version in neq_any:
            return False
        prefix_in = [str(x or "").strip().lower() for x in list(raw_rule.get("prefix_in") or []) if str(x or "").strip()]
        if prefix_in:
            version_l = version.lower()
            if not any(version_l == prefix or version_l.startswith(prefix + ".") for prefix in prefix_in):
                return False
        not_prefix_in = [str(x or "").strip().lower() for x in list(raw_rule.get("not_prefix_in") or []) if str(x or "").strip()]
        if not_prefix_in:
            version_l = version.lower()
            if any(version_l == prefix or version_l.startswith(prefix + ".") for prefix in not_prefix_in):
                return False
        for key in ("lt", "lte", "gt", "gte"):
            target = str(raw_rule.get(key, "") or "").strip()
            if not target:
                continue
            cmp_val = _version_cmp(version, target)
            if cmp_val is None:
                return False
            if key == "lt" and not (cmp_val < 0):
                return False
            if key == "lte" and not (cmp_val <= 0):
                return False
            if key == "gt" and not (cmp_val > 0):
                return False
            if key == "gte" and not (cmp_val >= 0):
                return False
    return True
