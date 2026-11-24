from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

import hashlib
import fcntl
import json
import logging
import os
from pathlib import Path
import shlex
import re
import tempfile
import tarfile
import shutil
import subprocess
import struct
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import PurePosixPath

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.archive_utils import safe_extract_tar


_RUN_MODES = {"auto", "tmux", "screen", "direct"}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _repo_base_url(config: AgentConfig) -> str:
    if config.custom_repo_base_url:
        return config.custom_repo_base_url.rstrip("/")
    if config.plugins_repo_base_url:
        return config.plugins_repo_base_url.rstrip("/")
    if config.mcc_url:
        return config.mcc_url.rstrip("/")
    raise RuntimeError("custom.repo_base_url or mcc.url must be configured")


def _manifest_url(config: AgentConfig) -> str:
    base = _repo_base_url(config).rstrip("/") + "/"
    path = str(config.custom_manifest_path).strip()
    if not path:
        path = "mauticctl/custom/manifest.json"
    if path.startswith("/"):
        path = path[1:]
    return urllib.parse.urljoin(base, path)


def _headers(config: AgentConfig) -> dict[str, str]:
    out = {
        "Accept": "application/json",
        "User-Agent": f"mcd-agent/{__version__}",
    }
    if config.mcc_token:
        out["Authorization"] = f"Bearer {config.mcc_token}"
    return out


def _cache_root(config: AgentConfig) -> Path:
    root = Path(config.custom_cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_manifest_path(config: AgentConfig) -> Path:
    return _cache_root(config) / "manifest.json"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_cached_bytes(root: Path, relative: object) -> bytes:
    if not isinstance(relative, str):
        raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
    posix = PurePosixPath(relative)
    if (not relative or posix.is_absolute() or posix.as_posix() != relative
            or ".." in posix.parts or "." in posix.parts):
        raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
    path = root.joinpath(*posix.parts)
    try:
        if root.is_symlink():
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        cursor = root
        for part in posix.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        root_real = root.resolve(strict=True)
        path_real = path.resolve(strict=True)
        if root_real not in path_real.parents or not path_real.is_file() or path_real.stat().st_size > 256 * 1024:
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        return path_real.read_bytes()
    except CustomSelectedTaskError:
        raise
    except (OSError, RuntimeError):
        raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid") from None


def _active_operation_binding(
    document: dict[str, Any],
    *,
    root: Path,
    component: str,
    operation: str,
    matches: list[dict[str, Any]],
) -> dict[str, Any] | None:
    active = document.get("active_bindings")
    if active is None:
        return None
    if not isinstance(active, dict):
        raise CustomSelectedTaskError("custom_operation_profile_manifest_invalid")
    selected: list[dict[str, Any]] = []
    for binding_schema, selector in active.items():
        if (not isinstance(binding_schema, str)
                or not isinstance(selector, dict)
                or set(selector) != {
                    "catalog_item_id", "catalog_item_revision", "binding_sha256",
                    "cache_file", "resource_sha256",
                }
                or not isinstance(selector.get("catalog_item_id"), str)
                or not isinstance(selector.get("catalog_item_revision"), str)
                or not isinstance(selector.get("binding_sha256"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", selector["binding_sha256"]) is None
                or not isinstance(selector.get("resource_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", selector["resource_sha256"]) is None):
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        raw = _safe_cached_bytes(root, selector.get("cache_file"))
        if hashlib.sha256(raw).hexdigest() != selector["resource_sha256"]:
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        try:
            identity = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid") from None
        if (not isinstance(identity, dict)
                or identity.get("schema") != binding_schema
                or identity.get("catalog_item_id") != selector["catalog_item_id"]
                or identity.get("catalog_item_revision") != selector["catalog_item_revision"]
                or "sha256:" + hashlib.sha256(_canonical_json(identity)).hexdigest() != selector["binding_sha256"]):
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        identity_bindings = identity.get("operation_bindings")
        if not isinstance(identity_bindings, list) or any(not isinstance(row, dict) for row in identity_bindings):
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        active_matches = [
            row for row in identity_bindings
            if row.get("component") == component and row.get("operation") == operation
        ]
        for row in active_matches:
            manifest_matches = [candidate for candidate in matches if _canonical_json(candidate) == _canonical_json(row)]
            if len(manifest_matches) != 1:
                code = "custom_operation_profile_descriptor_ambiguous" if len(manifest_matches) > 1 else "custom_operation_profile_descriptor_invalid"
                raise CustomSelectedTaskError(code)
            selected.append(manifest_matches[0])
    if len(selected) > 1:
        raise CustomSelectedTaskError("custom_operation_profile_descriptor_ambiguous")
    return selected[0] if selected else None


def load_configured_profile_cache(
    config: AgentConfig,
    *,
    component: str,
    operation: str,
) -> dict[str, Any] | None:
    """Read an explicitly bound profile object from the existing custom cache.

    Portal/MCC may attach the exact operator profile to an existing operation
    binding.  The binding is the authority for the relative cache filename and
    digest; MCD does not choose a filename, fetch a profile, or infer defaults.
    ``None`` means that this optional cache descriptor was not installed.
    """
    root = Path(config.custom_cache_dir)
    manifest = root / "manifest.json"
    try:
        if manifest.is_symlink():
            raise CustomSelectedTaskError("custom_operation_profile_manifest_invalid")
        if not manifest.exists():
            return None
        if not manifest.is_file() or manifest.stat().st_size > 256 * 1024:
            raise CustomSelectedTaskError("custom_operation_profile_manifest_invalid")
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except CustomSelectedTaskError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise CustomSelectedTaskError("custom_operation_profile_manifest_invalid") from None
    if not isinstance(document, dict):
        raise CustomSelectedTaskError("custom_operation_profile_manifest_invalid")
    if "operation_bindings" not in document:
        return None
    bindings = document["operation_bindings"]
    if not isinstance(bindings, list):
        raise CustomSelectedTaskError("custom_operation_profile_manifest_invalid")
    matches = [item for item in bindings if isinstance(item, dict)
               and item.get("component") == component and item.get("operation") == operation]
    if not matches:
        return None
    selected = _active_operation_binding(
        document, root=root, component=component, operation=operation, matches=matches,
    )
    if selected is None and len(matches) != 1:
        raise CustomSelectedTaskError("custom_operation_profile_descriptor_ambiguous")
    source = (selected or matches[0]).get("profile_source")
    if source is None:
        return None
    if (not isinstance(source, dict)
            or source.get("type") != "cached-json"
            or not isinstance(source.get("file"), str)
            or not isinstance(source.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"].strip().lower()) is None):
        raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
    relative = source["file"]
    posix = PurePosixPath(relative)
    if (not relative or posix.is_absolute() or posix.as_posix() != relative
            or ".." in posix.parts or "." in posix.parts):
        raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
    profile_path = root.joinpath(*posix.parts)
    try:
        if root.is_symlink():
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        cursor = root
        for part in posix.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        root_real = root.resolve()
        profile_real = profile_path.resolve(strict=True)
        if root_real not in profile_real.parents:
            raise CustomSelectedTaskError("custom_operation_profile_descriptor_invalid")
        if not profile_real.is_file() or profile_real.stat().st_size > 256 * 1024:
            raise CustomSelectedTaskError("custom_operation_profile_invalid")
        raw = profile_real.read_bytes()
    except CustomSelectedTaskError:
        raise
    except (OSError, RuntimeError):
        raise CustomSelectedTaskError("custom_operation_profile_missing") from None
    expected = source["sha256"].strip().lower()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise CustomSelectedTaskError("custom_operation_profile_checksum_mismatch")
    try:
        profile = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CustomSelectedTaskError("custom_operation_profile_invalid") from None
    if not isinstance(profile, dict):
        raise CustomSelectedTaskError("custom_operation_profile_invalid")
    return {
        "profile": profile,
        "source": {
            "type": "cached-json",
            "file": relative,
            "sha256": expected,
        },
    }


def cached_custom_manifest_keys(config: AgentConfig) -> set[str]:
    cache_path = _cache_manifest_path(config)
    if not cache_path.exists():
        return set()
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()
    try:
        rows = _parse_manifest(payload)
    except Exception:
        return set()
    out: set[str] = set()
    for row in rows:
        key = str(row.get("key", "")).strip()
        if key:
            out.add(key)
    return out


def cleanup_custom_cache(config: AgentConfig, *, known_keys: set[str] | None = None) -> dict[str, int]:
    """
    Prune local custom scripts cache to keep bounded storage.

    Policy:
    - logs: age + max-files cap
    - downloads: age + max-entries cap; keys absent from manifest are removed after grace period
    """
    root = _cache_root(config)
    now = time.time()
    logs_keep_sec = max(1, int(config.custom_logs_keep_days)) * 86400
    logs_max_files = max(1, int(config.custom_logs_max_files))
    downloads_keep_sec = max(1, int(config.custom_downloads_keep_days)) * 86400
    downloads_max_entries = max(1, int(config.custom_downloads_max_entries))
    missing_key_grace_sec = 86400

    stats = {
        "logs_removed": 0,
        "downloads_removed": 0,
        "errors": 0,
    }

    def _safe_mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except Exception:
            return 0.0

    logs_dir = root / "logs"
    if logs_dir.exists():
        items = [p for p in logs_dir.iterdir() if p.is_file()]
        items.sort(key=_safe_mtime, reverse=True)
        for idx, path in enumerate(items):
            mtime = _safe_mtime(path)
            age = max(0.0, now - mtime)
            if age > logs_keep_sec or idx >= logs_max_files:
                try:
                    path.unlink()
                    stats["logs_removed"] += 1
                except Exception:
                    stats["errors"] += 1

    downloads_dir = root / "downloads"
    manifest_keys = set(known_keys or set()) or cached_custom_manifest_keys(config)
    # An operator-selected task has no TTL. Retain its existing key directory,
    # including the verified archive/activation, until selection changes explicitly.
    selected_keys: set[str] = set()
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        rows = _parse_manifest(manifest, deduplicate=False)
        selections = manifest.get("selected_tasks", {})
        if isinstance(selections, dict):
            selected_keys = {record["response"]["external_handler"]["artifact"]["key"]
                             for key, record in selections.items()
                             if _public_selected_task(record, key, rows)}
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        pass
    resource_ref = getattr(config, "universal_workload_resource_ref", None)
    if isinstance(resource_ref, dict) and resource_ref:
        try:
            from mcd_agent.universal_workload import selected_universal_resource
            selected_keys.add(selected_universal_resource(resource_ref)["key"])
        except (RuntimeError, TypeError, ValueError):
            pass
    if downloads_dir.exists():
        entries = [p for p in downloads_dir.iterdir() if p.is_dir() or p.is_file()]
        entries.sort(key=_safe_mtime, reverse=True)
        for idx, path in enumerate(entries):
            mtime = _safe_mtime(path)
            age = max(0.0, now - mtime)
            key = path.name
            if key in selected_keys:
                continue
            unknown_key_expired = bool(manifest_keys) and key not in manifest_keys and age > missing_key_grace_sec
            if age > downloads_keep_sec or idx >= downloads_max_entries or unknown_key_expired:
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    stats["downloads_removed"] += 1
                except Exception:
                    stats["errors"] += 1

    return stats


def _flatten_items(items: list[Any], parent: list[str], out: list[dict[str, Any]]) -> None:
    for raw in items:
        if not isinstance(raw, dict):
            continue
        t = str(raw.get("type", "")).strip().lower()
        if t == "group":
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            child = raw.get("items")
            if isinstance(child, list):
                _flatten_items(child, parent + [name], out)
            continue
        if t != "script":
            if "key" not in raw or "file" not in raw:
                continue
        key = str(raw.get("key", "")).strip()
        file_rel = str(raw.get("file", "")).strip()
        if not key or not file_rel:
            continue
        out.append(
            {
                "key": key,
                "name": str(raw.get("name", "")).strip() or key,
                "description": str(raw.get("description", "")).strip(),
                "file": file_rel,
                "entrypoint": raw.get("entrypoint"),
                "sha256": str(raw.get("sha256", "")).strip().lower(),
                "size_bytes": raw.get("size_bytes"),
                "run_mode": str(raw.get("run_mode", "auto")).strip().lower() or "auto",
                "interactive": _to_bool(raw.get("interactive", False)),
                "args_help": str(raw.get("args_help", "")).strip(),
                "path": " / ".join(parent),
            }
        )


def _parse_manifest(payload: dict[str, Any], *, deduplicate: bool = True) -> list[dict[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("custom manifest: items must be a list")
    out: list[dict[str, Any]] = []
    _flatten_items(items, [], out)
    if not deduplicate:
        return out
    dedup: dict[str, dict[str, Any]] = {}
    for row in out:
        key = str(row.get("key", "")).strip()
        if not key:
            continue
        dedup[key] = row
    rows = sorted(
        dedup.values(),
        key=lambda x: (
            str(x.get("path", "")).lower(),
            str(x.get("name", "")).lower(),
            str(x.get("key", "")).lower(),
        ),
    )
    return rows


def standalone_custom_cache_config(cache_dir: str | Path | None = None) -> Any:
    """Read only optional custom-cache settings; no registration/config recovery."""
    from types import SimpleNamespace
    from mcd_agent.config import DEFAULT_CUSTOM_CACHE_DIR, _load_toml_with_includes
    selected = str(cache_dir or os.getenv("MCD_CUSTOM_CACHE_DIR", "")).strip()
    if not selected:
        explicit = os.getenv("MCD_CONFIG", "").strip()
        candidates = [Path(explicit)] if explicit else [Path("/opt/mcd/etc/mcd.toml"), Path("/etc/mcd/mcd.toml")]
        selected = DEFAULT_CUSTOM_CACHE_DIR
        for path in candidates:
            if path.is_file():
                try:
                    custom = _load_toml_with_includes(str(path)).get("custom", {})
                    selected = custom.get("cache_dir", DEFAULT_CUSTOM_CACHE_DIR)
                    if not isinstance(selected, str) or not selected.strip():
                        raise ValueError()
                except (OSError, ValueError, TypeError, AttributeError):
                    raise CustomSelectedTaskError("custom_cache_settings_invalid") from None
                break
    return SimpleNamespace(custom_cache_dir=selected, custom_manifest_path="manifest.json",
        custom_repo_base_url="", plugins_repo_base_url="", mcc_url="", mcc_token=None,
        mcc_host_name=None, host_template=False, template_autopromote_on_clone=False,
        custom_run_mode_default="direct")


class CustomSelectedTaskError(RuntimeError):
    """A cached public task is missing, invalid or unavailable for this call."""

    @property
    def code(self) -> str:
        """A bounded public code; callers must not expose arbitrary error text."""
        value = self.args[0] if self.args and isinstance(self.args[0], str) else ""
        return value if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value) else "custom_selected_task_failed"


def _public_json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def selected_task_key(component: str, operation: str, operation_input: dict[str, Any], *,
                      profile_sha256: str, runtime_context_sha256: str) -> str:
    if (not isinstance(component, str) or not component or not isinstance(operation, str) or not operation
            or not isinstance(operation_input, dict)
            or any(not isinstance(pin, str) or re.fullmatch("[0-9a-f]{64}", pin) is None
                   for pin in (profile_sha256, runtime_context_sha256))):
        raise CustomSelectedTaskError("custom_selected_task_invalid")
    return ":".join((component, operation, _public_json_digest(operation_input),
                     profile_sha256, runtime_context_sha256))


def _selected_record_key(record: dict[str, Any]) -> str:
    response = record["response"]
    handler = response["external_handler"]
    pins = {name: handler[name]["sha256"] for name in ("profile_input", "runtime_context_input", "operation_input_input")}
    if (pins["profile_input"] != _public_json_digest(response["profile"])
            or pins["runtime_context_input"] != _public_json_digest(handler["runtime_context"])
            or pins["operation_input_input"] != _public_json_digest(record["operation_input"])):
        raise CustomSelectedTaskError("custom_selected_task_invalid")
    return selected_task_key(record["component"], record["operation"], record["operation_input"],
                             profile_sha256=pins["profile_input"], runtime_context_sha256=pins["runtime_context_input"])


def _public_selected_task(record: Any, key: str, rows: list[dict[str, Any]]) -> bool:
    if not isinstance(record, dict) or set(record) != {"schema", "component", "operation", "operation_input", "response"} or record.get("schema") != "mcd-custom-selected-task-v1":
        return False
    try:
        if _selected_record_key(record) != key:
            return False
    except (ValueError, TypeError, KeyError, CustomSelectedTaskError):
        return False
    def private(value: Any) -> bool:
        if isinstance(value, dict):
            return any(not isinstance(k, str) or re.search(r"runtime_input|password|credential|secret|private.?key|token", k, re.I) or private(v) for k, v in value.items())
        return isinstance(value, list) and any(private(v) for v in value)
    if private(record):
        return False
    response = record["response"]
    if not isinstance(response, dict) or response.get("status") != "ok" or any(_public_json_digest(response.get(name)) != _public_json_digest(record[name]) for name in ("component", "operation", "operation_input")):
        return False
    handler = response.get("external_handler")
    if not isinstance(handler, dict) or handler.get("active") is not True or handler.get("state") != "active":
        return False
    if (handler.get("schema") != "mcc-service-profile-external-handler-v1" or handler.get("transport") != "mcd-custom-v1"
            or any(_public_json_digest(handler.get(name)) != _public_json_digest(record[name])
                   for name in ("component", "operation", "operation_input"))
            or handler.get("resolved_plan_key") != response.get("resolved_plan_key")):
        return False
    fields = handler["profile_input"].get("fields")
    profile = response.get("profile")
    if (handler["profile_input"].get("source") != "response.profile" or not isinstance(fields, list)
            or any(not isinstance(name, str) or not name for name in fields) or len(set(fields)) != len(fields)
            or not (profile is None or isinstance(profile, dict))
            or (isinstance(profile, dict) and not set(profile) <= set(fields))):
        return False
    artifact = handler.get("artifact")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("sha256"), str) or re.fullmatch("[0-9a-f]{64}", artifact["sha256"]) is None:
        return False
    return any(all(row.get(k) == artifact.get(k) for k in ("key", "sha256", "entrypoint")) for row in rows)


_PROFILE_NOT_SUPPLIED = object()


def cached_selected_task(config: AgentConfig, *, component: str, operation: str,
                         operation_input: dict[str, Any], runtime_context: dict[str, Any] | None = None,
                         profile: Any = _PROFILE_NOT_SUPPLIED) -> dict[str, Any]:
    # Read without mkdir or any network/current-pointer/provenance gate.
    path = Path(config.custom_cache_dir) / "manifest.json"
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        raise CustomSelectedTaskError("custom_selected_task_missing") from None
    except (OSError, ValueError):
        raise CustomSelectedTaskError("custom_selected_task_invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("selected_tasks", {}), dict):
        raise CustomSelectedTaskError("custom_selected_task_invalid")
    found = []
    seen = False
    try:
        rows = _parse_manifest(payload, deduplicate=False)
        for key, record in payload.get("selected_tasks", {}).items():
            if not isinstance(record, dict) or any(record.get(k) != v for k, v in
                    (("component", component), ("operation", operation))):
                continue
            if _public_json_digest(record.get("operation_input")) != _public_json_digest(operation_input):
                continue
            seen = True
            if not _public_selected_task(record, key, rows):
                raise CustomSelectedTaskError("custom_selected_task_invalid")
            response = record["response"]
            handler = response["external_handler"]
            if runtime_context is not None and _public_json_digest(handler.get("runtime_context")) != _public_json_digest(runtime_context):
                continue
            if profile is not _PROFILE_NOT_SUPPLIED:
                fields = handler.get("profile_input", {}).get("fields")
                if not isinstance(fields, list) or any(not isinstance(k, str) for k in fields):
                    raise CustomSelectedTaskError("custom_selected_task_invalid")
                if profile is None:
                    projected = None
                elif isinstance(profile, dict):
                    projected = {key: profile[key] for key in fields if key in profile}
                else:
                    raise CustomSelectedTaskError("custom_selected_task_input_mismatch")
                if _public_json_digest(projected) != handler["profile_input"]["sha256"]:
                    continue
            found.append(response)
    except (ValueError, KeyError, TypeError):
        raise CustomSelectedTaskError("custom_selected_task_invalid") from None
    if not found:
        raise CustomSelectedTaskError("custom_selected_task_input_mismatch" if seen else "custom_selected_task_missing")
    if len(found) != 1:
        raise CustomSelectedTaskError("custom_selected_task_ambiguous")
    return found[0]


def _merge_cached_manifest(config: AgentConfig, payload: dict[str, Any], *,
                           selected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize updates on the existing cache directory, then replace atomically.

    Remote catalogs contribute items, never host-local selected-task authority.
    No separate registry or persistent lock file is created.
    """
    root = _cache_root(config)
    lock = os.open(root, os.O_RDONLY)
    temporary: Path | None = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "manifest.json"
        try:
            previous = json.loads(path.read_text())
        except (OSError, ValueError):
            previous = {}
        old = previous.get("selected_tasks", {}) if isinstance(previous, dict) else {}
        if selected is not None:
            payload = previous
            if not isinstance(payload, dict):
                raise CustomSelectedTaskError("custom_selected_task_invalid")
        rows = _parse_manifest(payload, deduplicate=False)
        # Remote latest is not revocation of an already selected local task.
        # Retain its exact earlier item coordinates, including older revisions
        # with a reused script key. Byte integrity is still checked at launch;
        # missing/corrupt bytes stay typed and retain their pins for repair.
        if selected is None and isinstance(old, dict):
            try:
                earlier_rows = _parse_manifest(previous, deduplicate=False)
            except (ValueError, RuntimeError):
                earlier_rows = []
            extras = []
            for key, record in old.items():
                if not _public_selected_task(record, key, earlier_rows):
                    continue
                reference = record["response"]["external_handler"]["artifact"]
                matches = lambda row: all(row.get(k) == reference.get(k) for k in ("key", "sha256", "entrypoint"))
                if not any(matches(row) for row in rows):
                    saved = next(row for row in earlier_rows if matches(row))
                    if saved not in extras:
                        extras.append(saved)
            if extras:
                payload = {**payload, "items": [*extras, *payload["items"]]}
                rows = _parse_manifest(payload, deduplicate=False)
        kept = {key: record for key, record in old.items() if _public_selected_task(record, key, rows)} if isinstance(old, dict) else {}
        if selected is not None:
            key = _selected_record_key(selected)
            if not _public_selected_task(selected, key, rows):
                raise CustomSelectedTaskError("custom_selected_task_invalid")
            kept[key] = selected
        merged = {k: v for k, v in payload.items() if k != "selected_tasks"}
        if kept:
            merged["selected_tasks"] = kept
        with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".manifest-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(merged, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
        return merged
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(lock)


def cache_selected_task(config: AgentConfig, response: dict[str, Any]) -> None:
    # Caller first admits the authenticated public descriptor and exact cache.
    record = {"schema": "mcd-custom-selected-task-v1", "component": response.get("component"),
              "operation": response.get("operation"), "operation_input": response.get("operation_input"),
              "response": response}
    try:
        payload = json.loads((Path(config.custom_cache_dir) / "manifest.json").read_text())
        key = _selected_record_key(record)
        valid = _public_selected_task(record, key, _parse_manifest(payload, deduplicate=False))
    except (OSError, ValueError, TypeError, RuntimeError):
        valid = False
    if not valid:
        raise CustomSelectedTaskError("custom_selected_task_invalid")
    _merge_cached_manifest(config, {}, selected=record)


def _hydrate_manifest_cached_json_resources(config: AgentConfig, payload: dict[str, Any]) -> None:
    """Cache immutable JSON resources referenced by operation bindings.

    The public/custom manifest is authoritative for the relative file and its
    SHA-256.  MCD does not interpret the profile contents; it only makes the
    declared resource available to the generic offline operation dispatcher.
    A manifest is never activated with a missing, corrupt, escaping or
    ambiguous cached resource.
    """

    bindings = payload.get("operation_bindings", [])
    if bindings is None:
        return
    if not isinstance(bindings, list) or any(not isinstance(row, dict) for row in bindings):
        raise RuntimeError("custom manifest operation bindings are invalid")
    declared: dict[str, str] = {}
    for binding in bindings:
        for source in binding.values():
            if not isinstance(source, dict) or source.get("type") != "cached-json":
                continue
            relative = source.get("file")
            digest = source.get("sha256")
            if (
                not isinstance(relative, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest.strip().lower()) is None
            ):
                raise RuntimeError("custom manifest cached resource descriptor is invalid")
            posix = PurePosixPath(relative)
            if (
                not relative
                or posix.is_absolute()
                or posix.as_posix() != relative
                or any(part in {"", ".", ".."} for part in posix.parts)
            ):
                raise RuntimeError("custom manifest cached resource descriptor is invalid")
            normalized_digest = digest.strip().lower()
            prior = declared.get(relative)
            if prior is not None and prior != normalized_digest:
                raise RuntimeError("custom manifest cached resource descriptor is ambiguous")
            declared[relative] = normalized_digest
    if not declared:
        return

    root = _cache_root(config)
    if root.exists() and root.is_symlink():
        raise RuntimeError("custom cache root is invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_real = root.resolve(strict=True)
    base = _manifest_url(config).rsplit("/", 1)[0] + "/"
    for relative, expected in sorted(declared.items()):
        posix = PurePosixPath(relative)
        parent = root
        for part in posix.parts[:-1]:
            parent = parent / part
            if parent.exists() and parent.is_symlink():
                raise RuntimeError("custom manifest cached resource path is invalid")
            parent.mkdir(mode=0o700, exist_ok=True)
        if root_real != parent.resolve(strict=True) and root_real not in parent.resolve(strict=True).parents:
            raise RuntimeError("custom manifest cached resource path is invalid")
        target = parent / posix.name
        if target.is_symlink():
            raise RuntimeError("custom manifest cached resource path is invalid")
        try:
            if (
                target.is_file()
                and target.stat().st_size <= 256 * 1024
                and hashlib.sha256(target.read_bytes()).hexdigest() == expected
            ):
                continue
        except OSError:
            pass

        url = urllib.parse.urljoin(base, relative)
        request = mcc_request(url, headers=_headers(config))
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                body = response.read(256 * 1024 + 1)
        except Exception as exc:
            raise RuntimeError("custom manifest cached resource fetch failed") from exc
        if len(body) > 256 * 1024 or hashlib.sha256(body).hexdigest() != expected:
            raise RuntimeError("custom manifest cached resource integrity mismatch")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=parent, prefix=".resource-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def fetch_custom_manifest(config: AgentConfig, *, use_cache_on_error: bool = True) -> tuple[list[dict[str, Any]], str]:
    cache_path = _cache_manifest_path(config)
    try:
        url = _manifest_url(config)
        req = mcc_request(url, headers=_headers(config))
        with urllib.request.urlopen(req, timeout=12) as resp:
            text = (resp.read() or b"").decode("utf-8", errors="replace")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise RuntimeError("custom manifest is not a JSON object")
        rows = _parse_manifest(payload)
        _hydrate_manifest_cached_json_resources(config, payload)
        merged = _merge_cached_manifest(config, payload)
        return _parse_manifest(merged), "remote"
    except urllib.error.HTTPError as e:
        # Treat missing manifest as an empty custom scripts catalog.
        # This keeps interactive UX stable on fresh MCC installs before first custom publish.
        if int(getattr(e, "code", 0) or 0) == 404:
            empty = {"manifest_version": 1, "items": []}
            merged = _merge_cached_manifest(config, empty)
            return _parse_manifest(merged), "remote-empty"
        if use_cache_on_error and cache_path.exists():
            text = cache_path.read_text(encoding="utf-8", errors="replace")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise RuntimeError(f"custom manifest unavailable: {e}") from e
            rows = _parse_manifest(payload)
            return rows, "cache"
        raise RuntimeError(f"custom manifest fetch failed: {e}") from e
    except Exception as e:
        if use_cache_on_error and cache_path.exists():
            text = cache_path.read_text(encoding="utf-8", errors="replace")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise RuntimeError(f"custom manifest unavailable: {e}") from e
            rows = _parse_manifest(payload)
            return rows, "cache"
        raise RuntimeError(f"custom manifest fetch failed: {e}") from e


def _activate_bundle(archive_path: Path, cache_dir: Path, digest: str, entrypoint: str) -> Path:
    # Isolate concurrent invocations while retaining the whole bundle's identity.
    revision_dir = cache_dir / digest
    revision_dir.mkdir(mode=0o700, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".extract-", dir=revision_dir))
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            safe_extract_tar(archive, staged)
        selected = staged / entrypoint
        if not selected.is_file() or selected.is_symlink():
            raise RuntimeError("custom bundle entrypoint is missing or not a regular file")
        selected.chmod(selected.stat().st_mode | 0o500)
        activated = revision_dir / ("run-" + staged.name.removeprefix(".extract-"))
        os.replace(staged, activated)
        return activated / entrypoint
    finally:
        if staged.exists():
            shutil.rmtree(staged)


_VERSIONED_COMPONENT_KEY = re.compile(
    r"^(?P<identity>[A-Za-z0-9][A-Za-z0-9_.-]*-component)-(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.-]+)?)$"
)
_COMPONENT_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-component$")
_COMPONENT_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_COMPONENT_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPONENT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_COMPONENT_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_COMPONENT_STATUS_SCHEMA = "mcd-external-component-preflight-v1"
_COMPONENT_SELECTION_SCHEMA = "mcd-external-component-selection-v1"


class ExternalComponentCatalogUnavailable(RuntimeError):
    """The authoritative catalog could not be reached."""


class ExternalComponentCatalogInvalid(RuntimeError):
    """The authoritative catalog answered but violated its public contract."""


def _component_coordinates(key: str) -> tuple[str, str] | None:
    match = _VERSIONED_COMPONENT_KEY.fullmatch(str(key or "").strip())
    return (match.group("identity"), match.group("version")) if match else None


def _component_catalog_url(config: AgentConfig, identity: str) -> str:
    normalized = str(identity or "").strip()
    if _COMPONENT_IDENTITY_RE.fullmatch(normalized) is None:
        raise ExternalComponentCatalogInvalid("external_component_identity_invalid")
    base = str(getattr(config, "mcc_url", "") or "").strip().rstrip("/")
    if not base:
        raise ExternalComponentCatalogUnavailable("external_component_catalog_unconfigured")
    return f"{base}/api/v1/agent/custom-components/{urllib.parse.quote(normalized, safe='')}/catalog"


def _safe_component_relative_path(value: object, *, suffix: str = "") -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
    text = value.strip()
    path = PurePosixPath(text)
    if path.is_absolute() or path.as_posix() != text or any(part in {"", ".", ".."} for part in path.parts):
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
    if suffix and not text.endswith(suffix):
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
    return text


def _validate_external_component_record(identity: str, raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ExternalComponentCatalogInvalid("external_component_not_selected")
    version = str(raw.get("version") or "").strip()
    key = str(raw.get("key") or "").strip()
    digest = str(raw.get("sha256") or "").strip().lower()
    source_revision = str(raw.get("source_revision") or "").strip().lower()
    if (
        _COMPONENT_VERSION_RE.fullmatch(version) is None
        or key != f"{identity}-{version}"
        or _COMPONENT_SHA_RE.fullmatch(digest) is None
        or _COMPONENT_REVISION_RE.fullmatch(source_revision) is None
        or raw.get("identity") not in {None, identity}
    ):
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
    size = raw.get("size_bytes")
    if type(size) is not int or size < 0:
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
    artifact_file = _safe_component_relative_path(raw.get("artifact_file"), suffix=".tar.gz")
    entrypoint = _safe_component_relative_path(raw.get("entrypoint"))
    target_root = str(raw.get("target_root") or "").strip()
    if not target_root or not Path(target_root).is_absolute():
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
    return {
        "identity": identity,
        "version": version,
        "key": key,
        "sha256": digest,
        "size_bytes": size,
        "source_revision": source_revision,
        "artifact_file": artifact_file,
        "entrypoint": entrypoint,
        "target_root": target_root,
    }


def _component_operation_descriptors(raw: object) -> list[dict[str, Any]]:
    """Preserve MCC/component-owned operation metadata without interpreting it."""

    if not isinstance(raw, list) or not raw:
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
    descriptors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            descriptor: dict[str, Any] = {"id": item.strip()}
        elif isinstance(item, dict):
            try:
                descriptor = json.loads(
                    json.dumps(
                        item,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ExternalComponentCatalogInvalid("external_component_catalog_invalid") from exc
            operation = descriptor.get("operation", descriptor.get("id", descriptor.get("name")))
            operation = str(operation or "").strip()
        else:
            raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
        operation = str(
            descriptor.get("operation", descriptor.get("id", descriptor.get("name"))) or ""
        ).strip()
        if _COMPONENT_OPERATION_RE.fullmatch(operation) is None or operation in seen:
            raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
        if len(json.dumps(descriptor, ensure_ascii=True, separators=(",", ":"))) > 64 * 1024:
            raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
        descriptors.append(descriptor)
        seen.add(operation)
    return descriptors


def _component_operation_names(descriptors: object) -> tuple[str, ...]:
    return tuple(
        str(item.get("operation", item.get("id", item.get("name"))) or "").strip()
        for item in descriptors
        if isinstance(item, dict)
        and str(item.get("operation", item.get("id", item.get("name"))) or "").strip()
    ) if isinstance(descriptors, list) else ()


def _component_descriptor_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in ("descriptor_schema", "descriptor_version", "compatibility", "capability_projection"):
        if field not in payload:
            continue
        value = payload[field]
        if field in {"descriptor_schema", "descriptor_version"}:
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
            metadata[field] = value.strip()
            continue
        if not isinstance(value, dict):
            raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
        try:
            copied = json.loads(json.dumps(
                value, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ExternalComponentCatalogInvalid("external_component_catalog_invalid") from exc
        if len(json.dumps(copied, ensure_ascii=True, separators=(",", ":"))) > 64 * 1024:
            raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
        metadata[field] = copied
    return metadata


def fetch_external_component_catalog(config: AgentConfig, identity: str) -> dict[str, Any]:
    """Fetch one authenticated component selection without using the public manifest."""

    url = _component_catalog_url(config, identity)
    try:
        req = mcc_request(url, headers=_headers(config))
        with urllib.request.urlopen(req, timeout=12) as response:
            payload = json.loads((response.read() or b"").decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ExternalComponentCatalogUnavailable("external_component_catalog_unavailable") from exc
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "mcc-custom-component-catalog-v1"
        or payload.get("contract_version") != "1.0"
        or payload.get("identity") != identity
    ):
        raise ExternalComponentCatalogInvalid("external_component_catalog_invalid")
    operations = _component_operation_descriptors(payload.get("operations"))
    selected = payload.get("selected")
    return {
        "schema": payload["schema"],
        "contract_version": "1.0",
        "identity": identity,
        "operations": operations,
        "descriptor": _component_descriptor_metadata(payload),
        "selected": None if selected is None else _validate_external_component_record(identity, selected),
    }


def _component_entrypoint(component_root: Path, entrypoint: str) -> Path | None:
    current = component_root / "current"
    try:
        target = current.resolve(strict=True)
        revisions = (component_root / "revisions").resolve(strict=True)
        selected = target / entrypoint
        if revisions not in target.parents or not selected.is_file() or selected.is_symlink():
            return None
        return selected
    except (OSError, RuntimeError):
        return None


def _component_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise RuntimeError("custom component tree is invalid")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(b"d\0" if path.is_dir() else b"f\0")
        digest.update(relative)
        digest.update(b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _atomic_component_json(root: Path, name: str, payload: dict[str, Any]) -> None:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=f".{name}-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, root / name)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _installed_external_component(config: AgentConfig, identity: str) -> dict[str, Any] | None:
    """Return a verified active component, never a candidate or rollback tree."""

    if _COMPONENT_IDENTITY_RE.fullmatch(str(identity or "").strip()) is None:
        return None
    cache_dir = str(getattr(config, "custom_cache_dir", "") or "").strip()
    if not cache_dir:
        return None
    root = Path(cache_dir) / "components" / identity
    metadata_path = root / "metadata.json"
    current = root / "current"
    revisions = root / "revisions"
    try:
        if root.is_symlink() or revisions.is_symlink() or not revisions.is_dir():
            return None
        if metadata_path.is_symlink() or not metadata_path.is_file() or metadata_path.stat().st_size > 64 * 1024:
            return None
        if not current.is_symlink():
            return None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("schema") != "mcd-external-component-state-v1":
            return None
        version = str(metadata.get("version") or "").strip()
        key = str(metadata.get("key") or "").strip()
        digest = str(metadata.get("sha256") or "").strip().lower()
        entrypoint = _safe_component_relative_path(metadata.get("entrypoint"))
        size = metadata.get("size_bytes")
        if (
            metadata.get("identity") != identity
            or _COMPONENT_VERSION_RE.fullmatch(version) is None
            or key != f"{identity}-{version}"
            or _COMPONENT_SHA_RE.fullmatch(digest) is None
            or type(size) is not int
            or size < 0
        ):
            return None
        revision = current.resolve(strict=True)
        revisions_real = revisions.resolve(strict=True)
        if revision.parent != revisions_real or revision.name != digest or revision.is_symlink() or not revision.is_dir():
            return None
        selected = _component_entrypoint(root, entrypoint)
        if selected is None or not (selected.stat().st_mode & 0o111):
            return None
        entrypoint_sha256 = metadata.get("entrypoint_sha256")
        if entrypoint_sha256 is not None:
            if (
                not isinstance(entrypoint_sha256, str)
                or _COMPONENT_SHA_RE.fullmatch(entrypoint_sha256) is None
                or hashlib.sha256(selected.read_bytes()).hexdigest() != entrypoint_sha256
            ):
                return None
        tree_sha256 = metadata.get("tree_sha256")
        if tree_sha256 is not None:
            if (
                not isinstance(tree_sha256, str)
                or _COMPONENT_SHA_RE.fullmatch(tree_sha256) is None
                or _component_tree_sha256(revision) != tree_sha256
            ):
                return None
        return {
            "identity": identity,
            "version": version,
            "key": key,
            "sha256": digest,
            "size_bytes": size,
            "entrypoint": entrypoint,
            "source_revision": str(metadata.get("source_revision") or ""),
            "entrypoint_sha256": str(entrypoint_sha256 or ""),
            "tree_sha256": str(tree_sha256 or ""),
            "path": selected,
        }
    except (ExternalComponentCatalogInvalid, OSError, RuntimeError, UnicodeError, ValueError, TypeError):
        return None


def _write_component_selection(
    config: AgentConfig,
    identity: str,
    *,
    status: str,
    selected: dict[str, Any] | None,
    operations: list[dict[str, Any]] | None = None,
    descriptor: dict[str, Any] | None = None,
    reason: str = "",
) -> None:
    cache_dir = str(getattr(config, "custom_cache_dir", "") or "").strip()
    if not cache_dir:
        raise OSError("custom cache is not configured")
    root = Path(cache_dir) / "components" / identity
    payload = {
        "schema": _COMPONENT_SELECTION_SCHEMA,
        "identity": identity,
        "status": status,
        "reason": str(reason or "")[:128],
        "selected": dict(selected) if isinstance(selected, dict) else None,
        "operations": list(operations or []),
        "descriptor": dict(descriptor or {}),
        "updated_at": int(time.time()),
    }
    _atomic_component_json(root, "selection.json", payload)


def _read_component_selection(config: AgentConfig, identity: str) -> dict[str, Any] | None:
    cache_dir = str(getattr(config, "custom_cache_dir", "") or "").strip()
    if not cache_dir:
        return None
    path = Path(cache_dir) / "components" / identity / "selection.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != _COMPONENT_SELECTION_SCHEMA
            or payload.get("identity") != identity
            or payload.get("status") not in {"selected", "ready", "blocked"}
        ):
            return None
        selected = payload.get("selected")
        if selected is not None:
            selected = _validate_external_component_record(identity, selected)
        operations = payload.get("operations", [])
        if operations:
            operations = _component_operation_descriptors(operations)
        elif not isinstance(operations, list):
            return None
        descriptor = payload.get("descriptor", {})
        if not isinstance(descriptor, dict):
            return None
        descriptor = _component_descriptor_metadata(descriptor)
        return {
            **payload,
            "selected": selected,
            "operations": operations,
            "descriptor": descriptor,
        }
    except (ExternalComponentCatalogInvalid, OSError, UnicodeError, ValueError, TypeError):
        return None


def _component_record_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        left.get(field) == right.get(field)
        for field in (
            "identity",
            "version",
            "key",
            "sha256",
            "size_bytes",
            "entrypoint",
            "source_revision",
        )
    )


def _activate_versioned_component(
    config: AgentConfig,
    *,
    archive_path: Path,
    key: str,
    digest: str,
    size_bytes: int,
    entrypoint: str,
    source_revision: str = "",
) -> Path:
    coordinates = _component_coordinates(key)
    if coordinates is None:
        raise RuntimeError("custom component identity is invalid")
    identity, version = coordinates
    root = Path(config.custom_cache_dir) / "components" / identity
    revisions = root / "revisions"
    revisions.mkdir(parents=True, mode=0o700, exist_ok=True)
    revision = revisions / digest
    staged: Path | None = None
    if not revision.exists():
        staged = Path(tempfile.mkdtemp(prefix=".stage-", dir=revisions))
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                safe_extract_tar(archive, staged)
            selected = staged / entrypoint
            if not selected.is_file() or selected.is_symlink():
                raise RuntimeError("custom bundle entrypoint is missing or not a regular file")
            selected.chmod(selected.stat().st_mode | 0o500)
            os.replace(staged, revision)
            staged = None
        finally:
            if staged is not None and staged.exists():
                shutil.rmtree(staged)
    elif _COMPONENT_REVISION_RE.fullmatch(source_revision):
        verification = Path(tempfile.mkdtemp(prefix=".verify-", dir=revisions))
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                safe_extract_tar(archive, verification)
            selected = verification / entrypoint
            if not selected.is_file() or selected.is_symlink():
                raise RuntimeError("custom bundle entrypoint is missing or not a regular file")
            if _component_tree_sha256(verification) != _component_tree_sha256(revision):
                raise RuntimeError("custom component active revision does not match selected archive")
        finally:
            shutil.rmtree(verification, ignore_errors=True)

    lock_fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        previous_digest = ""
        current = root / "current"
        try:
            previous = current.resolve(strict=True)
            if previous.parent == revisions.resolve(strict=True):
                previous_digest = previous.name
        except (OSError, RuntimeError):
            pass
        temporary_link = root / (".current-" + uuid.uuid4().hex)
        temporary_link.symlink_to(Path("revisions") / digest)
        os.replace(temporary_link, current)
        rollback = root / "rollback"
        if previous_digest and previous_digest != digest:
            temporary_rollback = root / (".rollback-" + uuid.uuid4().hex)
            temporary_rollback.symlink_to(Path("revisions") / previous_digest)
            os.replace(temporary_rollback, rollback)
        elif rollback.exists() or rollback.is_symlink():
            rollback.unlink(missing_ok=True)
        metadata = {
            "schema": "mcd-external-component-state-v1",
            "identity": identity,
            "key": key,
            "version": version,
            "sha256": digest,
            "size_bytes": int(size_bytes),
            "entrypoint": entrypoint,
            "entrypoint_sha256": hashlib.sha256((revision / entrypoint).read_bytes()).hexdigest(),
            "tree_sha256": _component_tree_sha256(revision),
            "source_revision": source_revision if _COMPONENT_REVISION_RE.fullmatch(source_revision) else "",
            "rollback_sha256": previous_digest if previous_digest and previous_digest != digest else None,
        }
        with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".metadata-", delete=False) as stream:
            metadata_tmp = Path(stream.name)
            json.dump(metadata, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(metadata_tmp, root / "metadata.json")
        keep = {digest}
        if metadata["rollback_sha256"]:
            keep.add(str(metadata["rollback_sha256"]))
        for child in revisions.iterdir():
            if child.name not in keep:
                shutil.rmtree(child, ignore_errors=True)
    finally:
        os.close(lock_fd)
    selected = _component_entrypoint(root, entrypoint)
    if selected is None:
        raise RuntimeError("custom component activation failed")
    return selected


def _fetch_script_to_cache(config: AgentConfig, script: dict[str, Any], *, offline_only: bool = False,
                           data_only: bool = False, preseed_bytes: bytes | None = None) -> Path:
    file_rel = str(script.get("file", "")).strip()
    if not file_rel:
        raise RuntimeError("custom script entry has empty file field")
    if file_rel.startswith("/") or ".." in Path(file_rel).parts:
        raise RuntimeError("custom script file path is invalid")

    key = str(script.get("key", "")).strip() or "script"
    expected_sha = str(script.get("sha256", "")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
        raise RuntimeError("custom artifact requires an expected SHA-256")
    if preseed_bytes is not None and (not data_only or hashlib.sha256(preseed_bytes).hexdigest() != expected_sha):
        raise RuntimeError("data-only cache preseed checksum mismatch")

    entrypoint = script.get("entrypoint")
    if data_only and entrypoint is not None:
        raise RuntimeError("data-only resource cannot have an executable entrypoint")
    if entrypoint is not None:
        if (not isinstance(entrypoint, str) or not entrypoint.strip()
                or "\x00" in entrypoint or Path(entrypoint).is_absolute()
                or ".." in Path(entrypoint).parts):
            raise RuntimeError("custom bundle entrypoint path is invalid")
        if not file_rel.endswith(".tar.gz") or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
            raise RuntimeError("custom bundle requires tar.gz and a manifest SHA-256")

    component = _component_coordinates(key) if entrypoint is not None else None
    expected_size = script.get("size_bytes")
    component_root = Path(config.custom_cache_dir) / "components" / component[0] if component else None
    if component_root is not None:
        installed = _installed_external_component(config, component[0])
        if (
            installed is not None
            and installed.get("key") == key
            and installed.get("sha256") == expected_sha
            and installed.get("entrypoint") == str(entrypoint)
            and (
                _COMPONENT_REVISION_RE.fullmatch(str(script.get("source_revision") or "")) is None
                or installed.get("source_revision") == str(script.get("source_revision") or "")
            )
            and (
                type(expected_size) is not int
                or expected_size < 0
                or installed.get("size_bytes") == expected_size
            )
            and (
                _COMPONENT_REVISION_RE.fullmatch(str(script.get("source_revision") or "")) is None
                or bool(installed.get("entrypoint_sha256") and installed.get("tree_sha256"))
            )
        ):
            return Path(installed["path"])
        if type(expected_size) is not int or expected_size < 0:
            raise RuntimeError("custom component requires an expected size")
    dl_dir = Path(config.custom_cache_dir) / "downloads" / key
    suffix = ".tar.gz" if entrypoint is not None or (data_only and file_rel.endswith(".tar.gz")) else (Path(file_rel).suffix or ".sh")
    cached = dl_dir / f"{key}-{expected_sha}{suffix}"
    if (cached.is_file() and not cached.is_symlink()
            and (component is None or cached.stat().st_size == expected_size)
            and hashlib.sha256(cached.read_bytes()).hexdigest() == expected_sha):
        if entrypoint is not None:
            if component is not None:
                return _activate_versioned_component(
                    config,
                    archive_path=cached,
                    key=key,
                    digest=expected_sha,
                    size_bytes=cached.stat().st_size,
                    entrypoint=str(entrypoint),
                    source_revision=str(script.get("source_revision") or ""),
                )
            return _activate_bundle(cached, dl_dir, expected_sha, entrypoint)
        if not data_only:
            cached.chmod(cached.stat().st_mode | 0o500)
        return cached
    if offline_only and preseed_bytes is None:
        if component is not None:
            raise CustomSelectedTaskError(
                f"custom_selected_component_artifact_missing_or_corrupt:{component[0]}:{key}:{expected_sha}"
            )
        raise CustomSelectedTaskError("custom_selected_task_artifact_missing_or_corrupt")
    target_dir = component_root if component_root is not None else dl_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target_dir, prefix=".download-", delete=False) as temporary:
            staged = Path(temporary.name)
            if preseed_bytes is not None:
                temporary.write(preseed_bytes)
            else:
                base = _manifest_url(config).rsplit("/", 1)[0] + "/"
                url = urllib.parse.urljoin(base, file_rel)
                req = mcc_request(url, headers=_headers(config))
                with urllib.request.urlopen(req, timeout=120) as resp:
                    shutil.copyfileobj(resp, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        got_sha = hashlib.sha256(staged.read_bytes()).hexdigest().lower()
        if component is not None and staged.stat().st_size != expected_size:
            raise RuntimeError(
                f"custom component size mismatch for {key}: expected={expected_size} got={staged.stat().st_size}"
            )
        if expected_sha and got_sha != expected_sha:
            raise RuntimeError(
                f"custom script checksum mismatch for {key}: expected={expected_sha} got={got_sha}"
            )
        if component is not None:
            return _activate_versioned_component(
                config,
                archive_path=staged,
                key=key,
                digest=got_sha,
                size_bytes=staged.stat().st_size,
                entrypoint=str(entrypoint),
                source_revision=str(script.get("source_revision") or ""),
            )
        # A revision cannot overwrite the pathname of a different running script.
        dst = dl_dir / f"{key}-{got_sha}{suffix}"
        os.chmod(staged, 0o600 if data_only else 0o755)
        os.replace(staged, dst)
        if entrypoint is not None:
            return _activate_bundle(dst, dl_dir, got_sha, entrypoint)
        return dst
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _component_status(
    identity: str,
    *,
    status: str,
    reason: str,
    source: str,
    operations: list[dict[str, Any]] | None = None,
    descriptor: dict[str, Any] | None = None,
    component: str = "",
    selected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    public_selected = None
    if isinstance(selected, dict):
        public_selected = {
            field: selected.get(field)
            for field in ("version", "key", "sha256", "size_bytes", "entrypoint", "source_revision")
            if selected.get(field) not in {None, ""}
        }
    descriptors = list(operations or []) if status == "ready" else []
    descriptor_metadata = dict(descriptor or {})
    capability = {
        "schema": "mcd-external-component-capability-v1",
        "identity": identity,
        "component": str(component or ""),
        "descriptor": descriptor_metadata,
        "operations": descriptors,
        "command": (
            "mcd-cli external-component invoke --identity <identity> "
            "--operation <operation> --input-json <file|-> --json"
        ),
    }
    return {
        "schema": _COMPONENT_STATUS_SCHEMA,
        "identity": identity,
        "status": status,
        "reason": reason,
        "source": source,
        "operations": descriptors,
        "descriptor": descriptor_metadata,
        "capabilities": [capability] if status == "ready" else [],
        "selected": public_selected,
    }


def _cached_component_manifest(config: AgentConfig) -> dict[str, Any]:
    cache_dir = str(getattr(config, "custom_cache_dir", "") or "").strip()
    if not cache_dir:
        raise RuntimeError("external_component_manifest_unavailable")
    path = Path(cache_dir) / "manifest.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("external_component_manifest_unavailable")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError("external_component_manifest_unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("external_component_manifest_invalid")
    return payload


def _component_binding_templates(
    document: dict[str, Any],
    *,
    identity: str,
    operations: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    bindings = document.get("operation_bindings")
    if not isinstance(bindings, list):
        raise RuntimeError("external_component_bindings_missing")
    selected: dict[str, dict[str, Any]] = {}
    allowed_sources = {
        "existing_private_operator_profile",
        "caller_operation_input",
        "mcd_resolved_mail_runtime",
        "mcd_resolved_plugin_filesystem",
        "effective_caller_cfg",
        "mcd_resolved_operation_context",
        "mcd_resolved_source_installation_context",
    }
    identity_rows = [
        row for row in bindings
        if isinstance(row, dict) and row.get("component_identity") == identity
    ]
    if not identity_rows:
        identity_rows = [
            row for row in bindings
            if isinstance(row, dict)
            and row.get("component_identity") is None
            and isinstance(row.get("artifact"), dict)
            and _component_coordinates(str(row["artifact"].get("key") or ""))
            and _component_coordinates(str(row["artifact"].get("key") or ""))[0] == identity
        ]
    requested = tuple(operations or ())
    if not requested:
        requested = tuple(
            dict.fromkeys(
                str(row.get("operation") or "").strip()
                for row in identity_rows
                if _COMPONENT_OPERATION_RE.fullmatch(str(row.get("operation") or "").strip())
            )
        )
    if not requested:
        raise RuntimeError("external_component_bindings_missing")
    for operation in requested:
        matches = [
            row for row in identity_rows
            if row.get("operation") == operation
        ]
        if len(matches) != 1:
            raise RuntimeError("external_component_bindings_missing")
        binding = matches[0]
        artifact = binding.get("artifact")
        args = binding.get("args")
        schema = binding.get("runtime_input_schema")
        mapping = binding.get("runtime_input_mapping")
        fields = schema.get("fields") if isinstance(schema, dict) else None
        if (
            not isinstance(binding.get("component"), str)
            or not str(binding.get("component") or "").strip()
            or _COMPONENT_OPERATION_RE.fullmatch(str(binding.get("operation") or "").strip()) is None
            or not isinstance(artifact, dict)
            or _component_coordinates(str(artifact.get("key") or "")) is None
            or _component_coordinates(str(artifact.get("key") or ""))[0] != identity
            or _COMPONENT_SHA_RE.fullmatch(str(artifact.get("sha256") or "").strip().lower()) is None
            or not isinstance(artifact.get("entrypoint"), str)
            or not artifact.get("entrypoint")
            or not isinstance(args, list)
            or any(not isinstance(value, str) or "\x00" in value for value in args)
            or binding.get("capture_result") is not True
            or binding.get("detach") is not False
            or binding.get("live_output") is not False
            or binding.get("offline_only") is not True
            or not isinstance(schema, dict)
            or schema.get("type") != "string-map"
            or not isinstance(fields, list)
            or not fields
            or any(not isinstance(field, str) or not field for field in fields)
            or len(set(fields)) != len(fields)
            or not isinstance(mapping, dict)
            or set(mapping) != set(fields)
        ):
            raise RuntimeError("external_component_bindings_invalid")
        for field in fields:
            spec = mapping.get(field)
            if (
                not isinstance(spec, dict)
                or spec.get("encoding") != "json"
                or spec.get("source") not in allowed_sources
            ):
                raise RuntimeError("external_component_bindings_invalid")
        selected[operation] = binding
    if len({str(binding.get("component") or "").strip() for binding in selected.values()}) != 1:
        raise RuntimeError("external_component_bindings_invalid")
    return selected


def _component_manifest_for_selection(
    config: AgentConfig,
    *,
    document: dict[str, Any],
    identity: str,
    operation_descriptors: list[dict[str, Any]] | None,
    selected: dict[str, Any],
    require_existing_coordinates: bool,
) -> dict[str, Any]:
    operation_names = _component_operation_names(operation_descriptors)
    templates = _component_binding_templates(
        document,
        identity=identity,
        operations=operation_names or None,
    )
    if not operation_names:
        operation_names = tuple(templates)
        operation_descriptors = [{"operation": name} for name in operation_names]
    patched = json.loads(json.dumps(document, ensure_ascii=True))
    patched_bindings = patched.get("operation_bindings")
    if not isinstance(patched_bindings, list):
        raise RuntimeError("external_component_bindings_missing")
    for operation, original in templates.items():
        artifact = original["artifact"]
        if require_existing_coordinates and any(
            artifact.get(field) != selected.get(field)
            for field in ("key", "sha256", "entrypoint")
        ):
            raise RuntimeError("external_component_binding_selection_mismatch")
        replacement = next(
            row for row in patched_bindings
            if isinstance(row, dict)
            and row.get("component") == original.get("component")
            and row.get("operation") == operation
            and row.get("component_identity") in {None, identity}
        )
        replacement["component_identity"] = identity
        replacement["artifact"] = {
            "key": selected["key"],
            "sha256": selected["sha256"],
            "size_bytes": selected["size_bytes"],
            "entrypoint": selected["entrypoint"],
        }

    item_file = str(selected.get("artifact_file") or "").strip()
    if not item_file:
        for row in _parse_manifest(document, deduplicate=False):
            if (
                row.get("key") == selected.get("key")
                and row.get("sha256") == selected.get("sha256")
                and row.get("entrypoint") == selected.get("entrypoint")
            ):
                item_file = str(row.get("file") or "").strip()
                break
    if not item_file:
        raise RuntimeError("external_component_catalog_item_missing")
    selected_item = {
        "type": "script",
        "key": selected["key"],
        "name": identity,
        "description": "Versioned external MCD component",
        "file": item_file,
        "sha256": selected["sha256"],
        "size_bytes": selected["size_bytes"],
        "entrypoint": selected["entrypoint"],
        "run_mode": "direct",
        "interactive": False,
        "component_identity": identity,
        "component_version": selected["version"],
        "source_revision": str(selected.get("source_revision") or ""),
        "selection_state": "selected",
        "operations": list(operation_descriptors or []),
    }
    rows = _parse_manifest(patched, deduplicate=False)
    if not any(
        row.get("key") == selected_item["key"]
        and row.get("sha256") == selected_item["sha256"]
        and row.get("size_bytes") == selected_item["size_bytes"]
        and row.get("entrypoint") == selected_item["entrypoint"]
        for row in rows
    ):
        patched["items"] = [*list(patched.get("items") or []), selected_item]
    component_selection = patched.get("component_selection")
    if not isinstance(component_selection, dict):
        component_selection = {}
    component_selection[identity] = {
        field: selected.get(field)
        for field in ("identity", "version", "key", "sha256", "size_bytes", "source_revision", "artifact_file", "entrypoint", "target_root")
        if selected.get(field) not in {None, ""}
    }
    component_selection[identity]["operations"] = list(operation_descriptors or [])
    patched["component_selection"] = component_selection
    return patched


def external_component_preflight(
    config: AgentConfig,
    *,
    identity: str,
    refresh: bool = True,
    _manifest_refreshed: bool = False,
) -> dict[str, Any]:
    """Resolve one MCC-selected component without interpreting its operations."""

    identity = str(identity or "").strip()
    if _COMPONENT_IDENTITY_RE.fullmatch(identity) is None:
        return _component_status(
            identity,
            status="unavailable",
            reason="external_component_request_invalid",
            source="none",
        )

    if refresh and not _manifest_refreshed:
        try:
            fetch_custom_manifest(config, use_cache_on_error=True)
        except Exception as exc:
            logging.warning("external component manifest refresh failed identity=%s: %s", identity, exc)

    if refresh:
        try:
            catalog = fetch_external_component_catalog(config, identity)
        except ExternalComponentCatalogInvalid as exc:
            try:
                _write_component_selection(
                    config,
                    identity,
                    status="blocked",
                    selected=None,
                    operations=[],
                    reason=str(exc),
                )
            except OSError:
                pass
            return _component_status(
                identity,
                status="unavailable",
                reason="external_component_catalog_invalid",
                source="mcc-catalog",
            )
        except ExternalComponentCatalogUnavailable:
            catalog = None
        if catalog is not None:
            selected = catalog.get("selected")
            operation_descriptors = list(catalog.get("operations") or [])
            descriptor_metadata = dict(catalog.get("descriptor") or {})
            operation_names = _component_operation_names(operation_descriptors)
            if not isinstance(selected, dict):
                _write_component_selection(
                    config,
                    identity,
                    status="blocked",
                    selected=None,
                    operations=operation_descriptors,
                    descriptor=descriptor_metadata,
                    reason="external_component_not_selected",
                )
                return _component_status(
                    identity,
                    status="unavailable",
                    reason="external_component_not_selected",
                    source="mcc-catalog",
                )
            if not operation_names:
                _write_component_selection(
                    config,
                    identity,
                    status="blocked",
                    selected=selected,
                    operations=operation_descriptors,
                    descriptor=descriptor_metadata,
                    reason="external_component_capabilities_missing",
                )
                return _component_status(
                    identity,
                    status="unavailable",
                    reason="external_component_capabilities_missing",
                    source="mcc-catalog",
                    selected=selected,
                )
            try:
                document = _cached_component_manifest(config)
                templates = _component_binding_templates(
                    document,
                    identity=identity,
                    operations=operation_names,
                )
                component_names = {
                    str(binding.get("component") or "").strip()
                    for binding in templates.values()
                }
                if len(component_names) != 1:
                    raise RuntimeError("external_component_bindings_invalid")
                component = next(iter(component_names))
                _write_component_selection(
                    config,
                    identity,
                    status="selected",
                    selected=selected,
                    operations=operation_descriptors,
                    descriptor=descriptor_metadata,
                )
                row = {
                    "key": selected["key"],
                    "file": selected["artifact_file"],
                    "sha256": selected["sha256"],
                    "size_bytes": selected["size_bytes"],
                    "entrypoint": selected["entrypoint"],
                    "source_revision": selected["source_revision"],
                    "run_mode": "direct",
                    "interactive": False,
                }
                _fetch_script_to_cache(config, row)
                patched = _component_manifest_for_selection(
                    config,
                    document=document,
                    identity=identity,
                    operation_descriptors=operation_descriptors,
                    selected=selected,
                    require_existing_coordinates=False,
                )
                _merge_cached_manifest(config, patched)
                installed = _installed_external_component(config, identity)
                if installed is None or not _component_record_matches(installed, selected):
                    raise RuntimeError("external_component_activation_unverified")
                _write_component_selection(
                    config,
                    identity,
                    status="ready",
                    selected=selected,
                    operations=operation_descriptors,
                    descriptor=descriptor_metadata,
                )
                return _component_status(
                    identity,
                    status="ready",
                    reason="",
                    source="mcc-catalog",
                    operations=operation_descriptors,
                    descriptor=descriptor_metadata,
                    component=component,
                    selected=installed,
                )
            except Exception as exc:
                logging.warning("external component activation failed identity=%s: %s", identity, exc)
                try:
                    _write_component_selection(
                        config,
                        identity,
                        status="blocked",
                        selected=selected,
                        operations=operation_descriptors,
                        descriptor=descriptor_metadata,
                        reason="external_component_activation_failed",
                    )
                except OSError:
                    pass
                return _component_status(
                    identity,
                    status="unavailable",
                    reason="external_component_activation_failed",
                    source="mcc-catalog",
                    selected=selected,
                )

    installed = _installed_external_component(config, identity)
    selection = _read_component_selection(config, identity)
    selected = selection.get("selected") if isinstance(selection, dict) else None
    operation_descriptors = list(selection.get("operations") or []) if isinstance(selection, dict) else []
    descriptor_metadata = dict(selection.get("descriptor") or {}) if isinstance(selection, dict) else {}
    if (
        isinstance(selection, dict)
        and selection.get("status") == "blocked"
        and (
            installed is None
            or not isinstance(selected, dict)
            or not _component_record_matches(installed, selected)
        )
    ):
        return _component_status(
            identity,
            status="unavailable",
            reason=str(selection.get("reason") or "external_component_blocked"),
            source="local",
            selected=selected if isinstance(selected, dict) else installed,
        )
    if installed is None:
        return _component_status(
            identity,
            status="unavailable",
            reason="external_component_local_invalid",
            source="local",
            selected=selected if isinstance(selected, dict) else None,
        )
    if isinstance(selected, dict) and not _component_record_matches(installed, selected):
        return _component_status(
            identity,
            status="unavailable",
            reason="external_component_selection_not_active",
            source="local",
            selected=selected,
        )
    try:
        document = _cached_component_manifest(config)
        templates = _component_binding_templates(
            document,
            identity=identity,
            operations=_component_operation_names(operation_descriptors) or None,
        )
        if not operation_descriptors:
            operation_descriptors = [
                {"id": operation}
                for operation in templates
            ]
        component_names = {
            str(binding.get("component") or "").strip()
            for binding in templates.values()
        }
        if len(component_names) != 1:
            raise RuntimeError("external_component_bindings_invalid")
        component = next(iter(component_names))
        patched = _component_manifest_for_selection(
            config,
            document=document,
            identity=identity,
            operation_descriptors=operation_descriptors,
            selected=installed,
            require_existing_coordinates=True,
        )
        _merge_cached_manifest(config, patched)
    except Exception as exc:
        logging.warning("external component local preflight failed identity=%s: %s", identity, exc)
        return _component_status(
            identity,
            status="unavailable",
            reason=str(exc) if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", str(exc)) else "external_component_bindings_invalid",
            source="local",
            selected=installed,
        )
    return _component_status(
        identity,
        status="ready",
        reason="",
        source="local-offline" if refresh else "local",
        operations=operation_descriptors,
        descriptor=descriptor_metadata,
        component=component,
        selected=installed,
    )


def external_component_identities(config: AgentConfig) -> tuple[str, ...]:
    """Discover component identities declared by the cached MCC manifest."""

    try:
        document = _cached_component_manifest(config)
    except RuntimeError:
        return ()
    candidates: set[str] = set()
    selection = document.get("component_selection")
    if isinstance(selection, dict):
        candidates.update(str(value or "").strip() for value in selection)
    for collection, field in ((document.get("items"), "component_identity"),
                              (document.get("components"), "identity"),
                              (document.get("operation_bindings"), "component_identity")):
        if isinstance(collection, list):
            candidates.update(
                str(row.get(field) or "").strip()
                for row in collection
                if isinstance(row, dict)
            )
    delivery = document.get("component_delivery")
    if isinstance(delivery, dict):
        candidates.add(str(delivery.get("identity") or "").strip())
    return tuple(sorted(
        value for value in candidates
        if _COMPONENT_IDENTITY_RE.fullmatch(value) is not None
    ))


def external_component_capability_projection(
    config: AgentConfig,
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    """Project every declared component without deriving compatibility policy."""

    if refresh:
        try:
            fetch_custom_manifest(config, use_cache_on_error=True)
        except Exception as exc:
            logging.warning("external component projection manifest refresh failed: %s", exc)
    statuses = [
        external_component_preflight(
            config,
            identity=identity,
            refresh=refresh,
            _manifest_refreshed=refresh,
        )
        for identity in external_component_identities(config)
    ]
    capabilities = [
        capability
        for status in statuses if status.get("status") == "ready"
        for capability in status.get("capabilities", []) if isinstance(capability, dict)
    ]
    return {
        "schema": "mcd-external-component-capability-catalog-v1",
        "components": statuses,
        "capabilities": capabilities,
    }


def run_external_component_operation(
    config: AgentConfig,
    *,
    identity: str,
    operation: str,
    operation_input: dict[str, Any],
) -> tuple[int, str]:
    """Invoke one manifest-declared operation through the generic dispatcher."""

    identity = str(identity or "").strip()
    operation = str(operation or "").strip()
    if (
        _COMPONENT_IDENTITY_RE.fullmatch(identity) is None
        or _COMPONENT_OPERATION_RE.fullmatch(operation) is None
        or not isinstance(operation_input, dict)
    ):
        return 2, "external_component_request_invalid"
    status = external_component_preflight(config, identity=identity, refresh=True)
    if status.get("status") != "ready":
        return 2, str(status.get("reason") or "external_component_unavailable")
    operation_names = set(_component_operation_names(status.get("operations")))
    if operation not in operation_names:
        return 2, "external_component_operation_unavailable"
    try:
        templates = _component_binding_templates(
            _cached_component_manifest(config),
            identity=identity,
            operations=(operation,),
        )
    except RuntimeError as exc:
        return 2, str(exc)
    component = str(templates[operation].get("component") or "").strip()
    return run_configured_external_operation(
        config,
        component=component,
        operation=operation,
        profile={},
        operation_input=operation_input,
    )


def external_component_operation_input_fields(
    config: AgentConfig,
    *,
    identity: str,
    operation: str,
) -> tuple[str, ...]:
    """Return only descriptor-declared caller fields for an adapter operation."""

    templates = _component_binding_templates(
        _cached_component_manifest(config),
        identity=str(identity or "").strip(),
        operations=(str(operation or "").strip(),),
    )
    binding = templates[str(operation or "").strip()]
    fields: list[str] = []
    mapping = binding.get("runtime_input_mapping")
    for spec in mapping.values() if isinstance(mapping, dict) else ():
        if not isinstance(spec, dict) or spec.get("source") not in {
            "caller_operation_input",
            "mcd_resolved_mail_runtime",
            "mcd_resolved_plugin_filesystem",
        }:
            continue
        declared = spec.get("fields")
        if isinstance(declared, list):
            fields.extend(str(value or "").strip() for value in declared)
    return tuple(dict.fromkeys(value for value in fields if value))



def _choose_mode(config: AgentConfig, requested_mode: str, detach: bool) -> str:
    mode = (requested_mode or "").strip().lower()
    if mode not in _RUN_MODES:
        mode = str(config.custom_run_mode_default or "auto").strip().lower()
    if mode not in _RUN_MODES:
        mode = "auto"
    if mode == "auto":
        if detach and config.custom_prefer_tmux and shutil.which("tmux"):
            return "tmux"
        if detach and config.custom_prefer_screen and shutil.which("screen"):
            return "screen"
        return "direct"
    if mode == "tmux" and not shutil.which("tmux"):
        return "direct"
    if mode == "screen" and not shutil.which("screen"):
        return "direct"
    return mode


def _run_detached_tmux(config: AgentConfig, script_path: Path, script_key: str, args: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    ts = int(time.time())
    prefix = str(config.custom_tmux_session_prefix or "mcd-custom").strip() or "mcd-custom"
    session = f"{prefix}-{script_key}-{ts}"
    session = session[:64]
    logs_dir = _cache_root(config) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{session}.log"
    cmd = " ".join([shlex.quote(str(script_path)), *(shlex.quote(x) for x in args)])
    shell_cmd = f"{cmd} 2>&1 | tee -a {shlex.quote(str(log_path))}"
    proc = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, *(["-c", str(cwd)] if cwd is not None else []), "bash", "-lc", shell_cmd],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return 1, f"tmux start failed: {err}"
    return 0, f"started in tmux session={session} log={log_path}"


def _run_detached_screen(config: AgentConfig, script_path: Path, script_key: str, args: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    ts = int(time.time())
    prefix = str(config.custom_tmux_session_prefix or "mcd-custom").strip() or "mcd-custom"
    session = f"{prefix}-{script_key}-{ts}"
    session = session[:64]
    logs_dir = _cache_root(config) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{session}.log"
    cmd = " ".join([shlex.quote(str(script_path)), *(shlex.quote(x) for x in args)])
    shell_cmd = f"{cmd} 2>&1 | tee -a {shlex.quote(str(log_path))}"
    proc = subprocess.run(
        ["screen", "-dmS", session, "bash", "-lc", shell_cmd],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return 1, f"screen start failed: {err}"
    return 0, f"started in screen session={session} log={log_path}"


def _run_direct(script_path: Path, args: list[str], *, live_output: bool, cwd: Path | None = None) -> tuple[int, str]:
    if live_output:
        proc = subprocess.run([str(script_path), *args], cwd=cwd)
        return int(proc.returncode), ""
    proc = subprocess.run([str(script_path), *args], capture_output=True, text=True, cwd=cwd)
    out = (proc.stdout or "") + ((("\n" + proc.stderr) if proc.stderr else ""))
    return int(proc.returncode), out.strip()


def _run_capture_result(script_path: Path, args: list[str], *, cwd: Path | None = None,
                        runtime_input: dict[str, Any] | None = None) -> tuple[int, str]:
    """Reuse the existing process/terminal contract for raw JSONline artifacts."""
    from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
    from mcd_agent.commercial_portal import canonical_json
    from mcd_agent.scenario_launcher import MAX_CONTEXT_BYTES
    argv = [str(script_path.resolve()), *args]
    framed = b""
    private_values: list[str] = []
    if runtime_input is not None:
        try:
            payload = json.dumps(runtime_input, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
            if not isinstance(runtime_input, dict) or not 0 < len(payload) <= MAX_CONTEXT_BYTES:
                raise ValueError()
            if any(not isinstance(value, str) for value in runtime_input.values()):
                raise ValueError()
            private_values = [value for value in runtime_input.values() if value]
            framed = struct.pack("!I", len(payload)) + payload
        except (ValueError, TypeError, UnicodeError):
            return 2, "custom runtime input invalid"
    # Ordinary custom direct execution has no implicit lifetime cap. Keep that
    # behavior while reusing bounded output collection and process supervision.
    try:
        policy = ExternalProcessPolicy(argv)
        completed = policy._run_bounded(argv, framed, os.environ.copy(), cwd=cwd or Path.cwd(), unlimited=True)
    except (ValueError, OSError, TimeoutError) as exc:
        return 1, "custom structured execution rejected" if runtime_input is not None else "custom structured execution rejected: " + str(exc)
    if runtime_input is not None:
        # The external process never streams to public logs in capture mode.
        # Reject accidental private-value echoes before decoding public frames.
        for value in private_values:
            for variant in {value, value.rstrip("\n")} - {""}:
                forms = [variant, hashlib.sha256(variant.encode()).hexdigest(),
                         json.dumps(variant, ensure_ascii=True)[1:-1],
                         json.dumps(variant, ensure_ascii=False)[1:-1]]
                if any(form.encode() in stream for form in forms
                       for stream in (completed.stdout or b"", completed.stderr or b"")):
                    return 1, "custom private output rejected"
    try:
        wire = bytearray()
        for line in (completed.stdout or b"").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("custom_result_frame_invalid")
            payload = canonical_json(value)
            wire.extend(struct.pack("!I", len(payload)))
            wire.extend(payload)
        terminal = policy._decode_output_frames(bytes(wire))[-1]
        if terminal["exit_code"] != completed.returncode:
            raise ValueError("custom_result_exit_mismatch")
        if terminal["status"] != "succeeded":
            return int(completed.returncode) or 1, json.dumps(terminal, sort_keys=True)
        if "result" not in terminal:
            raise ValueError("custom_public_result_missing")
        return 0, json.dumps(terminal["result"], ensure_ascii=True, sort_keys=True)
    except (ValueError, UnicodeError) as exc:
        return int(completed.returncode) or 1, "custom structured result rejected" if runtime_input is not None else "custom structured result rejected: " + str(exc)


def run_custom_script_by_key(
    config: AgentConfig,
    *,
    script_key: str,
    args: list[str] | None = None,
    detach: bool | None = None,
    live_output: bool = True,
    expected_sha256: str | None = None,
    fetch_only: bool = False,
    capture_result: bool = False,
    expected_entrypoint: str | None = None,
    runtime_input: dict[str, Any] | None = None,
    offline_only: bool = False,
) -> tuple[int, str]:
    if runtime_input is not None and not capture_result:
        return 2, "custom runtime input requires synchronous result capture"
    if capture_result and (fetch_only or detach is True):
        return 2, "custom result capture requires synchronous execution without fetch-only"
    expected = str(expected_sha256 or "").strip().lower()
    if expected and re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        return 2, "custom expected SHA-256 is invalid"
    key = str(script_key or "").strip()
    if not key:
        return 2, "script key is required"
    manifest = None
    if expected:
        # The selected task pin, not remote freshness or file provenance, is
        # authoritative here. A manually populated matching cache is equal.
        cache_manifest = Path(config.custom_cache_dir) / "manifest.json"
        if cache_manifest.is_file():
            try:
                cached_rows = _parse_manifest(json.loads(cache_manifest.read_text()), deduplicate=False)
                if any(row.get("key") == key and row.get("sha256") == expected for row in cached_rows):
                    manifest = cached_rows
            except (OSError, ValueError, RuntimeError):
                pass
    if manifest is None:
        if offline_only:
            return 2, "custom_selected_task_catalog_item_missing"
        manifest, _src = fetch_custom_manifest(config, use_cache_on_error=True)
    found = None
    for item in manifest:
        if (str(item.get("key", "")).strip() == key
                and (not expected or item.get("sha256") == expected)
                and (expected_entrypoint is None or item.get("entrypoint") == expected_entrypoint)):
            found = item
            break
    if found is None:
        same_key = [item for item in manifest if str(item.get("key", "")).strip() == key]
        if expected and same_key and all(item.get("sha256") != expected for item in same_key):
            return 1, "custom script manifest checksum does not match the requested pin"
        if expected_entrypoint is not None and same_key:
            return 1, "custom script entrypoint does not match the requested descriptor"
        return 2, f"custom script not found in manifest: {key}"

    if expected and str(found.get("sha256", "")).strip().lower() != expected:
        return 1, "custom script manifest checksum does not match the requested pin"
    if expected_entrypoint is not None and found.get("entrypoint") != expected_entrypoint:
        return 1, "custom script entrypoint does not match the requested descriptor"
    if capture_result and _to_bool(found.get("interactive", False)):
        return 2, "custom result capture does not support interactive artifacts"
    try:
        script_path = _fetch_script_to_cache(config, found, offline_only=True) if offline_only else _fetch_script_to_cache(config, found)
    except CustomSelectedTaskError as e:
        return 2, str(e)
    except Exception as e:
        return 1, f"custom script download failed: {e}"

    if fetch_only:
        return 0, json.dumps({"status": "cached", "key": key,
                              "sha256": found["sha256"], "path": str(script_path)}, sort_keys=True)
    arg_list = list(args or [])
    script_interactive = _to_bool(found.get("interactive", False))
    detach_effective = (not script_interactive) if detach is None else bool(detach)
    mode = _choose_mode(config, str(found.get("run_mode", "auto")), detach=detach_effective)
    execution_options: dict[str, Any] = {}
    if found.get("entrypoint") is not None:
        execution_options["cwd"] = script_path.parents[len(Path(found["entrypoint"]).parts) - 1]
    if capture_result:
        if runtime_input is not None:
            execution_options["runtime_input"] = runtime_input
        return _run_capture_result(script_path, arg_list, **execution_options)
    if detach_effective and mode == "tmux":
        return _run_detached_tmux(config, script_path, key, arg_list, **execution_options)
    if detach_effective and mode == "screen":
        return _run_detached_screen(config, script_path, key, arg_list, **execution_options)
    return _run_direct(script_path, arg_list, live_output=live_output, **execution_options)


def run_configured_external_operation(
    config: AgentConfig,
    *,
    component: str,
    operation: str,
    profile: dict[str, Any] | None,
    operation_input: dict[str, Any],
    resolved_context: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Dispatch an authority-bound operation through the existing custom item.

    Operation bindings and runtime-input field mappings are external manifest
    data.  MCD only validates the binding, constructs its declared private
    string-map, and delegates execution to the established custom-item cache,
    framing, supervision and public-result decoder.
    """
    if not isinstance(operation_input, dict):
        return 2, "custom_operation_input_invalid"
    try:
        path = _cache_manifest_path(config)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            return 2, "custom_operation_binding_not_configured"
        document = json.loads(path.read_text(encoding="utf-8"))
    except (AttributeError, OSError, UnicodeError, ValueError, TypeError):
        return 2, "custom_operation_binding_not_configured"
    if not isinstance(document, dict):
        return 2, "custom_operation_mapping_invalid"
    bindings = document.get("operation_bindings")
    if not isinstance(bindings, list):
        return 2, "custom_operation_binding_not_configured"
    matches = [item for item in bindings if isinstance(item, dict)
               and item.get("component") == component and item.get("operation") == operation]
    if not matches:
        return 2, "custom_operation_binding_not_configured"
    try:
        selected = _active_operation_binding(
            document, root=Path(config.custom_cache_dir), component=component,
            operation=operation, matches=matches,
        )
    except CustomSelectedTaskError as exc:
        return 2, str(exc)
    if selected is None and len(matches) != 1:
        return 2, "custom_operation_mapping_invalid"
    binding = selected or matches[0]
    artifact = binding.get("artifact")
    if (not isinstance(artifact, dict)
            or not isinstance(artifact.get("key"), str) or not artifact["key"].strip()
            or not isinstance(artifact.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"].strip().lower()) is None
            or not isinstance(artifact.get("entrypoint"), str) or not artifact["entrypoint"].strip()):
        return 2, "custom_operation_mapping_artifact_invalid"
    args = binding.get("args", [])
    if not isinstance(args, list) or any(not isinstance(value, str) or "\x00" in value for value in args):
        return 2, "custom_operation_mapping_args_invalid"
    if binding.get("capture_result") is not True or binding.get("detach") is not False or binding.get("live_output") is not False:
        return 2, "custom_operation_mapping_execution_invalid"
    if binding.get("offline_only") is not True:
        return 2, "custom_operation_mapping_offline_policy_invalid"
    schema = binding.get("runtime_input_schema")
    if (not isinstance(schema, dict) or schema.get("type") != "string-map"
            or not isinstance(schema.get("fields"), list)
            or not schema["fields"]
            or any(not isinstance(field, str) or not field for field in schema["fields"])
            or len(set(schema["fields"])) != len(schema["fields"])):
        return 2, "custom_operation_mapping_runtime_schema_invalid"
    fields = list(schema["fields"])
    mapping = binding.get("runtime_input_mapping")
    if not isinstance(mapping, dict) or set(mapping) != set(fields):
        return 2, "custom_operation_mapping_runtime_mapping_invalid"

    def _encoded(value: Any, declared_fields: Any = None, envelope_key: Any = None) -> str | None:
        if envelope_key is not None:
            if not isinstance(envelope_key, str) or not envelope_key.strip():
                return None
            value = {envelope_key: value}
        if declared_fields is not None:
            if (not isinstance(declared_fields, list)
                    or any(not isinstance(field, str) for field in declared_fields)):
                return None
            if not isinstance(value, dict) or any(field not in value for field in declared_fields):
                return None
            value = {field: value[field] for field in declared_fields}
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, UnicodeError):
            return None

    runtime_input: dict[str, str] = {}
    for field in fields:
        spec = mapping.get(field)
        if not isinstance(spec, dict) or spec.get("encoding") != "json":
            return 2, "custom_operation_mapping_runtime_mapping_invalid"
        source = spec.get("source")
        if source == "existing_private_operator_profile":
            value = profile if isinstance(profile, dict) else {}
        elif source in {
            "caller_operation_input",
            "mcd_resolved_mail_runtime",
            "mcd_resolved_plugin_filesystem",
        }:
            # These recipes name an already-resolved, non-secret runtime
            # object explicitly. It is still caller input at this boundary;
            # the descriptor owns the exact fields and MCD never expands it.
            value = operation_input
        elif source == "effective_caller_cfg":
            declared = spec.get("fields")
            if not isinstance(declared, list) or any(not isinstance(name, str) or not name for name in declared):
                return 2, "custom_operation_mapping_runtime_mapping_invalid"
            value = {name: getattr(config, name, None) for name in declared}
            if any(not hasattr(config, name) for name in declared):
                return 2, "custom_operation_mapping_runtime_mapping_invalid"
        elif source in {"mcd_resolved_operation_context", "mcd_resolved_source_installation_context"}:
            expected_schema = spec.get("schema")
            if (not isinstance(resolved_context, dict)
                    or not isinstance(expected_schema, str)
                    or not expected_schema
                    or resolved_context.get("schema") != expected_schema):
                return 2, "custom_operation_resolved_context_invalid"
            value = dict(resolved_context)
        else:
            return 2, "custom_operation_mapping_runtime_mapping_invalid"
        encoded = _encoded(value, spec.get("fields"), spec.get("envelope_key"))
        if encoded is None:
            return 2, "custom_operation_runtime_input_invalid"
        runtime_input[field] = encoded
    return run_custom_script_by_key(
        config,
        script_key=artifact["key"].strip(),
        args=list(args),
        expected_sha256=artifact["sha256"].strip().lower(),
        expected_entrypoint=artifact["entrypoint"],
        capture_result=True,
        detach=False,
        live_output=False,
        runtime_input=runtime_input,
        offline_only=True,
    )


def format_custom_scripts_list(rows: list[dict[str, Any]], *, with_idx: bool = True) -> str:
    if not rows:
        return "No custom scripts"
    lines: list[str] = []
    for idx, row in enumerate(rows, start=1):
        path = str(row.get("path", "")).strip()
        label = str(row.get("name", "")).strip()
        key = str(row.get("key", "")).strip()
        mode = str(row.get("run_mode", "auto")).strip()
        interactive = _to_bool(row.get("interactive", False))
        desc = str(row.get("description", "")).strip()
        prefix = f"{idx}. " if with_idx else ""
        if path:
            lines.append(f"{prefix}{label} [{key}] ({path}) mode={mode} interactive={'yes' if interactive else 'no'}")
        else:
            lines.append(f"{prefix}{label} [{key}] mode={mode} interactive={'yes' if interactive else 'no'}")
        if desc:
            lines.append(f"   {desc}")
    return "\n".join(lines)
