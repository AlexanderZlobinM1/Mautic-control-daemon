from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any
from dataclasses import dataclass
from urllib import error as urlerror
import urllib.request

from mcd_agent.config import AgentConfig
from mcd_agent.discovery import discover_mautic
from mcd_agent.install_type import detect_install_type
from mcd_agent.maintenance_mode import cron_marker_path, restore_cron_service_if_needed, stop_cron_service
from mcd_agent.models import MauticInstall
from mcd_agent.amazon_mailer_dep import (
    ensure_amazon_mailer_for_bundles,
    ensure_mailer_packages_for_sender_config,
    installed_required_bundles,
    _ensure_node20,
    _resolve_composer_bin,
)
from mcd_agent.fs_permissions import ensure_instance_permissions
from mcd_agent.localphp import parse_local_php


CORE_PLUGIN_BUNDLES = {
    "GrapesJsBuilderBundle",
    "MauticClearbitBundle",
    "MauticCloudStorageBundle",
    "MauticCrmBundle",
    "MauticEmailMarketingBundle",
    "MauticFocusBundle",
    "MauticFullContactBundle",
    "MauticGmailBundle",
    "MauticOutlookBundle",
    "MauticSocialBundle",
    "MauticTagManagerBundle",
    "MauticZapierBundle",
}

FALLBACK_BRANCH_TARGETS: dict[str, str] = {
    "4.4": "4.4.13",
    "5.1": "5.1.1",
    "5.2": "5.2.9",
    "6.0": "6.0.7",
    "7.0": "7.0.0",
    "7.1": "7.1.2",
}

_VERSION_CACHE_REL = Path(".mcd") / "mautic.version"


@dataclass(slots=True)
class UpgradeProbeResult:
    ok: bool
    summary: str
    detail: str = ""


@dataclass(slots=True)
class UpgradeMaintenanceGuard:
    pause_flag: Path
    owned_pause_flag: bool
    owned_cron_stop: bool


def _pick_install_record(config: AgentConfig, root: str | None) -> MauticInstall:
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    if root:
        for inst in installs:
            if inst.root == root:
                return inst
        raise RuntimeError(f"Mautic install not found: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        raise RuntimeError("Multiple installs found, pass --root")
    return installs[0]


def _pick_install(config: AgentConfig, root: str | None) -> tuple[str, str]:
    inst = _pick_install_record(config, root)
    return inst.root, inst.console_path


def _branch_key(version: str) -> str:
    sv = _parse_semver(version)
    if sv == (0, 0, 0):
        return ""
    return f"{sv[0]}.{sv[1]}"


def _write_upgrade_version_cache(root: str, version: str) -> int:
    safe = str(version or "").strip()
    if not safe or safe == "0.0.0":
        return 0
    root_path = Path(root)
    candidates = [root_path]
    if root_path.name.lower() in {"public", "docroot", "public_html"}:
        candidates.append(root_path.parent)
    candidates.append(root_path.parent.parent)
    written = 0
    seen: set[str] = set()
    for base in candidates:
        key = str(base)
        if key in seen or key == "/" or not base.exists() or not base.is_dir():
            continue
        seen.add(key)
        target = base / _VERSION_CACHE_REL
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
            tmp.write_text(safe + "\n", encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
            written += 1
        except OSError:
            continue
    return written


def _scheduler_pause_flag(config: AgentConfig) -> Path:
    raw = str(getattr(config, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause") or "").strip()
    path = Path(raw or "/opt/mcd/var/scheduler.pause")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _enter_upgrade_maintenance(config: AgentConfig) -> UpgradeMaintenanceGuard:
    pause_flag = _scheduler_pause_flag(config)
    owned_pause_flag = not pause_flag.exists()
    if owned_pause_flag:
        pause_flag.write_text("paused\n", encoding="utf-8")
        print(f"Maintenance guard: scheduler paused ({pause_flag})")
    else:
        print(f"Maintenance guard: scheduler already paused ({pause_flag})")

    owned_cron_stop = False
    marker = cron_marker_path(config)
    if marker.exists():
        print(f"Maintenance guard: cron stop marker already present ({marker}); leaving ownership unchanged")
    else:
        cron_stop = stop_cron_service(config)
        if not bool(cron_stop.get("ok", False)):
            if owned_pause_flag and pause_flag.exists():
                pause_flag.unlink()
            message = str(cron_stop.get("message", "") or "").strip() or "cron service stop failed"
            unit = str(cron_stop.get("unit", "") or "").strip() or "-"
            raise RuntimeError(f"Maintenance guard failed: unable to stop cron unit {unit}: {message}")
        owned_cron_stop = True
        unit = str(cron_stop.get("unit", "") or "").strip() or "-"
        was_active = str(bool(cron_stop.get("was_active", False))).lower()
        print(f"Maintenance guard: cron stopped unit={unit} was_active={was_active}")

    return UpgradeMaintenanceGuard(
        pause_flag=pause_flag,
        owned_pause_flag=owned_pause_flag,
        owned_cron_stop=owned_cron_stop,
    )


def _exit_upgrade_maintenance(config: AgentConfig, guard: UpgradeMaintenanceGuard) -> None:
    if guard.owned_cron_stop:
        cron_restore = restore_cron_service_if_needed(config)
        if not bool(cron_restore.get("ok", False)):
            message = str(cron_restore.get("message", "") or "").strip() or "cron service restore failed"
            unit = str(cron_restore.get("unit", "") or "").strip() or "-"
            raise RuntimeError(f"Maintenance guard failed: unable to restore cron unit {unit}: {message}")
        unit = str(cron_restore.get("unit", "") or "").strip() or "-"
        started = str(bool(cron_restore.get("started", False))).lower()
        print(f"Maintenance guard: cron restored unit={unit} started={started}")
    else:
        print("Maintenance guard: cron ownership unchanged")

    if guard.owned_pause_flag:
        if guard.pause_flag.exists():
            guard.pause_flag.unlink()
        print(f"Maintenance guard: scheduler resumed ({guard.pause_flag})")
    else:
        print("Maintenance guard: scheduler pause left unchanged")


def _default_zip_url(version: str) -> str:
    return f"https://github.com/mautic/mautic/releases/download/{version}/{version}-update.zip"


def _release_targets_from_mcc(config: AgentConfig) -> dict[str, dict[str, str]]:
    if not config.mcc_url:
        return {}
    url = config.mcc_url.rstrip("/") + "/api/v1/agent/mautic/releases"
    req = urllib.request.Request(url=url, method="GET")
    if config.mcc_token:
        req.add_header("Authorization", f"Bearer {config.mcc_token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = (resp.read() or b"").decode("utf-8", errors="replace")
        raw = json.loads(body or "{}")
    except (urlerror.URLError, urlerror.HTTPError, TimeoutError, ValueError):
        return {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    branches = raw.get("branches", {})
    if not isinstance(branches, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for branch, row in branches.items():
        if not isinstance(row, dict):
            continue
        bk = str(branch).strip()
        ver = str(row.get("version", "")).strip()
        if not bk or not ver:
            continue
        zip_url = str(row.get("zip_url", "")).strip() or _default_zip_url(ver)
        out[bk] = {"version": ver, "zip_url": zip_url}
    return out


def _release_targets_fallback() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for bk, ver in FALLBACK_BRANCH_TARGETS.items():
        out[bk] = {"version": ver, "zip_url": _default_zip_url(ver)}
    return out


def _release_targets(config: AgentConfig) -> dict[str, dict[str, str]]:
    out = _release_targets_fallback()
    remote = _release_targets_from_mcc(config)
    if remote:
        out.update(remote)
    return out


def _available_targets(config: AgentConfig) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _release_targets(config).values():
        ver = str(row.get("version", "")).strip()
        if not ver:
            continue
        out[ver] = str(row.get("zip_url", "")).strip() or _default_zip_url(ver)
    cache_dir = Path("/opt/mcd/cache/updates")
    if cache_dir.exists():
        for p in cache_dir.glob("*-update.zip"):
            m = re.search(r"(\d+\.\d+\.\d+)-update\.zip$", p.name)
            if not m:
                continue
            v = m.group(1)
            out[v] = str(p)
    return out


def _parse_semver(raw: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _clean_target_version(raw: str | None) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    m = re.match(r"^(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?)$", value)
    if not m:
        raise RuntimeError(f"Invalid Mautic target version: {value}")
    return m.group(1)


def _mautic_console_cmd(cmd: list[str], run_as_user: str | None = "www-data") -> list[str]:
    user = str(run_as_user or "").strip()
    if user and user != "root" and hasattr(os, "geteuid") and os.geteuid() == 0:
        return ["sudo", "-H", "-u", user] + cmd
    return cmd


def _read_current_version(root: str, console_path: str, php_bin: str, run_as_user: str | None = "www-data") -> str:
    cmds = [
        [php_bin, console_path, "--version"],
        [php_bin, console_path, "about", "--no-interaction"],
    ]
    for cmd in cmds:
        try:
            proc = subprocess.run(
                _mautic_console_cmd(cmd, run_as_user),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            continue
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    lock = Path(root) / "composer.lock"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            for pkg in data.get("packages", []):
                if not isinstance(pkg, dict):
                    continue
                if str(pkg.get("name", "")) in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
                    v = str(pkg.get("version", ""))
                    m = re.search(r"(\d+\.\d+\.\d+)", v)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    return "0.0.0"


def _latest_same_branch(config: AgentConfig, version: str) -> str | None:
    targets = _available_targets(config)
    v = _parse_semver(version)
    if v == (0, 0, 0):
        return None
    candidates = [x for x in targets.keys() if _parse_semver(x)[:2] == v[:2]]
    if not candidates:
        return None
    latest = max(candidates, key=lambda x: _parse_semver(x))
    return latest if _parse_semver(latest) > v else None


def _upgrade_target_relation(current: str, target: str, *, allow_minor: bool = False) -> str:
    current_sv = _parse_semver(current)
    target_sv = _parse_semver(target)
    if current_sv == (0, 0, 0) or target_sv == (0, 0, 0):
        return "invalid"
    if target_sv <= current_sv:
        return "none"
    if target_sv[0] != current_sv[0]:
        return "blocked_major"
    if target_sv[1] == current_sv[1]:
        return "allowed"
    if allow_minor and target_sv[1] == current_sv[1] + 1:
        return "allowed"
    return "blocked_minor"


def _ensure_upgrade_target_allowed(current: str, target: str, *, allow_minor: bool = False) -> bool:
    relation = _upgrade_target_relation(current, target, allow_minor=allow_minor)
    if relation == "allowed":
        return True
    if relation == "none":
        return False
    if relation == "blocked_major":
        raise RuntimeError(
            f"Major upgrade is disabled in current flow: {current} -> {target}. "
            "Only patch updates are allowed by default; one-step minor updates require --allow-minor."
        )
    if relation == "blocked_minor":
        raise RuntimeError(
            f"Minor upgrade is disabled in current flow: {current} -> {target}. "
            "Pass --allow-minor for a one-step minor upgrade within the same major."
        )
    raise RuntimeError(f"Invalid Mautic upgrade target: {current} -> {target}")


def _resolve_update_package(config: AgentConfig, target: str) -> Path:
    targets = _available_targets(config)
    src = targets.get(target)
    if not src:
        raise RuntimeError(f"No package source for target {target}")
    p = Path(src)
    if p.exists():
        return p
    # src is URL from static targets
    cache_dir = Path("/opt/mcd/cache/updates")
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{target}-update.zip"
    if dst.exists():
        return dst
    with urllib.request.urlopen(src, timeout=120) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)
    return dst


def _command_with_user(cmd: list[str], *, as_www_data: bool = False) -> list[str]:
    full = cmd
    if as_www_data:
        full = ["sudo", "-u", "www-data"] + cmd
    return full


def _run_capture(cmd: list[str], *, cwd: str, as_www_data: bool = False) -> subprocess.CompletedProcess[str]:
    full = _command_with_user(cmd, as_www_data=as_www_data)
    logging.info("run: %s", " ".join(full))
    return subprocess.run(full, cwd=cwd, text=True, capture_output=True)


def _run(cmd: list[str], *, cwd: str, as_www_data: bool = False) -> None:
    full = _command_with_user(cmd, as_www_data=as_www_data)
    proc = _run_capture(cmd, cwd=cwd, as_www_data=as_www_data)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(full)}\n{proc.stdout}\n{proc.stderr}")


def _remove_tree_with_retry(path: Path, *, strict: bool) -> None:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))

    proc = subprocess.run(["rm", "-rf", "--", str(path)], text=True, capture_output=True, check=False)
    if not path.exists():
        return
    msg = str(last_error or "").strip()
    fallback = (proc.stderr or proc.stdout or "").strip()
    detail = "; ".join(x for x in (msg, fallback) if x)
    if strict:
        raise RuntimeError(f"Unable to remove {path}: {detail or 'directory still exists'}")
    print(f"WARN unable to remove old cache tree {path}: {detail or 'directory still exists'}")


def _hard_clear_prod_cache(root: str) -> None:
    cache_root = Path(root) / "var" / "cache"
    prod_cache = cache_root / "prod"
    if prod_cache.exists():
        old_cache = cache_root / f".prod.mcd-delete-{int(time.time())}-{os.getpid()}"
        try:
            prod_cache.rename(old_cache)
            _remove_tree_with_retry(old_cache, strict=False)
        except OSError:
            _remove_tree_with_retry(prod_cache, strict=True)
    prod_cache.mkdir(parents=True, exist_ok=True)
    try:
        shutil.chown(cache_root, user="www-data", group="www-data")
        shutil.chown(prod_cache, user="www-data", group="www-data")
    except Exception:
        subprocess.run(["chown", "-R", "www-data:www-data", str(cache_root)], check=False)
    print("Composer preflight: cleared var/cache/prod")


def _clear_prod_cache_with_fallback(project_root: str, console_path: str, php_bin: str) -> None:
    proc = _run_capture([php_bin, console_path, "cache:clear"], cwd=project_root, as_www_data=True)
    if proc.returncode == 0:
        print("Composer post-update cache clear ok")
        return
    print("WARN standard cache:clear failed; falling back to hard var/cache/prod clear")
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip())
    _hard_clear_prod_cache(project_root)
    _run([php_bin, console_path, "cache:clear"], cwd=project_root, as_www_data=True)


def _backup_install(root: str) -> Path:
    backups = Path("/opt/mcd/backups")
    backups.mkdir(parents=True, exist_ok=True)
    out = backups / f"mautic-backup-{Path(root).name}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="mcd-backup-") as td:
        tmp = Path(td) / "backup.tar.gz"
        subprocess.run(["tar", "-czf", str(tmp), "-C", str(Path(root).parent), str(Path(root).name)], check=True)
        shutil.move(str(tmp), out)
    return out


def _prepare_plugins_for_major_jump(root: str, from_ver: str, to_ver: str) -> None:
    if from_ver != "4.4.13" or to_ver != "5.1.1":
        return
    plugins_dir = Path(root) / "plugins"
    if not plugins_dir.exists():
        return
    bdir = Path("/opt/mcd/backups/plugins-pre-5.1.1")
    if bdir.exists():
        shutil.rmtree(bdir)
    shutil.copytree(plugins_dir, bdir)
    for d in plugins_dir.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name in CORE_PLUGIN_BUNDLES:
            continue
        shutil.rmtree(d)


def _apply_system_upgrade(from_ver: str, to_ver: str) -> None:
    if from_ver == "4.4.13" and to_ver == "5.1.1":
        subprocess.run(
            [
                "apt",
                "install",
                "php8.2-apcu",
                "php8.2-fpm",
                "php8.2-cli",
                "php8.2-curl",
                "php8.2-mysql",
                "php8.2-mailparse",
                "php8.2-gd",
                "php8.2-mbstring",
                "php8.2-imagick",
                "php8.2-bcmath",
                "php8.2-zip",
                "php8.2-tidy",
                "php8.2-soap",
                "php8.2-intl",
                "php8.2-xml",
                "php8.2-imap",
                "php8.2-redis",
                "php8.2-opcache",
                "-y",
            ],
            check=True,
        )
        _run(["sed", "-i", "s/php8\\.0-fpm/php8.2-fpm/g", "/etc/nginx/sites-enabled/*"], cwd="/")
        _run(["service", "nginx", "restart"], cwd="/")
        subprocess.run(["apt", "purge", "php8.0*", "-y"], check=True)
    if to_ver == "6.0.7":
        subprocess.run(
            [
                "apt",
                "install",
                "php8.3-apcu",
                "php8.3-fpm",
                "php8.3-cli",
                "php8.3-curl",
                "php8.3-mysql",
                "php8.3-mailparse",
                "php8.3-gd",
                "php8.3-mbstring",
                "php8.3-imagick",
                "php8.3-bcmath",
                "php8.3-zip",
                "php8.3-tidy",
                "php8.3-soap",
                "php8.3-intl",
                "php8.3-xml",
                "php8.3-imap",
                "php8.3-redis",
                "php8.3-opcache",
                "-y",
            ],
            check=True,
        )
    if to_ver == "7.0.0":
        subprocess.run(
            [
                "apt",
                "install",
                "php8.4-apcu",
                "php8.4-fpm",
                "php8.4-cli",
                "php8.4-curl",
                "php8.4-mysql",
                "php8.4-mailparse",
                "php8.4-gd",
                "php8.4-mbstring",
                "php8.4-imagick",
                "php8.4-bcmath",
                "php8.4-zip",
                "php8.4-tidy",
                "php8.4-soap",
                "php8.4-intl",
                "php8.4-xml",
                "php8.4-imap",
                "php8.4-redis",
                "php8.4-opcache",
                "-y",
            ],
            check=True,
        )


def _insert_migration_hacks_if_needed(root: str, to_ver: str) -> None:
    if to_ver != "6.0.7":
        return
    local_php = Path(root) / "config" / "local.php"
    if not local_php.exists():
        local_php = Path(root) / "app" / "config" / "local.php"
    if not local_php.exists():
        return
    cfg = parse_local_php(str(local_php))
    prefix = str(cfg.get("db_table_prefix", ""))
    table = f"{prefix}migrations"
    host = str(cfg.get("db_host", "localhost"))
    port = str(cfg.get("db_port", "3306"))
    name = str(cfg.get("db_name", ""))
    user = str(cfg.get("db_user", ""))
    pwd = str(cfg.get("db_password", ""))
    if not (name and user):
        return
    sql = (
        f"INSERT INTO `{table}` (version, executed_at, execution_time) VALUES "
        "(CONCAT('Mautic', CHAR(92), 'Migrations', CHAR(92), 'Version20211020114811'), NOW(), 0),"
        "(CONCAT('Mautic', CHAR(92), 'Migrations', CHAR(92), 'Version20230621074925'), NOW(), 0);"
    )
    cmd = [
        "mysql",
        f"-h{host}",
        f"-P{port}",
        f"-u{user}",
        f"-p{pwd}",
        name,
        "-e",
        sql,
    ]
    subprocess.run(cmd, check=True)


def _apply_zip(config: AgentConfig, root: str, console_path: str, php_bin: str, target: str) -> None:
    pkg = _resolve_update_package(config, target)
    dst = Path(root) / pkg.name
    shutil.copy2(pkg, dst)
    _run([php_bin, console_path, "mautic:update:apply", "--force", f"--update-package={dst.name}"], cwd=root, as_www_data=True)
    _run([php_bin, console_path, "mautic:update:apply", "--finish"], cwd=root, as_www_data=True)


def _replace_version_tokens(text: str, current: str, target: str) -> tuple[str, int]:
    if not current or not target or current == target:
        return text, 0
    count = 0
    updated = text
    # Replace exact semantic version tokens only, keeps unrelated values intact.
    for src, dst in ((f"v{current}", f"v{target}"), (current, target)):
        rx = re.compile(rf"(?<![0-9A-Za-z]){re.escape(src)}(?![0-9A-Za-z])")
        updated, n = rx.subn(dst, updated)
        count += n
    return updated, count


def _resolve_composer_project_root(root: str) -> str:
    p = Path(root)
    candidates = [p, p.parent]
    for c in candidates:
        if (c / "composer.json").exists() and (c / "bin" / "console").exists():
            return str(c)
    return root


def _ensure_www_data_composer_cache() -> None:
    cache_root = Path("/var/www/.cache/composer")
    (cache_root / "vcs").mkdir(parents=True, exist_ok=True)
    for path in (Path("/var/www/.cache"), cache_root, cache_root / "vcs"):
        try:
            os.chown(path, 33, 33)
        except Exception:
            subprocess.run(["chown", "www-data:www-data", str(path)], check=False)


def _doctrine_migrate_command(project_root: str, console_path: str, php_bin: str) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "www-data", php_bin, console_path, "list", "doctrine"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if "doctrine:migrations:migrate" in output:
        return "doctrine:migrations:migrate"
    if "doctrine:migration:migrate" in output:
        return "doctrine:migration:migrate"
    return "doctrine:migrations:migrate"


def _doctrine_version_command(project_root: str, console_path: str, php_bin: str) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "www-data", php_bin, console_path, "list", "doctrine"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if "doctrine:migrations:version" in output:
        return "doctrine:migrations:version"
    if "doctrine:migration:version" in output:
        return "doctrine:migration:version"
    return "doctrine:migrations:version"


def _doctrine_status_command(project_root: str, console_path: str, php_bin: str) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "www-data", php_bin, console_path, "list", "doctrine"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if "doctrine:migrations:status" in output:
        return "doctrine:migrations:status"
    if "doctrine:migration:status" in output:
        return "doctrine:migration:status"
    return "doctrine:migrations:status"


def _doctrine_up_to_date_command(project_root: str, console_path: str, php_bin: str) -> str:
    proc = subprocess.run(
        ["sudo", "-u", "www-data", php_bin, console_path, "list", "doctrine"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if "doctrine:migrations:up-to-date" in output:
        return "doctrine:migrations:up-to-date"
    if "doctrine:migration:up-to-date" in output:
        return "doctrine:migration:up-to-date"
    return "doctrine:migrations:up-to-date"


def _migration_status_count(output: str, label: str) -> int | None:
    m = re.search(rf"\|\s*{re.escape(label)}\s*\|\s*(\d+)\s*\|", output)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _migration_unavailable_count(output: str) -> int:
    return _migration_status_count(output, "Executed Unavailable") or 0


def _migration_new_count(output: str) -> int:
    return _migration_status_count(output, "New") or 0


def _migration_unavailable_versions(output: str) -> list[str]:
    versions: list[str] = []
    for match in re.finditer(r"\((Mautic\\Migrations\\Version[0-9A-Za-z_]+)\)", output):
        version = match.group(1)
        if version not in versions:
            versions.append(version)
    return versions


def _migration_failed_version(output: str) -> str | None:
    patterns = (
        r"Migration\s+(Mautic\\Migrations\\Version[0-9A-Za-z_]+)\s+failed",
        r"(Mautic\\Migrations\\Version[0-9A-Za-z_]+)",
    )
    for pattern in patterns:
        m = re.search(pattern, output)
        if m:
            return m.group(1)
    return None


def _migration_already_in_schema_error(output: str) -> bool:
    needles = (
        "SQLSTATE[42S01]",
        "Base table or view already exists",
        "Table already exists",
        "already exists",
        "SQLSTATE[42S21]",
        "Duplicate column name",
        "Duplicate key name",
        "Can't create table",
    )
    lowered = output.lower()
    return any(x.lower() in lowered for x in needles)


def _run_doctrine_migrate_with_reconcile(
    project_root: str,
    console_path: str,
    php_bin: str,
    migration_cmd: str,
) -> None:
    version_cmd = _doctrine_version_command(project_root, console_path, php_bin)
    marked_versions: set[str] = set()
    for _attempt in range(50):
        proc = _run_capture(
            [php_bin, console_path, migration_cmd, "--no-interaction"],
            cwd=project_root,
            as_www_data=True,
        )
        if proc.returncode == 0:
            return
        output = f"{proc.stdout}\n{proc.stderr}"
        if not _migration_already_in_schema_error(output):
            raise RuntimeError(
                f"Command failed ({proc.returncode}): sudo -u www-data {php_bin} {console_path} {migration_cmd} --no-interaction"
                f"\n{proc.stdout}\n{proc.stderr}"
            )
        version = _migration_failed_version(output)
        if not version:
            raise RuntimeError(
                "Doctrine migration failed with an already-in-schema error, "
                "but MCD could not identify the migration version to mark executed\n"
                + output.strip()
            )
        if version in marked_versions:
            raise RuntimeError(
                "Doctrine migration still fails after marking the same already-in-schema version executed\n"
                + output.strip()
            )
        marked_versions.add(version)
        print(f"Doctrine migrations reconcile: marking {version} as executed after already-in-schema failure")
        _run(
            [php_bin, console_path, version_cmd, "--add", version, "--no-interaction"],
            cwd=project_root,
            as_www_data=True,
        )
    raise RuntimeError("Doctrine migration reconcile exceeded 50 already-in-schema retries")


def _verify_or_reconcile_doctrine_migrations(project_root: str, console_path: str, php_bin: str) -> None:
    up_to_date_cmd = _doctrine_up_to_date_command(project_root, console_path, php_bin)
    status_cmd = _doctrine_status_command(project_root, console_path, php_bin)
    version_cmd = _doctrine_version_command(project_root, console_path, php_bin)

    check = _run_capture([php_bin, console_path, up_to_date_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
    if check.returncode == 0:
        print("Doctrine migrations post-check: up-to-date")
        return

    status = _run_capture([php_bin, console_path, status_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
    status_output = f"{status.stdout}\n{status.stderr}"
    new_count = _migration_new_count(status_output)
    unavailable_count = _migration_unavailable_count(status_output)

    if new_count > 0:
        print(f"Doctrine migrations reconcile: marking {new_count} already-applied available migrations as executed")
        _run(
            [php_bin, console_path, version_cmd, "--add", "--all", "--no-interaction"],
            cwd=project_root,
            as_www_data=True,
        )
        check = _run_capture([php_bin, console_path, up_to_date_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
        if check.returncode == 0:
            print("Doctrine migrations post-check: up-to-date after reconcile")
            return
        status = _run_capture([php_bin, console_path, status_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
        status_output = f"{status.stdout}\n{status.stderr}"
        new_count = _migration_new_count(status_output)
        unavailable_count = _migration_unavailable_count(status_output)

    if new_count == 0 and unavailable_count > 0:
        unavailable_versions = _migration_unavailable_versions(status_output)
        if unavailable_versions:
            print(
                "Doctrine migrations reconcile: removing "
                f"{len(unavailable_versions)} unavailable migration metadata record(s)"
            )
            for version in unavailable_versions:
                _run(
                    [php_bin, console_path, version_cmd, "--delete", version, "--no-interaction"],
                    cwd=project_root,
                    as_www_data=True,
                )
            check = _run_capture([php_bin, console_path, up_to_date_cmd, "--no-interaction"], cwd=project_root, as_www_data=True)
            if check.returncode == 0:
                print("Doctrine migrations post-check: up-to-date after unavailable metadata cleanup")
                return

        print(
            "Doctrine migrations post-check warning: "
            f"{unavailable_count} previously executed migration(s) are no longer registered; no pending migrations remain"
        )
        return

    output = f"{check.stdout}\n{check.stderr}".strip()
    raise RuntimeError("Post-migration up-to-date check failed\n" + output)


def _apply_composer(root: str, console_path: str, php_bin: str, current: str, target: str) -> None:
    project_root = _resolve_composer_project_root(root)
    cjson = Path(project_root) / "composer.json"
    if not cjson.exists():
        raise RuntimeError("composer.json not found")
    composer_bin = _resolve_composer_bin()
    _ensure_node20()
    _ensure_www_data_composer_cache()
    print(f"Composer preflight ok: {composer_bin}")
    node_version = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=20, check=False)
    if node_version.returncode == 0 and (node_version.stdout or "").strip():
        print(f"Node.js preflight ok: {(node_version.stdout or '').strip()}")
    text = cjson.read_text(encoding="utf-8")
    updated, changes = _replace_version_tokens(text, current, target)
    if changes > 0 and updated != text:
        cjson.write_text(updated, encoding="utf-8")
    _run([composer_bin, "update", "--with-dependencies"], cwd=project_root, as_www_data=True)
    _clear_prod_cache_with_fallback(project_root, console_path, php_bin)
    _run([php_bin, console_path, "mautic:update:apply", "--finish"], cwd=project_root, as_www_data=True)
    migration_cmd = _doctrine_migrate_command(project_root, console_path, php_bin)
    _run_doctrine_migrate_with_reconcile(project_root, console_path, php_bin, migration_cmd)
    _verify_or_reconcile_doctrine_migrations(project_root, console_path, php_bin)


def _permissions_check(config: AgentConfig, root: str, *, stage_label: str) -> None:
    user = str(config.mautic_run_as_user or "www-data").strip() or "www-data"
    res = ensure_instance_permissions(
        root=root,
        run_as_user=user,
        guard_paths=list(config.fs_permissions_guard_paths or []),
        fix_console_exec=bool(config.fs_permissions_guard_fix_console_exec),
        console_relpath=str(config.fs_permissions_guard_console_relpath or "bin/console"),
    )
    if res.errors:
        raise RuntimeError(
            "Pre-upgrade permissions check failed: " + "; ".join(str(x) for x in res.errors if str(x).strip())
        )
    repaired = len(res.repaired_paths)
    console_fixed = bool(res.console_exec_fixed)
    logging.info(
        "[%s] %s permissions check: repaired_paths=%s console_exec_fixed=%s missing_paths=%s",
        root,
        stage_label,
        repaired,
        console_fixed,
        len(res.missing_paths),
    )
    print(
        f"Permissions {stage_label}: ok"
        + (f" (repaired_paths={repaired}" + (", console_exec_fixed=1" if console_fixed else "") + ")" if (repaired or console_fixed) else "")
    )


def _pre_upgrade_permissions_check(config: AgentConfig, root: str) -> None:
    _permissions_check(config, root, stage_label="pre-upgrade")


def _active_php_fpm_services() -> list[str]:
    proc = subprocess.run(
        [
            "systemctl",
            "list-units",
            "--type",
            "service",
            "--state",
            "active",
            "--plain",
            "--no-legend",
            "php*-fpm.service",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for raw in (proc.stdout or "").splitlines():
        name = raw.strip().split(None, 1)[0].strip()
        if name.endswith(".service"):
            out.append(name)
    return out


def _curl_probe(url: str, *, resolve_local_host: str | None = None, follow_redirects: bool = True, timeout_sec: int = 20) -> UpgradeProbeResult:
    cmd = ["curl", "-ksS", "--max-time", str(max(5, int(timeout_sec)))]
    if follow_redirects:
        cmd.append("-L")
    if resolve_local_host:
        cmd.extend(["--resolve", f"{resolve_local_host}:443:127.0.0.1"])
    cmd.extend(["-o", "/dev/null", "-D", "-", url])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    body = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = body or f"curl rc={proc.returncode}"
        return UpgradeProbeResult(False, f"curl failed for {url}", detail)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    status_line = next((ln for ln in lines if ln.startswith("HTTP/")), "")
    if not status_line:
        return UpgradeProbeResult(False, f"probe returned no HTTP status for {url}", body[:600])
    m = re.search(r"^HTTP/\S+\s+(\d{3})\b", status_line)
    if not m:
        return UpgradeProbeResult(False, f"probe returned malformed HTTP status for {url}", status_line)
    code = int(m.group(1))
    if 200 <= code < 400:
        return UpgradeProbeResult(True, f"{url} -> HTTP {code}", status_line)
    return UpgradeProbeResult(False, f"{url} -> HTTP {code}", body[:600])


def _verify_nginx_and_php_services() -> None:
    proc = subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True)
    nginx_state = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        print(f"Post-check: nginx inactive ({nginx_state or 'unknown'}), attempting start")
        start = subprocess.run(["systemctl", "start", "nginx"], capture_output=True, text=True)
        if start.returncode != 0:
            raise RuntimeError(f"Post-check failed: cannot start nginx: {(start.stderr or start.stdout or '').strip()}")
        proc = subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True)
        nginx_state = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"Post-check failed: nginx still inactive after start: {nginx_state or 'unknown'}")
        print("Post-check: nginx auto-start OK")
    else:
        print("Post-check: nginx service OK")

    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        raise RuntimeError(f"Post-check failed: nginx -t: {(test.stderr or test.stdout or '').strip()}")
    print("Post-check: nginx -t OK")

    php_services = _active_php_fpm_services()
    if not php_services:
        raise RuntimeError("Post-check failed: no active php-fpm service found")
    print("Post-check: php-fpm active: " + ", ".join(php_services))


def _best_probe_domain(inst: MauticInstall) -> str:
    candidates: list[str] = []
    if str(inst.primary_domain or "").strip():
        candidates.append(str(inst.primary_domain or "").strip())
    for value in list(inst.domains or []):
        domain = str(value or "").strip()
        if domain and domain not in candidates:
            candidates.append(domain)
    return candidates[0] if candidates else ""


def _post_upgrade_verify(config: AgentConfig, inst: MauticInstall) -> None:
    _permissions_check(config, inst.root, stage_label="post-upgrade")
    _verify_nginx_and_php_services()
    domain = _best_probe_domain(inst)
    if not domain:
        print("Post-check: no instance domain detected, skipping local/external HTTP probes")
        return

    local = _curl_probe(f"https://{domain}/", resolve_local_host=domain, follow_redirects=True, timeout_sec=20)
    if not local.ok:
        raise RuntimeError(f"Post-check failed: local origin probe: {local.summary}; {local.detail}".strip())
    print("Post-check: local origin OK: " + local.summary)

    external = _curl_probe(f"https://{domain}/", follow_redirects=True, timeout_sec=25)
    if not external.ok:
        raise RuntimeError(f"Post-check failed: external probe: {external.summary}; {external.detail}".strip())
    print("Post-check: external HTTPS OK: " + external.summary)


def run_upgrade_check(config: AgentConfig, root: str | None) -> int:
    install_root, console = _pick_install(config, root)
    current = _read_current_version(install_root, console, config.php_bin, config.mautic_run_as_user)
    target = _latest_same_branch(config, current)
    branch = _branch_key(current)
    print(f"root={install_root}")
    print(f"current={current}")
    if branch:
        print(f"branch={branch}.x")
    if target is None:
        print("next=none")
    else:
        print(f"next={target}")
    return 0


def run_upgrade_apply(
    *,
    config: AgentConfig,
    root: str | None,
    mode: str,
    yes: bool,
    do_backup: bool,
    with_system_upgrade: bool,
    target_override: str | None = None,
    allow_minor: bool = False,
) -> int:
    inst = _pick_install_record(config, root)
    install_root, console = inst.root, inst.console_path
    current = _read_current_version(install_root, console, config.php_bin, config.mautic_run_as_user)
    target = _clean_target_version(target_override) or _latest_same_branch(config, current)
    if not target:
        print(f"No upgrade target for current version {current}")
        return 0
    if not _ensure_upgrade_target_allowed(current, target, allow_minor=allow_minor):
        print(f"No upgrade target for current version {current}")
        return 0

    print(f"Upgrade plan: {current} -> {target} (mode={mode})")
    if not yes:
        ans = input("Proceed? [y/N]: ").strip().lower()
        if ans not in {"y", "yes"}:
            print("Cancelled")
            return 0

    guard = _enter_upgrade_maintenance(config)
    try:
        # Mandatory preflight: align permissions before any upgrade action.
        _pre_upgrade_permissions_check(config, install_root)

        if do_backup:
            b = _backup_install(install_root)
            print(f"Backup created: {b}")

        chosen_mode = mode
        if chosen_mode == "auto":
            chosen_mode = detect_install_type(install_root)

        if chosen_mode == "zip":
            _apply_zip(config, install_root, console, config.php_bin, target)
        elif chosen_mode == "composer":
            _apply_composer(install_root, console, config.php_bin, current, target)
        else:
            raise RuntimeError(f"Unsupported mode: {mode}")

        if with_system_upgrade:
            _apply_system_upgrade(current, target)

        # Restore transport dependencies for API senders after upgrade
        # (especially relevant for zip installs where update flow may drop composer deps).
        ensure_mailer_packages_for_sender_config(
            config=config,
            root=install_root,
            console_path=console,
            reason="mautic-upgrade-sender-restore",
        )

        ensure_amazon_mailer_for_bundles(
            config=config,
            root=install_root,
            console_path=console,
            bundles=installed_required_bundles(install_root),
            reason="mautic-upgrade",
        )

        _post_upgrade_verify(config, inst)

        final_version = _read_current_version(install_root, console, config.php_bin, config.mautic_run_as_user)
        if _parse_semver(final_version) != _parse_semver(target):
            raise RuntimeError(f"Post-check failed: Mautic version is {final_version}, expected {target}")
        cache_count = _write_upgrade_version_cache(install_root, final_version)
        print(f"Mautic version cache refreshed: {final_version} ({cache_count} path(s))")
        print(f"Upgrade completed: {current} -> {final_version}")
    except Exception:
        try:
            _exit_upgrade_maintenance(config, guard)
        except Exception as cleanup_error:
            print(f"WARN maintenance cleanup failed after upgrade error: {cleanup_error}")
        raise

    _exit_upgrade_maintenance(config, guard)
    return 0


def run_upgrade_interactive(config: AgentConfig, root: str | None) -> int:
    install_root, console = _pick_install(config, root)
    current = _read_current_version(install_root, console, config.php_bin, config.mautic_run_as_user)
    target_next = _latest_same_branch(config, current)
    branch = _branch_key(current)
    print(f"root={install_root}")
    print(f"current={current}")
    if branch:
        print(f"branch={branch}.x")
    if not target_next:
        print("next=none")
        return 0
    print(f"latest_patch={target_next}")
    print("")
    print("Select target:")
    print("1. latest patch in current branch (recommended)")
    print("0. exit")
    t_choice = _ask("Target [1/0, default 1]: ").strip() or "1"
    if t_choice == "0":
        print("Cancelled")
        return 0
    target = target_next
    if not target:
        print("No valid target selected")
        return 0
    print("")
    print("Select upgrade mode:")
    print("1. auto (recommended)")
    print("2. zip")
    print("3. composer")
    print("0. exit")
    choice = _ask("Mode [1/2/3/0, default 1]: ").strip() or "1"
    if choice == "0":
        print("Cancelled")
        return 0
    mode_map = {"1": "auto", "2": "zip", "3": "composer"}
    mode = mode_map.get(choice, "auto")
    print("Backup note: MCD backup archives only the Mautic install directory.")
    backup = _ask("Create backup before upgrade? [Y/n]: ").strip().lower() not in {"n", "no"}
    sys_up = False
    print("")
    print(f"Plan: {current} -> {target} (mode={mode}, backup={backup}, with_system_upgrade=false)")
    confirm = _ask("Apply now? [y/N, 0=exit]: ").strip().lower()
    if confirm in {"0", "x", "exit", "q"}:
        print("Cancelled")
        return 0
    if confirm not in {"y", "yes"}:
        print("Cancelled")
        return 0
    return run_upgrade_apply(
        config=config,
        root=install_root,
        mode=mode,
        yes=True,
        do_backup=backup,
        with_system_upgrade=sys_up,
        target_override=target,
    )


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""
