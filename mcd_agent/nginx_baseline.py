"""Core fact and pending shared Mautic/delete compatibility helpers.

Host baseline recipes run exclusively through the verified external task.
Shared vhost generators below remain pending their own extraction acceptance.
"""
from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from mcd_agent.nginx_external import (
    default_stub_operation, ensure_nginx_baseline, ensure_cloudflare_real_ip,
    cloudflare_real_ip_state, nginx_baseline_satisfied,
)

MAUTIC_PUBLIC_APP_ASSETS_COMMENT = "MCD public Mautic app assets"


_CLOUDFLARE_REAL_IP_PROFILE_KEYS = {
    "cloudflare_real_ip_enabled",
    "cloudflare_real_ip_target_file",
    "cloudflare_real_ip_header",
    "cloudflare_real_ip_cidrs",
    "cloudflare_real_ip_set_real_ip_from",
    "cloudflare_real_ip_recursive",
    "cloudflare_real_ip_remove_when_disabled",
}


@dataclass
class _Snapshot:
    path: Path
    kind: str
    backup: Path | None = None
    target: str | None = None


def _snapshot(path: Path, backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> None:
    path = Path(path)
    if path in snapshots:
        return
    if path.is_symlink():
        snapshots[path] = _Snapshot(path=path, kind="symlink", target=os.readlink(path))
        return
    if os.path.exists(path):
        rel = str(path).lstrip("/").replace("/", "__")
        backup = backup_dir / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        snapshots[path] = _Snapshot(path=path, kind="file", backup=backup)
        return
    snapshots[path] = _Snapshot(path=path, kind="missing")


def _restore_snapshots(snapshots: dict[Path, _Snapshot]) -> None:
    for snap in reversed(list(snapshots.values())):
        path = snap.path
        try:
            if os.path.lexists(path):
                path.unlink()
            if snap.kind == "file" and snap.backup is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap.backup, path)
            elif snap.kind == "symlink" and snap.target is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(snap.target, path)
        except Exception:
            continue


def _mautic_public_app_asset_locations(indent: str) -> list[str]:
    return [
        f"{indent}# {MAUTIC_PUBLIC_APP_ASSETS_COMMENT}; keep before private /app deny rules.",
        f"{indent}location ~* ^/app/bundles/.*/Assets/ {{",
        f"{indent}    try_files $uri =404;",
        f"{indent}}}",
        "",
        f"{indent}location ~* ^/app/assets/ {{",
        f"{indent}    try_files $uri =404;",
        f"{indent}}}",
        "",
    ]


def _find_location_block_end(lines: list[str], start: int) -> int:
    depth = 0
    seen_open = False
    for idx in range(start, len(lines)):
        raw = lines[idx]
        depth += raw.count("{") - raw.count("}")
        if "{" in raw:
            seen_open = True
        if seen_open and depth <= 0:
            return idx + 1
    return min(start + 1, len(lines))


def _normalize_server_blocks(text: str, transform) -> str:
    lines = text.splitlines()
    trailing_newline = text.endswith("\n")
    out: list[str] = []
    depth = 0
    in_server = False
    server_start_depth = 0
    block: list[str] = []

    for raw in lines:
        stripped = raw.strip()
        starts_server = (not in_server) and re.match(r"^server\s*\{", stripped)
        if starts_server:
            in_server = True
            server_start_depth = depth
            block = [raw]
        elif in_server:
            block.append(raw)
        else:
            out.append(raw)

        depth += raw.count("{") - raw.count("}")
        if in_server and depth <= server_start_depth:
            out.extend(transform(block))
            in_server = False
            block = []

    if in_server and block:
        out.extend(transform(block))
    return "\n".join(out).rstrip("\n") + ("\n" if trailing_newline or out else "")


def ensure_mautic_public_app_asset_locations(text: str) -> str:
    """Allow Mautic public /app assets before legacy private /app deny rules."""
    def normalize_block(lines: list[str]) -> list[str]:
        block_text = "\n".join(lines)
        if "^/app/bundles/.*/Assets/" in block_text and "^/app/assets/" in block_text:
            return lines
        for idx, raw in enumerate(lines):
            stripped = raw.strip()
            if not stripped.startswith("location "):
                continue
            if "/app" not in stripped and "(?:app" not in stripped:
                continue
            end = _find_location_block_end(lines, idx)
            deny_block = "\n".join(lines[idx:end])
            if not re.search(r"\b(?:deny\s+all|return\s+403)\b", deny_block, flags=re.IGNORECASE):
                continue
            indent = raw[: len(raw) - len(raw.lstrip())]
            return [
                *lines[:idx],
                *_mautic_public_app_asset_locations(indent),
                *lines[idx:],
            ]
        return lines

    return _normalize_server_blocks(text, normalize_block)


def _nginx_version_tuple() -> tuple[int, int, int] | None:
    binary = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").exists() else "")
    if not binary:
        return None
    try:
        proc = subprocess.run([binary, "-v"], capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return None
    raw = (proc.stderr or proc.stdout or "").strip()
    match = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", raw)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _nginx_supports_http2_directive() -> bool:
    version = _nginx_version_tuple()
    return bool(version and version >= (1, 25, 1))


def _ipv6_listen_forbidden() -> bool:
    try:
        from mcd_agent.env import ipv6_runtime_disabled, ipv6_status

        return ipv6_runtime_disabled(ipv6_status()) is True
    except Exception:
        return False


def normalize_legacy_http2_listen(text: str, *, modern_http2: bool) -> str:
    """Convert deprecated `listen ... http2` syntax when nginx supports `http2 on`."""
    if not modern_http2 or "http2" not in text:
        return text

    listen_re = re.compile(r"^(\s*listen\s+)([^;#]*?)(\s*;.*)$", re.IGNORECASE)
    http2_on_re = re.compile(r"^\s*http2\s+on\s*;", re.IGNORECASE)

    def normalize_block(lines: list[str]) -> list[str]:
        changed = False
        has_http2_on = any(http2_on_re.match(raw.strip()) for raw in lines if not raw.lstrip().startswith("#"))
        out: list[str] = []
        last_listen_idx = -1
        last_listen_indent = "    "
        for raw in lines:
            line = raw.lstrip()
            match = listen_re.match(raw)
            if match and not line.startswith("#"):
                tokens = match.group(2).split()
                if "http2" in tokens:
                    tokens = [token for token in tokens if token != "http2"]
                    raw = match.group(1) + " ".join(tokens) + match.group(3)
                    changed = True
                last_listen_idx = len(out)
                last_listen_indent = raw[: len(raw) - len(raw.lstrip())]
            out.append(raw)
        if changed and not has_http2_on and last_listen_idx >= 0:
            out.insert(last_listen_idx + 1, f"{last_listen_indent}http2 on;")
        return out

    return _normalize_server_blocks(text, normalize_block)


def cloudflare_real_ip_profile_present(profile: dict[str, Any] | None) -> bool:
    if not isinstance(profile, dict):
        return False
    return any(key in profile for key in _CLOUDFLARE_REAL_IP_PROFILE_KEYS)
