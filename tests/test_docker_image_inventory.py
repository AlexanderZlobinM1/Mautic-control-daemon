import json

from mcd_agent.docker_image_inventory import capability, collect_signed_inventory
from mcd_agent.result_signing import ensure_result_key


def test_inventory_is_signed_bound_and_read_only(tmp_path):
    key_path = str(tmp_path / "result.key")
    private, public = ensure_result_key(key_path)
    image_id = "sha256:" + "1" * 64
    repo_digest = "sha256:" + "2" * 64
    calls = []

    class Result:
        def __init__(self, stdout, returncode=0, stderr=""):
            self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:4] == ["/usr/bin/docker", "image", "ls", "--all"]:
            return Result(json.dumps({"ID": image_id, "Repository": "mauticrfp/mautic", "Tag": "playground7-v4"}) + "\n")
        assert argv[:4] == ["/usr/bin/docker", "image", "inspect", image_id]
        return Result(json.dumps([{
            "Id": image_id,
            "RepoDigests": [f"mauticrfp/mautic@{repo_digest}"],
            "Config": {"Labels": {"mcc.files_sha256": "3" * 64, "org.opencontainers.image.version": "7"}},
        }]))

    inventory = collect_signed_inventory(
        agent_id="mcd-agent-1",
        host_fingerprint="sha256:" + "a" * 64,
        private_key=private,
        key_id=public["key_id"],
        run=run,
    )

    assert inventory["schema"] == "mcd-docker-runtime-image-inventory-v1"
    assert inventory["status"] == "ok"
    assert inventory["agent_id"] == "mcd-agent-1"
    assert inventory["host_fingerprint"] == "sha256:" + "a" * 64
    assert inventory["signature"]["key_id"] == public["key_id"]
    image = inventory["images"][0]
    assert image["image_ref"] == "mauticrfp/mautic:playground7-v4"
    assert image["runtime_image_digest"] == repo_digest
    assert image["fixed_runtime_ref"].endswith("@" + repo_digest)
    assert image["image_id"] == image_id
    assert image["files_sha256"] == "sha256:" + "3" * 64
    assert all(argv[0:3] == ["/usr/bin/docker", "image", "ls"] or argv[0:3] == ["/usr/bin/docker", "image", "inspect"] for argv in calls)


def test_inventory_capability_is_read_only_and_requires_runtime_digest():
    descriptor = capability()
    assert descriptor["read_only"] is True
    assert descriptor["pull"] is False
    assert descriptor["signed"] is True
    assert "runtime_image_digest" in descriptor["image_fields"]
    assert "files_sha256" in descriptor["image_fields"]
