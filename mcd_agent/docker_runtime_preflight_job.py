from __future__ import annotations
from typing import Any, Mapping

COMMAND = "docker_runtime_preflight_v1"

def validate_preflight_job(job: Mapping[str, Any], *, agent_id: str, host_fingerprint: str) -> dict[str, Any]:
    if job.get("command") != COMMAND or job.get("agent_id") != agent_id or job.get("host_fingerprint") != host_fingerprint:
        raise ValueError("docker_preflight_job_binding_invalid")
    required = ("job_id", "probe_id", "request_id", "image_ref", "runtime_image_digest", "adapter", "ttl_seconds")
    if any(not job.get(k) for k in required) or not str(job["runtime_image_digest"]).startswith("sha256:") or int(job["ttl_seconds"]) < 1 or int(job["ttl_seconds"]) > 300:
        raise ValueError("docker_preflight_job_fields_invalid")
    if any(k in job for k in ("shell", "command_line", "argv", "script")):
        raise ValueError("docker_preflight_job_arbitrary_payload_forbidden")
    return {k: job[k] for k in required} | {"command": COMMAND, "agent_id": agent_id, "host_fingerprint": host_fingerprint}

def execute_preflight_job(job: Mapping[str, Any], *, agent_id: str, host_fingerprint: str, store: dict[str, Any], probe: Any) -> dict[str, Any]:
    request = validate_preflight_job(job, agent_id=agent_id, host_fingerprint=host_fingerprint)
    key = str(request["job_id"])
    if key in store:
        return dict(store[key])
    result = dict(probe(request))
    terminal = {"schema": "mcd-docker-runtime-preflight-terminal-v1", "contract_version": "1.0", "job_id": key, "request_id": request["request_id"], "probe_id": request["probe_id"], "status": "succeeded", "idempotency_key": key, "result": result}
    store[key] = terminal
    return terminal
