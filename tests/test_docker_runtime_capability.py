from datetime import datetime, timezone

import pytest

from mcd_agent.docker_runtime_capability import observe_docker_runtime, validate_docker_runtime_capability


def test_docker_runtime_observation_is_container_bound_and_fresh():
    def run(command):
        return "PHP 8.4.2 (cli)" if command[-2:] == ["/usr/bin/php", "-v"] else "PHP 8.4.2 (fpm)"
    now = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
    value = observe_docker_runtime(image_ref="registry/mcd:7", image_digest="sha256:" + "a" * 64, adapter="mauticrfp-docker-v1", container="mcd", runner=run, now=now)
    assert validate_docker_runtime_capability(value, expected_image_digest=value["image_digest"], expected_adapter=value["adapter"], now=now)["php_cli_version"] == "8.4.2"
    with pytest.raises(ValueError, match="stale"):
        validate_docker_runtime_capability(value, expected_image_digest=value["image_digest"], expected_adapter=value["adapter"], now=datetime(2026, 10, 25, 11, 0, tzinfo=timezone.utc))
