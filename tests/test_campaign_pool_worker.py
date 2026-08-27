from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = CONTROL_PLANE_ROOT / "local-mcc" / "mauticctl" / "custom" / "scripts" / "campaign-pool-worker.py"


def _load_module():
    if not (CONTROL_PLANE_ROOT / "agent").exists() or not SCRIPT_PATH.exists():
        pytest.skip("campaign pool worker tests require a full control-plane checkout")
    spec = spec_from_file_location("campaign_pool_worker", SCRIPT_PATH)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_trigger_backlog_sql_tracks_root_contacts_and_due_events():
    module = _load_module()

    sql = module._trigger_backlog_sql(5129)

    assert "campaign_leads cl" in sql
    assert "campaign_events ev" in sql
    assert "ev.parent_id IS NULL" in sql
    assert "date_last_exited IS NULL" in sql
    assert "el.is_scheduled = 1" in sql
    assert "date_triggered IS NULL" not in sql
    assert "trigger_date <= NOW()" in sql
    assert "trigger_date <= UTC_TIMESTAMP()" in sql


def test_trigger_backlog_progress_counts_root_and_due_rows():
    module = _load_module()

    backlog = {"root_contacts": 244350, "due_events": 42, "remaining": 244392}

    assert not module.trigger_backlog_drained(backlog)
    assert module.format_trigger_backlog(backlog) == (
        "root_contacts=244350 due_events=42 remaining=244392"
    )


def test_trigger_backlog_progress_detects_root_drain_even_if_due_grows():
    module = _load_module()

    previous = {"root_contacts": 1000, "due_events": 20, "remaining": 1020}
    current = {"root_contacts": 900, "due_events": 25, "remaining": 925}

    assert module.trigger_backlog_progressed(current, previous)


def test_trigger_backlog_drained_requires_root_zero_and_small_due_tail():
    module = _load_module()

    assert module.trigger_backlog_drained(
        {"root_contacts": 0, "due_events": 5, "remaining": 5}
    )
    assert not module.trigger_backlog_drained(
        {"root_contacts": 1, "due_events": 0, "remaining": 1}
    )
    assert not module.trigger_backlog_drained(
        {"root_contacts": 0, "due_events": 6, "remaining": 6}
    )
