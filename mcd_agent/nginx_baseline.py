from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any


NGINX_CONF = Path("/etc/nginx/nginx.conf")
SITES_AVAILABLE = Path("/etc/nginx/sites-available")
SITES_ENABLED = Path("/etc/nginx/sites-enabled")
CONF_D = Path("/etc/nginx/conf.d")
SNIPPETS_DIR = Path("/etc/nginx/snippets")
HARDENING_SNIPPET = SNIPPETS_DIR / "mcd-mautic-hardening.conf"
SECURITY_HEADERS_SNIPPET = SNIPPETS_DIR / "security-headers.conf"
HARDENING_INCLUDE = "include /etc/nginx/snippets/mcd-mautic-hardening.conf;"
BACKUP_ROOT = Path("/var/backups/mcd-nginx-baseline")
SECURITY_HEADERS_START = "# mcd-security-headers-extra start"
SECURITY_HEADERS_END = "# mcd-security-headers-extra end"


@dataclass
class _Snapshot:
    path: Path
    kind: str
    backup: Path | None = None
    target: str | None = None


def _nginx_present() -> bool:
    return bool(shutil.which("nginx") or Path("/usr/sbin/nginx").exists() or NGINX_CONF.exists())


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _has_www_data_user(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^user\s+www-data\s*;", line):
            return True
        if re.match(r"^user\s+\S+\s*;", line):
            return False
    return False


def _has_sites_enabled_include(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^include\s+/etc/nginx/sites-enabled/[^;]*;", line):
            return True
    return False


def _conf_name(name: str) -> str:
    return name if name.endswith(".conf") else f"{name}.conf"


def _sites_enabled_has_regular_files() -> bool:
    if not SITES_ENABLED.exists():
        return False
    try:
        for path in SITES_ENABLED.iterdir():
            if path.name.startswith("."):
                continue
            if path.is_symlink():
                continue
            if path.is_file():
                return True
    except Exception:
        return True
    return False


def _sites_enabled_has_non_conf_entries() -> bool:
    if not SITES_ENABLED.exists():
        return False
    try:
        for path in SITES_ENABLED.iterdir():
            if path.name.startswith("."):
                continue
            if not path.name.endswith(".conf"):
                return True
    except Exception:
        return True
    return False


def nginx_baseline_satisfied() -> bool:
    """Cheap read-only check for the SalesSnap nginx layout baseline."""
    if not _nginx_present():
        return True
    if NGINX_CONF.exists():
        text = _read_text(NGINX_CONF)
        if not _has_www_data_user(text):
            return False
        if not _has_sites_enabled_include(text):
            return False
    if _sites_enabled_has_regular_files():
        return False
    if _sites_enabled_has_non_conf_entries():
        return False
    if _read_text(HARDENING_SNIPPET) != _desired_hardening_snippet():
        return False
    if SECURITY_HEADERS_SNIPPET.exists() and SECURITY_HEADERS_START not in _read_text(SECURITY_HEADERS_SNIPPET):
        return False
    for path in _active_server_config_files():
        if HARDENING_INCLUDE not in _read_text(path):
            return False
    return True


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


def _desired_nginx_conf(text: str) -> tuple[str, list[str]]:
    actions: list[str] = []
    lines = text.splitlines()
    out: list[str] = []
    user_seen = False
    user_changed = False
    for raw in lines:
        line = raw.strip()
        if not line.startswith("#") and re.match(r"^user\s+\S+\s*;", line):
            if not user_seen:
                indent = raw[: len(raw) - len(raw.lstrip())]
                desired = f"{indent}user www-data;"
                out.append(desired)
                user_seen = True
                if raw != desired:
                    user_changed = True
            else:
                user_changed = True
            continue
        out.append(raw)
    if not user_seen:
        out.insert(0, "user www-data;")
        user_changed = True
    if user_changed:
        actions.append("user_www_data")

    text_after_user = "\n".join(out)
    if _has_sites_enabled_include(text_after_user):
        return text_after_user.rstrip("\n") + "\n", actions

    inserted = False
    final: list[str] = []
    for raw in out:
        final.append(raw)
        if not inserted and re.match(r"^\s*http\s*\{", raw):
            indent = raw[: len(raw) - len(raw.lstrip())] + "    "
            final.append(f"{indent}include /etc/nginx/sites-enabled/*.conf;")
            inserted = True
    if inserted:
        actions.append("sites_enabled_include")
    return "\n".join(final).rstrip("\n") + "\n", actions


def _desired_hardening_snippet() -> str:
    return """# Managed by MCD. Deny project internals for zip/root and composer/docroot Mautic installs.
autoindex off;

# Never expose Mautic/runtime internals even when nginx root points at project root.
location ~* ^/(?:config|vendor|node_modules|tests|var|\\.git)(?:/|$) {
    return 403;
}

# Deny dependency, build, test and policy files that reveal implementation details.
location ~* ^/(?:composer\\.(?:json|lock)|package(?:-lock)?\\.json|yarn\\.lock|pnpm-lock\\.yaml|symfony\\.lock|webpack\\.config\\.js|tsconfig\\.json|phpunit\\.xml(?:\\.dist)?|codeception\\.yml|SECURITY\\.md|README(?:\\..*)?|CHANGELOG(?:\\..*)?)$ {
    return 403;
}

# Deny dotfiles except ACME challenge paths.
location ~* /\\.(?!well-known/) {
    return 403;
}

add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
add_header Strict-Transport-Security "max-age=31536000" always;
"""


def _write_hardening_snippet(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    desired = _desired_hardening_snippet()
    current = _read_text(HARDENING_SNIPPET)
    if current == desired:
        return []
    SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
    _snapshot(HARDENING_SNIPPET, backup_dir, snapshots)
    HARDENING_SNIPPET.write_text(desired, encoding="utf-8")
    return ["mautic_hardening_snippet"]


def _active_add_header_present(text: str, header: str) -> bool:
    pattern = re.compile(r"^\s*add_header\s+" + re.escape(header) + r"\b", re.IGNORECASE)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if pattern.search(raw):
            return True
    return False


def _strip_managed_security_header_block(text: str) -> str:
    pattern = re.compile(
        r"\n?" + re.escape(SECURITY_HEADERS_START) + r".*?" + re.escape(SECURITY_HEADERS_END) + r"\n?",
        re.DOTALL,
    )
    return pattern.sub("\n", text).rstrip("\n") + ("\n" if text else "")


def _desired_security_headers_snippet(text: str) -> str:
    base = _strip_managed_security_header_block(text)
    additions: list[str] = []
    if not _active_add_header_present(base, "X-Frame-Options"):
        additions.append('add_header X-Frame-Options "SAMEORIGIN" always;')
    if not _active_add_header_present(base, "Strict-Transport-Security"):
        additions.append('add_header Strict-Transport-Security "max-age=31536000" always;')
    if not _active_add_header_present(base, "Permissions-Policy"):
        additions.append('add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;')
    if not additions:
        return base
    block = "\n".join([SECURITY_HEADERS_START, *additions, SECURITY_HEADERS_END])
    return base.rstrip("\n") + "\n" + block + "\n"


def _ensure_security_headers_snippet(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    if not SECURITY_HEADERS_SNIPPET.exists():
        return []
    original = _read_text(SECURITY_HEADERS_SNIPPET)
    desired = _desired_security_headers_snippet(original)
    if desired == original:
        return []
    _snapshot(SECURITY_HEADERS_SNIPPET, backup_dir, snapshots)
    SECURITY_HEADERS_SNIPPET.write_text(desired, encoding="utf-8")
    return ["security_headers_snippet"]


def _active_server_config_files() -> list[Path]:
    files: dict[Path, None] = {}
    if SITES_ENABLED.exists():
        try:
            for path in sorted(SITES_ENABLED.iterdir()):
                if path.name.startswith(".") or not path.name.endswith(".conf"):
                    continue
                real = path.resolve() if path.is_symlink() else path
                if real.is_file():
                    files[real] = None
        except Exception:
            pass
    if CONF_D.exists():
        try:
            for path in sorted(CONF_D.glob("*.conf")):
                if path.name.startswith("zz-mcd-"):
                    continue
                if path.is_file():
                    files[path.resolve()] = None
        except Exception:
            pass
    return list(files.keys())


def _insert_hardening_include(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    out: list[str] = []
    depth = 0
    in_server = False
    server_start_depth = 0
    block: list[str] = []
    changed = False

    def flush_block(items: list[str]) -> list[str]:
        nonlocal changed
        if any(HARDENING_INCLUDE in x for x in items):
            return items
        insert_at = 1 if len(items) > 1 else len(items)
        for idx, item in enumerate(items):
            if re.match(r"^\s*server_name\s+.+;", item.strip()):
                insert_at = idx + 1
                break
        ref = items[insert_at - 1] if items else ""
        indent = ref[: len(ref) - len(ref.lstrip())] if ref else "    "
        items = [*items[:insert_at], f"{indent}{HARDENING_INCLUDE}", *items[insert_at:]]
        changed = True
        return items

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
            out.extend(flush_block(block))
            in_server = False
            block = []

    return "\n".join(out).rstrip("\n") + ("\n" if text.endswith("\n") or out else ""), changed


def _ensure_hardening_includes(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    actions: list[str] = []
    for path in _active_server_config_files():
        original = _read_text(path)
        if not original or "server" not in original:
            continue
        desired, changed = _insert_hardening_include(original)
        if not changed or desired == original:
            continue
        _snapshot(path, backup_dir, snapshots)
        path.write_text(desired, encoding="utf-8")
        actions.append(f"mautic_hardening_include:{path.name}")
    return actions


def _convert_sites_enabled_regular_files(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    actions: list[str] = []
    if not SITES_ENABLED.exists():
        return actions
    for enabled in sorted(SITES_ENABLED.iterdir()):
        if enabled.name.startswith("."):
            continue
        if enabled.is_symlink():
            continue
        if not enabled.is_file():
            continue
        SITES_AVAILABLE.mkdir(parents=True, exist_ok=True)
        enabled_name = _conf_name(enabled.name)
        available = SITES_AVAILABLE / enabled_name
        enabled_link = SITES_ENABLED / enabled_name
        _snapshot(enabled, backup_dir, snapshots)
        _snapshot(available, backup_dir, snapshots)
        if enabled_link != enabled:
            _snapshot(enabled_link, backup_dir, snapshots)
        try:
            copy_required = True
            if available.exists() and available.is_file():
                try:
                    copy_required = enabled.read_bytes() != available.read_bytes()
                except Exception:
                    copy_required = True
            if copy_required:
                shutil.copy2(enabled, available)
                actions.append(f"sites_available_updated:{available.name}")
            enabled.unlink()
            if os.path.lexists(enabled_link):
                enabled_link.unlink()
            os.symlink(str(available), str(enabled_link))
            actions.append(f"sites_enabled_symlink:{enabled.name}->{enabled_link.name}")
        except Exception as e:
            actions.append(f"sites_enabled_symlink_failed:{enabled.name}:{e}")
    return actions


def _normalize_sites_enabled_conf_suffix(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    actions: list[str] = []
    if not SITES_ENABLED.exists():
        return actions
    for enabled in sorted(SITES_ENABLED.iterdir()):
        if enabled.name.startswith(".") or enabled.name.endswith(".conf"):
            continue
        if not enabled.is_symlink():
            continue
        target = os.readlink(enabled)
        normalized = SITES_ENABLED / _conf_name(enabled.name)
        _snapshot(enabled, backup_dir, snapshots)
        _snapshot(normalized, backup_dir, snapshots)
        try:
            if os.path.lexists(normalized):
                normalized.unlink()
            os.symlink(target, normalized)
            enabled.unlink()
            actions.append(f"sites_enabled_conf_suffix:{enabled.name}->{normalized.name}")
        except Exception as e:
            actions.append(f"sites_enabled_conf_suffix_failed:{enabled.name}:{e}")
    return actions


def _run(cmd: list[str], timeout_sec: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=max(5, int(timeout_sec)))


def _nginx_test() -> tuple[bool, str]:
    if not shutil.which("nginx") and not Path("/usr/sbin/nginx").exists():
        return True, "nginx_test:skipped_no_binary"
    proc = _run(["nginx", "-t"], timeout_sec=30)
    msg = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode == 0:
        return True, "nginx_test:ok"
    return False, msg or "nginx -t failed"


def _reload_nginx() -> tuple[bool, str]:
    if shutil.which("systemctl"):
        active = _run(["systemctl", "is-active", "nginx"], timeout_sec=10)
        if active.returncode != 0 or (active.stdout or "").strip() != "active":
            return True, "nginx_reload:skipped_inactive"
        proc = _run(["systemctl", "reload", "nginx"], timeout_sec=30)
        msg = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode == 0:
            return True, "nginx_reload:ok"
        return False, msg or "nginx reload failed"
    if shutil.which("service"):
        proc = _run(["service", "nginx", "reload"], timeout_sec=30)
        msg = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode == 0:
            return True, "nginx_reload:ok"
        return False, msg or "nginx reload failed"
    return True, "nginx_reload:skipped_no_service_manager"


def ensure_nginx_baseline(*, reload_service: bool = True) -> dict[str, Any]:
    """Converge nginx to SalesSnap runtime assumptions safely and idempotently."""
    actions: list[str] = []
    if not _nginx_present():
        return {"status": "skipped", "changed": False, "actions": ["nginx:absent"]}

    backup_dir = BACKUP_ROOT / time.strftime("%Y%m%d%H%M%S")
    snapshots: dict[Path, _Snapshot] = {}
    changed = False

    try:
        if NGINX_CONF.exists():
            original = _read_text(NGINX_CONF)
            desired, conf_actions = _desired_nginx_conf(original)
            if desired != original:
                backup_dir.mkdir(parents=True, exist_ok=True)
                _snapshot(NGINX_CONF, backup_dir, snapshots)
                NGINX_CONF.write_text(desired, encoding="utf-8")
                actions.extend(conf_actions)
                changed = True
        if SITES_ENABLED.exists():
            symlink_actions = _convert_sites_enabled_regular_files(backup_dir, snapshots)
            if symlink_actions:
                actions.extend(symlink_actions)
                changed = True
            suffix_actions = _normalize_sites_enabled_conf_suffix(backup_dir, snapshots)
            if suffix_actions:
                actions.extend(suffix_actions)
                changed = True
        hardening_actions = _write_hardening_snippet(backup_dir, snapshots)
        if hardening_actions:
            actions.extend(hardening_actions)
            changed = True
        header_actions = _ensure_security_headers_snippet(backup_dir, snapshots)
        if header_actions:
            actions.extend(header_actions)
            changed = True
        include_actions = _ensure_hardening_includes(backup_dir, snapshots)
        if include_actions:
            actions.extend(include_actions)
            changed = True

        if not changed:
            return {"status": "ok", "changed": False, "actions": ["nginx_baseline:already_ok"]}

        test_ok, test_msg = _nginx_test()
        actions.append(test_msg if test_ok else "nginx_test:failed")
        if not test_ok:
            _restore_snapshots(snapshots)
            return {
                "status": "error",
                "changed": False,
                "actions": actions + ["rollback:done"],
                "error": test_msg,
            }

        if reload_service:
            reload_ok, reload_msg = _reload_nginx()
            actions.append(reload_msg if reload_ok else "nginx_reload:failed")
            if not reload_ok:
                return {"status": "error", "changed": True, "actions": actions, "error": reload_msg}

        return {"status": "ok", "changed": True, "actions": actions}
    except Exception as e:
        if snapshots:
            _restore_snapshots(snapshots)
        return {"status": "error", "changed": False, "actions": actions + ["rollback:done"], "error": str(e)}
