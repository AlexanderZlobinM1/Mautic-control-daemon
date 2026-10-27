from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from mcd_agent.scenario_launcher import (
    FRAME_HEADER,
    FRAME_MAGIC,
    encode_request_frame,
)
from mcd_agent.commercial_self_hosted import ExternalProcessPolicy


def test_frame_is_binary_artifact_plus_separate_context() -> None:
    artifact = b"#!/bin/sh\nprintf '%s\\n' signed\n"
    context = {
        "schema": "portal-self-hosted-executable-context-v1",
        "contract_version": "1.0",
        "job": {"job_id": "j", "request_id": "r", "command_id": "c", "idempotency_key": "i"},
        "scenario": {"scenario_id": "s", "revision": "1", "artifact_revision": "1", "artifact_digest": "sha256:" + "0" * 64},
        "applicability": {"facts_digest": "sha256:" + "1" * 64},
        "parameters": {},
        "secret_references": [],
    }
    frame = encode_request_frame(artifact, context)
    magic, version, header_bytes, artifact_size, context_size, _artifact_digest, _context_digest, reserved = FRAME_HEADER.unpack(frame[: FRAME_HEADER.size])
    assert magic == FRAME_MAGIC
    assert version == 1 and header_bytes == 88 and reserved == 0
    assert frame[FRAME_HEADER.size : FRAME_HEADER.size + artifact_size] == artifact
    context = json.loads(frame[FRAME_HEADER.size + artifact_size :].decode("utf-8"))
    assert context["applicability"]["facts_digest"].startswith("sha256:")


def test_launcher_executes_signed_artifact_without_shell_and_exposes_context_on_fd3(tmp_path: Path) -> None:
    artifact = b"#!/bin/sh\ncat <&3 >/dev/null || exit 71\nprintf '%s\\n' '{\"status\":\"succeeded\",\"job_id\":\"job-1\"}'\n"
    context = {
        "schema": "portal-self-hosted-executable-context-v1",
        "contract_version": "1.0",
        "job": {"job_id": "job-1", "request_id": "request-1", "command_id": "command-1", "idempotency_key": "idempotency-1"},
        "scenario": {"scenario_id": "scenario-1", "revision": "1", "artifact_revision": "1", "artifact_digest": "sha256:" + "0" * 64},
        "applicability": {"facts_digest": "sha256:" + "1" * 64},
        "parameters": {},
        "secret_references": [],
    }
    env = {"PYTHONPATH": str(Path(__file__).parents[1]), "PATH": os.defpath}
    result = subprocess.run(
        [sys.executable, "-m", "mcd_agent.scenario_launcher", "--mcd-scenario-apply"],
        input=encode_request_frame(artifact, context),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {"status": "succeeded", "job_id": "job-1"}


def test_launcher_rejects_non_executable_json_artifact() -> None:
    env = {"PYTHONPATH": str(Path(__file__).parents[1]), "PATH": os.defpath}
    result = subprocess.run(
        [sys.executable, "-m", "mcd_agent.scenario_launcher", "--mcd-scenario-apply"],
        input=encode_request_frame(
            b'{"schema":"portal-self-hosted-scenario-artifact-v1"}',
            {
                "schema": "portal-self-hosted-executable-context-v1",
                "contract_version": "1.0",
                "job": {"job_id": "j", "request_id": "r", "command_id": "c", "idempotency_key": "i"},
                "scenario": {"scenario_id": "s", "revision": "1", "artifact_revision": "1", "artifact_digest": "sha256:" + "0" * 64},
                "applicability": {"facts_digest": "sha256:" + "1" * 64},
                "parameters": {},
                "secret_references": [],
            },
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["code"] == "self_hosted_mcd_runner_entrypoint_invalid"


def test_frame_rejects_oversized_context() -> None:
    with pytest.raises(ValueError, match="context_size_invalid"):
        encode_request_frame(
            b"#!/bin/sh\n",
            {
                "schema": "portal-self-hosted-executable-context-v1",
                "contract_version": "1.0",
                "job": {"job_id": "j", "request_id": "r", "command_id": "c", "idempotency_key": "i"},
                "scenario": {"scenario_id": "s", "revision": "1", "artifact_revision": "1", "artifact_digest": "sha256:" + "0" * 64},
                "applicability": {"facts_digest": "sha256:" + "1" * 64},
                "parameters": {"value": "x" * (256 * 1024)},
                "secret_references": [],
            },
        )


def test_portal_shared_frame_vector_is_byte_identical() -> None:
    artifact = b"#!/bin/sh\nprintf '%s\\n' scenario-fixture\n"
    context = {
        "schema": "portal-self-hosted-executable-context-v1",
        "contract_version": "1.0",
        "job": {"job_id": "job-vector-1", "request_id": "request-vector-1", "command_id": "command-vector-1", "idempotency_key": "vector-1"},
        "scenario": {"scenario_id": "debian-family-self-hosted", "revision": "3.0.0-executable-fixture", "artifact_revision": "3.0.0-executable-fixture", "artifact_digest": "sha256:" + hashlib.sha256(artifact).hexdigest()},
        "applicability": {"facts_digest": "sha256:" + "1" * 64},
        "parameters": {"domain": "tetnew.sales-snap.com", "dns_provider": "fixture", "dns_reference": "dns-ref-1"},
        "secret_references": ["commercial_key_binding"],
    }
    frame = encode_request_frame(artifact, context)
    assert hashlib.sha256(frame).hexdigest() == "ea370a2f282f48a5a74ee7234a29fa0c45671b67c3b2fb03550a550391038635"
    assert hashlib.sha256(json.dumps(context, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == "77bf8e86c337f9fca9e4b20257d63aad725bf022b53abb42baacffb4156e148f"


def _runner_script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "runner"
    path.write_text(f"#!{sys.executable}\nimport sys\n{body}\n", encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def _run_policy(path: str, *, timeout: int = 2) -> dict[str, object]:
    return ExternalProcessPolicy([path], timeout_sec=timeout).run(
        artifact=b"#! /bin/true\n",
        job={
            "job_id": "job-1",
            "request_id": "request-1",
            "idempotency_key": "idempotency-1",
            "scenario": {"scenario_id": "scenario-1", "revision": "1", "artifact_digest": "sha256:" + "0" * 64},
            "scenario_artifact": {"artifact_revision": "1", "artifact_digest": "sha256:" + "0" * 64, "artifact_media_type": "application/x-sh"},
            "applicability": {"facts_digest": "sha256:" + "1" * 64},
        },
        facts={"os_id": "ubuntu"},
    )


def test_policy_maps_signal_and_bounds_output(tmp_path: Path) -> None:
    signal_runner = _runner_script(tmp_path, "sys.stdin.buffer.read(); import os; os.kill(os.getpid(), signal.SIGTERM)")
    # The child script imports signal only when requested by the body.
    Path(signal_runner).write_text(f"#!{sys.executable}\nimport sys, os, signal\nsys.stdin.buffer.read(); os.kill(os.getpid(), signal.SIGTERM)\n", encoding="utf-8")
    Path(signal_runner).chmod(0o700)
    with pytest.raises(ValueError, match="runner_signal_15"):
        _run_policy(signal_runner)

    output_runner = _runner_script(tmp_path, "sys.stdin.buffer.read(); sys.stdout.buffer.write((70000).to_bytes(4, 'big') + b'x' * 70000)")
    with pytest.raises(ValueError, match="runner_output_frame_invalid"):
        _run_policy(output_runner)


def test_policy_accepts_canonical_progress_and_terminal_frames(tmp_path: Path) -> None:
    runner = _runner_script(
        tmp_path,
        "import json, struct; sys.stdin.buffer.read(); frames=[{'schema':'mcd-self-hosted-executable-output-v1','contract_version':'1.0','frame_type':'progress','sequence':1,'status':'running','message_code':'started','percent':5},{'schema':'mcd-self-hosted-executable-output-v1','contract_version':'1.0','frame_type':'result','status':'succeeded','exit_code':0,'completed':1,'total':1,'rollback':'not_attempted'}]; out=b''.join(struct.pack('!I', len(p)) + p for p in [json.dumps(f, separators=(',',':'), sort_keys=True).encode() for f in frames]); sys.stdout.buffer.write(out)",
    )
    result = _run_policy(runner)
    assert result["status"] == "succeeded"


def test_policy_timeout_terminates_process_group(tmp_path: Path) -> None:
    runner = _runner_script(tmp_path, "sys.stdin.buffer.read(); time.sleep(3)")
    Path(runner).write_text(f"#!{sys.executable}\nimport sys, time\nsys.stdin.buffer.read(); time.sleep(3)\n", encoding="utf-8")
    Path(runner).chmod(0o700)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="runner_timeout"):
        _run_policy(runner, timeout=1)
    assert time.monotonic() - started < 4


def test_policy_redacts_secret_shaped_runner_result(tmp_path: Path) -> None:
    runner = _runner_script(tmp_path, "import json, struct; sys.stdin.buffer.read(); p=json.dumps({'schema':'mcd-self-hosted-executable-output-v1','contract_version':'1.0','frame_type':'result','status':'succeeded','exit_code':0,'completed':1,'total':1,'rollback':'not_attempted','token':'do-not-persist'}, separators=(',',':'), sort_keys=True).encode(); sys.stdout.buffer.write(struct.pack('!I', len(p)) + p)")
    with pytest.raises(ValueError, match="runner_output_secret_detected"):
        _run_policy(runner)
