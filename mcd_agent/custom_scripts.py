from __future__ import annotations

from mcd_agent.mcc_transport import mcc_request

import hashlib
import fcntl
import json
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
    if downloads_dir.exists():
        entries = [p for p in downloads_dir.iterdir() if p.is_dir() or p.is_file()]
        entries.sort(key=_safe_mtime, reverse=True)
        for idx, path in enumerate(entries):
            mtime = _safe_mtime(path)
            age = max(0.0, now - mtime)
            key = path.name
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
        merged = _merge_cached_manifest(config, payload)
        return _parse_manifest(merged), "remote"
    except urllib.error.HTTPError as e:
        # Treat missing manifest as an empty custom scripts catalog.
        # This keeps interactive UX stable on fresh MCC installs before first custom publish.
        if int(getattr(e, "code", 0) or 0) == 404:
            empty = {"manifest_version": 1, "items": []}
            cache_path.write_text(json.dumps(empty, ensure_ascii=False) + "\n", encoding="utf-8")
            return [], "remote-empty"
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


def _fetch_script_to_cache(config: AgentConfig, script: dict[str, Any], *, offline_only: bool = False) -> Path:
    file_rel = str(script.get("file", "")).strip()
    if not file_rel:
        raise RuntimeError("custom script entry has empty file field")
    if file_rel.startswith("/") or ".." in Path(file_rel).parts:
        raise RuntimeError("custom script file path is invalid")

    key = str(script.get("key", "")).strip() or "script"
    expected_sha = str(script.get("sha256", "")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
        raise RuntimeError("custom artifact requires an expected SHA-256")

    entrypoint = script.get("entrypoint")
    if entrypoint is not None:
        if (not isinstance(entrypoint, str) or not entrypoint.strip()
                or "\x00" in entrypoint or Path(entrypoint).is_absolute()
                or ".." in Path(entrypoint).parts):
            raise RuntimeError("custom bundle entrypoint path is invalid")
        if not file_rel.endswith(".tar.gz") or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
            raise RuntimeError("custom bundle requires tar.gz and a manifest SHA-256")

    dl_dir = Path(config.custom_cache_dir) / "downloads" / key
    suffix = ".tar.gz" if entrypoint is not None else (Path(file_rel).suffix or ".sh")
    cached = dl_dir / f"{key}-{expected_sha}{suffix}"
    if cached.is_file() and not cached.is_symlink() and hashlib.sha256(cached.read_bytes()).hexdigest() == expected_sha:
        if entrypoint is not None:
            return _activate_bundle(cached, dl_dir, expected_sha, entrypoint)
        cached.chmod(cached.stat().st_mode | 0o500)
        return cached
    if offline_only:
        raise CustomSelectedTaskError("custom_selected_task_artifact_missing_or_corrupt")
    dl_dir.mkdir(parents=True, exist_ok=True)
    base = _manifest_url(config).rsplit("/", 1)[0] + "/"
    url = urllib.parse.urljoin(base, file_rel)
    req = mcc_request(url, headers=_headers(config))
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=dl_dir, prefix=".download-", delete=False) as temporary:
            staged = Path(temporary.name)
            with urllib.request.urlopen(req, timeout=120) as resp:
                shutil.copyfileobj(resp, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        got_sha = hashlib.sha256(staged.read_bytes()).hexdigest().lower()
        if expected_sha and got_sha != expected_sha:
            raise RuntimeError(
                f"custom script checksum mismatch for {key}: expected={expected_sha} got={got_sha}"
            )
        # A revision cannot overwrite the pathname of a different running script.
        dst = dl_dir / f"{key}-{got_sha}{suffix}"
        os.chmod(staged, 0o755)
        os.replace(staged, dst)
        if entrypoint is not None:
            return _activate_bundle(dst, dl_dir, got_sha, entrypoint)
        return dst
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)



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
