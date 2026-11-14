"""Independent instance discovery and immutable typed-fact admission receipts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcd_agent.mautic_patch_facts import PatchFactsError, ReadOnlyObserver, digest


def needs_facts(plan: dict[str, Any]) -> bool:
    records = plan.get("patches")
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise PatchFactsError("fact_records_invalid")
    return any(record.get("preconditions") or record.get("rollback_preconditions") for record in records)


def _select_instance(installs, selected_root):
    from mcd_agent.mautic_patch_stage import application_root
    requested = application_root(selected_root)
    matches = []
    for install in installs:
        try:
            candidate = Path(install.root).resolve()
        except OSError:
            continue
        # Inspect a layout only after proving it can own the selected root.
        # A legacy/malformed sibling is not a selected-instance dependency.
        if candidate != requested and not (requested.name in {"docroot", "public"} and candidate == requested.parent):
            continue
        if application_root(candidate) == requested:
            matches.append(install)
    if len(matches) != 1:
        raise PatchFactsError("fact_independent_instance_ambiguous")
    return matches[0], requested


def validate_context(plan: dict[str, Any]) -> None:
    from mcd_agent.mautic_patch_facts import _table, validate_predicates
    needed = needs_facts(plan)
    if not needed:
        if "execution_context" in plan:
            raise PatchFactsError("fact_context_without_predicates")
        return
    context = plan.get("execution_context")
    if not isinstance(context, dict) or set(context) != {"instance_uid", "application_root", "table_prefix"}:
        raise PatchFactsError("fact_execution_context_required")
    if (not isinstance(context["instance_uid"], str) or not context["instance_uid"]
            or not isinstance(context["application_root"], str)
            or not Path(context["application_root"]).is_absolute()
            or str(Path(context["application_root"])) != context["application_root"]
            or ".." in Path(context["application_root"]).parts):
        raise PatchFactsError("fact_execution_context_invalid")
    _table(context["table_prefix"], "binding_probe")
    for record in plan["patches"]:
        for field in ("preconditions", "rollback_preconditions"):
            if field in record:
                validate_predicates(record[field])


def require_receipt(accepted: dict[str, Any], current: dict[str, Any]) -> None:
    if (not isinstance(accepted, dict) or accepted.get("observations_sha256") != digest(accepted.get("observation_binding"))
            or accepted.get("observation_binding") != current.get("observation_binding")
            or current.get("observations_sha256") != digest(current.get("observation_binding"))):
        raise PatchFactsError("fact_admission_or_binding_drift")


def discover_execution_context(config: Any, selected: Any) -> dict[str, str] | None:
    """Supply a proven resolve context, never a discovery default or secret."""
    from mcd_agent.discovery import discover_mautic
    from mcd_agent.localphp import parse_local_php
    from mcd_agent.mautic_patch_facts import _IDENT
    from mcd_agent.instance_uid import instance_uid_matches
    root = getattr(selected, "root", None)
    if not root or not hasattr(config, "discovery_roots"):
        return None
    try:
        installs = discover_mautic(config.discovery_roots, config.exclude_path_contains,
                                   config.supported_mautic_majors, config.custom_instances)
        install, app = _select_instance(installs, root)
        db = install.db
        if (not instance_uid_matches(selected.instance_uid, install.instance_uid,
                                     getattr(config, "mcc_host_name", ""))
                or db is None or not install.local_php_path):
            return None
        local = parse_local_php(install.local_php_path)
        prefix = local.get("db_table_prefix")
        if (not isinstance(prefix, str) or (prefix and not _IDENT.fullmatch(prefix))
                or prefix != db.table_prefix or local.get("db_name") != db.name):
            return None
        return {"instance_uid": selected.instance_uid, "application_root": str(app), "table_prefix": prefix}
    except (PatchFactsError, OSError, ValueError):
        return None


def bound_provider(config: Any, selected_root: str):
    """Rediscover the selected root on each observation; never trust plan DB data."""
    admitted_connection = None
    def observe(plan: dict[str, Any], operation: str, phase: str) -> dict[str, Any]:
        nonlocal admitted_connection
        import pymysql
        from pymysql.cursors import DictCursor
        from mcd_agent.discovery import discover_mautic
        from mcd_agent.localphp import parse_local_php
        from mcd_agent.mautic_patch_stage import application_root
        from mcd_agent.instance_uid import instance_uid_matches
        validate_context(plan)
        installs = discover_mautic(config.discovery_roots, config.exclude_path_contains,
                                   config.supported_mautic_majors, config.custom_instances)
        install, requested = _select_instance(installs, selected_root)
        context = plan["execution_context"]
        db = install.db
        if (not instance_uid_matches(context["instance_uid"], install.instance_uid,
                                     getattr(config, "mcc_host_name", ""))
                or str(requested) != context["application_root"]
                or db is None or db.table_prefix != context["table_prefix"] or not install.local_php_path):
            raise PatchFactsError("fact_independent_instance_binding_mismatch")
        # Prefix omission is not proof of an empty prefix. Do not use discovery's
        # historical default to fabricate a database binding for a typed plan.
        local = parse_local_php(install.local_php_path)
        if local.get("db_table_prefix") != db.table_prefix or local.get("db_name") != db.name:
            raise PatchFactsError("fact_local_database_binding_unknown")
        predicates: list[dict[str, Any]] = []
        indexes: dict[str, int] = {}
        record_indexes: dict[str, dict[str, list[int]]] = {}
        for record in plan["patches"]:
            mapping: dict[str, list[int]] = {}
            for field in ("preconditions", "rollback_preconditions"):
                mapping[field] = []
                for predicate in record.get(field, []):
                    key = digest(predicate)
                    if key not in indexes:
                        indexes[key] = len(predicates)
                        predicates.append(predicate)
                    mapping[field].append(indexes[key])
            record_indexes[record["id"]] = mapping
        endpoint = {"driver": "mysql_protocol", "host": db.host, "port": db.port, "database": db.name}
        connection_binding = digest({"context": context, "endpoint": endpoint})
        if admitted_connection is not None and connection_binding != admitted_connection:
            raise PatchFactsError("fact_local_connection_binding_drift")
        try:
            connection = pymysql.connect(host=db.host, port=db.port, database=db.name, user=db.user,
                                         password=db.password, charset="utf8mb4", cursorclass=DictCursor,
                                         autocommit=True, connect_timeout=5, read_timeout=10, write_timeout=10)
            try:
                observation = ReadOnlyObserver(connection, db.table_prefix).observe(predicates)
            finally:
                connection.close()
        except PatchFactsError:
            raise
        except Exception as exc:
            raise PatchFactsError("fact_selected_database_unavailable") from exc
        if observation["database_identity"]["database_name"] != db.name:
            raise PatchFactsError("fact_actual_database_binding_mismatch")
        admitted_connection = connection_binding
        binding = {"schema": "mcd-mautic-patch-facts-admission-v1", "execution_context": context,
                   "database_binding_sha256": digest(endpoint), "plan_sha256": digest(dict(plan, operation="apply")),
                   "run_id": plan["run_id"], "source_version": plan["source_version"], "target_version": plan["target_version"],
                   "trigger": plan["trigger"], "plan_phase": plan["phase"], "observation": observation}
        matches_by_record = {patch_id: {field: all(observation["facts"][index]["matched"] for index in indexes)
                                      for field, indexes in mapping.items()}
                             for patch_id, mapping in record_indexes.items()}
        receipt = {"schema": "mcd-mautic-patch-facts-admission-v1", "observation_binding": binding,
                   "observations_sha256": digest(binding), "records": matches_by_record}
        return dict(receipt, invocation={"operation": operation, "phase": phase},
                    invocation_sha256=digest({"receipt": receipt, "operation": operation, "phase": phase}))
    return observe
