import subprocess
import sys
from types import SimpleNamespace

from mcd_agent import daemon


def test_configured_campaign_ids_do_not_consult_campaign_publication(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("campaign publication must not be queried for configured IDs")

    monkeypatch.setattr(daemon, "MauticDB", forbidden)
    inst = SimpleNamespace(instance_uid="instance.example", root="/srv/instance",
                           name="instance.example", domains=[], primary_domain="instance.example")
    cfg = SimpleNamespace(disable_whitelist=False, campaign_super_priority=[],
                          campaign_super_priority_instance_settings={"instance.example": [987654321, 82]})
    assert daemon._campaign_super_priority_effective_setting(cfg, inst) == {987654321, 82}


def test_native_commands_preserve_opaque_id_and_report_one_failure_independently(monkeypatch):
    cfg = SimpleNamespace(php_bin=sys.executable, mautic_run_as_user=None,
                          cmd_campaign_rebuild_template="rebuild", cmd_campaign_trigger_template="trigger",
                          campaign_limit=0, campaign_batch_limit=1000)
    seen = []

    def render(**kwargs):
        phase, identifier = kwargs["template"], kwargs["id"]
        seen.append((phase, identifier))
        failure = identifier == 987654321
        return [sys.executable, "-c",
                "print(" + repr(phase + ":" + str(identifier)) + ");"
                + ("raise SystemExit(1)" if failure else "raise SystemExit(0)")]

    monkeypatch.setattr(daemon, "render_mautic_command", render)
    failed = subprocess.run(
        daemon._campaign_super_priority_args(cfg, "/srv/instance", 987654321),
        capture_output=True, text=True)
    next_item = subprocess.run(
        daemon._campaign_super_priority_args(cfg, "/srv/instance", 82),
        capture_output=True, text=True)
    assert failed.returncode == 100
    assert failed.stdout.splitlines() == ["rebuild:987654321"]
    assert next_item.returncode == 0
    assert next_item.stdout.splitlines() == ["rebuild:82", "trigger:82"]
    assert seen == [("rebuild", 987654321), ("trigger", 987654321),
                    ("rebuild", 82), ("trigger", 82)]
