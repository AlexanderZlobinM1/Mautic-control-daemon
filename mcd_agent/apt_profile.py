from __future__ import annotations

import configparser
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib import request as urlrequest

from mcd_agent.nginx_baseline import ensure_nginx_baseline, nginx_baseline_satisfied


def _run(cmd: list[str], *, timeout_sec: int = 120) -> subprocess.CompletedProcess[str]:
    env = {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(5, int(timeout_sec)),
        env={**os.environ, **env},
    )


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int, min_v: int = 0, max_v: int = 86400) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out < min_v:
        out = min_v
    if out > max_v:
        out = max_v
    return out


def _normalize_source_line(line: str) -> str:
    x = line.split("#", 1)[0].strip()
    x = re.sub(r"\s+", " ", x)
    return x


def _collect_list_source_lines() -> dict[str, list[str]]:
    files: list[Path] = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.list")))
    out: dict[str, list[str]] = {}
    for p in files:
        if not p.exists() or not p.is_file():
            continue
        lines: list[str] = []
        try:
            for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                n = _normalize_source_line(raw)
                if not n:
                    continue
                if n.startswith("deb "):
                    lines.append(n)
        except Exception:
            continue
        if lines:
            out[str(p)] = lines
    return out


def detect_duplicate_list_sources() -> dict[str, Any]:
    per_file = _collect_list_source_lines()
    counts: dict[str, int] = {}
    for lines in per_file.values():
        for line in lines:
            counts[line] = counts.get(line, 0) + 1
    duplicates = {k: v for k, v in counts.items() if v > 1}
    return {
        "count": int(sum(v - 1 for v in duplicates.values())),
        "items": [{"source": k, "count": v} for k, v in sorted(duplicates.items())],
    }


def dedupe_list_sources() -> dict[str, Any]:
    files: list[Path] = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.list")))
    seen: set[str] = set()
    changed_files: list[str] = []
    removed = 0
    for p in files:
        if not p.exists() or not p.is_file():
            continue
        try:
            original = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        new_lines: list[str] = []
        file_changed = False
        for line in original:
            n = _normalize_source_line(line)
            if n.startswith("deb "):
                if n in seen:
                    removed += 1
                    file_changed = True
                    continue
                seen.add(n)
            new_lines.append(line)
        if file_changed:
            p.write_text("\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8")
            changed_files.append(str(p))
    return {"changed_files": changed_files, "removed_lines": int(removed)}


def _parse_upgradable_count(raw: str) -> int:
    count = 0
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.lower().startswith("listing"):
            continue
        if x.startswith("WARNING:"):
            continue
        if "/" in x and "upgradable from:" in x:
            count += 1
    return count


def _parse_upgradable_packages(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.lower().startswith("listing"):
            continue
        if x.startswith("WARNING:"):
            continue
        if "/" not in x or "upgradable from:" not in x:
            continue
        parts = x.split()
        if len(parts) < 2:
            continue
        name = parts[0].split("/", 1)[0].strip()
        candidate = str(parts[1]).strip()
        if not name or name in seen:
            continue
        m = re.search(r"\[upgradable from:\s*([^\]]+)\]", x)
        current = m.group(1).strip() if m else ""
        out.append({"name": name, "current": current, "candidate": candidate})
        seen.add(name)
    return out


def _parse_sim_upgrade_packages(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x.startswith("Inst "):
            continue
        parts = x.split()
        if len(parts) < 2:
            continue
        name = str(parts[1]).strip()
        if not name or name in seen:
            continue
        out.append({"name": name, "current": "", "candidate": ""})
        seen.add(name)
    return out


def _parse_phasing_packages(raw: str) -> set[str]:
    out: set[str] = set()
    in_block = False
    for line in (raw or "").splitlines():
        x = line.strip()
        low = x.lower()
        if "deferred due to phasing" in low:
            in_block = True
            continue
        if not in_block:
            continue
        if not x:
            continue
        if re.match(r"^\d+\s+upgraded,\s+\d+\s+newly installed,", x):
            break
        if x.startswith("The following "):
            break
        for tok in x.split():
            name = tok.strip()
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]*$", name):
                out.add(name)
    return out


def _parse_policy_phasing_packages(raw: str) -> set[str]:
    out: set[str] = set()
    current = ""
    for line in (raw or "").splitlines():
        x = line.rstrip()
        s = x.strip()
        if not s:
            continue
        # apt-cache policy block header: "<pkg>:"
        if not x.startswith((" ", "\t")) and s.endswith(":") and "/" not in s:
            current = s[:-1].strip()
            continue
        if not current:
            continue
        if "(phased " in s.lower():
            out.add(current)
    return out


def _policy_phasing_packages(packages: set[str], timeout_sec: int = 45) -> set[str]:
    names = sorted({str(x).strip() for x in packages if str(x).strip()})
    if not names:
        return set()
    out: set[str] = set()
    # Keep command line bounded for safety.
    chunk = 50
    for i in range(0, len(names), chunk):
        part = names[i : i + chunk]
        try:
            p = _run(["apt-cache", "policy", *part], timeout_sec=timeout_sec)
            merged = f"{p.stdout or ''}\n{p.stderr or ''}"
            out.update(_parse_policy_phasing_packages(merged))
        except Exception:
            continue
    return out


def _pending_updates(timeout_sec: int = 45) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    upgradable: list[dict[str, str]] = []
    try:
        p = _run(["apt", "list", "--upgradable"], timeout_sec=timeout_sec)
        merged = f"{p.stdout or ''}\n{p.stderr or ''}"
        cnt = _parse_upgradable_count(merged)
        upgradable = _parse_upgradable_packages(merged)
        if p.returncode not in (0,):
            errors.append(f"apt_list_upgradable_rc_{p.returncode}")
            return (
                {
                    "pending_total": int(cnt),
                    "pending_regular": int(cnt),
                    "pending_phasing": 0,
                    "pending_hold": 0,
                    "pending_updates": int(cnt),
                    "upgradable_packages": upgradable,
                    "phasing_packages": [],
                    "held_packages": [],
                },
                errors,
            )
    except Exception as e:
        errors.append(f"apt_list_upgradable_exception:{e}")

    sim_stdout = ""
    try:
        p2 = _run(["apt-get", "-s", "upgrade"], timeout_sec=timeout_sec)
        sim_stdout = p2.stdout or ""
        if not upgradable:
            upgradable = _parse_sim_upgrade_packages(sim_stdout)
        cnt = len(upgradable)
        if p2.returncode not in (0, 100):
            errors.append(f"apt_get_sim_upgrade_rc_{p2.returncode}")
    except Exception as e:
        errors.append(f"apt_get_sim_upgrade_exception:{e}")
    hold_set: set[str] = set()
    try:
        p3 = _run(["apt-mark", "showhold"], timeout_sec=timeout_sec)
        if p3.returncode == 0:
            hold_set = {x.strip() for x in (p3.stdout or "").splitlines() if x.strip()}
    except Exception:
        hold_set = set()
    phasing_set = _parse_phasing_packages(sim_stdout)
    upgradable_names = {str(row.get("name", "")).strip() for row in upgradable if str(row.get("name", "")).strip()}
    # Ubuntu phased updates often appear as "kept back" without explicit phasing section.
    # Fallback to apt-cache policy phased marker, e.g. "(phased 60%)".
    if upgradable_names:
        policy_phasing = _policy_phasing_packages(upgradable_names, timeout_sec=max(10, int(timeout_sec)))
        phasing_set.update(policy_phasing)
    phasing_set = {x for x in phasing_set if x in upgradable_names}
    hold_set = {x for x in hold_set if x in upgradable_names}

    pending_regular = 0
    pending_pack_rows: list[dict[str, str]] = []
    for row in upgradable:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        tags: list[str] = []
        if name in phasing_set:
            tags.append("phasing")
        if name in hold_set:
            tags.append("hold")
        if not tags:
            pending_regular += 1
        pending_pack_rows.append(
            {
                "name": name,
                "current": str(row.get("current", "")).strip(),
                "candidate": str(row.get("candidate", "")).strip(),
                "state": "+".join(tags) if tags else "regular",
            }
        )

    payload = {
        "pending_total": int(len(pending_pack_rows)),
        "pending_regular": int(pending_regular),
        "pending_phasing": int(len(phasing_set)),
        "pending_hold": int(len(hold_set)),
        # Backward-compatible field consumed by MCC and older clients.
        "pending_updates": int(pending_regular),
        "upgradable_packages": pending_pack_rows[:200],
        "phasing_packages": sorted(phasing_set),
        "held_packages": sorted(hold_set),
    }
    return payload, errors


def _apt_update_errors(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.startswith("E:"):
            out.append(x)
        elif "NO_PUBKEY" in x or "EXPKEYSIG" in x or "not signed" in x.lower():
            out.append(x)
    dedup: list[str] = []
    seen: set[str] = set()
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        dedup.append(x)
    return dedup[:20]


def _has_ppa_marker(marker: str) -> bool:
    marker_l = marker.strip().lower()
    if not marker_l:
        return False
    for p in _active_source_files():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue
        if marker_l in txt:
            return True
    return False


def _ensure_ppa(ppa: str, *, timeout_sec: int) -> tuple[bool, str]:
    add_repo = Path("/usr/bin/add-apt-repository")
    if not add_repo.exists():
        return False, "add-apt-repository not found"
    cmd = [str(add_repo), "-y", f"ppa:{ppa}"]
    p = _run(cmd, timeout_sec=timeout_sec)
    if p.returncode == 0:
        return True, f"added ppa:{ppa}"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or f"add ppa:{ppa} failed"


_NGINX_OFFICIAL_KEY_URL = "https://nginx.org/keys/nginx_signing.key"
_NGINX_OFFICIAL_KEYRING = Path("/usr/share/keyrings/nginx-archive-keyring.gpg")
_NGINX_OFFICIAL_SOURCE = Path("/etc/apt/sources.list.d/nginx.list")
_NGINX_OFFICIAL_FINGERPRINT = "573BFD6B3D8FBC641079A6ABABF5BD827BD9BF62"


def _ubuntu_codename() -> str:
    proc = _run(["lsb_release", "-cs"], timeout_sec=10) if shutil.which("lsb_release") else None
    if proc is not None and proc.returncode == 0:
        value = (proc.stdout or "").strip().splitlines()
        if value and re.match(r"^[a-z][a-z0-9._-]+$", value[0]):
            return value[0]
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore").splitlines():
            if raw.startswith("VERSION_CODENAME="):
                value = raw.split("=", 1)[1].strip().strip('"').strip("'")
                if value and re.match(r"^[a-z][a-z0-9._-]+$", value):
                    return value
    except Exception:
        pass
    raise RuntimeError("ubuntu codename not detected")


def _gpg_keyring_has_fingerprint(path: Path, fingerprint: str) -> bool:
    if not path.exists() or not shutil.which("gpg"):
        return False
    proc = _run(["gpg", "--show-keys", "--with-colons", "--fingerprint", str(path)], timeout_sec=20)
    if proc.returncode != 0:
        return False
    expected = re.sub(r"[^A-Fa-f0-9]", "", str(fingerprint or "")).upper()
    for line in (proc.stdout or "").splitlines():
        if line.startswith("fpr:"):
            parts = line.split(":")
            if len(parts) > 9 and parts[9].strip().upper() == expected:
                return True
    return False


def _write_nginx_official_keyring(*, timeout_sec: int) -> tuple[bool, str]:
    if not shutil.which("gpg"):
        return False, "gpg not found; install gnupg2 before enabling nginx.org repository"
    try:
        with urlrequest.urlopen(_NGINX_OFFICIAL_KEY_URL, timeout=max(10, min(int(timeout_sec), 60))) as resp:
            key_bytes = resp.read()
    except Exception as e:
        return False, f"nginx key download failed:{e}"
    if not key_bytes:
        return False, "nginx key download returned empty body"
    _NGINX_OFFICIAL_KEYRING.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="nginx-signing-", suffix=".key", delete=False) as raw_tmp:
        raw_tmp.write(key_bytes)
        raw_tmp_path = Path(raw_tmp.name)
    with tempfile.NamedTemporaryFile(prefix="nginx-keyring-", suffix=".gpg", delete=False) as gpg_tmp:
        gpg_tmp_path = Path(gpg_tmp.name)
    try:
        proc = _run(["gpg", "--batch", "--yes", "--dearmor", "-o", str(gpg_tmp_path), str(raw_tmp_path)], timeout_sec=30)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            return False, msg or "nginx key dearmor failed"
        if not _gpg_keyring_has_fingerprint(gpg_tmp_path, _NGINX_OFFICIAL_FINGERPRINT):
            return False, "nginx key fingerprint mismatch"
        gpg_tmp_path.chmod(0o644)
        os.replace(str(gpg_tmp_path), str(_NGINX_OFFICIAL_KEYRING))
        return True, f"installed:{_NGINX_OFFICIAL_KEYRING}"
    finally:
        try:
            raw_tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            gpg_tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _nginx_official_repo_line(codename: str) -> str:
    return (
        "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] "
        f"https://nginx.org/packages/ubuntu {codename} nginx"
    )


def _nginx_official_repo_present() -> bool:
    txt = _all_sources_text_lower()
    return "nginx.org/packages/ubuntu" in txt and " nginx" in txt


def _source_has_ondrej_nginx_marker(text: str) -> bool:
    low = text.lower()
    return any(
        marker in low
        for marker in (
            "ondrej/nginx",
            "ppa.launchpadcontent.net/ondrej/nginx",
            "ppa.launchpad.net/ondrej/nginx",
        )
    )


def _disable_ondrej_nginx_sources() -> list[str]:
    changed: list[str] = []
    leftovers = sorted(Path("/etc/apt/sources.list.d").glob("*ondrej*nginx*.mcd-disabled-*"))
    for p in leftovers:
        try:
            p.unlink()
            changed.append(f"removed_leftover:{p}")
        except Exception:
            continue
    for p in _active_source_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not _source_has_ondrej_nginx_marker(text):
            continue
        if p.suffix == ".sources":
            try:
                p.unlink()
                changed.append(f"removed:{p}")
            except Exception:
                continue
            continue
        lines = text.splitlines()
        new_lines: list[str] = []
        touched = False
        for line in lines:
            if _source_has_ondrej_nginx_marker(line) and not line.lstrip().startswith("#"):
                new_lines.append("# disabled by mcd nginx_official_stable profile: " + line)
                touched = True
            else:
                new_lines.append(line)
        if touched:
            p.write_text("\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8")
            changed.append(str(p))
    return changed


def _active_ondrej_nginx_source_present() -> bool:
    for p in _active_source_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if _source_has_ondrej_nginx_marker(line):
                return True
    return False


def _ondrej_nginx_disabled_leftovers_present() -> bool:
    try:
        return any(Path("/etc/apt/sources.list.d").glob("*ondrej*nginx*.mcd-disabled-*"))
    except Exception:
        return False


def _nginx_official_profile_satisfied(*, remove_ondrej: bool) -> bool:
    if not _gpg_keyring_has_fingerprint(_NGINX_OFFICIAL_KEYRING, _NGINX_OFFICIAL_FINGERPRINT):
        return False
    if not _nginx_official_repo_present():
        return False
    if _ondrej_nginx_disabled_leftovers_present():
        return False
    if remove_ondrej and _active_ondrej_nginx_source_present():
        return False
    if not nginx_baseline_satisfied():
        return False
    return True


def _ensure_nginx_official_stable_repo(
    *,
    remove_ondrej: bool,
    timeout_sec: int,
) -> tuple[bool, str, list[str]]:
    actions: list[str] = []
    if remove_ondrej:
        disabled = _disable_ondrej_nginx_sources()
        if disabled:
            actions.append("disabled_ondrej_nginx_sources:" + ",".join(disabled))

    key_ok = _gpg_keyring_has_fingerprint(_NGINX_OFFICIAL_KEYRING, _NGINX_OFFICIAL_FINGERPRINT)
    if not key_ok:
        ok, msg = _write_nginx_official_keyring(timeout_sec=timeout_sec)
        if not ok:
            return False, msg, actions
        actions.append(msg)

    codename = _ubuntu_codename()
    desired = _nginx_official_repo_line(codename) + "\n"
    existing = ""
    try:
        if _NGINX_OFFICIAL_SOURCE.exists():
            existing = _NGINX_OFFICIAL_SOURCE.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        existing = ""
    if existing != desired:
        _NGINX_OFFICIAL_SOURCE.parent.mkdir(parents=True, exist_ok=True)
        _NGINX_OFFICIAL_SOURCE.write_text(desired, encoding="utf-8")
        actions.append(f"wrote:{_NGINX_OFFICIAL_SOURCE}")

    if not _nginx_official_repo_present():
        return False, "nginx.org repository source not detected after write", actions
    if not actions:
        actions.append("nginx_official_stable_repo:already_present")
    return True, "nginx_official_stable_repo:ok", actions


def _run_mariadb_repo_setup(version: str, *, timeout_sec: int) -> tuple[bool, str]:
    v = str(version or "").strip() or "mariadb-11.4"
    script = (
        "set -euo pipefail; "
        "curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup "
        f"| bash -s -- --mariadb-server-version=\"{v}\""
    )
    p = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(30, int(timeout_sec)),
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"},
    )
    if p.returncode == 0:
        return True, f"mariadb_repo_setup:{v}"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or f"mariadb_repo_setup:{v} failed"


def _run_percona_repo_setup(target: str, *, timeout_sec: int) -> tuple[bool, str]:
    ch = str(target or "").strip().lower()
    if ch not in {"ps80", "pxc80"}:
        return False, f"percona_repo_setup:unsupported_target:{ch or '-'}"
    script = (
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "deb='/tmp/percona-release_latest.generic_all.deb'; "
        "if ! command -v percona-release >/dev/null 2>&1; then "
        "curl -fsSL -o \"$deb\" https://repo.percona.com/apt/percona-release_latest.generic_all.deb; "
        "dpkg -i \"$deb\" >/dev/null 2>&1 || apt-get install -y \"$deb\" >/dev/null 2>&1; "
        "fi; "
        "percona-release disable all >/dev/null 2>&1 || true; "
        f"percona-release setup -y {ch}"
    )
    p = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(30, int(timeout_sec)),
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"},
    )
    if p.returncode == 0:
        return True, f"percona_repo_setup:{ch}"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or f"percona_repo_setup:{ch} failed"


def _run_mysql84_repo_setup(*, timeout_sec: int) -> tuple[bool, str]:
    script = (
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "if grep -Rqs 'repo.mysql.com' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then exit 0; fi; "
        "apt-get update -qq >/dev/null 2>&1 || true; "
        "apt-get install -y wget gnupg lsb-release >/dev/null 2>&1 || true; "
        "deb='/tmp/mysql-apt-config_0.8.33-1_all.deb'; "
        "wget -qO \"$deb\" https://dev.mysql.com/get/mysql-apt-config_0.8.33-1_all.deb; "
        "echo 'mysql-apt-config mysql-apt-config/select-server select mysql-8.4-lts' | debconf-set-selections || true; "
        "echo 'mysql-apt-config mysql-apt-config/select-product select Ok' | debconf-set-selections || true; "
        "dpkg -i \"$deb\" >/dev/null 2>&1 || apt-get install -y \"$deb\" >/dev/null 2>&1"
    )
    p = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(45, int(timeout_sec)),
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"},
    )
    if p.returncode == 0:
        return True, "mysql_repo_setup:8.4"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or "mysql_repo_setup:8.4 failed"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zbx_marker_path(cfg: Any | None = None) -> Path:
    if cfg is not None:
        state_db = str(getattr(cfg, "state_db_path", "") or "").strip()
        if state_db:
            return Path(state_db).parent / "zabbix-mysql-bootstrap.json"
    return Path("/opt/mcd/var/zabbix-mysql-bootstrap.json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _apt_repo_marker_path(cfg: Any | None = None) -> Path:
    if cfg is not None:
        state_db = str(getattr(cfg, "state_db_path", "") or "").strip()
        if state_db:
            return Path(state_db).parent / "apt-repo-profiles.json"
    return Path("/opt/mcd/var/apt-repo-profiles.json")


def collect_apt_repo_profiles_state(*, cfg: Any | None = None) -> dict[str, Any]:
    marker_path = _apt_repo_marker_path(cfg)
    marker = _read_json(marker_path)
    profiles = marker.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    return {
        "marker_path": str(marker_path),
        "updated_at_utc": str(marker.get("updated_at_utc", "") or ""),
        "profiles": profiles,
    }


def clear_apt_repo_profile_markers(*, cfg: Any | None = None) -> dict[str, Any]:
    marker_path = _apt_repo_marker_path(cfg)
    previous = _read_json(marker_path)
    prev_profiles = previous.get("profiles")
    prev_count = len(prev_profiles) if isinstance(prev_profiles, dict) else 0
    removed = False
    try:
        if marker_path.exists():
            marker_path.unlink()
            removed = True
    except Exception:
        removed = False
    return {
        "status": "ok",
        "removed": bool(removed),
        "profiles_count_before": int(prev_count),
        "marker_path": str(marker_path),
    }


def _active_source_files() -> list[Path]:
    files: list[Path] = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.list")))
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.sources")))
    return [p for p in files if p.exists() and p.is_file()]


def _all_sources_text_lower() -> str:
    chunks: list[str] = []
    for p in _active_source_files():
        try:
            chunks.append(p.read_text(encoding="utf-8", errors="ignore").lower())
        except Exception:
            continue
    return "\n".join(chunks)


def _has_source_markers(markers: list[str]) -> bool:
    checks = [str(x).strip().lower() for x in markers if str(x).strip()]
    if not checks:
        return False
    txt = _all_sources_text_lower()
    return any(x in txt for x in checks)


def _dpkg_installed_versions(*, timeout_sec: int = 20) -> dict[str, str]:
    proc = _run(
        ["dpkg-query", "-W", "-f=${Package}\t${Status}\t${Version}\n"],
        timeout_sec=max(10, int(timeout_sec)),
    )
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        pkg = str(parts[0]).strip().lower()
        status = str(parts[1]).strip().lower()
        ver = str(parts[2]).strip()
        if not pkg or "install ok installed" not in status:
            continue
        out[pkg] = ver
    return out


def _version_mm(raw: str) -> str:
    m = re.search(r"(\d+)\.(\d+)", str(raw or ""))
    if not m:
        return ""
    return f"{m.group(1)}.{m.group(2)}"


def _detect_db_repo_target(*, timeout_sec: int = 20) -> dict[str, str]:
    installed = _dpkg_installed_versions(timeout_sec=timeout_sec)
    if not installed:
        return {
            "profile": "none",
            "action": "none",
            "reason": "dpkg_query_failed_or_empty",
        }

    # Priority order: PXC -> Percona Server -> MariaDB -> MySQL
    for pkg, ver in installed.items():
        if pkg.startswith("percona-xtradb-cluster-server"):
            mm = _version_mm(ver)
            if mm == "8.0" or not mm:
                return {"profile": "percona_cluster_8_0", "action": "ensure", "reason": "detected_percona_cluster", "package": pkg, "version": ver}
            return {"profile": "none", "action": "none", "reason": f"unsupported_percona_cluster_{mm or 'unknown'}", "package": pkg, "version": ver}

    for pkg, ver in installed.items():
        if pkg.startswith("percona-server-server"):
            mm = _version_mm(ver)
            if mm == "8.0" or not mm:
                return {"profile": "percona_server_8_0", "action": "ensure", "reason": "detected_percona_server", "package": pkg, "version": ver}
            return {"profile": "none", "action": "none", "reason": f"unsupported_percona_server_{mm or 'unknown'}", "package": pkg, "version": ver}

    for pkg, ver in installed.items():
        if pkg == "mariadb-server" or pkg.startswith("mariadb-server-"):
            mm = _version_mm(ver)
            if mm == "11.4":
                return {"profile": "mariadb_11_4", "action": "ensure", "reason": "detected_mariadb_11_4", "package": pkg, "version": ver}
            return {"profile": "none", "action": "none", "reason": f"mariadb_{mm or 'unknown'}_no_profile", "package": pkg, "version": ver}

    for pkg, ver in installed.items():
        if pkg in {"mysql-server", "mysql-community-server"} or pkg.startswith("mysql-server-"):
            mm = _version_mm(ver)
            if mm == "8.0":
                return {"profile": "mysql_8_0", "action": "skip", "reason": "mysql_8_0_repo_unchanged", "package": pkg, "version": ver}
            if mm == "8.4":
                return {"profile": "mysql_8_4", "action": "ensure", "reason": "detected_mysql_8_4", "package": pkg, "version": ver}
            return {"profile": "none", "action": "none", "reason": f"mysql_{mm or 'unknown'}_no_profile", "package": pkg, "version": ver}

    return {"profile": "none", "action": "none", "reason": "db_stack_not_detected"}


def _repo_profile_present(profile_key: str) -> bool:
    key = str(profile_key or "").strip().lower()
    txt = _all_sources_text_lower()
    if key == "mariadb_11_4":
        for raw_line in txt.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not any(x in line for x in ("r.mariadb.com", "deb.mariadb.org", "mariadb.com", "dlm.mariadb.com")):
                continue
            if "11.4" in line or "mariadb-11.4" in line:
                return True
        return False
    if key == "percona_server_8_0":
        return ("repo.percona.com" in txt) and ("ps80" in txt or "ps-80" in txt)
    if key == "percona_cluster_8_0":
        return ("repo.percona.com" in txt) and ("pxc80" in txt or "pxc-80" in txt)
    if key == "mysql_8_4":
        return ("repo.mysql.com" in txt) and ("mysql-8.4" in txt or "mysql-8.4-lts" in txt)
    if key == "mysql_8_0":
        return True
    return False


def _apply_repo_profiles(
    profile: dict[str, Any],
    *,
    cfg: Any | None = None,
    timeout_sec: int = 180,
    force_rescan: bool = False,
) -> tuple[list[str], list[str]]:
    actions: list[str] = []
    errors: list[str] = []
    marker_path = _apt_repo_marker_path(cfg)
    marker = _read_json(marker_path)
    profiles = marker.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        marker["profiles"] = profiles

    now = _now_utc_iso()
    profile_hash = hashlib.sha256(json.dumps(profile or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _mark(
        key: str,
        *,
        status: str,
        applied: bool,
        reason: str = "",
        detected: dict[str, str] | None = None,
        attempted_once: bool | None = None,
    ) -> None:
        row = profiles.get(key)
        if not isinstance(row, dict):
            row = {}
        row["last_status"] = str(status or "")
        row["applied"] = bool(applied)
        row["profile_hash"] = profile_hash
        row["checked_at_utc"] = now
        row["last_error"] = "" if applied else str(reason or "")
        if bool(applied):
            row["applied_at_utc"] = now
        if attempted_once is None:
            row["attempted_once"] = bool(row.get("attempted_once", False)) or bool(applied)
        else:
            row["attempted_once"] = bool(attempted_once)
        if isinstance(detected, dict):
            row["detected"] = detected
        profiles[key] = row

    # DB repo profile (auto-detected by installed DB stack).
    db_enabled = _bool(profile.get("db_repo_profile_enabled", profile.get("mariadb_repo_setup_enabled")), True)
    db_once = _bool(profile.get("db_repo_profile_apply_once"), True)
    db_row = profiles.get("db_repo") if isinstance(profiles.get("db_repo"), dict) else {}
    db_same_profile = str(db_row.get("profile_hash", "") or "") == profile_hash
    if not db_enabled:
        actions.append("db_repo_profile:disabled")
    elif db_once and bool(db_row.get("applied", False)) and db_same_profile and not force_rescan:
        actions.append("db_repo_profile:skip_once")
    else:
        detected = _detect_db_repo_target(timeout_sec=max(10, int(timeout_sec)))
        det_profile = str(detected.get("profile", "none") or "none")
        det_action = str(detected.get("action", "none") or "none")
        if det_action in {"none", "skip"}:
            _mark("db_repo", status="skipped", applied=True, reason=str(detected.get("reason", "")), detected=detected)
            actions.append(f"db_repo_profile:skipped:{det_profile}")
        else:
            if _repo_profile_present(det_profile):
                _mark("db_repo", status="already_present", applied=True, detected=detected)
                actions.append(f"db_repo_profile:already_present:{det_profile}")
            else:
                ok = False
                msg = f"unsupported_repo_profile:{det_profile}"
                if det_profile == "mariadb_11_4":
                    ok, msg = _run_mariadb_repo_setup(
                        str(profile.get("mariadb_repo_setup_version", "mariadb-11.4")),
                        timeout_sec=max(30, int(timeout_sec)),
                    )
                elif det_profile == "percona_server_8_0":
                    ok, msg = _run_percona_repo_setup("ps80", timeout_sec=max(30, int(timeout_sec)))
                elif det_profile == "percona_cluster_8_0":
                    ok, msg = _run_percona_repo_setup("pxc80", timeout_sec=max(30, int(timeout_sec)))
                elif det_profile == "mysql_8_4":
                    ok, msg = _run_mysql84_repo_setup(timeout_sec=max(45, int(timeout_sec)))
                if ok:
                    _mark("db_repo", status="applied", applied=True, detected=detected)
                    actions.append(f"db_repo_profile:applied:{det_profile}")
                    actions.append(msg)
                else:
                    _mark("db_repo", status="error", applied=False, reason=msg, detected=detected, attempted_once=False)
                    errors.append(f"db_repo_profile:{msg}")

    # Ondrej PHP repo profile (independent one-time profile).
    php_enabled = _bool(profile.get("ondrej_php_profile_enabled", profile.get("ensure_ondrej_php_ppa")), True)
    php_once = _bool(profile.get("ondrej_php_profile_apply_once"), True)
    php_row = profiles.get("ondrej_php") if isinstance(profiles.get("ondrej_php"), dict) else {}
    php_same_profile = str(php_row.get("profile_hash", "") or "") == profile_hash
    if not php_enabled:
        actions.append("ondrej_php_profile:disabled")
    elif php_once and bool(php_row.get("applied", False)) and php_same_profile and not force_rescan:
        actions.append("ondrej_php_profile:skip_once")
    else:
        if _has_ppa_marker("ondrej/php"):
            _mark("ondrej_php", status="already_present", applied=True)
            actions.append("ondrej_php_profile:already_present")
        else:
            ok, msg = _ensure_ppa("ondrej/php", timeout_sec=max(20, int(timeout_sec)))
            if ok:
                _mark("ondrej_php", status="applied", applied=True)
                actions.append("ondrej_php_profile:applied")
                actions.append(msg)
            else:
                _mark("ondrej_php", status="error", applied=False, reason=msg, attempted_once=False)
                errors.append(f"ondrej_php_profile:{msg}")

    # Official stable nginx.org repo profile. This replaces the historical Ondrej
    # nginx PPA for nginx packages, while keeping Ondrej PHP independent.
    nginx_official_enabled = _bool(
        profile.get("nginx_official_stable_profile_enabled", profile.get("ensure_nginx_official_stable_repo")),
        False,
    )
    nginx_official_once = _bool(profile.get("nginx_official_stable_profile_apply_once"), True)
    nginx_official_remove_ondrej = _bool(profile.get("nginx_official_stable_remove_ondrej"), True)
    nginx_official_row = profiles.get("nginx_official_stable") if isinstance(profiles.get("nginx_official_stable"), dict) else {}
    nginx_official_same_profile = str(nginx_official_row.get("profile_hash", "") or "") == profile_hash
    if not nginx_official_enabled:
        actions.append("nginx_official_stable_profile:disabled")
    elif (
        nginx_official_once
        and bool(nginx_official_row.get("applied", False))
        and nginx_official_same_profile
        and _nginx_official_profile_satisfied(remove_ondrej=bool(nginx_official_remove_ondrej))
        and not force_rescan
    ):
        actions.append("nginx_official_stable_profile:skip_once")
    else:
        ok, msg, repo_actions = _ensure_nginx_official_stable_repo(
            remove_ondrej=bool(nginx_official_remove_ondrej),
            timeout_sec=max(30, int(timeout_sec)),
        )
        actions.extend(repo_actions)
        if ok:
            baseline = ensure_nginx_baseline(reload_service=True)
            baseline_actions = baseline.get("actions") if isinstance(baseline, dict) else []
            if isinstance(baseline_actions, list):
                actions.extend(f"nginx_runtime_baseline:{x}" for x in baseline_actions)
            if str(baseline.get("status", "") if isinstance(baseline, dict) else "").lower() == "error":
                err = str(baseline.get("error", "nginx runtime baseline failed") if isinstance(baseline, dict) else "nginx runtime baseline failed")
                _mark("nginx_official_stable", status="error", applied=False, reason=err, attempted_once=False)
                errors.append(f"nginx_official_stable_profile:{err}")
            else:
                _mark("nginx_official_stable", status="applied", applied=True)
                actions.append("nginx_official_stable_profile:applied")
        else:
            _mark("nginx_official_stable", status="error", applied=False, reason=msg, attempted_once=False)
            errors.append(f"nginx_official_stable_profile:{msg}")

    # Legacy Ondrej nginx repo profile (independent one-time profile).
    nginx_enabled = _bool(profile.get("ondrej_nginx_profile_enabled", profile.get("ensure_ondrej_nginx_ppa")), False)
    nginx_once = _bool(profile.get("ondrej_nginx_profile_apply_once"), True)
    nginx_row = profiles.get("ondrej_nginx") if isinstance(profiles.get("ondrej_nginx"), dict) else {}
    nginx_same_profile = str(nginx_row.get("profile_hash", "") or "") == profile_hash
    if nginx_official_enabled:
        actions.append("ondrej_nginx_profile:disabled_by_nginx_official_stable")
    elif not nginx_enabled:
        actions.append("ondrej_nginx_profile:disabled")
    elif nginx_once and bool(nginx_row.get("applied", False)) and nginx_same_profile and not force_rescan:
        actions.append("ondrej_nginx_profile:skip_once")
    else:
        if _has_ppa_marker("ondrej/nginx"):
            _mark("ondrej_nginx", status="already_present", applied=True)
            actions.append("ondrej_nginx_profile:already_present")
        else:
            ok, msg = _ensure_ppa("ondrej/nginx", timeout_sec=max(20, int(timeout_sec)))
            if ok:
                _mark("ondrej_nginx", status="applied", applied=True)
                actions.append("ondrej_nginx_profile:applied")
                actions.append(msg)
            else:
                _mark("ondrej_nginx", status="error", applied=False, reason=msg, attempted_once=False)
                errors.append(f"ondrej_nginx_profile:{msg}")

    marker["updated_at_utc"] = now
    _write_json(marker_path, marker)
    return actions, errors


def _normalize_unattended_blacklist(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = [str(x or "").strip() for x in raw]
    else:
        text = str(raw or "").strip()
        items = [x.strip() for x in text.split(",")] if text else []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        if not re.match(r"^[A-Za-z0-9+_.:*?-]+$", item):
            continue
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _validate_cron_expr(expr: str) -> bool:
    raw = str(expr or "").strip()
    if not raw:
        return False
    parts = raw.split()
    if len(parts) != 5:
        return False
    token_re = re.compile(r"^[0-9*/,\-]+$")
    if not all(token_re.match(x) for x in parts):
        return False
    return True


def _write_if_changed(path: Path, content: str) -> bool:
    prev = ""
    try:
        if path.exists():
            prev = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        prev = ""
    if prev == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _apply_unattended_upgrades(profile: dict[str, Any], *, timeout_sec: int = 180) -> tuple[list[str], list[str], list[str]]:
    actions: list[str] = []
    errors: list[str] = []
    changed: list[str] = []

    mode = str(profile.get("unattended_upgrade_mode", "off") or "off").strip().lower()
    if mode not in {"off", "security", "all"}:
        mode = "off"
    schedule = str(profile.get("unattended_upgrade_schedule_cron", "") or "").strip()
    if mode != "off" and not schedule:
        schedule = "30 23 * * 0"
    if schedule and not _validate_cron_expr(schedule):
        errors.append(f"unattended_upgrade_invalid_schedule:{schedule}")
        schedule = ""

    blacklist = _normalize_unattended_blacklist(
        profile.get(
            "unattended_upgrade_blacklist",
            ["mysql", "mariadb", "percona", "php", "nginx", "haproxy"],
        )
    )

    conf_auto = Path("/etc/apt/apt.conf.d/20auto-upgrades")
    conf_u = Path("/etc/apt/apt.conf.d/52mcd-unattended-upgrades")
    cron_file = Path("/etc/cron.d/mcd-unattended-upgrades")

    if mode == "off":
        auto_txt = (
            "// managed by mcd apt profile\n"
            "APT::Periodic::Update-Package-Lists \"0\";\n"
            "APT::Periodic::Unattended-Upgrade \"0\";\n"
            "APT::Periodic::Download-Upgradeable-Packages \"0\";\n"
        )
        if _write_if_changed(conf_auto, auto_txt):
            changed.append(str(conf_auto))
        if conf_u.exists():
            try:
                conf_u.unlink()
                changed.append(str(conf_u))
            except Exception as e:
                errors.append(f"unattended_upgrade_remove_conf_failed:{e}")
        if cron_file.exists():
            try:
                cron_file.unlink()
                changed.append(str(cron_file))
            except Exception as e:
                errors.append(f"unattended_upgrade_remove_cron_failed:{e}")
        actions.append("unattended_upgrade:off")
        return actions, errors, changed

    p_install = _run(["apt-get", "install", "-y", "unattended-upgrades"], timeout_sec=max(30, int(timeout_sec)))
    if p_install.returncode != 0:
        errors.append((p_install.stderr or p_install.stdout or "install unattended-upgrades failed").strip())
        actions.append("unattended_upgrade:package_install_failed")
        return actions, errors, changed

    u_lines: list[str] = [
        "// managed by mcd apt profile",
        'Unattended-Upgrade::MinimalSteps "true";',
        'Unattended-Upgrade::Remove-Unused-Dependencies "true";',
        'Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";',
        'Unattended-Upgrade::Automatic-Reboot "false";',
    ]
    if mode == "all":
        u_lines.extend(
            [
                "Unattended-Upgrade::Origins-Pattern {",
                '  "origin=*";',
                "};",
            ]
        )
    if blacklist:
        u_lines.append("Unattended-Upgrade::Package-Blacklist {")
        for item in blacklist:
            u_lines.append(f'  "{item}";')
        u_lines.append("};")
    u_txt = "\n".join(u_lines).rstrip("\n") + "\n"
    if _write_if_changed(conf_u, u_txt):
        changed.append(str(conf_u))

    if schedule:
        auto_txt = (
            "// managed by mcd apt profile (cron-scheduled)\n"
            "APT::Periodic::Update-Package-Lists \"0\";\n"
            "APT::Periodic::Unattended-Upgrade \"0\";\n"
            "APT::Periodic::Download-Upgradeable-Packages \"0\";\n"
        )
        cron_txt = (
            "# managed by mcd apt profile\n"
            f"{schedule} root /usr/bin/flock -n /run/mcd-unattended-upgrades.lock "
            "/bin/bash -lc '/usr/bin/apt-get update -qq && /usr/bin/unattended-upgrade -v' "
            ">> /var/log/unattended-upgrades/mcd-cron.log 2>&1\n"
        )
        if _write_if_changed(conf_auto, auto_txt):
            changed.append(str(conf_auto))
        if _write_if_changed(cron_file, cron_txt):
            changed.append(str(cron_file))
        actions.append(f"unattended_upgrade:scheduled:{schedule}")
    else:
        auto_txt = (
            "// managed by mcd apt profile (apt periodic)\n"
            "APT::Periodic::Update-Package-Lists \"1\";\n"
            "APT::Periodic::Unattended-Upgrade \"1\";\n"
            "APT::Periodic::Download-Upgradeable-Packages \"1\";\n"
        )
        if _write_if_changed(conf_auto, auto_txt):
            changed.append(str(conf_auto))
        if cron_file.exists():
            try:
                cron_file.unlink()
                changed.append(str(cron_file))
            except Exception as e:
                errors.append(f"unattended_upgrade_remove_cron_failed:{e}")
        actions.append("unattended_upgrade:apt_periodic")

    actions.append(f"unattended_upgrade:mode={mode}")
    return actions, errors, changed


def collect_unattended_upgrade_state() -> dict[str, Any]:
    conf_auto = Path("/etc/apt/apt.conf.d/20auto-upgrades")
    conf_u = Path("/etc/apt/apt.conf.d/52mcd-unattended-upgrades")
    cron_file = Path("/etc/cron.d/mcd-unattended-upgrades")
    state = {
        "managed_conf_present": bool(conf_u.exists()),
        "cron_present": bool(cron_file.exists()),
        "auto_upgrades_present": bool(conf_auto.exists()),
    }
    if conf_auto.exists():
        try:
            txt = conf_auto.read_text(encoding="utf-8", errors="ignore")
            state["auto_mode"] = "enabled" if '"1"' in txt else "disabled"
        except Exception:
            state["auto_mode"] = "unknown"
    else:
        state["auto_mode"] = "missing"
    return state


def _mysql_client_bin() -> str | None:
    for name in ("mariadb", "mysql"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _sql_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "''")


def _normalize_grants(raw: Any) -> list[str]:
    default = ["REPLICATION CLIENT", "PROCESS", "SHOW DATABASES", "SHOW VIEW"]
    if not isinstance(raw, list):
        return default
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().upper()
        if not s:
            continue
        if not re.match(r"^[A-Z_ ]+$", s):
            continue
        out.append(s)
    return out or default


def _is_local_mysql_host(raw: str) -> bool:
    host = str(raw or "").strip().lower()
    return host in {"", "localhost", "127.0.0.1", "::1"}


def _run_mysql_root_sql(sql: str, *, timeout_sec: int = 20, cfg: Any | None = None) -> tuple[bool, str]:
    mysql_bin = _mysql_client_bin()
    if not mysql_bin:
        return False, "mysql_client_not_found"
    candidates: list[dict[str, str]] = [
        {
            "label": "root_socket",
            "user": "",
            "password": "",
            "socket": "",
            "protocol": "socket",
        }
    ]
    # Some hosts use password-protected root; fallback to distro-maintained DB admin creds.
    debian_cnf = Path("/etc/mysql/debian.cnf")
    if debian_cnf.exists() and debian_cnf.is_file():
        try:
            parser = configparser.RawConfigParser(interpolation=None)
            parser.read(debian_cnf, encoding="utf-8")
            sections = [s for s in parser.sections()]
            if "" not in sections:
                sections.insert(0, "")
            seen: set[tuple[str, str, str]] = set()
            for sec in sections:
                user = parser.get(sec, "user", fallback="").strip() if sec else parser.defaults().get("user", "").strip()
                password = parser.get(sec, "password", fallback="").strip() if sec else parser.defaults().get("password", "").strip()
                socket = parser.get(sec, "socket", fallback="").strip() if sec else parser.defaults().get("socket", "").strip()
                if not user:
                    continue
                key = (user, password, socket)
                if key in seen:
                    continue
                seen.add(key)
                label = f"debian_cnf:{sec or 'defaults'}"
                candidates.append(
                    {
                        "label": label,
                        "user": user,
                        "password": password,
                        "socket": socket,
                        "protocol": "socket",
                    }
                )
        except Exception:
            pass

    # Optional fallback: MCD runtime state DB credentials can be a valid local
    # DB-admin source on some hosts even when root socket auth is disabled.
    if cfg is not None:
        try:
            rt_user = str(getattr(cfg, "state_mysql_user", "") or "").strip()
            rt_password = str(getattr(cfg, "state_mysql_password", "") or "")
            rt_socket = str(getattr(cfg, "state_mysql_unix_socket", "") or "").strip()
            rt_host = str(getattr(cfg, "state_mysql_host", "") or "").strip() or "127.0.0.1"
            rt_port = int(getattr(cfg, "state_mysql_port", 3306) or 3306)
            if rt_user:
                if rt_socket:
                    candidates.append(
                        {
                            "label": "runtime_state:socket",
                            "user": rt_user,
                            "password": rt_password,
                            "socket": rt_socket,
                            "protocol": "socket",
                        }
                    )
                else:
                    candidates.append(
                        {
                            "label": "runtime_state:tcp",
                            "user": rt_user,
                            "password": rt_password,
                            "host": rt_host,
                            "port": str(rt_port),
                            "protocol": "tcp",
                        }
                    )
        except Exception:
            pass

    errors: list[str] = []
    for cand in candidates:
        protocol = str(cand.get("protocol", "socket") or "socket").strip().lower()
        cmd = [mysql_bin, "--batch", "--skip-column-names"]
        if protocol == "tcp":
            host = str(cand.get("host", "")).strip() or "127.0.0.1"
            port = str(cand.get("port", "")).strip()
            cmd.extend(["--protocol=tcp", "-h", host])
            if port:
                cmd.extend(["-P", port])
        else:
            cmd.append("--protocol=socket")
        user = str(cand.get("user", "")).strip()
        password = str(cand.get("password", ""))
        socket = str(cand.get("socket", "")).strip()
        if protocol != "tcp" and socket:
            cmd.extend(["--socket", socket])
        if user:
            cmd.extend(["-u", user])
        if password:
            cmd.append(f"-p{password}")
        cmd.extend(["-e", sql])
        proc = _run(cmd, timeout_sec=timeout_sec)
        if proc.returncode == 0:
            return True, (proc.stdout or "").strip()
        detail = (proc.stderr or proc.stdout or "").strip() or f"mysql_exec_failed_rc_{proc.returncode}"
        errors.append(f"{cand.get('label', 'unknown')}:{detail}")

    return False, " | ".join(errors[:3])


def _needs_validate_password_policy_workaround(detail: str) -> bool:
    txt = str(detail or "").strip().lower()
    if not txt:
        return False
    return (
        "error 1819" in txt
        or "does not satisfy the current policy requirements" in txt
        or ("validate_password" in txt and "policy" in txt)
    )


def _mysql_get_validate_password_policy(*, timeout_sec: int = 12, cfg: Any | None = None) -> tuple[bool, str, str]:
    ok, detail = _run_mysql_root_sql(
        "SHOW VARIABLES LIKE 'validate_password.policy';",
        timeout_sec=timeout_sec,
        cfg=cfg,
    )
    if not ok:
        return False, "", str(detail or "validate_password_policy_probe_failed")
    policy = ""
    for raw in (detail or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = re.split(r"\s+", line)
        if len(parts) >= 2 and parts[0].strip().lower() == "validate_password.policy":
            policy = parts[1].strip()
            break
    if not policy:
        return False, "", f"validate_password_policy_not_found:{(detail or '').strip()}"
    return True, policy, ""


def _run_sql_with_validate_password_policy_relax(
    sql: str,
    *,
    timeout_sec: int = 20,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    policy_ok, previous_policy_raw, policy_err = _mysql_get_validate_password_policy(
        timeout_sec=max(5, int(timeout_sec)),
        cfg=cfg,
    )
    if not policy_ok:
        return False, str(policy_err or "validate_password_policy_probe_failed")

    previous_policy = str(previous_policy_raw or "").strip().upper() or "MEDIUM"
    lowered = False
    if previous_policy != "LOW":
        ok_low, low_detail = _run_mysql_root_sql(
            "SET GLOBAL validate_password.policy='LOW';",
            timeout_sec=max(5, int(timeout_sec)),
            cfg=cfg,
        )
        if not ok_low:
            return False, f"validate_password_policy_set_low_failed:{str(low_detail or '').strip()}"
        lowered = True

    ok_apply, apply_detail = _run_mysql_root_sql(sql, timeout_sec=max(8, int(timeout_sec)), cfg=cfg)

    restore_warn = ""
    if lowered:
        ok_restore, restore_detail = _run_mysql_root_sql(
            f"SET GLOBAL validate_password.policy='{_sql_escape(previous_policy)}';",
            timeout_sec=max(5, int(timeout_sec)),
            cfg=cfg,
        )
        if not ok_restore:
            restore_warn = f"validate_password_policy_restore_failed:{str(restore_detail or '').strip()}"

    if not ok_apply:
        reason = f"apply_failed_under_relaxed_policy:{str(apply_detail or '').strip()}"
        if restore_warn:
            reason = f"{reason} | {restore_warn}"
        return False, reason

    detail = str(apply_detail or "").strip()
    if restore_warn:
        detail = f"{detail} | {restore_warn}" if detail else restore_warn
    return True, detail


def _mysql_user_exists(
    user: str,
    host: str,
    *,
    timeout_sec: int = 12,
    cfg: Any | None = None,
) -> tuple[bool | None, str]:
    sql = (
        "SELECT 1 FROM mysql.user "
        f"WHERE User='{_sql_escape(user)}' AND Host='{_sql_escape(host)}' LIMIT 1;"
    )
    ok, detail = _run_mysql_root_sql(sql, timeout_sec=timeout_sec, cfg=cfg)
    if not ok:
        return None, detail
    exists = any(x.strip() == "1" for x in (detail or "").splitlines())
    return exists, ""


def collect_zabbix_mysql_monitor_state(*, cfg: Any | None = None) -> dict[str, Any]:
    marker_path = _zbx_marker_path(cfg)
    marker = _read_json(marker_path)
    out = {
        "status": str(marker.get("last_status", "unknown") or "unknown"),
        "applied": bool(marker.get("applied", False)),
        "user": str(marker.get("user", "zbx_monitor") or "zbx_monitor"),
        "host": str(marker.get("host", "127.0.0.1") or "127.0.0.1"),
        "attempted_at_utc": str(marker.get("attempted_at_utc", "") or ""),
        "applied_at_utc": str(marker.get("applied_at_utc", "") or ""),
        "last_error": str(marker.get("last_error", "") or ""),
        "marker_path": str(marker_path),
    }
    return out


def ensure_zabbix_mysql_monitor_user(
    profile: dict[str, Any] | None,
    *,
    cfg: Any | None = None,
    force: bool = False,
    timeout_sec: int = 20,
) -> dict[str, Any]:
    prof = profile if isinstance(profile, dict) else {}
    enabled = _bool(prof.get("zabbix_mysql_monitor_enabled"), True)
    user = str(prof.get("zabbix_mysql_monitor_user", "zbx_monitor") or "zbx_monitor").strip() or "zbx_monitor"
    host = str(prof.get("zabbix_mysql_monitor_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
    password = str(prof.get("zabbix_mysql_monitor_password", "zbx_monitor") or "zbx_monitor")
    grants = _normalize_grants(prof.get("zabbix_mysql_monitor_grants"))
    apply_once = _bool(prof.get("zabbix_mysql_monitor_apply_once"), True)
    marker_path = _zbx_marker_path(cfg)
    marker = _read_json(marker_path)

    if not enabled:
        result = {
            "status": "disabled",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "reason": "zabbix_mysql_monitor_disabled",
        }
        marker.update(
            {
                "last_status": "disabled",
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "last_error": "",
            }
        )
        _write_json(marker_path, marker)
        return result

    if apply_once and not force and bool(marker.get("applied", False)):
        return {
            "status": "noop",
            "reason": "already_applied_once",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "applied_at_utc": str(marker.get("applied_at_utc", "") or ""),
        }

    exists_before, exists_err = _mysql_user_exists(user, host, timeout_sec=max(5, int(timeout_sec)), cfg=cfg)
    if exists_before is True:
        marker.update(
            {
                "last_status": "already_present",
                "applied": True,
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "applied_at_utc": str(marker.get("applied_at_utc") or _now_utc_iso()),
                "last_error": "",
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "already_present",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "user_exists_before": True,
            "user_exists_after": True,
        }
    if exists_before is None:
        marker.update(
            {
                "last_status": "error",
                "applied": bool(marker.get("applied", False)),
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
                "last_error": str(exists_err or "mysql_user_probe_failed"),
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": str(exists_err or "mysql_user_probe_failed"),
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    if apply_once and not force and bool(marker.get("attempted_once", False)):
        return {
            "status": "skipped",
            "reason": "already_attempted_once",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "last_status": str(marker.get("last_status", "") or ""),
            "last_error": str(marker.get("last_error", "") or ""),
        }

    grants_sql = ", ".join(grants)
    sql = "\n".join(
        [
            f"CREATE USER IF NOT EXISTS '{_sql_escape(user)}'@'{_sql_escape(host)}' IDENTIFIED BY '{_sql_escape(password)}';",
            f"GRANT {grants_sql} ON *.* TO '{_sql_escape(user)}'@'{_sql_escape(host)}';",
            "FLUSH PRIVILEGES;",
        ]
    )
    ok, detail = _run_mysql_root_sql(sql, timeout_sec=max(8, int(timeout_sec)), cfg=cfg)
    validate_password_policy_relaxed = False
    if not ok and _needs_validate_password_policy_workaround(detail):
        wrk_ok, wrk_detail = _run_sql_with_validate_password_policy_relax(
            sql,
            timeout_sec=max(8, int(timeout_sec)),
            cfg=cfg,
        )
        if wrk_ok:
            ok = True
            detail = wrk_detail
            validate_password_policy_relaxed = True
        else:
            detail = f"{str(detail or '').strip()} | {str(wrk_detail or '').strip()}".strip(" |")
    attempted_at = _now_utc_iso()
    if not ok:
        marker.update(
            {
                "last_status": "error",
                "applied": bool(marker.get("applied", False)),
                "user": user,
                "host": host,
                "attempted_at_utc": attempted_at,
                "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
                "last_error": str(detail or "mysql_exec_failed"),
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": str(detail or "mysql_exec_failed"),
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    exists_after, verify_err = _mysql_user_exists(user, host, timeout_sec=max(5, int(timeout_sec)), cfg=cfg)
    if exists_after is not True:
        reason = str(verify_err or "mysql_user_not_visible_after_apply")
        marker.update(
            {
                "last_status": "error",
                "applied": False,
                "user": user,
                "host": host,
                "attempted_at_utc": attempted_at,
                "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
                "last_error": reason,
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": reason,
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    marker.update(
        {
            "last_status": "applied",
            "applied": True,
            "user": user,
            "host": host,
            "attempted_at_utc": attempted_at,
            "applied_at_utc": _now_utc_iso(),
            "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
            "last_error": "",
        }
    )
    _write_json(marker_path, marker)
    return {
        "status": "applied",
        "user": user,
        "host": host,
        "marker_path": str(marker_path),
        "user_exists_before": False,
        "user_exists_after": True,
        "grants": grants,
        "validate_password_policy_relaxed": bool(validate_password_policy_relaxed),
    }


def collect_apt_state(
    *,
    timeout_sec: int = 45,
    cfg: Any | None = None,
    auto_bootstrap_zabbix: bool = True,
) -> dict[str, Any]:
    pending_info, pending_err = _pending_updates(timeout_sec=max(10, int(timeout_sec)))
    pending = int(pending_info.get("pending_updates", 0) or 0)
    pending_total = int(pending_info.get("pending_total", pending) or pending)
    pending_phasing = int(pending_info.get("pending_phasing", 0) or 0)
    pending_hold = int(pending_info.get("pending_hold", 0) or 0)
    duplicates = detect_duplicate_list_sources()
    errors: list[str] = list(pending_err)
    dup_count = int(duplicates.get("count", 0) or 0)
    if dup_count > 0:
        errors.append(f"duplicate_apt_sources:{dup_count}")

    status = "ok"
    if errors:
        status = "error"
    elif pending > 0:
        status = "updates_pending"
    elif pending_phasing > 0 or pending_hold > 0:
        status = "updates_deferred"

    if status == "ok":
        level = 0
    elif status == "updates_deferred":
        level = 2
    else:
        level = 5
    zbx_payload = (
        ensure_zabbix_mysql_monitor_user({}, cfg=cfg, force=False, timeout_sec=min(12, max(5, int(timeout_sec))))
        if bool(auto_bootstrap_zabbix)
        else collect_zabbix_mysql_monitor_state(cfg=cfg)
    )
    return {
        "status": status,
        "level": level,
        "pending_total": int(pending_total),
        "pending_updates": int(pending),
        "pending_regular": int(pending),
        "pending_phasing": int(pending_phasing),
        "pending_hold": int(pending_hold),
        "error_count": int(len(errors)),
        "errors": errors[:20],
        "duplicate_sources": duplicates,
        "upgradable_packages": list(pending_info.get("upgradable_packages", []) or [])[:200],
        "phasing_packages": list(pending_info.get("phasing_packages", []) or [])[:200],
        "held_packages": list(pending_info.get("held_packages", []) or [])[:200],
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zabbix_mysql_monitor": zbx_payload,
        "repo_profiles": collect_apt_repo_profiles_state(cfg=cfg),
        "unattended_upgrade": collect_unattended_upgrade_state(),
    }


def apply_apt_profile(
    profile: dict[str, Any],
    *,
    dry_run: bool = False,
    cfg: Any | None = None,
    force_zabbix_bootstrap: bool = False,
    force_repo_rescan: bool = False,
) -> dict[str, Any]:
    if profile is None:
        profile = {}
    if dry_run:
        current = collect_apt_state(timeout_sec=25, cfg=cfg, auto_bootstrap_zabbix=False)
        return {
            "status": "planned",
            "actions": [
                "remove_third_party_sources",
                "dedupe_list_sources",
                "repo_profiles",
                "nginx_official_stable_repo",
                "unattended_upgrades",
                "repair_repos_on_error",
                "apt_update",
                "optional_package_ops",
                "zabbix_mysql_monitor_bootstrap",
            ],
            "current": current,
        }

    actions: list[str] = []
    changed: list[str] = []
    errors: list[str] = []

    refresh_cache = _bool(profile.get("refresh_cache"), True)
    refresh_timeout_sec = _int(profile.get("refresh_timeout_sec"), 180, 30, 3600)
    repair_on_error = _bool(profile.get("repair_on_error"), True)
    remove_third_party = _bool(profile.get("remove_third_party_sources"), True)
    dedupe_sources = _bool(profile.get("dedupe_list_sources"), True)
    mariadb_repair = _bool(profile.get("mariadb_repo_setup_enabled"), True)
    mariadb_version = str(profile.get("mariadb_repo_setup_version", "mariadb-11.4"))
    ensure_php_ppa = _bool(profile.get("ensure_ondrej_php_ppa"), False)
    ensure_nginx_ppa = _bool(profile.get("ensure_ondrej_nginx_ppa"), False)
    ensure_apache_ppa = _bool(profile.get("ensure_ondrej_apache_ppa"), False)
    db_repo_profile_enabled = _bool(profile.get("db_repo_profile_enabled", mariadb_repair), True)
    ondrej_php_profile_enabled = _bool(profile.get("ondrej_php_profile_enabled", ensure_php_ppa), True)
    ondrej_nginx_profile_enabled = _bool(profile.get("ondrej_nginx_profile_enabled", ensure_nginx_ppa), False)
    nginx_official_stable_profile_enabled = _bool(
        profile.get("nginx_official_stable_profile_enabled", profile.get("ensure_nginx_official_stable_repo")),
        False,
    )
    upgrade_mode = str(profile.get("upgrade_mode", "none")).strip().lower() or "none"
    packages_present = [str(x).strip() for x in list(profile.get("packages_present", []) or []) if str(x).strip()]
    packages_absent = [str(x).strip() for x in list(profile.get("packages_absent", []) or []) if str(x).strip()]

    if remove_third_party:
        p = Path("/etc/apt/sources.list.d/third-party.sources")
        if p.exists():
            p.unlink()
            changed.append(str(p))
            actions.append("removed:/etc/apt/sources.list.d/third-party.sources")

    if dedupe_sources:
        dedupe = dedupe_list_sources()
        removed = int(dedupe.get("removed_lines", 0) or 0)
        if removed > 0:
            actions.append(f"dedupe_list_sources:removed={removed}")
        for f in list(dedupe.get("changed_files", []) or []):
            changed.append(str(f))

    repo_actions, repo_errors = _apply_repo_profiles(
        profile,
        cfg=cfg,
        timeout_sec=max(30, int(refresh_timeout_sec)),
        force_rescan=bool(force_repo_rescan),
    )
    actions.extend(repo_actions)
    errors.extend(repo_errors)
    unattended_actions, unattended_errors, unattended_changed = _apply_unattended_upgrades(
        profile, timeout_sec=max(30, int(refresh_timeout_sec))
    )
    actions.extend(unattended_actions)
    errors.extend(unattended_errors)
    changed.extend(unattended_changed)

    def _run_update() -> list[str]:
        if not refresh_cache:
            return []
        p = _run(["apt-get", "update"], timeout_sec=refresh_timeout_sec)
        merged = f"{p.stdout or ''}\n{p.stderr or ''}"
        errs = _apt_update_errors(merged)
        if p.returncode != 0 and not errs:
            errs = [f"apt-get update failed rc={p.returncode}"]
        return errs

    update_errors = _run_update()
    if update_errors:
        errors.extend(update_errors)

    if repair_on_error and update_errors:
        fixed_any = False
        txt = "\n".join(update_errors).lower()
        if (not db_repo_profile_enabled) and mariadb_repair and ("mariadb" in txt or "r.mariadb.com" in txt):
            ok, msg = _run_mariadb_repo_setup(mariadb_version, timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)

        if (not ondrej_php_profile_enabled) and ensure_php_ppa and ("ondrej" in txt or "php" in txt) and not _has_ppa_marker("ondrej/php"):
            ok, msg = _ensure_ppa("ondrej/php", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)
        if (
            (not nginx_official_stable_profile_enabled)
            and (not ondrej_nginx_profile_enabled)
            and ensure_nginx_ppa
            and ("ondrej" in txt or "nginx" in txt)
            and not _has_ppa_marker("ondrej/nginx")
        ):
            ok, msg = _ensure_ppa("ondrej/nginx", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)
        if ensure_apache_ppa and ("ondrej" in txt or "apache" in txt or "apache2" in txt) and not _has_ppa_marker("ondrej/apache2"):
            ok, msg = _ensure_ppa("ondrej/apache2", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)

        if fixed_any:
            retry_errors = _run_update()
            errors = [e for e in errors if not e.startswith("E:") and "apt-get update failed" not in e]
            errors.extend(retry_errors)
            if not retry_errors:
                actions.append("apt_update_repair_ok")

    if packages_present:
        cmd = ["apt-get", "install", "-y"] + packages_present
        p = _run(cmd, timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append(f"packages_present:{len(packages_present)}")
        else:
            errors.append((p.stderr or p.stdout or "apt install failed").strip())
    if packages_absent:
        cmd = ["apt-get", "purge", "-y"] + packages_absent
        p = _run(cmd, timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append(f"packages_absent:{len(packages_absent)}")
        else:
            errors.append((p.stderr or p.stdout or "apt purge failed").strip())

    if upgrade_mode in {"safe", "upgrade"}:
        p = _run(["apt-get", "upgrade", "-y"], timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append("upgrade:upgrade")
        else:
            errors.append((p.stderr or p.stdout or "apt upgrade failed").strip())
    elif upgrade_mode in {"full", "dist", "dist-upgrade"}:
        p = _run(["apt-get", "dist-upgrade", "-y"], timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append("upgrade:dist-upgrade")
        else:
            errors.append((p.stderr or p.stdout or "apt dist-upgrade failed").strip())

    zbx_result = ensure_zabbix_mysql_monitor_user(
        profile,
        cfg=cfg,
        force=bool(force_zabbix_bootstrap),
        timeout_sec=max(8, int(refresh_timeout_sec)),
    )
    zbx_status = str(zbx_result.get("status", "") or "").strip().lower()
    if zbx_status in {"applied", "already_present", "noop", "disabled", "skipped"}:
        actions.append(f"zabbix_mysql_monitor:{zbx_status}")
    elif zbx_status == "error":
        errors.append(f"zabbix_mysql_monitor:{str(zbx_result.get('reason', 'unknown_error'))}")
    else:
        actions.append(f"zabbix_mysql_monitor:{zbx_status or 'unknown'}")

    state = collect_apt_state(timeout_sec=45, cfg=cfg, auto_bootstrap_zabbix=False)
    state["zabbix_mysql_monitor"] = zbx_result
    if errors:
        # Preserve explicit action errors together with state-derived errors.
        merged = list(errors)
        for e in list(state.get("errors", []) or []):
            if e not in merged:
                merged.append(e)
        state["errors"] = merged[:40]
        state["error_count"] = int(len(merged))
        state["status"] = "error"
        state["level"] = 5

    return {
        "status": "applied",
        "actions": actions,
        "changed_files": changed,
        "state": state,
    }
