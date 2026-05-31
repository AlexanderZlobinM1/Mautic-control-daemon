from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from mcd_agent import __version__
from mcd_agent.config import AgentConfig


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
                "sha256": str(raw.get("sha256", "")).strip().lower(),
                "run_mode": str(raw.get("run_mode", "auto")).strip().lower() or "auto",
                "interactive": _to_bool(raw.get("interactive", False)),
                "args_help": str(raw.get("args_help", "")).strip(),
                "path": " / ".join(parent),
            }
        )


def _parse_manifest(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("custom manifest: items must be a list")
    out: list[dict[str, Any]] = []
    _flatten_items(items, [], out)
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


def fetch_custom_manifest(config: AgentConfig, *, use_cache_on_error: bool = True) -> tuple[list[dict[str, Any]], str]:
    url = _manifest_url(config)
    req = urllib.request.Request(url, headers=_headers(config))
    cache_path = _cache_manifest_path(config)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            text = (resp.read() or b"").decode("utf-8", errors="replace")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise RuntimeError("custom manifest is not a JSON object")
        rows = _parse_manifest(payload)
        cache_path.write_text(text, encoding="utf-8")
        return rows, "remote"
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


def _fetch_script_to_cache(config: AgentConfig, script: dict[str, Any]) -> Path:
    file_rel = str(script.get("file", "")).strip()
    if not file_rel:
        raise RuntimeError("custom script entry has empty file field")
    if file_rel.startswith("/") or ".." in Path(file_rel).parts:
        raise RuntimeError("custom script file path is invalid")

    base = _manifest_url(config).rsplit("/", 1)[0] + "/"
    url = urllib.parse.urljoin(base, file_rel)
    key = str(script.get("key", "")).strip() or "script"
    expected_sha = str(script.get("sha256", "")).strip().lower()

    dl_dir = _cache_root(config) / "downloads" / key
    dl_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file_rel).suffix or ".sh"
    dst = dl_dir / f"{key}{suffix}"

    req = urllib.request.Request(url, headers=_headers(config))
    with urllib.request.urlopen(req, timeout=120) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)
    os.chmod(dst, 0o755)

    got_sha = hashlib.sha256(dst.read_bytes()).hexdigest().lower()
    if expected_sha and got_sha != expected_sha:
        raise RuntimeError(
            f"custom script checksum mismatch for {key}: expected={expected_sha} got={got_sha}"
        )
    return dst


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


def _run_detached_tmux(config: AgentConfig, script_path: Path, script_key: str, args: list[str]) -> tuple[int, str]:
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
        ["tmux", "new-session", "-d", "-s", session, "bash", "-lc", shell_cmd],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return 1, f"tmux start failed: {err}"
    return 0, f"started in tmux session={session} log={log_path}"


def _run_detached_screen(config: AgentConfig, script_path: Path, script_key: str, args: list[str]) -> tuple[int, str]:
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
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return 1, f"screen start failed: {err}"
    return 0, f"started in screen session={session} log={log_path}"


def _run_direct(script_path: Path, args: list[str], *, live_output: bool) -> tuple[int, str]:
    if live_output:
        proc = subprocess.run([str(script_path), *args])
        return int(proc.returncode), ""
    proc = subprocess.run([str(script_path), *args], capture_output=True, text=True)
    out = (proc.stdout or "") + ((("\n" + proc.stderr) if proc.stderr else ""))
    return int(proc.returncode), out.strip()


def run_custom_script_by_key(
    config: AgentConfig,
    *,
    script_key: str,
    args: list[str] | None = None,
    detach: bool | None = None,
    live_output: bool = True,
) -> tuple[int, str]:
    key = str(script_key or "").strip()
    if not key:
        return 2, "script key is required"
    manifest, _src = fetch_custom_manifest(config, use_cache_on_error=True)
    found = None
    for item in manifest:
        if str(item.get("key", "")).strip() == key:
            found = item
            break
    # Optional fallback: allow selection by display name from manifest.
    if found is None:
        for item in manifest:
            if str(item.get("name", "")).strip().lower() == key.lower():
                found = item
                break
    if found is None:
        return 2, f"custom script not found in manifest: {key}"

    try:
        script_path = _fetch_script_to_cache(config, found)
    except Exception as e:
        return 1, f"custom script download failed: {e}"

    arg_list = list(args or [])
    script_interactive = _to_bool(found.get("interactive", False))
    detach_effective = (not script_interactive) if detach is None else bool(detach)
    mode = _choose_mode(config, str(found.get("run_mode", "auto")), detach=detach_effective)
    if detach_effective and mode == "tmux":
        return _run_detached_tmux(config, script_path, key, arg_list)
    if detach_effective and mode == "screen":
        return _run_detached_screen(config, script_path, key, arg_list)
    return _run_direct(script_path, arg_list, live_output=live_output)


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
