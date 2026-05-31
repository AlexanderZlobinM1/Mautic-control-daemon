from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd: list[str], timeout_sec: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)


def _scheduler_pause_flag(cfg: Any) -> Path:
    raw = str(getattr(cfg, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause") or "").strip()
    p = Path(raw or "/opt/mcd/var/scheduler.pause")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


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


def _detect_cron_unit() -> str | None:
    for unit in ("cron", "crond"):
        try:
            proc = _run(["systemctl", "show", "-p", "LoadState", "--value", unit], timeout_sec=5)
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        state = str(proc.stdout or "").strip().lower()
        if state and state != "not-found":
            return unit
    return None


def _cron_is_active(unit: str) -> bool | None:
    try:
        proc = _run(["systemctl", "is-active", unit], timeout_sec=5)
    except Exception:
        return None
    if proc.returncode == 0 and str(proc.stdout or "").strip().lower() == "active":
        return True
    if proc.returncode != 0:
        return False
    return False


def stop_cron_service(cfg: Any) -> dict[str, Any]:
    marker = _read_cron_marker(cfg)
    unit = str(marker.get("unit", "") or "").strip() or (_detect_cron_unit() or "")
    if not unit:
        return {
            "ok": False,
            "requested": True,
            "unit": "",
            "was_active": False,
            "cron_stopped": False,
            "message": "cron service unit not found (expected cron/crond)",
        }
    active = _cron_is_active(unit)
    was_active = bool(active) if active is not None else False
    stopped_ok = False
    message = ""
    if active:
        proc = _run(["systemctl", "stop", unit], timeout_sec=20)
        stopped_ok = proc.returncode == 0
        if not stopped_ok:
            message = (proc.stderr or proc.stdout or "failed to stop cron service").strip()
    else:
        stopped_ok = True

    if stopped_ok:
        _write_cron_marker(
            cfg,
            {
                "unit": unit,
                "was_active": was_active,
                "stopped_at_utc": _utc_now_iso(),
            },
        )
    return {
        "ok": bool(stopped_ok),
        "requested": True,
        "unit": unit,
        "was_active": was_active,
        "cron_stopped": bool(stopped_ok),
        "message": message,
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
    unit = str(marker.get("unit", "") or "").strip() or (_detect_cron_unit() or "")
    was_active = bool(marker.get("was_active", False))
    if not unit:
        _clear_cron_marker(cfg)
        return {
            "ok": False,
            "requested": bool(was_active),
            "unit": "",
            "started": False,
            "message": "cron service unit not found while restoring",
        }
    started = False
    message = ""
    ok = True
    if was_active:
        proc = _run(["systemctl", "start", unit], timeout_sec=20)
        ok = proc.returncode == 0
        if ok:
            started = True
        else:
            message = (proc.stderr or proc.stdout or "failed to start cron service").strip()
    _clear_cron_marker(cfg)
    return {
        "ok": bool(ok),
        "requested": bool(was_active),
        "unit": unit,
        "started": bool(started),
        "message": message,
    }


def collect_maintenance_state(cfg: Any) -> dict[str, Any]:
    pause_flag = _scheduler_pause_flag(cfg)
    paused = pause_flag.exists()
    marker = _read_cron_marker(cfg)
    unit = str(marker.get("unit", "") or "").strip() or (_detect_cron_unit() or "")
    cron_active = _cron_is_active(unit) if unit else None
    return {
        "mode": "on" if paused else "off",
        "paused": bool(paused),
        "active": bool(paused),
        "cron_stopped": bool(marker),
        "cron_service_name": unit,
        "cron_service_active": cron_active,
        "cron_marker_present": bool(marker),
        "checked_at_utc": _utc_now_iso(),
    }

