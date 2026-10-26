from mcd_agent.docker_preflight_runner import DockerPreflightError, DockerRuntimePreflightRunner


def test_runner_rejects_mcc_artifact_digest_with_typed_identity_evidence():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)

    try:
        DockerRuntimePreflightRunner(run=run)({
            "image_ref": "mauticrfp/mautic:playground7-52c88074fa23-v4",
            "image_digest": "sha256:" + "d" * 64,
            "adapter": "mauticrfp-docker-v1",
        })
    except DockerPreflightError as exc:
        assert exc.error_code == "docker_preflight_runtime_image_digest_missing"
        assert exc.stage == "identity"
        assert exc.detail == "runtime_image_digest is required; image_digest is not accepted"
        assert exc.argv == []
    else:
        raise AssertionError("artifact digest must be rejected before Docker execution")
    assert calls == []


def test_runner_rejects_invalid_runtime_digest_with_typed_identity_evidence():
    try:
        DockerRuntimePreflightRunner(run=lambda *args, **kwargs: None)({
            "image_ref": "r/mcd",
            "runtime_image_digest": "sha256:UPPERCASE",
            "adapter": "mauticrfp-docker-v1",
        })
    except DockerPreflightError as exc:
        assert exc.error_code == "docker_preflight_runtime_image_digest_invalid"
        assert exc.stage == "identity"
        assert exc.detail == "runtime_image_digest must match sha256:<64 lowercase hex>"
    else:
        raise AssertionError("invalid runtime digest must fail closed")

def test_runner_has_fixed_argv_and_cleanup_on_failure():
    calls = []
    class Result:
        returncode = 1
        stdout = ""
        stderr = ""
    def run(argv, **kwargs):
        calls.append(argv); return Result()
    try: DockerRuntimePreflightRunner(run=run)({"image_ref":"r/mcd", "runtime_image_digest":"sha256:"+"a"*64, "adapter":"mauticrfp-docker-v1"})
    except RuntimeError: pass
    assert calls[0][0] == "/usr/bin/docker" and all("shell" not in str(x) for x in calls)


def test_runner_classifies_missing_digest_image_with_fixed_argv_evidence():
    calls = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "Error response from daemon: No such image: playground7@sha256:" + "a" * 64

    def run(argv, **kwargs):
        calls.append(argv)
        return Result()

    try:
        DockerRuntimePreflightRunner(run=run)({"image_ref": "playground7", "runtime_image_digest": "sha256:" + "a" * 64, "adapter": "mauticrfp-docker-v1"})
    except DockerPreflightError as exc:
        assert exc.error_code == "docker_preflight_image_unavailable"
        assert exc.stage == "create"
        assert exc.exit_code == 1
        assert exc.cleanup == "not_started"
        assert exc.argv == calls[0]
    else:
        raise AssertionError("missing image must fail with structured evidence")
