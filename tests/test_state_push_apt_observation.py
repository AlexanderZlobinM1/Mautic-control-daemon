from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.state_push import MCCStatePusher


def test_periodic_apt_state_is_observation_only(tmp_path: Path) -> None:
    cfg = SimpleNamespace(state_db_path=str(tmp_path / "state.sqlite"))
    pusher = MCCStatePusher(cfg)  # type: ignore[arg-type]
    calls: list[dict[str, object]] = []

    with patch.object(pusher, "_apt_probe_key", return_value="probe"), patch(
        "mcd_agent.state_push.collect_apt_state",
        side_effect=lambda **kwargs: calls.append(kwargs) or {"status": "ok"},
    ):
        result = pusher._apt_state(100.0)

    assert result == {"status": "ok"}
    assert calls == [{"timeout_sec": 30, "cfg": cfg, "auto_bootstrap_zabbix": False}]
