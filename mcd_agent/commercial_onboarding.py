from __future__ import annotations

from typing import Any, Callable, Mapping


ONBOARDING_STAGE_KINDS = (
    "preflight",
    "read_only_scan",
    "ensure_supported_base_packages",
    "resolve_compatibility_runtime",
    "provision_database_and_cache",
    "configure_web_and_vhost",
    "composer_install_current_mautic",
    "install_catalog_composer_plugins",
    "configure_certbot_vhost",
    "install_commercial_mcd_passive",
    "verify_onboarding_receipts",
)

ONBOARDING_RECEIPTS = {"mcd-onboarding-stage-receipt-v1", "mcd-onboarding-terminal-receipt-v1"}
FORBIDDEN_PRIMITIVES = {"arbitrary_shell", "arbitrary_sql", "delete_unowned_data", "delete_migration_history", "take_cron_ownership"}


def validate_onboarding_artifact(scenario: Mapping[str, Any]) -> None:
    security = scenario.get("security")
    rollback = scenario.get("rollback")
    if not isinstance(security, dict) or security.get("embedded_secrets") is not False or security.get("unknown_input_policy") != "reject":
        raise ValueError("commercial_onboarding_security_policy_invalid")
    if security.get("executor_boundary") != "MCD fixed onboarding primitives only":
        raise ValueError("commercial_onboarding_executor_boundary_invalid")
    if not isinstance(rollback, dict) or rollback.get("requires_checkpoint_receipt") is not True:
        raise ValueError("commercial_onboarding_rollback_policy_invalid")
    forbidden = set(rollback.get("forbidden") or [])
    if not FORBIDDEN_PRIMITIVES.issubset(forbidden):
        raise ValueError("commercial_onboarding_forbidden_primitives_incomplete")
    for stage in scenario.get("stages") or []:
        if not isinstance(stage, dict) or stage.get("receipt") not in ONBOARDING_RECEIPTS:
            raise ValueError("commercial_onboarding_stage_receipt_invalid")
        if not isinstance(stage.get("idempotency"), dict) or stage["idempotency"].get("replay") not in {"return_existing_receipt", "verify_and_continue"}:
            raise ValueError("commercial_onboarding_idempotency_invalid")


def build_onboarding_primitive_registry(
    implementations: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
) -> dict[str, Callable[[dict[str, Any]], Any]]:
    """Return the closed typed registry; no arbitrary shell/SQL is admitted."""
    supplied = dict(implementations or {})
    unknown = set(supplied) - set(ONBOARDING_STAGE_KINDS)
    if unknown:
        raise ValueError("commercial_onboarding_unknown_primitive")

    def make(kind: str) -> Callable[[dict[str, Any]], Any]:
        def run(envelope: dict[str, Any]) -> Any:
            stage = envelope.get("stage") if isinstance(envelope.get("stage"), dict) else {}
            context = envelope.get("context") if isinstance(envelope.get("context"), dict) else {}
            if str(stage.get("kind") or "") != kind:
                raise ValueError("commercial_onboarding_primitive_kind_mismatch")
            if context.get("cron_ownership") not in {None, "none"}:
                raise ValueError("commercial_onboarding_cron_ownership_forbidden")
            implementation = supplied.get(kind)
            if implementation is None:
                raise ValueError("commercial_onboarding_primitive_not_bound")
            return implementation(envelope)

        return run

    return {kind: make(kind) for kind in ONBOARDING_STAGE_KINDS}
