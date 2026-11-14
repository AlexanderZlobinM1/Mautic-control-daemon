from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request
from uuid import uuid4

from mcd_agent.config import AgentConfig
from mcd_agent.install_type import detect_install_type, plugin_dir_candidates
from mcd_agent.runtime_descriptor import descriptor_for_root
from mcd_agent.upgrade_os_external import run_upgrade_os_operation


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


def _verify_composer_as_www_data(project_root: str, composer_bin: str) -> None:
    _run(
        [composer_bin, "-V", "--no-interaction", "--no-ansi"],
        cwd=project_root,
        as_www_data=True,
        check=True,
        timeout_sec=60,
    )


def _prepare_node_runtime(config: AgentConfig) -> None:
    run_upgrade_os_operation(
        config,
        component="apt",
        operation="apt_apply",
        operation_input={
            "package_role": "node_prerequisite",
            "package_state": "present",
            "force_repo_rescan": "true",
            "dry_run": "false",
        },
    )


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


def _resolve_mailer_bridge_requirement(
    *,
    project_root: str,
    composer_bin: str,
    package_name: str,
) -> str:
    """Constrain Symfony mailer bridges to the installed Mailer minor."""
    show_name = _composer_show_name(package_name)
    bridge_names = {
        _composer_show_name(AMAZON_MAILER_PACKAGE),
        _composer_show_name(SENDGRID_MAILER_PACKAGE),
    }
    if show_name not in bridge_names:
        return package_name

    _, separator, constraint = str(package_name or "").partition(":")
    if separator and constraint.strip() not in {"", "*"}:
        return package_name

    proc = _run(
        [
            composer_bin,
            "show",
            "symfony/mailer",
            "--format=json",
            "--no-interaction",
            "--no-ansi",
        ],
        cwd=project_root,
        as_www_data=True,
        check=False,
        timeout_sec=180,
    )
    if proc.returncode != 0:
        raise RuntimeError("Cannot determine installed symfony/mailer version")

    versions: list[str] = []
    try:
        payload = json.loads(proc.stdout or "{}")
        raw_versions = payload.get("versions", []) if isinstance(payload, dict) else []
        if isinstance(raw_versions, list):
            versions.extend(str(value) for value in raw_versions)
        if isinstance(payload, dict) and payload.get("version"):
            versions.append(str(payload["version"]))
    except (TypeError, ValueError):
        versions = []

    for version in versions:
        match = re.search(r"\bv?(\d+)\.(\d+)(?:\.\d+)?\b", version)
        if match:
            return f"{show_name}:^{match.group(1)}.{match.group(2)}"

    raise RuntimeError("Cannot parse installed symfony/mailer version")


def _composer_update_targeted_package(
    *,
    project_root: str,
    composer_bin: str,
    package_name: str,
    timeout_sec: int,
) -> None:
    """Install one package without resolving unrelated private VCS repositories."""
    composer_json = Path(project_root) / "composer.json"
    composer_lock = Path(project_root) / "composer.lock"
    original_json = composer_json.read_bytes()
    original_lock = composer_lock.read_bytes() if composer_lock.exists() else None
    original_data = json.loads(original_json.decode("utf-8"))
    completed = False
    try:
        package_requirement = _resolve_mailer_bridge_requirement(
            project_root=project_root,
            composer_bin=composer_bin,
            package_name=package_name,
        )
        data = original_data
        repositories = data.get("repositories") if isinstance(data, dict) else None
        if isinstance(repositories, list):
            data["repositories"] = [
                repo for repo in repositories
                if "git.sales-snap.com" not in json.dumps(repo, ensure_ascii=False)
            ]
            composer_json.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        _run(
            [
                composer_bin,
                "require",
                package_requirement,
                "--no-update",
                "--no-interaction",
                "--no-scripts",
                "--no-progress",
            ],
            cwd=project_root, as_www_data=True, timeout_sec=timeout_sec,
        )
        _run(
            [composer_bin, "update", _composer_show_name(package_name), "--with-dependencies", "--no-interaction", "--no-scripts", "--no-progress"],
            cwd=project_root, as_www_data=True, timeout_sec=timeout_sec,
        )
        updated_data = json.loads(composer_json.read_text(encoding="utf-8"))
        if not isinstance(updated_data, dict):
            raise RuntimeError("Composer did not leave a JSON object in composer.json")
        if isinstance(repositories, list):
            updated_data["repositories"] = repositories
        elif isinstance(original_data, dict) and "repositories" not in original_data:
            updated_data.pop("repositories", None)
        composer_json.write_text(
            json.dumps(updated_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        completed = True
    except Exception:
        if original_lock is None:
            composer_lock.unlink(missing_ok=True)
        else:
            composer_lock.write_bytes(original_lock)
        raise
    finally:
        if not completed:
            composer_json.write_bytes(original_json)


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


def _ensure_composer_packages(
    *,
    config: AgentConfig,
    root: str,
    console_path: str,
    required: set[str],
    reason: str,
    ensure_node: bool,
    no_scripts: bool = False,
) -> bool:
    if not required:
        return False

    descriptor = descriptor_for_root(root)
    if descriptor is not None:
        if str(descriptor.install_type or "unknown") != "composer":
            raise RuntimeError(
                "Docker runtime packages require a Composer-based application image"
            )
        lock_path = descriptor.host_composer_lock_path
        if lock_path is None:
            raise RuntimeError(
                "Docker Composer metadata is unavailable; rebuild the image with composer.lock metadata"
            )
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Docker Composer metadata is invalid") from exc
        installed = {
            str(row.get("name") or "").strip().lower()
            for section in ("packages", "packages-dev")
            for row in (lock.get(section) or [])
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        }
        required_names = {
            str(package or "").split(":", 1)[0].strip().lower()
            for package in required
            if str(package or "").strip()
        }
        missing = sorted(required_names - installed)
        if missing:
            raise RuntimeError(
                "Docker runtime dependencies are image-managed; rebuild/synchronize the image with: "
                + ", ".join(missing)
            )
        logging.info(
            "[%s] Docker image already contains required Composer packages=%s (%s)",
            root,
            ",".join(sorted(required_names)),
            reason,
        )
        return False

    install_type = detect_install_type(root)
    project_root = _resolve_project_root(root)
    if not _composer_project_is_mautic(project_root):
        logging.warning(
            "[%s] skip composer package preflight: %s/composer.json is not a valid Mautic composer project (%s)",
            root,
            project_root,
            reason,
        )
        return False
    if not _mautic_console_healthy(project_root=project_root, console_path=console_path, php_bin=config.php_bin):
        raise RuntimeError(f"Mautic console is not healthy before composer package preflight: {project_root}")
    host_receipt = run_upgrade_os_operation(
        config,
        component="composer",
        operation="composer_host_prepare",
        operation_input={
            "operation_id": f"mailer-{uuid4().hex}",
            "working_root": project_root,
            "dry_run": "false",
        },
    )
    composer_bin = str(host_receipt.get("composer_binary") or "").strip()
    if not composer_bin or host_receipt.get("composer_dependency_commands_owned_by_mcd") is not True:
        raise RuntimeError("external composer host preparation receipt is invalid")
    _verify_composer_as_www_data(project_root, composer_bin)

    node_prepared = False
    if ensure_node:
        _prepare_node_runtime(config)
        node_prepared = True

    missing = sorted([pkg for pkg in required if not _composer_has_package(project_root, composer_bin, pkg)])
    if not missing:
        logging.info("[%s] composer packages already installed required=%s (%s)", root, ",".join(sorted(required)), reason)
        return False

    if install_type == "zip" and not node_prepared:
        _prepare_node_runtime(config)

    for pkg in missing:
        _composer_update_targeted_package(
            project_root=project_root,
            composer_bin=composer_bin,
            package_name=pkg,
            timeout_sec=max(int(config.command_timeout_sec or 900), 900),
        )

    if not _mautic_console_healthy(project_root=project_root, console_path=console_path, php_bin=config.php_bin):
        raise RuntimeError(f"Mautic console is not healthy after composer require: {project_root}")
    console_abs = _normalize_console_path(project_root, console_path)
    _run(
        [config.php_bin, console_abs, "cache:clear"],
        cwd=project_root,
        as_www_data=True,
        timeout_sec=max(int(config.command_timeout_sec or 900), 600),
    )
    logging.info(
        "[%s] composer packages installed=%s (%s)",
        root,
        ",".join(missing),
        reason,
    )
    return True


def ensure_composer_runtime_packages(
    *,
    config: AgentConfig,
    root: str,
    console_path: str,
    packages: set[str],
    reason: str,
    no_scripts: bool = True,
) -> bool:
    return _ensure_composer_packages(
        config=config,
        root=root,
        console_path=console_path,
        required=set(packages),
        reason=reason,
        ensure_node=False,
        no_scripts=no_scripts,
    )


def ensure_mailer_packages_for_bundles(
    *,
    config: AgentConfig,
    root: str,
    console_path: str,
    bundles: set[str],
    reason: str,
) -> bool:
    required = _required_mailer_packages_from_bundles(bundles)
    return _ensure_composer_packages(
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
    return _ensure_composer_packages(
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

    return _ensure_composer_packages(
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
    return _ensure_composer_packages(
        config=config,
        root=root,
        console_path=str(Path(root) / "bin" / "console"),
        required=set(packages),
        reason=reason,
        ensure_node=SENDGRID_MAILER_PACKAGE in packages,
    )
