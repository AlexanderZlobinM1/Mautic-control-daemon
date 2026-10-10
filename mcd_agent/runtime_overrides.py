from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent import __version__
from mcd_agent.config import (
    AgentConfig,
    apply_runtime_overrides,
    runtime_overrides_from_config_file,
    runtime_remote_allowed_keys,
    runtime_supported_keys,
)
from mcd_agent.host_identity import resolve_agent_identity


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 12) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def _api_base(cfg: AgentConfig) -> str | None:
    if not cfg.mcc_url:
        return None
    return cfg.mcc_url.rstrip("/")


_INSTANCE_RUNTIME_KEYS = {
    "segment_whitelist_instance_settings",
    "segment_priority_instance_settings",
    "segment_super_priority_instance_settings",
    "segment_special_rings_v1",
    "segment_recurring_priority_v1",
    "campaign_whitelist_instance_settings",
    "page_hits_orphan_cleanup_instance_settings",
    "plugin_operation_instance_settings",
    "message_queue_instance_settings",
    "monitored_email_parser_instance_settings",
    "form_embed_instance_settings",
    "empty_leads_cleanup_instance_settings",
}

_SPECIAL_RING_RUNTIME_KEY = "segment_special_rings_v1"
_SPECIAL_RING_TARGET_KEYS = {
    "priority": "segment_priority_instance_settings",
    "super_priority": "segment_super_priority_instance_settings",
}
_PROFILE_NAMES = {
    "passive",
    "tiny",
    "mini",
    "midi",
    "maxi",
    "hiload",
    "ultra",
    "farm-tiny",
    "farm-mini",
    "farm-midi",
    "farm-maxi",
    "farm-hiload",
    "farm-ultra",
    "custom",
}


def _instance_keys(inst: object) -> list[str]:
    values = [
        getattr(inst, "instance_uid", None),
        getattr(inst, "root", None),
        getattr(inst, "name", None),
        getattr(inst, "primary_domain", None),
    ]
    domains = getattr(inst, "domains", None)
    if isinstance(domains, list):
        values.extend(domains)
    return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def _matching_instance_entry(entries: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        if key in entries:
            return key
    aliases = set(keys)
    canonical = [
        str(key)
        for key in entries
        if "@" in str(key) and str(key).split("@", 1)[0] in aliases
    ]
    return canonical[0] if len(canonical) == 1 else None


def _canonical_instance_uids(instance_uids: list[str]) -> list[str]:
    canonical: list[str] = []
    for raw_uid in instance_uids:
        uid = str(raw_uid or "").strip()
        if not uid:
            continue
        canonical.append(uid)
    return list(dict.fromkeys(canonical))


def _decode_json_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _segment_ids(value: object) -> list[int] | None:
    value = _decode_json_value(value)
    if isinstance(value, dict):
        for key in ("segments", "segment_ids", "ids", "items"):
            if key in value:
                return _segment_ids(value.get(key))
        return None
    if isinstance(value, (tuple, list, set)):
        out: list[int] = []
        seen: set[int] = set()
        for raw in value:
            if isinstance(raw, bool):
                return None
            try:
                segment_id = int(raw)
            except (TypeError, ValueError):
                return None
            if segment_id <= 0:
                return None
            if segment_id in seen:
                continue
            seen.add(segment_id)
            out.append(segment_id)
        return out
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        return _segment_ids(parts)
    return None


def _normalize_special_ring_lane(value: object) -> dict[str, Any] | None:
    value = _decode_json_value(value)
    if isinstance(value, bool):
        return {"segments": []} if not value else None
    if isinstance(value, (tuple, list, set, str)):
        ids = _segment_ids(value)
        return {"segments": ids} if ids is not None else None
    if not isinstance(value, dict):
        return None
    supported_keys = {
        "enabled",
        "disabled",
        "segments",
        "segment_ids",
        "ids",
        "items",
        "slots_override",
    }
    if set(value) - supported_keys:
        return None
    if value.get("enabled") is False or value.get("disabled") is True:
        return {"segments": []}
    if not value:
        return {"segments": []}
    ids = _segment_ids(value)
    if ids is None:
        return None
    normalized: dict[str, Any] = {"segments": ids}
    if "slots_override" in value:
        raw_slots = value.get("slots_override")
        if isinstance(raw_slots, bool):
            return None
        try:
            slots = int(raw_slots)
        except (TypeError, ValueError):
            return None
        if slots <= 0:
            return None
        normalized["slots_override"] = slots
    return normalized


def normalize_segment_special_rings(value: object) -> dict[str, dict[str, Any]] | None:
    """Translate MCC's special-ring envelope to supported lane settings.

    The canonical envelope is intentionally instance-agnostic and contains
    ``priority`` and ``super_priority`` lane values.  MCD's effective config
    already has generic per-instance settings for those two lanes, so the
    translation keeps the immutable instance UID as the only map key and
    leaves no host or segment-specific branch in the consumer.
    """

    raw = _decode_json_value(value)
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("rings"), dict):
        raw = raw["rings"]
    if not any(key in raw for key in _SPECIAL_RING_TARGET_KEYS):
        return None
    normalized: dict[str, dict[str, Any]] = {}
    for lane, target_key in _SPECIAL_RING_TARGET_KEYS.items():
        lane_value = _normalize_special_ring_lane(raw.get(lane, []))
        if lane_value is None:
            return None
        normalized[target_key] = lane_value
    return normalized


def _remove_instance_aliases(mapping: dict[str, Any], uid: str) -> None:
    base_uid = str(uid).split("@", 1)[0]
    for current_uid in list(mapping):
        if str(current_uid).split("@", 1)[0] == base_uid:
            mapping.pop(current_uid, None)


def _set_instance_lane(merged: dict[str, Any], key: str, uid: str, value: object) -> None:
    current = merged.get(key)
    next_map = dict(current) if isinstance(current, dict) else {}
    _remove_instance_aliases(next_map, uid)
    next_map[str(uid)] = value
    merged[key] = next_map


def _special_ring_instance_entries(value: object) -> dict[str, object] | None:
    raw = _decode_json_value(value)
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("instances"), dict):
        return {str(uid): item for uid, item in raw["instances"].items()}
    if any(key in raw for key in _SPECIAL_RING_TARGET_KEYS):
        return {"": raw}
    return {str(uid): item for uid, item in raw.items()}


def normalize_runtime_override_payload(
    runtime: dict[str, Any],
    *,
    instance_uid: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Normalize supported compatibility envelopes before generic apply.

    Returns the translated payload and compatibility keys that could not be
    interpreted.  Callers can reject a revision containing those keys rather
    than acknowledging it as applied while silently dropping work.
    """

    normalized = dict(runtime) if isinstance(runtime, dict) else {}
    if _SPECIAL_RING_RUNTIME_KEY not in normalized:
        return normalized, []
    special = normalized.pop(_SPECIAL_RING_RUNTIME_KEY)
    if instance_uid:
        lanes = normalize_segment_special_rings(special)
        if lanes is None:
            normalized[_SPECIAL_RING_RUNTIME_KEY] = special
            return normalized, [_SPECIAL_RING_RUNTIME_KEY]
        for key, value in lanes.items():
            _set_instance_lane(normalized, key, str(instance_uid), value)
        return normalized, []

    direct = normalize_segment_special_rings(special)
    if direct is not None:
        normalized["segment_priority"] = list(direct["segment_priority_instance_settings"].get("segments", []))
        normalized["segment_super_priority"] = list(direct["segment_super_priority_instance_settings"].get("segments", []))
        return normalized, []

    entries = _special_ring_instance_entries(special)
    if entries is None:
        normalized[_SPECIAL_RING_RUNTIME_KEY] = special
        return normalized, [_SPECIAL_RING_RUNTIME_KEY]
    for uid, value in entries.items():
        clean_uid = str(uid or "").strip()
        lanes = normalize_segment_special_rings(value)
        if not clean_uid or lanes is None:
            normalized[_SPECIAL_RING_RUNTIME_KEY] = special
            return normalized, [_SPECIAL_RING_RUNTIME_KEY]
        for key, lane_value in lanes.items():
            _set_instance_lane(normalized, key, clean_uid, lane_value)
    return normalized, []


def instance_desired_states(runtime: dict[str, Any], installs: list[object]) -> dict[str, dict[str, Any]]:
    """Extract instance-scoped settings using immutable UIDs as canonical keys."""
    out: dict[str, dict[str, Any]] = {}
    for inst in installs:
        keys = _instance_keys(inst)
        if not keys:
            continue
        uid = keys[0]
        scoped: dict[str, Any] = {}
        for runtime_key in _INSTANCE_RUNTIME_KEYS:
            entries = runtime.get(runtime_key)
            if not isinstance(entries, dict):
                continue
            matched_key = _matching_instance_entry(entries, keys)
            if matched_key is not None:
                scoped[runtime_key] = entries[matched_key]
                if "@" in matched_key:
                    uid = matched_key
        if scoped:
            out[uid] = scoped
    return out


def merge_instance_desired_states(runtime: dict[str, Any], states: object) -> dict[str, Any]:
    """Overlay MCC instance state without relying on the local root path."""
    merged = dict(runtime)
    if not isinstance(states, dict):
        return merged
    for raw_uid, raw_state in states.items():
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(raw_state, dict):
            continue
        # MCC returns one authoritative desired-state row per known instance,
        # including rows with no runtime overrides. Clear that instance's
        # previous values first so an unset MCC override cannot live forever.
        for key in _INSTANCE_RUNTIME_KEYS:
            current = merged.get(key)
            if isinstance(current, dict):
                next_map = dict(current)
                uid_base = uid.split("@", 1)[0]
                for current_uid in current:
                    if str(current_uid).split("@", 1)[0] == uid_base:
                        next_map.pop(current_uid, None)
                if next_map:
                    merged[key] = next_map
                else:
                    merged.pop(key, None)
        overrides = raw_state.get("runtime_overrides", raw_state)
        if not isinstance(overrides, dict):
            continue
        # A canonical row is authoritative for this UID. Clear aliases and
        # stale lane values before applying its current envelope so a cleared
        # special ring cannot remain effective after the next poll.
        scoped_keys = set(_INSTANCE_RUNTIME_KEYS) | set(_SPECIAL_RING_TARGET_KEYS.values())
        for key in scoped_keys:
            current = merged.get(key)
            if not isinstance(current, dict):
                continue
            next_map = dict(current)
            _remove_instance_aliases(next_map, uid)
            if next_map:
                merged[key] = next_map
            else:
                merged.pop(key, None)
        for key, value in overrides.items():
            if key == _SPECIAL_RING_RUNTIME_KEY:
                lanes = normalize_segment_special_rings(value)
                if lanes is None:
                    # Preserve malformed data for generic rejection/logging;
                    # do not claim a revision whose lane payload was dropped.
                    invalid = merged.get(_SPECIAL_RING_RUNTIME_KEY)
                    invalid_map = dict(invalid) if isinstance(invalid, dict) else {}
                    invalid_map[uid] = value
                    merged[_SPECIAL_RING_RUNTIME_KEY] = invalid_map
                    continue
                for lane_key, lane_value in lanes.items():
                    _set_instance_lane(merged, lane_key, uid, lane_value)
                continue
            if key not in _INSTANCE_RUNTIME_KEYS:
                continue
            _set_instance_lane(merged, key, uid, value)
    return merged


def fetch_runtime_overrides(cfg: AgentConfig, *, instance_uids: list[str] | None = None) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    canonical_instance_uids = _canonical_instance_uids(instance_uids or [])
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
        "desired_state_protocol": 1,
        "instance_uids": canonical_instance_uids,
    }
    url = base + "/api/v1/agent/runtime-overrides"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        if not isinstance(out.get("runtime_overrides"), dict):
            out["runtime_overrides"] = {}
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}", "runtime_overrides": {}}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}", "runtime_overrides": {}}
    except Exception as e:
        return {"status": "error", "reason": str(e), "runtime_overrides": {}}


def push_runtime_overrides(
    cfg: AgentConfig,
    runtime_overrides: dict[str, Any],
    *,
    merge: bool = False,
    target: str = "observed",
    desired_state_revision: int | None = None,
    instance_desired_states: dict[str, dict[str, Any]] | None = None,
    instance_desired_state_revisions: dict[str, int] | None = None,
) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    target_mode = str(target or "observed").strip().lower()
    if target_mode not in {"observed", "desired"}:
        target_mode = "observed"
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
        "runtime_overrides": runtime_overrides if isinstance(runtime_overrides, dict) else {},
        "push_mode": "merge" if merge else "replace",
        "target": target_mode,
    }
    if target_mode == "desired":
        payload["desired_state_protocol"] = 1
        if isinstance(desired_state_revision, int):
            payload["desired_state_revision"] = desired_state_revision
        if isinstance(instance_desired_states, dict):
            payload["instance_desired_states"] = instance_desired_states
        if isinstance(instance_desired_state_revisions, dict):
            payload["instance_desired_state_revisions"] = instance_desired_state_revisions
    url = base + "/api/v1/agent/runtime-overrides"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def local_runtime_overrides(cfg: AgentConfig) -> dict[str, Any]:
    return runtime_overrides_from_config_file(cfg.config_file_path)


def overrides_fingerprint(overrides: dict[str, Any]) -> str:
    normalized = json.dumps(overrides, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def poll_trigger_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "runtime-overrides.poll"


def touch_poll_trigger(cfg: AgentConfig) -> str:
    p = poll_trigger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(time.time())), encoding="utf-8")
    return str(p)


def consume_poll_trigger(cfg: AgentConfig) -> bool:
    p = poll_trigger_path(cfg)
    if not p.exists():
        return False
    try:
        p.unlink()
    except Exception:
        return True
    return True


def apply_remote_overrides(base_cfg: AgentConfig, overrides: dict[str, Any]) -> dict[str, Any]:
    normalized, compatibility_unsupported = normalize_runtime_override_payload(overrides)
    cfg2, applied, unsupported, blocked = apply_runtime_overrides(
        base_cfg,
        normalized,
        allowed_keys=runtime_remote_allowed_keys(),
    )
    return {
        "config": cfg2,
        "applied_keys": applied,
        "unsupported_keys": sorted(set([*unsupported, *compatibility_unsupported])),
        "blocked_keys": blocked,
    }


def acknowledge_runtime_state(
    cfg: AgentConfig,
    *,
    scope: str,
    scope_key: str,
    revision: int,
    status: str,
    error: str = "",
    observed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
        "desired_state_protocol": 1,
        "desired_state_ack": {
            "scope": str(scope),
            "scope_key": str(scope_key),
            "revision": int(revision),
            "status": str(status),
            "error": str(error or ""),
            "observed": observed if isinstance(observed, dict) else {},
        },
    }
    try:
        out = _post_json(base + "/api/v1/agent/runtime-overrides", payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def acknowledge_desired_runtime_states(
    cfg: AgentConfig,
    store: Any,
    *,
    desired_state: object,
    host_runtime: dict[str, Any],
    instance_states: object,
    runtime_fingerprint: str,
) -> list[str]:
    """Retry each revision until MCC confirms the exact scoped value."""
    saved = store.get_runtime_sync("mcc_runtime_acknowledged")
    acknowledged = dict(saved) if isinstance(saved, dict) else {}
    failures: list[str] = []

    def send(scope: str, scope_key: str, revision: int, runtime: dict[str, Any]) -> None:
        normalized_runtime, compatibility_unsupported = normalize_runtime_override_payload(
            runtime,
            instance_uid=scope_key if scope == "instance" else None,
        )
        unknown_keys = sorted(set(normalized_runtime) - runtime_supported_keys())
        blocked_keys = sorted(set(normalized_runtime) - runtime_remote_allowed_keys())
        rejected_keys = sorted(set([*compatibility_unsupported, *unknown_keys, *blocked_keys]))
        if rejected_keys:
            result = acknowledge_runtime_state(
                cfg,
                scope=scope,
                scope_key=scope_key,
                revision=revision,
                status="rejected",
                error="unsupported runtime keys: " + ",".join(rejected_keys),
                observed={
                    "runtime_fingerprint": runtime_fingerprint,
                    "unsupported_keys": rejected_keys,
                },
            )
            failures.append(
                f"{scope}:{scope_key}:{revision}:rejected:{','.join(rejected_keys)}"
                if str(result.get("status", "")).lower() == "ok"
                else f"{scope}:{scope_key}:{revision}:{result.get('reason', 'rejected')}"
            )
            return

        content_sha256 = overrides_fingerprint(normalized_runtime)
        if scope == "host":
            previous = acknowledged.get("host")
        else:
            instances = acknowledged.get("instances")
            previous = instances.get(scope_key) if isinstance(instances, dict) else None
        if (
            isinstance(previous, dict)
            and previous.get("scope_key") == scope_key
            and previous.get("revision") == revision
            and previous.get("content_sha256") == content_sha256
        ):
            return

        result = acknowledge_runtime_state(
            cfg,
            scope=scope,
            scope_key=scope_key,
            revision=revision,
            status="applied",
            observed={
                "runtime_fingerprint": runtime_fingerprint,
                "content_sha256": content_sha256,
                "effective_runtime": normalized_runtime,
            },
        )
        if str(result.get("status", "")).lower() != "ok":
            failures.append(f"{scope}:{scope_key}:{revision}:{result.get('reason', 'unknown')}")
            return

        entry = {
            "scope_key": scope_key,
            "revision": revision,
            "content_sha256": content_sha256,
        }
        if scope == "host":
            acknowledged["host"] = entry
        else:
            instances = acknowledged.get("instances")
            updated = dict(instances) if isinstance(instances, dict) else {}
            updated[scope_key] = entry
            acknowledged["instances"] = updated
        store.put_runtime_sync("mcc_runtime_acknowledged", acknowledged)

    if isinstance(desired_state, dict):
        revision = desired_state.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            send(
                "host",
                str(desired_state.get("scope_key", "") or ""),
                revision,
                host_runtime if isinstance(host_runtime, dict) else {},
            )
    if isinstance(instance_states, dict):
        for uid, item in instance_states.items():
            if not isinstance(item, dict):
                continue
            revision = item.get("revision")
            runtime = item.get("runtime_overrides")
            if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(runtime, dict):
                continue
            send("instance", str(uid), revision, runtime)
    return failures


def canonical_profile_from_runtime_response(response: object) -> str | None:
    """Read the MCC-selected workload profile without binding to a host."""

    def value_from(raw: object) -> str | None:
        if isinstance(raw, str):
            candidate = raw.strip().lower()
            return candidate if candidate in _PROFILE_NAMES else None
        if not isinstance(raw, dict):
            return None
        for key in (
            "desired_profile",
            "selected_profile",
            "profile_name",
            "workload_profile",
            "profile",
        ):
            value = raw.get(key)
            if isinstance(value, dict):
                nested = value_from(value)
                if nested:
                    return nested
            elif isinstance(value, str):
                candidate = value.strip().lower()
                if candidate in _PROFILE_NAMES:
                    return candidate
        return None

    if not isinstance(response, dict):
        return None
    # Desired state is authoritative when both desired and observed snapshots
    # are present. Keep generic profile fields after explicit desired-state
    # envelopes because MCC may also include a stale observed profile at the
    # response top level.
    for key in ("desired_profile", "selected_profile", "desired_state", "desired", "profile_name"):
        found = value_from(response.get(key))
        if found:
            return found
    for key in ("observed_profile", "observed_state", "observed", "host_state"):
        found = value_from(response.get(key))
        if found:
            return found
    return value_from(response)


def desired_profile_from_runtime_response(response: object) -> str | None:
    """Return a profile explicitly supplied by MCC desired state, if any."""

    if not isinstance(response, dict):
        return None
    desired: dict[str, object] = {}
    for key in ("desired_profile", "selected_profile", "profile_name", "desired_state", "desired"):
        if key in response:
            desired[key] = response[key]
    return canonical_profile_from_runtime_response(desired)
