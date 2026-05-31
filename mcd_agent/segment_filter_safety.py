from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import phpserialize


_BARE_RELATIVE_DATE_RE = re.compile(
    r"^(?:today|tomorrow|yesterday|now)\s*[+-]\s*\d+$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SegmentFilterIssue:
    segment_id: int
    field: str
    value: str
    reason: str


def _php_array_values(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _decode_filters(filters_raw: object) -> list[dict[str, object]]:
    raw = str(filters_raw or "").strip()
    if not raw:
        return []
    try:
        parsed = phpserialize.loads(raw.encode("utf-8"), decode_strings=True)
    except Exception:
        return []
    rows: list[dict[str, object]] = []
    for item in _php_array_values(parsed):
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _clause_filter_value(clause: dict[str, object]) -> object:
    props = clause.get("properties")
    if isinstance(props, dict) and "filter" in props:
        return props.get("filter")
    return clause.get("filter")


def _filter_values(raw: object) -> list[str]:
    values: list[str] = []
    for item in _php_array_values(raw):
        if isinstance(item, bytes):
            text = item.decode("utf-8", errors="ignore").strip()
        else:
            text = str(item or "").strip()
        if text:
            values.append(text)
    return values


def _is_date_like_clause(clause: dict[str, object]) -> bool:
    obj = str(clause.get("object") or "").strip().lower()
    if obj and obj != "lead":
        return False
    field_type = str(clause.get("type") or "").strip().lower()
    if field_type in {"date", "datetime", "time"}:
        return True
    field = str(clause.get("field") or "").strip().lower()
    return field.endswith("_date") or field.endswith("_datetime") or field in {"date_added", "date_modified"}


def segment_invalid_filter_issues(segment_rows: list[dict[str, Any]]) -> dict[int, list[SegmentFilterIssue]]:
    issues: dict[int, list[SegmentFilterIssue]] = {}
    for row in segment_rows:
        try:
            segment_id = int(row.get("id") or 0)
        except Exception:
            segment_id = 0
        if segment_id <= 0:
            continue
        for clause in _decode_filters(row.get("filters")):
            if not _is_date_like_clause(clause):
                continue
            field = str(clause.get("field") or "").strip()
            for value in _filter_values(_clause_filter_value(clause)):
                if _BARE_RELATIVE_DATE_RE.match(value):
                    issues.setdefault(segment_id, []).append(
                        SegmentFilterIssue(
                            segment_id=segment_id,
                            field=field,
                            value=value,
                            reason="date_expression_missing_unit",
                        )
                    )
    return issues


def format_segment_filter_issues(issues: dict[int, list[SegmentFilterIssue]], *, max_items: int = 8) -> str:
    parts: list[str] = []
    for segment_id in sorted(issues):
        for issue in issues[segment_id]:
            parts.append(f"{segment_id}:{issue.field}={issue.value} ({issue.reason})")
            if len(parts) >= max_items:
                break
        if len(parts) >= max_items:
            break
    return ", ".join(parts)
