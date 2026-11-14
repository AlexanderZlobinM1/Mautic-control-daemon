from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mcd_agent import security_blocklist


def _runtime() -> dict[str, object]:
    return {
        "managed_root": "/var/lib/mcd/security-os",
        "enabled": True,
        "allowlist": [],
        "blocked": [],
        "required_allowlist": [],
        "filter_path": "etc/fail2ban/filter.d/mcc-global.conf",
        "auth_filter_path": "etc/fail2ban/filter.d/mautic-auth-nginx.conf",
        "jail_path": "etc/fail2ban/jail.d/98-mcd-security.local",
        "log_path": "var/log/fail2ban-mcc-global.log",
        "state_path": "var/lib/mcd/security-state.json",
        "client_path": "bin/fail2ban-client",
        "jail_name": "mcc-global",
        "ssh_jails": ["sshd"],
        "action_template": "",
        "generated_at": "test",
        "validate_command": [],
        "reload_command": [],
        "ban_command_prefix": ["bin/ban"],
        "unban_command_prefix": ["bin/unban"],
    }


def _public_result(operation: str, status: str = "noop") -> str:
    nested_operation = {
        "security_blocklist_apply": "apply",
        "security_blocklist_status": "status",
        "security_blocklist_cleanup": "cleanup",
    }[operation]
    return json.dumps(
        {
            "schema": "mcd-security-os-v1",
            "contract_version": "1.0",
            "component": "security_os",
            "operation": nested_operation,
            "status": status,
            "changed_paths": [],
            "mutation_started": False,
        }
    )


@pytest.mark.parametrize(
    "operation,status",
    [
        ("security_blocklist_apply", "noop"),
        ("security_blocklist_status", "status"),
        ("security_blocklist_cleanup", "noop"),
    ],
)
def test_external_security_operation_maps_all_declared_operations(monkeypatch, operation, status):
    calls = []

    def dispatch(config, *, component, operation, profile, operation_input):
        calls.append((component, operation, profile, operation_input))
        return 0, _public_result(operation, status)

    monkeypatch.setattr(security_blocklist, "_security_external_binding_present", lambda *_args: True)
    monkeypatch.setattr(security_blocklist, "run_configured_external_operation", dispatch)

    result = security_blocklist._configured_security_operation(
        SimpleNamespace(custom_cache_dir="/tmp/mcd-security-test-cache"),
        operation=operation,
        profile={"runtime": _runtime()},
        required_allowlist={"65.21.54.91"},
    )

    assert result["status"] == status
    assert calls[0][0:3] == ("security_blocklist", operation, {})
    assert calls[0][3]["managed_root"] == "var/lib/mcd/security-os"
    assert calls[0][3]["filter_path"] == "etc/fail2ban/filter.d/mcc-global.conf"
    if operation != "security_blocklist_apply":
        assert set(calls[0][3]) == {
            "managed_root",
            "filter_path",
            "auth_filter_path",
            "jail_path",
            "log_path",
            "state_path",
            "jail_name",
        }


def test_external_security_apply_overrides_policy_addresses_and_required_allowlist(monkeypatch):
    captured = {}

    def dispatch(_config, *, component, operation, profile, operation_input):
        captured.update(component=component, operation=operation, profile=profile, runtime=operation_input)
        return 0, _public_result(operation, "applied")

    monkeypatch.setattr(security_blocklist, "_security_external_binding_present", lambda *_args: True)
    monkeypatch.setattr(security_blocklist, "run_configured_external_operation", dispatch)
    result = security_blocklist._configured_security_operation(
        SimpleNamespace(custom_cache_dir="/tmp/mcd-security-test-cache"),
        operation="security_blocklist_apply",
        profile={
            "runtime": _runtime(),
            "allowlist": ["65.21.54.91", "not-an-ip"],
            "blocked": ["8.8.8.8", "not-an-ip"],
            "enabled": True,
        },
        required_allowlist={"65.21.54.91"},
    )

    assert result["status"] == "applied"
    assert captured["component"] == "security_blocklist"
    assert captured["runtime"]["allowlist"] == ["65.21.54.91"]
    assert captured["runtime"]["blocked"] == ["8.8.8.8"]
    assert captured["runtime"]["required_allowlist"] == ["65.21.54.91"]


@pytest.mark.parametrize("value", ["/", "../escape", "var/../escape", "var//escape", "./escape"])
def test_external_security_managed_root_rejects_empty_or_traversal(value):
    with pytest.raises(ValueError, match="security_external_managed_root_invalid"):
        security_blocklist._security_root_relative_path(value)


def test_external_security_managed_root_normalizes_absolute_root():
    assert security_blocklist._security_root_relative_path("/var/lib/mcd/security-os") == "var/lib/mcd/security-os"


def test_external_security_paths_reject_absolute_or_traversal():
    with pytest.raises(ValueError, match="security_external_filter_path_invalid"):
        security_blocklist._security_relative_path("/etc/fail2ban", "filter_path")
    with pytest.raises(ValueError, match="security_external_filter_path_invalid"):
        security_blocklist._security_relative_path("etc/../fail2ban", "filter_path")


@pytest.mark.parametrize(
    "value,expected",
    [(None, 300), ("", 300), ("bad", 300), (1, 30), (30, 30), (3600, 3600), (9999, 3600)],
)
def test_security_poll_interval_has_default_and_clamp(value, expected):
    assert security_blocklist._security_poll_interval(value) == expected


def test_bound_external_failure_never_silently_falls_back(monkeypatch):
    def dispatch(*_args, **_kwargs):
        return 2, "custom_operation_binding_not_configured"

    monkeypatch.setattr(security_blocklist, "_security_external_binding_present", lambda *_args: True)
    monkeypatch.setattr(security_blocklist, "run_configured_external_operation", dispatch)

    result = security_blocklist._configured_security_operation(
        SimpleNamespace(custom_cache_dir="/tmp/mcd-security-test-cache"),
        operation="security_blocklist_apply",
        profile={"runtime": _runtime()},
        required_allowlist=set(),
    )

    assert result == {"status": "error", "reason": "security_external_binding_lost"}


def test_bound_external_sync_skips_builtin_control_plane_writer(monkeypatch, tmp_path):
    cfg = SimpleNamespace(custom_cache_dir=str(tmp_path), mcc_url="http://65.21.54.91")
    profile = {"runtime": _runtime(), "enabled": True, "blocked": ["8.8.8.8"], "poll_sec": 9999}
    monkeypatch.setattr(security_blocklist, "fetch_security_blocklist", lambda _cfg: {"status": "ok", "profile": profile})
    monkeypatch.setattr(security_blocklist, "_security_external_binding_present", lambda *_args: True)
    monkeypatch.setattr(
        security_blocklist,
        "_configured_security_operation",
        lambda *_args, **_kwargs: {"status": "applied", "changed_paths": []},
    )
    monkeypatch.setattr(security_blocklist, "SYNC_LOCK_PATH", tmp_path / "security.lock")

    result = security_blocklist.sync_security_blocklist_once(cfg)

    assert result["status"] == "applied"
    assert result["apply"]["poll_sec"] == 3600

