from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from typing import Any
from urllib.request import Request, urlopen


YOURLS_REPO_API = "https://api.github.com/repos/YOURLS/YOURLS/releases/latest"
YOURLS_TARBALL_URL = "https://github.com/YOURLS/YOURLS/archive/refs/tags/{version}.tar.gz"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _semver(value: str) -> tuple[int, int, int, tuple[int, ...]]:
    parts = [int(x) for x in re.findall(r"\d+", str(value or ""))]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2], tuple(parts[3:]))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def yourls_version(root: str | Path) -> str:
    root_p = Path(root)
    candidates = [
        root_p / "includes" / "version.php",
        root_p / "includes" / "functions.php",
        root_p / "yourls-loader.php",
    ]
    for path in candidates:
        if not path.exists():
            continue
        text = _read_text(path)
        m = re.search(r"YOURLS_VERSION['\"]?\s*,\s*['\"]([^'\"]+)['\"]", text)
        if m:
            return m.group(1).strip()
    return ""


def yourls_site_url(root: str | Path) -> str:
    cfg = Path(root) / "user" / "config.php"
    if not cfg.exists():
        return ""
    text = _read_text(cfg)
    m = re.search(r"YOURLS_SITE['\"]?\s*,\s*['\"]([^'\"]+)['\"]", text)
    return m.group(1).strip() if m else ""


def _nginx_root_map() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    paths = [Path("/etc/nginx/sites-available"), Path("/etc/nginx/sites-enabled")]
    for base in paths:
        if not base.exists():
            continue
        for conf in base.glob("*"):
            if not conf.is_file():
                continue
            text = _read_text(conf)
            roots = re.findall(r"^\s*root\s+([^;]+);", text, flags=re.MULTILINE)
            names = re.findall(r"^\s*server_name\s+([^;]+);", text, flags=re.MULTILINE)
            server_names: list[str] = []
            for raw in names:
                server_names.extend([x.strip() for x in raw.split() if x.strip() and x.strip() != "_"])
            for root in roots:
                norm = str(Path(root.strip()).resolve())
                out.setdefault(norm, [])
                for name in server_names:
                    if name not in out[norm]:
                        out[norm].append(name)
    return out


def discover_yourls(search_roots: list[str] | None = None) -> list[dict[str, Any]]:
    roots = [Path(x) for x in (search_roots or ["/var/www", "/srv/www", "/home"])]
    nginx = _nginx_root_map()
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    skip_dirs = {"cache", "node_modules", "vendor", ".git", "var", "tmp", "logs"}
    for base in roots:
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
            if "yourls-api.php" not in filenames or "yourls-loader.php" not in filenames:
                continue
            root = str(Path(dirpath).resolve())
            if root in seen:
                continue
            seen.add(root)
            version = yourls_version(root)
            site_url = yourls_site_url(root)
            server_names = nginx.get(root, [])
            rows.append(
                {
                    "kind": "yourls",
                    "provider": "yourls",
                    "root": root,
                    "site_url": site_url,
                    "api_url": (site_url.rstrip("/") + "/yourls-api.php") if site_url else "",
                    "version": version,
                    "active_nginx": bool(server_names),
                    "server_names": server_names,
                    "legacy_candidate": root.endswith("_old") or "/old" in root.lower(),
                }
            )
    rows.sort(key=lambda x: (not bool(x.get("active_nginx")), bool(x.get("legacy_candidate")), str(x.get("root", ""))))
    return rows


def latest_yourls_version() -> str:
    req = Request(
        YOURLS_REPO_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "mcd-agent-yourls",
        },
    )
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    tag = str(payload.get("tag_name") or payload.get("name") or "").strip().lstrip("v")
    if not re.search(r"\d+\.\d+", tag):
        raise RuntimeError("latest YOURLS release has no usable semantic version")
    return tag


def check_yourls_update(root: str | Path) -> dict[str, Any]:
    installed = yourls_version(root)
    latest = latest_yourls_version()
    return {
        "status": "ok",
        "kind": "yourls",
        "root": str(Path(root).resolve()),
        "version": installed,
        "latest_version": latest,
        "update_available": bool(installed and _semver(latest) > _semver(installed)),
        "checked_at": _now_iso(),
    }


def _copy_core(src: Path, dst: Path) -> None:
    # Keep user/ untouched: it contains config, plugins, and local customizations.
    for item in src.iterdir():
        if item.name == "user":
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(item, target, follow_symlinks=False)


def _safe_extract_tar(tf: tarfile.TarFile, target: Path) -> None:
    target_resolved = target.resolve()
    for member in tf.getmembers():
        member_path = (target / member.name).resolve()
        if not str(member_path).startswith(str(target_resolved) + os.sep):
            raise RuntimeError(f"unsafe path in YOURLS archive: {member.name}")
    tf.extractall(target)


def update_yourls(root: str | Path, *, target_version: str | None = None, yes: bool = False) -> dict[str, Any]:
    root_p = Path(root).resolve()
    if not yes:
        raise RuntimeError("refusing to update without --yes")
    if not (root_p / "yourls-api.php").exists() or not (root_p / "user" / "config.php").exists():
        raise RuntimeError(f"not a YOURLS install root: {root_p}")
    current = yourls_version(root_p)
    target = (target_version or latest_yourls_version()).strip().lstrip("v")
    if current and _semver(target) <= _semver(current):
        return {
            "status": "ok",
            "changed": False,
            "root": str(root_p),
            "version": current,
            "latest_version": target,
            "message": "already up to date",
        }
    backup_dir = Path("/var/backups/mcd/shortners")
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"yourls-{root_p.name}-{ts}.tar.gz"
    with tarfile.open(backup_path, "w:gz") as tf:
        tf.add(root_p, arcname=root_p.name)
    with tempfile.TemporaryDirectory(prefix="mcd-yourls-update-") as td:
        tar_path = Path(td) / "yourls.tar.gz"
        req = Request(YOURLS_TARBALL_URL.format(version=target), headers={"User-Agent": "mcd-agent-yourls"})
        with urlopen(req, timeout=60) as resp:
            tar_path.write_bytes(resp.read())
        extract_dir = Path(td) / "extract"
        extract_dir.mkdir()
        with tarfile.open(tar_path, "r:gz") as tf:
            _safe_extract_tar(tf, extract_dir)
        source_dirs = [p for p in extract_dir.iterdir() if p.is_dir()]
        if not source_dirs:
            raise RuntimeError("downloaded YOURLS archive did not contain a source directory")
        _copy_core(source_dirs[0], root_p)
    new_version = yourls_version(root_p)
    return {
        "status": "ok",
        "changed": True,
        "root": str(root_p),
        "previous_version": current,
        "version": new_version,
        "target_version": target,
        "backup_path": str(backup_path),
        "updated_at": _now_iso(),
    }
