"""Generic launcher for Portal-owned executable scenario artifacts.

The launcher is intentionally policy-free.  It does not know an operating
system, Mautic, a package manager, a domain, or an installation recipe.  MCD
verifies the signed artifact before starting this module; this module only
provides a bounded, shell-free hand-off of that artifact and its dynamic
context.
"""

from __future__ import annotations

import json
import hashlib
import os
import struct
import sys
import tempfile
from typing import Any, Mapping


FRAME_MAGIC = b"SSMCDX01"
FRAME_VERSION = 1
FRAME_HEADER_BYTES = 88
FRAME_HEADER = struct.Struct("!8sHHII32s32sI")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_CONTEXT_BYTES = 256 * 1024
CONTEXT_FD = 3
_SECRET_WORDS = ("secret", "password", "token", "private", "credential", "access_key")


class ScenarioLauncherError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def encode_request_frame(artifact: bytes, context: Mapping[str, Any]) -> bytes:
    if not isinstance(artifact, bytes) or not artifact or len(artifact) > MAX_ARTIFACT_BYTES:
        raise ValueError("self_hosted_mcd_runner_artifact_size_invalid")
    context_bytes = json.dumps(dict(context), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(context_bytes) > MAX_CONTEXT_BYTES:
        raise ValueError("self_hosted_mcd_runner_context_size_invalid")
    _validate_context(dict(context))
    return FRAME_HEADER.pack(
        FRAME_MAGIC,
        FRAME_VERSION,
        FRAME_HEADER_BYTES,
        len(artifact),
        len(context_bytes),
        hashlib.sha256(artifact).digest(),
        hashlib.sha256(context_bytes).digest(),
        0,
    ) + artifact + context_bytes


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = int(size)
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ScenarioLauncherError("self_hosted_mcd_runner_frame_truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_request_frame(stream: Any) -> tuple[bytes, dict[str, Any]]:
    header = _read_exact(stream, FRAME_HEADER.size)
    magic, version, header_bytes, artifact_size, context_size, artifact_digest, context_digest, reserved = FRAME_HEADER.unpack(header)
    if magic != FRAME_MAGIC or version != FRAME_VERSION or header_bytes != FRAME_HEADER_BYTES or reserved != 0:
        raise ScenarioLauncherError("self_hosted_mcd_runner_frame_invalid")
    if artifact_size <= 0 or artifact_size > MAX_ARTIFACT_BYTES:
        raise ScenarioLauncherError("self_hosted_mcd_runner_artifact_size_invalid")
    if context_size <= 0 or context_size > MAX_CONTEXT_BYTES:
        raise ScenarioLauncherError("self_hosted_mcd_runner_context_size_invalid")
    artifact = _read_exact(stream, artifact_size)
    raw_context = _read_exact(stream, context_size)
    if hashlib.sha256(artifact).digest() != artifact_digest or hashlib.sha256(raw_context).digest() != context_digest:
        raise ScenarioLauncherError("self_hosted_mcd_runner_frame_digest_invalid")
    try:
        context = json.loads(raw_context.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScenarioLauncherError("self_hosted_mcd_runner_context_invalid") from exc
    if not isinstance(context, dict):
        raise ScenarioLauncherError("self_hosted_mcd_runner_context_invalid")
    _validate_context(context)
    return artifact, context


def _validate_context(value: Mapping[str, Any]) -> None:
    if value.get("schema") != "portal-self-hosted-executable-context-v1" or value.get("contract_version") != "1.0":
        raise ScenarioLauncherError("self_hosted_mcd_runner_context_invalid")
    job = value.get("job")
    scenario = value.get("scenario")
    applicability = value.get("applicability")
    parameters = value.get("parameters")
    references = value.get("secret_references")
    if not isinstance(job, Mapping) or not isinstance(scenario, Mapping) or not isinstance(applicability, Mapping) or not isinstance(parameters, Mapping) or not isinstance(references, list):
        raise ScenarioLauncherError("self_hosted_mcd_runner_context_invalid")
    required_job = ("job_id", "request_id", "command_id", "idempotency_key")
    required_scenario = ("scenario_id", "revision", "artifact_revision", "artifact_digest")
    if any(not isinstance(job.get(key), str) or not job.get(key) for key in required_job) or any(not isinstance(scenario.get(key), str) or not scenario.get(key) for key in required_scenario):
        raise ScenarioLauncherError("self_hosted_mcd_runner_context_invalid")
    digest = str(scenario.get("artifact_digest") or "")
    facts_digest = str(applicability.get("facts_digest") or "")
    if not all(_valid_digest(item) for item in (digest, facts_digest)):
        raise ScenarioLauncherError("self_hosted_mcd_runner_context_invalid")
    for key, item in parameters.items():
        if not isinstance(key, str) or any(word in key.lower() for word in _SECRET_WORDS):
            raise ScenarioLauncherError("self_hosted_mcd_runner_secret_in_context")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ScenarioLauncherError("self_hosted_mcd_runner_context_parameters_invalid")
        if isinstance(item, str) and any(word in item.lower() for word in _SECRET_WORDS):
            raise ScenarioLauncherError("self_hosted_mcd_runner_secret_in_context")
    if any(not isinstance(item, str) or not item or len(item) > 256 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-" for char in item) for item in references):
        raise ScenarioLauncherError("self_hosted_mcd_runner_secret_reference_invalid")


def _valid_digest(value: str) -> bool:
    return len(value) == 71 and value.startswith("sha256:") and all(char in "0123456789abcdef" for char in value[7:])


def _validate_entrypoint(artifact: bytes) -> None:
    # ELF is directly executable.  Script artifacts must declare an absolute
    # interpreter in their signed shebang; MCD never invokes a shell or eval.
    if b"\x00" in artifact:
        raise ScenarioLauncherError("self_hosted_mcd_runner_entrypoint_invalid")
    if artifact.startswith(b"\x7fELF"):
        return
    first_line = artifact.splitlines(True)[0] if artifact else b""
    if not first_line.startswith(b"#!") or len(first_line) > 128:
        raise ScenarioLauncherError("self_hosted_mcd_runner_entrypoint_invalid")
    interpreter = first_line[2:].strip()
    allowed = (b"/bin/sh", b"/usr/bin/sh", b"/bin/bash", b"/usr/bin/bash")
    if interpreter not in allowed or b"\x00" in interpreter:
        raise ScenarioLauncherError("self_hosted_mcd_runner_entrypoint_invalid")


def _write_memfd(name: str, payload: bytes, *, mode: int) -> int:
    create = getattr(os, "memfd_create", None)
    if not callable(create):
        raise ScenarioLauncherError("self_hosted_mcd_runner_memfd_unavailable")
    try:
        fd = int(create(name, 0))
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.lseek(fd, 0, os.SEEK_SET)
        os.set_inheritable(fd, True)
        return fd
    except (OSError, ValueError) as exc:
        raise ScenarioLauncherError("self_hosted_mcd_runner_disk_error") from exc


def _sterile_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
    }


def _exec_path(path: str, context_fd: int) -> None:
    os.dup2(context_fd, CONTEXT_FD, inheritable=True)
    if context_fd != CONTEXT_FD:
        os.close(context_fd)
    os.execve(path, [path, "--mcd-scenario-apply"], _sterile_environment())


def _run_with_secure_files(artifact: bytes, context_bytes: bytes) -> int:
    """Portable fallback for platforms without Linux memfd_create."""
    try:
        with tempfile.TemporaryDirectory(prefix=".mcd-scenario-", dir=tempfile.gettempdir()) as directory:
            os.chmod(directory, 0o700)
            artifact_path = os.path.join(directory, "artifact")
            with open(artifact_path, "wb") as handle:
                os.chmod(artifact_path, 0o700)
                handle.write(artifact)
            context_path = os.path.join(directory, "context")
            with open(context_path, "wb") as handle:
                os.chmod(context_path, 0o600)
                handle.write(context_bytes)
            context_fd = os.open(context_path, os.O_RDONLY)
            os.set_inheritable(context_fd, True)
            pid = os.fork()
            if pid == 0:
                try:
                    _exec_path(artifact_path, context_fd)
                except PermissionError:
                    os._exit(126)
                except OSError:
                    os._exit(127)
            os.close(context_fd)
            _pid, status = os.waitpid(pid, 0)
            if os.WIFSIGNALED(status):
                return 128 + os.WTERMSIG(status)
            return os.WEXITSTATUS(status)
    except (OSError, ValueError) as exc:
        raise ScenarioLauncherError("self_hosted_mcd_runner_disk_error") from exc


def _run(artifact: bytes, context: Mapping[str, Any]) -> int | None:
    _validate_entrypoint(artifact)
    context_bytes = json.dumps(
        dict(context), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if not callable(getattr(os, "memfd_create", None)):
        return _run_with_secure_files(artifact, context_bytes)
    context_fd = _write_memfd("mcd-scenario-context", context_bytes, mode=0o600)
    # Allocate context first so the fixed inherited context FD cannot alias
    # the artifact memfd used as the execve target.
    artifact_fd = _write_memfd("mcd-scenario-artifact", artifact, mode=0o700)
    try:
        artifact_path = f"/proc/self/fd/{artifact_fd}"
        _exec_path(artifact_path, context_fd)
    except PermissionError as exc:
        raise ScenarioLauncherError("self_hosted_mcd_runner_permissions") from exc
    except OSError as exc:
        raise ScenarioLauncherError("self_hosted_mcd_runner_exec_failed") from exc
    finally:
        try:
            os.close(artifact_fd)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["--mcd-scenario-apply"]:
        return 64
    try:
        artifact, context = _read_request_frame(sys.stdin.buffer)
        result = _run(artifact, context)
        return 0 if result is None else int(result)
    except ScenarioLauncherError as exc:
        sys.stdout.write(json.dumps({"status": "failed", "error": {"code": exc.code}}, separators=(",", ":")) + "\n")
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
