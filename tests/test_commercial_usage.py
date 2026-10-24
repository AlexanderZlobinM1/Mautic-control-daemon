from __future__ import annotations

from datetime import datetime, timezone

from mcd_agent.commercial_usage import collect_commercial_usage


class _Cfg:
    table_prefix = "ss_"


class _DB:
    cfg = _Cfg()

    def _safe_table(self, name: str) -> str:
        return name

    def fetch_rows(self, query: str, *, limit: int = 1):
        if "current_contacts" in query:
            return [{"current_contacts": 17}]
        assert "date_sent IS NOT NULL" in query
        assert "is_failed" in query
        return [{"successful_sends": 23}]


def test_collects_source_metrics_without_denormalized_counters():
    payload = collect_commercial_usage(
        _DB(), observed_at=datetime(2026, 10, 24, 12, 34, 56, tzinfo=timezone.utc)
    )
    assert payload == {
        "observed_at": "2026-10-24T12:34:56Z",
        "contacts_current": 17,
        "successful_sends_cumulative": 23,
        "sources": {
            "contacts_current": "leads",
            "successful_sends_cumulative": "email_stats.date_sent",
        },
    }


def test_rejects_naive_observation_time():
    try:
        collect_commercial_usage(_DB(), observed_at=datetime(2026, 10, 24, 12, 34, 56))
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("naive timestamp must be rejected")
