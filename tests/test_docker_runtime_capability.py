from datetime import datetime, timezone

import pytest

from mcd_agent.docker_runtime_capability import observe_docker_runtime, validate_docker_runtime_capability
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import base64


def test_docker_runtime_observation_is_container_bound_and_fresh():
    def run(command):
        return "PHP 8.4.2 (cli)" if command[-2:] == ["/usr/bin/php", "-v"] else "PHP 8.4.2 (fpm)"
    now = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    value = observe_docker_runtime(image_ref="registry/mcd:7", image_digest="sha256:" + "a" * 64, adapter="mauticrfp-docker-v1", container="mcd", runner=run, agent_id="a1", installation_id="i1", host_fingerprint="sha256:h", signing_key=key, key_id="k1", now=now)
    kwargs = dict(expected_image_digest=value["image_digest"], expected_adapter=value["adapter"], portal_keys={"k1": public}, expected_agent_id="a1", expected_installation_id="i1", expected_host_fingerprint="sha256:h", now=now)
    assert validate_docker_runtime_capability(value, **kwargs)["php_cli_version"] == "8.4.2"
    with pytest.raises(ValueError, match="stale"):
        validate_docker_runtime_capability(value, **{**kwargs, "now": datetime(2026, 10, 25, 11, 0, tzinfo=timezone.utc)})
    with pytest.raises(ValueError, match="unknown"):
        validate_docker_runtime_capability(value, **{**kwargs, "portal_keys": {}})
