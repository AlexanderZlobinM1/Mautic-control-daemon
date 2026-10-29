#!/usr/bin/env python3
"""Build a detached manifest for the separately transported MCD bridge."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


SCHEMA = "mcd-runtime-bridge-package-v1"
CONTRACT_VERSION = "1.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(value: str, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return normalized


def build_manifest(
    bridge: Path,
    *,
    version: str,
    source_commit: str,
    artifact_name: str,
    artifact_url: str,
    runtime_archive_sha256: str,
    runtime_manifest_sha256: str,
    qa_sidecar_sha256: str,
) -> dict[str, object]:
    bridge = bridge.resolve()
    if not bridge.is_file():
        raise ValueError(f"bridge file not found: {bridge}")
    version = str(version or "").strip()
    if not version:
        raise ValueError("version is required")
    source_commit = str(source_commit or "").strip().lower()
    if not COMMIT_RE.fullmatch(source_commit):
        raise ValueError("source_commit must be a 40-character lowercase commit")
    artifact_name = str(artifact_name or "").strip()
    if not re.fullmatch(rf"mcd-runtime-{re.escape(version)}\.bridge(?:-r[1-9][0-9]*)?\.py", artifact_name):
        raise ValueError("artifact_name must match the release version and optional bridge revision")
    artifact_url = str(artifact_url or "").strip()
    if not artifact_url.startswith("https://"):
        raise ValueError("artifact_url must use HTTPS")
    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "version": version,
        "artifact": artifact_name,
        "artifact_url": artifact_url,
        "entrypoint": "mcd_runtime_bootstrap.py",
        "artifact_sha256": sha256(bridge),
        "source_commit": source_commit,
        "runtime_release": {
            "archive_sha256": _sha(runtime_archive_sha256, "runtime_archive_sha256"),
            "manifest_sha256": _sha(runtime_manifest_sha256, "runtime_manifest_sha256"),
            "qa_sidecar_sha256": _sha(qa_sidecar_sha256, "qa_sidecar_sha256"),
        },
        "transport": "https_sha256",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--runtime-archive-sha256", required=True)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--qa-sidecar-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = build_manifest(
        args.bridge,
        version=args.version,
        source_commit=args.source_commit,
        artifact_name=args.artifact_name,
        artifact_url=args.artifact_url,
        runtime_archive_sha256=args.runtime_archive_sha256,
        runtime_manifest_sha256=args.runtime_manifest_sha256,
        qa_sidecar_sha256=args.qa_sidecar_sha256,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    print(json.dumps(manifest, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
