from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import struct
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcd_agent.commercial_portal import (
    MCC_ARTIFACT_SIGNATURE_DOMAIN,
    canonical_json,
    verify_self_hosted_external_artifact,
)
from mcd_agent.commercial_self_hosted import ExternalProcessPolicy
from mcd_agent.scenario_launcher import encode_request_frame


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _artifact() -> bytes:
    """Return a minimal ephemeral vector, never a production scenario."""
    script = r'''#!/bin/sh
exec python3 -c 'import json,struct,sys; f={"contract_version":"1.0","frame_type":"result","schema":"mcd-self-hosted-executable-output-v1","status":"succeeded","exit_code":0,"completed":1,"total":1,"rollback":"not_attempted"}; b=json.dumps(f,sort_keys=True,separators=(",",":")).encode(); sys.stdout.buffer.write(struct.pack("!I",len(b))+b)'
'''
    return script.encode("utf-8")


def _context(artifact: bytes) -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
    return {
        "schema": "portal-self-hosted-executable-context-v1",
        "contract_version": "1.0",
        "job": {
            "job_id": "job-boundary-1",
            "request_id": "request-boundary-1",
            "command_id": "command-boundary-1",
            "idempotency_key": "idempotency-boundary-1",
        },
        "scenario": {
            "scenario_id": "synthetic-boundary-vector",
            "revision": "1",
            "artifact_revision": "synthetic-boundary-v1",
            "artifact_digest": digest,
        },
        "applicability": {"facts_digest": "sha256:" + "0" * 64},
        "parameters": {"mode": "check"},
        "secret_references": [],
    }


def _pin(artifact: bytes, key: Ed25519PrivateKey) -> dict[str, object]:
    statement = {
        "kind": "scenario",
        "revision": "synthetic-boundary-v1",
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "media_type": "application/x-sh",
        "size_bytes": len(artifact),
    }
    return {
        "artifact_uri": "https://mcc.example.invalid/artifact",
        "artifact_digest": "sha256:" + statement["sha256"],
        "artifact_size_bytes": len(artifact),
        "artifact_media_type": "application/x-sh",
        "artifact_revision": statement["revision"],
        "signature": {
            "algorithm": "ed25519",
            "key_id": "synthetic-boundary-key",
            "value": _b64(key.sign(MCC_ARTIFACT_SIGNATURE_DOMAIN + canonical_json(statement))),
        },
    }


def _job(context: dict[str, object], pin: dict[str, object]) -> dict[str, object]:
    return {
        "job_id": "job-boundary-1",
        "request_id": "request-boundary-1",
        "command_id": "command-boundary-1",
        "idempotency_key": "idempotency-boundary-1",
        "scenario": context["scenario"],
        "applicability": context["applicability"],
        "parameters": context["parameters"],
        "secret_references": context["secret_references"],
        "scenario_artifact": pin,
    }


@pytest.mark.skipif(not Path("/proc/self/fd/3").exists(), reason="Linux inherited-fd vector")
def test_mcc_artifact_verifies_and_runs_through_generic_runner(tmp_path: Path) -> None:
    artifact = _artifact()
    context = _context(artifact)
    key = Ed25519PrivateKey.generate()
    pin = _pin(artifact, key)
    public = _b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    verified = verify_self_hosted_external_artifact(
        pin,
        artifact,
        {"synthetic-boundary-key": {"algorithm": "ed25519", "status": "active", "value": public}},
        kind="scenario",
        signature_domain=MCC_ARTIFACT_SIGNATURE_DOMAIN,
    )
    assert verified["artifact_revision"] == "synthetic-boundary-v1"

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
        job=_job(context, pin),
        facts={},
        context=context,
    )
    assert result["status"] == "succeeded"
    assert result["runner"] == {
        "contract_version": "1.0",
        "frame_type": "result",
        "schema": "mcd-self-hosted-executable-output-v1",
        "status": "succeeded",
        "exit_code": 0,
        "completed": 1,
        "total": 1,
        "rollback": "not_attempted",
    }


def test_runtime_vector_frame_and_binding_are_exact() -> None:
    artifact = _artifact()
    context = _context(artifact)
    frame = encode_request_frame(artifact, context)
    magic, version, header_bytes, artifact_size, context_size, artifact_digest, context_digest, reserved = struct.unpack(
        "!8sHHII32s32sI", frame[:88]
    )
    assert magic == b"SSMCDX01"
    assert (version, header_bytes, reserved) == (1, 88, 0)
    assert artifact_size == len(artifact)
    assert context_size == len(canonical_json(context))
    assert artifact_digest == hashlib.sha256(artifact).digest()
    assert context_digest == hashlib.sha256(canonical_json(context)).digest()
