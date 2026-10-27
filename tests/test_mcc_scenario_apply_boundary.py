from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys

import pytest

from mcd_agent.commercial_portal import (
    MCC_ARTIFACT_SIGNATURE_DOMAIN,
    canonical_json,
    verify_self_hosted_external_artifact,
)
from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
from mcd_agent.scenario_launcher import encode_request_frame


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURE_ROOT / "mcc-scenario-apply-boundary-v1.json"
ARTIFACT = FIXTURE_ROOT / "mcc-scenario-apply-boundary-v1.sh"


def _fixture() -> tuple[dict[str, object], bytes]:
    return json.loads(FIXTURE.read_text(encoding="utf-8")), ARTIFACT.read_bytes()


def _normalized_job(fixture: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    binding = fixture["job_binding"]
    assert isinstance(binding, dict)
    host = binding["host"]
    assert isinstance(host, dict)
    context = fixture["runner_context"]
    assert isinstance(context, dict)
    artifact = fixture["artifact"]
    assert isinstance(artifact, dict)
    pin = {
        "artifact_uri": "https://mcc-fixture.invalid/mcc-scenario-apply-boundary-v1.sh",
        "artifact_digest": artifact["sha256"],
        "artifact_size_bytes": artifact["size_bytes"],
        "artifact_media_type": artifact["media_type"],
        "artifact_revision": artifact["revision"],
        "signature": artifact["signature"],
    }
    runner_context = dict(context)
    job = {
        "job_id": binding["job_id"],
        "request_id": binding["request_id"],
        "command_id": binding["command_id"],
        "idempotency_key": binding["idempotency_key"],
        "scenario": runner_context["scenario"],
        "applicability": runner_context["applicability"],
        "parameters": runner_context["parameters"],
        "secret_references": runner_context["secret_references"],
        "scenario_artifact": pin,
        "host": host,
    }
    return job, pin


@pytest.mark.skipif(not Path("/proc/self/fd/3").exists(), reason="MCC fixture uses Linux /proc/self/fd/3")
def test_mcc_fixture_verifies_and_runs_through_existing_generic_runner(tmp_path: Path) -> None:
    fixture, artifact = _fixture()
    metadata = fixture["artifact"]
    assert isinstance(metadata, dict)
    job, pin = _normalized_job(fixture)
    public_key = str(metadata["public_key_base64url"])
    verified = verify_self_hosted_external_artifact(
        pin,
        artifact,
        {
            str(metadata["signature"]["key_id"]): {
                "algorithm": "ed25519",
                "status": "active",
                "value": public_key,
            }
        },
        kind="scenario",
        signature_domain=MCC_ARTIFACT_SIGNATURE_DOMAIN,
    )
    assert verified["artifact_revision"] == metadata["revision"]

    runner = tmp_path / "mcd-scenario-runner"
    runner.write_text(
        "#!/bin/sh\n"
        f"exec {sys.executable} -c \"import sys; sys.path.insert(0, "
        f"'{Path(__file__).parents[1]}'); from mcd_agent.scenario_launcher import main; "
        "raise SystemExit(main())\" \"$@\"\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    result = ExternalProcessPolicy([str(runner)], timeout_sec=30).run(
        artifact=artifact,
        job=job,
        facts={},
        context=fixture["runner_context"],
    )
    assert result["status"] == "succeeded"
    assert result["runner"] == {
        "contract_version": "1.0",
        "exit_code": 0,
        "frame_type": "result",
        "schema": "mcd-self-hosted-executable-output-v1",
        "status": "succeeded",
        "completed": 20,
        "total": 20,
        "rollback": "not_attempted",
    }


def test_mcc_fixture_frame_and_binding_are_exact() -> None:
    fixture, artifact = _fixture()
    context = fixture["runner_context"]
    frame_meta = fixture["frame"]
    assert isinstance(context, dict) and isinstance(frame_meta, dict)
    frame = encode_request_frame(artifact, context)
    magic, version, header_bytes, artifact_size, context_size, artifact_digest, context_digest, reserved = struct.unpack(
        "!8sHHII32s32sI", frame[:88]
    )
    assert magic == b"SSMCDX01"
    assert (version, header_bytes, reserved) == (1, 88, 0)
    assert artifact_size == 1919 and context_size == 763
    assert artifact_digest.hex() == str(fixture["artifact"]["sha256"])[7:]
    assert context_digest.hex() == str(frame_meta["context_sha256"])[7:]
    assert hashlib.sha256(frame).hexdigest() == str(frame_meta["frame_sha256"])[7:]
    assert hashlib.sha256(canonical_json(context)).hexdigest() == str(frame_meta["context_sha256"])[7:]
