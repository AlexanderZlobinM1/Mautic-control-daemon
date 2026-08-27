from __future__ import annotations

import hashlib
import json
import os
import grp
import pwd
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcd_agent.config import AgentConfig
from mcd_agent.executor import execute_mautic_command
from mcd_agent.install_type import app_bundle_dir_candidates, plugin_dir_candidates
from mcd_agent.inventory import InstanceInventory, MauticInstall, ensure_seeded


ASSET_SCOPE = ("plugins", "bundles")
_IGNORED_NAMES = {".stfolder", ".stversions"}
_CONFLICT_RE = re.compile(
    r"(sync-conflict|\.sync-conflict|\.stversions|\.syncthing\..*\.tmp$|\.tmp$|\.part$)",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _asset_path(root: str, name: str) -> Path:
    if name == "plugins":
        candidates = plugin_dir_candidates(root)
        return next((p for p in candidates if p.exists()), candidates[0])
    if name == "bundles":
        candidates = app_bundle_dir_candidates(root)
        return next((p for p in candidates if p.exists()), candidates[0])
    raise ValueError(f"unsupported asset name: {name}")


def _is_ignored(path: Path, rel: str) -> bool:
    parts = path.parts
    if any(part in _IGNORED_NAMES for part in parts):
        return True
    name = path.name
    if name.startswith(".syncthing.") and name.endswith(".tmp"):
        return True
    return bool(rel and rel != "." and _CONFLICT_RE.search(rel))


def _mount_info(path: Path) -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["findmnt", "-T", str(path), "-J", "-o", "SOURCE,FSTYPE,TARGET,OPTIONS"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as e:
        return {"error": str(e)}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "").strip()}
    try:
        data = json.loads(proc.stdout or "{}")
        rows = data.get("filesystems")
        if isinstance(rows, list) and rows:
            row = rows[0] if isinstance(rows[0], dict) else {}
            return {
                "source": str(row.get("source", "") or ""),
                "fstype": str(row.get("fstype", "") or ""),
                "target": str(row.get("target", "") or ""),
                "options": str(row.get("options", "") or ""),
            }
    except Exception as e:
        return {"error": str(e)}
    return {}


def _path_owner(path: Path) -> str:
    try:
        st = path.lstat()
        user = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        return f"{user}:{group}"
    except Exception:
        return ""


def _scan_conflicts(base: Path, *, limit: int = 50) -> tuple[list[str], list[str]]:
    conflicts: list[str] = []
    markers: list[str] = []
    try:
        for p in base.rglob("*"):
            rel = str(p.relative_to(base))
            if any(part in _IGNORED_NAMES for part in p.parts):
                markers.append(rel)
                continue
            if _CONFLICT_RE.search(rel):
                conflicts.append(rel)
            if len(conflicts) >= limit and len(markers) >= limit:
                break
    except Exception as e:
        conflicts.append(f"<scan-error: {e}>")
    return conflicts[:limit], markers[:limit]


def _permission_sample(path: Path, user: str, *, limit: int = 20) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    if not user:
        return samples
    try:
        proc = subprocess.run(
            [
                "find",
                str(path),
                "-xdev",
                "(",
                "!",
                "-user",
                user,
                "-o",
                "!",
                "-group",
                user,
                ")",
                "-print",
                "-quit",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        found = (proc.stdout or "").strip().splitlines()
        if found:
            samples.append({"kind": "owner", "path": found[0]})
    except Exception as e:
        samples.append({"kind": "owner_check_error", "path": str(e)})
    for kind, args in (
        ("dir_mode", ["-type", "d", "!", "-perm", "-u+rwx"]),
        ("file_mode", ["-type", "f", "!", "-perm", "-u+rw"]),
    ):
        try:
            proc = subprocess.run(
                ["find", str(path), "-xdev"] + args + ["-print", "-quit"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            found = (proc.stdout or "").strip().splitlines()
            if found:
                samples.append({"kind": kind, "path": found[0]})
        except Exception as e:
            samples.append({"kind": f"{kind}_check_error", "path": str(e)})
        if len(samples) >= limit:
            break
    return samples[:limit]


def compute_asset_manifest(root: str, asset: str, *, run_as_user: str | None = "www-data") -> dict[str, Any]:
    path = _asset_path(root, asset)
    payload: dict[str, Any] = {
        "asset": asset,
        "path": str(path),
        "exists": path.exists(),
        "status": "ok",
        "digest": "",
        "content_digest": "",
        "files": 0,
        "dirs": 0,
        "symlinks": 0,
        "bytes": 0,
        "latest_mtime": 0,
        "owner": "",
        "mount": {},
        "conflicts": [],
        "syncthing_markers": [],
        "permission_issues": [],
        "errors": [],
    }
    if not path.exists():
        payload["status"] = "error"
        payload["errors"] = ["missing_path"]
        return payload
    if not path.is_dir():
        payload["status"] = "error"
        payload["errors"] = ["not_directory"]
        return payload

    payload["owner"] = _path_owner(path)
    payload["mount"] = _mount_info(path)
    conflicts, markers = _scan_conflicts(path)
    payload["conflicts"] = conflicts
    payload["syncthing_markers"] = markers
    payload["permission_issues"] = _permission_sample(path, str(run_as_user or "").strip())

    h = hashlib.sha256()
    ch = hashlib.sha256()
    errors: list[str] = []
    try:
        entries = [path]
        entries.extend(sorted(path.rglob("*"), key=lambda p: str(p.relative_to(path))))
        for entry in entries:
            rel = "." if entry == path else str(entry.relative_to(path))
            if _is_ignored(entry, rel):
                continue
            try:
                st = entry.lstat()
            except Exception as e:
                errors.append(f"{rel}:stat:{e}")
                continue
            mode = st.st_mode & 0o7777
            mtime = int(st.st_mtime)
            payload["latest_mtime"] = max(int(payload["latest_mtime"] or 0), mtime)
            if entry.is_symlink():
                target = os.readlink(entry)
                payload["symlinks"] = int(payload["symlinks"] or 0) + 1
                line = f"L\0{rel}\0{mode:o}\0{st.st_uid}\0{st.st_gid}\0{target}\n"
                content_line = f"L\0{rel}\0{target}\n"
            elif entry.is_dir():
                payload["dirs"] = int(payload["dirs"] or 0) + 1
                line = f"D\0{rel}\0{mode:o}\0{st.st_uid}\0{st.st_gid}\n"
                content_line = f"D\0{rel}\n"
            elif entry.is_file():
                payload["files"] = int(payload["files"] or 0) + 1
                payload["bytes"] = int(payload["bytes"] or 0) + int(st.st_size)
                try:
                    file_hash = _sha256_file(entry)
                except Exception as e:
                    errors.append(f"{rel}:hash:{e}")
                    continue
                line = f"F\0{rel}\0{mode:o}\0{st.st_uid}\0{st.st_gid}\0{st.st_size}\0{file_hash}\n"
                content_line = f"F\0{rel}\0{st.st_size}\0{file_hash}\n"
            else:
                line = f"O\0{rel}\0{mode:o}\0{st.st_uid}\0{st.st_gid}\n"
                content_line = f"O\0{rel}\n"
            h.update(line.encode("utf-8", errors="surrogateescape"))
            ch.update(content_line.encode("utf-8", errors="surrogateescape"))
    except Exception as e:
        errors.append(str(e))

    payload["digest"] = h.hexdigest()
    payload["content_digest"] = ch.hexdigest()
    if errors:
        payload["errors"] = errors[:20]
    if conflicts or payload["permission_issues"] or errors:
        payload["status"] = "error" if conflicts or errors else "warning"
    return payload


def _combined_digest(items: list[dict[str, Any]], key: str) -> str:
    h = hashlib.sha256()
    for item in sorted(items, key=lambda x: str(x.get("asset", ""))):
        h.update(str(item.get("asset", "")).encode("utf-8"))
        h.update(b"\0")
        h.update(str(item.get(key, "")).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _select_installs(cfg: AgentConfig, installs: list[MauticInstall] | None, root: str | None) -> list[MauticInstall]:
    if installs is None:
        inv = InstanceInventory(cfg.state_db_path)
        ensure_seeded(inv, cfg)
        installs = inv.list_instances()
    selector = str(root or "").strip()
    if not selector:
        return list(installs)
    out: list[MauticInstall] = []
    for inst in installs:
        domains = [str(x).strip().lower() for x in (inst.domains or []) if str(x).strip()]
        if (
            inst.root == selector
            or inst.instance_uid == selector
            or inst.name == selector
            or str(inst.primary_domain or "").strip().lower() == selector.lower()
            or selector.lower() in domains
        ):
            out.append(inst)
    return out


def collect_cluster_assets_status(
    cfg: AgentConfig,
    *,
    installs: list[MauticInstall] | None = None,
    root: str | None = None,
) -> dict[str, Any]:
    enabled = bool(getattr(cfg, "cluster_assets_enabled", False))
    selected = _select_installs(cfg, installs, root)
    run_as_user = str(getattr(cfg, "mautic_run_as_user", "") or "www-data").strip() or "www-data"
    payload: dict[str, Any] = {
        "enabled": enabled,
        "scope": list(ASSET_SCOPE),
        "status": "disabled" if not enabled else "ok",
        "checked_at_utc": _utc_now(),
        "instances": [],
        "errors": [],
    }
    if not enabled and root is None:
        return payload
    if not selected:
        payload["status"] = "error"
        payload["errors"] = ["no_instances"]
        return payload

    status_rank = {"ok": 0, "warning": 1, "error": 2}
    worst = 0
    for inst in selected:
        assets = [compute_asset_manifest(inst.root, asset, run_as_user=run_as_user) for asset in ASSET_SCOPE]
        inst_status = "ok"
        if any(str(a.get("status")) == "error" for a in assets):
            inst_status = "error"
        elif any(str(a.get("status")) == "warning" for a in assets):
            inst_status = "warning"
        worst = max(worst, status_rank.get(inst_status, 0))
        payload["instances"].append(
            {
                "instance_uid": inst.instance_uid,
                "name": inst.name,
                "root": inst.root,
                "primary_domain": inst.primary_domain,
                "status": inst_status,
                "digest": _combined_digest(assets, "digest"),
                "content_digest": _combined_digest(assets, "content_digest"),
                "assets": assets,
            }
        )
    payload["status"] = "error" if worst >= 2 else ("warning" if worst == 1 else "ok")
    return payload


def _load_state(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: str, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def fix_cluster_asset_permissions(cfg: AgentConfig, *, installs: list[MauticInstall] | None = None, root: str | None = None) -> dict[str, Any]:
    selected = _select_installs(cfg, installs, root)
    user = str(getattr(cfg, "mautic_run_as_user", "") or "www-data").strip() or "www-data"
    results: list[dict[str, Any]] = []
    for inst in selected:
        for asset in ASSET_SCOPE:
            path = _asset_path(inst.root, asset)
            row: dict[str, Any] = {"root": inst.root, "asset": asset, "path": str(path), "status": "skipped"}
            if not path.exists():
                row["reason"] = "missing_path"
                results.append(row)
                continue
            try:
                subprocess.run(["chown", "-R", f"{user}:{user}", str(path)], check=True, timeout=300)
                subprocess.run(["find", str(path), "-xdev", "-type", "d", "-exec", "chmod", "u+rwx,g+rx,o+rx", "{}", "+"], check=True, timeout=300)
                subprocess.run(["find", str(path), "-xdev", "-type", "f", "-exec", "chmod", "u+rw,g+r,o+r", "{}", "+"], check=True, timeout=300)
                row["status"] = "ok"
            except Exception as e:
                row["status"] = "error"
                row["reason"] = str(e)
            results.append(row)
    status = "ok" if all(str(r.get("status")) in {"ok", "skipped"} for r in results) else "error"
    return {"status": status, "checked_at_utc": _utc_now(), "results": results}


def reload_cluster_asset_runtime(
    cfg: AgentConfig,
    *,
    installs: list[MauticInstall] | None = None,
    root: str | None = None,
    cache_clear: bool = True,
    cache_warm: bool = True,
    fpm_reload: bool = True,
) -> dict[str, Any]:
    selected = _select_installs(cfg, installs, root)
    results: list[dict[str, Any]] = []
    for inst in selected:
        row: dict[str, Any] = {"root": inst.root, "instance_uid": inst.instance_uid, "status": "ok", "steps": []}
        if cache_clear:
            rc, output = execute_mautic_command(
                php_bin=cfg.php_bin,
                root=inst.root,
                command="cache:clear",
                instance_id=None,
                timeout_sec=max(60, int(getattr(cfg, "cluster_assets_reload_timeout_sec", 300) or 300)),
                run_as_user=cfg.mautic_run_as_user,
            )
            row["steps"].append({"step": "cache_clear", "rc": rc, "output": output[-2000:]})
            if rc != 0:
                row["status"] = "error"
        if cache_warm:
            rc, output = execute_mautic_command(
                php_bin=cfg.php_bin,
                root=inst.root,
                command="cache:warmup",
                instance_id=None,
                timeout_sec=max(60, int(getattr(cfg, "cluster_assets_reload_timeout_sec", 300) or 300)),
                run_as_user=cfg.mautic_run_as_user,
            )
            row["steps"].append({"step": "cache_warmup", "rc": rc, "output": output[-2000:]})
            if rc != 0:
                row["status"] = "error"
        results.append(row)
    fpm_step: dict[str, Any] | None = None
    if fpm_reload:
        proc = subprocess.run(
            (
                "units=$(systemctl list-units --type=service --all --no-legend 'php*-fpm.service' "
                "| awk '{print $1}' | tr '\\n' ' '); "
                "if [ -z \"$units\" ]; then exit 0; fi; "
                "systemctl reload $units || systemctl restart $units"
            ),
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        fpm_step = {
            "step": "php_fpm_reload",
            "rc": int(proc.returncode),
            "output": ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()[-2000:],
        }
    status = "ok"
    if any(str(r.get("status")) == "error" for r in results) or (fpm_step and int(fpm_step.get("rc", 0) or 0) != 0):
        status = "error"
    return {"status": status, "checked_at_utc": _utc_now(), "instances": results, "fpm": fpm_step}


def guard_cluster_assets(
    cfg: AgentConfig,
    *,
    installs: list[MauticInstall] | None = None,
    root: str | None = None,
    fix_permissions: bool | None = None,
    reload_on_change: bool | None = None,
) -> dict[str, Any]:
    state_path = str(
        getattr(cfg, "cluster_assets_state_file", "") or "/opt/mcd/var/cluster-assets-state.json"
    )
    old_state = _load_state(state_path)
    payload = collect_cluster_assets_status(cfg, installs=installs, root=root)
    selected_instances = payload.get("instances") if isinstance(payload.get("instances"), list) else []
    result: dict[str, Any] = {
        "status": payload.get("status", "unknown"),
        "checked_at_utc": payload.get("checked_at_utc"),
        "state_file": state_path,
        "assets": payload,
        "permission_fix": None,
        "reload": None,
        "changed": [],
    }
    if not bool(payload.get("enabled")) and root is None:
        _write_state(state_path, {"updated_at": _utc_now(), "instances": {}})
        return result

    state_instances = old_state.get("instances") if isinstance(old_state.get("instances"), dict) else {}
    new_instances: dict[str, Any] = {}
    changed_roots: list[str] = []
    for inst in selected_instances:
        if not isinstance(inst, dict):
            continue
        root_value = str(inst.get("root", "") or "").strip()
        content_digest = str(inst.get("content_digest", "") or "").strip()
        digest = str(inst.get("digest", "") or "").strip()
        prev = state_instances.get(root_value) if isinstance(state_instances, dict) else None
        prev_content = str((prev or {}).get("content_digest", "") or "") if isinstance(prev, dict) else ""
        if prev_content and content_digest and prev_content != content_digest:
            changed_roots.append(root_value)
        new_instances[root_value] = {
            "content_digest": content_digest,
            "digest": digest,
            "updated_at": payload.get("checked_at_utc"),
        }

    if bool(fix_permissions if fix_permissions is not None else getattr(cfg, "cluster_assets_fix_permissions", False)):
        fix_res = fix_cluster_asset_permissions(cfg, installs=installs, root=root)
        result["permission_fix"] = fix_res
        if str(fix_res.get("status")) == "error":
            result["status"] = "error"
        payload = collect_cluster_assets_status(cfg, installs=installs, root=root)
        result["assets"] = payload
        selected_instances = payload.get("instances") if isinstance(payload.get("instances"), list) else []
        new_instances = {}
        for inst in selected_instances:
            if not isinstance(inst, dict):
                continue
            root_value = str(inst.get("root", "") or "").strip()
            new_instances[root_value] = {
                "content_digest": str(inst.get("content_digest", "") or "").strip(),
                "digest": str(inst.get("digest", "") or "").strip(),
                "updated_at": payload.get("checked_at_utc"),
            }

    has_conflicts = False
    for inst in selected_instances:
        if not isinstance(inst, dict):
            continue
        for asset in inst.get("assets", []) if isinstance(inst.get("assets"), list) else []:
            if isinstance(asset, dict) and asset.get("conflicts"):
                has_conflicts = True
                break

    should_reload = bool(
        changed_roots
        and not has_conflicts
        and (reload_on_change if reload_on_change is not None else getattr(cfg, "cluster_assets_reload_on_change", False))
    )
    if should_reload:
        result["reload"] = reload_cluster_asset_runtime(
            cfg,
            installs=installs,
            root=root,
            cache_clear=bool(getattr(cfg, "cluster_assets_cache_clear_on_change", True)),
            cache_warm=bool(getattr(cfg, "cluster_assets_cache_warm_on_change", True)),
            fpm_reload=bool(getattr(cfg, "cluster_assets_fpm_reload_on_change", True)),
        )
        if str((result["reload"] or {}).get("status")) == "error":
            result["status"] = "error"
    result["changed"] = changed_roots
    _write_state(state_path, {"updated_at": _utc_now(), "instances": new_instances})
    return result


def format_cluster_assets_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        "cluster assets: status={status} enabled={enabled} scope={scope}".format(
            status=str(payload.get("status", "unknown")),
            enabled=1 if bool(payload.get("enabled")) else 0,
            scope=",".join(str(x) for x in payload.get("scope", ASSET_SCOPE)),
        )
    )
    for inst in payload.get("instances", []) if isinstance(payload.get("instances"), list) else []:
        if not isinstance(inst, dict):
            continue
        lines.append(
            "root={root} status={status} digest={digest} content={content}".format(
                root=str(inst.get("root", "")),
                status=str(inst.get("status", "")),
                digest=str(inst.get("digest", ""))[:12],
                content=str(inst.get("content_digest", ""))[:12],
            )
        )
        for asset in inst.get("assets", []) if isinstance(inst.get("assets"), list) else []:
            if not isinstance(asset, dict):
                continue
            lines.append(
                "  {asset}: status={status} files={files} dirs={dirs} bytes={bytes} conflicts={conflicts} perms={perms} path={path}".format(
                    asset=str(asset.get("asset", "")),
                    status=str(asset.get("status", "")),
                    files=int(asset.get("files", 0) or 0),
                    dirs=int(asset.get("dirs", 0) or 0),
                    bytes=int(asset.get("bytes", 0) or 0),
                    conflicts=len(asset.get("conflicts", []) if isinstance(asset.get("conflicts"), list) else []),
                    perms=len(asset.get("permission_issues", []) if isinstance(asset.get("permission_issues"), list) else []),
                    path=str(asset.get("path", "")),
                )
            )
    return "\n".join(lines)
