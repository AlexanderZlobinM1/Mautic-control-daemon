from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcd_agent.commercial_portal import (
    MCC_ARTIFACT_SIGNATURE_DOMAIN,
    MCC_AUTHENTICATED_ARTIFACT_TRANSPORT,
    MCC_CATALOG_SHA256_TRUST_MODE,
    SELF_HOSTED_MCD_JOB_SCHEMA,
    SELF_HOSTED_MCD_LEGACY_CONTRACT_VERSION,
    SELF_HOSTED_MCD_CONTRACT_VERSION,
    canonical_json,
    verify_self_hosted_mcd_job,
    verify_self_hosted_external_artifact,
)
from mcd_agent.commercial_self_hosted import (
    RunnerExecutionError,
    SelfHostedPortalWorker,
    SelfHostedScenarioExecutor,
    validate_self_hosted_stage_receipt,
    validate_self_hosted_terminal_receipt,
)
from mcd_agent.commercial_portal import CommercialState


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _artifact(pin_kind: str, revision: str, body: bytes, key: Ed25519PrivateKey) -> dict[str, object]:
    digest = _digest(body)
    statement = {
        "kind": pin_kind,
        "revision": revision,
        "sha256": digest[7:],
        "media_type": "application/json",
        "size_bytes": len(body),
    }
    return {
        "artifact_uri": f"https://artifacts.example.test/{revision}.json",
        "artifact_digest": digest,
        "artifact_size_bytes": len(body),
        "artifact_media_type": "application/json",
        "artifact_revision": revision,
        "signature": {
            "algorithm": "ed25519",
            "key_id": "publisher-1",
            "value": _b64(key.sign(MCC_ARTIFACT_SIGNATURE_DOMAIN + canonical_json(statement))),
        },
    }


def _mcc_catalog_artifact(revision: str, body: bytes, *, status: str = "active") -> dict[str, object]:
    digest = _digest(body)
    return {
        "artifact_trust_mode": MCC_CATALOG_SHA256_TRUST_MODE,
        "artifact_transport": MCC_AUTHENTICATED_ARTIFACT_TRANSPORT,
        "artifact_path": f"/api/v1/commercial/artifacts/{revision}",
        "artifact_grant": "mcc-grant-1",
        "artifact_digest": digest,
        "artifact_size_bytes": len(body),
        "artifact_media_type": "application/x-sh",
        "artifact_revision": revision,
        "trusted_catalog": {
            "source": "mcc",
            "transport": MCC_AUTHENTICATED_ARTIFACT_TRANSPORT,
            "status": status,
            "revision": revision,
            "artifact_digest": digest,
        },
    }


def _job(portal: Ed25519PrivateKey, publisher: Ed25519PrivateKey, *, contract_version: str = SELF_HOSTED_MCD_LEGACY_CONTRACT_VERSION) -> tuple[dict[str, object], bytes, bytes, dict[str, str]]:
    facts = {
        "os_id": "ubuntu",
        "os_version": "24.04",
        "architecture": "amd64",
        "package_manager": "apt-get",
        "capabilities": ["package_manager.query"],
        "target_kind": "existing_server",
    }
    facts_digest = _digest(canonical_json({**facts, "capabilities": sorted(facts["capabilities"])}))
    bootstrap = b"bootstrap-artifact"
    scenario = b"scenario-artifact"
    scenario_pin = _artifact("scenario", "scenario-r1", scenario, publisher)
    job: dict[str, object] = {
        "schema": SELF_HOSTED_MCD_JOB_SCHEMA,
        "contract_version": contract_version,
        "job_id": "job-1",
        "request_id": "request-1",
        "idempotency_key": "request-1:scenario-r1",
        "account_id": "account-1",
        "commercial_key_id": "commercial-key-1",
        "agent_id": "agent-1",
        "installation_id": "installation-1",
        "host_fingerprint": "sha256:" + "b" * 64,
        "scenario": {
            "scenario_id": "portal-ubuntu-24-04",
            "revision": "scenario-r1",
            "mode": "scan",
            "artifact_digest": scenario_pin["artifact_digest"],
        },
        "applicability": {"facts": facts, "facts_digest": facts_digest},
        "scenario_artifact": scenario_pin,
        "execution": {"primitive": "scenario.apply", "version": "1.0"},
        "issued_at": "2026-10-26T10:00:00Z",
        "expires_at": "2099-10-26T11:00:00Z",
        "nonce": "nonce-1",
        "portal_signature": {"algorithm": "ed25519", "key_id": "portal-1", "value": ""},
    }
    if contract_version == SELF_HOSTED_MCD_LEGACY_CONTRACT_VERSION:
        job["bootstrap_artifact"] = _artifact("bootstrap", "bootstrap-r1", bootstrap, publisher)
    unsigned = dict(job)
    unsigned.pop("portal_signature")
    job["portal_signature"] = {"algorithm": "ed25519", "key_id": "portal-1", "value": _b64(portal.sign(canonical_json(unsigned)))}
    return job, bootstrap, scenario, {
        "portal-1": _b64(portal.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
        "publisher-1": _b64(publisher.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
    }


def test_v2_job_rejects_scenario_artifact_revision_mismatch() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, _bootstrap, _scenario, keys = _job(portal, publisher)
    job["scenario_artifact"]["artifact_revision"] = "scenario-r2"
    unsigned = dict(job)
    unsigned.pop("portal_signature")
    job["portal_signature"] = {"algorithm": "ed25519", "key_id": "portal-1", "value": _b64(portal.sign(canonical_json(unsigned)))}
    try:
        verify_self_hosted_mcd_job(
            job,
            {"portal-1": keys["portal-1"]},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
        )
    except ValueError as exc:
        assert str(exc) == "self_hosted_mcd_scenario_revision_mismatch"
    else:
        raise AssertionError("scenario and artifact revisions must be immutable together")


def test_unsigned_v2_job_is_accepted_without_portal_signing_keys() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, _bootstrap, _scenario, _keys = _job(portal, publisher)
    job.pop("portal_signature", None)
    verified = verify_self_hosted_mcd_job(
        job,
        {},
        expected_agent_id="agent-1",
        expected_installation_id="installation-1",
        expected_commercial_key_id="commercial-key-1",
        expected_host_fingerprint="sha256:" + "b" * 64,
    )
    assert verified["job_id"] == "job-1"


def _resign(job: dict[str, object], portal: Ed25519PrivateKey) -> None:
    unsigned = dict(job)
    unsigned.pop("portal_signature", None)
    job["portal_signature"] = {"algorithm": "ed25519", "key_id": "portal-1", "value": _b64(portal.sign(canonical_json(unsigned)))}


def test_v21_registered_scenario_job_has_no_bootstrap_artifact() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, _bootstrap, _scenario, keys = _job(portal, publisher, contract_version=SELF_HOSTED_MCD_CONTRACT_VERSION)

    verified = verify_self_hosted_mcd_job(
        job,
        {"portal-1": keys["portal-1"]},
        expected_agent_id="agent-1",
        expected_installation_id="installation-1",
        expected_commercial_key_id="commercial-key-1",
        expected_host_fingerprint="sha256:" + "b" * 64,
    )

    assert verified["contract_version"] == SELF_HOSTED_MCD_CONTRACT_VERSION
    assert verified["bootstrap_artifact"] is None


def test_v21_portal_fixture_matches_signed_cross_component_vector() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "portal-self-hosted-mcd-v2.1-job.json"
    job = json.loads(fixture_path.read_text(encoding="utf-8"))
    public_key = (fixture_path.with_suffix(".public-key")).read_text(encoding="utf-8").strip()
    expected_hash = (fixture_path.with_suffix(".sha256")).read_text(encoding="utf-8").strip()

    verified = verify_self_hosted_mcd_job(
        job,
        {job["portal_signature"]["key_id"]: public_key},
        expected_agent_id=job["agent_id"],
        expected_installation_id=job["installation_id"],
        expected_commercial_key_id=job["commercial_key_id"],
        expected_host_fingerprint=job["host_fingerprint"],
        now=datetime(2099, 10, 26, 0, 30, tzinfo=timezone.utc).timestamp(),
    )

    assert verified["contract_version"] == SELF_HOSTED_MCD_CONTRACT_VERSION
    assert "bootstrap_artifact" not in job
    assert hashlib.sha256(canonical_json(job)).hexdigest() == expected_hash


def test_mcc_catalog_sha_only_pin_requires_active_catalog_and_exact_bytes() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, _bootstrap, scenario, keys = _job(portal, publisher, contract_version=SELF_HOSTED_MCD_CONTRACT_VERSION)
    pin = _mcc_catalog_artifact("scenario-r1", scenario)
    job["scenario_artifact"] = pin
    job["scenario"]["artifact_digest"] = pin["artifact_digest"]
    _resign(job, portal)

    verified = verify_self_hosted_mcd_job(
        job,
        {"portal-1": keys["portal-1"]},
        expected_agent_id="agent-1",
        expected_installation_id="installation-1",
        expected_commercial_key_id="commercial-key-1",
        expected_host_fingerprint="sha256:" + "b" * 64,
    )
    assert verified["scenario_artifact"]["artifact_trust_mode"] == MCC_CATALOG_SHA256_TRUST_MODE
    assert verify_self_hosted_external_artifact(pin, scenario, {}, kind="scenario")["artifact_digest"] == pin["artifact_digest"]

    stale = dict(pin)
    stale["trusted_catalog"] = {**pin["trusted_catalog"], "status": "stale"}
    job["scenario_artifact"] = stale
    _resign(job, portal)
    try:
        verify_self_hosted_mcd_job(
            job,
            {"portal-1": keys["portal-1"]},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
        )
    except ValueError as exc:
        assert str(exc) == "self_hosted_mcd_scenario_artifact_invalid_catalog"
    else:
        raise AssertionError("stale MCC catalog must fail closed")


def test_v21_worker_uses_authenticated_mcc_api_for_sha_only_catalog() -> None:
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = Ed25519PrivateKey.generate()
    job, _bootstrap, scenario, keys = _job(portal_key, publisher_key, contract_version=SELF_HOSTED_MCD_CONTRACT_VERSION)
    pin = _mcc_catalog_artifact("scenario-r1", scenario)
    job["scenario_artifact"] = pin
    job["scenario"]["artifact_digest"] = pin["artifact_digest"]
    _resign(job, portal_key)

    class Portal:
        def __init__(self) -> None:
            self.receipts: list[dict[str, object]] = []
            self.downloads: list[tuple[str, str, int]] = []

        def pull_job(self, **_: object):
            return {"cursor": "cursor-mcc-sha", "commands": [job]}

        def download_artifact(self, *, path: str, grant: str, expected_size: int) -> bytes:
            self.downloads.append((path, grant, expected_size))
            return scenario

        def send_receipt(self, receipt: dict[str, object], **_: object):
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        events: list[bytes] = []
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **kwargs: events.append(kwargs["artifact"]) or {"status": "succeeded"},
        )
        client = Portal()
        worker = SelfHostedPortalWorker(
            client,
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )

        result = worker.run_once()

    assert result == {"status": "succeeded", "job_id": "job-1"}
    assert client.downloads == [(pin["artifact_path"], pin["artifact_grant"], len(scenario))]
    assert events == [scenario]


def test_v21_registered_scenario_job_rejects_mixed_bootstrap_artifact() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, bootstrap, _scenario, keys = _job(portal, publisher, contract_version=SELF_HOSTED_MCD_CONTRACT_VERSION)
    job["bootstrap_artifact"] = _artifact("bootstrap", "bootstrap-r1", bootstrap, publisher)
    _resign(job, portal)

    try:
        verify_self_hosted_mcd_job(
            job,
            {"portal-1": keys["portal-1"]},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
        )
    except ValueError as exc:
        assert str(exc) == "self_hosted_mcd_bootstrap_artifact_forbidden"
    else:
        raise AssertionError("2.1 scenario jobs must fail closed when bootstrap is mixed in")


def test_v20_legacy_job_still_requires_bootstrap_artifact() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, _bootstrap, _scenario, keys = _job(portal, publisher)
    job.pop("bootstrap_artifact")
    _resign(job, portal)

    try:
        verify_self_hosted_mcd_job(
            job,
            {"portal-1": keys["portal-1"]},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
        )
    except ValueError as exc:
        assert str(exc) == "self_hosted_mcd_bootstrap_artifact_invalid"
    else:
        raise AssertionError("legacy 2.0 jobs must continue to require bootstrap")


def test_v21_worker_downloads_and_executes_only_scenario_artifact(monkeypatch) -> None:
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = Ed25519PrivateKey.generate()
    job, _bootstrap, scenario, keys = _job(portal_key, publisher_key, contract_version=SELF_HOSTED_MCD_CONTRACT_VERSION)
    downloaded: list[str] = []

    class Portal:
        def __init__(self) -> None:
            self.receipts: list[dict[str, object]] = []

        def pull_job(self, **_: object):
            return {"cursor": "cursor-21", "job": job}

        def send_receipt(self, receipt: dict[str, object], **_: object):
            self.receipts.append(receipt)
            return {}

    def download(uri: str, expected_size: int) -> bytes:
        downloaded.append(uri)
        assert "bootstrap" not in uri
        assert expected_size == len(scenario)
        return scenario

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        events: list[bytes] = []
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **kwargs: events.append(kwargs["artifact"]) or {"status": "succeeded"},
        )
        monkeypatch.setattr("mcd_agent.commercial_self_hosted.download_external_artifact", download)
        worker = SelfHostedPortalWorker(
            Portal(),
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )

        result = worker.run_once()

        assert result == {"status": "succeeded", "job_id": "job-1"}
        assert downloaded == [job["scenario_artifact"]["artifact_uri"]]
        assert events == [scenario]
        assert worker.client.receipts[-1]["contract_version"] == SELF_HOSTED_MCD_CONTRACT_VERSION


def test_activated_worker_polls_and_emits_terminal_receipt_v2(monkeypatch) -> None:
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = Ed25519PrivateKey.generate()
    job, _bootstrap, scenario, keys = _job(portal_key, publisher_key, contract_version=SELF_HOSTED_MCD_CONTRACT_VERSION)

    class Portal:
        def __init__(self) -> None:
            self.adoptions: list[dict[str, object]] = []
            self.receipts: list[dict[str, object]] = []

        def adopt(self, **kwargs: object):
            self.adoptions.append(kwargs)
            return {"status": "adopted"}

        def pull_job(self, **_: object):
            return {"cursor": "cursor-active", "job": job}

        def send_receipt(self, receipt: dict[str, object], **_: object):
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        state.put("commercial_portal_connection_state", "active")
        events: list[bytes] = []
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **kwargs: events.append(kwargs["artifact"]) or {"status": "succeeded"},
        )
        monkeypatch.setattr("mcd_agent.commercial_self_hosted.download_external_artifact", lambda *_args, **_kwargs: scenario)
        portal = Portal()
        worker = SelfHostedPortalWorker(
            portal,
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )

        result = worker.run_once()

    assert result == {"status": "succeeded", "job_id": "job-1"}
    assert portal.adoptions and portal.adoptions[0]["installation_id"] == "installation-1"
    assert events == [scenario]
    terminal = portal.receipts[-1]
    assert terminal["schema"] == "mcd-self-hosted-terminal-receipt-v2"
    validate_self_hosted_terminal_receipt(
        terminal,
        binding={
            "job_id": job["job_id"],
            "request_id": job["request_id"],
            "account_id": job["account_id"],
            "commercial_key_id": job["commercial_key_id"],
            "agent_id": job["agent_id"],
            "installation_id": job["installation_id"],
            "host_fingerprint": job["host_fingerprint"],
            "facts_digest": job["applicability"]["facts_digest"],
            "scenario": job["scenario"],
        },
    )


def test_v2_external_artifact_execution_has_exact_binding_and_no_handlers() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, bootstrap, scenario, keys = _job(portal, publisher)
    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        events: list[bytes] = []
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **kwargs: events.append(kwargs["artifact"]) or {"status": "succeeded", "steps": 2},
        )
        receipts: list[dict[str, object]] = []
        terminal = executor.run(
            job,
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
            bootstrap_artifact=bootstrap,
            scenario_artifact=scenario,
            receipt_sink=receipts.append,
        )
        assert terminal["status"] == "succeeded"
        assert events == [scenario]
        assert [receipt["stage_id"] for receipt in receipts[:2]] == ["scenario_dispatch", "execution"]
        assert receipts[-1]["schema"] == "mcd-self-hosted-terminal-receipt-v2"
        binding = {key: terminal[key] for key in ("job_id", "request_id", "account_id", "commercial_key_id", "agent_id", "installation_id", "host_fingerprint", "facts_digest", "scenario")}
        validate_self_hosted_stage_receipt(receipts[0], binding=binding)
        validate_self_hosted_terminal_receipt(terminal, binding=binding)
        replay = executor.run(
            job,
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
            bootstrap_artifact=bootstrap,
            scenario_artifact=scenario,
        )
        assert replay == terminal
        assert events == [scenario]


def test_v2_runner_failure_persists_bounded_evidence_and_replays_without_rerun() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, bootstrap, scenario, keys = _job(portal, publisher)
    calls = 0

    def fail_runner(**_kwargs):
        nonlocal calls
        calls += 1
        raise RunnerExecutionError(
            "self_hosted_mcd_runner_failed",
            evidence={
                "schema": "mcd-self-hosted-runner-evidence-v1",
                "admission_stage": "completed",
                "exit_code": 23,
                "signal": None,
                "timed_out": False,
                "stdout_bytes": 0,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stdout_truncated": False,
                "stderr_bytes": 5,
                "stderr_sha256": hashlib.sha256(b"error").hexdigest(),
                "stderr_truncated": False,
                "stderr_preview": "error",
            },
        )

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=fail_runner,
        )
        terminal = executor.run(
            job,
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
            bootstrap_artifact=bootstrap,
            scenario_artifact=scenario,
        )
        assert terminal["status"] == "failed"
        assert terminal["runner_evidence"]["exit_code"] == 23
        assert terminal["error"]["runner_evidence"]["stderr_preview"] == "error"
        saved = executor._load("job-1")
        assert saved["runner_evidence"]["stderr_bytes"] == 5
        replay = executor.run(
            job,
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            expected_agent_id="agent-1",
            expected_installation_id="installation-1",
            expected_commercial_key_id="commercial-key-1",
            expected_host_fingerprint="sha256:" + "b" * 64,
            bootstrap_artifact=bootstrap,
            scenario_artifact=scenario,
        )
        assert replay == terminal
        assert calls == 1


def test_v2_external_artifact_uses_sha256_only_without_publisher_trust() -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, _bootstrap, scenario, keys = _job(portal, publisher)
    pin = job["scenario_artifact"]

    pin.pop("signature", None)
    assert verify_self_hosted_external_artifact(pin, scenario, {}, kind="scenario")["artifact_revision"] == "scenario-r1"


def test_v2_worker_accepts_unsigned_digest_only_bootstrap_and_scenario(monkeypatch) -> None:
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = Ed25519PrivateKey.generate()
    job, bootstrap, scenario, keys = _job(portal_key, publisher_key)
    job["bootstrap_artifact"].pop("signature", None)
    job["scenario_artifact"].pop("signature", None)

    class Portal:
        def __init__(self) -> None:
            self.receipts: list[dict[str, object]] = []
            self.polls = 0

        def pull_job(self, **_: object):
            self.polls += 1
            return {"cursor": "cursor-1", "job": job}

        def send_receipt(self, receipt: dict[str, object], **_: object):
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **_: {"status": "succeeded"},
        )
        monkeypatch.setattr(
            "mcd_agent.commercial_self_hosted.download_external_artifact",
            lambda uri, expected_size: bootstrap if "bootstrap" in uri else scenario,
        )
        portal = Portal()
        worker = SelfHostedPortalWorker(
            portal,
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )

        result = worker.run_once()

        assert result == {"status": "succeeded", "job_id": "job-1"}
        assert portal.polls == 1
        assert portal.receipts[-1]["status"] == "succeeded"
        assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_v2_worker_command6_shape_expiry_emits_terminal_and_advances(monkeypatch) -> None:
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = Ed25519PrivateKey.generate()
    job, _bootstrap, _scenario, keys = _job(portal_key, publisher_key)
    job["job_id"] = "job-command6"
    job["command_id"] = "mcd_command_job-command6_retry_1"
    job["delivery_cursor"] = 6
    job["expires_at"] = "2020-01-01T00:00:00Z"
    unsigned = dict(job)
    unsigned.pop("portal_signature")
    job["portal_signature"] = {"algorithm": "ed25519", "key_id": "portal-1", "value": _b64(portal_key.sign(canonical_json(unsigned)))}

    class Portal:
        def __init__(self) -> None:
            self.receipts: list[dict[str, object]] = []

        def pull_job(self, **_: object):
            return {"cursor": "6", "commands": [job]}

        def send_receipt(self, receipt: dict[str, object], **_: object):
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        executor = SelfHostedScenarioExecutor(state, facts_reader=lambda **_: job["applicability"]["facts"], runner=lambda **_: (_ for _ in ()).throw(AssertionError("expired job must not execute")))
        portal = Portal()
        worker = SelfHostedPortalWorker(
            portal,
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )

        result = worker.run_once()

        assert result == {"status": "failed", "job_id": "job-command6"}
        assert len(portal.receipts) == 1
        assert portal.receipts[0]["schema"] == "mcd-self-hosted-terminal-receipt-v2"
        assert portal.receipts[0]["sequence"] == 2
        assert portal.receipts[0]["idempotency_key"] == "job-command6:terminal:2"
        assert portal.receipts[0]["error"]["type"] == "self_hosted_mcd_job_expired_or_not_yet_valid"
        assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_v2_worker_revoked_state_closes_already_accepted_delivery(monkeypatch) -> None:
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = Ed25519PrivateKey.generate()
    job, _bootstrap, _scenario, keys = _job(portal_key, publisher_key)

    class Portal:
        def __init__(self) -> None:
            self.receipts: list[dict[str, object]] = []

        def pull_job(self, **_: object):
            return {"cursor": "6", "commands": [job]}

        def send_receipt(self, receipt: dict[str, object], **_: object):
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        state.put("commercial_portal_connection_state", "revoked")
        executor = SelfHostedScenarioExecutor(state, facts_reader=lambda **_: job["applicability"]["facts"], runner=lambda **_: (_ for _ in ()).throw(AssertionError("revoked job must not execute")))
        portal = Portal()
        worker = SelfHostedPortalWorker(
            portal,
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )

        result = worker.run_once()

        assert result == {"status": "failed", "job_id": "job-1"}
        assert portal.receipts[0]["error"]["type"] == "self_hosted_mcd_commercial_state_revoked"
        assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_v2_worker_draining_recovery_terminal_closes_delivery_without_replay() -> None:
    portal_key = Ed25519PrivateKey.generate()
    publisher_key = Ed25519PrivateKey.generate()
    job, _bootstrap, _scenario, keys = _job(portal_key, publisher_key)
    job["command_id"] = "mcd_command_job-1_retry_1"
    job["delivery_cursor"] = 6

    class Portal:
        def __init__(self) -> None:
            self.receipts: list[dict[str, object]] = []

        def pull_job(self, **_: object):
            return None

        def send_receipt(self, receipt: dict[str, object], **_: object):
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        state.put(
            "commercial_self_hosted_pending_delivery_v1",
            {"cursor": "5", "cursor_next": "6", "installation_id": "installation-1", "job": {"commands": [job]}},
        )
        state.put(
            "commercial_self_hosted_pending_receipts",
            [{
                "schema": "mcd-self-hosted-terminal-receipt-v2",
                "job_id": "job-1",
                "sequence": 1,
                "idempotency_key": "job-1:terminal:1",
                "status": "failed",
            }],
        )
        calls = {"runner": 0}

        def runner(**_: object):
            calls["runner"] += 1
            return {"status": "succeeded"}

        worker = SelfHostedPortalWorker(
            Portal(),
            SelfHostedScenarioExecutor(state, runner=runner),
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )

        worker.run_once()

        assert calls["runner"] == 0
        assert worker.client.receipts[0]["sequence"] == 2
        assert worker.client.receipts[0]["idempotency_key"] == "job-1:terminal:2"
        assert state.get("commercial_self_hosted_pending_receipts") == []
        assert state.get("commercial_self_hosted_pending_delivery_v1") is None


def test_v2_status_and_binding_reject_legacy_passed_receipt() -> None:
    binding = {
        "job_id": "job-1",
        "request_id": "request-1",
        "account_id": "account-1",
        "commercial_key_id": "commercial-key-1",
        "agent_id": "agent-1",
        "installation_id": "installation-1",
        "host_fingerprint": "sha256:" + "b" * 64,
        "facts_digest": "sha256:" + "c" * 64,
        "scenario": {"scenario_id": "s", "revision": "r", "mode": "scan", "artifact_digest": "sha256:" + "d" * 64},
    }
    receipt = {**binding, "schema": "mcd-self-hosted-stage-receipt-v2", "contract_version": "2.0", "stage_id": "execution", "sequence": 1, "idempotency_key": "job-1:execution:1", "status": "passed", "observed_at": "2026-10-26T10:00:00Z"}
    try:
        validate_self_hosted_stage_receipt(receipt, binding=binding)
    except ValueError as exc:
        assert str(exc) == "self_hosted_mcd_stage_receipt_invalid"
    else:
        raise AssertionError("legacy passed status must be rejected")


def test_v2_receipts_queue_in_order_across_portal_outage_and_restart(monkeypatch) -> None:
    portal = Ed25519PrivateKey.generate()
    publisher = Ed25519PrivateKey.generate()
    job, bootstrap, scenario, keys = _job(portal, publisher)

    class FakePortal:
        def __init__(self) -> None:
            self.fail_once = True
            self.receipts: list[dict[str, object]] = []

        def pull_job(self, **_: object):
            return job

        def send_receipt(self, receipt: dict[str, object]):
            if self.fail_once:
                self.fail_once = False
                raise OSError("portal outage")
            self.receipts.append(receipt)
            return {}

    with TemporaryDirectory() as directory:
        state = CommercialState(str(Path(directory) / "commercial.sqlite"))
        fake = FakePortal()
        executor = SelfHostedScenarioExecutor(
            state,
            facts_reader=lambda **_: job["applicability"]["facts"],
            runner=lambda **_: {"status": "succeeded"},
        )
        monkeypatch.setattr(
            "mcd_agent.commercial_self_hosted.download_external_artifact",
            lambda uri, expected_size: bootstrap if "bootstrap" in uri else scenario,
        )
        worker = SelfHostedPortalWorker(
            fake,
            executor,
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )
        result = worker.run_once()
        assert result["status"] == "succeeded"
        pending = state.get("commercial_self_hosted_pending_receipts")
        assert isinstance(pending, list) and pending[0]["stage_id"] == "scenario_dispatch"

        restarted = SelfHostedPortalWorker(
            fake,
            SelfHostedScenarioExecutor(
                state,
                facts_reader=lambda **_: job["applicability"]["facts"],
                runner=lambda **_: (_ for _ in ()).throw(AssertionError("replay executed runner")),
            ),
            installation_id="installation-1",
            portal_keys={"portal-1": keys["portal-1"]},
            publisher_keys={"publisher-1": keys["publisher-1"]},
            context={},
            commercial_key_id="commercial-key-1",
            agent_id="agent-1",
            host_fingerprint="sha256:" + "b" * 64,
        )
        restarted.run_once()
        assert state.get("commercial_self_hosted_pending_receipts") == []
        assert [receipt["sequence"] for receipt in fake.receipts[:2]] == [1, 2]
