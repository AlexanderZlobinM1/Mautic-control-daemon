from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mcd_agent.commercial_detector import (
    APPLICABILITY_FACTS_CONTRACT_VERSION,
    APPLICABILITY_FACTS_SCHEMA,
    PORTAL_LOCAL_FACTS_CAPABILITIES,
    applicability_facts_digest,
    build_applicability_facts_snapshot,
    normalize_applicability_facts,
)
from mcd_agent.commercial_portal import CommercialState, PortalClient, build_standalone_settings_snapshot, canonical_json
from mcd_agent.commercial_self_hosted import SelfHostedPortalWorker, SelfHostedScenarioExecutor


FACTS = {
    "os_id": "ubuntu",
    "os_version": "24.04",
    "architecture": "amd64",
    "package_manager": "apt-get",
    "capabilities": sorted(PORTAL_LOCAL_FACTS_CAPABILITIES),
    "target_kind": "existing_server",
}
FACTS_WITH_SCHEMA = {**FACTS, "detector_schema": "mcd-scenario-detector-v1"}
EXPECTED_DIGEST = "sha256:00e5053242ac752adfe732155962a6b9de9d75eab340dd9f4eb9b63a9dcd1e9b"


def _reader(**_: object) -> dict[str, object]:
    return dict(FACTS_WITH_SCHEMA)


def test_inventory_fixture_is_the_same_full_fact_mapping_used_by_guard(tmp_path: Path) -> None:
    snapshot = build_applicability_facts_snapshot(
        target_kind="existing_server",
        capabilities=list(PORTAL_LOCAL_FACTS_CAPABILITIES),
        facts_reader=_reader,
    )
    assert snapshot == {
        "schema": APPLICABILITY_FACTS_SCHEMA,
        "contract_version": APPLICABILITY_FACTS_CONTRACT_VERSION,
        "facts": FACTS_WITH_SCHEMA,
        "facts_digest": EXPECTED_DIGEST,
    }

    executor = SelfHostedScenarioExecutor(
        CommercialState(str(tmp_path / "commercial.sqlite")),
        facts_reader=_reader,
    )
    assert executor._local_facts(FACTS, {"local_facts_capabilities": list(PORTAL_LOCAL_FACTS_CAPABILITIES)}) == FACTS


def test_guard_rejects_added_or_removed_capabilities_and_producer_fails_without_schema(tmp_path: Path) -> None:
    executor = SelfHostedScenarioExecutor(CommercialState(str(tmp_path / "commercial.sqlite")), facts_reader=_reader)
    with pytest.raises(ValueError, match="applicability_facts_mismatch"):
        executor._local_facts({**FACTS, "capabilities": FACTS["capabilities"][:-1]}, {"local_facts_capabilities": list(PORTAL_LOCAL_FACTS_CAPABILITIES)})
    with pytest.raises(ValueError, match="applicability_facts_mismatch"):
        executor._local_facts({**FACTS, "capabilities": [*FACTS["capabilities"], "unexpected"]}, {"local_facts_capabilities": list(PORTAL_LOCAL_FACTS_CAPABILITIES)})

    with pytest.raises(ValueError, match="detector_schema_missing"):
        build_applicability_facts_snapshot(
            target_kind="existing_server",
            capabilities=list(PORTAL_LOCAL_FACTS_CAPABILITIES),
            facts_reader=lambda **_: dict(FACTS),
        )


def test_tampering_changes_canonical_digest_and_signed_inventory_binding(tmp_path: Path) -> None:
    snapshot = build_applicability_facts_snapshot(
        target_kind="existing_server",
        capabilities=list(PORTAL_LOCAL_FACTS_CAPABILITIES),
        facts_reader=_reader,
    )
    tampered = json.loads(json.dumps(snapshot))
    tampered["facts"]["capabilities"].append("unexpected")
    assert applicability_facts_digest(tampered["facts"]) != snapshot["facts_digest"]

    key = Ed25519PrivateKey.generate()
    state = CommercialState(str(tmp_path / "signed.sqlite"))
    state.put("agent_private_key", base64.urlsafe_b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).rstrip(b"=").decode())
    state.put("agent_key_id", "agent-test")
    client = PortalClient(state)
    inventory = {
        "schema": "portal-mcd-inventory-snapshot-v1",
        "contract_version": "1.0",
        "binding": {
            "agent_id": "agent-1",
            "installation_id": "installation-1",
            "host_fingerprint": "sha256:" + "a" * 64,
            "commercial_key_id": "commercial-key-1",
        },
        "snapshot_revision": "1",
        "complete": True,
        "applicability_facts": snapshot,
    }
    signed = client._agent_signed(inventory)
    signature = signed["agent_signature"]
    public = Ed25519PublicKey.from_public_bytes(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    unsigned = {key: value for key, value in signed.items() if key != "agent_signature"}
    decoded = base64.urlsafe_b64decode(str(signature["value"]) + "=" * (-len(str(signature["value"])) % 4))
    public.verify(decoded, canonical_json(unsigned))
    unsigned["binding"] = {**unsigned["binding"], "installation_id": "other-installation"}
    with pytest.raises(Exception):
        public.verify(decoded, canonical_json(unsigned))


def test_inventory_revision_is_stable_then_bumps_once_for_facts_change_and_legacy_consumer_works(tmp_path: Path) -> None:
    class Portal:
        def __init__(self) -> None:
            self.inventories: list[dict[str, object]] = []

        def send_inventory(self, **kwargs: object) -> dict[str, str]:
            self.inventories.append(dict(kwargs["inventory"]))
            return {"status": "accepted"}

        def pull_job(self, **_: object) -> None:
            return None

    class Executor:
        def __init__(self, state: CommercialState) -> None:
            self.state = state

        def run(self, *_args: object, **_kwargs: object) -> dict[str, str]:
            return {"status": "succeeded"}

    portal = Portal()
    state = CommercialState(str(tmp_path / "inventory.sqlite"))
    executor = Executor(state)
    current = {"facts": build_applicability_facts_snapshot(target_kind="existing_server", capabilities=list(PORTAL_LOCAL_FACTS_CAPABILITIES), facts_reader=_reader)}

    def inventory(_installation_id: str) -> dict[str, object]:
        return {
            "schema": "portal-mcd-inventory-snapshot-v1",
            "contract_version": "1.0",
            "request_id": "volatile",
            "observed_at": "2026-10-27T00:00:00Z",
            "binding": {"agent_id": "agent-1", "installation_id": "installation-1", "host_fingerprint": "sha256:" + "a" * 64, "commercial_key_id": "commercial-key-1"},
            "snapshot_revision": "0",
            "complete": True,
            "host": {"mcd_version": "1.5.260", "profile": "passive", "service_status": "active", "scheduler_paused": False, "settings": build_standalone_settings_snapshot({}, revision=0, sync={})},
            "applicability_facts": current["facts"],
            "instances": [],
        }

    # The legacy consumer only stores/accepts the additive inventory and does
    # not need to understand the nested coordinate.
    worker = SelfHostedPortalWorker(
        portal,
        executor,
        installation_id="installation-1",
        portal_keys={},
        publisher_keys={},
        context={},
        commercial_key_id="commercial-key-1",
        agent_id="agent-1",
        host_fingerprint="sha256:" + "a" * 64,
        inventory_provider=inventory,
    )
    assert worker.run_once()["status"] == "idle"
    assert portal.inventories[-1]["snapshot_revision"] == "1"
    assert worker.run_once()["status"] == "idle"
    assert len(portal.inventories) == 1

    changed = dict(FACTS_WITH_SCHEMA)
    changed["capabilities"] = [*FACTS["capabilities"], "new-host-capability"]
    current["facts"] = {
        "schema": APPLICABILITY_FACTS_SCHEMA,
        "contract_version": APPLICABILITY_FACTS_CONTRACT_VERSION,
        "facts": changed,
        "facts_digest": applicability_facts_digest(changed),
    }
    assert worker.run_once()["status"] == "idle"
    assert portal.inventories[-1]["snapshot_revision"] == "2"

    restarted = SelfHostedPortalWorker(
        portal,
        Executor(state),
        installation_id="installation-1",
        portal_keys={},
        publisher_keys={},
        context={},
        commercial_key_id="commercial-key-1",
        agent_id="agent-1",
        host_fingerprint="sha256:" + "a" * 64,
        inventory_provider=inventory,
    )
    assert restarted.run_once()["status"] == "idle"
    assert len(portal.inventories) == 2
    assert state.get("commercial_inventory_state_v1")["installations"]["installation-1"]["snapshot_revision"] == 2
