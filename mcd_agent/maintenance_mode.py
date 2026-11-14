from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any
from uuid import uuid4

from mcd_agent.upgrade_os_external import run_upgrade_os_operation

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX development environments
    fcntl = None  # type: ignore[assignment]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scheduler_pause_flag(cfg: Any) -> Path:
    raw = str(getattr(cfg, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause") or "").strip()
    p = Path(raw or "/opt/mcd/var/scheduler.pause")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def scheduler_dispatch_fence_path(cfg: Any) -> Path:
    """Return the cross-process admission fence used by MCD scheduling.

    The fence is deliberately next to the existing pause flag so an upgrade
    process and a restarted daemon share the same coordination namespace.
    """
    raw = str(getattr(cfg, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause") or "").strip()
    return Path(raw or "/opt/mcd/var/scheduler.pause").parent / "maintenance.dispatch.fence.lock"


@contextmanager
def scheduler_dispatch_fence(
    cfg: Any,
    *,
    exclusive: bool = False,
    blocking: bool = True,
):
    """Coordinate scheduler admission with an upgrade maintenance window.

    Upgrade owns an exclusive lock for the complete drain/upgrade window.
    Scheduler admission and external-task adoption take a non-blocking shared
    lock; when an upgrade owns the fence they skip the operation instead of
    dispatching work after the pause flag was observed.
    """
    if not str(getattr(cfg, "scheduler_pause_flag_path", "") or "").strip():
        # Lightweight unit fixtures may not model an AgentConfig. Preserve
        # their standalone admission semantics without creating production
        # paths such as /opt/mcd during a test.
        yield True
        return
    if fcntl is None:
        # The pause flag remains the best available fallback on non-POSIX
        # development environments. Production MCD hosts use POSIX flock.
        yield True
        return
    path = scheduler_dispatch_fence_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A lightweight read-only/test config may retain the production
        # default path without owning /opt/mcd. The existing pause flag check
        # remains authoritative there; do not make admission fail merely
        # because the optional lock directory cannot be created.
        yield True
        return
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        flags |= fcntl.LOCK_NB
    acquired = False
    try:
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def cron_marker_path(cfg: Any) -> Path:
    return _scheduler_pause_flag(cfg).parent / "maintenance.cron.stopped.json"


def _read_cron_marker(cfg: Any) -> dict[str, Any]:
    p = cron_marker_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_cron_marker(cfg: Any, payload: dict[str, Any]) -> None:
    p = cron_marker_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")


def _clear_cron_marker(cfg: Any) -> None:
    p = cron_marker_path(cfg)
    if p.exists():
        p.unlink()


def stop_cron_service(
    cfg: Any,
    *,
    operation_id: str | None = None,
    target_php_version: str = "",
) -> dict[str, Any]:
    marker = _read_cron_marker(cfg)
    if marker:
        return {
            "ok": True,
            "requested": False,
            "unit": str(marker.get("unit", "") or ""),
            "was_active": bool(marker.get("was_active", False)),
            "cron_stopped": bool(marker.get("owned_stop", False)),
            "operation_id": str(marker.get("operation_id", "") or ""),
            "message": "cron ownership marker already present",
        }
    opid = str(operation_id or f"maintenance-{uuid4().hex}")
    receipt = run_upgrade_os_operation(
        cfg,
        component="service",
        operation="upgrade_service_transition",
        operation_input={
            "operation_id": opid,
            "target_php_version": str(target_php_version or ""),
            "action": "quiesce",
            "owned_stop": "false",
            "dry_run": "false",
        },
    )
    unit = str(receipt.get("cron_service") or "")
    was_active = bool(receipt.get("was_active", False))
    owned_stop = bool(receipt.get("owned_stop", False))
    if owned_stop:
        _write_cron_marker(
            cfg,
            {
                "operation_id": opid,
                "unit": unit,
                "was_active": was_active,
                "owned_stop": True,
                "stopped_at_utc": _utc_now_iso(),
            },
        )
    return {
        "ok": True,
        "requested": True,
        "unit": unit,
        "was_active": was_active,
        "cron_stopped": owned_stop,
        "operation_id": opid,
        "message": "",
    }


def restore_cron_service_if_needed(cfg: Any) -> dict[str, Any]:
    marker = _read_cron_marker(cfg)
    if not marker:
        return {
            "ok": True,
            "requested": False,
            "unit": "",
            "started": False,
            "message": "cron marker not set",
        }
    unit = str(marker.get("unit", "") or "").strip()
    was_active = bool(marker.get("was_active", False))
    opid = str(marker.get("operation_id", "") or "")
    if not opid or marker.get("owned_stop") is not True:
        return {
            "ok": False,
            "requested": True,
            "unit": unit,
            "started": False,
            "message": "cron ownership marker is incomplete",
        }
    receipt = run_upgrade_os_operation(
        cfg,
        component="service",
        operation="upgrade_service_transition",
        operation_input={
            "operation_id": opid,
            "target_php_version": "",
            "action": "recover",
            "owned_stop": "true",
            "dry_run": "false",
        },
    )
    started = bool(receipt.get("recovered", False))
    ok = bool(started or not was_active)
    if ok:
        _clear_cron_marker(cfg)
    return {
        "ok": bool(ok),
        "requested": bool(was_active),
        "unit": unit,
        "started": bool(started),
        "operation_id": opid,
        "message": "" if ok else "external cron recovery did not confirm active state",
    }


def collect_maintenance_state(cfg: Any) -> dict[str, Any]:
    pause_flag = _scheduler_pause_flag(cfg)
    paused = pause_flag.exists()
    marker = _read_cron_marker(cfg)
    receipt = run_upgrade_os_operation(
        cfg,
        component="service",
        operation="upgrade_service_transition",
        operation_input={
            "operation_id": "",
            "target_php_version": "",
            "action": "status",
            "owned_stop": "false",
            "dry_run": "false",
        },
    )
    unit = str(receipt.get("cron_service") or marker.get("unit") or "")
    active = receipt.get("active")
    cron_active = active.get(unit) if isinstance(active, dict) and unit else None
    return {
        "mode": "on" if paused else "off",
        "paused": bool(paused),
        "active": bool(paused),
        "cron_stopped": bool(marker),
        "cron_service_name": unit,
        "cron_service_active": cron_active,
        "cron_marker_present": bool(marker),
        "checked_at_utc": _utc_now_iso(),
        # Preserve the typed external observation for callers that must prove
        # the configured Debian binding was actually dispatched.  The CLI
        # keeps its established summary, while the Portal generic primitive
        # can carry this bounded, secret-free receipt in its normal terminal.
        "external_receipt": receipt,
    }


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(pid: int, grace_sec: int) -> str:
    if not _is_pid_alive(pid):
        return "already-exited"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return "already-exited"
    deadline = time.time() + max(0, int(grace_sec))
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return "terminated"
        time.sleep(0.2)
    if _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return "terminated"
    return "killed" if not _is_pid_alive(pid) else "failed"


def _tracked_running_tasks(cfg: Any) -> list[dict[str, object]]:
    try:
        from mcd_agent.daemon import TaskStore

        store = TaskStore(cfg.state_db_path, cfg)
        return [
            {
                "id": int(r.get("id") or 0),
                "root": str(r.get("root") or ""),
                "task_type": str(r.get("task_type") or ""),
                "entity_id": r.get("entity_id"),
                "pid": int(r.get("pid") or 0),
                "command_str": str(r.get("command_str") or ""),
            }
            for r in store.running_task_summaries()
        ]
    except Exception:
        return []


def _managed_instance_roots(cfg: Any) -> list[str]:
    try:
        from mcd_agent.inventory import InstanceInventory, ensure_seeded

        inventory = InstanceInventory(cfg.state_db_path)
        ensure_seeded(inventory, cfg)
        return [
            str(getattr(inst, "root", "") or "").strip()
            for inst in inventory.list_instances()
            if str(getattr(inst, "root", "") or "").strip()
        ]
    except Exception:
        return []


def _external_running_tasks(cfg: Any, tracked: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    tracked_rows = tracked if tracked is not None else _tracked_running_tasks(cfg)
    tracked_pids = {int(row.get("pid") or 0) for row in tracked_rows if int(row.get("pid") or 0) > 0}
    roots = _managed_instance_roots(cfg)
    if not roots:
        return []
    try:
        from mcd_agent.daemon import list_external_runtime_task_summaries

        return list_external_runtime_task_summaries(roots, tracked_tasks=tracked_rows, tracked_pids=tracked_pids)
    except Exception:
        return []


def observed_running_tasks(cfg: Any) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    tracked = _tracked_running_tasks(cfg)
    external = _external_running_tasks(cfg, tracked)
    return tracked, external


def _reconcile_exited_tracked_tasks(cfg: Any) -> list[dict[str, object]]:
    """Release durable running rows whose process already exited.

    Upgrade maintenance cannot safely carry a dead lease into its quiescence
    decision.  The normal daemon reconciler will also emit its richer receipt;
    this bounded pass exists for the CLI/daemon race where the upgrade guard
    owns the exclusive fence and the daemon cannot run another cycle.
    """
    try:
        from mcd_agent.daemon import TaskStore

        store = TaskStore(cfg.state_db_path, cfg)
    except Exception:
        return []
    released: list[dict[str, object]] = []
    try:
        for row in store.running_task_summaries():
            pid = int(row.get("pid") or 0)
            if pid > 0 and _is_pid_alive(pid):
                continue
            row_id = int(row.get("id") or 0)
            if row_id <= 0:
                continue
            store.finish(row_id, state="lost", rc=None, note="maintenance_stale:pid_not_alive")
            released.append(
                {
                    "root": str(row.get("root") or ""),
                    "task_type": str(row.get("task_type") or ""),
                    "entity_id": row.get("entity_id"),
                    "pid": pid,
                    "reason": "pid_not_alive",
                }
            )
    except Exception:
        return released
    finally:
        try:
            store.close()
        except Exception:
            pass
    return released


def list_mautic_console_processes() -> list[tuple[int, str]]:
    proc = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []
    out: list[tuple[int, str]] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        args = parts[1]
        if "bin/console" in args and "mautic:" in args:
            out.append((pid, args))
    return out


def stop_running_tasks_for_maintenance(
    cfg: Any,
    *,
    grace_sec: int = 30,
    kill_orphans: bool = True,
    stable_snapshots: int = 3,
    stable_interval_sec: float = 0.2,
    drain_timeout_sec: int = 30,
) -> dict[str, Any]:
    stable_required = max(2, int(stable_snapshots))
    deadline = time.monotonic() + max(1.0, float(drain_timeout_sec))
    stopped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    released_stale: list[dict[str, object]] = []
    attempts = 0
    stable_count = 0
    tracked: list[dict[str, object]] = []
    external: list[dict[str, object]] = []
    consoles: list[tuple[int, str]] = []

    while True:
        attempts += 1
        tracked, external = observed_running_tasks(cfg)
        released_stale.extend(_reconcile_exited_tracked_tasks(cfg))
        if released_stale:
            tracked, external = observed_running_tasks(cfg)
        managed = tracked + external
        seen_pids: set[int] = set()

        for task in managed:
            pid = int(task.get("pid") or 0)
            if pid <= 0 or pid in seen_pids:
                continue
            seen_pids.add(pid)
            res = _kill_pid(pid, int(grace_sec))
            row = {
                "pid": pid,
                "task_type": str(task.get("task_type") or ""),
                "entity_id": task.get("entity_id"),
                "result": res,
            }
            if res in {"terminated", "killed", "already-exited"}:
                stopped.append(row)
            else:
                failed.append(row)

        if kill_orphans:
            tracked_pids = {
                int(x.get("pid") or 0)
                for x in managed
                if int(x.get("pid") or 0) > 0
            }
            for pid, cmd in list_mautic_console_processes():
                if pid in tracked_pids or pid == os.getpid():
                    continue
                res = _kill_pid(pid, int(grace_sec))
                row = {"pid": pid, "task_type": "orphan_console", "command": cmd, "result": res}
                if res in {"terminated", "killed", "already-exited"}:
                    stopped.append(row)
                else:
                    failed.append(row)

        tracked_after, external_after = observed_running_tasks(cfg)
        consoles = list_mautic_console_processes()
        remaining = tracked_after + external_after
        if not remaining and not consoles:
            stable_count += 1
        else:
            stable_count = 0
        if stable_count >= stable_required:
            tracked, external = tracked_after, external_after
            break
        if time.monotonic() >= deadline:
            tracked, external = tracked_after, external_after
            break
        time.sleep(max(0.01, float(stable_interval_sec)))
    return {
        "ok": not failed and not (tracked or external or consoles),
        "stopped": len(stopped),
        "stop_failed": len(failed),
        "stopped_tasks": stopped,
        "failed_tasks": failed,
        "tracked_running": len(tracked),
        "external_running": len(external),
        "managed_running": len(tracked) + len(external),
        "mautic_console_total": len(consoles),
        "tracked_tasks": [
            {
                "root": str(row.get("root") or ""),
                "task_type": str(row.get("task_type") or ""),
                "entity_id": row.get("entity_id"),
                "pid": int(row.get("pid") or 0),
            }
            for row in tracked[:100]
        ],
        "external_tasks": [
            {
                "root": str(row.get("root") or ""),
                "task_type": str(row.get("task_type") or ""),
                "entity_id": row.get("entity_id"),
                "pid": int(row.get("pid") or 0),
            }
            for row in external[:100]
        ],
        "stable_snapshots": stable_count,
        "stable_snapshots_required": stable_required,
        "drain_attempts": attempts,
        "drain_timed_out": bool(time.monotonic() >= deadline and (tracked or external or consoles)),
        "released_stale_tasks": released_stale[:100],
    }
