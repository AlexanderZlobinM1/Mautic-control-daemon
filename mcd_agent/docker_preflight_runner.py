from __future__ import annotations
import re, subprocess, uuid
from typing import Any, Callable

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ADAPTER = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_PHP = re.compile(r"PHP\s+(\d+\.\d+(?:\.\d+)?)", re.I)


class DockerPreflightError(RuntimeError):
    """Safe, structured evidence for a fixed Docker preflight argv failure."""

    def __init__(
        self,
        error_code: str,
        *,
        stage: str,
        argv: list[str],
        exit_code: int | None = None,
        stderr: str = "",
        detail: str = "",
        cleanup: str = "not_started",
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.stage = stage
        self.argv = list(argv)
        self.exit_code = exit_code
        self.stderr = _safe_stderr(stderr)
        self.detail = _safe_stderr(detail)
        self.cleanup = cleanup


def _safe_stderr(value: object) -> str:
    """Bound daemon output and redact common secret-shaped assignments."""
    text = str(value or "").strip()
    text = re.sub(r"(?i)(authorization|password|passwd|token|secret)=\S+", r"\1=<redacted>", text)
    return text[-512:]


def _create_error(stderr: object, returncode: int, argv: list[str]) -> DockerPreflightError:
    lower = str(stderr or "").lower()
    if "no such image" in lower or "pull access denied" in lower or "manifest unknown" in lower:
        code = "docker_preflight_image_unavailable"
    elif "permission denied" in lower or "cannot connect to the docker daemon" in lower:
        code = "docker_preflight_docker_access_failed"
    elif "invalid reference format" in lower:
        code = "docker_preflight_image_reference_invalid"
    else:
        code = "docker_preflight_create_failed"
    return DockerPreflightError(code, stage="create", argv=argv, exit_code=returncode, stderr=str(stderr or ""))

class DockerRuntimePreflightRunner:
    """Closed primitive; no caller-supplied executable, argv, shell or mounts."""
    def __init__(self, *, run: Callable[..., Any] = subprocess.run, timeout_sec: int = 120):
        self.run, self.timeout_sec = run, max(1, min(300, int(timeout_sec)))

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        # Only the actual Docker RepoDigest is accepted; MCC asset-pair/files
        # digests are never valid runtime image identities.
        digest = str(request.get("runtime_image_digest") or "")
        adapter = str(request.get("adapter") or "")
        if not digest:
            raise DockerPreflightError(
                "docker_preflight_runtime_image_digest_missing",
                stage="identity",
                argv=[],
                detail="runtime_image_digest is required; image_digest is not accepted",
            )
        if not _DIGEST.fullmatch(digest):
            raise DockerPreflightError(
                "docker_preflight_runtime_image_digest_invalid",
                stage="identity",
                argv=[],
                detail="runtime_image_digest must match sha256:<64 lowercase hex>",
            )
        if not _ADAPTER.fullmatch(adapter):
            raise DockerPreflightError(
                "docker_preflight_adapter_invalid",
                stage="identity",
                argv=[],
                detail="adapter must be a bounded lowercase identifier",
            )
        name = "mcd-preflight-" + uuid.uuid4().hex
        image = str(request.get("image_ref") or "") + "@" + digest
        container = None
        try:
            create_argv = ["/usr/bin/docker", "create", "--name", name, "--network", "none", image, "/bin/sleep", "300"]
            try:
                created = self.run(create_argv, capture_output=True, text=True, timeout=self.timeout_sec, check=False)
            except subprocess.TimeoutExpired as exc:
                raise DockerPreflightError("docker_preflight_create_timeout", stage="create", argv=create_argv, stderr=str(exc)) from exc
            if created.returncode != 0:
                raise _create_error(created.stderr, created.returncode, create_argv)
            container = name
            facts = {}
            for binary, field in (("/usr/bin/php", "php_cli_version"), ("/usr/sbin/php-fpm", "php_fpm_version")):
                run_argv = ["/usr/bin/docker", "run", "--rm", "--network", "none", image, binary, "-v"]
                try:
                    result = self.run(run_argv, capture_output=True, text=True, timeout=self.timeout_sec, check=False)
                except subprocess.TimeoutExpired as exc:
                    raise DockerPreflightError("docker_preflight_php_observation_timeout", stage=field, argv=run_argv, stderr=str(exc), cleanup="container_pending") from exc
                match = _PHP.search((result.stdout or "") + (result.stderr or ""))
                if result.returncode != 0 or not match:
                    raise DockerPreflightError(
                        "docker_preflight_php_observation_failed",
                        stage=field,
                        argv=run_argv,
                        exit_code=result.returncode,
                        stderr=(result.stderr or result.stdout or ""),
                        cleanup="container_pending",
                    )
                facts[field] = match.group(1)
            return {"status": "ready", "runtime": "docker", "image_digest": digest, "adapter": adapter, "capabilities": ["php.cli", "php.fpm"], **facts, "cleanup": "clean", "persistent_workload": False}
        finally:
            if container:
                try:
                    self.run(["/usr/bin/docker", "rm", "-f", container], capture_output=True, text=True, timeout=self.timeout_sec, check=False)
                except Exception:
                    # The terminal receipt remains a failure; cleanup errors
                    # must never mask the original fixed-argv evidence.
                    pass
