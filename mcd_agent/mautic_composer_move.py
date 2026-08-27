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
from mcd_agent.nginx_baseline import (
    _nginx_supports_http2_directive,
    ensure_mautic_public_app_asset_locations,
    normalize_legacy_http2_listen,
)

RETIRED_INSTANCE_MARKER = ".mcd-retired-after-composer-move"
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")


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


def _write_switched_vhost(plan: ComposerMovePlan) -> dict[str, str]:
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    source_site = plan.site_available
    target_site = plan.site_available
    enabled_site = plan.site_enabled
    source_parent = source_site.parent.resolve(strict=False)
    enabled_dir = NGINX_SITES_ENABLED.resolve(strict=False)
    available_dir = NGINX_SITES_AVAILABLE.resolve(strict=False)
    managed_nginx_site = source_parent in {enabled_dir, available_dir}
    if source_parent == enabled_dir:
        target_site = NGINX_SITES_AVAILABLE / source_site.name
        enabled_site = source_site

    backup_dir = NGINX_SITES_AVAILABLE if managed_nginx_site else target_site.parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"zip-backup-{ts}-{target_site.name}"
    shutil.copy2(source_site, backup)
    if target_site.exists() and target_site.resolve(strict=False) != source_site.resolve(strict=False):
        existing_backup = backup_dir / f"pre-composer-{ts}-{target_site.name}"
        shutil.copy2(target_site, existing_backup)

    text = source_site.read_text(encoding="utf-8", errors="replace")
    old = str(plan.source_root)
    new = str(plan.nginx_root)
    if old not in text:
        root_re = re.compile(r"(^\s*root\s+)([^;]+)(;)", re.MULTILINE)
        text, count = root_re.subn(lambda m: m.group(1) + new + m.group(3), text, count=1)
        if count <= 0:
            raise RuntimeError(f"no nginx root directive found in {plan.site_available}")
    else:
        text = text.replace(old, new)
    target_sock = f"unix:/var/run/php/php{plan.php_version}-fpm.sock"
    text = re.sub(
        r"unix:/(?:var/)?run/php/php[0-9]+\.[0-9]+-fpm\.sock",
        target_sock,
        text,
    )
    text = ensure_mautic_public_app_asset_locations(text)
    text = normalize_legacy_http2_listen(text, modern_http2=_nginx_supports_http2_directive())
    target_site.parent.mkdir(parents=True, exist_ok=True)
    target_site.write_text(text, encoding="utf-8")
    if enabled_site is not None:
        enabled_site.parent.mkdir(parents=True, exist_ok=True)
        if enabled_site.exists() or enabled_site.is_symlink():
            same_target = enabled_site.is_symlink() and enabled_site.resolve(strict=False) == target_site.resolve(strict=False)
            if not same_target:
                enabled_site.unlink()
        if not enabled_site.exists() and not enabled_site.is_symlink():
            enabled_site.symlink_to(os.path.relpath(target_site, enabled_site.parent))
    return {"active": str(target_site), "backup": str(backup)}


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
            print("Switching nginx vhost")
            vhost = _write_switched_vhost(plan)
            rc, out = _run(["nginx", "-t"], timeout_sec=30)
            if rc != 0:
                raise RuntimeError("nginx -t failed: " + out)
            _run(["systemctl", "reload", "nginx"], timeout_sec=30)
            retired_marker = _mark_source_root_retired(plan)
        except Exception:
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
        "source_retired_marker": retired_marker,
        "vhost": vhost,
        "instances": count,
    }
