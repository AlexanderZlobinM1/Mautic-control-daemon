from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcd_agent.commercial_portal import CommercialState
from mcd_agent.commercial_registration import SCHEMA, import_activation_material


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class PortalRegistrationTests(unittest.TestCase):
    def _material(self, *, fingerprint: str = "") -> dict[str, object]:
        signing = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {
            "schema": SCHEMA,
            "contract_version": "1.0",
            "portal_api_base": "https://portal.sales-snap.com/api/v1/commercial",
            "portal_agent_credential": "activation-derived-credential",
            "agent_id": "agent-existing",
            "host_fingerprint": fingerprint,
            "portal_job_signing_keys": {"portal-existing": _b64(signing)},
            "commercial_key_id": "commercial_key_existing",
            "installation_ids": {"cluster1.mail.m1.rs": "installation-existing"},
        }

    def test_import_material_is_atomic_complete_and_network_free(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            state.put("commercial_portal_cursor", "17")
            with patch("mcd_agent.commercial_registration._host_fingerprint", return_value="sha256:" + "a" * 64), patch(
                "mcd_agent.commercial_portal._host_fingerprint", return_value="sha256:" + "a" * 64
            ):
                receipt = import_activation_material(state, self._material())
            self.assertEqual(receipt["status"], "configured")
            self.assertEqual(receipt["agent_id"], "agent-existing")
            self.assertEqual(state.get("commercial_portal_cursor"), "17")
            with patch("mcd_agent.commercial_portal._host_fingerprint", return_value="sha256:" + "a" * 64):
                self.assertTrue(state.commercial_enabled)
            self.assertEqual(state.get("portal_agent_credential"), "activation-derived-credential")

    def test_import_rejects_host_mismatch_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            material = self._material(fingerprint="sha256:" + "b" * 64)
            with patch("mcd_agent.commercial_registration._host_fingerprint", return_value="sha256:" + "a" * 64):
                with self.assertRaisesRegex(ValueError, "portal_registration_host_mismatch"):
                    import_activation_material(state, material)
            self.assertIsNone(state.get("portal_agent_credential"))

    def test_import_preserves_existing_binding_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            material = self._material()
            with patch("mcd_agent.commercial_registration._host_fingerprint", return_value="sha256:" + "a" * 64), patch(
                "mcd_agent.commercial_portal._host_fingerprint", return_value="sha256:" + "a" * 64
            ):
                first = import_activation_material(state, material)
                second = import_activation_material(state, material)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(state.get("installation_ids"), {"cluster1.mail.m1.rs": "installation-existing"})

    def test_import_accepts_installation_bound_commercial_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            material = self._material()
            material.pop("portal_agent_credential")
            material["commercial_key"] = {
                "commercial_key_id": "commercial_key_existing",
                "secret": "installation-bound-secret",
                "display_name": "test-key",
            }
            with patch("mcd_agent.commercial_registration._host_fingerprint", return_value="sha256:" + "a" * 64):
                import_activation_material(state, material)
            self.assertEqual(state.get("portal_agent_credential"), "installation-bound-secret")
            self.assertEqual(state.get("commercial_key_metadata"), {"commercial_key_id": "commercial_key_existing", "display_name": "test-key"})

    def test_import_ignores_legacy_portal_artifact_trust(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "portal-artifact-trust-v1.json").read_text())
        material = self._material()
        material["portal_artifact_trust_v1"] = fixture["artifact_trust"]
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            with patch("mcd_agent.commercial_registration._host_fingerprint", return_value="sha256:" + "a" * 64):
                import_activation_material(state, material)
            self.assertIsNone(state.get("portal_artifact_trust_v1"))
            self.assertIsNone(state.get("portal_artifact_signing_keys"))

    def test_import_ignores_invalid_legacy_artifact_trust(self) -> None:
        material = self._material()
        material["portal_artifact_trust_v1"] = {"schema": "portal-artifact-trust-v1"}
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            with patch("mcd_agent.commercial_registration._host_fingerprint", return_value="sha256:" + "a" * 64):
                import_activation_material(state, material)
            self.assertEqual(state.get("portal_agent_credential"), "activation-derived-credential")

    def test_import_scrubs_legacy_agent_key_state_and_ignores_legacy_material(self) -> None:
        material = self._material()
        material["agent_private_key"] = "legacy-private-material"
        material["agent_key_id"] = "legacy-agent-key"
        with tempfile.TemporaryDirectory() as td:
            state = CommercialState(str(Path(td) / "state.sqlite"))
            state.put("agent_private_key", "legacy-private-state")
            state.put("agent_key_id", "legacy-agent-key")
            with patch("mcd_agent.commercial_registration._host_fingerprint", return_value="sha256:" + "a" * 64):
                import_activation_material(state, material)
            self.assertIsNone(state.get("agent_private_key"))
            self.assertIsNone(state.get("agent_key_id"))


if __name__ == "__main__":
    unittest.main()
