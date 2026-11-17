from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from mcd_agent import uninstall


MANAGED = (
    "# MCD_MANAGED 2026-11-17: disabled by mcd profile=active\n"
    "# */5 * * * * php /var/www/example/bin/console mautic:segments:update\n"
)


def _sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install = tmp_path / "opt" / "mcd"
    etc = tmp_path / "etc" / "mcd"
    systemd = tmp_path / "etc" / "systemd" / "system"
    wrapper = tmp_path / "usr" / "local" / "bin" / "mcd-cli"
    (install / "var" / "backup").mkdir(parents=True)
    etc.mkdir(parents=True)
    systemd.mkdir(parents=True)
    wrapper.parent.mkdir(parents=True)
    (etc / "mcd.toml").write_text("[runtime]\n", encoding="utf-8")
    wrapper.write_text("#!/bin/sh\nexec /opt/mcd/venv/bin/python -m mcd_agent \"$@\"\n", encoding="utf-8")
    monkeypatch.setattr(uninstall.os, "geteuid", lambda: 0)
    monkeypatch.setattr(uninstall, "SYSTEMD_DIR", systemd)
    monkeypatch.setattr(uninstall, "WRAPPER_PATH", wrapper)
    return install, etc, systemd, wrapper


def _cron_fakes(monkeypatch: pytest.MonkeyPatch, initial: dict[str, str], *, fail_user: str = ""):
    state = dict(initial)
    writes: list[tuple[str, str]] = []
    failure_injected = False

    def read(user=None):
        key = user or "root"
        if key not in state:
            return 1, "", f"no crontab for {key}"
        return 0, state[key], ""

    def write(content, user=None):
        nonlocal failure_injected
        key = user or "root"
        writes.append((key, content))
        if key == fail_user and not failure_injected:
            failure_injected = True
            return 73, "injected write error"
        state[key] = content
        return 0, ""

    monkeypatch.setattr(uninstall, "_read_crontab", read)
    monkeypatch.setattr(uninstall, "_write_crontab", write)
    return state, writes


def _systemctl_ok(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    service = {"active": True, "enabled": True}

    def run(cmd):
        calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            return (0, "active") if service["active"] else (3, "inactive")
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return (0, "enabled") if service["enabled"] else (1, "disabled")
        if cmd[:2] == ["systemctl", "stop"]:
            service["active"] = False
        elif cmd[:2] == ["systemctl", "disable"]:
            service["enabled"] = False
        elif cmd[:2] == ["systemctl", "start"]:
            service["active"] = True
        return 0, ""

    monkeypatch.setattr(uninstall, "_run", run)
    return calls


def test_uninstall_restores_only_marker_pairs_and_preserves_unrelated_cron(monkeypatch, tmp_path):
    install, etc, systemd, wrapper = _sandbox(monkeypatch, tmp_path)
    backup = install / "var" / "backup" / "root.pre-active.crontab"
    backup.write_text("0 0 * * * old-before-takeover\n", encoding="utf-8")
    unit = systemd / "mcd.service"
    unit.write_text("[Unit]\nDescription=MCD Agent\n[Service]\nExecStart=/opt/mcd/venv/bin/python -m mcd_agent\n", encoding="utf-8")
    wants = systemd / "multi-user.target.wants"
    wants.mkdir()
    (wants / "mcd.service").symlink_to(unit)
    root_new = "17 4 * * * /usr/local/bin/foreign-root-sentinel\n"
    www_new = "29 4 * * * /usr/local/bin/foreign-www-sentinel\n"
    state, writes = _cron_fakes(
        monkeypatch,
        {"root": MANAGED + root_new, "www-data": www_new + MANAGED},
    )
    calls = _systemctl_ok(monkeypatch)
    foreign_sibling = tmp_path / "var" / "www" / "mautic"
    foreign_sibling.mkdir(parents=True)
    foreign_file = foreign_sibling / "foreign-process-sentinel"
    foreign_file.write_text("must survive", encoding="utf-8")

    result = uninstall.run_uninstall(
        service_name="mcd",
        install_dir=str(install),
        etc_dir=str(etc),
        operation_id="op-success",
    )

    assert result.ok is True
    assert result.receipt["outcome"] == "removed"
    assert result.receipt["operation_id"] == "op-success"
    assert result.receipt["cron"]["root"]["status"] == "restored"
    assert result.receipt["cron"]["www-data"]["status"] == "restored"
    assert "*/5 * * * * php /var/www/example/bin/console mautic:segments:update\n" in state["root"]
    assert root_new in state["root"]
    assert www_new in state["www-data"]
    assert "old-before-takeover" not in state["root"]
    assert {user for user, _content in writes} == {"root", "www-data"}
    assert ["systemctl", "stop", "mcd"] in calls
    assert ["systemctl", "disable", "mcd"] in calls
    assert ["systemctl", "daemon-reload"] in calls
    assert not install.exists()
    assert not etc.exists()
    assert not wrapper.exists()
    assert foreign_file.read_text(encoding="utf-8") == "must survive"
    assert not any(cmd[0] in {"kill", "killall", "pkill"} for cmd in calls)

    payload = dict(result.receipt)
    digest = payload.pop("receipt_sha256")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert digest == hashlib.sha256(canonical).hexdigest()


def test_systemctl_disable_may_remove_enable_alias_before_owned_cleanup(monkeypatch, tmp_path):
    install, etc, systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    unit = systemd / "mcd.service"
    unit.write_text("[Unit]\nDescription=MCD Agent\n[Service]\nExecStart=/opt/mcd/venv/bin/python -m mcd_agent\n", encoding="utf-8")
    wants = systemd / "multi-user.target.wants"
    wants.mkdir()
    alias = wants / "mcd.service"
    alias.symlink_to(unit)
    _state, _writes = _cron_fakes(monkeypatch, {"root": "foreign\n", "www-data": "foreign\n"})
    service = {"active": True, "enabled": True}

    def run(cmd):
        if cmd[:2] == ["systemctl", "is-active"]:
            return (0, "active") if service["active"] else (3, "inactive")
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return (0, "enabled") if service["enabled"] else (1, "disabled")
        if cmd[:2] == ["systemctl", "stop"]:
            service["active"] = False
        if cmd[:2] == ["systemctl", "disable"]:
            service["enabled"] = False
            alias.unlink()
        return 0, ""

    monkeypatch.setattr(uninstall, "_run", run)
    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is True
    assert result.receipt["outcome"] == "removed"
    assert not unit.exists() and not alias.exists()


def test_cron_write_failure_is_terminal_and_keeps_restore_state(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    backup = install / "var" / "backup" / "root.pre-active.crontab"
    backup.write_text("pre-takeover\n", encoding="utf-8")
    state, _writes = _cron_fakes(
        monkeypatch,
        {"root": MANAGED + "foreign-root\n", "www-data": "foreign-www\n"},
        fail_user="root",
    )
    _systemctl_ok(monkeypatch)

    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["status"] == "failed"
    assert result.receipt["error_code"] == "cron_write_failed"
    assert install.exists() and etc.exists() and backup.exists()
    assert state["root"] == MANAGED + "foreign-root\n"
    assert str(install) in result.receipt["preserved_paths"]
    assert result.receipt["steps"][-1]["name"] == "cron_failure_recovery"
    assert result.receipt["steps"][-1]["service_restored"] is True


def test_www_failure_rolls_back_root_and_restarts_previously_active_service(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    root_original = MANAGED + "foreign-root\n"
    www_original = MANAGED + "foreign-www\n"
    state, _writes = _cron_fakes(
        monkeypatch,
        {"root": root_original, "www-data": www_original},
        fail_user="www-data",
    )
    calls = _systemctl_ok(monkeypatch)

    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "cron_write_failed"
    assert state["root"] == root_original
    assert state["www-data"] == www_original
    assert ["systemctl", "start", "mcd"] in calls
    assert ["systemctl", "disable", "mcd"] not in calls
    assert result.receipt["steps"][-1]["cron_restored"] is True
    assert result.receipt["steps"][-1]["service_restored"] is True
    assert install.exists() and etc.exists()


def test_systemctl_failure_prevents_cron_and_purge(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    _state, writes = _cron_fakes(monkeypatch, {"root": MANAGED, "www-data": "foreign\n"})

    def run(cmd):
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, "active"
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return 0, "enabled"
        if cmd == ["systemctl", "stop", "mcd"]:
            return 1, "permission denied"
        return 0, ""

    monkeypatch.setattr(uninstall, "_run", run)
    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "service_stop_failed"
    assert writes == []
    assert install.exists() and etc.exists()


def test_cron_read_failure_prevents_first_mutation(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    calls = _systemctl_ok(monkeypatch)

    def read(user=None):
        if user is None:
            return 74, "", "injected read error"
        return 0, "foreign\n", ""

    monkeypatch.setattr(uninstall, "_read_crontab", read)
    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "cron_read_failed"
    assert calls == []
    assert install.exists() and etc.exists()


def test_cron_readback_mismatch_is_terminal_and_keeps_runtime(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    crons = {"root": MANAGED, "www-data": "foreign\n"}

    def read(user=None):
        return 0, crons[user or "root"], ""

    def write_without_applying(_content, _user=None):
        return 0, ""

    monkeypatch.setattr(uninstall, "_read_crontab", read)
    monkeypatch.setattr(uninstall, "_write_crontab", write_without_applying)
    _systemctl_ok(monkeypatch)
    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "cron_verify_failed"
    assert install.exists() and etc.exists()


def test_malformed_marker_fails_before_first_mutation(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    _state, writes = _cron_fakes(
        monkeypatch,
        {"root": "# MCD_MANAGED broken\nnot-commented\n", "www-data": "foreign\n"},
    )
    calls = _systemctl_ok(monkeypatch)

    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "cron_marker_incomplete"
    assert calls == []
    assert writes == []
    assert install.exists() and etc.exists()


def test_foreign_top_level_file_blocks_purge_without_mutation(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    foreign = install / "customer-data.txt"
    foreign.write_text("preserve", encoding="utf-8")
    _state, writes = _cron_fakes(monkeypatch, {"root": MANAGED, "www-data": "foreign\n"})
    calls = _systemctl_ok(monkeypatch)

    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "foreign_path_present"
    assert foreign.read_text(encoding="utf-8") == "preserve"
    assert calls == []
    assert writes == []


@pytest.mark.parametrize("name", ["foreign.key", "foreign.pem", "foreign.crt", "mcd-foreign.conf"])
def test_foreign_config_name_or_extension_does_not_imply_ownership(monkeypatch, tmp_path, name):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    foreign = etc / name
    foreign.write_text("foreign certificate or config", encoding="utf-8")
    _state, writes = _cron_fakes(monkeypatch, {"root": MANAGED, "www-data": "foreign\n"})
    calls = _systemctl_ok(monkeypatch)

    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "foreign_path_present"
    assert foreign.read_text(encoding="utf-8") == "foreign certificate or config"
    assert calls == []
    assert writes == []


def test_delete_error_is_not_reported_as_success(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    _state, _writes = _cron_fakes(monkeypatch, {"root": "foreign\n", "www-data": "foreign\n"})
    _systemctl_ok(monkeypatch)
    original_remove = uninstall._remove_path

    def fail_install(path):
        if path == install:
            raise uninstall._UninstallFailure("path_remove_failed", "injected delete error")
        return original_remove(path)

    monkeypatch.setattr(uninstall, "_remove_path", fail_install)
    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "path_remove_failed"
    assert install.exists() and etc.exists()


def test_daemon_reload_failure_is_terminal_and_preserves_recovery_state(monkeypatch, tmp_path):
    install, etc, systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    backup = install / "var" / "backup" / "root.pre-active.crontab"
    backup.write_text("pre-takeover\n", encoding="utf-8")
    unit = systemd / "mcd.service"
    unit.write_text("[Unit]\nDescription=MCD Agent\n[Service]\nExecStart=/opt/mcd/venv/bin/python -m mcd_agent\n", encoding="utf-8")
    _state, _writes = _cron_fakes(monkeypatch, {"root": "foreign\n", "www-data": "foreign\n"})

    service = {"active": True, "enabled": True}

    def run(cmd):
        if cmd[:2] == ["systemctl", "is-active"]:
            return (0, "active") if service["active"] else (3, "inactive")
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return (0, "enabled") if service["enabled"] else (1, "disabled")
        if cmd[:2] == ["systemctl", "stop"]:
            service["active"] = False
            return 0, ""
        if cmd[:2] == ["systemctl", "disable"]:
            service["enabled"] = False
            return 0, ""
        if cmd == ["systemctl", "daemon-reload"]:
            return 1, "injected reload error"
        return 0, ""

    monkeypatch.setattr(uninstall, "_run", run)
    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "daemon_reload_failed"
    assert install.exists() and etc.exists() and backup.exists()
    assert not unit.exists()


def test_symlink_purge_root_is_rejected_before_mutation(monkeypatch, tmp_path):
    target = tmp_path / "runtime-target"
    target.mkdir()
    install = tmp_path / "opt" / "mcd"
    install.parent.mkdir(parents=True)
    install.symlink_to(target, target_is_directory=True)
    etc = tmp_path / "etc" / "mcd"
    etc.mkdir(parents=True)
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    monkeypatch.setattr(uninstall.os, "geteuid", lambda: 0)
    monkeypatch.setattr(uninstall, "SYSTEMD_DIR", systemd)
    monkeypatch.setattr(uninstall, "WRAPPER_PATH", tmp_path / "missing-wrapper")
    calls = _systemctl_ok(monkeypatch)

    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert result.ok is False
    assert result.receipt["error_code"] == "unsafe_purge_path"
    assert calls == []
    assert install.is_symlink() and target.exists()


def test_repeated_uninstall_returns_already_absent_without_systemctl(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    state, _writes = _cron_fakes(monkeypatch, {"root": "foreign\n", "www-data": "foreign\n"})
    calls = _systemctl_ok(monkeypatch)

    first = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))
    assert first.ok is True
    calls.clear()
    second = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc))

    assert second.ok is True
    assert second.receipt["outcome"] == "already_absent"
    assert calls == []
    assert state == {"root": "foreign\n", "www-data": "foreign\n"}


def test_no_markers_never_rewrites_crontab(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    root = "MAILTO=ops@example.test\n3 2 * * * /usr/local/bin/custom\n"
    www = "7 2 * * * /usr/local/bin/custom-www\n"
    state, writes = _cron_fakes(monkeypatch, {"root": root, "www-data": www})
    _systemctl_ok(monkeypatch)

    result = uninstall.run_uninstall(install_dir=str(install), etc_dir=str(etc), purge=False)

    assert result.ok is True
    assert writes == []
    assert state == {"root": root, "www-data": www}
    assert result.receipt["cron"]["root"]["status"] == "unchanged"
    assert install.exists() and etc.exists()


def test_machine_line_is_single_parseable_terminal_result(monkeypatch, tmp_path):
    install, etc, _systemd, _wrapper = _sandbox(monkeypatch, tmp_path)
    _state, _writes = _cron_fakes(monkeypatch, {"root": "foreign\n", "www-data": "foreign\n"})
    _systemctl_ok(monkeypatch)

    result = uninstall.run_uninstall(
        install_dir=str(install), etc_dir=str(etc), purge=False, operation_id="mcc-op-1"
    )
    line = result.machine_line()

    assert line.startswith(uninstall.RESULT_PREFIX)
    payload = json.loads(line.removeprefix(uninstall.RESULT_PREFIX))
    assert payload["schema"] == "mcd-uninstall-result-v1"
    assert payload["terminal"] is True
    assert payload["operation_id"] == "mcc-op-1"


def test_cli_machine_result_is_one_line_and_keeps_yes_as_separate_confirmation(monkeypatch, capsys):
    from mcd_agent import cli

    receipt = {
        "schema": "mcd-uninstall-result-v1",
        "terminal": True,
        "ok": True,
        "operation_id": "mcc-op-cli",
    }
    result = uninstall.UninstallResult(ok=True, lines=["human-only"], receipt=receipt)
    calls = []

    def fake_run_uninstall(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(cli, "run_uninstall", fake_run_uninstall)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mcd-cli", "uninstall", "--yes", "--machine-result", "--operation-id", "mcc-op-cli"],
    )
    assert cli.main() == 0
    output = capsys.readouterr().out.splitlines()
    assert output == [uninstall.RESULT_PREFIX + json.dumps(receipt, sort_keys=True, separators=(",", ":"))]
    assert calls == [{
        "service_name": "mcd",
        "install_dir": "/opt/mcd",
        "etc_dir": "/etc/mcd",
        "purge": True,
        "operation_id": "mcc-op-cli",
    }]

    calls.clear()
    monkeypatch.setattr(cli, "_ask", lambda _prompt: "n")
    monkeypatch.setattr(sys, "argv", ["mcd-cli", "uninstall", "--machine-result"])
    assert cli.main() == 1
    assert calls == []
