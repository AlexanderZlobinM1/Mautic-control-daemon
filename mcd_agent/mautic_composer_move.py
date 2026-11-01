from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import re
import shutil
import tarfile
import tempfile
from typing import Any

from mcd_agent.config import AgentConfig
from mcd_agent.mautic_image_install import _artifact_url, _download, _run, _safe_extract
from mcd_agent.mode import _read_crontab, _write_crontab
from mcd_agent.nginx_external import write_switched_vhost as _external_write_switched_vhost

_write_switched_vhost = _external_write_switched_vhost

RETIRED_INSTANCE_MARKER = ".mcd-retired-after-composer-move"
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
COMPOSER_MOVE_CRON_MARKER = "MCD_COMPOSER_MOVE"
_MAX_CRON_WRAPPER_BYTES = 128 * 1024
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w./-])(/[A-Za-z0-9_@%+=:,./-]+)")


@dataclass
class ComposerMovePlan:
    source_root: Path
    target_root: Path
    nginx_root: Path
    domain: str
    image_ref: str
    php_version: str
    site_available: Path
    site_enabled: Path | None


def _domain(raw: str) -> str:
    domain = str(raw or "").strip().lower().rstrip(".")
    if not re.match(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", domain):
        raise RuntimeError(f"invalid domain: {raw}")
    return domain


def _short(raw: str) -> str:
    first = _domain(raw).split(".", 1)[0]
    value = re.sub(r"[^a-z0-9_]+", "_", first).strip("_")
    if not value:
        raise RuntimeError("empty instance short name")
    return value[:48].rstrip("_")


def _image_ref_for_major(major: int) -> str:
    if int(major or 0) == 6:
        return "composer6-skeleton"
    if int(major or 0) == 7:
        return "composer7-skeleton"
    raise RuntimeError(f"composer skeleton is not configured for Mautic major {major}")


def _php_version_for_major(major: int) -> str:
    return "8.4" if int(major or 0) >= 7 else "8.3"


def _target_root(domain: str) -> Path:
    short = _short(domain)
    base = Path("/var/www") / short / "public_html"
    if not base.exists() and not base.parent.exists():
        return base
    prefixed = Path("/var/www") / f"composer-{short}" / "public_html"
    if not prefixed.exists() and not prefixed.parent.exists():
        return prefixed
    for idx in range(2, 100):
        candidate = Path("/var/www") / f"composer-{short}-{idx}" / "public_html"
        if not candidate.exists() and not candidate.parent.exists():
            return candidate
    raise RuntimeError(f"no free target root found for {domain}")


def _find_site(domain: str, source_root: Path) -> tuple[Path, Path | None]:
    candidates: list[Path] = []
    for root in (NGINX_SITES_ENABLED, NGINX_SITES_AVAILABLE):
        if not root.exists():
            continue
        for path in sorted(root.iterdir()):
            try:
                real = path.resolve()
                text = real.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if domain in text and str(source_root) in text:
                candidates.append(real)
            elif domain in text and not candidates:
                candidates.append(real)
    if not candidates:
        raise RuntimeError(f"nginx vhost for {domain} was not found")
    site_available = candidates[0]
    enabled = NGINX_SITES_ENABLED / site_available.name
    return site_available, enabled if enabled.exists() or enabled.is_symlink() else None


def _copy_tree(src: Path, dst: Path, *, ignore: set[str] | None = None) -> None:
    if not src.exists():
        return
    ignore = ignore or set()
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*sorted(ignore)) if ignore else None)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _mutable_path_map(source_root: Path, target_root: Path) -> dict[Path, Path]:
    return {
        source_root / "plugins": target_root / "docroot" / "plugins",
        source_root / "media": target_root / "docroot" / "media",
        source_root / "themes": target_root / "docroot" / "themes",
        source_root / "translations": target_root / "docroot" / "translations",
        source_root / "templates": target_root / "docroot" / "templates",
    }


def _copy_mutable_state(source_root: Path, target_root: Path) -> list[str]:
    copied: list[str] = []
    local_php = source_root / "config" / "local.php"
    if not local_php.exists():
        local_php = source_root / "app" / "config" / "local.php"
    if not local_php.exists():
        raise RuntimeError("source local.php was not found")
    dst_local = target_root / "config" / "local.php"
    dst_local.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_php, dst_local)
    copied.append("config/local.php")

    for src, dst in _mutable_path_map(source_root, target_root).items():
        if src.exists():
            _copy_tree(src, dst)
            copied.append(str(src.relative_to(source_root)))

    src_var = source_root / "var"
    if src_var.exists():
        _copy_tree(src_var, target_root / "var", ignore={"cache", "logs", "tmp", "sessions"})
        copied.append("var")

    for rel in (".env.local", ".env"):
        src = source_root / rel
        if src.exists():
            _copy_tree(src, target_root / rel)
            copied.append(rel)

    return copied


def _patch_paths_in_local_php(target_root: Path, source_root: Path) -> bool:
    path = target_root / "config" / "local.php"
    text = path.read_text(encoding="utf-8", errors="replace")
    patched = text
    for src, dst in sorted(_mutable_path_map(source_root, target_root).items(), key=lambda item: len(str(item[0])), reverse=True):
        patched = patched.replace(str(src), str(dst))
    patched = patched.replace(str(source_root), str(target_root))
    composer_paths = {
        "upload_dir": target_root / "docroot" / "media" / "files",
        "form_upload_dir": target_root / "docroot" / "media" / "files" / "form",
        "contact_export_dir": target_root / "docroot" / "media" / "files" / "temp",
        "report_temp_dir": target_root / "docroot" / "media" / "files" / "temp",
        "tmp_path": target_root / "var" / "tmp",
        "cache_path": target_root / "var" / "cache",
        "log_path": target_root / "var" / "logs",
    }
    for key, value in composer_paths.items():
        patched = _set_local_php_path(patched, key, str(value))
    if patched != text:
        path.write_text(patched, encoding="utf-8")
        return True
    return False


def _set_local_php_path(text: str, key: str, value: str) -> str:
    quoted = value.replace("\\", "\\\\").replace("'", "\\'")
    existing = re.compile(r"((?:'|\")" + re.escape(key) + r"(?:'|\")\s*=>\s*)(?:'|\")[^'\"]*(?:'|\")")
    replaced, count = existing.subn(r"\1'" + quoted + "'", text, count=1)
    if count:
        return replaced
    entry = f"\t'{key}' => '{quoted}',\n"
    for closing in (r"\n\s*\);", r"\n\s*\];"):
        match = list(re.finditer(closing, text))
        if match:
            pos = match[-1].start() + 1
            return text[:pos] + entry + text[pos:]
    return text


def _ensure_runtime_dirs(target_root: Path) -> list[str]:
    runtime_dirs = [
        target_root / "var" / "cache",
        target_root / "var" / "logs",
        target_root / "var" / "tmp",
        target_root / "docroot" / "media" / "files",
        target_root / "docroot" / "media" / "files" / "form",
        target_root / "docroot" / "media" / "files" / "temp",
    ]
    created: list[str] = []
    for path in runtime_dirs:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o775)
        created.append(str(path.relative_to(target_root)))
    return created




def _mark_source_root_retired(plan: ComposerMovePlan) -> str:
    marker = plan.source_root / RETIRED_INSTANCE_MARKER
    payload = {
        "reason": "composer_move_completed",
        "domain": plan.domain,
        "target_root": str(plan.target_root),
        "nginx_root": str(plan.nginx_root),
        "created_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(marker)


def _rewrite_composer_move_crontab(
    content: str,
    *,
    source_root: Path,
    target_root: Path,
    mautic_major: int,
) -> tuple[str, int, int]:
    source = str(source_root)
    target = str(target_root)
    rewritten = 0
    retired = 0
    output: list[str] = []
    for raw in content.splitlines():
        line = raw.replace(source, target)
        if line != raw:
            rewritten += 1
        stripped = line.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and int(mautic_major or 0) >= 5
            and "mautic:emails:send" in stripped
        ):
            output.append(
                f"# {COMPOSER_MOVE_CRON_MARKER}: disabled because mautic:emails:send is unavailable in Mautic 5+"
            )
            output.append("# " + line)
            retired += 1
            continue
        output.append(line)
    suffix = "\n" if content.endswith("\n") else ""
    return "\n".join(output) + suffix, rewritten, retired


def _active_cron_references_root(content: str, root: Path) -> bool:
    token = str(root)
    return any(
        token in line and bool(line.strip()) and not line.lstrip().startswith("#")
        for line in content.splitlines()
    )


def _active_cron_wrapper_paths(content: str, source_root: Path) -> list[Path]:
    source = str(source_root).encode()
    paths: set[Path] = set()
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        for match in _ABSOLUTE_PATH_RE.finditer(line):
            path = Path(match.group(1).rstrip(";|&)"))
            try:
                stat = path.stat()
                payload = path.read_bytes()
            except OSError:
                continue
            if not path.is_file() or stat.st_size <= 0 or stat.st_size > _MAX_CRON_WRAPPER_BYTES:
                continue
            if source in payload and b"bin/console" in payload:
                paths.add(path)
    return sorted(paths)


def _rewrite_composer_move_cron_wrapper(
    path: Path,
    *,
    source_root: Path,
    target_root: Path,
    mautic_major: int,
) -> tuple[bytes, int, int]:
    original = path.read_bytes()
    text = original.decode("utf-8")
    updated, rewritten, retired = _rewrite_composer_move_crontab(
        text,
        source_root=source_root,
        target_root=target_root,
        mautic_major=mautic_major,
    )
    if updated != text:
        path.write_bytes(updated.encode("utf-8"))
    return original, rewritten, retired


def _migrate_composer_move_crontabs(plan: ComposerMovePlan, mautic_major: int) -> dict[str, Any]:
    snapshots: dict[str, str] = {}
    wrapper_snapshots: dict[str, bytes] = {}
    results: dict[str, dict[str, int]] = {}
    wrapper_results: dict[str, dict[str, int]] = {}
    written: list[str] = []
    try:
        for user in ("root", "www-data"):
            cron_user = None if user == "root" else user
            rc, current = _read_crontab(cron_user)
            if rc != 0:
                continue
            snapshots[user] = current
            for wrapper in _active_cron_wrapper_paths(current, plan.source_root):
                key = str(wrapper)
                if key in wrapper_snapshots:
                    continue
                original, wrapper_rewritten, wrapper_retired = _rewrite_composer_move_cron_wrapper(
                    wrapper,
                    source_root=plan.source_root,
                    target_root=plan.target_root,
                    mautic_major=mautic_major,
                )
                wrapper_snapshots[key] = original
                wrapper_results[key] = {"rewritten": wrapper_rewritten, "retired": wrapper_retired}
            updated, rewritten, retired = _rewrite_composer_move_crontab(
                current,
                source_root=plan.source_root,
                target_root=plan.target_root,
                mautic_major=mautic_major,
            )
            results[user] = {"rewritten": rewritten, "retired": retired}
            if updated != current:
                write_rc, detail = _write_crontab(updated, cron_user)
                if write_rc != 0:
                    raise RuntimeError(f"failed to migrate {user} crontab: {detail}")
                written.append(user)
            verify_rc, verified = _read_crontab(cron_user)
            if verify_rc != 0:
                raise RuntimeError(f"failed to verify {user} crontab after Composer migration")
            if _active_cron_references_root(verified, plan.source_root):
                raise RuntimeError(f"active {user} crontab still references retired root {plan.source_root}")
            if _active_cron_wrapper_paths(verified, plan.source_root):
                raise RuntimeError(f"active {user} cron wrapper still references retired root {plan.source_root}")
    except Exception:
        for user in written:
            cron_user = None if user == "root" else user
            _write_crontab(snapshots[user], cron_user)
        for path, content in wrapper_snapshots.items():
            Path(path).write_bytes(content)
        raise
    return {
        "users": results,
        "wrappers": wrapper_results,
        "snapshots": snapshots,
        "wrapper_snapshots": wrapper_snapshots,
    }


def _restore_composer_move_crontabs(snapshot: dict[str, Any]) -> None:
    rows = snapshot.get("snapshots") if isinstance(snapshot, dict) else {}
    if not isinstance(rows, dict):
        return
    failures: list[str] = []
    for user, content in rows.items():
        cron_user = None if user == "root" else str(user)
        rc, detail = _write_crontab(str(content), cron_user)
        if rc != 0:
            failures.append(f"{user}: {detail}")
    wrapper_rows = snapshot.get("wrapper_snapshots") if isinstance(snapshot, dict) else {}
    if isinstance(wrapper_rows, dict):
        for path, content in wrapper_rows.items():
            try:
                Path(str(path)).write_bytes(bytes(content))
            except OSError as exc:
                failures.append(f"{path}: {exc}")
    if failures:
        raise RuntimeError("failed to restore crontab after Composer migration error: " + "; ".join(failures))


def _preflight(plan: ComposerMovePlan) -> list[str]:
    problems: list[str] = []
    if os.geteuid() != 0:
        problems.append("must run as root")
    if not plan.source_root.exists():
        problems.append(f"source root does not exist: {plan.source_root}")
    if plan.target_root.exists() or plan.target_root.parent.exists():
        problems.append(f"target root already exists: {plan.target_root}")
    if not Path(f"/run/php/php{plan.php_version}-fpm.sock").exists():
        problems.append(f"missing PHP-FPM socket: /run/php/php{plan.php_version}-fpm.sock")
    for cmd in ("tar", "nginx"):
        if not shutil.which(cmd):
            problems.append(f"missing command: {cmd}")
    if not str(plan.image_ref or "").strip():
        problems.append("image_ref is required")
    return problems


def move_zip_to_composer(
    cfg: AgentConfig,
    *,
    root: str,
    domain: str,
    mautic_major: int,
    yes: bool = False,
) -> dict[str, Any]:
    if not yes:
        raise RuntimeError("--yes is required")
    if not str(cfg.mcc_token or "").strip():
        raise RuntimeError("mcc.token is not configured")
    source_root = Path(root).resolve()
    d = _domain(domain)
    image_ref = _image_ref_for_major(int(mautic_major or 0))
    php_version = _php_version_for_major(int(mautic_major or 0))
    target_root = _target_root(d)
    site_available, site_enabled = _find_site(d, source_root)
    plan = ComposerMovePlan(
        source_root=source_root,
        target_root=target_root,
        nginx_root=target_root / "docroot",
        domain=d,
        image_ref=image_ref,
        php_version=php_version,
        site_available=site_available,
        site_enabled=site_enabled,
    )
    print(f"Preflight: {d} {source_root} -> {target_root} via {image_ref}")
    problems = _preflight(plan)
    if problems:
        for p in problems:
            print(f"preflight_error: {p}")
        raise RuntimeError("preflight failed")

    with tempfile.TemporaryDirectory(prefix="mcd-composer-move-") as td:
        tmp = Path(td)
        files_tgz = tmp / "files.tar.gz"
        print("Downloading composer skeleton")
        _download(_artifact_url(cfg, image_ref, "files"), str(cfg.mcc_token), files_tgz)
        print("Extracting composer skeleton")
        plan.target_root.parent.mkdir(parents=True, exist_ok=False)
        plan.target_root.mkdir(parents=True, exist_ok=False)
        cron_migration: dict[str, Any] = {}
        try:
            with tarfile.open(files_tgz, "r:gz") as tf:
                _safe_extract(tf, plan.target_root)
            print("Copying mutable state")
            copied = _copy_mutable_state(plan.source_root, plan.target_root)
            path_patched = _patch_paths_in_local_php(plan.target_root, plan.source_root)
            print("Fixing permissions")
            _run(["chown", "-R", "www-data:www-data", str(plan.target_root.parent)], timeout_sec=600)
            for rel in ("var/cache", "var/logs", "var/tmp", "docroot/var/cache", "docroot/var/logs"):
                shutil.rmtree(plan.target_root / rel, ignore_errors=True)
            runtime_dirs = _ensure_runtime_dirs(plan.target_root)
            _run(["chown", "-R", "www-data:www-data", str(plan.target_root / "var"), str(plan.target_root / "docroot" / "media" / "files")], timeout_sec=300)
            print("Migrating crontabs to Composer root")
            cron_migration = _migrate_composer_move_crontabs(plan, int(mautic_major or 0))
            print("Switching nginx vhost")
            vhost = _external_write_switched_vhost(plan, cfg=cfg)
            retired_marker = _mark_source_root_retired(plan)
        except Exception:
            if cron_migration:
                _restore_composer_move_crontabs(cron_migration)
            shutil.rmtree(plan.target_root.parent, ignore_errors=True)
            raise

    from mcd_agent.inventory import InstanceInventory

    inv = InstanceInventory(cfg.state_db_path)
    count = inv.rescan(cfg)
    print(f"Rescan complete: {count} instances")
    return {
        "status": "ok",
        "domain": plan.domain,
        "source_root": str(plan.source_root),
        "target_root": str(plan.target_root),
        "nginx_root": str(plan.nginx_root),
        "image_ref": plan.image_ref,
        "php_version": plan.php_version,
        "copied": copied,
        "local_php_path_patched": path_patched,
        "runtime_dirs": runtime_dirs,
        "cron_migration": cron_migration.get("users", {}),
        "cron_wrapper_migration": cron_migration.get("wrappers", {}),
        "source_retired_marker": retired_marker,
        "vhost": vhost,
        "instances": count,
    }
