import pytest
from mcd_agent.mcd_control import ControlStore

def test_control_response_is_durable_and_replay_safe(tmp_path):
    store = ControlStore(str(tmp_path / "state.sqlite"))
    command = {"command": "docker_runtime_preflight_v1", "job_id": "j1", "request_id": "r1", "agent_id": "a", "host_fingerprint": "sha256:h", "image_ref": "r/mcd", "image_digest": "sha256:" + "a" * 64, "adapter": "mauticrfp-docker-v1", "ttl_seconds": 60}
    envelope = {"schema": "mcd-control-v1", "contract_version": "1.0", "commands": [command]}
    assert store.ingest(envelope, agent_id="a", host_fingerprint="sha256:h")[0]["status"] == "accepted"
    restored = ControlStore(str(tmp_path / "state.sqlite"))
    assert restored.ingest(envelope, agent_id="a", host_fingerprint="sha256:h")[0]["job_id"] == "j1"
    with pytest.raises(ValueError, match="arbitrary"):
        restored.ingest({**envelope, "commands": [{**command, "script": "id"}]}, agent_id="a", host_fingerprint="sha256:h")

def test_control_executes_once_only_with_active_key(tmp_path):
    store = ControlStore(str(tmp_path / "state.sqlite")); calls=[]
    command = {"command":"docker_runtime_preflight_v1","job_id":"j","request_id":"r","probe_id":"p","agent_id":"a","host_fingerprint":"sha256:h","image_ref":"r/mcd","image_digest":"sha256:"+"a"*64,"adapter":"mauticrfp-docker-v1","ttl_seconds":60}
    store.ingest({"schema":"mcd-control-v1","contract_version":"1.0","commands":[command]}, agent_id="a", host_fingerprint="sha256:h")
    assert store.execute_pending(agent_id="a", host_fingerprint="sha256:h", active_key=None, runner=lambda x: {}, signer=lambda x,*y:x) == []
    output = store.execute_pending(agent_id="a", host_fingerprint="sha256:h", active_key=(object(),"k"), runner=lambda x: calls.append(x) or {"cleanup":"clean"}, signer=lambda x,*y:x)
    assert output[0]["status"] == "succeeded" and len(calls) == 1


def test_control_receipt_preserves_structured_runner_failure(tmp_path):
    store = ControlStore(str(tmp_path / "state.sqlite"))
    command = {"command":"docker_runtime_preflight_v1","job_id":"j-evidence","request_id":"r","probe_id":"p","agent_id":"a","host_fingerprint":"sha256:h","image_ref":"playground7","image_digest":"sha256:"+"a"*64,"adapter":"mauticrfp-docker-v1","ttl_seconds":60}
    store.ingest({"schema":"mcd-control-v1","contract_version":"1.0","commands":[command]}, agent_id="a", host_fingerprint="sha256:h")

    class Failure(RuntimeError):
        error_code = "docker_preflight_image_unavailable"
        stage = "create"
        exit_code = 1
        stderr = "No such image"
        cleanup = "not_started"
        argv = ["/usr/bin/docker", "create", "playground7@sha256:" + "a" * 64]

    output = store.execute_pending(
        agent_id="a",
        host_fingerprint="sha256:h",
        active_key=(object(), "k"),
        runner=lambda _request: (_ for _ in ()).throw(Failure()),
        signer=lambda value, *_args: value,
    )
    assert output[0]["error_code"] == "docker_preflight_image_unavailable"
    assert output[0]["stage"] == "create"
    assert output[0]["exit_code"] == 1
    assert output[0]["argv"][0] == "/usr/bin/docker"
