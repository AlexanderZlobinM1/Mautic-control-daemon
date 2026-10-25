from __future__ import annotations
import re, subprocess, uuid
from typing import Any, Callable

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ADAPTER = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_PHP = re.compile(r"PHP\s+(\d+\.\d+(?:\.\d+)?)", re.I)

class DockerRuntimePreflightRunner:
    """Closed primitive; no caller-supplied executable, argv, shell or mounts."""
    def __init__(self, *, run: Callable[..., Any] = subprocess.run, timeout_sec: int = 120):
        self.run, self.timeout_sec = run, max(1, min(300, int(timeout_sec)))

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        digest, adapter = str(request.get("image_digest") or ""), str(request.get("adapter") or "")
        if not _DIGEST.fullmatch(digest) or not _ADAPTER.fullmatch(adapter):
            raise ValueError("docker_preflight_runner_identity_invalid")
        name = "mcd-preflight-" + uuid.uuid4().hex
        image = str(request.get("image_ref") or "") + "@" + digest
        container = None
        try:
            created = self.run(["/usr/bin/docker", "create", "--name", name, "--network", "none", image, "/bin/sleep", "300"], capture_output=True, text=True, timeout=self.timeout_sec, check=False)
            if created.returncode != 0:
                raise RuntimeError("docker_preflight_create_failed")
            container = name
            facts = {}
            for binary, field in (("/usr/bin/php", "php_cli_version"), ("/usr/sbin/php-fpm", "php_fpm_version")):
                result = self.run(["/usr/bin/docker", "run", "--rm", "--network", "none", image, binary, "-v"], capture_output=True, text=True, timeout=self.timeout_sec, check=False)
                match = _PHP.search((result.stdout or "") + (result.stderr or ""))
                if result.returncode != 0 or not match:
                    raise RuntimeError("docker_preflight_php_observation_failed")
                facts[field] = match.group(1)
            return {"status": "ready", "runtime": "docker", "image_digest": digest, "adapter": adapter, "capabilities": ["php.cli", "php.fpm"], **facts, "cleanup": "clean", "persistent_workload": False}
        finally:
            if container:
                self.run(["/usr/bin/docker", "rm", "-f", container], capture_output=True, text=True, timeout=self.timeout_sec, check=False)
