from __future__ import annotations
import re, subprocess, uuid
from typing import Any, Callable

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ADAPTER = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_PHP = re.compile(r"PHP\s+(\d+\.\d+(?:\.\d+)?)", re.I)
_PHP_FPM_BINARIES = (
    "/usr/sbin/php-fpm",
    "/usr/sbin/php-fpm8.4",
    "/usr/sbin/php-fpm8.3",
    "/usr/sbin/php-fpm8.2",
    "/usr/sbin/php-fpm8.1",
    "/usr/sbin/php-fpm8.0",
    "/usr/sbin/php-fpm7.4",
    "/usr/sbin/php-fpm7.3",
    "/usr/sbin/php-fpm7.2",
    "/usr/sbin/php-fpm7.1",
    "/usr/sbin/php-fpm7.0",
    "/usr/bin/php-fpm",
    "/usr/bin/php-fpm8.4",
    "/usr/bin/php-fpm8.3",
    "/usr/bin/php-fpm8.2",
    "/usr/bin/php-fpm8.1",
    "/usr/bin/php-fpm8.0",
    "/usr/bin/php-fpm7.4",
    "/usr/bin/php-fpm7.3",
    "/usr/bin/php-fpm7.2",
    "/usr/bin/php-fpm7.1",
    "/usr/bin/php-fpm7.0",
)
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")


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


def _missing_binary(result: Any) -> bool:
    return result.returncode == 127 and bool(re.search(r"(?i)(not found|no such file|executable file not found)", str(result.stderr or "")))


def _cleanup_result_ok(result: Any) -> bool:
    return result.returncode == 0 or bool(re.search(r"(?i)no such container", str(result.stderr or "")))

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
        self._reconcile_created_containers()
        name = "mcd-preflight-" + uuid.uuid4().hex
        image = str(request.get("image_ref") or "") + "@" + digest
        container = None
        failure: DockerPreflightError | None = None
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
            for binary, field in (("/usr/bin/php", "php_cli_version"),):
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
            for binary in _PHP_FPM_BINARIES:
                run_argv = ["/usr/bin/docker", "run", "--rm", "--network", "none", image, binary, "-v"]
                try:
                    result = self.run(run_argv, capture_output=True, text=True, timeout=self.timeout_sec, check=False)
                except subprocess.TimeoutExpired as exc:
                    raise DockerPreflightError("docker_preflight_php_observation_timeout", stage="php_fpm_version", argv=run_argv, stderr=str(exc), cleanup="container_pending") from exc
                if _missing_binary(result):
                    continue
                match = _PHP.search((result.stdout or "") + (result.stderr or ""))
                if result.returncode != 0 or not match:
                    raise DockerPreflightError(
                        "docker_preflight_php_observation_failed",
                        stage="php_fpm_version",
                        argv=run_argv,
                        exit_code=result.returncode,
                        stderr=(result.stderr or result.stdout or ""),
                        cleanup="container_pending",
                    )
                facts["php_fpm_version"] = match.group(1)
                break
            else:
                raise DockerPreflightError(
                    "docker_preflight_php_fpm_binary_not_found",
                    stage="php_fpm_version",
                    argv=[],
                    detail="no allowed PHP-FPM binary found in image",
                    cleanup="container_pending",
                )
            return {"status": "ready", "runtime": "docker", "image_digest": digest, "adapter": adapter, "capabilities": ["php.cli", "php.fpm"], **facts, "cleanup": "clean", "persistent_workload": False}
        except DockerPreflightError as exc:
            failure = exc
            raise
        finally:
            if container:
                cleanup_argv = ["/usr/bin/docker", "rm", "-f", container]
                try:
                    cleaned = self.run(cleanup_argv, capture_output=True, text=True, timeout=self.timeout_sec, check=False)
                except subprocess.TimeoutExpired as exc:
                    cleaned = None
                    if failure is not None:
                        failure.cleanup = "failed"
                        failure.detail = _safe_stderr(failure.detail or f"container cleanup timeout: {exc}")
                    else:
                        raise DockerPreflightError("docker_preflight_cleanup_timeout", stage="cleanup", argv=cleanup_argv, stderr=str(exc), cleanup="failed") from exc
                except Exception as exc:
                    cleaned = None
                    if failure is not None:
                        failure.cleanup = "failed"
                        failure.detail = _safe_stderr(failure.detail or f"container cleanup failed: {exc}")
                    else:
                        raise DockerPreflightError("docker_preflight_cleanup_failed", stage="cleanup", argv=cleanup_argv, detail="container cleanup failed", cleanup="failed") from exc
                if failure is not None:
                    if cleaned is not None and _cleanup_result_ok(cleaned):
                        failure.cleanup = "clean"
                    elif cleaned is not None:
                        failure.cleanup = "failed"
                        failure.stderr = _safe_stderr(failure.stderr or cleaned.stderr)
                        failure.detail = _safe_stderr(failure.detail or "container cleanup failed")
                elif cleaned is not None and not _cleanup_result_ok(cleaned):
                    raise DockerPreflightError("docker_preflight_cleanup_failed", stage="cleanup", argv=cleanup_argv, exit_code=cleaned.returncode, stderr=cleaned.stderr, detail="container cleanup failed", cleanup="failed")

    def _reconcile_created_containers(self) -> None:
        list_argv = [
            "/usr/bin/docker", "ps", "-aq",
            "--filter", "name=^mcd-preflight-",
            "--filter", "status=created",
            "--format", "{{.ID}}",
        ]
        try:
            listed = self.run(list_argv, capture_output=True, text=True, timeout=self.timeout_sec, check=False)
        except subprocess.TimeoutExpired as exc:
            raise DockerPreflightError("docker_preflight_cleanup_reconcile_timeout", stage="cleanup", argv=list_argv, stderr=str(exc), cleanup="failed") from exc
        if listed.returncode != 0:
            raise DockerPreflightError("docker_preflight_cleanup_reconcile_failed", stage="cleanup", argv=list_argv, exit_code=listed.returncode, stderr=listed.stderr, cleanup="failed")
        for raw_id in str(listed.stdout or "").splitlines():
            container_id = raw_id.strip()
            if not container_id:
                continue
            if not _CONTAINER_ID.fullmatch(container_id):
                raise DockerPreflightError("docker_preflight_cleanup_reconcile_invalid_id", stage="cleanup", argv=list_argv, detail="Docker returned an invalid preflight container id", cleanup="failed")
            cleanup_argv = ["/usr/bin/docker", "rm", "-f", container_id]
            try:
                removed = self.run(cleanup_argv, capture_output=True, text=True, timeout=self.timeout_sec, check=False)
            except subprocess.TimeoutExpired as exc:
                raise DockerPreflightError("docker_preflight_cleanup_reconcile_timeout", stage="cleanup", argv=cleanup_argv, stderr=str(exc), cleanup="failed") from exc
            if not _cleanup_result_ok(removed):
                raise DockerPreflightError("docker_preflight_cleanup_reconcile_failed", stage="cleanup", argv=cleanup_argv, exit_code=removed.returncode, stderr=removed.stderr, cleanup="failed")
