from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import security_blocklist


def test_control_plane_ip_is_resolved_without_local_firewall_write():
    config = SimpleNamespace(mcc_url="http://65.21.54.91")
    assert security_blocklist._control_plane_addresses(config) == {"65.21.54.91"}


def test_sync_fails_closed_when_security_binding_is_absent():
    config = SimpleNamespace(mcc_url="http://65.21.54.91", custom_cache_dir="/tmp/no-security-binding")
    with tempfile.TemporaryDirectory() as td:
        with (
            patch.object(security_blocklist, "SYNC_LOCK_PATH", Path(td) / "security.lock"),
            patch.object(
                security_blocklist,
                "fetch_security_blocklist",
                return_value={"status": "ok", "profile": {"enabled": False}},
            ),
        ):
            result = security_blocklist.sync_security_blocklist_once(config)
    assert result["status"] == "error"
    assert result["reason"] == "security_external_binding_not_configured"


def test_failed_fetch_never_invokes_external_operation():
    with tempfile.TemporaryDirectory() as td:
        with (
            patch.object(security_blocklist, "SYNC_LOCK_PATH", Path(td) / "security.lock"),
            patch.object(
                security_blocklist,
                "fetch_security_blocklist",
                return_value={"status": "error", "reason": "snapshot stale"},
            ),
            patch.object(security_blocklist, "_configured_security_operation") as dispatch,
        ):
            result = security_blocklist.sync_security_blocklist_once(SimpleNamespace(mcc_url="http://65.21.54.91"))
    assert result["status"] == "error"
    dispatch.assert_not_called()
