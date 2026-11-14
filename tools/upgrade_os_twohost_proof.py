#!/usr/bin/env python3
"""Emit a canonical, non-mutating Upgrade OS bound-runtime proof.

The caller prepares one isolated cache containing an immutable manifest,
profile and bundle.  Every operation runs with ``dry_run=true`` through the
real MCD configured-operation resolver and subprocess framing path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcd_agent.upgrade_os_external import run_upgrade_os_operation


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower()
            in {
                "argv",
                "command_line",
                "raw_stdout",
                "raw_stderr",
                "target_db_credentials_json",
            }
            or _forbidden(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_forbidden(child) for child in value)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--profile-file", required=True)
    parser.add_argument("--artifact-file", required=True)
    args = parser.parse_args()

    cache = Path(args.cache_dir).resolve()
    profile = Path(args.profile_file).resolve()
    artifact = Path(args.artifact_file).resolve()
    manifest = cache / "manifest.json"
    proof_root = cache.parent / "proof-root"
    proof_root.mkdir(parents=True, exist_ok=True)

    class Config:
        custom_cache_dir = str(cache)
        custom_run_mode_default = "direct"
        custom_logs_keep_days = 7
        custom_logs_max_files = 10
        custom_downloads_keep_days = 7
        custom_downloads_max_entries = 32

    cases = [
        (
            "apt",
            "apt_apply",
            {
                "package_role": "node_prerequisite",
                "package_state": "mixed",
                "force_repo_rescan": "false",
                "dry_run": "true",
            },
        ),
        (
            "php",
            "php_runtime_transition",
            {
                "operation_id": "proof-php",
                "action": "apply",
                "target_php_version": "8.4",
                "previous_php_version": "8.3",
                "dry_run": "true",
            },
        ),
        (
            "composer",
            "composer_host_prepare",
            {
                "operation_id": "proof-composer",
                "working_root": str(proof_root),
                "dry_run": "true",
            },
        ),
        (
            "filesystem",
            "upgrade_tree_transition",
            {
                "operation_id": "proof-tree",
                "instance_root": str(proof_root / "instance"),
                "prepared_root": "",
                "backup_id": "proof-backup",
                "action": "backup",
                "dry_run": "true",
            },
        ),
        (
            "nginx",
            "nginx_service_transition",
            {
                "operation_id": "proof-nginx",
                "target_php_version": "8.4",
                "action": "test",
                "dry_run": "true",
            },
        ),
        (
            "certificate",
            "certificate_material",
            {
                "operation_id": "proof-certificate",
                "domain": "example.invalid",
                "action": "issue_http01",
                "webroot": str(proof_root),
                "email": "",
                "certificate_ref": "",
                "dry_run": "true",
            },
        ),
        (
            "database",
            "database_prepare_import",
            {
                "operation_id": "proof-database",
                "engine": "mariadb",
                "database": "mautic",
                "database_user": "mautic",
                "action": "provision",
                "host": "127.0.0.1",
                "port": "3306",
                "unix_socket": "",
                "dump_ref": "",
                "target_db_credentials_json": "{}",
                "dry_run": "true",
            },
        ),
        (
            "service",
            "upgrade_service_transition",
            {
                "operation_id": "proof-service",
                "target_php_version": "8.4",
                "action": "quiesce",
                "owned_stop": "false",
                "dry_run": "true",
            },
        ),
    ]

    operations: list[dict[str, Any]] = []
    for component, operation, operation_input in cases:
        result = run_upgrade_os_operation(
            Config(),
            component=component,
            operation=operation,
            operation_input=operation_input,
        )
        if (
            not isinstance(result, dict)
            or result.get("status") != "planned"
            or result.get("mutation_started") is not False
            or _forbidden(result)
        ):
            raise RuntimeError(f"upgrade_os_twohost_proof_failed:{operation}")
        operations.append(
            {
                "component": component,
                "operation": operation,
                "receipt": result,
            }
        )

    receipt = {
        "schema": "mcd-upgrade-os-twohost-bound-proof-v1",
        "status": "passed",
        "mutation_started": False,
        "adapter_sha256": _sha256(
            Path(__import__("mcd_agent.upgrade_os_external", fromlist=["x"]).__file__)
        ),
        "manifest_sha256": _sha256(manifest),
        "profile_sha256": _sha256(profile),
        "artifact_sha256": _sha256(artifact),
        "operation_count": len(operations),
        "operations": operations,
    }
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
