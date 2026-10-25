from datetime import datetime, timezone
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from mcd_agent.docker_runtime_capability import build_docker_runtime_preflight, validate_docker_runtime_preflight

def test_preflight_is_signed_request_bound_and_has_no_installation():
    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    now = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
    def run(command): return "PHP 8.4.2" if command[-1] == "php-cli" else "PHP 8.4.2"
    value = build_docker_runtime_preflight(image_ref="registry/mcd:7", image_digest="sha256:" + "b" * 64, adapter="mauticrfp-docker-v1", agent_id="a", host_fingerprint="sha256:h", probe_id="p", request_id="r", runner=run, signing_key=key, key_id="k", now=now)
    assert validate_docker_runtime_preflight(value, portal_keys={"k": public}, expected_agent_id="a", expected_host_fingerprint="sha256:h", expected_image_digest=value["image_digest"], expected_adapter=value["adapter"], expected_probe_id="p", expected_request_id="r", now=now)["installation_id"] is None
    with pytest.raises(ValueError, match="binding"):
        validate_docker_runtime_preflight(value, portal_keys={"k": public}, expected_agent_id="a", expected_host_fingerprint="sha256:h", expected_image_digest=value["image_digest"], expected_adapter=value["adapter"], expected_probe_id="p", expected_request_id="other", now=now)
