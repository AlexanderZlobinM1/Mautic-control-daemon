from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
AUDIT = ROOT / "mcd_agent" / "contracts" / "mcd-execution-ownership-audit-v1.json"


def test_execution_ownership_audit_is_complete_and_preservation_gated() -> None:
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    assert audit["schema"] == "mcd-execution-ownership-audit-v1"
    assert audit["status"] == "preservation-audit-required-before-deletion"
    rows = {row["capability"]: row for row in audit["rows"]}
    assert set(rows) == {
        "standalone_cli",
        "scheduler_cron_taskstore",
        "plugin_operations",
        "upgrades_and_migrations",
        "backup_and_rollback",
        "scenario_execution",
        "settings_recovery",
    }
    for row in rows.values():
        assert row["owner"] == "mcd"
        assert row["canonical_paths"]
        assert row["preserve_entrypoints"]
        assert row["preservation_tests"]
        for relative in row["canonical_paths"]:
            assert (ROOT / relative).is_file(), relative
        for relative in row["preservation_tests"]:
            assert (ROOT / relative).is_file(), relative


def test_scenario_execution_has_one_generic_local_path_and_legacy_alias_only() -> None:
    launcher = (ROOT / "mcd_agent" / "scenario_launcher.py").read_text(encoding="utf-8")
    executor = (ROOT / "mcd_agent" / "commercial_self_hosted.py").read_text(encoding="utf-8")
    assert 'SCENARIO_CONTEXT_SCHEMA = "mcd-scenario-executable-context-v1"' in launcher
    assert 'LEGACY_SCENARIO_CONTEXT_SCHEMA = "portal-self-hosted-executable-context-v1"' in launcher
    assert "class ScenarioExecutor:" in executor
    assert "SelfHostedScenarioExecutor = ScenarioExecutor" in executor
    assert "mcd-cli mautic-upgrade apply" not in launcher


def test_full_settings_recovery_remains_an_explicit_preservation_case() -> None:
    algorithm = (ROOT / "ALGORITHM.md").read_text(encoding="utf-8")
    assert "полный durable runtime snapshot" in algorithm
    assert "все MCC-allowed host- и instance-runtime keys" in algorithm
    assert "false/zero остаются валидными значениями" in algorithm
    assert "Явный MCC tombstone имеет приоритет" in algorithm
