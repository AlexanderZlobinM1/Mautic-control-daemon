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


def test_runner_selects_fixed_versioned_php_fpm_binary():
    calls = []

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["ps", "-aq"]:
            return Result()
        if argv[1:3] == ["create", "--name"]:
            return Result()
        if argv[-2:] == ["/usr/bin/php", "-v"]:
            return Result(stdout="PHP 8.4.3 (cli)")
        if argv[-2:] == ["/usr/sbin/php-fpm", "-v"]:
            return Result(returncode=127, stderr="exec /usr/sbin/php-fpm: no such file or directory")
        if argv[-2:] == ["/usr/sbin/php-fpm8.4", "-v"]:
            return Result(stdout="PHP 8.4.3 (fpm-fcgi)")
        if argv[1:3] == ["rm", "-f"]:
            return Result()
        raise AssertionError(f"unexpected argv: {argv}")

    result = DockerRuntimePreflightRunner(run=run)({
        "image_ref": "mauticrfp/mautic:playground7-52c88074fa23-v4",
        "runtime_image_digest": "sha256:" + "d" * 64,
        "adapter": "mauticrfp-docker-v1",
    })
    assert result["php_cli_version"] == "8.4.3"
    assert result["php_fpm_version"] == "8.4.3"
    assert [argv[-2] for argv in calls if argv[-1] == "-v"] == ["/usr/bin/php", "/usr/sbin/php-fpm", "/usr/sbin/php-fpm8.4"]


def test_runner_reports_missing_php_fpm_allowlist_as_structured_failure():
    class Result:
        returncode = 0
        stdout = "PHP 8.4.3 (cli)"
        stderr = ""

    def run(argv, **kwargs):
        if argv[1:3] == ["ps", "-aq"]:
                return type("Empty", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if argv[1:3] == ["create", "--name"] or argv[1:3] == ["rm", "-f"]:
            return Result()
        if argv[-2:] == ["/usr/bin/php", "-v"]:
            return Result()
        return type("Missing", (), {"returncode": 127, "stdout": "", "stderr": "executable file not found"})()

    try:
        DockerRuntimePreflightRunner(run=run)({
            "image_ref": "r/mcd",
            "runtime_image_digest": "sha256:" + "a" * 64,
            "adapter": "mauticrfp-docker-v1",
        })
    except DockerPreflightError as exc:
        assert exc.error_code == "docker_preflight_php_fpm_binary_not_found"
        assert exc.stage == "php_fpm_version"
        assert exc.cleanup == "clean"
        assert exc.argv == []
    else:
        raise AssertionError("missing PHP-FPM binary must fail with structured evidence")

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
        if argv[1:3] == ["ps", "-aq"]:
            return type("Inventory", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return Result()

    try:
        DockerRuntimePreflightRunner(run=run)({"image_ref": "playground7", "runtime_image_digest": "sha256:" + "a" * 64, "adapter": "mauticrfp-docker-v1"})
    except DockerPreflightError as exc:
        assert exc.error_code == "docker_preflight_image_unavailable"
        assert exc.stage == "create"
        assert exc.exit_code == 1
        assert exc.cleanup == "not_started"
        assert exc.argv == calls[1]
    else:
        raise AssertionError("missing image must fail with structured evidence")


def test_runner_reconciles_created_preflight_container_before_new_probe():
    calls = []

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["ps", "-aq"]:
            return Result(stdout="deadbeefdead\n")
        if argv[1:3] == ["rm", "-f"]:
            return Result()
        return Result(returncode=1, stderr="No such image")

    try:
        DockerRuntimePreflightRunner(run=run)({
            "image_ref": "r/mcd",
            "runtime_image_digest": "sha256:" + "a" * 64,
            "adapter": "mauticrfp-docker-v1",
        })
    except DockerPreflightError as exc:
        assert exc.error_code == "docker_preflight_image_unavailable"
    else:
        raise AssertionError("missing image must fail")
    assert calls[0][1:3] == ["ps", "-aq"]
    assert calls[1][1:3] == ["rm", "-f"]


def test_runner_reports_cleanup_failure_without_masking_probe_failure():
    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def run(argv, **kwargs):
        if argv[1:3] == ["ps", "-aq"]:
            return Result()
        if argv[1:3] == ["create", "--name"]:
            return Result()
        if argv[-2:] == ["/usr/bin/php", "-v"]:
            return Result(stdout="PHP 8.4.3 (cli)")
        if argv[-2:] == ["/usr/sbin/php-fpm", "-v"]:
            return Result(returncode=127, stderr="not found")
        if argv[1:3] == ["rm", "-f"]:
            return Result(returncode=1, stderr="permission denied")
        return Result(returncode=127, stderr="not found")

    try:
        DockerRuntimePreflightRunner(run=run)({
            "image_ref": "r/mcd",
            "runtime_image_digest": "sha256:" + "a" * 64,
            "adapter": "mauticrfp-docker-v1",
        })
    except DockerPreflightError as exc:
        assert exc.error_code == "docker_preflight_php_fpm_binary_not_found"
        assert exc.stage == "php_fpm_version"
        assert exc.cleanup == "failed"
        assert "permission denied" in exc.stderr
    else:
        raise AssertionError("missing PHP-FPM binary must fail")
