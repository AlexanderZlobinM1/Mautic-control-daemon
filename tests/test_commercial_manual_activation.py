from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.commercial_portal import CommercialState
from mcd_agent.commercial_manual_activation import (
    activate_manual_key,
    disconnect_manual_key,
    stage_manual_key,
)


class ManualCommercialActivationTests(unittest.TestCase):
    def _stage(self, state: CommercialState, register):
        with patch("mcd_agent.commercial_manual_activation._host_fingerprint", return_value="sha256:" + "a" * 64), patch(
            "mcd_agent.commercial_manual_activation.PortalClient.register_installation", side_effect=register
        ):
            return stage_manual_key(
                state,
                portal_url="https://portal.sales-snap.com",
                agent_id="agent-existing",
                commercial_key_id="commercial-key-existing",
                commercial_key="installation-bound-secret",
                local_identity="cluster1.mail.m1.rs",
                display_name="Cluster 1",
                mautic_version="7.2.2",
                application_root_fingerprint="sha256:" + "b" * 64,
            )

    def test_stage_is_pending_secret_free_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            register = lambda **_kwargs: {
                "schema": "portal-mcd-installation-register-response-v1",
                "contract_version": "1.0",
                "installation_id": "installation-existing",
            }
            first = self._stage(state, register)
            second = self._stage(state, register)
            self.assertEqual(first["status"], "pending_activation")
            self.assertTrue(second["idempotent"])
            self.assertNotIn("installation-bound-secret", json.dumps(first))
            self.assertEqual(state.get("commercial_portal_connection_state"), "pending_activation")
            self.assertEqual(state.get("commercial_agent_secret"), None)
            pending = state.get("commercial_pending_activation")
            assert pending["local_identity"] == "cluster1.mail.m1.rs"
            assert pending["display_name"] == "Cluster 1"
            assert pending["mautic_version"] == "7.2.2"
            assert pending["application_root_fingerprint"] == "sha256:" + "b" * 64

    def test_explicit_activate_disconnect_and_reactivate_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            self._stage(
                state,
                lambda **_kwargs: {"installation_id": "installation-existing", "schema": "portal-mcd-installation-register-response-v1"},
            )
            active = activate_manual_key(state)
            replay = activate_manual_key(state)
            disconnected = disconnect_manual_key(state)
            reactivated = activate_manual_key(state)
            self.assertEqual(active["status"], "active")
            self.assertTrue(replay["idempotent"])
            self.assertEqual(disconnected["status"], "disconnected")
            self.assertEqual(reactivated["status"], "active")
            self.assertEqual(state.get("portal_agent_credential"), "installation-bound-secret")
            self.assertFalse(bool(state.get("commercial_detached")))

    def test_entitlement_expiry_never_revokes_or_requires_new_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            self._stage(
                state,
                lambda **_kwargs: {"installation_id": "installation-existing", "schema": "portal-mcd-installation-register-response-v1"},
            )
            state.put("subscription_entitlement_expires_at", "2020-01-01T00:00:00Z")
            state.put("commercial_key_expires_at", "2020-01-01T00:00:00Z")
            activate_manual_key(state)
            self.assertEqual(state.get("commercial_portal_connection_state"), "active")
            self.assertFalse(bool(state.get("commercial_revoked")))
            self.assertFalse(bool(state.get("commercial_detached")))
            self.assertEqual(state.get("portal_agent_credential"), "installation-bound-secret")

    def test_failed_registration_keeps_pending_state_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            calls = []

            def register(**_kwargs):
                calls.append(True)
                if len(calls) == 1:
                    raise RuntimeError("transport without secret")
                return {"installation_id": "installation-existing", "schema": "portal-mcd-installation-register-response-v1"}

            first = self._stage(state, register)
            second = self._stage(state, register)
            self.assertEqual(first["status"], "pending_activation")
            self.assertEqual(second["pending_registration_id"], "installation-existing")
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
