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


def test_catalog_plugin_operation_runtime_settings_are_stable() -> None:
    assert "plugin_operation_instance_settings" in daemon._STABLE_RUNTIME_KEYS
    assert not any("housekeeping" in key or "viber" in key for key in daemon._STABLE_RUNTIME_KEYS)


def test_segment_whitelist_instance_runtime_keys_are_stable() -> None:
    expected = {
        "segment_whitelist_instance_settings",
        "campaign_whitelist_instance_settings",
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
