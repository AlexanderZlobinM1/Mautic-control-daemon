from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

from mcd_agent.config import AgentConfig
from mcd_agent.install_type import detect_install_type, plugin_dir_candidates


AMAZON_MAILER_REQUIRED_BUNDLES: set[str] = {
    "AmazonSnsCallbackBundle",
    "MauticAmazonSesBundle",
}

SENDGRID_MAILER_REQUIRED_BUNDLES: set[str] = {
    "MauticSendGridSnsBundle",
    "MauticSendgridSnsBundle",
    "SendGridSnsBundle",
    "SendgridSnsBundle",
    "SendGridBundle",
    "SendgridBundle",
    "SendGridMailerBundle",
    "SendgridMailerBundle",
}

AMAZON_MAILER_PACKAGE = "symfony/amazon-mailer"
HTTP_CLIENT_PACKAGE = "symfony/http-client"
SENDGRID_MAILER_PACKAGE = "symfony/sendgrid-mailer:*"

_MAUTIC_COMPOSER_REQUIRE_MARKERS = {
    "mautic/core-lib",
    "mautic/core-composer-scaffold",
    "mautic/recommended-project",
}


def _run(
    cmd: list[str],
    *,
    cwd: str,
    timeout_sec: int = 900,
    as_www_data: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    full = cmd
    if as_www_data:
        full = ["sudo", "-u", "www-data"] + cmd
    logging.info("mailer preflight run: %s", " ".join(full))
    proc = subprocess.run(full, cwd=cwd, text=True, capture_output=True, timeout=timeout_sec)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(full)}\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return proc


def _resolve_project_root(root: str) -> str:
    p = Path(root)
    candidates = [p, p.parent]
    for c in candidates:
        if (c / "composer.json").exists() and (c / "bin" / "console").exists():
            return str(c)
    return root


def _load_composer_json(project_root: str) -> dict[str, object]:
    path = Path(project_root) / "composer.json"
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _composer_project_is_mautic(project_root: str) -> bool:
    data = _load_composer_json(project_root)
    if not data:
        return False
    name = str(data.get("name", "") or "").strip().lower()
    if name in {"mautic/mautic", "mautic/recommended-project"}:
        return True
    if name.startswith("mautic/"):
        return True
    req = data.get("require", {})
    req_keys = set(str(k).strip().lower() for k in req.keys()) if isinstance(req, dict) else set()
    return bool(req_keys.intersection(_MAUTIC_COMPOSER_REQUIRE_MARKERS))


def _mautic_console_healthy(
    *,
    project_root: str,
    console_path: str,
    php_bin: str,
    run_as_user: str = "www-data",
) -> bool:
    console_abs = _normalize_console_path(project_root, console_path)
    proc = _run(
        [php_bin, console_abs, "--version"],
        cwd=project_root,
        as_www_data=bool(run_as_user == "www-data"),
        check=False,
        timeout_sec=90,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0 and bool(re.search(r"\bMautic\s+\d+\.\d+\.\d+\b", out, re.IGNORECASE))


def _resolve_composer_bin() -> str:
    preferred = Path("/usr/local/bin/composer")
    if preferred.exists():
        return str(preferred)
    found = shutil.which("composer")
    if found:
        return found
    with tempfile.NamedTemporaryFile(prefix="composer-setup-", suffix=".php", delete=False) as tf:
        setup_path = Path(tf.name)
    try:
        with urllib.request.urlopen("https://getcomposer.org/installer", timeout=90) as resp:
            setup_path.write_bytes(resp.read())
        subprocess.run(
            ["php", str(setup_path), "--install-dir=/usr/local/bin", "--filename=composer"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        setup_path.unlink(missing_ok=True)
    return str(preferred)


def _verify_composer_as_www_data(project_root: str, composer_bin: str) -> None:
    _run(
        [composer_bin, "-V", "--no-interaction", "--no-ansi"],
        cwd=project_root,
        as_www_data=True,
        check=True,
        timeout_sec=60,
    )


def _node_major() -> int:
    found = shutil.which("node")
    if not found:
        return 0
    proc = subprocess.run([found, "-v"], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        return 0
    raw = (proc.stdout or "").strip()
    m = re.search(r"v?(\d+)", raw)
    if not m:
        return 0
    return int(m.group(1))


def _npm_ok() -> bool:
    found = shutil.which("npm")
    if not found:
        return False
    proc = subprocess.run([found, "-v"], capture_output=True, text=True, timeout=20)
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _ensure_node20() -> None:
    if _node_major() >= 20 and _npm_ok():
        return
    if _node_major() < 20:
        subprocess.run(["apt-get", "remove", "--purge", "-y", "nodejs", "libnode-dev"], check=False)
        subprocess.run(["bash", "-lc", "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"], check=True, timeout=300)
        subprocess.run(["apt-get", "install", "-y", "nodejs"], check=True, timeout=300)
    else:
        # Node without npm is usually a broken package source state. Refresh
        # NodeSource first so reinstalling `nodejs` restores npm as well.
        subprocess.run(["bash", "-lc", "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"], check=True, timeout=300)
        subprocess.run(["apt-get", "install", "--reinstall", "-y", "nodejs"], check=True, timeout=300)
    if _node_major() < 20:
        raise RuntimeError("Node.js 20 preflight failed (node -v is not v20+)")
    if not _npm_ok():
        raise RuntimeError("Node.js 20 preflight failed (npm -v is not available)")


def _composer_show_name(package_name: str) -> str:
    # Composer `require` may include a constraint (e.g. `pkg:*`), but
    # `composer show` must receive only the package name.
    return str(package_name or "").split(":", 1)[0].strip()


def _composer_has_package(project_root: str, composer_bin: str, package_name: str) -> bool:
    show_name = _composer_show_name(package_name)
    if not show_name:
        return False
    proc = _run(
        [composer_bin, "show", show_name, "--no-interaction", "--no-ansi"],
        cwd=project_root,
        as_www_data=True,
        check=False,
        timeout_sec=180,
    )
    return proc.returncode == 0


def _normalize_console_path(project_root: str, console_path: str) -> str:
    if Path(console_path).is_absolute():
        return console_path
    return str(Path(project_root) / console_path)


def _read_php_array_string(cfg_text: str, key: str) -> str:
    pat = re.compile(rf"['\"]{re.escape(key)}['\"]\s*=>\s*['\"]([^'\"]*)['\"]", re.IGNORECASE)
    m = pat.search(cfg_text or "")
    if not m:
        return ""
    return str(m.group(1) or "").strip()


def _parse_mailer_config(root: str) -> dict[str, str]:
    project_root = _resolve_project_root(root)
    candidates = [
        Path(root) / "config" / "local.php",
        Path(root) / "app" / "config" / "local.php",
        Path(root) / "docroot" / "config" / "local.php",
        Path(project_root) / "config" / "local.php",
        Path(project_root) / "app" / "config" / "local.php",
        Path(project_root) / "docroot" / "config" / "local.php",
    ]
    seen: set[str] = set()
    for p in candidates:
        ps = str(p)
        if ps in seen:
            continue
        seen.add(ps)
        if not p.exists() or not p.is_file():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        return {
            "mailer_dsn": _read_php_array_string(txt, "mailer_dsn"),
            "mailer_transport": _read_php_array_string(txt, "mailer_transport"),
            "mail_transport": _read_php_array_string(txt, "mail_transport"),
            "email_transport": _read_php_array_string(txt, "email_transport"),
        }
    return {}


def _required_mailer_packages_from_config(root: str) -> set[str]:
    cfg = _parse_mailer_config(root)
    dsn = str(cfg.get("mailer_dsn", "") or "").strip().lower()
    transport = (
        str(cfg.get("mailer_transport", "") or "").strip().lower()
        or str(cfg.get("mail_transport", "") or "").strip().lower()
        or str(cfg.get("email_transport", "") or "").strip().lower()
    )
    out: set[str] = set()
    if (
        "mautic+ses+api://" in dsn
        or "ses+api://" in dsn
        or "amazonaws.com" in dsn
        or "amazon" in transport
        or "ses" in transport
    ):
        out.add(AMAZON_MAILER_PACKAGE)
    if "sendgrid+" in dsn or "sendgrid" in transport:
        out.add(SENDGRID_MAILER_PACKAGE)
    return out


def _normalize_bundle_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _bundle_requires_sendgrid_mailer(bundle: str) -> bool:
    if bundle in SENDGRID_MAILER_REQUIRED_BUNDLES:
        return True
    normalized = _normalize_bundle_name(bundle)
    return "sendgrid" in normalized or "sengrid" in normalized


def _required_mailer_packages_from_bundles(bundles: set[str]) -> set[str]:
    out: set[str] = set()
    for bundle in bundles:
        b = str(bundle or "").strip()
        if not b:
            continue
        if b in AMAZON_MAILER_REQUIRED_BUNDLES:
            out.update({AMAZON_MAILER_PACKAGE, HTTP_CLIENT_PACKAGE})
        if _bundle_requires_sendgrid_mailer(b):
            out.add(SENDGRID_MAILER_PACKAGE)
    return out


def installed_required_bundles(root: str) -> set[str]:
    out: set[str] = set()
    for base in plugin_dir_candidates(root):
        if not base.exists() or not base.is_dir():
            continue
        for name in AMAZON_MAILER_REQUIRED_BUNDLES | SENDGRID_MAILER_REQUIRED_BUNDLES:
            if (base / name).exists():
                out.add(name)
        for child in base.iterdir():
            if child.is_dir() and _bundle_requires_sendgrid_mailer(child.name):
                out.add(child.name)
    return out


def _ensure_mailer_packages(
    *,
    config: AgentConfig,
    root: str,
    console_path: str,
    required: set[str],
    reason: str,
    ensure_node: bool,
) -> bool:
    if not required:
        return False

    install_type = detect_install_type(root)
    project_root = _resolve_project_root(root)
    if not _composer_project_is_mautic(project_root):
        logging.warning(
            "[%s] skip mailer package composer preflight: %s/composer.json is not a valid Mautic composer project (%s)",
            root,
            project_root,
            reason,
        )
        return False
    if not _mautic_console_healthy(project_root=project_root, console_path=console_path, php_bin=config.php_bin):
        raise RuntimeError(f"Mautic console is not healthy before mailer package preflight: {project_root}")
    composer_bin = _resolve_composer_bin()
    _verify_composer_as_www_data(project_root, composer_bin)

    if ensure_node:
        _ensure_node20()

    missing = sorted([pkg for pkg in required if not _composer_has_package(project_root, composer_bin, pkg)])
    if not missing:
        logging.info("[%s] mailer packages already installed required=%s (%s)", root, ",".join(sorted(required)), reason)
        return False

    # For zip installs we also normalize Node.js runtime to v20 before composer require.
    if install_type == "zip":
        _ensure_node20()

    for pkg in missing:
        _run(
            [composer_bin, "require", pkg, "--no-interaction"],
            cwd=project_root,
            as_www_data=True,
            timeout_sec=max(int(config.command_timeout_sec or 900), 900),
        )

    if not _mautic_console_healthy(project_root=project_root, console_path=console_path, php_bin=config.php_bin):
        raise RuntimeError(f"Mautic console is not healthy after mailer package composer require: {project_root}")
    console_abs = _normalize_console_path(project_root, console_path)
    _run(
        [config.php_bin, console_abs, "cache:clear"],
        cwd=project_root,
        as_www_data=True,
        timeout_sec=max(int(config.command_timeout_sec or 900), 600),
    )
    logging.info(
        "[%s] mailer packages installed=%s (%s)",
        root,
        ",".join(missing),
        reason,
    )
    return True


def ensure_mailer_packages_for_bundles(
    *,
    config: AgentConfig,
    root: str,
    console_path: str,
    bundles: set[str],
    reason: str,
) -> bool:
    required = _required_mailer_packages_from_bundles(bundles)
    return _ensure_mailer_packages(
        config=config,
        root=root,
        console_path=console_path,
        required=required,
        reason=reason,
        ensure_node=SENDGRID_MAILER_PACKAGE in required,
    )


def ensure_amazon_mailer_for_bundles(
    *,
    config: AgentConfig,
    root: str,
    console_path: str,
    bundles: set[str],
    reason: str,
) -> bool:
    targets = set(bundles).intersection(AMAZON_MAILER_REQUIRED_BUNDLES)
    if not targets:
        return False
    return _ensure_mailer_packages(
        config=config,
        root=root,
        console_path=console_path,
        required={AMAZON_MAILER_PACKAGE, HTTP_CLIENT_PACKAGE},
        reason=reason,
        ensure_node=False,
    )


def ensure_mailer_packages_for_sender_config(
    *,
    config: AgentConfig,
    root: str,
    console_path: str,
    reason: str,
) -> bool:
    required = _required_mailer_packages_from_config(root)
    if not required:
        return False

    return _ensure_mailer_packages(
        config=config,
        root=root,
        console_path=console_path,
        required=required,
        reason=reason,
        ensure_node=SENDGRID_MAILER_PACKAGE in required,
    )


def ensure_mailer_packages(
    *,
    config: AgentConfig,
    root: str,
    packages: set[str],
    reason: str,
) -> bool:
    return _ensure_mailer_packages(
        config=config,
        root=root,
        console_path=str(Path(root) / "bin" / "console"),
        required=set(packages),
        reason=reason,
        ensure_node=SENDGRID_MAILER_PACKAGE in packages,
    )
