from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


SCENARIO_SCHEMA = "mcc-bootstrap-precheck-scenario-v1"
RECEIPT_SCHEMA = "mcd-bootstrap-precheck-receipt-v1"
CONTRACT_VERSION = "1.0"
MAX_TTL_SECONDS = 900
CHECKS = {
    "ssh_auth",
    "privilege",
    "host_fingerprint",
    "os_support",
    "architecture",
    "resources",
    "ports",
    "package_manager",
    "egress",
    "time_sync",
    "existing_stack",
    "dns_exact_server",
}
STATUSES = {"PASS", "WARN", "FAIL"}


def validate_precheck_scenario(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("schema") != SCENARIO_SCHEMA or value.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("bootstrap_precheck_schema_invalid")
    checks = value.get("checks")
    if not isinstance(checks, list) or not checks or any(str(item) not in CHECKS for item in checks):
        raise ValueError("bootstrap_precheck_checks_invalid")
    ttl = int(value.get("ttl_seconds") or 0)
    if ttl < 1 or ttl > MAX_TTL_SECONDS:
        raise ValueError("bootstrap_precheck_ttl_invalid")
    if value.get("mutates_host") is not False or value.get("persists_secrets") is not False or value.get("installs_packages") is not False:
        raise ValueError("bootstrap_precheck_mutation_policy_invalid")
    dns = value.get("dns_proof")
    if not isinstance(dns, dict) or not dns.get("fqdn") or not dns.get("nonce") or dns.get("cleanup_required") is not True:
        raise ValueError("bootstrap_precheck_dns_proof_invalid")
    return {"schema": SCENARIO_SCHEMA, "contract_version": CONTRACT_VERSION, "ttl_seconds": ttl, "checks": list(checks), "dns_proof": {"fqdn": str(dns["fqdn"]), "nonce": str(dns["nonce"]), "cleanup_required": True}}


def validate_precheck_receipt(receipt: Mapping[str, Any], *, input_fingerprint: str, now: datetime | None = None) -> dict[str, Any]:
    portal_schema = receipt.get("schema") == "portal-self-hosted-preflight-receipt-v1"
    if receipt.get("schema") not in {RECEIPT_SCHEMA, "portal-self-hosted-preflight-receipt-v1"} or receipt.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("bootstrap_precheck_receipt_schema_invalid")
    bound_fingerprint = receipt.get("tuple_key") if portal_schema else receipt.get("input_fingerprint")
    if bound_fingerprint != input_fingerprint or not str(receipt.get("host_fingerprint") or "").startswith("sha256:"):
        raise ValueError("bootstrap_precheck_receipt_binding_invalid")
    observed = str(receipt.get("observed_at") or "")
    expires = str(receipt.get("expires_at") or "")
    try:
        observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("bootstrap_precheck_receipt_time_invalid") from exc
    if observed_at.tzinfo is None or expires_at.tzinfo is None or expires_at <= observed_at:
        raise ValueError("bootstrap_precheck_receipt_time_invalid")
    current = now or datetime.now(timezone.utc)
    if current.astimezone(timezone.utc) >= expires_at:
        raise ValueError("bootstrap_precheck_receipt_expired")
    results = receipt.get("checks") if portal_schema else receipt.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("bootstrap_precheck_receipt_results_invalid")
    for result in results:
        if not isinstance(result, dict) or result.get("status") not in STATUSES or not (result.get("code") or result.get("id")) or not (result.get("remediation") or result.get("reason")):
            raise ValueError("bootstrap_precheck_result_invalid")
        if any(secret in str(result).lower() for secret in ("password", "private_key", "credential", "token")):
            raise ValueError("bootstrap_precheck_secret_disclosure")
    cleanup = receipt.get("cleanup")
    cleanup_ok = cleanup == "clean" if portal_schema else isinstance(cleanup, dict) and cleanup.get("status") == "PASS" and cleanup.get("temporary_responder_removed") is True
    if not cleanup_ok:
        raise ValueError("bootstrap_precheck_cleanup_incomplete")
    return {"schema": str(receipt["schema"]), "contract_version": CONTRACT_VERSION, "input_fingerprint": str(input_fingerprint), "host_fingerprint": str(receipt["host_fingerprint"]), "status": str(receipt.get("status") or ("PASS" if receipt.get("ready") else "FAIL")), "result_count": len(results), "cleanup": {"status": "PASS", "temporary_responder_removed": True}}
