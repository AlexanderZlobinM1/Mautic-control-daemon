from __future__ import annotations

from mcd_agent.time_scheduler import (
    TimeTaskSpec,
    WallClockDispatcher,
    reconcile_state,
    state_key,
    wall_clock_anchor,
)


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, dict] = {}

    def get_runtime_sync(self, key: str):
        return dict(self.values[key]) if key in self.values else None

    def put_runtime_sync(self, key: str, value: dict) -> None:
        self.values[key] = dict(value)


def test_interval_60_uses_minute_wall_clock_and_does_not_chain_completion() -> None:
    store = MemoryStore()
    calls: list[tuple[float, float]] = []
    dispatcher = WallClockDispatcher(store)
    spec = TimeTaskSpec(
        key="instance-a/plugin/warmup",
        interval_sec=60,
        enabled=True,
        dispatch=lambda due, _state: calls.append((due, 3621.0)) or {"status": "launched"},
    )

    dispatcher.tick([spec], now_ts=3621.0)
    dispatcher.tick([spec], now_ts=3659.0)
    dispatcher.tick([spec], now_ts=3681.0)

    assert calls == [(3600.0, 3621.0), (3660.0, 3621.0)]
    state = store.get_runtime_sync(state_key(spec.key))
    assert state["anchor_at"] == 3600.0
    assert state["next_due"] == 3720.0


def test_interval_3600_preserves_enable_anchor_across_restart() -> None:
    raw = reconcile_state(
        None,
        task_key="instance-b/plugin/hourly",
        interval_sec=3600,
        enabled=True,
        now_ts=10 * 3600 + 193.0,
    )
    assert raw["anchor_at"] == 36180.0
    assert raw["next_due"] == 36180.0

    restarted = reconcile_state(
        raw,
        task_key="instance-b/plugin/hourly",
        interval_sec=3600,
        enabled=True,
        now_ts=10 * 3600 + 900.0,
    )
    assert restarted["anchor_at"] == raw["anchor_at"]
    assert restarted["next_due"] == raw["next_due"]


def test_independent_tasks_and_deferred_receipt_do_not_block_each_other() -> None:
    store = MemoryStore()
    calls: list[str] = []
    dispatcher = WallClockDispatcher(store)
    first = TimeTaskSpec(
        key="instance-a/slow",
        interval_sec=60,
        enabled=True,
        dispatch=lambda _due, _state: {"status": "deferred", "reason": "worker_slot"},
    )
    second = TimeTaskSpec(
        key="instance-b/fast",
        interval_sec=60,
        enabled=True,
        dispatch=lambda _due, _state: calls.append("fast") or {"status": "launched"},
    )

    outcomes = dispatcher.tick([first, second], now_ts=600.0)

    assert calls == ["fast"]
    assert any(item["key"] == first.key and item["status"] == "deferred" for item in outcomes)
    assert any(item["key"] == second.key and item["status"] == "launched" for item in outcomes)
    first_state = store.get_runtime_sync(state_key(first.key))
    second_state = store.get_runtime_sync(state_key(second.key))
    assert first_state["next_due"] == 600.0
    assert first_state["deferred_count"] == 1
    assert second_state["next_due"] == 660.0


def test_anchor_is_minute_quantized() -> None:
    assert wall_clock_anchor(623.9) == 600.0
