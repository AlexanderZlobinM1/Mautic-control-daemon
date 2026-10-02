from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from mcd_agent.fs_permissions import ensure_instance_permissions
from mcd_agent.runtime_descriptor import descriptor_for_root
from mcd_agent.native_comparator import NativeComparator

SUPPORTED_COMMANDS = {
    "campaign:trigger": "mautic:campaigns:trigger",
    "segments:update": "mautic:segments:update",
    "campaign:rebuild": "mautic:campaigns:rebuild",
    # Keep backward-compatible CLI alias: update == rebuild.
    "campaigns:rebuild": "mautic:campaigns:rebuild",
    "campaigns:update": "mautic:campaigns:rebuild",
    "campaigns:trigger": "mautic:campaigns:trigger",
    "import": "mautic:import",
    "cache:clear": "cache:clear",
    "cache:warmup": "cache:warmup",
}

COMMAND_TASK_TYPES = {
    "campaign:trigger": "campaign_trigger",
    "campaigns:trigger": "campaign_trigger",
    "segments:update": "segment",
    "campaign:rebuild": "campaign_rebuild",
    "campaigns:rebuild": "campaign_rebuild",
    "campaigns:update": "campaign_rebuild",
    "import": "import",
}

_CACHE_COMMANDS = {"cache:clear", "cache:warmup"}
_PERMISSION_DENIED_RE = re.compile(r"permission denied", re.IGNORECASE)
_CACHE_PERM_WARNING_MARKER = "MCD_WARNING_CACHE_PERMISSIONS_REPAIRED"
_DISABLED_LIMIT_VALUES = {"", "0", "none", "null", "false", "off", "disabled", "unlimited", "all"}


def _resolve_console_path(root: str) -> str | None:
    runtime = descriptor_for_root(root)
    if runtime is not None:
        return runtime.console_path
    root_path = Path(root)
    console_bin = root_path / "bin" / "console"
    console_legacy = root_path / "app" / "console"

    if console_bin.exists():
        return str(console_bin)
    if console_legacy.exists():
        return str(console_legacy)
    return None


def _is_disabled_limit(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    text = str(value).strip().lower()
    if text in _DISABLED_LIMIT_VALUES:
        return True
    try:
        return int(text) <= 0
    except (TypeError, ValueError):
        return False


def _strip_option(parts: list[str], option: str) -> list[str]:
    stripped: list[str] = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part == option:
            skip_next = True
            continue
        if part.startswith(f"{option}="):
            continue
        stripped.append(part)
    return stripped


def render_mautic_command(
    *,
    php_bin: str,
    root: str,
    template: str,
    run_as_user: str | None = None,
    command_env: dict[str, str] | None = None,
    **params: object,
) -> list[str]:
    console = _resolve_console_path(root)
    if not console:
        raise FileNotFoundError(f"Console not found in root: {root}")
    render_params = dict(params)
    has_campaign_limit_param = "campaign_limit" in render_params
    campaign_limit = render_params.get("campaign_limit")
    if has_campaign_limit_param and _is_disabled_limit(campaign_limit):
        render_params.setdefault("campaign_limit_arg", "")
    elif has_campaign_limit_param:
        render_params.setdefault("campaign_limit_arg", f" --campaign-limit={campaign_limit}")
    else:
        render_params.setdefault("campaign_limit_arg", "")

    rendered = template.format(**render_params)
    parts = shlex.split(rendered)
    if has_campaign_limit_param and _is_disabled_limit(campaign_limit):
        parts = _strip_option(parts, "--campaign-limit")
    runtime = descriptor_for_root(root)
    env_args = [f"{key}={value}" for key, value in sorted((command_env or {}).items())]
    if runtime is not None:
        if not runtime.has_capability("console"):
            raise ValueError("runtime descriptor does not allow Mautic console execution")
        runtime_prefix = runtime.docker_exec_prefix()
        if env_args:
            # docker_exec_prefix ends with the PHP binary and console path.
            # Run env inside the container, immediately before PHP.
            cmd = runtime_prefix[:-2] + ["env", *env_args] + runtime_prefix[-2:]
        else:
            cmd = runtime_prefix
        cmd += parts + ["--no-interaction"]
    else:
        cmd = [php_bin, console] + parts + ["--no-interaction"]
        if env_args:
            cmd = ["env", *env_args] + cmd
    if run_as_user and runtime is None:
        cmd = ["sudo", "-u", run_as_user] + cmd
    return cmd


def command_task_type(command: str) -> str | None:
    return COMMAND_TASK_TYPES.get(command)


def build_mautic_exec_args(
    *,
    php_bin: str,
    root: str,
    command: str,
    instance_id: int | None,
    run_as_user: str | None = None,
) -> list[str]:
    if command not in SUPPORTED_COMMANDS:
        raise ValueError(f"Unsupported command: {command}")

    template = SUPPORTED_COMMANDS[command]
    if command in {"campaign:trigger", "segments:update", "campaign:rebuild", "campaigns:rebuild", "campaigns:update", "campaigns:trigger", "import"} and instance_id is not None:
        template += f" -i {instance_id}"
    return render_mautic_command(
        php_bin=php_bin,
        root=root,
        template=template,
        run_as_user=run_as_user,
    )


def execute_mautic_command(
    *,
    php_bin: str,
    root: str,
    command: str,
    instance_id: int | None,
    timeout_sec: int,
    run_as_user: str | None = None,
) -> tuple[int, str]:
    try:
        cmd = build_mautic_exec_args(
            php_bin=php_bin,
            root=root,
            command=command,
            instance_id=instance_id,
            run_as_user=run_as_user,
        )
    except ValueError as e:
        return 2, str(e)
    except FileNotFoundError as e:
        return 3, str(e)

    rc, output = _run_cmd(cmd=cmd, root=root, timeout_sec=timeout_sec)
    if (
        command in _CACHE_COMMANDS
        and rc != 0
        and _looks_like_permission_error(output)
        and descriptor_for_root(root) is None
    ):
        repaired, repair_note = _repair_cache_permissions(root=root, run_as_user=run_as_user)
        rc2, output2 = _run_cmd(cmd=cmd, root=root, timeout_sec=timeout_sec)
        combined = _combine_output(output, repair_note, output2)
        if rc2 == 0:
            marker = f"{_CACHE_PERM_WARNING_MARKER}: command={command}"
            if combined:
                combined = marker + "\n" + combined
            else:
                combined = marker
            return 0, combined
        if repaired:
            logging.warning(
                "cache command failed after auto-repair root=%s command=%s rc=%s",
                root,
                command,
                rc2,
            )
        return rc2, combined
    return rc, output


def execute_mautic_command_template(
    *,
    php_bin: str,
    root: str,
    template: str,
    timeout_sec: int,
    run_as_user: str | None = None,
    command_env: dict[str, str] | None = None,
    **params: object,
) -> tuple[int, str]:
    rendered = str(template).format(**params)
    command_name = _template_command_name(rendered)
    try:
        cmd = render_mautic_command(
            php_bin=php_bin,
            root=root,
            template=rendered,
            run_as_user=run_as_user,
            command_env=command_env,
        )
    except FileNotFoundError as e:
        return 3, str(e)

    rc, output = _run_cmd(cmd=cmd, root=root, timeout_sec=timeout_sec)
    if (
        command_name in _CACHE_COMMANDS
        and rc != 0
        and _looks_like_permission_error(output)
        and descriptor_for_root(root) is None
    ):
        repaired, repair_note = _repair_cache_permissions(root=root, run_as_user=run_as_user)
        rc2, output2 = _run_cmd(cmd=cmd, root=root, timeout_sec=timeout_sec)
        combined = _combine_output(output, repair_note, output2)
        if rc2 == 0:
            marker = f"{_CACHE_PERM_WARNING_MARKER}: command={command_name}"
            if combined:
                combined = marker + "\n" + combined
            else:
                combined = marker
            return 0, combined
        if repaired:
            logging.warning(
                "cache template command failed after auto-repair root=%s command=%s rc=%s",
                root,
                command_name,
                rc2,
            )
        return rc2, combined
    return rc, output


def _run_cmd(*, cmd: list[str], root: str, timeout_sec: int) -> tuple[int, str]:
    run_timeout = timeout_sec if timeout_sec and timeout_sec > 0 else None
    proc = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=run_timeout,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return int(proc.returncode), output.strip()


def _looks_like_permission_error(output: str) -> bool:
    return bool(_PERMISSION_DENIED_RE.search(str(output or "")))


def _repair_cache_permissions(*, root: str, run_as_user: str | None) -> tuple[bool, str]:
    user = str(run_as_user or "www-data").strip() or "www-data"
    try:
        repaired = ensure_instance_permissions(root=root, run_as_user=user)
    except Exception as e:
        logging.warning("cache permission auto-repair failed root=%s user=%s: %s", root, user, e)
        return False, f"MCD_WARN: cache-permissions repair failed ({e})"

    fixed = bool(repaired.repaired_paths or repaired.console_exec_fixed)
    details: list[str] = []
    details.append(
        "MCD_WARN: cache-permissions repair attempted "
        f"(fixed_paths={len(repaired.repaired_paths)} console_exec_fixed={1 if repaired.console_exec_fixed else 0})"
    )
    if repaired.repaired_paths:
        details.append("MCD_WARN: repaired paths: " + ", ".join(sorted(set(repaired.repaired_paths))))
    if repaired.errors:
        details.append("MCD_WARN: repair errors: " + "; ".join(repaired.errors))

    if fixed:
        logging.warning(
            "cache permission auto-repair applied root=%s user=%s repaired_paths=%s console_exec_fixed=%s",
            root,
            user,
            ",".join(sorted(set(repaired.repaired_paths))),
            repaired.console_exec_fixed,
        )
    return fixed, "\n".join(details)


def _template_command_name(rendered_template: str) -> str:
    parts = shlex.split(str(rendered_template or ""))
    if not parts:
        return ""
    return str(parts[0]).strip().lower()


def _combine_output(*parts: str) -> str:
    out = [str(x).strip() for x in parts if str(x).strip()]
    return "\n".join(out).strip()


def execute_native_comparator_cycle(
    *,
    comparator: NativeComparator,
    cycle_id: str,
    root: str,
    command: Sequence[str],
    observations: list[Mapping[str, Any]],
    after_observer: Callable[[], list[Mapping[str, Any]]],
    outcome_observer: Callable[[str], Mapping[str, Any]],
    quiescence_probe: Callable[[], bool],
    instance_uid: str,
    operation: str,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    """Run one externally scheduled native cycle through the real process path.

    The caller owns cycle creation/preflight and supplies producer-collected
    evidence.  This helper deliberately does not schedule work or use the
    combined campaign fallback; it binds the actual subprocess PID, lock and
    return code into the durable comparator receipt.
    """
    command_args = [str(part) for part in command]
    if not command_args:
        raise ValueError("native comparator command is empty")
    context = comparator.cycle_context(cycle_id)
    if context["root"] != str(root) or context["instance_uid"] != str(instance_uid) or context["operation"] != str(operation):
        raise ValueError("native comparator execution context mismatch")
    if timeout_sec <= 0:
        raise ValueError("native comparator timeout must be positive")
    with comparator.native_window(cycle_id, quiescence_probe) as lease:
        started_at = time.time()
        process: subprocess.Popen[bytes] | None = None
        try:
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(
                    command_args,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                for row in observations:
                    comparator.record_start(
                        cycle_id, str(row["object_id"]), run_id=f"native:{cycle_id}:{process.pid}",
                        pid=int(process.pid), lock_path=lease.lock_path, started_at=started_at,
                        input_generation=str(row["input_generation"]), input_fingerprint=str(row["input_fingerprint"]),
                    )
                try:
                    process.wait(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                return_code = int(process.returncode if process.returncode is not None else 124)
                output.seek(0)
                output.read(64 * 1024)
            finished_at = time.time()
            status = "ok" if return_code == 0 else "error"
            for row in observations:
                object_id = str(row["object_id"])
                outcome = dict(outcome_observer(object_id)) if return_code == 0 else {}
                comparator.record_result(
                    cycle_id, object_id, status=status, return_code=return_code,
                    processed_input_generation=outcome.get("processed_input_generation"),
                    processed_input_fingerprint=outcome.get("processed_input_fingerprint"),
                    effect_evidence=dict(outcome.get("effect_evidence") or {}),
                    finished_at=finished_at, outcome=str(outcome.get("outcome") or ("native_completed" if return_code == 0 else "native_failed")),
                )
            after = after_observer()
            return comparator.finalize(cycle_id, after, finished_at=finished_at)
        except Exception as exc:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            comparator.abort_cycle(cycle_id, f"executor_failure:{type(exc).__name__}")
            raise
