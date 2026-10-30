from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon


def test_page_hits_cleanup_runtime_keys_are_stable() -> None:
    expected = {
        "enable_page_hits_orphan_cleanup",
        "page_hits_orphan_cleanup_interval_sec",
        "page_hits_orphan_cleanup_quiet_hour",
        "page_hits_orphan_cleanup_quiet_window_min",
        "page_hits_orphan_cleanup_batch_size",
        "page_hits_orphan_cleanup_batches_per_run",
        "page_hits_orphan_cleanup_sleep_sec",
        "page_hits_orphan_cleanup_grace_min",
        "page_hits_orphan_cleanup_max_run_sec",
        "page_hits_orphan_cleanup_instance_settings",
    }

    assert expected <= daemon._STABLE_RUNTIME_KEYS


def test_catalog_plugin_operation_settings_are_stable_without_catalog_definitions() -> None:
    assert "plugin_operation_instance_settings" in daemon._STABLE_RUNTIME_KEYS
    assert "plugin_operations" in daemon._STABLE_RUNTIME_KEYS
    assert not any(
        name in key
        for key in daemon._STABLE_RUNTIME_KEYS
        for name in ("viber", "housekeeping", "oracle", "ohip", "mailru", "woocommerce")
    )


def test_segment_whitelist_instance_runtime_keys_are_stable() -> None:
    expected = {
        "segment_whitelist_instance_settings",
        "campaign_whitelist_instance_settings",
        "segment_recurring_priority_v1",
    }

    assert expected <= daemon._STABLE_RUNTIME_KEYS


def test_monitored_email_parser_runtime_keys_are_stable() -> None:
    expected = {
        "monitored_email_parser_enabled",
        "monitored_email_parser_interval_sec",
        "monitored_email_parser_batch_size",
        "monitored_email_parser_force_seen",
        "monitored_email_parser_delete_processed",
        "monitored_email_parser_disable_mautic_fetch",
        "monitored_email_parser_types",
        "monitored_email_parser_whitelist",
        "monitored_email_parser_instance_settings",
    }

    assert expected <= daemon._STABLE_RUNTIME_KEYS


def test_oracle_sync_uses_catalog_settings_instead_of_agent_stable_keys() -> None:
    assert not any("oracle" in key or "ohip" in key for key in daemon._STABLE_RUNTIME_KEYS)


def test_message_queue_runtime_keys_are_stable() -> None:
    expected = {
        "message_queue_enabled",
        "message_queue_interval_sec",
        "message_queue_instance_settings",
    }

    assert expected <= daemon._STABLE_RUNTIME_KEYS


def test_form_embed_status_reconcile_publishes_observed_state() -> None:
    cfg = SimpleNamespace(
        form_embed_instance_settings={"instance-1": {"enabled": True}},
    )
    observed = {"instance-1": {"status": "applied", "vhosts": [{"result": "managed"}]}}
    with (
        patch.object(daemon, "sync_form_embed_settings", return_value={"instances": observed}) as sync,
        patch.object(daemon, "push_runtime_overrides", return_value={"status": "ok"}) as push,
    ):
        assert daemon._sync_and_publish_form_embed_status(cfg, ["install"], reason="test") is True

    sync.assert_called_once_with(cfg, ["install"])
    push.assert_called_once_with(
        cfg,
        {"form_embed_instance_status": observed},
        merge=True,
    )


def test_form_embed_status_reconcile_skips_hosts_without_policy() -> None:
    cfg = SimpleNamespace(form_embed_instance_settings={})
    with (
        patch.object(daemon, "sync_form_embed_settings") as sync,
        patch.object(daemon, "push_runtime_overrides") as push,
    ):
        assert daemon._sync_and_publish_form_embed_status(cfg, ["install"], reason="test") is False

    sync.assert_not_called()
    push.assert_not_called()


def test_form_policy_change_and_explicit_trigger_reconcile_without_restart() -> None:
    cfg = SimpleNamespace(form_embed_instance_settings={"app": {"enabled": True}}, poll_interval_sec=60)
    state = {}
    with patch.object(daemon, "_sync_and_publish_form_embed_status", return_value=True) as sync:
        daemon._reconcile_form_embed_runtime(cfg, [], state, now=10)
        daemon._reconcile_form_embed_runtime(cfg, [], state, now=11)
        assert sync.call_count == 1
        cfg.form_embed_instance_settings["app"]["cors_origins"] = ["https://customer.example"]
        daemon._reconcile_form_embed_runtime(cfg, [], state, now=12)
        assert sync.call_count == 2
        daemon._reconcile_form_embed_runtime(cfg, [], state, now=13, force=True)
        assert sync.call_count == 3


def test_form_reconcile_failed_effect_or_delivery_retries_and_inventory_change_is_immediate() -> None:
    cfg = SimpleNamespace(form_embed_instance_settings={"app": {"enabled": True}}, poll_interval_sec=60)
    state = {}
    with patch.object(daemon, "_sync_and_publish_form_embed_status", side_effect=[False, False, True]) as sync:
        daemon._reconcile_form_embed_runtime(cfg, [], state, now=10)
        daemon._reconcile_form_embed_runtime(cfg, [], state, now=20)
        assert sync.call_count == 1
        daemon._reconcile_form_embed_runtime(cfg, [], state, now=70)
        assert sync.call_count == 2
        daemon._reconcile_form_embed_runtime(cfg, [SimpleNamespace(instance_uid="app", root="/app")], state, now=71)
        assert sync.call_count == 3


def test_form_reconcile_no_policy_never_touches_nginx() -> None:
    with patch.object(daemon, "_sync_and_publish_form_embed_status") as sync:
        daemon._reconcile_form_embed_runtime(SimpleNamespace(form_embed_instance_settings={}), [], {}, now=1, force=True)
    sync.assert_not_called()


def test_form_local_apply_does_not_wait_for_mcc_delivery_and_standalone_is_complete() -> None:
    cfg = SimpleNamespace(form_embed_instance_settings={"app": {"enabled": True}})
    with (
        patch.object(daemon, "sync_form_embed_settings", return_value={"status": "ok", "instances": {"app": {"status": "applied"}}}) as sync,
        patch.object(daemon, "push_runtime_overrides", side_effect=[{"status": "error", "reason": "http_400"}, {"status": "disabled"}]),
    ):
        assert daemon._sync_and_publish_form_embed_status(cfg, [], reason="test") is False
        assert daemon._sync_and_publish_form_embed_status(cfg, [], reason="test") is True
    assert sync.call_count == 2


def test_form_blocked_or_empty_inventory_is_not_complete_even_when_mcc_accepts_status() -> None:
    cfg = SimpleNamespace(form_embed_instance_settings={"app": {"enabled": True}})
    with (
        patch.object(daemon, "sync_form_embed_settings", side_effect=[{"status": "ok", "instances": {"app": {"status": "blocked"}}}, {"status": "ok", "instances": {}}]),
        patch.object(daemon, "push_runtime_overrides", return_value={"status": "ok"}),
    ):
        assert daemon._sync_and_publish_form_embed_status(cfg, [], reason="test") is False
        assert daemon._sync_and_publish_form_embed_status(cfg, [], reason="test") is False
