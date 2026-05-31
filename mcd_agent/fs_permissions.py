from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_GUARD_PATHS: tuple[str, ...] = (
    "var/cache",
    "var/logs",
    "var/spool",
    "var/tmp",
    "app/config",
    "config",
    "media/files",
    "media/images",
    "translations",
)

_DEEP_CHECK_PREFIXES: tuple[str, ...] = (
    "var/cache",
    "var/logs",
    "var/spool",
    "var/tmp",
)


def default_guard_paths() -> list[str]:
    return list(_DEFAULT_GUARD_PATHS)


def normalize_guard_paths(raw_paths: list[str] | tuple[str, ...] | None) -> list[str]:
    if not raw_paths:
        return default_guard_paths()
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        rel = str(raw or "").strip().replace("\\", "/")
        if not rel:
            continue
        rel = rel.lstrip("/")
        rel = rel.rstrip("/")
        if not rel:
            continue
        rel_norm = str(Path(rel))
        if rel_norm in {"", "."}:
            continue
        if rel_norm in seen:
            continue
        seen.add(rel_norm)
        out.append(rel_norm)
    return out or default_guard_paths()


@dataclass(frozen=True)
class PermissionRepairEvent:
    rel_path: str
    target_path: str
    sample_path: str
    reason: str
    before_owner_group: str
    before_mode: str
    actor: str
    actor_source: str
    repaired: bool
    error: str


@dataclass(frozen=True)
class PermissionsGuardResult:
    root: str
    checked_paths: list[str]
    repaired_paths: list[str]
    missing_paths: list[str]
    console_exec_fixed: bool
    errors: list[str]
    repair_events: list[PermissionRepairEvent]


def _is_deep_check_path(rel_path: str) -> bool:
    rel = rel_path.strip().replace("\\", "/")
    return any(rel == p or rel.startswith(f"{p}/") for p in _DEEP_CHECK_PREFIXES)


def _is_path_writable_by_owner(st_mode: int) -> bool:
    return bool(st_mode & stat.S_IWUSR)


def _run_find_first(path: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["find", str(path)] + args,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _path_needs_repair(
    path: Path,
    *,
    owner: str,
    group: str,
    expected_uid: int,
    expected_gid: int,
    deep: bool,
) -> tuple[bool, str, str]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return False, "", ""
    except Exception:
        return False, "", ""

    if st.st_uid != expected_uid or st.st_gid != expected_gid:
        return True, "root_owner_mismatch", str(path)
    if not _is_path_writable_by_owner(st.st_mode):
        return True, "root_mode_no_u_write", str(path)

    if not deep:
        return False, "", ""

    owner_drift = _run_find_first(
        path,
        [
            "-maxdepth",
            "4",
            "(",
            "!",
            "-user",
            owner,
            "-o",
            "!",
            "-group",
            group,
            ")",
            "-print",
            "-quit",
        ],
    )
    if owner_drift:
        return True, "deep_owner_mismatch", owner_drift

    dir_mode_drift = _run_find_first(
        path,
        [
            "-maxdepth",
            "4",
            "-type",
            "d",
            "!",
            "-perm",
            "-u+w",
            "-print",
            "-quit",
        ],
    )
    if dir_mode_drift:
        return True, "deep_dir_mode_no_u_write", dir_mode_drift

    file_mode_drift = _run_find_first(
        path,
        [
            "-maxdepth",
            "4",
            "-type",
            "f",
            "!",
            "-perm",
            "-u+w",
            "-print",
            "-quit",
        ],
    )
    if file_mode_drift:
        return True, "deep_file_mode_no_u_write", file_mode_drift
    return False, "", ""


_UID_CACHE: dict[str, int] = {}
_GID_CACHE: dict[str, int] = {}
_UID_NAME_CACHE: dict[int, str] = {}
_GID_NAME_CACHE: dict[int, str] = {}
_AUDIT_UID_RE = re.compile(r"\bauid=([^ \n]+)")
_AUDIT_ACCT_RE = re.compile(r'\bacct="([^"]+)"')
_AUDIT_EXE_RE = re.compile(r'\bexe="([^"]+)"')
_AUSEARCH_EXISTS = bool(shutil.which("ausearch"))


def _uid_for(user: str) -> int:
    if user in _UID_CACHE:
        return _UID_CACHE[user]
    import pwd

    uid = int(pwd.getpwnam(user).pw_uid)
    _UID_CACHE[user] = uid
    return uid


def _gid_for(group: str) -> int:
    if group in _GID_CACHE:
        return _GID_CACHE[group]
    import grp

    gid = int(grp.getgrnam(group).gr_gid)
    _GID_CACHE[group] = gid
    return gid


def _user_for_uid(uid: int) -> str:
    if uid in _UID_NAME_CACHE:
        return _UID_NAME_CACHE[uid]
    try:
        import pwd

        name = str(pwd.getpwuid(int(uid)).pw_name)
    except Exception:
        name = str(uid)
    _UID_NAME_CACHE[uid] = name
    return name


def _group_for_gid(gid: int) -> str:
    if gid in _GID_NAME_CACHE:
        return _GID_NAME_CACHE[gid]
    try:
        import grp

        name = str(grp.getgrgid(int(gid)).gr_name)
    except Exception:
        name = str(gid)
    _GID_NAME_CACHE[gid] = name
    return name


def _path_before_state(path: Path) -> tuple[str, str]:
    try:
        st = path.stat()
        owner_group = f"{_user_for_uid(int(st.st_uid))}:{_group_for_gid(int(st.st_gid))}"
        mode = format(int(st.st_mode) & 0o777, "04o")
        return owner_group, mode
    except Exception:
        return "-", "-"


def _detect_actor_from_audit(path: Path) -> tuple[str, str]:
    if not _AUSEARCH_EXISTS:
        return "", ""
    try:
        proc = subprocess.run(
            ["ausearch", "-m", "SYSCALL", "-f", str(path), "-ts", "recent", "-i"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return "", ""
    if proc.returncode != 0:
        return "", ""
    text = (proc.stdout or "").strip()
    if not text:
        return "", ""

    auid = ""
    acct = ""
    exe = ""
    for line in reversed(text.splitlines()):
        if not auid:
            m = _AUDIT_UID_RE.search(line)
            if m:
                auid = str(m.group(1) or "").strip()
        if not acct:
            m = _AUDIT_ACCT_RE.search(line)
            if m:
                acct = str(m.group(1) or "").strip()
        if not exe:
            m = _AUDIT_EXE_RE.search(line)
            if m:
                exe = str(m.group(1) or "").strip()
        if auid and exe:
            break

    parts: list[str] = []
    if acct:
        parts.append(f"acct={acct}")
    if auid:
        parts.append(f"auid={auid}")
    if exe:
        parts.append(f"exe={exe}")
    if not parts:
        return "", ""
    return " ".join(parts), "auditd"


def _guess_actor(sample_path: Path, *, expected_owner: str) -> tuple[str, str]:
    actor, source = _detect_actor_from_audit(sample_path)
    if actor:
        return actor, source
    owner_group, _mode = _path_before_state(sample_path)
    owner = owner_group.split(":", 1)[0] if ":" in owner_group else owner_group
    if owner and owner not in {"-", expected_owner}:
        return f"owner={owner}", "owner_guess"
    return "unknown", "unknown"


def _rel_or_abs(path: Path, root_path: Path) -> str:
    try:
        return str(path.relative_to(root_path))
    except Exception:
        return str(path)


def _repair_path(path: Path, *, owner_group: str) -> tuple[bool, str]:
    cmds = (
        ["chown", "-R", owner_group, str(path)],
        ["chmod", "-R", "u+rwX,g+rwX", str(path)],
    )
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"rc={proc.returncode}"
            return False, f"{' '.join(cmd)}: {err}"
    return True, ""


def _ensure_console_exec(console_path: Path, *, owner_group: str) -> tuple[bool, str]:
    if not console_path.exists() or not console_path.is_file():
        return False, ""
    changed = False
    try:
        st = console_path.stat()
        mode = int(st.st_mode)
        need_exec = not bool(mode & stat.S_IXUSR) or not bool(mode & stat.S_IXGRP)
        if need_exec:
            proc = subprocess.run(
                ["chmod", "ug+x", str(console_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip() or f"rc={proc.returncode}"
                return False, f"chmod ug+x {console_path}: {err}"
            changed = True

        owner, group = owner_group.split(":", 1)
        if st.st_uid != _uid_for(owner) or st.st_gid != _gid_for(group):
            proc = subprocess.run(
                ["chown", owner_group, str(console_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip() or f"rc={proc.returncode}"
                return False, f"chown {owner_group} {console_path}: {err}"
            changed = True
    except Exception as e:
        return False, str(e)
    return changed, ""


def ensure_instance_permissions(
    *,
    root: str,
    run_as_user: str = "www-data",
    guard_paths: list[str] | None = None,
    fix_console_exec: bool = True,
    console_relpath: str = "bin/console",
) -> PermissionsGuardResult:
    owner = (run_as_user or "www-data").strip() or "www-data"
    group = owner
    owner_group = f"{owner}:{group}"
    root_path = Path(root)
    paths = normalize_guard_paths(guard_paths)
    checked: list[str] = []
    repaired: list[str] = []
    missing: list[str] = []
    errors: list[str] = []
    repair_events: list[PermissionRepairEvent] = []
    console_exec_fixed = False
    expected_uid = _uid_for(owner)
    expected_gid = _gid_for(group)

    for rel in paths:
        abs_path = root_path / rel
        if not abs_path.exists():
            missing.append(rel)
            continue
        checked.append(rel)
        deep = _is_deep_check_path(rel)
        needs_repair, reason, sample = _path_needs_repair(
            abs_path,
            owner=owner,
            group=group,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            deep=deep,
        )
        if not needs_repair:
            continue
        sample_path = Path(sample) if sample else abs_path
        if not sample_path.is_absolute():
            sample_path = (root_path / sample_path).resolve()
        before_owner_group, before_mode = _path_before_state(sample_path)
        actor, actor_source = _guess_actor(sample_path, expected_owner=owner)
        ok, err = _repair_path(abs_path, owner_group=owner_group)
        if not ok:
            errors.append(err)
        else:
            repaired.append(rel)
        repair_events.append(
            PermissionRepairEvent(
                rel_path=rel,
                target_path=str(abs_path),
                sample_path=_rel_or_abs(sample_path, root_path),
                reason=reason or "repair_required",
                before_owner_group=before_owner_group,
                before_mode=before_mode,
                actor=actor,
                actor_source=actor_source,
                repaired=bool(ok),
                error=str(err or ""),
            )
        )

    if fix_console_exec:
        rel = str(console_relpath or "bin/console").strip().lstrip("/")
        console_path = root_path / rel
        console_before_owner_group, console_before_mode = _path_before_state(console_path)
        console_actor, console_actor_source = _guess_actor(console_path, expected_owner=owner)
        changed, err = _ensure_console_exec(console_path, owner_group=owner_group)
        if err:
            errors.append(err)
            repair_events.append(
                PermissionRepairEvent(
                    rel_path=rel,
                    target_path=str(console_path),
                    sample_path=_rel_or_abs(console_path, root_path),
                    reason="console_exec_guard",
                    before_owner_group=console_before_owner_group,
                    before_mode=console_before_mode,
                    actor=console_actor,
                    actor_source=console_actor_source,
                    repaired=False,
                    error=str(err),
                )
            )
        else:
            console_exec_fixed = bool(changed)
            if changed:
                repair_events.append(
                    PermissionRepairEvent(
                        rel_path=rel,
                        target_path=str(console_path),
                        sample_path=_rel_or_abs(console_path, root_path),
                        reason="console_exec_guard",
                        before_owner_group=console_before_owner_group,
                        before_mode=console_before_mode,
                        actor=console_actor,
                        actor_source=console_actor_source,
                        repaired=True,
                        error="",
                    )
                )

    return PermissionsGuardResult(
        root=str(root),
        checked_paths=checked,
        repaired_paths=repaired,
        missing_paths=missing,
        console_exec_fixed=console_exec_fixed,
        errors=errors,
        repair_events=repair_events,
    )
