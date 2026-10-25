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
