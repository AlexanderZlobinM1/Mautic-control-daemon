from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.daemon import _mcc_state_push_tick


class _Pusher:
    def __init__(self) -> None:
        self.cfg = None
        self.last_alert_poll_ts = 0.0
        self.monitor_calls = 0
        self.state_calls = 0
        self.signals = None

    def enabled(self) -> bool:
        return True

    def set_signals(self, payload, now_ts) -> None:
        self.signals = (payload, now_ts)

    def should_push_monitor_signals(self, _now_ts, _payload) -> bool:
        return True

    def send_signals(self, _payload):
        self.monitor_calls += 1
        return True, "ok"

    def build_payload(self, **_kwargs):
        return {"schema": "mcd-state-v1", "sent_at_utc": "now"}

    def should_push(self, _now_ts, _payload):
        return True, True

    def send(self, _payload):
        self.state_calls += 1
        return True, "ok"

    def mark_pushed(self, _now_ts, _payload) -> None:
        return None


def test_state_push_tick_runs_while_scheduler_work_is_blocked() -> None:
    planner_started = threading.Event()
    planner_release = threading.Event()

    def slow_planner() -> None:
        planner_started.set()
        planner_release.wait(timeout=2)

    planner = threading.Thread(target=slow_planner)
    planner.start()
    assert planner_started.wait(timeout=1)

    cfg = SimpleNamespace(
        mcc_push_alert_poll_interval_sec=60,
        mcc_push_alert_window_min=15,
        profile_name="farm-hiload",
    )
    pusher = _Pusher()
    inventory = SimpleNamespace(
        list_all_instances=lambda: [SimpleNamespace(root="/var/www/active/public_html")]
    )

    with (
        patch("mcd_agent.daemon.should_poll_alert", return_value=True),
        patch("mcd_agent.daemon.collect_signals", return_value={"overall": {"level": 0}}),
        patch("mcd_agent.daemon.collect_web_availability", return_value=None),
        patch("mcd_agent.daemon.collect_monitor_signals", return_value={"monitor_only": True}),
        patch("mcd_agent.daemon.read_pending_profile_event", return_value=None),
        patch("mcd_agent.daemon.log_push_result"),
    ):
        result = _mcc_state_push_tick(
            config=cfg,
            pusher=pusher,
            inventory=inventory,
            active_installs=[],
            now_ts=100.0,
        )

    planner_release.set()
    planner.join(timeout=1)

    assert result["state_pushed"] is True
    assert result["monitor_signals_pushed"] is True
    assert pusher.state_calls == 1
    assert pusher.monitor_calls == 1
    assert pusher.signals is not None
