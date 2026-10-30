from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from mcd_agent.config import AgentConfig
from mcd_agent.discovery import discover_mautic
from mcd_agent.models import MauticInstall
from mcd_agent.runtime_descriptor import descriptor_for_root

_SEMVER_RE = re.compile(r"(\d+\.\d+\.\d+)")
_VERSION_CACHE_ROOT = Path("/opt/mcd/generated/mautic-version")
_LEGACY_VERSION_CACHE_REL = Path(".mcd") / "mautic.version"
_ZABBIX_HELPER_PATH = Path("/usr/local/bin/zbx_mautic_version_cached.sh")
_ZABBIX_CONF_PATH = Path("/etc/zabbix/zabbix_agent2.d/mautic.conf")
def version_cache_path(root: str | Path) -> Path:
    normalized = os.path.abspath(str(root))
    key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return _VERSION_CACHE_ROOT / f"{key}.version"


def _legacy_version_cache_path(root: str | Path) -> Path:
    return Path(root) / _LEGACY_VERSION_CACHE_REL


def read_cached_mautic_version(root: str | Path) -> str | None:
    path = version_cache_path(root)
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if raw and raw != "-":
        return raw.splitlines()[0].strip() or None
    return None


def migrate_legacy_mautic_version_cache(root: str | Path) -> bool:
    legacy = _legacy_version_cache_path(root)
    try:
        raw = legacy.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return False
    if not raw or raw == "-":
        return False
    return write_mautic_version_cache(root, raw.splitlines()[0])


def _atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_mautic_version_cache(root: str | Path, version: str) -> bool:
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        return False
    safe = str(version or "").strip() or "-"
    if "\n" in safe:
        safe = safe.splitlines()[0].strip() or "-"
    try:
        _atomic_write_text(version_cache_path(root_path), safe + "\n")
        return True
    except OSError:
        return False


def _candidate_roots(root: str) -> list[Path]:
    root_path = Path(root)
    candidates: list[Path] = [root_path]
    base = root_path.name.lower()
    if base in {"public", "docroot", "public_html"}:
        candidates.append(root_path.parent)
    out: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        ps = str(p)
        if ps == "/" or ps in seen:
            continue
        seen.add(ps)
        out.append(p)
    return out


def _version_major(version: str | None) -> int | None:
    match = _SEMVER_RE.search(str(version or ""))
    if not match:
        return None
    try:
        return int(match.group(1).split(".", 1)[0])
    except ValueError:
        return None


def _version_tuple(version: str | None) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.search(str(version or ""))
    if not match:
        return None
    try:
        parts = match.group(1).split(".")
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _read_version_from_release_metadata(root: Path) -> str | None:
    versions: list[str] = []
    for relative in (
        "app/release_metadata.json",
        "app/bundles/CoreBundle/release_metadata.json",
        "docroot/app/release_metadata.json",
        "docroot/app/bundles/CoreBundle/release_metadata.json",
        "public/app/release_metadata.json",
        "public/app/bundles/CoreBundle/release_metadata.json",
    ):
        path = root / relative
        if not path.is_file():
            continue
        try:
            value = str(json.loads(path.read_text(encoding="utf-8")).get("version", "")).strip()
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if _version_tuple(value) is not None:
            versions.append(value)
    return max(versions, key=lambda value: _version_tuple(value) or (0, 0, 0)) if versions else None


def _newer_local_version_metadata(root: Path, cached: str) -> str | None:
    cached_version = _version_tuple(cached)
    if cached_version is None:
        return None
    candidates = [
        value
        for value in (_read_version_from_composer_lock(root), _read_version_from_release_metadata(root))
        if value is not None and (_version_tuple(value) or (0, 0, 0)) > cached_version
    ]
    return max(candidates, key=lambda value: _version_tuple(value) or (0, 0, 0)) if candidates else None


def _read_static_mautic_version_evidence(root: Path) -> tuple[str | None, str]:
    static_versions = [
        value
        for value in (_read_version_from_release_metadata(root), _read_version_from_composer_lock(root))
        if value
    ]
    if not static_versions:
        return None, "unavailable_read_only"
    if len({_version_tuple(value) for value in static_versions}) != 1:
        return None, "conflicting_static_metadata"
    return max(static_versions, key=lambda value: _version_tuple(value) or (0, 0, 0)), "static_metadata"


def _read_static_mautic_version(root: Path) -> str | None:
    version, source = _read_static_mautic_version_evidence(root)
    return version if source == "static_metadata" else None


def read_mautic_version_evidence_read_only(root: str | Path) -> dict[str, str | None]:
    """Return read-only version evidence and distinguish cache fallback."""
    candidates = _candidate_roots(str(root))
    for candidate in candidates:
        version, source = _read_static_mautic_version_evidence(candidate)
        if source == "conflicting_static_metadata":
            return {"version": None, "source": source}
        if source == "static_metadata":
            return {"version": version, "source": source}
    for candidate in candidates:
        cached = read_cached_mautic_version(candidate)
        if cached:
            return {"version": cached, "source": "cache_fallback"}
    return {"version": None, "source": "unavailable_read_only"}


def read_mautic_version_read_only(root: str | Path) -> str | None:
    """Read version evidence without invoking PHP, Symfony or Mautic code."""
    return read_mautic_version_evidence_read_only(root).get("version")


def read_installed_mautic_version_read_only(root: str | Path) -> str | None:
    """Observe Composer's installed packages, without executing or writing app code."""
    for candidate in _candidate_roots(str(root)):
        try:
            with (candidate / "vendor/composer/installed.json").open("rb") as handle:
                raw = handle.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                return None
            payload = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        packages = payload.get("packages") if isinstance(payload, dict) else payload
        if not isinstance(packages, list):
            continue
        versions = set()
        for package in packages:
            if not isinstance(package, dict) or package.get("name") not in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
                continue
            value = package.get("pretty_version") or package.get("version")
            if isinstance(value, str) and re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?(?:\+[0-9A-Za-z][0-9A-Za-z.-]*)?", value):
                versions.add(value.removeprefix("v"))
        if len(versions) > 1:
            return None
        if versions:
            return versions.pop()
    return None


def _read_version_from_composer_lock(root: Path) -> str | None:
    lock = root / "composer.lock"
    if not lock.exists():
        return None
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return None
    packages = data.get("packages", [])
    if not isinstance(packages, list):
        return None
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        if str(pkg.get("name", "")).strip() not in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
            continue
        version = str(pkg.get("version", "")).strip()
        match = _SEMVER_RE.search(version)
        if match:
            return match.group(1)
    return None


def _read_major_from_composer_lock(root: Path) -> int | None:
    lock = root / "composer.lock"
    if not lock.exists():
        return None
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return None
    packages = data.get("packages", [])
    if not isinstance(packages, list):
        return None
    for package in packages:
        if not isinstance(package, dict):
            continue
        if str(package.get("name", "")).strip() not in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
            continue
        match = re.match(r"^v?(\d+)(?:\.|$)", str(package.get("version", "")).strip())
        if match:
            return int(match.group(1))
    return None


def _read_version_from_runtime_only(
    root: Path,
    php_bin: str,
    console_path: str | None = None,
    *,
    run_as_user: str | None = "www-data",
) -> str | None:
    try:
        descriptor = descriptor_for_root(str(root))
    except (OSError, ValueError):
        descriptor = None
    if descriptor is not None:
        commands = [[*descriptor.docker_exec_prefix(), "--version"], [*descriptor.docker_exec_prefix(), "about", "--no-interaction"]]
    else:
        console = _console_path_for_root(root, console_path)
        if console is None:
            return None
        commands = [[php_bin, str(console), "--version"], [php_bin, str(console), "about", "--no-interaction"]]
        user = str(run_as_user or "").strip()
        if user and user != "root" and hasattr(os, "geteuid") and os.geteuid() == 0:
            commands = [["sudo", "-H", "-u", user, *command] for command in commands]
    for command in commands:
        try:
            proc = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception:
            continue
        match = _SEMVER_RE.search((proc.stdout or "") + "\n" + (proc.stderr or ""))
        if proc.returncode == 0 and match:
            return match.group(1)
    return None


def confirmed_mautic_major(
    root: str,
    php_bin: str,
    *,
    console_path: str | None = None,
    local_php_path: str | None = None,
    expected_major: int | None = None,
    run_as_user: str | None = "www-data",
) -> int | None:
    """Return a major only when runtime, lock metadata and layout agree."""
    root_path = Path(root)
    runtime_major = _version_major(
        _read_version_from_runtime_only(
            root_path,
            php_bin,
            console_path,
            run_as_user=run_as_user,
        )
    )
    if runtime_major is None:
        return None

    try:
        descriptor = descriptor_for_root(str(root_path))
    except (OSError, ValueError):
        descriptor = None
    lock_majors: set[int] = set()
    if descriptor is not None and descriptor.host_composer_lock_path is not None:
        major = _read_major_from_composer_lock(descriptor.host_composer_lock_path.parent)
        if major is not None:
            lock_majors.add(major)
    else:
        for candidate in _candidate_roots(root):
            major = _read_major_from_composer_lock(candidate)
            if major is not None:
                lock_majors.add(major)
    if lock_majors and lock_majors != {runtime_major}:
        return None
    if expected_major is not None and int(expected_major) != runtime_major:
        return None

    local_path = str(local_php_path or "").strip()
    if local_path.endswith("/app/config/local.php") and runtime_major != 4:
        return None
    if local_path.endswith("/config/local.php") and not local_path.endswith("/app/config/local.php"):
        if runtime_major < 5:
            return None
    return runtime_major


def _console_path_for_root(root: Path, console_path: str | None = None) -> Path | None:
    if console_path:
        supplied = Path(console_path)
        if supplied.exists() and supplied.is_file():
            return supplied
    for rel in ("bin/console", "app/console"):
        candidate = root / rel
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _read_version_from_mcd_source(
    root: Path,
    php_bin: str,
    console_path: str | None = None,
    *,
    run_as_user: str | None = "www-data",
) -> str | None:
    try:
        descriptor = descriptor_for_root(str(root))
    except (OSError, ValueError):
        descriptor = None
    if descriptor is not None:
        for arguments in (["--version"], ["about", "--no-interaction"]):
            try:
                proc = subprocess.run(
                    [*descriptor.docker_exec_prefix(), *arguments],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except Exception:
                continue
            match = _SEMVER_RE.search((proc.stdout or "") + "\n" + (proc.stderr or ""))
            if proc.returncode == 0 and match:
                return match.group(1)
        if descriptor.host_composer_lock_path is not None:
            return _read_version_from_composer_lock(descriptor.host_composer_lock_path.parent)
        return None
    console = _console_path_for_root(root, console_path)
    if not console:
        return None
    try:
        # Avoid a module cycle: mautic_upgrade also refreshes this shared cache.
        from mcd_agent.mautic_upgrade import _read_current_version

        value = _read_current_version(str(root), str(console), php_bin, run_as_user).strip()
    except Exception:
        return None
    if value and value != "0.0.0" and _SEMVER_RE.search(value):
        return value
    return None


def _cache_roots(original_root: str, detected_root: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    for item in [Path(original_root), detected_root]:
        if item is None:
            continue
        if item.exists() and item.is_dir() and item not in roots:
            roots.append(item)
    return roots


def collect_mautic_version(
    root: str,
    php_bin: str,
    *,
    console_path: str | None = None,
    update_cache: bool = True,
    run_as_user: str | None = "www-data",
    force_refresh: bool = False,
    expected_major: int | None = None,
) -> str:
    detected_root: Path | None = None
    version: str | None = None
    read_only_evidence = read_mautic_version_evidence_read_only(root)
    if read_only_evidence.get("source") == "conflicting_static_metadata":
        return "-"

    # Regular state pushes must be lightweight and must not start Mautic console
    # every few seconds. A forced refresh is used by the explicit Zabbix/cache
    # guard and runs the console as the Mautic runtime user, not root.
    if not force_refresh:
        version = str(read_only_evidence.get("version") or "") or None
        if version and expected_major is not None and _version_major(version) != int(expected_major):
            version = None
        if version:
            detected_root = Path(root)

    if force_refresh and not version:
        for candidate in _candidate_roots(root):
            version = _read_static_mautic_version(candidate)
            if version:
                detected_root = candidate
                break

    if not version:
        for candidate in _candidate_roots(root):
            version = _read_version_from_mcd_source(candidate, php_bin, console_path, run_as_user=run_as_user)
            if version:
                detected_root = candidate
                break

    if not version:
        # Last resort only: composer.lock can lag behind patched runtime state on
        # some migrated installs, so do not prefer it over the Mautic console.
        for candidate in _candidate_roots(root):
            version = _read_version_from_composer_lock(candidate)
            if version:
                detected_root = candidate
                break

    value = version or "-"
    if update_cache and value != "-":
        for cache_root in _cache_roots(root, detected_root):
            write_mautic_version_cache(cache_root, value)
    return value


def refresh_mautic_version_cache(
    installs: list[MauticInstall],
    php_bin: str,
    *,
    run_as_user: str | None = "www-data",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    ok = 0
    for inst in installs:
        version = collect_mautic_version(
            inst.root,
            php_bin,
            console_path=inst.console_path,
            update_cache=True,
            run_as_user=run_as_user,
            force_refresh=True,
            expected_major=inst.mautic_major,
        )
        cache = version_cache_path(inst.root)
        cached = cache.exists() and cache.is_file()
        if cached and version != "-":
            ok += 1
        rows.append(
            {
                "instance_uid": inst.instance_uid,
                "name": inst.name,
                "root": inst.root,
                "version": version,
                "cache_path": str(cache),
                "cached": cached,
            }
        )
    return {"status": "ok", "updated": ok, "instances": rows}


def discover_and_refresh_mautic_version_cache(cfg: AgentConfig) -> dict[str, Any]:
    installs = discover_mautic(
        cfg.discovery_roots,
        cfg.exclude_path_contains,
        supported_mautic_majors=cfg.supported_mautic_majors,
        custom_instances=cfg.custom_instances,
    )
    return refresh_mautic_version_cache(installs, cfg.php_bin, run_as_user=cfg.mautic_run_as_user)


def retire_zabbix_mautic_version_userparameter() -> dict[str, Any]:
    """Remove the retired per-instance Zabbix version probe without touching other checks."""
    changed = False
    if _ZABBIX_CONF_PATH.exists():
        try:
            lines = _ZABBIX_CONF_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
            kept = [line for line in lines if not line.strip().startswith("UserParameter=mautic.version[")]
            if kept != lines:
                _atomic_write_text(_ZABBIX_CONF_PATH, "\n".join(kept).rstrip() + ("\n" if kept else ""))
                changed = True
        except OSError as exc:
            return {"status": "error", "reason": str(exc), "changed": False}
    try:
        if _ZABBIX_HELPER_PATH.exists():
            _ZABBIX_HELPER_PATH.unlink()
            changed = True
        legacy_backup = _ZABBIX_CONF_PATH.with_suffix(_ZABBIX_CONF_PATH.suffix + ".mcd-bak")
        if legacy_backup.exists():
            legacy_backup.unlink()
            changed = True
    except OSError as exc:
        return {"status": "error", "reason": str(exc), "changed": changed}
    if not changed:
        return {"status": "ok", "changed": False, "restarted": False}
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "zabbix-agent2"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if active.returncode == 0 and (active.stdout or "").strip() == "active":
            restarted = subprocess.run(
                ["systemctl", "restart", "zabbix-agent2"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if restarted.returncode != 0:
                return {
                    "status": "error",
                    "reason": (restarted.stderr or restarted.stdout or "zabbix restart failed").strip(),
                    "changed": True,
                    "restarted": False,
                }
            return {"status": "ok", "changed": True, "restarted": True}
    except OSError as exc:
        return {"status": "error", "reason": str(exc), "changed": True, "restarted": False}
    return {"status": "ok", "changed": True, "restarted": False}
