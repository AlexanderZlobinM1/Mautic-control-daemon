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
    _PROFILE_LOCKED_RUNTIME_KEYS,
    apply_runtime_overrides,
    runtime_overrides_from_config_file,
    runtime_effective_map,
    runtime_remote_allowed_keys,
    runtime_supported_keys,
)
from mcd_agent.host_identity import resolve_agent_identity


_RUNTIME_ACK_CHUNK_SCHEMA = "mcd-runtime-key-observation-chunk-v1"
_RUNTIME_ACK_MAX_BYTES = 256 * 1024
_RUNTIME_ACK_SAFE_BYTES = 240 * 1024
_RUNTIME_ACK_MAX_KEYS = 128
_RUNTIME_DIGEST_ENCODING = "canonical-json-sha256"


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
    "segment_lazy_update_instance_settings",
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
_RUNTIME_CAPABILITY_KEYS = frozenset({"segment_lazy_update_enabled"})


def _canonical_legacy_whitelist_value(value: object) -> object:
    if not isinstance(value, dict):
        return value
    legacy_keys = ("segment_whitelist", "whitelist", "priority", "ids")
    if any(key in value for key in legacy_keys):
        raw = next((value.get(key) for key in legacy_keys if key in value), [])
        if isinstance(raw, (list, tuple, set)):
            segments = [int(item) for item in raw if str(item).strip().lstrip("-").isdigit()]
        else:
            segments = []
        result: dict[str, Any] = {"segments": segments}
        if "slots_override" in value:
            result["slots_override"] = value["slots_override"]
        return result
    if set(value).issubset({"segments", "slots_override"}):
        return dict(value)
    return {str(uid): _canonical_legacy_whitelist_value(item) for uid, item in value.items()}
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


def _canonical_instance_uids(instance_uids: list[str], *, host_name: str = "") -> list[str]:
    """Return local UIDs plus one host-qualified compatibility alias.

    Inventory keeps the stable local UID, while older/current MCC desired
    rows may be addressed by the immutable ``<uid>@<mcc-host>`` key.  Send
    both forms without inventing a host or binding the code to a domain.
    """
    canonical: list[str] = []
    host = str(host_name or "").strip()
    for raw_uid in instance_uids:
        uid = str(raw_uid or "").strip()
        if not uid:
            continue
        canonical.append(uid)
        if host and "@" not in uid:
            canonical.append(f"{uid}@{host}")
    return list(dict.fromkeys(canonical))


def associate_instance_desired_states(
    states: dict[str, dict[str, Any]],
    known_uids: object,
) -> dict[str, dict[str, Any]]:
    """Associate local instance rows with unambiguous MCC UID aliases."""
    available = {
        str(uid or "").strip()
        for uid in (known_uids if isinstance(known_uids, (list, tuple, set, dict)) else [])
        if str(uid or "").strip()
    }
    if not available:
        return dict(states)
    out: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for raw_uid, value in states.items():
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(value, dict):
            continue
        target = uid if uid in available else ""
        if not target:
            base = uid.split("@", 1)[0]
            matches = [
                candidate
                for candidate in available
                if candidate not in used and candidate.split("@", 1)[0] == base
            ]
            if len(matches) == 1:
                target = matches[0]
        target = target or uid
        out[target] = value
        used.add(target)
    return out


def normalize_instance_desired_state_rows(
    states: object,
    requested_uids: object = None,
) -> dict[str, dict[str, Any]]:
    """Collapse legacy/base and immutable host-qualified rows per instance."""
    if not isinstance(states, dict):
        return {}
    requested = {
        str(uid or "").strip()
        for uid in (requested_uids if isinstance(requested_uids, (list, tuple, set, dict)) else [])
        if str(uid or "").strip()
    }
    grouped: dict[str, list[tuple[str, dict[str, Any], int]]] = {}
    for position, (raw_uid, raw_state) in enumerate(states.items()):
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(raw_state, dict):
            continue
        grouped.setdefault(uid.split("@", 1)[0], []).append((uid, raw_state, position))

    out: dict[str, dict[str, Any]] = {}
    for rows in grouped.values():
        def rank(row: tuple[str, dict[str, Any], int]) -> tuple[int, int, int, int, int]:
            uid, state, position = row
            revision = state.get("revision")
            revision_value = int(revision) if isinstance(revision, int) and not isinstance(revision, bool) else -1
            return (
                1 if "@" in uid and uid in requested else 0,
                1 if "@" in uid else 0,
                1 if uid in requested else 0,
                revision_value,
                -position,
            )

        uid, state, _position = max(rows, key=rank)
        out[uid] = state
    return out


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
        legacy_fallback = None
        legacy_map = merged.get("segment_whitelist_instance_settings")
        if isinstance(legacy_map, dict):
            for current_uid, current_value in legacy_map.items():
                if str(current_uid).split("@", 1)[0] == uid.split("@", 1)[0]:
                    legacy_fallback = current_value
                    break
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
            if (
                key == "segment_priority_instance_settings"
                and isinstance(value, dict)
                and value.get("segments") == []
                and legacy_fallback is not None
                and not bool(value.get("tombstone"))
            ):
                value = legacy_fallback
            _set_instance_lane(merged, key, uid, value)
    return merged


def fetch_runtime_overrides(cfg: AgentConfig, *, instance_uids: list[str] | None = None) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    effective_host_name = str(ident.get("effective_mcc_host_name") or ident.get("effective_hostname") or "").strip()
    canonical_instance_uids = _canonical_instance_uids(
        instance_uids or [],
        host_name=effective_host_name,
    )
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
        out["instance_desired_states"] = normalize_instance_desired_state_rows(
            out.get("instance_desired_states"),
            canonical_instance_uids,
        )
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
    runtime_change_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    target_mode = str(target or "observed").strip().lower()
    if target_mode not in {"observed", "desired"}:
        target_mode = "observed"
    runtime_payload = runtime_overrides if isinstance(runtime_overrides, dict) else {}
    instance_payload = instance_desired_states
    if target_mode == "desired":
        # Desired state is a bidirectional runtime contract.  Project the
        # host and instance envelopes through the same authority metadata
        # used by remote application so local-only/bootstrap keys and future
        # unknown keys never get persisted as MCC desired state.  Observed
        # pushes intentionally remain open for telemetry envelopes that are
        # not runtime settings.
        remote_keys = runtime_remote_allowed_keys()
        runtime_payload = {
            key: value for key, value in runtime_payload.items() if key in remote_keys
        }
        if isinstance(instance_desired_states, dict):
            instance_payload = {
                str(uid): {
                    key: value for key, value in values.items() if key in remote_keys
                }
                for uid, values in instance_desired_states.items()
                if isinstance(values, dict)
            }
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
        "runtime_overrides": runtime_payload,
        "push_mode": "merge" if merge else "replace",
        "target": target_mode,
    }
    if target_mode == "desired":
        payload["desired_state_protocol"] = 1
        if isinstance(desired_state_revision, int):
            payload["desired_state_revision"] = desired_state_revision
        if isinstance(instance_payload, dict):
            payload["instance_desired_states"] = instance_payload
        if isinstance(instance_desired_state_revisions, dict):
            payload["instance_desired_state_revisions"] = instance_desired_state_revisions
        if isinstance(runtime_change_metadata, dict):
            payload["runtime_change_metadata"] = runtime_change_metadata
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
    legacy = normalized.get("segment_whitelist_instance_settings")
    priority = normalized.get("segment_priority_instance_settings")
    if isinstance(legacy, dict):
        legacy = _canonical_legacy_whitelist_value(legacy)
        merged = dict(legacy)
        if isinstance(priority, dict):
            # Modern priority rows, including explicit empty tombstones, win
            # for the same UID; otherwise legacy IDs become canonical lanes.
            merged.update(priority)
        normalized["segment_priority_instance_settings"] = merged
        normalized.pop("segment_whitelist_instance_settings", None)
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


def runtime_key_observation(
    cfg: AgentConfig,
    runtime: dict[str, Any],
    *,
    revision: int,
    source: str = "mcd",
    scope_key: str | None = None,
) -> dict[str, Any]:
    """Return truthful per-key desired/effective runtime status for ACKs."""
    normalized, compatibility_unsupported = normalize_runtime_override_payload(runtime)
    effective = runtime_effective_map(cfg)
    profile = str(getattr(cfg, "profile_name", "") or "").strip().lower()
    observations: dict[str, dict[str, Any]] = {}
    for key, desired in normalized.items():
        if key == "segment_whitelist_instance_settings":
            desired = _canonical_legacy_whitelist_value(desired)
        if scope_key and key.endswith("_instance_settings") and isinstance(desired, dict):
            desired = _project_instance_observation_value(desired, scope_key)
        if key == "segment_whitelist_instance_settings":
            # Legacy whitelist is the wire alias of canonical priority; it is
            # not an independent scheduler lane.
            effective_value = effective.get("segment_whitelist_instance_settings")
            if not isinstance(effective_value, dict) or not effective_value:
                effective_value = effective.get("segment_priority_instance_settings")
            effective_value = _canonical_legacy_whitelist_value(effective_value)
        else:
            effective_value = False if key in _RUNTIME_CAPABILITY_KEYS else effective.get(key)
        if scope_key and key.endswith("_instance_settings") and isinstance(effective_value, dict):
            effective_value = _project_instance_observation_value(effective_value, scope_key)
        elif key.endswith("_instance_settings") and isinstance(desired, dict) and isinstance(effective_value, dict):
            # Host ACKs carry a sparse desired map. Compare only the exact
            # requested UIDs, including explicit empty tombstones; unrelated
            # default-filled instance rows are not part of this observation.
            effective_value = {
                str(uid): effective_value.get(uid, {})
                for uid in desired
            }
        if key in {
            "segment_whitelist_instance_settings",
            "segment_priority_instance_settings",
        }:
            # Both wire aliases describe the same priority lane.  ACK evidence
            # uses the canonical segments codec while retaining the original
            # desired value and its provenance/status.
            desired = _canonical_legacy_whitelist_value(desired)
            effective_value = _canonical_legacy_whitelist_value(effective_value)
        if key in _RUNTIME_CAPABILITY_KEYS:
            if effective_value == desired:
                status, origin, reason = "unchanged", "capability", "already_effective"
            else:
                status, origin, reason = "rejected", "capability", "effective_value_mismatch"
        elif key in compatibility_unsupported or key not in runtime_supported_keys():
            status, origin, reason = "rejected", "none", "unsupported_key"
        elif key not in runtime_remote_allowed_keys():
            status, origin, reason = "rejected", "none", "remote_blocked"
        elif key in _PROFILE_LOCKED_RUNTIME_KEYS and profile not in {"", "custom"}:
            status, origin, reason = "profile_locked", "profile", "profile_authority"
        elif effective_value == desired:
            status, origin, reason = "unchanged", source, "already_effective"
        else:
            status, origin, reason = "rejected", source, "effective_value_mismatch"
        observations[key] = {
            "status": status,
            "desired": _runtime_evidence_value(desired),
            "effective": _runtime_evidence_value(effective_value),
            "source": origin,
            "revision": int(revision),
            "reason": reason,
        }
    return {
        "schema": "mcd-runtime-key-observation-v1",
        "profile": profile or None,
        "keys": observations,
        # MCC validates this map against the exact observed keys. It is a
        # bounded per-requested-key readback, not the whole effective runtime.
        "effective_runtime": {key: row.get("effective") for key, row in observations.items()},
    }


def _runtime_evidence_value(value: object) -> object:
    """Keep small JSON values inline and digest oversized values canonically."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return value
    if len(encoded) <= _RUNTIME_ACK_SAFE_BYTES:
        return value
    return {
        "encoding": _RUNTIME_DIGEST_ENCODING,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }


def _project_instance_observation_value(value: object, scope_key: str) -> object:
    """Project a host map to one instance without losing a scoped row.

    MCC sends both shapes: a host-level sparse map keyed by UID and an
    instance-desired-state row whose value is already scoped. The latter can
    legitimately be a mapping such as ``{"enabled": true}``; treating every
    mapping as a UID map would turn that row into an empty ACK.
    """
    if not isinstance(value, dict):
        return value
    if scope_key in value:
        return value[scope_key]
    base_key = scope_key.split("@", 1)[0]
    if base_key in value:
        return value[base_key]
    if any(isinstance(item, dict) for item in value.values()):
        return {}
    return value


def _runtime_observation_chunks(
    observation: dict[str, Any],
    *,
    batch_id: str,
) -> list[dict[str, Any]]:
    """Partition a runtime observation deterministically under MCC limits."""
    keys = observation.get("keys")
    effective = observation.get("effective_runtime")
    if not isinstance(keys, dict) or not isinstance(effective, dict):
        return []
    ordered = sorted(str(key) for key in keys)
    if len(ordered) <= _RUNTIME_ACK_MAX_KEYS:
        encoded = json.dumps({"runtime": observation}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= _RUNTIME_ACK_SAFE_BYTES:
            return []

    parts: list[list[str]] = []
    current: list[str] = []
    for key in ordered:
        candidate = [*current, key]
        runtime = {
            "schema": observation.get("schema"),
            "profile": observation.get("profile"),
            "keys": {name: keys[name] for name in candidate},
            "effective_runtime": {name: effective.get(name) for name in candidate},
        }
        probe = {
            "runtime_chunk": {
                "schema": _RUNTIME_ACK_CHUNK_SCHEMA,
                "batch_id": batch_id,
                "part": 0,
                "total": 1,
                "runtime": runtime,
            }
        }
        encoded = json.dumps(probe, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if current and (len(candidate) > _RUNTIME_ACK_MAX_KEYS or len(encoded) > _RUNTIME_ACK_SAFE_BYTES):
            parts.append(current)
            current = [key]
        else:
            current = candidate
        single = json.dumps(
            {"runtime_chunk": {"schema": _RUNTIME_ACK_CHUNK_SCHEMA, "batch_id": batch_id,
                               "part": 0, "total": 1,
                               "runtime": {"schema": observation.get("schema"),
                                           "profile": observation.get("profile"),
                                           "keys": {key: keys[key]},
                                           "effective_runtime": {key: effective.get(key)}}}},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(single) > _RUNTIME_ACK_SAFE_BYTES:
            raise ValueError(f"runtime key {key} exceeds MCC ACK chunk limit")
    if current:
        parts.append(current)
    total = len(parts)
    return [
        {
            "schema": _RUNTIME_ACK_CHUNK_SCHEMA,
            "batch_id": batch_id,
            "part": index,
            "total": total,
            "runtime": {
                "schema": observation.get("schema"),
                "profile": observation.get("profile"),
                "keys": {key: keys[key] for key in part_keys},
                "effective_runtime": {key: effective.get(key) for key in part_keys},
            },
        }
        for index, part_keys in enumerate(parts)
    ]


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

    def send(
        scope: str,
        scope_key: str,
        revision: int,
        runtime: dict[str, Any],
        accepted_observation_revision: int | None = None,
    ) -> None:
        # Host ACKs are evidence for the exact canonical desired envelope,
        # never for an expanded effective/runtime snapshot.  MCC validates
        # chunk keys against the request that produced the revision; sending
        # additional supported local keys is therefore a protocol error.
        if scope == "host" and isinstance(desired_state, dict):
            canonical_runtime = desired_state.get("runtime_overrides")
            if isinstance(canonical_runtime, dict):
                runtime = canonical_runtime
        normalized_runtime, compatibility_unsupported = normalize_runtime_override_payload(
            runtime,
            instance_uid=scope_key if scope == "instance" else None,
        )
        unknown_keys = sorted(
            set(normalized_runtime) - runtime_supported_keys() - _RUNTIME_CAPABILITY_KEYS
        )
        blocked_keys = sorted(
            set(normalized_runtime) - runtime_remote_allowed_keys() - _RUNTIME_CAPABILITY_KEYS
        )
        rejected_keys = sorted(set([*compatibility_unsupported, *unknown_keys, *blocked_keys]))

        content_sha256 = overrides_fingerprint(normalized_runtime)
        key_observation = runtime_key_observation(
            cfg,
            normalized_runtime,
            revision=revision,
            source="mcc",
            scope_key=scope_key if scope == "instance" else None,
        )
        key_rows = key_observation.get("keys", {})
        statuses = {str(row.get("status")) for row in key_rows.values() if isinstance(row, dict)}
        all_effective = not statuses or statuses <= {"applied", "unchanged"}
        if not normalized_runtime:
            ack_status = "unverified"
        elif "profile_locked" in statuses and statuses <= {"profile_locked", "unchanged"}:
            ack_status = "profile_locked"
        elif rejected_keys or "rejected" in statuses:
            ack_status = "rejected"
        elif not all_effective:
            ack_status = "partial"
        else:
            ack_status = "applied"
        if scope == "host":
            previous = acknowledged.get("host")
        else:
            instances = acknowledged.get("instances")
            previous = instances.get(scope_key) if isinstance(instances, dict) else None
        batch_id = hashlib.sha256(
            f"{scope}|{scope_key}|{revision}|{content_sha256}".encode("utf-8")
        ).hexdigest()
        previous_parts: set[int] = set()
        previous_batch_id = ""
        previous_matches_scope = (
            isinstance(previous, dict)
            and previous.get("scope_key") == scope_key
            and previous.get("revision") == revision
        )
        previous_is_current_schema = (
            isinstance(previous, dict)
            and str(previous.get("agent_version", "")) == __version__
            and str(previous.get("ack_schema", "")) == "mcd-runtime-key-observation-v1"
        )
        if previous_matches_scope:
            previous_batch_id = str(previous.get("batch_id") or "").strip()
            raw_parts = previous.get("parts")
            if isinstance(raw_parts, list):
                previous_parts = {
                    int(part)
                    for part in raw_parts
                    if isinstance(part, int) and not isinstance(part, bool) and part >= 0
                }
        # MCC keeps an incomplete observation batch for a desired revision.
        # When the desired envelope is corrected without advancing that
        # revision, a new content-derived batch id is rejected as a second
        # pending batch. Continue the existing batch and send only its missing
        # parts; the next fresh revision still gets a new deterministic id.
        if (
            previous_matches_scope
            and previous_batch_id
            and previous_parts
            and accepted_observation_revision != revision
        ):
            batch_id = previous_batch_id
        try:
            chunks = _runtime_observation_chunks(key_observation, batch_id=batch_id) if normalized_runtime else []
        except ValueError as exc:
            failures.append(f"{scope}:{scope_key}:{revision}:{exc}")
            return
        if len(key_observation.get("keys", {})) > _RUNTIME_ACK_MAX_KEYS and not chunks:
            failures.append(
                f"{scope}:{scope_key}:{revision}:runtime ACK chunk selection produced no chunks"
            )
            return
        previous_matches = (
            isinstance(previous, dict)
            and previous.get("scope_key") == scope_key
            and previous.get("revision") == revision
            and previous.get("content_sha256") == content_sha256
        )
        previous_ack_status = str(previous.get("ack_status", "applied")).strip().lower() if isinstance(previous, dict) else ""
        if (
            previous_matches
            and previous_is_current_schema
            and previous_ack_status in {"applied", "unchanged"}
            and (not chunks or set(range(len(chunks))) <= previous_parts)
        ):
            return
        observed = {
            "runtime_fingerprint": runtime_fingerprint,
            "content_sha256": content_sha256,
        }
        if normalized_runtime and not chunks:
            observed["runtime"] = key_observation
        if chunks:
            outbound = [(index, {"runtime_chunk": chunk}) for index, chunk in enumerate(chunks)]
        else:
            outbound = [(None, observed)]
        completed_parts = set(previous_parts)
        for part, part_observed in outbound:
            if part is not None and part in completed_parts:
                continue
            result = acknowledge_runtime_state(
                cfg,
                scope=scope,
                scope_key=scope_key,
                revision=revision,
                status=ack_status,
                observed=part_observed,
            )
            if str(result.get("status", "")).lower() != "ok":
                failures.append(f"{scope}:{scope_key}:{revision}:{result.get('reason', 'unknown')}")
                return
            if part is not None:
                completed_parts.add(part)
                part_entry = {
                    "scope_key": scope_key,
                    "revision": revision,
                    "content_sha256": content_sha256,
                    "batch_id": batch_id,
                    "parts": sorted(completed_parts),
                    "total": len(chunks),
                    "complete": False,
                }
                if scope == "host":
                    acknowledged["host"] = part_entry
                else:
                    updated_parts = dict(acknowledged.get("instances") or {})
                    updated_parts[scope_key] = part_entry
                    acknowledged["instances"] = updated_parts
                store.put_runtime_sync("mcc_runtime_acknowledged", acknowledged)

        entry = {
            "scope_key": scope_key,
            "revision": revision,
            "content_sha256": content_sha256,
            "agent_version": __version__,
            "ack_schema": "mcd-runtime-key-observation-v1",
        }
        # Empty desired scopes are intentionally unverified and retain the
        # legacy successful receipt shape; non-empty rejected receipts must
        # remain retryable on the next poll.
        if normalized_runtime:
            entry["ack_status"] = ack_status
        if chunks:
            entry.update(
                {
                    "batch_id": batch_id,
                    "parts": sorted(completed_parts),
                    "total": len(chunks),
                    "complete": True,
                }
            )
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
            apply_state = desired_state.get("apply")
            accepted_revision = (
                apply_state.get("accepted_observation_revision")
                if isinstance(apply_state, dict)
                else None
            )
            if not isinstance(accepted_revision, int) or isinstance(accepted_revision, bool):
                accepted_revision = None
            send(
                "host",
                str(desired_state.get("scope_key", "") or ""),
                revision,
                host_runtime if isinstance(host_runtime, dict) else {},
                accepted_revision,
            )
    if isinstance(instance_states, dict):
        for uid, item in instance_states.items():
            if not isinstance(item, dict):
                continue
            revision = item.get("revision")
            runtime = item.get("runtime_overrides")
            if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(runtime, dict):
                continue
            apply_state = item.get("apply")
            accepted_revision = (
                apply_state.get("accepted_observation_revision")
                if isinstance(apply_state, dict)
                else None
            )
            if not isinstance(accepted_revision, int) or isinstance(accepted_revision, bool):
                accepted_revision = None
            send("instance", str(uid), revision, runtime, accepted_revision)
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
