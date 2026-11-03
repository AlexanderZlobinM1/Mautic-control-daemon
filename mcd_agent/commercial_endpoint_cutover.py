"""Authenticated endpoint-only cutover for an existing DNS-bound Portal agent."""

from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime
import hashlib
import ipaddress
import json
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from mcd_agent.commercial_portal import (
    MAX_SAFE_INVENTORY_REVISION,
    CommercialPortalError,
    CommercialState,
    PortalClient,
    canonical_json,
    inventory_durable_hash,
    inventory_semantic_hash,
    portal_inventory_semantic_hash,
)
from mcd_agent.portal_identity import dns_identity, identity_coordinate


REQUEST_SCHEMA = "portal-mcd-dns-agent-cutover-request-v1"
RESPONSE_SCHEMA = "portal-mcd-dns-agent-cutover-response-v1"
RECEIPT_SCHEMA = "mcd-portal-endpoint-cutover-receipt-v1"
CONTRACT_VERSION = "1.0"
ENDPOINT_PATH = "dns-agent-cutovers"


class EndpointCutoverError(ValueError):
    """Bounded, secret-free endpoint cutover failure."""

    def __init__(
        self,
        code: str,
        *,
        cause_code: str = "",
        http_status: int | None = None,
        retryable: bool = False,
    ):
        self.code = str(code)
        self.cause_code = str(cause_code)
        self.http_status = int(http_status) if http_status is not None else None
        self.retryable = bool(retryable)
        super().__init__(self.code)

    def receipt_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"code": self.code}
        if self.cause_code:
            cause: dict[str, Any] = {"code": self.cause_code, "retryable": self.retryable}
            if self.http_status is not None:
                cause["http_status"] = self.http_status
            fields["cause"] = cause
        return fields


def _error(code: str) -> EndpointCutoverError:
    return EndpointCutoverError(str(code))


def _portal_error(code: str, cause: CommercialPortalError) -> EndpointCutoverError:
    return EndpointCutoverError(
        code,
        cause_code=cause.code,
        http_status=cause.http_status,
        retryable=cause.retryable,
    )


def _text(value: object) -> str:
    return str(value or "").strip()


def canonical_portal_api_base(value: object, *, public_target: bool = False) -> str:
    raw = _text(value).rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise _error("portal_endpoint_cutover_url_invalid") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise _error("portal_endpoint_cutover_https_required")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise _error("portal_endpoint_cutover_url_invalid")
    path = parsed.path.rstrip("/")
    if path in ("", "/"):
        path = "/api/v1/commercial"
    elif path != "/api/v1/commercial":
        raise _error("portal_endpoint_cutover_path_invalid")
    hostname = parsed.hostname.rstrip(".").lower()
    if public_target:
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise _error("portal_endpoint_cutover_public_target_required")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None:
            raise _error("portal_endpoint_cutover_public_target_required")
    netloc = hostname
    if ":" in hostname:
        netloc = f"[{hostname}]"
    if port is not None and port != 443:
        netloc += f":{port}"
    return urlunsplit(("https", netloc, path, "", ""))


def _cursor_map(state: CommercialState, installation_ids: Mapping[str, str]) -> dict[str, str]:
    raw = state.get("commercial_portal_cursors_v1")
    cursors = dict(raw) if isinstance(raw, Mapping) else {}
    legacy = state.get("commercial_portal_cursor")
    result: dict[str, str] = {}
    for installation_id in installation_ids.values():
        value = cursors.get(installation_id)
        if value in (None, "") and len(installation_ids) == 1:
            value = legacy
        text = _text(value)
        if not re.fullmatch(r"0|[1-9][0-9]*", text):
            raise _error("portal_endpoint_cutover_cursor_invalid")
        result[str(installation_id)] = text
    return result


def _pending_receipt_ids(state: CommercialState) -> list[str]:
    raw = state.get("commercial_self_hosted_pending_receipts")
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise _error("portal_endpoint_cutover_pending_receipts_invalid")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise _error("portal_endpoint_cutover_pending_receipts_invalid")
        identity = _text(
            item.get("receipt_id")
            or item.get("command_id")
            or item.get("job_id")
            or item.get("idempotency_key")
        )
        if not identity:
            raise _error("portal_endpoint_cutover_pending_receipts_invalid")
        result.append(identity)
    if len(set(result)) != len(result):
        raise _error("portal_endpoint_cutover_pending_receipts_invalid")
    return sorted(result)


def local_cutover_binding(state: CommercialState) -> dict[str, Any]:
    if state.get("identity_kind") != "dns" or state.get("host_fingerprint"):
        raise _error("portal_endpoint_cutover_dns_binding_required")
    try:
        coordinate = identity_coordinate(
            {
                "identity_kind": state.get("identity_kind"),
                "local_identity": state.get("local_identity"),
            }
        )
    except ValueError as exc:
        raise _error("portal_endpoint_cutover_identity_invalid") from exc
    values = {
        "account_id": _text(state.get("account_id")),
        "agent_id": _text(state.get("agent_id")),
        "commercial_key_id": _text(state.get("commercial_key_id")),
    }
    if not all(values.values()) or not _text(state.get("portal_agent_credential")):
        raise _error("portal_endpoint_cutover_binding_incomplete")
    raw_installations = state.get("installation_ids")
    if not isinstance(raw_installations, Mapping) or not raw_installations:
        raise _error("portal_endpoint_cutover_installations_invalid")
    try:
        installation_ids = {
            dns_identity(local): _text(installation)
            for local, installation in raw_installations.items()
            if _text(local) and _text(installation)
        }
    except ValueError as exc:
        raise _error("portal_endpoint_cutover_installations_invalid") from exc
    if len(installation_ids) != len(raw_installations) or len(set(installation_ids.values())) != len(installation_ids):
        raise _error("portal_endpoint_cutover_installations_invalid")
    if state.get("ownership") != "sales_snap_managed" and set(installation_ids) != {coordinate["local_identity"]}:
        raise _error("portal_endpoint_cutover_installations_invalid")
    cursors = _cursor_map(state, installation_ids)
    active = state.get("commercial_portal_connection_state") == "active"
    installations = sorted(
        [
            {
                "local_identity": local_identity,
                "installation_id": installation_id,
                "display_name": local_identity,
                "status": "active" if active else "passive",
                "connection": {
                    "status": "active" if active else "offline",
                    "channel_contract": CONTRACT_VERSION,
                    "cursor": int(cursors[installation_id]),
                    "next_command_cursor": int(cursors[installation_id]),
                    **_local_inventory_state(state, installation_id),
                },
            }
            for local_identity, installation_id in installation_ids.items()
        ],
        key=lambda item: item["installation_id"],
    )
    binding: dict[str, Any] = {
        **values,
        **coordinate,
        "agent_status": "active" if active else "passive",
        "installations": installations,
        "pending_receipt_ids": _pending_receipt_ids(state),
    }
    return binding


def _local_inventory_state(state: CommercialState, installation_id: str) -> dict[str, Any]:
    inventory_state = state.get("commercial_inventory_state_v1")
    records = inventory_state.get("installations") if isinstance(inventory_state, Mapping) else None
    record = records.get(installation_id) if isinstance(records, Mapping) else None
    snapshot = record.get("durable_snapshot") if isinstance(record, Mapping) else None
    revision = record.get("snapshot_revision") if isinstance(record, Mapping) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or revision > MAX_SAFE_INVENTORY_REVISION:
        raise _error("portal_endpoint_cutover_inventory_state_invalid")
    if not isinstance(snapshot, Mapping) or _text(snapshot.get("snapshot_revision")) != str(revision):
        raise _error("portal_endpoint_cutover_inventory_state_invalid")
    return {
        "last_inventory_revision": str(revision),
        "inventory_semantic_sha256": portal_inventory_semantic_hash(snapshot),
    }


def _request_identity(previous: str, target: str, binding: Mapping[str, Any]) -> tuple[str, str]:
    digest = hashlib.sha256(
        canonical_json(
            {
                "schema": REQUEST_SCHEMA,
                "contract_version": CONTRACT_VERSION,
                "previous_portal_api_base": previous,
                "target_portal_api_base": target,
                "binding": dict(binding),
            }
        )
    ).digest()
    token = base64.urlsafe_b64encode(digest[:18]).rstrip(b"=").decode("ascii")
    return "endpoint-cutover-" + token, "endpoint-cutover-" + hashlib.sha256(digest).hexdigest()


def build_cutover_request(previous: str, target: str, binding: Mapping[str, Any]) -> dict[str, Any]:
    request_id, idempotency_key = _request_identity(previous, target, binding)
    pre_state = dict(binding)
    return {
        "schema": REQUEST_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "previous_portal_api_base": previous,
        "target_portal_api_base": target,
        "pre_state": pre_state,
        "pre_state_sha256": "sha256:" + hashlib.sha256(canonical_json(pre_state)).hexdigest(),
    }


def validate_cutover_response(response: object, request_payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise _error("portal_endpoint_cutover_response_invalid")
    expected = {
        "contract_version": CONTRACT_VERSION,
        "request_id": request_payload["request_id"],
        "idempotency_key": request_payload["idempotency_key"],
        "previous_portal_api_base": request_payload["previous_portal_api_base"],
        "target_portal_api_base": request_payload["target_portal_api_base"],
        "account_id": request_payload["pre_state"]["account_id"],
        "commercial_key_id": request_payload["pre_state"]["commercial_key_id"],
        "agent_id": request_payload["pre_state"]["agent_id"],
        "installation_ids": sorted(str(item["installation_id"]) for item in request_payload["pre_state"]["installations"]),
        "pre_state_sha256": request_payload["pre_state_sha256"],
        "pending_receipt_count": len(request_payload["pre_state"]["pending_receipt_ids"]),
    }
    if response.get("schema") != RESPONSE_SCHEMA:
        raise _error("portal_endpoint_cutover_response_invalid")
    if any(response.get(key) != value for key, value in expected.items()):
        raise _error("portal_endpoint_cutover_response_mismatch")
    requested_cursors = {}
    for item in request_payload["pre_state"]["installations"]:
        if isinstance(item, Mapping) and isinstance(item.get("connection"), Mapping):
            requested_cursors[str(item.get("installation_id"))] = item["connection"]
    accepted_raw = response.get("cursor_continuity")
    accepted_cursors = {
        str(item.get("installation_id")): item
        for item in accepted_raw
        if isinstance(item, Mapping)
    } if isinstance(accepted_raw, list) else {}
    if set(requested_cursors) != set(accepted_cursors):
        raise _error("portal_endpoint_cutover_cursor_state_mismatch")
    for installation_id, requested in requested_cursors.items():
        accepted = accepted_cursors[installation_id]
        try:
            requested_cursor = int(str(requested.get("cursor")))
        except (TypeError, ValueError) as exc:
            raise _error("portal_endpoint_cutover_cursor_state_mismatch") from exc
        if (
            isinstance(accepted.get("cursor"), bool)
            or accepted.get("cursor") != requested_cursor
            or isinstance(accepted.get("next_command_cursor"), bool)
            or accepted.get("next_command_cursor") != requested_cursor
        ):
            raise _error("portal_endpoint_cutover_cursor_state_mismatch")
        if accepted.get("inventory_semantic_sha256") != requested.get("inventory_semantic_sha256"):
            raise _error("portal_endpoint_cutover_inventory_semantic_mismatch")
        try:
            before_revision = int(str(requested.get("last_inventory_revision")))
        except (TypeError, ValueError) as exc:
            raise _error("portal_endpoint_cutover_inventory_revision_invalid") from exc
        try:
            after_revision = int(str(accepted.get("last_inventory_revision")))
        except (TypeError, ValueError) as exc:
            raise _error("portal_endpoint_cutover_inventory_revision_invalid") from exc
        if (
            after_revision < before_revision
            or after_revision > MAX_SAFE_INVENTORY_REVISION
        ):
            raise _error("portal_endpoint_cutover_inventory_revision_invalid")
    if response.get("status") != "accepted" or response.get("reconciliation") not in {"validated", "reconciled"} or response.get("no_replay") is not True:
        raise _error("portal_endpoint_cutover_response_rejected")
    receipt_id = _text(response.get("receipt_id"))
    post_state = _text(response.get("post_state_sha256"))
    accepted_at = _text(response.get("accepted_at"))
    try:
        accepted_time = datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise _error("portal_endpoint_cutover_response_invalid") from None
    if accepted_time.tzinfo is None or not receipt_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", post_state):
        raise _error("portal_endpoint_cutover_response_invalid")
    return dict(response)


def _inventory_reconciliation(
    state: CommercialState,
    requested: Mapping[str, Any],
    accepted: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    before = state.get("commercial_inventory_state_v1")
    if not isinstance(before, Mapping) or not isinstance(before.get("installations"), Mapping):
        raise _error("portal_endpoint_cutover_inventory_state_invalid")
    after = copy.deepcopy(dict(before))
    after_records = after["installations"]
    accepted_rows = {
        str(item["installation_id"]): item
        for item in accepted["cursor_continuity"]
        if isinstance(item, Mapping) and item.get("installation_id")
    }
    reconciliations: list[dict[str, Any]] = []
    for installation in requested["pre_state"]["installations"]:
        installation_id = str(installation["installation_id"])
        requested_row = installation["connection"]
        accepted_row = accepted_rows[installation_id]
        record = after_records.get(installation_id)
        snapshot = record.get("durable_snapshot") if isinstance(record, Mapping) else None
        local_revision = int(str(requested_row["last_inventory_revision"]))
        server_revision = int(str(accepted_row["last_inventory_revision"]))
        semantic_hash = requested_row["inventory_semantic_sha256"]
        local_semantic_hash = inventory_semantic_hash(snapshot) if isinstance(snapshot, Mapping) else ""
        if (
            not isinstance(record, Mapping)
            or record.get("snapshot_revision") != local_revision
            or not isinstance(snapshot, Mapping)
            or portal_inventory_semantic_hash(snapshot) != semantic_hash
        ):
            raise _error("portal_endpoint_cutover_inventory_state_conflict")
        next_record = dict(record)
        next_snapshot = dict(snapshot)
        next_snapshot["snapshot_revision"] = str(server_revision)
        if portal_inventory_semantic_hash(next_snapshot) != semantic_hash:
            raise _error("portal_endpoint_cutover_inventory_semantic_mismatch")
        next_durable_hash = inventory_durable_hash(next_snapshot)
        next_record.update(
            snapshot_revision=server_revision,
            durable_hash=next_durable_hash,
            durable_snapshot=next_snapshot,
            last_published={"durable_hash": next_durable_hash, "snapshot_revision": server_revision},
            last_acknowledged={"durable_hash": next_durable_hash, "snapshot_revision": server_revision},
        )
        after_records[installation_id] = next_record
        reconciliations.append(
            {
                "installation_id": installation_id,
                "inventory_revision_before": local_revision,
                "inventory_revision_after": server_revision,
                "inventory_semantic_sha256": semantic_hash,
                "local_inventory_semantic_sha256": local_semantic_hash,
                "rebased": server_revision != local_revision,
            }
        )
    return dict(before), after, reconciliations


def prepare_cutover_request(
    state: CommercialState,
    portal_url: str,
    *,
    expected_agent_id: str = "",
    expected_local_identity: str = "",
    expected_current_url: str = "",
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    previous = canonical_portal_api_base(state.get("portal_api_base"))
    target = canonical_portal_api_base(portal_url, public_target=True)
    binding = local_cutover_binding(state)
    if expected_agent_id and binding["agent_id"] != expected_agent_id:
        raise _error("portal_endpoint_cutover_agent_mismatch")
    if expected_local_identity and binding["local_identity"] != expected_local_identity:
        raise _error("portal_endpoint_cutover_identity_mismatch")
    if expected_current_url and previous != canonical_portal_api_base(expected_current_url):
        raise _error("portal_endpoint_cutover_current_endpoint_mismatch")
    return previous, target, binding, build_cutover_request(previous, target, binding)


class _TargetState:
    def __init__(self, state: CommercialState, target: str):
        self.state = state
        self.target = target

    def get(self, key: str, default: Any = None) -> Any:
        if key == "portal_api_base":
            return self.target
        value = self.state.get(key)
        return default if value is None else value


Transport = Callable[[str, str, Mapping[str, Any], str, int], Mapping[str, Any]]


def _network_transport(
    target: str,
    path: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    timeout_sec: int,
    *,
    state: CommercialState,
) -> Mapping[str, Any]:
    client = PortalClient(_TargetState(state, target), timeout_sec=timeout_sec)  # type: ignore[arg-type]
    return client._request("POST", path, dict(payload), idempotency_key=idempotency_key)


def cutover_portal_endpoint(
    state: CommercialState,
    portal_url: str,
    *,
    expected_agent_id: str = "",
    expected_local_identity: str = "",
    expected_current_url: str = "",
    timeout_sec: int = 15,
    transport: Transport | None = None,
) -> dict[str, Any]:
    previous, target, binding, payload = prepare_cutover_request(
        state,
        portal_url,
        expected_agent_id=expected_agent_id,
        expected_local_identity=expected_local_identity,
        expected_current_url=expected_current_url,
    )
    path = ENDPOINT_PATH
    invoke = transport or (
        lambda base, request_path, body, key, timeout: _network_transport(
            base,
            request_path,
            body,
            key,
            timeout,
            state=state,
        )
    )
    mutable_keys = ("portal_api_base", "commercial_inventory_state_v1", "commercial_inventory_revision")
    continuity_before = state.continuity_digest(exclude_meta_keys=mutable_keys)
    full_state_before = state.continuity_digest()
    try:
        first = invoke(target, path, payload, str(payload["idempotency_key"]), timeout_sec)
        validated = validate_cutover_response(first, payload)
    except EndpointCutoverError:
        raise
    except CommercialPortalError as exc:
        raise _portal_error("portal_endpoint_cutover_prepare_failed", exc) from exc
    except Exception as exc:
        raise _error("portal_endpoint_cutover_prepare_failed") from exc
    inventory_before, inventory_after, reconciliations = _inventory_reconciliation(state, payload, validated)
    changed = previous != target
    legacy_present = state.contains("commercial_inventory_revision")
    legacy_before = state.get("commercial_inventory_revision")
    revision_floor = max(item["inventory_revision_after"] for item in reconciliations)
    try:
        legacy_after = max(int(legacy_before or 0), revision_floor)
    except (TypeError, ValueError) as exc:
        raise _error("portal_endpoint_cutover_inventory_state_invalid") from exc
    expected_values = {
        "portal_api_base": previous,
        "commercial_inventory_state_v1": inventory_before,
    }
    next_values = {
        "portal_api_base": target,
        "commercial_inventory_state_v1": inventory_after,
    }
    if legacy_present or legacy_after:
        expected_values["commercial_inventory_revision"] = legacy_before
        next_values["commercial_inventory_revision"] = legacy_after
    if not state.compare_and_swap_many(expected_values, next_values):
        raise _error("portal_endpoint_cutover_state_conflict")
    try:
        second = invoke(target, path, payload, str(payload["idempotency_key"]), timeout_sec)
        second_validated = validate_cutover_response(second, payload)
        if second_validated.get("cursor_continuity") != validated.get("cursor_continuity"):
            raise _error("portal_endpoint_cutover_postcheck_mismatch")
        continuity_after = state.continuity_digest(exclude_meta_keys=mutable_keys)
        if continuity_after != continuity_before:
            raise _error("portal_endpoint_cutover_state_changed")
    except Exception as exc:
        rollback_expected = dict(next_values)
        rollback_values = {
            "portal_api_base": previous,
            "commercial_inventory_state_v1": inventory_before,
        }
        rollback_delete: tuple[str, ...] = ()
        if legacy_present:
            rollback_values["commercial_inventory_revision"] = legacy_before
        elif "commercial_inventory_revision" in next_values:
            rollback_delete = ("commercial_inventory_revision",)
        if not state.compare_and_swap_many(
            rollback_expected,
            rollback_values,
            delete_keys=rollback_delete,
        ):
            raise _error("portal_endpoint_cutover_rollback_conflict") from exc
        if state.continuity_digest() != full_state_before:
            raise _error("portal_endpoint_cutover_rollback_state_changed") from exc
        if isinstance(exc, EndpointCutoverError):
            raise
        if isinstance(exc, CommercialPortalError):
            raise _portal_error("portal_endpoint_cutover_postcheck_failed", exc) from exc
        raise _error("portal_endpoint_cutover_postcheck_failed") from exc
    return {
        "schema": RECEIPT_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "status": "succeeded",
        "idempotent": not changed and not any(item["rebased"] for item in reconciliations),
        "request_id": payload["request_id"],
        "receipt_id": validated["receipt_id"],
        "previous_portal_api_base": previous,
        "portal_api_base": target,
        "binding": binding,
        "no_replay": True,
        "continuity_digest": continuity_after,
        "inventory_reconciliation": reconciliations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut over one existing DNS-bound MCD agent to a verified Portal endpoint")
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--portal-url", required=True)
    parser.add_argument("--expected-agent-id", default="")
    parser.add_argument("--expected-local-identity", default="")
    parser.add_argument("--expected-current-url", default="")
    parser.add_argument("--timeout-sec", type=int, default=15)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        state = CommercialState(args.state_db)
        if args.prepare_only:
            _previous, _target, _binding, request_payload = prepare_cutover_request(
                state,
                args.portal_url,
                expected_agent_id=args.expected_agent_id,
                expected_local_identity=args.expected_local_identity,
                expected_current_url=args.expected_current_url,
            )
            receipt = {
                "schema": "mcd-portal-endpoint-cutover-preflight-v1",
                "contract_version": CONTRACT_VERSION,
                "status": "prepared",
                "request": request_payload,
            }
        else:
            receipt = cutover_portal_endpoint(
                state,
                args.portal_url,
                expected_agent_id=args.expected_agent_id,
                expected_local_identity=args.expected_local_identity,
                expected_current_url=args.expected_current_url,
                timeout_sec=max(1, min(60, args.timeout_sec)),
            )
    except EndpointCutoverError as exc:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "status": "failed",
            **exc.receipt_fields(),
        }
        print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
        return 1
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
