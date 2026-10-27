import base64
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcd_agent import mautic_upgrade as upgrade
from mcd_agent import mautic_patch_plan as patch_plan
from mcd_agent import mautic_patch_plan_v3 as executor
from test_mautic_patch_plan_v3 import _plan


PHASES = [
    ["post_source_install"], ["before_asset_generation"],
    ["post_source_install", "before_cache_warmup"],
    ["post_source_install", "before_cache_warmup"],
    ["post_source_install", "before_campaign_trigger"],
    ["post_source_install", "before_campaign_execution"],
    ["post_source_install", "before_background_imports"],
]


def six_record_plan(root, mode):
    plan = _plan()
    template = plan["patches"][0]
    payload = base64.b64decode(plan["payloads"][0]["content_base64"])
    plan.update(trigger="upgrade_lifecycle", phase="dependency_update_preflight",
                source_version="7.2.0", target_version="7.2.1", install_type=mode)
    plan["patches"] = []; plan["payloads"] = []
    for index, phases in enumerate(PHASES):
        path = f"docroot/app/consumer{index}.txt"
        source = root / path; source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"before\n")
        record = copy.deepcopy(template)
        record.update(id=f"FIXTURE-CONSUMER-{index}", phases=phases,
                      triggers=["upgrade_lifecycle"], phase_order=10 + index,
                      source_paths=[path], payload_path=f"fixtures/consumer{index}.patch")
        for gate in record["gate"]: gate["path"] = path
        content = payload.replace(b"docroot/app/fixture.txt", path.encode())
        plan["patches"].append(record)
        plan["payloads"].append(dict(path=record["payload_path"],
            sha256=hashlib.sha256(content).hexdigest(),
            content_base64=base64.b64encode(content).decode()))
    return plan


def wire(root, plan, monkeypatch, *, drift=False):
    events = []; installed = [False]
    inst = SimpleNamespace(root=str(root), console_path="bin/console", runtime="host", instance_uid="fixture")
    cfg = SimpleNamespace(php_bin="php", mautic_run_as_user="www-data", mcc_url="",
                          state_db_path=str(root / "state.db"))
    stage = SimpleNamespace(close=lambda: events.append("stage_close"),
        verify_original=lambda *a: events.append("original_bound"),
        verify_live=lambda *a: events.append("target_bound") or {"status": "success"})
    for name, value in [("_pick_install_record", inst), ("_ensure_upgrade_target_allowed", True),
            ("_require_release_approval", None), ("_prepare_patch_target_stage", stage),
            ("_pre_upgrade_permissions_check", None), ("installed_required_bundles", []),
            ("_write_upgrade_version_cache", 0)]:
        monkeypatch.setattr(upgrade, name, lambda *a, _value=value, **kw: _value)
    monkeypatch.setattr(upgrade, "_read_current_version", lambda *a: "7.2.1" if installed[0] else "7.2.0")
    monkeypatch.setattr(upgrade, "_resolve_composer_project_root", lambda root: root)
    monkeypatch.setattr(upgrade, "_enter_upgrade_maintenance", lambda *a: events.append("maintenance") or object())
    monkeypatch.setattr(upgrade, "_exit_upgrade_maintenance", lambda *a: events.append("resume"))
    for name in ("_verify_assetmapper_upgrade", "ensure_mailer_packages_for_sender_config",
                 "ensure_amazon_mailer_for_bundles", "_post_upgrade_verify"):
        monkeypatch.setattr(upgrade, name, lambda *a, **kw: None)
    monkeypatch.setattr("mcd_agent.mautic_patch_stage.application_root", lambda root: root)
    monkeypatch.setattr(patch_plan, "atomic_preflight", lambda *a, **kw: {"status": "success"})
    real_execute = patch_plan.execute
    def execute(root, raw, phase, run_id, **kwargs):
        events.append(phase)
        return real_execute(root, raw, phase, run_id, **kwargs)
    monkeypatch.setattr(patch_plan, "execute", execute)
    def install(*args):
        hook = args[-1]; events.append("replacement")
        hook(str(root))
        assert "maintenance" in events and "resume" not in events
        # Both early phases are real executor passes over already patched
        # records; no file or raw plan can be deduplicated or rewritten.
        before = [(root / row["source_paths"][0]).read_bytes() for row in plan["patches"]]
        for phase in ("before_campaign_trigger", "before_campaign_execution", "before_background_imports"):
            hook.apply_phase(str(root), phase)
        assert before == [(root / row["source_paths"][0]).read_bytes() for row in plan["patches"]]
        if drift: (root / plan["patches"][1]["source_paths"][0]).write_bytes(b"operator drift\n")
        for phase in ("before_cache_warmup", "before_asset_generation", "preflight_frontend_assets", "before_doctrine_migrations"):
            upgrade._patch_lifecycle_phase(hook, str(root), phase)
        events.append("scripts")
        installed[0] = True
    monkeypatch.setattr(upgrade, "_apply_zip", install)
    monkeypatch.setattr(upgrade, "_apply_composer", install)
    return dict(config=cfg, root=str(root), mode=plan["install_type"], yes=True,
        do_backup=False, with_system_upgrade=False, target_override="7.2.1",
        patch_plan_json=json.dumps(plan), patch_run_id=plan["run_id"]), events


@pytest.mark.parametrize("mode", ["zip", "composer"])
def test_upgrade_entrypoint_executes_all_six_record_phases_before_consumers(tmp_path, monkeypatch, mode):
    plan = six_record_plan(tmp_path, mode); before = copy.deepcopy(plan)
    args, events = wire(tmp_path, plan, monkeypatch)
    assert upgrade.run_upgrade_apply(**args) == 0
    assert plan == before
    assert events.index("target_bound") < events.index("post_source_install")
    assert events.index("post_source_install") < events.index("before_campaign_trigger") < events.index("before_campaign_execution") < events.index("before_background_imports")
    assert events.index("before_background_imports") < events.index("before_cache_warmup") < events.index("before_asset_generation") < events.index("scripts") < events.index("resume")
    assert all((tmp_path / row["source_paths"][0]).read_bytes() == b"after\n" for row in plan["patches"])
    assert executor.verify_applied(str(tmp_path), plan)["status"] == "success"


def test_v3_upgrade_failure_evidence_is_submitted_against_the_issued_plan(capsys):
    plan = _plan()
    plan.update(
        trigger="upgrade_lifecycle", phase="dependency_update_preflight",
        source_version="7.2.0", target_version="7.2.1", operation="apply",
    )
    digest = upgrade.canonical_json_sha256(plan) if hasattr(upgrade, "canonical_json_sha256") else hashlib.sha256(
        (json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    ).hexdigest()
    resolution = {
        "status": "selected", "host_id": "host-1", "instance_uid": "fixture",
        "catalog_revision": "catalog-1", "catalog_sha256": "c" * 64,
        "plan_sha256": digest, "plan": plan,
    }
    install = SimpleNamespace(instance_uid="fixture")
    config = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
    failure = {"failure": {"id": plan["patches"][0]["id"], "gates": []}, "completed": []}
    with patch("mcd_agent.mautic_patch_resolution._host_identity", return_value=("host", "fixture.example")), \
         patch("mcd_agent.mautic_patch_resolution.report_evidence", return_value={"status": "accepted"}) as report:
        assert upgrade._report_upgrade_patch_evidence(
            config, install, resolution, plan, started_at="2026-10-27T00:00:00+00:00",
            status="error", failure=failure,
        )
    evidence = report.call_args.kwargs["evidence"]
    assert evidence["schema"] == "mcd-mautic-patch-evidence-v1"
    assert evidence["status"] == "error"
    assert evidence["selected"] == [plan["patches"][0]["id"]]
    assert [row["id"] for row in evidence["records"]] == evidence["selected"]
    assert "MCD_PATCH_PLAN_EVIDENCE=" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["zip", "composer"])
def test_phase_source_drift_stops_upgrade_before_scripts(tmp_path, monkeypatch, mode):
    plan = six_record_plan(tmp_path, mode)
    args, events = wire(tmp_path, plan, monkeypatch, drift=True)
    with pytest.raises(RuntimeError): upgrade.run_upgrade_apply(**args)
    assert "scripts" not in events
    assert (tmp_path / plan["patches"][1]["source_paths"][0]).read_bytes() == b"operator drift\n"


def test_other_unavailable_phase_still_rejects_before_target_stage(tmp_path, monkeypatch):
    plan = six_record_plan(tmp_path, "composer")
    plan["patches"][0]["phases"] = ["before_plugin_reload"]
    args, events = wire(tmp_path, plan, monkeypatch)
    with pytest.raises(RuntimeError, match="phase unavailable"):
        upgrade.run_upgrade_apply(**args)
    assert not events
    assert all((tmp_path / row["source_paths"][0]).read_bytes() == b"before\n" for row in plan["patches"])
