from __future__ import annotations

import hashlib
from typing import Any, Mapping

from mcd_agent.commercial_portal import canonical_json
from mcd_agent.commercial_onboarding import FORBIDDEN_PRIMITIVES, ONBOARDING_RECEIPTS, PRIMITIVE_VERSIONS, ONBOARDING_STAGE_KINDS


SCHEMA = "mcd-capability-manifest-v1"


def build_capability_manifest(*, agent_version: str, source_commit: str = "", runtime_archive_sha256: str = "") -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "contract_version": "1.0",
        "agent_version": str(agent_version),
        "capabilities": ["portal_scenario_execution_v1", "mcd_passive_activation_v1"],
        "primitives": [{"id": kind, "version": PRIMITIVE_VERSIONS[kind]} for kind in ONBOARDING_STAGE_KINDS],
        "receipts": {"stage": sorted(ONBOARDING_RECEIPTS), "terminal": "mcd-onboarding-terminal-receipt-v1", "preflight": "portal-self-hosted-preflight-receipt-v1"},
        "requirements": {"idempotency": "required", "replay": "return_existing_receipt", "rollback": "checkpoint_receipt_required", "portal_signature": "ed25519", "cron_ownership": "none"},
        "platform": {"os_ids": ["external_portal_catalog"], "architectures": ["amd64", "arm64"], "target_kinds": ["new_server", "existing_server"]},
        "forbidden_operations": sorted(FORBIDDEN_PRIMITIVES),
        "provenance": {"source_commit": str(source_commit), "runtime_archive_sha256": str(runtime_archive_sha256)},
    }
    manifest["manifest_sha256"] = "sha256:" + hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest
