from __future__ import annotations

import json
from pathlib import Path
import time

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.self_update import check_with_mcc


def _semver(v: str) -> tuple[int, int, int]:
    nums = [x for x in "".join(ch if ch.isdigit() else "." for ch in (v or "")).split(".") if x.isdigit()]
    while len(nums) < 3:
        nums.append("0")
    return (int(nums[0]), int(nums[1]), int(nums[2]))


def maybe_notify_update(cfg: AgentConfig) -> str | None:
    if not cfg.mcd_update_notify:
        return None
    state_path = Path(cfg.state_db_path).parent / "mcd-update-check.json"
    now = int(time.time())
    state: dict = {}
    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = raw
        except Exception:
            state = {}
    last = int(state.get("checked_at", 0) or 0)
    decision = state.get("decision") if isinstance(state.get("decision"), dict) else None
    if not isinstance(decision, dict) or now - last >= max(60, int(cfg.mcd_update_check_interval_sec or 3600)):
        decision = check_with_mcc(cfg, auto_update_enabled=False)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"checked_at": now, "decision": decision}, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
    status = str(decision.get("status", "")).strip().lower()
    if status not in {"update", "update_available"}:
        return None
    target = str(decision.get("target", "")).strip()
    if not target or _semver(target) <= _semver(__version__):
        return None
    policy = str(decision.get("policy", cfg.mcd_update_policy)).strip().lower() or "approved"
    return f"MCD update available: current={__version__}, target={target}, policy={policy}."
