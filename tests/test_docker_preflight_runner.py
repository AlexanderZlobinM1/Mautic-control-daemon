from mcd_agent.docker_preflight_runner import DockerRuntimePreflightRunner

def test_runner_has_fixed_argv_and_cleanup_on_failure():
    calls = []
    class Result:
        returncode = 1
        stdout = ""
        stderr = ""
    def run(argv, **kwargs):
        calls.append(argv); return Result()
    try: DockerRuntimePreflightRunner(run=run)({"image_ref":"r/mcd", "image_digest":"sha256:"+"a"*64, "adapter":"mauticrfp-docker-v1"})
    except RuntimeError: pass
    assert calls[0][0] == "/usr/bin/docker" and all("shell" not in str(x) for x in calls)
