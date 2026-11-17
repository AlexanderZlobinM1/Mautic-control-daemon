from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from mcd_agent import __version__


RESULT_SCHEMA = "mcd-uninstall-result-v1"
RESULT_PREFIX = "MCD_UNINSTALL_RESULT="
SYSTEMD_DIR = Path("/etc/systemd/system")
WRAPPER_PATH = Path("/usr/local/bin/mcd-cli")

_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_NO_CRONTAB_RE = re.compile(r"(?:no crontab for|no crontab)", re.IGNORECASE)
_MISSING_UNIT_TEXT = (
    "not loaded", "does not exist", "not found", "not-found",
    "could not be found", "no such file or directory",
)
_PROTECTED_DELETE_ROOTS = {
    Path("/"), Path("/etc"), Path("/home"), Path("/opt"), Path("/private"),
    Path("/private/tmp"), Path("/root"), Path("/tmp"), Path("/usr"),
    Path("/usr/local"), Path("/var"),
}
_INSTALL_TOP_LEVEL = {
    ".mcd-install.json", ".mcd-runtime.json", "backups", "bin", "bridge",
    "cache", "current", "etc", "generated", "lib", "logs", "mcd-agent",
    "mcd-cli", "mcd_agent", "releases", "run", "runtime", "src", "state",
    "tmp", "var", "venv", "wheelhouse",
}
_ETC_TOP_LEVEL = {
    "campaign-whitelist.txt", "commercial.toml", "instances.d", "local-mail",
    "mcc-operation-signing.key",
    "mcd.local.toml", "mcd.toml", "mcd-agent.operator.toml",
    "mcd-agent.system.toml", "runtime.d", "segment-whitelist.txt",
}


@dataclass
class UninstallResult:
    ok: bool
    lines: list[str]
    receipt: dict[str, Any] = field(default_factory=dict)

    def machine_line(self) -> str:
        return RESULT_PREFIX + json.dumps(self.receipt, sort_keys=True, separators=(",", ":"))


class _UninstallFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, out


def _read_crontab(user: str | None = None) -> tuple[int, str, str]:
    cmd = ["crontab"]
    if user:
        cmd.extend(["-u", user])
    cmd.append("-l")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _write_crontab(content: str, user: str | None = None) -> tuple[int, str]:
    cmd = ["crontab"]
    if user:
        cmd.extend(["-u", user])
    cmd.append("-")
    proc = subprocess.run(cmd, input=content, capture_output=True, text=True)
    out = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, out


def _is_marker(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("# MCD_MANAGED") or stripped.startswith("# mcd-managed:")


def _restore_from_markers(content: str) -> tuple[str, int]:
    """Restore only complete MCD marker/comment pairs in the current crontab."""

    lines = content.splitlines(keepends=True)
    restored: list[str] = []
    count = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _is_marker(line.rstrip("\r\n")):
            restored.append(line)
            index += 1
            continue
        if index + 1 >= len(lines):
            raise _UninstallFailure(
                "cron_marker_incomplete",
                "MCD cron marker is not followed by its managed cron line",
            )
        managed = lines[index + 1]
        managed_body = managed.rstrip("\r\n")
        if not managed_body.startswith("# ") or not managed_body[2:].strip():
            raise _UninstallFailure(
                "cron_marker_incomplete",
                "MCD cron marker is followed by an invalid managed cron line",
            )
        ending = managed[len(managed_body) :]
        restored.append(managed_body[2:] + ending)
        count += 1
        index += 2
    return "".join(restored), count


def _cron_snapshot(user: str) -> tuple[str, str, int]:
    cli_user = None if user == "root" else user
    rc, stdout, stderr = _read_crontab(cli_user)
    if rc != 0:
        diagnostic = (stderr or stdout).strip()
        if _NO_CRONTAB_RE.search(diagnostic):
            return "absent", "", 0
        raise _UninstallFailure(
            "cron_read_failed",
            f"cannot read {user} crontab: rc={rc} {diagnostic}".strip(),
        )
    _restored, pairs = _restore_from_markers(stdout)
    return "present", stdout, pairs


def _restore_user_cron(
    user: str,
    recovery: list[tuple[str, str]],
) -> dict[str, Any]:
    state, _current, pairs = _cron_snapshot(user)
    if state == "absent":
        return {"status": "absent", "managed_pairs": 0, "written": False}
    if pairs == 0:
        return {"status": "unchanged", "managed_pairs": 0, "written": False}

    # Re-read immediately before the write so unrelated lines added after the
    # preflight remain in the new crontab.
    _state, current, pairs = _cron_snapshot(user)
    restored, pairs = _restore_from_markers(current)
    if pairs == 0:
        return {"status": "unchanged", "managed_pairs": 0, "written": False}
    cli_user = None if user == "root" else user
    # A non-zero crontab exit can still follow a partial write. Record the exact
    # pre-write bytes before invoking it so the caller can always reconcile.
    recovery.append((user, current))
    rc, out = _write_crontab(restored, cli_user)
    if rc != 0:
        raise _UninstallFailure(
            "cron_write_failed",
            f"cannot restore {user} crontab: rc={rc} {out}".strip(),
        )
    verify_rc, verify_stdout, verify_stderr = _read_crontab(cli_user)
    if verify_rc != 0 or verify_stdout != restored:
        diagnostic = (verify_stderr or verify_stdout).strip()
        raise _UninstallFailure(
            "cron_verify_failed",
            f"{user} crontab readback does not match restored content: "
            f"rc={verify_rc} {diagnostic}".strip(),
        )
    return {"status": "restored", "managed_pairs": pairs, "written": True}


def _restore_cron_originals(recovery: list[tuple[str, str]]) -> list[str]:
    errors: list[str] = []
    for user, original in reversed(recovery):
        cli_user = None if user == "root" else user
        rc, out = _write_crontab(original, cli_user)
        if rc != 0:
            errors.append(f"rollback {user} write rc={rc} {out}".strip())
            continue
        verify_rc, verify_stdout, verify_stderr = _read_crontab(cli_user)
        if verify_rc != 0 or verify_stdout != original:
            diagnostic = (verify_stderr or verify_stdout).strip()
            errors.append(f"rollback {user} readback rc={verify_rc} {diagnostic}".strip())
    return errors


def _service_state(service_name: str) -> dict[str, Any]:
    active_rc, active_out = _run(["systemctl", "is-active", service_name])
    active_text = active_out.strip().lower()
    if active_rc == 0:
        active = True
        present = True
    elif active_text in {"inactive", "failed", "activating", "deactivating"}:
        active = active_text in {"activating", "deactivating"}
        present = True
    elif any(token in active_text for token in _MISSING_UNIT_TEXT) or active_text == "unknown":
        active = False
        present = False
    else:
        raise _UninstallFailure(
            "service_state_failed",
            f"systemctl is-active {service_name} failed: rc={active_rc} {active_out}".strip(),
        )

    enabled_rc, enabled_out = _run(["systemctl", "is-enabled", service_name])
    enabled_text = enabled_out.strip().lower()
    if enabled_rc == 0:
        enabled = True
        present = True
    elif enabled_text in {"disabled", "static", "masked", "indirect", "generated", "transient"}:
        enabled = False
        present = True
    elif any(token in enabled_text for token in _MISSING_UNIT_TEXT) or enabled_text == "unknown":
        enabled = False
    else:
        raise _UninstallFailure(
            "service_state_failed",
            f"systemctl is-enabled {service_name} failed: rc={enabled_rc} {enabled_out}".strip(),
        )
    return {"present": present, "active": active, "enabled": enabled}


def _verify_service_inactive(service_name: str) -> None:
    state = _service_state(service_name)
    if state["active"]:
        raise _UninstallFailure(
            "service_stop_verify_failed",
            f"service remains active after stop: {service_name}",
        )


def _restart_service_after_cron_failure(service_name: str, was_active: bool) -> list[str]:
    if not was_active:
        return []
    rc, out = _run(["systemctl", "start", service_name])
    if rc != 0:
        return [f"restart service rc={rc} {out}".strip()]
    try:
        state = _service_state(service_name)
    except _UninstallFailure as exc:
        return [str(exc)]
    if not state["active"]:
        return [f"service did not become active after recovery: {service_name}"]
    return []


def _systemctl_step(action: str, service_name: str) -> dict[str, Any]:
    rc, out = _run(["systemctl", action, service_name])
    missing = rc != 0 and any(token in out.lower() for token in _MISSING_UNIT_TEXT)
    if rc != 0 and not missing:
        raise _UninstallFailure(
            f"service_{action}_failed",
            f"systemctl {action} {service_name} failed: rc={rc} {out}".strip(),
        )
    return {
        "name": f"service_{action}",
        "status": "already_absent" if missing else "ok",
        "rc": rc,
        "detail": out,
    }


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def _validate_delete_root(path: Path, *, kind: str) -> None:
    if not path.is_absolute():
        raise _UninstallFailure("unsafe_purge_path", f"{kind} path must be absolute: {path}")
    normalized = Path(os.path.normpath(str(path)))
    if normalized in _PROTECTED_DELETE_ROOTS or len(normalized.parts) < 3:
        raise _UninstallFailure("unsafe_purge_path", f"refusing broad {kind} path: {path}")
    if path.is_symlink():
        raise _UninstallFailure("unsafe_purge_path", f"refusing symlink {kind} root: {path}")
    if not path.exists():
        return
    if not path.is_dir():
        raise _UninstallFailure("unsafe_purge_path", f"{kind} path is not a directory: {path}")

    allowed = _INSTALL_TOP_LEVEL if kind == "install" else _ETC_TOP_LEVEL
    foreign: list[str] = []
    for child in path.iterdir():
        name = child.name
        if name in allowed:
            continue
        foreign.append(name)
    if foreign:
        raise _UninstallFailure(
            "foreign_path_present",
            f"refusing to purge {path}; unrecognized top-level entries: {', '.join(sorted(foreign))}",
        )


def _validate_owned_file(path: Path, install_dir: Path, *, kind: str) -> None:
    if not _path_present(path):
        return
    if path.is_symlink():
        if not _is_within(path, install_dir):
            raise _UninstallFailure(
                "foreign_path_present",
                f"refusing to remove {kind} symlink outside MCD runtime: {path}",
            )
        return
    if not path.is_file():
        raise _UninstallFailure("foreign_path_present", f"refusing non-file {kind}: {path}")
    try:
        sample = path.read_text(encoding="utf-8", errors="ignore")[:65536]
    except OSError as exc:
        raise _UninstallFailure("owned_path_read_failed", f"cannot inspect {kind} {path}: {exc}") from exc
    markers = ("mcd_agent", "MCD Agent", str(install_dir), "mcd-cli")
    if not any(marker in sample for marker in markers):
        raise _UninstallFailure("foreign_path_present", f"refusing unverified {kind}: {path}")


def _unit_paths(service_name: str) -> tuple[Path, Path, Path]:
    unit_name = service_name if service_name.endswith(".service") else f"{service_name}.service"
    unit = SYSTEMD_DIR / unit_name
    dropins = SYSTEMD_DIR / f"{unit_name}.d"
    wants = SYSTEMD_DIR / "multi-user.target.wants" / unit_name
    return unit, dropins, wants


def _validate_unit_artifacts(service_name: str, install_dir: Path) -> list[Path]:
    unit, dropins, wants = _unit_paths(service_name)
    removable: list[Path] = []
    if _path_present(unit):
        _validate_owned_file(unit, install_dir, kind="systemd unit")
        removable.append(unit)
    if _path_present(wants):
        if not wants.is_symlink() or wants.resolve(strict=False) != unit.resolve(strict=False):
            raise _UninstallFailure("foreign_path_present", f"refusing unverified systemd alias: {wants}")
        removable.append(wants)
    if _path_present(dropins):
        if dropins.is_symlink() or not dropins.is_dir():
            raise _UninstallFailure("foreign_path_present", f"refusing unverified systemd drop-in path: {dropins}")
        for child in dropins.iterdir():
            _validate_owned_file(child, install_dir, kind="systemd drop-in")
        removable.append(dropins)
    return removable


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        raise _UninstallFailure("path_remove_failed", f"cannot remove {path}: {exc}") from exc
    if _path_present(path):
        raise _UninstallFailure("path_remove_failed", f"path still exists after removal: {path}")


def _receipt(
    *, ok: bool, operation_id: str, outcome: str, service_name: str,
    install_dir: Path, etc_dir: Path, purge: bool, cron: dict[str, Any],
    steps: list[dict[str, Any]], removed_paths: list[str], preserved_paths: list[str],
    error_code: str = "", error: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "terminal": True,
        "ok": bool(ok),
        "status": "success" if ok else "failed",
        "outcome": outcome,
        "operation_id": str(operation_id or ""),
        "mcd_version": __version__,
        "service_name": service_name,
        "install_dir": str(install_dir),
        "etc_dir": str(etc_dir),
        "purge_requested": bool(purge),
        "cron": cron,
        "steps": steps,
        "removed_paths": removed_paths,
        "preserved_paths": preserved_paths,
        "error_code": error_code,
        "error": error,
    }
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    data["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
    return data


def run_uninstall(
    *,
    service_name: str = "mcd",
    install_dir: str = "/opt/mcd",
    etc_dir: str = "/etc/mcd",
    purge: bool = True,
    operation_id: str = "",
) -> UninstallResult:
    lines: list[str] = []
    steps: list[dict[str, Any]] = []
    cron: dict[str, Any] = {}
    removed_paths: list[str] = []
    preserved_paths: list[str] = []
    install = Path(install_dir)
    etc = Path(etc_dir)

    def finish_failure(exc: _UninstallFailure) -> UninstallResult:
        lines.append(f"ERROR [{exc.code}]: {exc}")
        for candidate in (install, etc):
            if _path_present(candidate) and str(candidate) not in preserved_paths:
                preserved_paths.append(str(candidate))
        receipt = _receipt(
            ok=False, operation_id=operation_id, outcome="failed", service_name=service_name,
            install_dir=install, etc_dir=etc, purge=purge, cron=cron, steps=steps,
            removed_paths=removed_paths, preserved_paths=preserved_paths,
            error_code=exc.code, error=str(exc),
        )
        return UninstallResult(ok=False, lines=lines, receipt=receipt)

    try:
        if os.geteuid() != 0:
            raise _UninstallFailure("root_required", "uninstall requires root privileges")
        if not service_name or not _SERVICE_RE.fullmatch(service_name):
            raise _UninstallFailure("invalid_service_name", f"invalid service name: {service_name!r}")
        if purge:
            _validate_delete_root(install, kind="install")
            _validate_delete_root(etc, kind="etc")
        _validate_owned_file(WRAPPER_PATH, install, kind="CLI wrapper")
        unit_artifacts = _validate_unit_artifacts(service_name, install)

        # Validate both crontabs before the first mutation.
        cron_preflight: dict[str, tuple[str, str, int]] = {}
        for user in ("root", "www-data"):
            cron_preflight[user] = _cron_snapshot(user)
            cron[user] = {
                "status": "pending" if cron_preflight[user][2] else cron_preflight[user][0],
                "managed_pairs": cron_preflight[user][2],
                "written": False,
            }

        owned_present = any((
            _path_present(install), _path_present(etc), _path_present(WRAPPER_PATH),
            bool(unit_artifacts), any(item[2] for item in cron_preflight.values()),
        ))
        if not owned_present:
            receipt = _receipt(
                ok=True, operation_id=operation_id, outcome="already_absent",
                service_name=service_name, install_dir=install, etc_dir=etc, purge=purge,
                cron=cron, steps=steps, removed_paths=removed_paths,
                preserved_paths=preserved_paths,
            )
            return UninstallResult(ok=True, lines=["MCD is already absent"], receipt=receipt)

        service_before = _service_state(service_name)
        steps.append({"name": "service_preflight", "status": "ok", **service_before})
        stop_step = _systemctl_step("stop", service_name)
        steps.append(stop_step)
        lines.append(f"systemctl stop {service_name}: rc={stop_step['rc']} {stop_step['detail']}".strip())
        _verify_service_inactive(service_name)
        steps.append({"name": "service_stop_readback", "status": "ok", "active": False})

        cron_recovery: list[tuple[str, str]] = []
        try:
            for user in ("root", "www-data"):
                cron[user] = _restore_user_cron(user, cron_recovery)
                lines.append(
                    f"{user} crontab: {cron[user]['status']} "
                    f"managed_pairs={cron[user]['managed_pairs']}"
                )
        except _UninstallFailure as cron_exc:
            rollback_errors = _restore_cron_originals(cron_recovery)
            for recovered_user, _original in cron_recovery:
                if recovered_user in cron:
                    cron[recovered_user] = {
                        **cron[recovered_user],
                        "status": "rolled_back" if not rollback_errors else "recovery_failed",
                        "written": False,
                    }
            restart_errors = _restart_service_after_cron_failure(
                service_name,
                bool(service_before["active"]),
            )
            recovery_errors = rollback_errors + restart_errors
            steps.append({
                "name": "cron_failure_recovery",
                "status": "failed" if recovery_errors else "ok",
                "cron_restored": not bool(rollback_errors),
                "service_restored": not bool(restart_errors),
                "detail": "; ".join(recovery_errors),
            })
            if recovery_errors:
                raise _UninstallFailure(
                    "cron_recovery_failed",
                    f"{cron_exc}; recovery failed: {'; '.join(recovery_errors)}",
                ) from cron_exc
            raise cron_exc

        disable_step = _systemctl_step("disable", service_name)
        steps.append(disable_step)
        lines.append(
            f"systemctl disable {service_name}: rc={disable_step['rc']} "
            f"{disable_step['detail']}".strip()
        )

        for path in unit_artifacts:
            # systemctl disable normally removes the enable alias itself.
            if not _path_present(path):
                lines.append(f"systemd artifact already absent: {path}")
                continue
            _remove_path(path)
            removed_paths.append(str(path))
            lines.append(f"removed systemd artifact: {path}")
        if unit_artifacts:
            rc, out = _run(["systemctl", "daemon-reload"])
            steps.append({"name": "daemon_reload", "status": "ok" if rc == 0 else "failed", "rc": rc, "detail": out})
            if rc != 0:
                raise _UninstallFailure(
                    "daemon_reload_failed",
                    f"systemctl daemon-reload failed: rc={rc} {out}".strip(),
                )
            lines.append(f"systemctl daemon-reload: rc={rc} {out}".strip())

        if _path_present(WRAPPER_PATH):
            _remove_path(WRAPPER_PATH)
            removed_paths.append(str(WRAPPER_PATH))
            lines.append(f"removed wrapper: {WRAPPER_PATH}")

        if purge:
            for path in (install, etc):
                if not _path_present(path):
                    continue
                _remove_path(path)
                removed_paths.append(str(path))
                lines.append(f"purged path: {path}")
        else:
            for path in (install, etc):
                if _path_present(path):
                    preserved_paths.append(str(path))
            lines.append("runtime and configuration retained (--no-purge)")

        receipt = _receipt(
            ok=True, operation_id=operation_id, outcome="removed" if purge else "disabled",
            service_name=service_name, install_dir=install, etc_dir=etc, purge=purge,
            cron=cron, steps=steps, removed_paths=removed_paths,
            preserved_paths=preserved_paths,
        )
        return UninstallResult(ok=True, lines=lines, receipt=receipt)
    except _UninstallFailure as exc:
        return finish_failure(exc)
    except OSError as exc:
        return finish_failure(_UninstallFailure("filesystem_error", str(exc)))
