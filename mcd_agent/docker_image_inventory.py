from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import subprocess
from importlib.resources import files
from typing import Any, Callable, Mapping

from mcd_agent.commercial_portal import canonical_json
from mcd_agent.result_signing import sign_result

SCHEMA = "mcd-docker-runtime-image-inventory-v1"
CAPABILITY_SCHEMA = "mcd-docker-runtime-image-inventory-capability-v1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LABEL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_FILES_SHA = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_MAX_IMAGES = 256


def capability() -> dict[str, Any]:
    contract = files("mcd_agent").joinpath("contracts/mcd-docker-runtime-image-inventory-v1.json").read_bytes()
    return {
        "schema": CAPABILITY_SCHEMA,
        "contract_version": "1.0",
        "inventory_schema": SCHEMA,
        "read_only": True,
        "pull": False,
        "signed": True,
        "bound_fields": ["agent_id", "host_fingerprint", "key_id"],
        "image_fields": [
            "image_ref",
            "fixed_runtime_ref",
            "runtime_image_digest",
            "image_id",
            "repo_digests",
            "labels",
            "files_sha256",
        ],
        "contract_sha256": hashlib.sha256(contract).hexdigest(),
    }


def _run_default(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, **kwargs)


def _safe_labels(raw: object) -> tuple[dict[str, str], str | None]:
    labels = raw if isinstance(raw, Mapping) else {}
    safe: dict[str, str] = {}
    files_sha256: str | None = None
    for raw_key, raw_value in labels.items():
        key = str(raw_key or "")
        value = str(raw_value or "")
        if not _LABEL_KEY.fullmatch(key) or len(value) > 512:
            continue
        safe[key] = value
        normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if normalized.endswith("files_sha256") or normalized.endswith("files_sha"):
            candidate = value if value.startswith("sha256:") else "sha256:" + value
            if _FILES_SHA.fullmatch(candidate):
                files_sha256 = candidate
    return dict(sorted(safe.items())), files_sha256


def _image_item(raw: Mapping[str, Any], listed: Mapping[str, Any]) -> dict[str, Any]:
    image_id = str(raw.get("Id") or listed.get("ID") or "").strip()
    repo_digests = sorted(
        {
            str(value).strip()
            for value in raw.get("RepoDigests") or []
            if isinstance(value, str) and "@sha256:" in value and _DIGEST.fullmatch(value.rsplit("@", 1)[1])
        }
    )
    repository = str(listed.get("Repository") or "").strip()
    tag = str(listed.get("Tag") or "").strip()
    image_ref = None if not repository or repository == "<none>" or not tag or tag == "<none>" else f"{repository}:{tag}"
    matching = [value for value in repo_digests if image_ref and value.split("@", 1)[0] == repository]
    runtime_digest = matching[0].rsplit("@", 1)[1] if matching else None
    labels, files_sha256 = _safe_labels((raw.get("Config") or {}).get("Labels") if isinstance(raw.get("Config"), dict) else {})
    return {
        "image_ref": image_ref,
        "fixed_runtime_ref": f"{image_ref}@{runtime_digest}" if image_ref and runtime_digest else None,
        "runtime_image_digest": runtime_digest,
        "image_id": image_id,
        "repo_digests": repo_digests,
        "labels": labels,
        "files_sha256": files_sha256,
    }


def collect_signed_inventory(
    *,
    agent_id: str,
    host_fingerprint: str,
    private_key: Any | None,
    key_id: str,
    run: Callable[..., Any] = _run_default,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect local Docker metadata only; never pulls, creates or runs images."""
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "contract_version": "1.0",
        "status": "unavailable",
        "agent_id": str(agent_id),
        "host_fingerprint": str(host_fingerprint),
        "observed_at": observed,
        "images": [],
    }
    if private_key is None or not key_id:
        base["reason"] = "result_signing_key_unavailable"
        return base
    try:
        listed_result = run(
            ["/usr/bin/docker", "image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if listed_result.returncode != 0:
            base["reason"] = "docker_image_inventory_list_failed"
            base["stderr"] = str(listed_result.stderr or "").strip()[-512:]
        else:
            images: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for line in str(listed_result.stdout or "").splitlines()[:_MAX_IMAGES]:
                try:
                    listed = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(listed, dict):
                    continue
                image_id = str(listed.get("ID") or "").strip()
                if not image_id or image_id in seen_ids:
                    continue
                seen_ids.add(image_id)
                inspected = run(
                    ["/usr/bin/docker", "image", "inspect", image_id],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if inspected.returncode != 0:
                    continue
                try:
                    value = json.loads(str(inspected.stdout or ""))
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    images.append(_image_item(value[0], listed))
            base["status"] = "ok"
            base["images"] = sorted(images, key=lambda item: (str(item.get("image_ref") or ""), str(item.get("image_id") or "")))
    except Exception as exc:
        base["reason"] = "docker_image_inventory_probe_failed"
        base["error_type"] = type(exc).__name__
    base["inventory_sha256"] = "sha256:" + hashlib.sha256(canonical_json({"images": base["images"]})).hexdigest()
    return sign_result(base, private_key, str(key_id))
