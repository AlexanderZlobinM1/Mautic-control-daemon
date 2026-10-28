"""Atomic local store for the one MCD scenario execution lane.

Delivery is deliberately separate from execution.  This module only stages,
verifies, activates and retains signed scenario bytes; it never starts a
runner.  A previous active object is removed only after the caller has emitted
the verified delivery receipt.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import time
from typing import Any, Callable, Mapping


SCHEMA = "mcd-scenario-artifact-delivery-receipt-v1"
CONTRACT_VERSION = "1.0"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
# The store accepts the legacy audit-only JSON pin so delivery remains
# backward-compatible; the generic executable runner still rejects JSON at
# execution admission.  Delivery and execution are separate gates.
MEDIA_TYPES = frozenset({"application/x-sh", "application/x-elf", "application/json"})


class ScenarioArtifactStoreError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _valid_digest(value: object) -> bool:
    text = str(value or "")
    return len(text) == 71 and text.startswith("sha256:") and all(char in "0123456789abcdef" for char in text[7:])


class ScenarioArtifactStore:
    """Durable, concurrency-safe active scenario object store."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not self.root.is_absolute() or self.root.is_symlink():
            raise ScenarioArtifactStoreError("scenario_store_path_invalid")
        self.objects = self.root / "objects"
        self.staging = self.root / "staging"
        self.pointer = self.root / "active.json"
        self.pending = self.root / "pending-cleanup.json"
        self.lock_path = self.root / ".lock"
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (self.objects, self.staging):
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        os.chmod(self.root, 0o700)

    def _lock(self):
        handle = self.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @staticmethod
    def _fsync(handle: Any) -> None:
        handle.flush()
        os.fsync(handle.fileno())

    def _fsync_dir(self, path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise ScenarioArtifactStoreError("scenario_store_fsync_failed") from exc

    def _read_pointer(self) -> dict[str, Any] | None:
        try:
            raw = self.pointer.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ScenarioArtifactStoreError("scenario_store_pointer_unavailable") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScenarioArtifactStoreError("scenario_store_pointer_invalid") from exc
        if not isinstance(value, dict) or not isinstance(value.get("digest"), str) or not _valid_digest(value["digest"]):
            raise ScenarioArtifactStoreError("scenario_store_pointer_invalid")
        object_path = self.objects / value["digest"][7:]
        if not object_path.is_file() or object_path.is_symlink():
            raise ScenarioArtifactStoreError("scenario_store_active_object_missing")
        return value

    def active(self) -> dict[str, Any] | None:
        handle = self._lock()
        try:
            return self._read_pointer()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _write_json_atomic(self, destination: Path, value: Mapping[str, Any]) -> None:
        temporary = self.staging / ("." + destination.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                handle.write(_canonical(dict(value)).decode("utf-8") + "\n")
                self._fsync(handle)
            os.replace(temporary, destination)
            self._fsync_dir(destination.parent)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ScenarioArtifactStoreError("scenario_store_write_failed") from exc

    def _cleanup_staging(self, path: Path) -> None:
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError:
            pass

    def deliver(
        self,
        *,
        pin: Mapping[str, Any],
        artifact: bytes,
        verify: Callable[[Mapping[str, Any], bytes], Mapping[str, Any]],
        binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Verify and atomically activate one pinned artifact.

        ``verify`` must perform the existing signature/trust and applicability
        checks.  It is called before the object is moved or the active pointer
        changes.  A failed write or verification leaves the old pointer intact.
        """
        if not isinstance(pin, Mapping):
            raise ScenarioArtifactStoreError("scenario_store_metadata_invalid")
        revision = str(pin.get("artifact_revision") or "").strip()
        digest = str(pin.get("artifact_digest") or "").lower()
        media_type = str(pin.get("artifact_media_type") or "").strip()
        expected_size = pin.get("artifact_size_bytes")
        if not revision or not _valid_digest(digest) or media_type not in MEDIA_TYPES or type(expected_size) is not int or expected_size <= 0 or expected_size > MAX_ARTIFACT_BYTES:
            raise ScenarioArtifactStoreError("scenario_store_metadata_invalid")
        if not isinstance(artifact, bytes) or len(artifact) != expected_size or _digest(artifact) != digest:
            raise ScenarioArtifactStoreError("scenario_store_artifact_integrity_invalid")
        try:
            verified = dict(verify(pin, artifact))
        except ScenarioArtifactStoreError:
            raise
        except Exception as exc:
            raise ScenarioArtifactStoreError("scenario_store_signature_or_trust_invalid") from exc
        if str(verified.get("artifact_revision") or verified.get("revision") or "") != revision or str(verified.get("artifact_digest") or verified.get("digest") or "") != digest:
            raise ScenarioArtifactStoreError("scenario_store_verification_binding_mismatch")

        handle = self._lock()
        staging_path = self.staging / (digest[7:] + "." + secrets.token_hex(8) + ".part")
        try:
            previous = self._read_pointer()
            if previous and previous.get("digest") == digest and previous.get("revision") == revision:
                receipt_id = str((self._read_pending() or {}).get("receipt_id") or secrets.token_hex(16))
                receipt = self._receipt(receipt_id, pin, binding, previous, idempotent=True)
                return {"receipt": receipt, "artifact_path": str(self.objects / digest[7:]), "cleanup_pending": bool(self._read_pending())}
            with staging_path.open("xb") as output:
                os.chmod(staging_path, 0o600)
                output.write(artifact)
                self._fsync(output)
            object_path = self.objects / digest[7:]
            if object_path.exists() and not object_path.is_file():
                raise ScenarioArtifactStoreError("scenario_store_object_invalid")
            if not object_path.exists():
                os.replace(staging_path, object_path)
                self._fsync_dir(self.objects)
            else:
                staging_path.unlink(missing_ok=True)
            receipt_id = secrets.token_hex(16)
            active = {
                "schema": "mcd-scenario-artifact-active-v1",
                "contract_version": CONTRACT_VERSION,
                "receipt_id": receipt_id,
                "revision": revision,
                "digest": digest,
                "media_type": media_type,
                "size_bytes": len(artifact),
                "activated_at": int(time.time()),
            }
            self._write_json_atomic(self.pointer, active)
            if previous and previous.get("digest") != digest:
                self._write_json_atomic(self.pending, {"receipt_id": receipt_id, "previous_digest": previous.get("digest"), "new_digest": digest})
            receipt = self._receipt(receipt_id, pin, binding, active, idempotent=False)
            return {"receipt": receipt, "artifact_path": str(object_path), "cleanup_pending": bool(previous and previous.get("digest") != digest)}
        except ScenarioArtifactStoreError:
            self._cleanup_staging(staging_path)
            raise
        except (OSError, ValueError, TypeError) as exc:
            self._cleanup_staging(staging_path)
            raise ScenarioArtifactStoreError("scenario_store_activation_failed") from exc
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _read_pending(self) -> dict[str, Any] | None:
        if not self.pending.is_file() or self.pending.is_symlink():
            return None
        try:
            value = json.loads(self.pending.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ScenarioArtifactStoreError("scenario_store_pending_cleanup_invalid") from exc
        return dict(value) if isinstance(value, dict) else None

    def _receipt(self, receipt_id: str, pin: Mapping[str, Any], binding: Mapping[str, Any], active: Mapping[str, Any], *, idempotent: bool) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "delivery_id": receipt_id,
            "idempotency_key": str(binding.get("idempotency_key") or receipt_id),
            "binding": dict(binding),
            "status": "verified",
            "revision": str(active["revision"]),
            "digest": str(active["digest"]),
            "media_type": str(active["media_type"]),
            "size_bytes": int(active["size_bytes"]),
            "idempotent": bool(idempotent),
            "cleanup_after_receipt": True,
            "observed_at": int(time.time()),
        }

    def acknowledge(self, receipt: Mapping[str, Any]) -> None:
        """Finish cleanup only after the verified delivery receipt was emitted."""
        if not isinstance(receipt, Mapping) or receipt.get("schema") != SCHEMA or not receipt.get("delivery_id"):
            raise ScenarioArtifactStoreError("scenario_store_receipt_invalid")
        handle = self._lock()
        try:
            pending = self._read_pending()
            if not pending or pending.get("receipt_id") != receipt.get("delivery_id"):
                return
            previous = str(pending.get("previous_digest") or "")
            active = self._read_pointer()
            if active is not None and active.get("digest") == pending.get("new_digest") and _valid_digest(previous):
                old_path = self.objects / previous[7:]
                if old_path.exists() and old_path.is_file() and not old_path.is_symlink():
                    old_path.unlink()
                    self._fsync_dir(self.objects)
            self.pending.unlink(missing_ok=True)
            self._fsync_dir(self.root)
        except OSError as exc:
            raise ScenarioArtifactStoreError("scenario_store_cleanup_failed") from exc
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def acknowledge_pending(self) -> None:
        """Acknowledge the durable delivery receipt after its queue drains."""
        pending = self._read_pending()
        if pending:
            self.acknowledge({"schema": SCHEMA, "delivery_id": pending.get("receipt_id")})
