from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Any
from urllib.parse import urlsplit

from mcd_agent.models import MauticInstall


NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
BACKUP_ROOT = Path("/var/backups/mcd-form-embed")
STATE_PATH = Path("/opt/mcd/var/state/form-embed.json")

_MANAGED_BEGIN = "# BEGIN MCD managed form embed"
_MANAGED_END = "# END MCD managed form embed"
_MANAGED_BLOCK_RE = re.compile(
    rf"(?ms)^[ \t]*{re.escape(_MANAGED_BEGIN)}\n.*?^[ \t]*{re.escape(_MANAGED_END)}\n?"
)
_SERVER_START_RE = re.compile(r"(?m)^\s*server\s*\{")
_SERVER_NAME_RE = re.compile(r"(?m)^\s*server_name\s+([^;]+);")
_ROOT_RE = re.compile(r"(?m)^\s*root\s+([^;]+);")
_FORM_LOCATION_RE = re.compile(r"(?m)^\s*location\s+\^~\s+/form/\s*\{")
_LOCATION_RE = re.compile(r"(?m)^\s*location\s+")
_FASTCGI_PASS_RE = re.compile(r"(?m)^\s*fastcgi_pass\s+([^;]+);")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SAFE_UPSTREAM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class FormEmbedError(ValueError):
    """Raised when an origin or managed form setting is unsafe."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    return bool(default)


def _origin_items(raw: Any) -> list[object]:
    if isinstance(raw, str):
        return [part.strip() for part in raw.splitlines()]
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    return []


def normalize_origins(raw: Any) -> list[str]:
    """Accept only canonical, exact HTTPS web origins.

    Nginx emits these values directly into quoted directives, so this is also an
    injection boundary. Paths, credentials, wildcard hosts and non-HTTPS
    endpoints are intentionally rejected rather than silently repaired.
    """
    result: list[str] = []
    seen: set[str] = set()
    for item in _origin_items(raw):
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 300 or any(ch.isspace() for ch in text):
            raise FormEmbedError("origin must be one HTTPS origin without whitespace")
        try:
            parsed = urlsplit(text)
            port = parsed.port
        except ValueError as exc:
            raise FormEmbedError(f"invalid origin: {text}") from exc
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if (
            parsed.scheme.lower() != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise FormEmbedError(f"origin must be an exact HTTPS origin: {text}")
        if not _DOMAIN_RE.fullmatch(host):
            raise FormEmbedError(f"origin host is invalid: {text}")
        if port is not None and not 1 <= port <= 65535:
            raise FormEmbedError(f"origin port is invalid: {text}")
        canonical = "https://" + host
        if port not in {None, 443}:
            canonical += f":{port}"
        if canonical in seen:
            continue
        seen.add(canonical)
        result.append(canonical)
        if len(result) > 100:
            raise FormEmbedError("at most 100 origins are allowed")
    return result


def normalize_setting(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": _coerce_bool(raw.get("enabled"), False),
        "frame_ancestors": normalize_origins(raw.get("frame_ancestors", raw.get("frameAncestors", []))),
        "cors_origins": normalize_origins(raw.get("cors_origins", raw.get("corsOrigins", []))),
    }


def render_form_embed_location(
    *,
    fastcgi_pass: str,
    frame_ancestors: list[str] | tuple[str, ...] | None = None,
    cors_origins: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Render a self-contained, ordinary-Mautic `/form/` FastCGI location."""
    upstream = _safe_fastcgi_pass(fastcgi_pass)
    frames = normalize_origins(list(frame_ancestors or []))
    cors = normalize_origins(list(cors_origins or []))
    csp = "frame-ancestors 'self'" + (" " + " ".join(frames) if frames else "")
    lines = [
        _MANAGED_BEGIN,
        "location ^~ /form/ {",
        "    fastcgi_hide_header Content-Security-Policy;",
        "    fastcgi_hide_header X-Frame-Options;",
        f'    add_header Content-Security-Policy "{csp}" always;',
        '    add_header X-Content-Type-Options "nosniff" always;',
        '    add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
        '    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;',
        '    add_header Strict-Transport-Security "max-age=31536000" always;',
        '    add_header Cache-Control "max-age=0, no-cache, no-store, must-revalidate" always;',
        '    add_header Vary "Origin" always;',
    ]
    if cors:
        lines.extend(
            [
                '    set $mcd_form_cors_origin "";',
                '    set $mcd_form_cors_credentials "";',
                '    set $mcd_form_cors_methods "";',
                '    set $mcd_form_cors_headers "";',
            ]
        )
        for origin in cors:
            lines.extend(
                [
                    f'    if ($http_origin = "{origin}") {{',
                    '        set $mcd_form_cors_origin $http_origin;',
                    '        set $mcd_form_cors_credentials "true";',
                    '        set $mcd_form_cors_methods "GET, POST, OPTIONS";',
                    '        set $mcd_form_cors_headers "Content-Type, Origin, Accept, X-Requested-With";',
                    "    }",
                ]
            )
        lines.extend(
            [
                '    add_header Access-Control-Allow-Origin $mcd_form_cors_origin always;',
                '    add_header Access-Control-Allow-Credentials $mcd_form_cors_credentials always;',
                '    add_header Access-Control-Allow-Methods $mcd_form_cors_methods always;',
                '    add_header Access-Control-Allow-Headers $mcd_form_cors_headers always;',
                "",
                "    if ($request_method = OPTIONS) {",
                "        return 204;",
                "    }",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "    # No CORS origins are configured for this instance.",
                "    if ($request_method = OPTIONS) {",
                "        return 403;",
                "    }",
            ]
        )
    lines.extend(
        [
            "",
            "    include fastcgi_params;",
            "    fastcgi_param SCRIPT_FILENAME $document_root/index.php;",
            "    fastcgi_param SCRIPT_NAME /index.php;",
            "    fastcgi_param DOCUMENT_URI /index.php;",
            "    fastcgi_param REQUEST_URI $request_uri;",
            '    fastcgi_param PATH_INFO "";',
            f"    fastcgi_pass {upstream};",
            "    fastcgi_read_timeout 300;",
            "}",
            _MANAGED_END,
        ]
    )
    return "\n".join(lines)


def _safe_fastcgi_pass(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("unix:/"):
        path = raw.removeprefix("unix:")
        if re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", path):
            return raw
    if _SAFE_UPSTREAM_RE.fullmatch(raw):
        return raw
    raise FormEmbedError("nginx FastCGI upstream is unsafe or unsupported")


def _brace_end(text: str, opening: int) -> int | None:
    depth = 0
    quote = ""
    escaped = False
    comment = False
    for index in range(opening, len(text)):
        char = text[index]
        if comment:
            if char == "\n":
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char == "#":
            comment = True
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _server_blocks(text: str) -> list[tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    for match in _SERVER_START_RE.finditer(text):
        opening = text.find("{", match.start(), match.end())
        if opening < 0:
            continue
        ending = _brace_end(text, opening)
        if ending is not None:
            blocks.append((match.start(), ending))
    return blocks


def _instance_domains(inst: MauticInstall) -> set[str]:
    values = [inst.name, inst.primary_domain, *(inst.domains or [])]
    return {str(value or "").strip().lower() for value in values if str(value or "").strip()}


def _instance_roots(inst: MauticInstall) -> set[str]:
    root = Path(str(inst.root or ""))
    values = {str(root)} if str(root) else set()
    for child in ("docroot", "public"):
        candidate = root / child
        if candidate.is_dir():
            values.add(str(candidate))
    return values


def _server_matches_instance(block: str, inst: MauticInstall) -> bool:
    roots = {value.strip().strip("'\"") for value in _ROOT_RE.findall(block)}
    if roots & _instance_roots(inst):
        return True
    names: set[str] = set()
    for value in _SERVER_NAME_RE.findall(block):
        names.update(item.strip().lower() for item in value.split() if item.strip())
    return bool(names & _instance_domains(inst))


def _active_vhosts_for_instance(inst: MauticInstall) -> list[Path]:
    paths: list[Path] = []
    if not NGINX_SITES_ENABLED.is_dir():
        return paths
    for path in sorted(NGINX_SITES_ENABLED.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".conf"):
            continue
        try:
            real = path.resolve(strict=True)
            text = real.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(_server_matches_instance(text[start:end], inst) for start, end in _server_blocks(text)) and real not in paths:
            paths.append(real)
    return paths


def _fastcgi_pass_in(block: str) -> str | None:
    for match in _FASTCGI_PASS_RE.finditer(block):
        try:
            return _safe_fastcgi_pass(match.group(1))
        except FormEmbedError:
            continue
    return None


def _insert_before_first_location(block: str, rendered: str) -> str | None:
    match = _LOCATION_RE.search(block)
    if match is None:
        return None
    indent = re.match(r"[ \t]*", block[match.start() :]).group(0)
    indented = "\n".join((indent + line) if line else "" for line in rendered.splitlines())
    return block[: match.start()] + indented + "\n\n" + block[match.start() :]


def _rewrite_vhost(text: str, inst: MauticInstall, setting: dict[str, Any]) -> tuple[str, str]:
    enabled = bool(setting.get("enabled", False))
    result = text
    markers = list(_MANAGED_BLOCK_RE.finditer(result))
    if markers:
        pieces: list[str] = []
        cursor = 0
        for marker in markers:
            old = marker.group(0)
            upstream = _fastcgi_pass_in(old)
            if enabled and upstream is None:
                return text, "blocked_managed_upstream_missing"
            replacement = (
                render_form_embed_location(
                    fastcgi_pass=upstream or "",
                    frame_ancestors=setting.get("frame_ancestors", []),
                    cors_origins=setting.get("cors_origins", []),
                )
                if enabled
                else ""
            )
            pieces.extend([result[cursor : marker.start()], replacement])
            cursor = marker.end()
        pieces.append(result[cursor:])
        return "".join(pieces), "managed"
    if not enabled:
        return text, "disabled"

    replacements: list[tuple[int, int, str]] = []
    matched = False
    for start, end in _server_blocks(text):
        block = text[start:end]
        if not _server_matches_instance(block, inst):
            continue
        matched = True
        if _FORM_LOCATION_RE.search(block):
            return text, "blocked_custom_form_location"
        upstream = _fastcgi_pass_in(block)
        if not upstream:
            continue
        replacement = _insert_before_first_location(
            block,
            render_form_embed_location(
                fastcgi_pass=upstream,
                frame_ancestors=setting.get("frame_ancestors", []),
                cors_origins=setting.get("cors_origins", []),
            ),
        )
        if replacement is not None:
            replacements.append((start, end, replacement))
    if not matched:
        return text, "blocked_vhost_not_found"
    if not replacements:
        return text, "blocked_fastcgi_not_found"
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text, "managed"


def _instance_setting_keys(inst: MauticInstall) -> list[str]:
    raw = [inst.instance_uid, inst.root, inst.name, inst.primary_domain, *(inst.domains or [])]
    out: list[str] = []
    for value in raw:
        key = str(value or "").strip()
        if key and key not in out:
            out.append(key)
    return out


def _setting_for_instance(settings: Any, inst: MauticInstall) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(settings, dict):
        return None
    for key in [*_instance_setting_keys(inst), "default"]:
        if key in settings:
            return key, normalize_setting(settings[key])
    return None


def _write_state(statuses: dict[str, dict[str, Any]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "instances": statuses}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def load_form_embed_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"instances": {}}
    return value if isinstance(value, dict) else {"instances": {}}


def sync_form_embed_settings(config: Any, installs: list[MauticInstall]) -> dict[str, Any]:
    """Apply saved form embedding settings only to explicitly configured instances."""
    settings = getattr(config, "form_embed_instance_settings", {})
    statuses: dict[str, dict[str, Any]] = {}
    snapshots: dict[Path, str] = {}
    changed = False
    backup_dir = BACKUP_ROOT / _utc_stamp()
    try:
        for inst in installs:
            selected = _setting_for_instance(settings, inst)
            if selected is None:
                continue
            source_key, setting = selected
            instance_key = str(inst.instance_uid or inst.root or inst.name or "").strip() or "unknown"
            row: dict[str, Any] = {
                "source_key": source_key,
                "enabled": bool(setting["enabled"]),
                "frame_ancestors": list(setting["frame_ancestors"]),
                "cors_origins": list(setting["cors_origins"]),
                "status": "unchanged",
                "vhosts": [],
            }
            paths = _active_vhosts_for_instance(inst)
            if not paths:
                row.update({"status": "blocked", "reason": "nginx_vhost_not_found"})
                statuses[instance_key] = row
                continue
            for path in paths:
                original = path.read_text(encoding="utf-8", errors="ignore")
                rendered, outcome = _rewrite_vhost(original, inst, setting)
                row["vhosts"].append({"path": str(path), "result": outcome})
                if outcome.startswith("blocked_"):
                    row.update({"status": "blocked", "reason": outcome})
                    break
                if rendered != original:
                    if path not in snapshots:
                        snapshots[path] = original
                    backup = backup_dir / path.as_posix().lstrip("/")
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if not backup.exists():
                        shutil.copy2(path, backup)
                    tmp = path.with_name(path.name + ".mcd-form.tmp")
                    tmp.write_text(rendered, encoding="utf-8")
                    os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
                    os.replace(tmp, path)
                    changed = True
                    row["status"] = "applied"
            statuses[instance_key] = row

        if changed:
            tested = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=30, check=False)
            if tested.returncode != 0:
                raise RuntimeError("nginx -t failed: " + (tested.stderr or tested.stdout or "unknown error").strip())
            reloaded = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=30, check=False)
            if reloaded.returncode != 0:
                raise RuntimeError("nginx reload failed: " + (reloaded.stderr or reloaded.stdout or "unknown error").strip())
    except Exception as exc:
        for path, original in snapshots.items():
            path.write_text(original, encoding="utf-8")
        if snapshots:
            subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=30, check=False)
            subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=30, check=False)
        for row in statuses.values():
            if row.get("status") == "applied":
                row.update({"status": "error", "reason": str(exc)})
        _write_state(statuses)
        return {"status": "error", "changed": False, "reason": str(exc), "instances": statuses, "backup_dir": str(backup_dir)}

    _write_state(statuses)
    return {
        "status": "ok",
        "changed": changed,
        "instances": statuses,
        "backup_dir": str(backup_dir) if snapshots else "",
    }
