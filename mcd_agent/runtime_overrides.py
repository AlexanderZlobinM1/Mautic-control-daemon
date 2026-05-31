from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent import __version__
from mcd_agent.config import AgentConfig, apply_runtime_overrides, runtime_remote_allowed_keys, runtime_overrides_from_config_file
from mcd_agent.host_identity import resolve_agent_identity


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 12) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def _api_base(cfg: AgentConfig) -> str | None:
    if not cfg.mcc_url:
        return None
    return cfg.mcc_url.rstrip("/")


def fetch_runtime_overrides(cfg: AgentConfig) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
    }
    url = base + "/api/v1/agent/runtime-overrides"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        if not isinstance(out.get("runtime_overrides"), dict):
            out["runtime_overrides"] = {}
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}", "runtime_overrides": {}}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}", "runtime_overrides": {}}
    except Exception as e:
        return {"status": "error", "reason": str(e), "runtime_overrides": {}}


def push_runtime_overrides(
    cfg: AgentConfig,
    runtime_overrides: dict[str, Any],
    *,
    merge: bool = False,
    target: str = "observed",
) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    target_mode = str(target or "observed").strip().lower()
    if target_mode not in {"observed", "desired"}:
        target_mode = "observed"
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
        "runtime_overrides": runtime_overrides if isinstance(runtime_overrides, dict) else {},
        "push_mode": "merge" if merge else "replace",
        "target": target_mode,
    }
    url = base + "/api/v1/agent/runtime-overrides"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def local_runtime_overrides(cfg: AgentConfig) -> dict[str, Any]:
    return runtime_overrides_from_config_file(cfg.config_file_path)


def overrides_fingerprint(overrides: dict[str, Any]) -> str:
    normalized = json.dumps(overrides, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def poll_trigger_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "runtime-overrides.poll"


def touch_poll_trigger(cfg: AgentConfig) -> str:
    p = poll_trigger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(time.time())), encoding="utf-8")
    return str(p)


def consume_poll_trigger(cfg: AgentConfig) -> bool:
    p = poll_trigger_path(cfg)
    if not p.exists():
        return False
    try:
        p.unlink()
    except Exception:
        return True
    return True


def apply_remote_overrides(base_cfg: AgentConfig, overrides: dict[str, Any]) -> dict[str, Any]:
    cfg2, applied, unsupported, blocked = apply_runtime_overrides(
        base_cfg,
        overrides,
        allowed_keys=runtime_remote_allowed_keys(),
    )
    return {
        "config": cfg2,
        "applied_keys": applied,
        "unsupported_keys": unsupported,
        "blocked_keys": blocked,
    }
