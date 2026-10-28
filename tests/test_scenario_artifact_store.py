from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mcd_agent.scenario_artifact_store import (
    SCHEMA,
    ScenarioArtifactStore,
    ScenarioArtifactStoreError,
)


def _pin(artifact: bytes, revision: str) -> dict[str, object]:
    return {
        "artifact_uri": "https://artifacts.example.test/" + revision,
        "artifact_digest": "sha256:" + hashlib.sha256(artifact).hexdigest(),
        "artifact_size_bytes": len(artifact),
        "artifact_media_type": "application/x-sh",
        "artifact_revision": revision,
        "signature": {"algorithm": "ed25519", "key_id": "k1", "value": "signed"},
    }


def _verify(pin: dict[str, object], artifact: bytes) -> dict[str, object]:
    assert pin["artifact_digest"] == "sha256:" + hashlib.sha256(artifact).hexdigest()
    return dict(pin)


def test_delivery_is_atomic_and_old_object_waits_for_receipt(tmp_path: Path) -> None:
    store = ScenarioArtifactStore(tmp_path / "store")
    first = b"#!/bin/sh\nprintf first\n"
    second = b"#!/bin/sh\nprintf second\n"
    binding = {"job_id": "job-1", "request_id": "request-1", "idempotency_key": "key-1"}

    activated = store.deliver(pin=_pin(first, "r1"), artifact=first, verify=_verify, binding=binding)
    assert activated["receipt"]["schema"] == SCHEMA
    assert Path(activated["artifact_path"]).read_bytes() == first
    assert store.active()["revision"] == "r1"

    replaced = store.deliver(pin=_pin(second, "r2"), artifact=second, verify=_verify, binding=binding)
    old_path = tmp_path / "store" / "objects" / hashlib.sha256(first).hexdigest()
    assert Path(replaced["artifact_path"]).read_bytes() == second
    assert old_path.is_file()
    assert replaced["cleanup_pending"] is True

    store.acknowledge(replaced["receipt"])
    assert not old_path.exists()
    assert store.active()["digest"] == _pin(second, "r2")["artifact_digest"]


def test_same_revision_is_idempotent_and_restart_can_finish_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = ScenarioArtifactStore(root)
    first = b"first"
    second = b"second"
    binding = {"job_id": "job-1", "request_id": "request-1", "idempotency_key": "key-1"}
    store.deliver(pin=_pin(first, "r1"), artifact=first, verify=_verify, binding=binding)
    replacement = store.deliver(pin=_pin(second, "r2"), artifact=second, verify=_verify, binding=binding)

    restarted = ScenarioArtifactStore(root)
    replay = restarted.deliver(pin=_pin(second, "r2"), artifact=second, verify=_verify, binding=binding)
    assert replay["receipt"]["idempotent"] is True
    restarted.acknowledge_pending()
    assert not (root / "objects" / hashlib.sha256(first).hexdigest()).exists()


def test_failed_verification_or_partial_bytes_keeps_previous_pointer(tmp_path: Path) -> None:
    store = ScenarioArtifactStore(tmp_path / "store")
    first = b"first"
    second = b"second"
    binding = {"job_id": "job-1", "request_id": "request-1", "idempotency_key": "key-1"}
    store.deliver(pin=_pin(first, "r1"), artifact=first, verify=_verify, binding=binding)
    previous = store.active()

    with pytest.raises(ScenarioArtifactStoreError, match="integrity_invalid"):
        store.deliver(pin=_pin(second, "r2"), artifact=b"partial", verify=_verify, binding=binding)
    assert store.active() == previous

    def reject(_pin_value: dict[str, object], _artifact: bytes) -> dict[str, object]:
        raise ValueError("signature mismatch")

    with pytest.raises(ScenarioArtifactStoreError, match="signature_or_trust_invalid"):
        store.deliver(pin=_pin(second, "r2"), artifact=second, verify=reject, binding=binding)
    assert store.active() == previous
    assert not list((tmp_path / "store" / "staging").glob("*.part"))


def test_disk_failure_does_not_switch_active_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ScenarioArtifactStore(tmp_path / "store")
    first = b"first"
    second = b"second"
    binding = {"job_id": "job-1", "request_id": "request-1", "idempotency_key": "key-1"}
    store.deliver(pin=_pin(first, "r1"), artifact=first, verify=_verify, binding=binding)
    previous = store.active()
    original = store._fsync
    monkeypatch.setattr(store, "_fsync", lambda _handle: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(ScenarioArtifactStoreError, match="activation_failed"):
        store.deliver(pin=_pin(second, "r2"), artifact=second, verify=_verify, binding=binding)
    monkeypatch.setattr(store, "_fsync", original)
    assert store.active() == previous


def test_concurrent_delivery_leaves_one_valid_active_object(tmp_path: Path) -> None:
    store = ScenarioArtifactStore(tmp_path / "store")
    artifacts = [(b"one", "r1"), (b"two", "r2")]

    def deliver(item: tuple[bytes, str]) -> dict[str, object]:
        artifact, revision = item
        return store.deliver(
            pin=_pin(artifact, revision),
            artifact=artifact,
            verify=_verify,
            binding={"job_id": "job-1", "request_id": "request-1", "idempotency_key": revision},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(deliver, artifacts))
    active = store.active()
    assert active is not None
    assert active["revision"] in {"r1", "r2"}
    assert Path(store.objects / str(active["digest"])[7:]).read_bytes() in {b"one", b"two"}
    assert all(result["receipt"]["status"] == "verified" for result in results)
