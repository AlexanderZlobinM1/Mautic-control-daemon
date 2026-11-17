"""Resolve immutable pre-reload patch plans from the configured public catalog."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any
import urllib.parse
import uuid

from mcd_agent.mautic_patch_fact_binding import discover_execution_context, needs_facts
from mcd_agent.mautic_patch_resolution import canonical_json_sha256


REF_SCHEMA = "mcd-mautic-patch-catalog-ref-v1"
CATALOG_SCHEMA = "mcd-mautic-patch-catalog-projection-v1"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_FIELDS = {"schema", "url", "sha256", "required"}
_CATALOG_FIELDS = {"schema", "registry_commit", "registry_sha256", "selectors"}
_SELECTOR_FIELDS = {"trigger", "phase", "observed_majors", "install_types", "plan_template"}
_TEMPLATE_FIELDS = {"schema", "registry_commit", "registry_sha256", "patches", "payloads"}


class PublicPatchCatalogError(RuntimeError):
    """The configured public patch projection is unavailable or invalid."""


def _same_origin(base: str, target: str) -> bool:
    left = urllib.parse.urlparse(base)
    right = urllib.parse.urlparse(target)
    return (left.scheme.lower(), left.netloc.lower()) == (right.scheme.lower(), right.netloc.lower())


def _manifest_url(config: Any, major: int) -> str | None:
    base = str(getattr(config, "plugins_repo_base_url", "") or "").rstrip("/")
    template = str(getattr(config, "plugins_manifest_path_template", "") or "").strip()
    if not base or not template:
        return None
    return base + template.format(major=major)


def _validated_ref(manifest: dict[str, Any]) -> dict[str, Any] | None:
    raw = manifest.get("mautic_patch_catalog")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != _REF_FIELDS:
        raise PublicPatchCatalogError("public_patch_catalog_ref_fields_invalid")
    if raw.get("schema") != REF_SCHEMA:
        raise PublicPatchCatalogError("public_patch_catalog_ref_schema_invalid")
    if not isinstance(raw.get("url"), str) or not str(raw["url"]).strip():
        raise PublicPatchCatalogError("public_patch_catalog_ref_url_invalid")
    if not _SHA64_RE.fullmatch(str(raw.get("sha256", ""))):
        raise PublicPatchCatalogError("public_patch_catalog_ref_sha256_invalid")
    if not isinstance(raw.get("required"), bool):
        raise PublicPatchCatalogError("public_patch_catalog_ref_required_invalid")
    return dict(raw)


def _validated_catalog(value: dict[str, Any], *, expected_sha256: str) -> dict[str, Any]:
    if canonical_json_sha256(value) != expected_sha256:
        raise PublicPatchCatalogError("public_patch_catalog_sha256_mismatch")
    if set(value) != _CATALOG_FIELDS or value.get("schema") != CATALOG_SCHEMA:
        raise PublicPatchCatalogError("public_patch_catalog_fields_invalid")
    commit = str(value.get("registry_commit", ""))
    digest = str(value.get("registry_sha256", ""))
    if not _SHA40_RE.fullmatch(commit) or not _SHA64_RE.fullmatch(digest):
        raise PublicPatchCatalogError("public_patch_catalog_registry_invalid")
    selectors = value.get("selectors")
    if not isinstance(selectors, list):
        raise PublicPatchCatalogError("public_patch_catalog_selectors_invalid")
    for selector in selectors:
        if not isinstance(selector, dict) or set(selector) != _SELECTOR_FIELDS:
            raise PublicPatchCatalogError("public_patch_catalog_selector_fields_invalid")
        if not isinstance(selector.get("observed_majors"), list) or not selector["observed_majors"]:
            raise PublicPatchCatalogError("public_patch_catalog_selector_majors_invalid")
        if any(isinstance(item, bool) or not isinstance(item, int) or item not in range(1, 100)
               for item in selector["observed_majors"]):
            raise PublicPatchCatalogError("public_patch_catalog_selector_majors_invalid")
        if not isinstance(selector.get("install_types"), list) or not selector["install_types"]:
            raise PublicPatchCatalogError("public_patch_catalog_selector_install_types_invalid")
        template = selector.get("plan_template")
        if not isinstance(template, dict) or set(template) != _TEMPLATE_FIELDS:
            raise PublicPatchCatalogError("public_patch_catalog_plan_template_invalid")
        if (template.get("schema") != "mcd-mautic-patch-plan-v3"
                or template.get("registry_commit") != commit
                or template.get("registry_sha256") != digest
                or not isinstance(template.get("patches"), list)
                or not isinstance(template.get("payloads"), list)):
            raise PublicPatchCatalogError("public_patch_catalog_plan_template_invalid")
    return value


def resolve_public_pre_reload_plan(
    config: Any,
    install: Any,
    *,
    trigger: str,
    phase: str,
    operation: str,
    observed_version: str | None,
    observed_major: int,
    install_type: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return one locally bound v3 plan or an explicit optional-source noop."""
    manifest_url = _manifest_url(config, observed_major)
    if manifest_url is None:
        return {"status": "noop", "reason": "public_patch_catalog_not_configured"}

    # Reuse the plugin transport, fallback-IP behavior and configured source.
    from mcd_agent.plugins import _fetch_json, _plugins_fallback_ip

    fallback_ip = _plugins_fallback_ip(config)
    try:
        manifest = _fetch_json(
            manifest_url,
            getattr(config, "mcc_token", None),
            timeout_sec=12,
            fallback_ip=fallback_ip,
        )
    except Exception as exc:
        raise PublicPatchCatalogError("public_patch_manifest_unavailable") from exc
    if not isinstance(manifest, dict):
        raise PublicPatchCatalogError("public_patch_manifest_invalid")
    ref = _validated_ref(manifest)
    if ref is None:
        return {"status": "noop", "reason": "public_patch_catalog_not_configured"}

    catalog_url = urllib.parse.urljoin(manifest_url, str(ref["url"]))
    if not _same_origin(manifest_url, catalog_url):
        raise PublicPatchCatalogError("public_patch_catalog_cross_origin_forbidden")
    try:
        catalog = _fetch_json(
            catalog_url,
            getattr(config, "mcc_token", None),
            timeout_sec=12,
            fallback_ip=fallback_ip,
        )
    except Exception as exc:
        if ref["required"]:
            raise PublicPatchCatalogError("required_public_patch_catalog_unavailable") from exc
        return {"status": "noop", "reason": "optional_public_patch_catalog_unavailable"}
    if not isinstance(catalog, dict):
        raise PublicPatchCatalogError("public_patch_catalog_invalid")
    catalog = _validated_catalog(catalog, expected_sha256=str(ref["sha256"]))

    matches = [
        row for row in catalog["selectors"]
        if row.get("trigger") == trigger
        and row.get("phase") == phase
        and observed_major in row.get("observed_majors", [])
        and install_type in row.get("install_types", [])
    ]
    if len(matches) != 1:
        if ref["required"]:
            reason = "required_public_patch_projection_missing" if not matches else "required_public_patch_projection_ambiguous"
            raise PublicPatchCatalogError(reason)
        return {"status": "noop", "reason": "no_applicable_public_patch_projection"}

    template = deepcopy(matches[0]["plan_template"])
    plan = {
        **template,
        "source_version": observed_version,
        "target_version": observed_version,
        "install_type": install_type,
        "trigger": trigger,
        "phase": phase,
        "operation": operation,
        "run_id": run_id or uuid.uuid4().hex,
    }
    if needs_facts(plan):
        context = discover_execution_context(config, install)
        if context is None:
            raise PublicPatchCatalogError("public_patch_execution_context_unavailable")
        plan["execution_context"] = context
    return {
        "status": "selected",
        "reason": "public_patch_projection_selected",
        "host_id": "",
        "instance_uid": str(getattr(install, "instance_uid", "") or ""),
        "catalog_revision": str(catalog["registry_commit"]),
        "catalog_sha256": str(ref["sha256"]),
        "registry_commit": str(catalog["registry_commit"]),
        "registry_sha256": str(catalog["registry_sha256"]),
        "plan": plan,
        "plan_sha256": canonical_json_sha256(plan),
        "source": "public_catalog",
        "required": bool(ref["required"]),
        "catalog_url": catalog_url,
    }
