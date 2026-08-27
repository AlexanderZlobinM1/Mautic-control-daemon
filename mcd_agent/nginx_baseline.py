from __future__ import annotations

from dataclasses import dataclass
import ipaddress
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
FASTCGI_PHP_SNIPPET = SNIPPETS_DIR / "fastcgi-php.conf"
SECURITY_HEADERS_SNIPPET = SNIPPETS_DIR / "security-headers.conf"
MAUTIC_FORM_HEADERS_NAME = "20-mcd-mautic-form-headers.conf"
HARDENING_INCLUDE = "include /etc/nginx/snippets/mcd-mautic-hardening.conf;"
BACKUP_ROOT = Path("/var/backups/mcd-nginx-baseline")
SECURITY_HEADERS_START = "# mcd-security-headers-extra start"
SECURITY_HEADERS_END = "# mcd-security-headers-extra end"
MAUTIC_PUBLIC_APP_ASSETS_COMMENT = "MCD public Mautic app assets"
CLOUDFLARE_REAL_IP_FILE = CONF_D / "10-mcd-cloudflare-real-ip.conf"
CLOUDFLARE_REAL_IP_MARKER = "# Managed by MCD host baseline: trust Cloudflare edge and pass client IP to PHP/Mautic."
DEFAULT_DENY_FILE = CONF_D / "00-mcd-default-deny.conf"
DEFAULT_DENY_MARKER = "# Managed by MCD host baseline: deny unknown/default virtual hosts."
MAUTIC_FORM_HEADERS_MARKER = "# Managed by MCD: allow native cross-domain Mautic form responses."
CLOUDFLARE_REAL_IP_CIDRS = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
]
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


def _has_conf_d_include(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^include\s+/etc/nginx/conf\.d/[^;]*;", line):
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


def _default_deny_ssl_pair() -> tuple[Path, Path] | None:
    letsencrypt = Path("/etc/letsencrypt/live")
    if letsencrypt.exists():
        try:
            for fullchain in sorted(letsencrypt.glob("*/fullchain.pem")):
                privkey = fullchain.parent / "privkey.pem"
                if fullchain.exists() and privkey.exists():
                    return fullchain, privkey
        except Exception:
            pass
    snakeoil_cert = Path("/etc/ssl/certs/ssl-cert-snakeoil.pem")
    snakeoil_key = Path("/etc/ssl/private/ssl-cert-snakeoil.key")
    if snakeoil_cert.exists() and snakeoil_key.exists():
        return snakeoil_cert, snakeoil_key
    return None


def _listen_default_server_ports(text: str) -> set[int]:
    ports: set[int] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^listen\s+([^;#]+);", line, flags=re.IGNORECASE)
        if not match:
            continue
        args = match.group(1).split()
        if "default_server" not in {x.lower() for x in args}:
            continue
        address = args[0]
        port_match = re.search(r":(\d+)$", address)
        if port_match:
            ports.add(int(port_match.group(1)))
            continue
        if address.isdigit():
            ports.add(int(address))
    return ports


def _active_default_server_ports() -> set[int]:
    ports: set[int] = set()
    for path in _active_server_config_files():
        if path.resolve(strict=False) == DEFAULT_DENY_FILE.resolve(strict=False):
            continue
        ports.update(_listen_default_server_ports(_read_text(path)))
    return ports


def _desired_default_deny_config(
    pair: tuple[Path, Path] | None = None,
    default_server_ports: set[int] | None = None,
) -> str:
    ssl_pair = pair if pair is not None else _default_deny_ssl_pair()
    occupied_ports = _active_default_server_ports() if default_server_ports is None else default_server_ports
    lines = [
        DEFAULT_DENY_MARKER,
    ]
    if 80 not in occupied_ports:
        lines.extend(
            [
                "server {",
                "    listen 80 default_server;",
                "    server_name _;",
                "    return 444;",
                "}",
                "",
            ]
        )
    if ssl_pair is not None:
        cert, key = ssl_pair
        if 443 not in occupied_ports:
            lines.extend(
                [
                    "server {",
                    "    listen 443 ssl default_server;",
                    "    server_name _;",
                    f"    ssl_certificate {cert};",
                    f"    ssl_certificate_key {key};",
                    "    return 444;",
                    "}",
                    "",
                ]
            )
    return "\n".join(lines)


def _default_deny_satisfied() -> bool:
    current = _read_text(DEFAULT_DENY_FILE)
    if DEFAULT_DENY_MARKER not in current:
        return False
    desired = _desired_default_deny_config()
    return current == desired


def nginx_baseline_satisfied() -> bool:
    """Cheap read-only check for the SalesSnap nginx layout baseline."""
    if not _nginx_present():
        return True
    if not SITES_AVAILABLE.is_dir() or not SITES_ENABLED.is_dir():
        return False
    if NGINX_CONF.exists():
        text = _read_text(NGINX_CONF)
        if not _has_www_data_user(text):
            return False
        if not _has_conf_d_include(text):
            return False
        if not _has_sites_enabled_include(text):
            return False
    if _sites_enabled_has_regular_files():
        return False
    if _sites_enabled_has_non_conf_entries():
        return False
    if _read_text(HARDENING_SNIPPET) != _desired_hardening_snippet():
        return False
    if _read_text(_mautic_form_headers_file()) != _desired_mautic_form_headers_config():
        return False
    if _read_text(FASTCGI_PHP_SNIPPET) != _desired_fastcgi_php_snippet():
        return False
    if SECURITY_HEADERS_SNIPPET.exists() and SECURITY_HEADERS_START not in _read_text(SECURITY_HEADERS_SNIPPET):
        return False
    if not _default_deny_satisfied():
        return False
    modern_http2 = _nginx_supports_http2_directive()
    strip_ipv6_listens = _ipv6_listen_forbidden()
    for path in _active_server_config_files():
        text = _read_text(path)
        if HARDENING_INCLUDE not in text:
            return False
        if ensure_mautic_public_app_asset_locations(text) != text:
            return False
        if strip_ipv6_listens and remove_ipv6_listen_directives(text) != text:
            return False
        if normalize_legacy_http2_listen(text, modern_http2=modern_http2) != text:
            return False
        if remove_duplicate_server_autoindex_directives(text) != text:
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

    def insert_http_include(lines: list[str], directive: str) -> tuple[list[str], bool]:
        inserted = False
        out_lines: list[str] = []
        for idx, raw in enumerate(lines):
            out_lines.append(raw)
            if inserted or not re.match(r"^\s*http\s*\{", raw):
                continue
            insert_at = len(out_lines)
            while insert_at < len(lines):
                nxt = lines[insert_at].strip()
                if not nxt.startswith("include "):
                    break
                insert_at += 1
            indent = raw[: len(raw) - len(raw.lstrip())] + "    "
            out_lines.extend(lines[idx + 1:insert_at])
            out_lines.append(f"{indent}{directive}")
            out_lines.extend(lines[insert_at:])
            inserted = True
            return out_lines, True
        return out_lines, False

    final = list(out)
    text_after_user = "\n".join(final)
    if not _has_conf_d_include(text_after_user):
        final, inserted = insert_http_include(final, "include /etc/nginx/conf.d/*.conf;")
        if inserted:
            actions.append("conf_d_include")
    text_after_conf_d = "\n".join(final)
    if not _has_sites_enabled_include(text_after_conf_d):
        final, inserted = insert_http_include(final, "include /etc/nginx/sites-enabled/*.conf;")
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

add_header X-Frame-Options $mcd_x_frame_options always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
add_header Strict-Transport-Security "max-age=31536000" always;
"""


def _desired_mautic_form_headers_config() -> str:
    return f"""{MAUTIC_FORM_HEADERS_MARKER}
map $request_uri $mcd_x_frame_options {{
    default "SAMEORIGIN";
    ~^/form/submit(?:\\?|$) "";
}}
"""


def _mautic_form_headers_file() -> Path:
    return CONF_D / MAUTIC_FORM_HEADERS_NAME


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


def remove_duplicate_server_autoindex_directives(text: str) -> str:
    """Remove server-level autoindex directives duplicated by MCD hardening."""
    if "autoindex" not in text:
        return text

    autoindex_re = re.compile(r"^\s*autoindex\s+\S+\s*;", re.IGNORECASE)

    def normalize_block(lines: list[str]) -> list[str]:
        has_hardening = any(HARDENING_INCLUDE in raw for raw in lines)
        seen_autoindex = has_hardening
        out: list[str] = []
        depth = 0
        for raw in lines:
            stripped = raw.strip()
            is_server_autoindex = (
                depth == 1
                and not stripped.startswith("#")
                and autoindex_re.match(raw) is not None
            )
            if is_server_autoindex:
                if seen_autoindex:
                    depth += raw.count("{") - raw.count("}")
                    continue
                seen_autoindex = True
            out.append(raw)
            depth += raw.count("{") - raw.count("}")
        return out

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


_IPV6_LISTEN_RE = re.compile(r"^\s*listen\s+\[[0-9a-fA-F:.]*:[0-9a-fA-F:.]*\](?::\d+)?(?:\s+[^;#]*)?\s*;", re.IGNORECASE)


def remove_ipv6_listen_directives(text: str) -> str:
    if "[" not in text or "listen" not in text:
        return text
    out: list[str] = []
    changed = False
    for raw in text.splitlines():
        if not raw.lstrip().startswith("#") and _IPV6_LISTEN_RE.match(raw):
            changed = True
            continue
        out.append(raw)
    if not changed:
        return text
    return "\n".join(out).rstrip("\n") + ("\n" if text.endswith("\n") else "")


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


def _desired_fastcgi_php_snippet() -> str:
    return """# Managed by MCD. Compatibility snippet for nginx.org packages without Debian fastcgi snippets.
fastcgi_split_path_info ^(.+?\\.php)(/.*)$;
try_files $fastcgi_script_name =404;
set $path_info $fastcgi_path_info;
fastcgi_param PATH_INFO $path_info;
fastcgi_index index.php;
fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
include fastcgi_params;
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


def _ensure_mautic_form_headers_config(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    target = _mautic_form_headers_file()
    desired = _desired_mautic_form_headers_config()
    current = _read_text(target)
    if current == desired:
        return []
    _snapshot(target, backup_dir, snapshots)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(desired, encoding="utf-8")
    return ["mautic_form_headers"]


def _write_fastcgi_php_snippet(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    desired = _desired_fastcgi_php_snippet()
    current = _read_text(FASTCGI_PHP_SNIPPET)
    if current == desired:
        return []
    SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
    _snapshot(FASTCGI_PHP_SNIPPET, backup_dir, snapshots)
    FASTCGI_PHP_SNIPPET.write_text(desired, encoding="utf-8")
    return ["fastcgi_php_snippet"]


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
        additions.append("add_header X-Frame-Options $mcd_x_frame_options always;")
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


def _ensure_default_deny_config(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    desired = _desired_default_deny_config()
    original = _read_text(DEFAULT_DENY_FILE)
    if original == desired:
        return []
    _snapshot(DEFAULT_DENY_FILE, backup_dir, snapshots)
    DEFAULT_DENY_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_DENY_FILE.write_text(desired, encoding="utf-8")
    return ["default_deny_vhost"]


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
                if path.resolve(strict=False) == DEFAULT_DENY_FILE.resolve(strict=False):
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


def _ensure_server_config_normalization(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    actions: list[str] = []
    modern_http2 = _nginx_supports_http2_directive()
    strip_ipv6_listens = _ipv6_listen_forbidden()
    for path in _active_server_config_files():
        original = _read_text(path)
        if not original or "server" not in original:
            continue
        desired = ensure_mautic_public_app_asset_locations(original)
        asset_changed = desired != original
        after_ipv6 = remove_ipv6_listen_directives(desired) if strip_ipv6_listens else desired
        ipv6_changed = after_ipv6 != desired
        desired = after_ipv6
        after_http2 = normalize_legacy_http2_listen(desired, modern_http2=modern_http2)
        http2_changed = after_http2 != desired
        desired = after_http2
        after_autoindex = remove_duplicate_server_autoindex_directives(desired)
        autoindex_changed = after_autoindex != desired
        desired = after_autoindex
        if desired == original:
            continue
        _snapshot(path, backup_dir, snapshots)
        path.write_text(desired, encoding="utf-8")
        if asset_changed:
            actions.append(f"mautic_public_app_assets:{path.name}")
        if ipv6_changed:
            actions.append(f"ipv6_listen_removed:{path.name}")
        if http2_changed:
            actions.append(f"http2_listen_modernized:{path.name}")
        if autoindex_changed:
            actions.append(f"duplicate_autoindex_removed:{path.name}")
    return actions


def _ensure_sites_directories() -> list[str]:
    actions: list[str] = []
    for path in (SITES_AVAILABLE, SITES_ENABLED):
        if path.exists() and not path.is_dir():
            raise RuntimeError(f"nginx sites path is not a directory: {path}")
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            actions.append(f"created:{path}")
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


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def cloudflare_real_ip_profile_present(profile: dict[str, Any] | None) -> bool:
    if not isinstance(profile, dict):
        return False
    return any(key in profile for key in _CLOUDFLARE_REAL_IP_PROFILE_KEYS)


def _cloudflare_real_ip_enabled(profile: dict[str, Any] | None) -> bool | None:
    if not cloudflare_real_ip_profile_present(profile):
        return None
    return _as_bool((profile or {}).get("cloudflare_real_ip_enabled"), False)


def _cloudflare_real_ip_target_file(profile: dict[str, Any] | None) -> Path:
    raw = str((profile or {}).get("cloudflare_real_ip_target_file") or CLOUDFLARE_REAL_IP_FILE).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = CONF_D / path
    if path.suffix != ".conf":
        raise ValueError("cloudflare real-ip target must be a .conf file")
    try:
        path.relative_to(CONF_D)
    except ValueError:
        raise ValueError("cloudflare real-ip target must live under /etc/nginx/conf.d") from None
    return path


def _cloudflare_real_ip_header(profile: dict[str, Any] | None) -> str:
    raw = str((profile or {}).get("cloudflare_real_ip_header") or "CF-Connecting-IP").strip()
    if not re.match(r"^[A-Za-z0-9_-]+$", raw):
        raise ValueError("cloudflare real-ip header contains invalid characters")
    return raw


def _cloudflare_real_ip_cidrs(profile: dict[str, Any] | None) -> list[str]:
    raw = None
    if isinstance(profile, dict):
        raw = profile.get("cloudflare_real_ip_cidrs")
        if raw is None:
            raw = profile.get("cloudflare_real_ip_set_real_ip_from")
    src = raw if isinstance(raw, list) else CLOUDFLARE_REAL_IP_CIDRS
    out: list[str] = []
    seen: set[str] = set()
    for item in src:
        try:
            cidr = str(ipaddress.ip_network(str(item).strip(), strict=False))
        except Exception:
            continue
        if cidr in seen:
            continue
        seen.add(cidr)
        out.append(cidr)
    return out or list(CLOUDFLARE_REAL_IP_CIDRS)


def _desired_cloudflare_real_ip_config(profile: dict[str, Any] | None) -> str:
    header = _cloudflare_real_ip_header(profile)
    recursive = "on" if _as_bool((profile or {}).get("cloudflare_real_ip_recursive"), True) else "off"
    lines = [CLOUDFLARE_REAL_IP_MARKER]
    lines.extend(f"set_real_ip_from {cidr};" for cidr in _cloudflare_real_ip_cidrs(profile))
    lines.append(f"real_ip_header {header};")
    lines.append(f"real_ip_recursive {recursive};")
    return "\n".join(lines) + "\n"


def cloudflare_real_ip_state(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        target = _cloudflare_real_ip_target_file(profile)
        enabled = _cloudflare_real_ip_enabled(profile)
        desired = _desired_cloudflare_real_ip_config(profile)
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}
    exists = target.exists()
    current = _read_text(target) if exists else ""
    included = _has_conf_d_include(_read_text(NGINX_CONF)) if NGINX_CONF.exists() else False
    managed = CLOUDFLARE_REAL_IP_MARKER in current if current else False
    matches = bool(exists and current == desired)
    desired_cidrs = _cloudflare_real_ip_cidrs(profile)
    current_cidrs = re.findall(r"^\s*set_real_ip_from\s+([^;#\s]+)\s*;", current, flags=re.MULTILINE)
    current_header_match = re.search(r"^\s*real_ip_header\s+([^;#\s]+)\s*;", current, flags=re.MULTILINE)
    current_header = current_header_match.group(1) if current_header_match else ""
    recursive_on = bool(re.search(r"^\s*real_ip_recursive\s+on\s*;", current, flags=re.MULTILINE | re.IGNORECASE))

    if enabled is True:
        if not _nginx_present():
            status = "pending"
            message = "nginx is not installed"
        elif not exists:
            status = "missing"
            message = f"{target} is missing"
        elif not included:
            status = "drift"
            message = "nginx.conf does not include /etc/nginx/conf.d/*.conf"
        elif not matches:
            status = "drift"
            message = "managed Cloudflare real-ip template differs from desired state"
        else:
            status = "ok"
            message = f"{target} active"
    elif enabled is False:
        if exists and managed and _as_bool((profile or {}).get("cloudflare_real_ip_remove_when_disabled"), True):
            status = "drift"
            message = "managed Cloudflare real-ip file is present while edge mode is direct"
        else:
            status = "disabled"
            message = "disabled by profile"
    else:
        status = "present" if exists else "absent"
        message = f"{target} present" if exists else f"{target} absent"

    return {
        "status": status,
        "message": message,
        "enabled": enabled,
        "target_file": str(target),
        "exists": bool(exists),
        "managed": bool(managed),
        "matches": bool(matches),
        "included": bool(included),
        "real_ip_header": current_header,
        "real_ip_recursive": "on" if recursive_on else ("off" if "real_ip_recursive" in current else ""),
        "cidr_count": len(current_cidrs),
        "desired_cidr_count": len(desired_cidrs),
    }


def ensure_cloudflare_real_ip(profile: dict[str, Any] | None, *, reload_service: bool = True) -> dict[str, Any]:
    if not cloudflare_real_ip_profile_present(profile):
        return {"status": "skipped", "reason": "profile_not_configured"}
    if os.geteuid() != 0:
        raise RuntimeError("cloudflare real-ip apply requires root")
    target = _cloudflare_real_ip_target_file(profile)
    enabled = _cloudflare_real_ip_enabled(profile) is True
    remove_when_disabled = _as_bool((profile or {}).get("cloudflare_real_ip_remove_when_disabled"), True)
    before = _read_text(target) if target.exists() else None

    if not enabled:
        if target.exists() and CLOUDFLARE_REAL_IP_MARKER in (before or "") and remove_when_disabled:
            target.unlink()
            test_ok, test_msg = _nginx_test()
            if not test_ok:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(before or "", encoding="utf-8")
                return {"status": "error", "reason": test_msg, "changed_files": [str(target)], "rolled_back": True}
            actions = ["cloudflare_real_ip:removed", test_msg]
            if reload_service:
                reload_ok, reload_msg = _reload_nginx()
                actions.append(reload_msg if reload_ok else "nginx_reload:failed")
                if not reload_ok:
                    return {"status": "error", "reason": reload_msg, "actions": actions, "changed_files": [str(target)]}
            return {"status": "applied", "actions": actions, "changed_files": [str(target)], "state": cloudflare_real_ip_state(profile)}
        return {"status": "disabled", "actions": ["cloudflare_real_ip:disabled"], "changed_files": [], "state": cloudflare_real_ip_state(profile)}

    if not _nginx_present():
        return {"status": "skipped", "reason": "nginx_absent"}

    baseline = ensure_nginx_baseline(reload_service=False)
    if str(baseline.get("status", "")).lower() == "error":
        return {"status": "error", "reason": str(baseline.get("error", "nginx baseline failed")), "baseline": baseline}

    desired = _desired_cloudflare_real_ip_config(profile)
    if before == desired:
        return {"status": "noop", "actions": ["cloudflare_real_ip:already_ok"], "changed_files": [], "state": cloudflare_real_ip_state(profile)}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(desired, encoding="utf-8")
    test_ok, test_msg = _nginx_test()
    if not test_ok:
        if before is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(before, encoding="utf-8")
        return {"status": "error", "reason": test_msg, "changed_files": [str(target)], "rolled_back": True}

    actions = ["cloudflare_real_ip:written", test_msg]
    if isinstance(baseline.get("actions"), list):
        actions.extend(f"nginx_baseline:{x}" for x in baseline.get("actions", []))
    if reload_service:
        reload_ok, reload_msg = _reload_nginx()
        actions.append(reload_msg if reload_ok else "nginx_reload:failed")
        if not reload_ok:
            return {"status": "error", "reason": reload_msg, "actions": actions, "changed_files": [str(target)]}
    return {"status": "applied", "actions": actions, "changed_files": [str(target)], "state": cloudflare_real_ip_state(profile)}


def ensure_nginx_baseline(*, reload_service: bool = True) -> dict[str, Any]:
    """Converge nginx to SalesSnap runtime assumptions safely and idempotently."""
    actions: list[str] = []
    if not _nginx_present():
        return {"status": "skipped", "changed": False, "actions": ["nginx:absent"]}

    backup_dir = BACKUP_ROOT / time.strftime("%Y%m%d%H%M%S")
    snapshots: dict[Path, _Snapshot] = {}
    changed = False

    try:
        dir_actions = _ensure_sites_directories()
        if dir_actions:
            actions.extend(dir_actions)
            changed = True
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
        form_header_actions = _ensure_mautic_form_headers_config(backup_dir, snapshots)
        if form_header_actions:
            actions.extend(form_header_actions)
            changed = True
        fastcgi_actions = _write_fastcgi_php_snippet(backup_dir, snapshots)
        if fastcgi_actions:
            actions.extend(fastcgi_actions)
            changed = True
        header_actions = _ensure_security_headers_snippet(backup_dir, snapshots)
        if header_actions:
            actions.extend(header_actions)
            changed = True
        deny_actions = _ensure_default_deny_config(backup_dir, snapshots)
        if deny_actions:
            actions.extend(deny_actions)
            changed = True
        include_actions = _ensure_hardening_includes(backup_dir, snapshots)
        if include_actions:
            actions.extend(include_actions)
            changed = True
        normalize_actions = _ensure_server_config_normalization(backup_dir, snapshots)
        if normalize_actions:
            actions.extend(normalize_actions)
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
