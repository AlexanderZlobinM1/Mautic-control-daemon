from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from mcd_agent.mode import (
    _read_profile_name,
    _resolve_mutable_config_path,
)


PROFILE_SELECTION_FILE = "hardware-profile-selection.json"

_PROFILE_AUTHORITY_SECTION_RE = re.compile(
    r"(?ms)^\[(?P<name>profile|hardware)\]\s*.*?(?=^\[|\Z)"
)


@dataclass(frozen=True, slots=True)
class HardwareProfileResult:
    mode: str
    previous_profile: str
    profile: str
    cpu_count: int
    memory_kib: int
    changed: bool


def read_memory_kib(path: Path = Path("/proc/meminfo")) -> int:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                return max(0, int(line.split()[1]))
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def recommended_profile(*, cpu_count: int, memory_kib: int) -> str:
    cpus = max(1, int(cpu_count or 1))
    gib = max(0.0, float(memory_kib or 0) / (1024.0 * 1024.0))
    # MemTotal is lower than the provider's nominal RAM because firmware and
    # the kernel reserve part of it. Use conservative lower bounds for the
    # common 4/8/16/32 GiB hardware classes instead of misclassifying every
    # nominal class by one tier (for example, a CX23 reports about 3.73 GiB).
    if cpus <= 1 or (gib and gib < 3.5):
        return "tiny"
    if cpus <= 2 or (gib and gib < 7):
        return "mini"
    if cpus <= 4 or (gib and gib < 14):
        return "midi"
    if cpus <= 8 or (gib and gib < 28):
        return "maxi"
    if cpus >= 24 and (not gib or gib >= 96):
        return "ultra"
    return "hiload"


def recommended_farm_profile(*, cpu_count: int, memory_kib: int) -> str:
    standard = recommended_profile(cpu_count=cpu_count, memory_kib=memory_kib)
    return f"farm-{standard}"


def _state_path(install_dir: str) -> Path:
    return Path(install_dir) / "var" / PROFILE_SELECTION_FILE


def _read_hardware_profile_name(config_path: str) -> str:
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return "auto"
    text = p.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^\[hardware\]\s*(.*?)^(?=\[|\Z)", text)
    if not m:
        return "auto"
    n = re.search(r'(?m)^\s*(?:profile|profile_name)\s*=\s*"([^"]+)"', m.group(1))
    return n.group(1).strip().lower() if n else "auto"


def _write_hardware_profile_name(config_path: str, name: str) -> None:
    p = _resolve_mutable_config_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    section = f'[hardware]\nprofile = "{name}"\n\n'
    m = re.search(r"(?ms)^\[hardware\]\s*(.*?)^(?=\[|\Z)", text)
    if not m:
        p.write_text(text + ("\n" if text and not text.endswith("\n") else "") + section, encoding="utf-8")
        return
    body = m.group(1)
    if re.search(r'(?m)^\s*(?:profile|profile_name)\s*=\s*"[^"]+"', body):
        body = re.sub(r'(?m)^\s*(?:profile|profile_name)\s*=\s*"[^"]+"', f'profile = "{name}"', body, count=1)
    else:
        body = f'profile = "{name}"\n' + body
    p.write_text(text[: m.start(1)] + body + text[m.end(1) :], encoding="utf-8")


def read_profile_selection(install_dir: str) -> dict[str, object]:
    path = _state_path(install_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"mode": "auto"}
    if not isinstance(raw, dict):
        return {"mode": "auto"}
    mode = str(raw.get("mode") or "auto").strip().lower()
    raw["mode"] = mode if mode in {"auto", "manual"} else "auto"
    return raw


def _selection_state_exists(install_dir: str) -> bool:
    path = _state_path(install_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return isinstance(raw, dict) and str(raw.get("mode") or "").strip().lower() in {
        "auto",
        "manual",
    }


def write_profile_selection(
    install_dir: str,
    *,
    mode: str,
    profile: str,
    cpu_count: int | None = None,
    memory_kib: int | None = None,
) -> None:
    clean_mode = str(mode or "").strip().lower()
    if clean_mode not in {"auto", "manual"}:
        raise ValueError("profile selection mode must be auto or manual")
    cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    memory = max(0, int(read_memory_kib() if memory_kib is None else memory_kib))
    path = _state_path(install_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "mode": clean_mode,
        "profile": str(profile or "").strip().lower(),
        "cpu_count": cpus,
        "memory_kib": memory,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def reconcile_hardware_profile(
    *,
    config_path: str,
    install_dir: str = "/opt/mcd",
    cpu_count: int | None = None,
    memory_kib: int | None = None,
) -> HardwareProfileResult:
    cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    memory = max(0, int(read_memory_kib() if memory_kib is None else memory_kib))
    current = _read_hardware_profile_name(config_path)
    state = read_profile_selection(install_dir)
    mode = str(state.get("mode") or "auto").strip().lower()
    if current == "farm":
        target = recommended_farm_profile(cpu_count=cpus, memory_kib=memory)
        _write_hardware_profile_name(config_path, target)
        write_profile_selection(
            install_dir,
            mode="manual",
            profile=target,
            cpu_count=cpus,
            memory_kib=memory,
        )
        return HardwareProfileResult("manual", current, target, cpus, memory, True)
    if mode == "manual":
        return HardwareProfileResult("manual", current, current, cpus, memory, False)

    state_exists = _selection_state_exists(install_dir)
    recorded_profile = str(state.get("profile") or "").strip().lower()
    # Preserve existing active installations when this state file is introduced:
    # their named profile predates automatic selection and is therefore treated
    # as an operator choice. A direct profile edit after auto mode was recorded
    # is likewise a manual override. `mcd-cli profile auto` explicitly writes an
    # auto marker before calling this function, so operators can always opt in.
    legacy_manual = not state_exists and current not in {"", "auto"}
    changed_outside_auto = bool(state_exists and recorded_profile and recorded_profile != current)
    if legacy_manual or changed_outside_auto:
        write_profile_selection(
            install_dir,
            mode="manual",
            profile=current,
            cpu_count=cpus,
            memory_kib=memory,
        )
        return HardwareProfileResult("manual", current, current, cpus, memory, False)

    target = recommended_profile(cpu_count=cpus, memory_kib=memory)
    changed = target != current
    if changed:
        _write_hardware_profile_name(config_path, target)
    write_profile_selection(
        install_dir,
        mode="auto",
        profile=target,
        cpu_count=cpus,
        memory_kib=memory,
    )
    return HardwareProfileResult("auto", current, target, cpus, memory, changed)


def profile_authority_snapshot(config_path: str, install_dir: str = "/opt/mcd") -> dict[str, object]:
    """Return separate desired/effective MCD and hardware profile state."""
    return {
        "mcd_profile": {
            "desired": _read_profile_name(config_path),
            "effective": _read_profile_name(config_path),
            "scope": "mautic_workload",
        },
        "hardware_service_profile": {
            "desired": _read_hardware_profile_name(config_path),
            "effective": _read_hardware_profile_name(config_path),
            "scope": "php_fpm_mysql_nginx_redis_os",
        },
        "generated_runtime_files": [
            str(_resolve_mutable_config_path(config_path)),
            str(_state_path(install_dir)),
        ],
        "instance_local_settings": ["nginx.client_max_body_size"],
    }


def capture_profile_authority(config_path: str, install_dir: str = "/opt/mcd") -> dict[str, object]:
    """Capture only the operator-selected profile authority for package swaps.

    Source rollouts must not make a profile decision.  Keep the two selected
    profile sections and the hardware-selection marker separate from the
    source tree so a deploy or rollback cannot turn an active host into the
    example/passive profile or silently re-run hardware auto-selection.
    """

    path = _resolve_mutable_config_path(config_path) if str(config_path or "").strip() else Path("")
    if not str(config_path or "").strip() or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    sections: dict[str, str | None] = {"profile": None, "hardware": None}
    for match in _PROFILE_AUTHORITY_SECTION_RE.finditer(text):
        sections[str(match.group("name"))] = match.group(0)

    selection_path = _state_path(install_dir)
    selection_exists = selection_path.exists()
    selection_bytes = selection_path.read_bytes() if selection_exists else b""
    return {
        "config_path": str(path),
        "config_sections": sections,
        "selection_path": str(selection_path),
        "selection_exists": selection_exists,
        "selection_bytes": selection_bytes,
    }


def _replace_profile_authority_sections(text: str, sections: dict[str, object]) -> str:
    """Restore profile/hardware sections without touching other config."""

    updated = text
    for name in ("profile", "hardware"):
        desired = sections.get(name)
        match = None
        for candidate in _PROFILE_AUTHORITY_SECTION_RE.finditer(updated):
            if str(candidate.group("name")) == name:
                match = candidate
                break
        if desired is None:
            if match is not None:
                updated = updated[: match.start()] + updated[match.end() :]
            continue
        raw = str(desired)
        if match is not None:
            updated = updated[: match.start()] + raw + updated[match.end() :]
        else:
            if updated and not updated.endswith("\n"):
                updated += "\n"
            updated += ("\n" if updated else "") + raw
    return updated


def restore_profile_authority(snapshot: dict[str, object] | None) -> None:
    """Restore a previously captured profile selection after a source swap.

    The function is intentionally value/path based; it contains no host or
    instance names and never recalculates a profile from local hardware.
    """

    if not isinstance(snapshot, dict) or not snapshot:
        return
    config_name = str(snapshot.get("config_path") or "").strip()
    if config_name:
        config_path = Path(config_name)
        sections = snapshot.get("config_sections")
        if config_path.exists() and isinstance(sections, dict):
            original = config_path.read_text(encoding="utf-8")
            updated = _replace_profile_authority_sections(original, sections)
            if updated != original:
                config_path.write_text(updated, encoding="utf-8")

    selection_name = str(snapshot.get("selection_path") or "").strip()
    if not selection_name:
        return
    selection_path = Path(selection_name)
    if bool(snapshot.get("selection_exists")):
        raw = snapshot.get("selection_bytes")
        if not isinstance(raw, bytes):
            raise RuntimeError("profile authority snapshot has invalid selection bytes")
        selection_path.parent.mkdir(parents=True, exist_ok=True)
        temp = selection_path.with_suffix(selection_path.suffix + ".restore-tmp")
        temp.write_bytes(raw)
        os.chmod(temp, 0o600)
        os.replace(temp, selection_path)
    elif selection_path.exists():
        selection_path.unlink()
