import pytest
from mcd_agent.docker_runtime_preflight_job import execute_preflight_job, validate_preflight_job

def test_preflight_job_is_allowlisted_idempotent_and_no_shell():
    job = {"command": "docker_runtime_preflight_v1", "job_id": "j", "probe_id": "p", "request_id": "r", "agent_id": "a", "host_fingerprint": "sha256:h", "image_ref": "r/mcd", "runtime_image_digest": "sha256:" + "a" * 64, "adapter": "mauticrfp-docker-v1", "ttl_seconds": 60}
    store = {}
    calls = []
    probe = lambda request: calls.append(request) or {"status": "ready"}
    assert execute_preflight_job(job, agent_id="a", host_fingerprint="sha256:h", store=store, probe=probe)["status"] == "succeeded"
    execute_preflight_job(job, agent_id="a", host_fingerprint="sha256:h", store=store, probe=probe)
    assert len(calls) == 1
    with pytest.raises(ValueError, match="arbitrary"):
        validate_preflight_job({**job, "script": "id"}, agent_id="a", host_fingerprint="sha256:h")
