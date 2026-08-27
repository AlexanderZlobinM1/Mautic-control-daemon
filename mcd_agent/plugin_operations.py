from __future__ import annotations

import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from mcd_agent.install_type import plugin_dir_candidates


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{1,191}$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")


def instance_runtime_keys(inst: object) -> list[str]:
    values = [
        getattr(inst, "instance_uid", None),
        getattr(inst, "root", None),
        getattr(inst, "name", None),
        getattr(inst, "primary_domain", None),
        *(getattr(inst, "domains", None) or []),
    ]
    out: list[str] = []
    for value in values:
        key = str(value or "").strip()
        if key and key not in out:
            out.append(key)
    return out


def _bundle_installed(inst: object, bundle: str) -> bool:
    expected = str(bundle or "").strip().lower()
    root = str(getattr(inst, "root", "") or "").strip()
    if not expected or not root:
        return False
    for directory in plugin_dir_candidates(root):
        try:
            if (directory / bundle).is_dir():
                return True
            if directory.is_dir() and any(row.is_dir() and row.name.lower() == expected for row in directory.iterdir()):
                return True
        except OSError:
            continue
    return False


def operations_for_instance(config: object, inst: object) -> list[dict[str, Any]]:
    definitions = getattr(config, "plugin_operations", {})
    if not isinstance(definitions, dict):
        return []
    raw_items: Any = []
    for key in instance_runtime_keys(inst) + ["default"]:
        if key in definitions:
            raw_items = definitions.get(key)
            break
    if not isinstance(raw_items, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        operation_key = str(raw.get("operation_key", "") or "").strip().lower()
        bundle = str(raw.get("bundle", "") or "").strip()
        operation = raw.get("operation") if isinstance(raw.get("operation"), dict) else {}
        op_id = str(operation.get("id", "") or "").strip().lower()
        mode = str(operation.get("mode", "") or "").strip().lower()
        command = operation.get("command") if isinstance(operation.get("command"), dict) else {}
        command_name = str(command.get("name", "") or "").strip()
        if (
            not _ID_RE.fullmatch(operation_key)
            or not op_id
            or mode not in {"scheduled", "action"}
            or not _COMMAND_RE.fullmatch(command_name)
            or not _bundle_installed(inst, bundle)
        ):
            continue
        out.append(dict(raw))
    return out


def effective_values(config: object, inst: object, item: dict[str, Any]) -> dict[str, Any]:
    operation = item.get("operation") if isinstance(item.get("operation"), dict) else {}
    values = {
        str(field.get("id")): field.get("default")
        for field in list(operation.get("fields") or [])
        if isinstance(field, dict) and str(field.get("id", "") or "")
    }
    legacy_values = item.get("legacy_values") if isinstance(item.get("legacy_values"), dict) else {}
    for field_id in list(values):
        if field_id in legacy_values:
            values[field_id] = legacy_values[field_id]
    settings = getattr(config, "plugin_operation_instance_settings", {})
    if not isinstance(settings, dict):
        return values
    op_key = str(item.get("operation_key", "") or "").strip().lower()
    for key in instance_runtime_keys(inst) + ["default"]:
        block = settings.get(key)
        if not isinstance(block, dict):
            continue
        override = block.get(op_key)
        if isinstance(override, dict):
            for field_id in list(values):
                if field_id in override:
                    values[field_id] = override[field_id]
        break
    return values


def _argument_tokens(spec: Any, values: dict[str, Any]) -> list[str]:
    if isinstance(spec, str):
        return [spec]
    if not isinstance(spec, dict):
        return []
    if "literal" in spec:
        return [str(spec.get("literal") or "")]
    field_id = str(spec.get("field", "") or "").strip().lower()
    if not field_id or field_id not in values:
        return []
    value = values.get(field_id)
    flag = str(spec.get("flag", "") or "").strip()
    choices = spec.get("choices") if isinstance(spec.get("choices"), dict) else None
    if choices is not None:
        selected = value if isinstance(value, list) else [value]
        return [str(choices.get(str(item), "") or "") for item in selected if str(choices.get(str(item), "") or "")]
    if isinstance(value, bool):
        return [flag] if value and flag else []
    if value is None or str(value) == "":
        return []
    return ([flag] if flag else []) + [str(value)]


def command_template(item: dict[str, Any], values: dict[str, Any]) -> str:
    operation = item.get("operation") if isinstance(item.get("operation"), dict) else {}
    command = operation.get("command") if isinstance(operation.get("command"), dict) else {}
    if str(command.get("runner", "") or "") != "mautic_console":
        raise ValueError("scheduled plugin operations require mautic_console runner")
    name = str(command.get("name", "") or "").strip()
    if not _COMMAND_RE.fullmatch(name):
        raise ValueError("invalid plugin operation command")
    tokens = [name]
    for spec in list(command.get("args") or []):
        tokens.extend(_argument_tokens(spec, values))
    if any("\x00" in token or "\n" in token or "\r" in token for token in tokens):
        raise ValueError("invalid plugin operation command argument")
    return shlex.join(tokens)


def schedule_due(
    item: dict[str, Any],
    values: dict[str, Any],
    *,
    now_epoch: float,
    now_local: datetime,
    last_epoch: float,
) -> bool:
    operation = item.get("operation") if isinstance(item.get("operation"), dict) else {}
    for constraint in list(operation.get("constraints") or []):
        if not isinstance(constraint, dict):
            continue
        when = constraint.get("when") if isinstance(constraint.get("when"), dict) else {}
        if values.get(str(when.get("field", "") or "")) != when.get("equals", True):
            continue
        if not any(bool(values.get(str(field_id))) for field_id in list(constraint.get("require_any") or [])):
            return False
    schedule = operation.get("schedule") if isinstance(operation.get("schedule"), dict) else {}
    enabled_field = str(schedule.get("enabled_field", "enabled") or "enabled")
    if not bool(values.get(enabled_field, True)):
        return False
    interval_field = str(schedule.get("interval_field", "interval_sec") or "interval_sec")
    try:
        interval = max(60, int(values.get(interval_field, 60) or 60))
    except Exception:
        return False
    if last_epoch > 0 and now_epoch - last_epoch < interval:
        return False
    schedule_type = str(schedule.get("type", "interval") or "interval")
    if schedule_type == "quiet_window":
        hour_field = str(schedule.get("quiet_hour_field", "quiet_hour") or "quiet_hour")
        window_field = str(schedule.get("quiet_window_field", "quiet_window_min") or "quiet_window_min")
        try:
            start = max(0, min(23, int(values.get(hour_field, 0) or 0))) * 60
            window = max(1, min(1440, int(values.get(window_field, 60) or 60)))
        except Exception:
            return False
        current = now_local.hour * 60 + now_local.minute
        return ((current - start) % 1440) < window
    if schedule_type == "cron":
        cron_field = str(schedule.get("cron_field", "cron_expr") or "cron_expr")
        return cron_matches(str(values.get(cron_field, "") or ""), now_local)
    return True


def cron_matches(expression: str, now_local: datetime) -> bool:
    parts = str(expression or "").strip().split()
    if len(parts) != 5:
        return False

    def matches_field(token: str, value: int, minimum: int, maximum: int, *, dow: bool = False) -> bool:
        allowed: set[int] = set()
        for part in token.split(","):
            base, separator, step_raw = part.partition("/")
            if separator and (not step_raw.isdigit() or int(step_raw) <= 0):
                return False
            step = int(step_raw) if separator else 1
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start_raw, end_raw = base.split("-", 1)
                if not start_raw.isdigit() or not end_raw.isdigit():
                    return False
                start, end = int(start_raw), int(end_raw)
            elif base.isdigit():
                start = end = int(base)
            else:
                return False
            if start < minimum or end > maximum or start > end:
                return False
            allowed.update(range(start, end + 1, step))
        if dow and 7 in allowed:
            allowed.add(0)
        return value in allowed

    minute_ok = matches_field(parts[0], now_local.minute, 0, 59)
    hour_ok = matches_field(parts[1], now_local.hour, 0, 23)
    month_ok = matches_field(parts[3], now_local.month, 1, 12)
    dom_ok = matches_field(parts[2], now_local.day, 1, 31)
    dow_ok = matches_field(parts[4], (now_local.weekday() + 1) % 7, 0, 7, dow=True)
    if not (minute_ok and hour_ok and month_ok):
        return False
    if parts[2] != "*" and parts[4] != "*":
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def legacy_cron_rules(config: object, installs: list[object]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for inst in installs:
        root = str(getattr(inst, "root", "") or "").strip()
        for item in operations_for_instance(config, inst):
            operation = item.get("operation") if isinstance(item.get("operation"), dict) else {}
            legacy = operation.get("legacy_cron") if isinstance(operation.get("legacy_cron"), dict) else None
            if str(operation.get("mode", "") or "") != "scheduled" or legacy is None:
                continue
            match = [str(x) for x in list(legacy.get("match") or []) if str(x)]
            schedule = operation.get("schedule") if isinstance(operation.get("schedule"), dict) else {}
            if root and match:
                out.append(
                    {
                        "root": root,
                        "instance_key": instance_runtime_keys(inst)[0] if instance_runtime_keys(inst) else root,
                        "operation_key": str(item.get("operation_key", "") or ""),
                        "action": str(legacy.get("action", "comment") or "comment"),
                        "match": match,
                        "migrate_schedule": bool(legacy.get("migrate_schedule", False)),
                        "interval_field": str(legacy.get("interval_field", "interval_sec") or "interval_sec"),
                        "enabled_field": str(schedule.get("enabled_field", "enabled") or "enabled"),
                    }
                )
    return out
