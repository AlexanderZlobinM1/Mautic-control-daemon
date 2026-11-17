from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from mcd_agent import cli
from mcd_agent import plugins
from mcd_agent.config import load_config
from mcd_agent.plugin_update_policy import (
    PluginAutoUpdatePolicyError,
    normalize_policy,
    read_policy,
    terminal_result,
    validate_identity,
    write_policy,
)


INSTANCE_UID = "www-1d94d5bb"
PLUGIN_UID = "hostnet.auth:6-7"
STANDARD_PACKAGE_UID = "hostnet.auth.standard:6-7"
MANAGED_PACKAGE_UID = "hostnet.auth.managed:6-7"


def test_policy_rejects_host_qualified_instance_identity() -> None:
    with pytest.raises(PluginAutoUpdatePolicyError, match="exact_bare_uid"):
        validate_identity(
            instance_uid=INSTANCE_UID + "@host-a",
            plugin_uid=PLUGIN_UID,
            package_uid=STANDARD_PACKAGE_UID,
            operation_id="policy-1",
        )


def test_hostnet_variants_remain_independent_despite_shared_install_bundle() -> None:
    settings, changed = write_policy(
        {},
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        disabled=True,
    )
    assert changed is True
    settings, changed = write_policy(
        settings,
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=MANAGED_PACKAGE_UID,
        disabled=False,
    )
    assert changed is True
    assert read_policy(
        settings,
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
    ) is True
    assert read_policy(
        settings,
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=MANAGED_PACKAGE_UID,
    ) is False


def test_set_and_unset_are_idempotent_and_unset_is_explicit_tombstone() -> None:
    settings, first_set = write_policy(
        {},
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        disabled=True,
    )
    settings, second_set = write_policy(
        settings,
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        disabled=True,
    )
    settings, first_unset = write_policy(
        settings,
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        disabled=False,
    )
    settings, second_unset = write_policy(
        settings,
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        disabled=False,
    )
    assert (first_set, second_set, first_unset, second_unset) == (True, False, True, False)
    assert settings[INSTANCE_UID][STANDARD_PACKAGE_UID] == {
        "plugin_uid": PLUGIN_UID,
        "auto_update_disabled": False,
    }


def test_package_projection_cannot_be_rebound_to_another_plugin() -> None:
    settings, _ = write_policy(
        {},
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        disabled=True,
    )
    with pytest.raises(PluginAutoUpdatePolicyError, match="different_plugin"):
        write_policy(
            settings,
            instance_uid=INSTANCE_UID,
            plugin_uid="other.auth:6-7",
            package_uid=STANDARD_PACKAGE_UID,
            disabled=True,
        )
    with pytest.raises(PluginAutoUpdatePolicyError, match="different_plugin"):
        read_policy(
            settings,
            instance_uid=INSTANCE_UID,
            plugin_uid="other.auth:6-7",
            package_uid=STANDARD_PACKAGE_UID,
        )


def test_normalization_drops_invalid_rows_without_broadening_scope() -> None:
    assert normalize_policy({
        INSTANCE_UID + "@host-a": {
            STANDARD_PACKAGE_UID: {
                "plugin_uid": PLUGIN_UID,
                "auto_update_disabled": True,
            }
        },
        INSTANCE_UID: {
            STANDARD_PACKAGE_UID: {
                "plugin_uid": PLUGIN_UID,
                "auto_update_disabled": "yes",
            },
            MANAGED_PACKAGE_UID: {
                "plugin_uid": PLUGIN_UID,
                "auto_update_disabled": True,
            },
        },
    }) == {
        INSTANCE_UID: {
            MANAGED_PACKAGE_UID: {
                "plugin_uid": PLUGIN_UID,
                "auto_update_disabled": True,
            }
        }
    }


def test_terminal_receipt_hashes_exact_result() -> None:
    result = terminal_result(
        status="ok",
        action="set",
        operation_id="policy-1",
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        auto_update_disabled=True,
        changed=True,
        local_state_confirmed=True,
        mcc_sync={"status": "disabled"},
    )
    digest = result.pop("receipt_sha256")
    canonical = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    assert digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_auto_action_skips_only_exact_disabled_projection() -> None:
    rows = [
        {"plugin_uid": PLUGIN_UID, "package_uid": STANDARD_PACKAGE_UID},
        {"plugin_uid": PLUGIN_UID, "package_uid": MANAGED_PACKAGE_UID},
    ]
    settings, _ = write_policy(
        {},
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        disabled=True,
    )
    allowed, skipped = plugins._filter_auto_update_policy(
        SimpleNamespace(plugin_auto_update_instance_settings=settings),
        SimpleNamespace(instance_uid=INSTANCE_UID),
        rows,
    )
    assert allowed == [rows[1]]
    assert skipped == [rows[0]]


def test_cli_set_get_unset_persists_without_plugin_mutation(tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "mcd.toml"
    config_path.write_text(
        '[profile]\nname = "custom"\n\n[runtime]\nplugin_auto_update_instance_settings = {}\n',
        encoding="utf-8",
    )
    cfg = load_config(str(config_path), allow_recover_from_mcc=False)
    monkeypatch.setattr(
        cli,
        "_select_instance_for_ops",
        lambda _cfg, selector: SimpleNamespace(instance_uid=selector),
    )
    monkeypatch.setattr(cli, "touch_poll_trigger", lambda _cfg: "trigger")
    monkeypatch.setattr(
        cli,
        "run_plugins_machine",
        lambda **_kwargs: pytest.fail("policy changes must not mutate plugin files or versions"),
    )

    def run(action: str, operation_id: str) -> dict[str, object]:
        current = load_config(str(config_path), allow_recover_from_mcc=False)
        args = SimpleNamespace(
            plugin_auto_update_policy_cmd=action,
            instance_uid=INSTANCE_UID,
            plugin_uid=PLUGIN_UID,
            package_uid=STANDARD_PACKAGE_UID,
            operation_id=operation_id,
        )
        assert cli._plugin_auto_update_policy_cli(current, args) == 0
        return json.loads(capsys.readouterr().out)

    set_result = run("set", "policy-set-1")
    repeated_set = run("set", "policy-set-2")
    get_result = run("get", "policy-get-1")
    unset_result = run("unset", "policy-unset-1")
    repeated_unset = run("unset", "policy-unset-2")

    assert set_result["auto_update_disabled"] is True and set_result["changed"] is True
    assert repeated_set["changed"] is False
    assert get_result["auto_update_disabled"] is True and get_result["changed"] is False
    assert unset_result["auto_update_disabled"] is False and unset_result["changed"] is True
    assert repeated_unset["changed"] is False
    reloaded = load_config(str(config_path), allow_recover_from_mcc=False)
    assert reloaded.plugin_auto_update_instance_settings[INSTANCE_UID][STANDARD_PACKAGE_UID] == {
        "plugin_uid": PLUGIN_UID,
        "auto_update_disabled": False,
    }


def test_cli_reports_preserved_local_state_when_mcc_push_raises(tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "mcd.toml"
    config_path.write_text(
        '[profile]\nname = "custom"\n\n[runtime]\nplugin_auto_update_instance_settings = {}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "_select_instance_for_ops",
        lambda _cfg, selector: SimpleNamespace(instance_uid=selector),
    )
    monkeypatch.setattr(cli, "touch_poll_trigger", lambda _cfg: "trigger")
    monkeypatch.setattr(cli, "push_runtime_overrides", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    args = SimpleNamespace(
        plugin_auto_update_policy_cmd="set",
        instance_uid=INSTANCE_UID,
        plugin_uid=PLUGIN_UID,
        package_uid=STANDARD_PACKAGE_UID,
        operation_id="policy-partial-1",
    )

    cfg = load_config(str(config_path), allow_recover_from_mcc=False)
    assert cli._plugin_auto_update_policy_cli(cfg, args) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "error"
    assert failed["reason"] == "offline"
    assert failed["auto_update_disabled"] is True
    assert failed["changed"] is True
    assert failed["local_state_confirmed"] is True

    monkeypatch.setattr(cli, "push_runtime_overrides", lambda *_args, **_kwargs: {"status": "disabled"})
    args.operation_id = "policy-partial-2"
    reloaded = load_config(str(config_path), allow_recover_from_mcc=False)
    assert cli._plugin_auto_update_policy_cli(reloaded, args) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried["auto_update_disabled"] is True
    assert retried["changed"] is False
    assert retried["local_state_confirmed"] is True
