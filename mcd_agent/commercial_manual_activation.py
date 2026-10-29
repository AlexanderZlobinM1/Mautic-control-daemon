"""Interactive, secret-safe fallback for an existing Portal commercial key.

This path is intentionally narrower than activation-code redemption. The
operator pastes an already-issued reusable commercial key at a hidden prompt;
MCD stores it in its existing mode-0600 commercial state, registers the
installation as passive, and waits for an explicit local ``activate``. The
same key can then be reactivated after an explicit ``disconnect``.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from urllib.parse import urlparse

from mcd_agent import __version__
from mcd_agent.commercial_portal import CommercialState, PortalClient, _host_fingerprint, canonical_json


MANUAL_KEY_SCHEMA = "mcd-manual-commercial-key-v1"
PENDING_REGISTRATION_SCHEMA = "mcd-commercial-pending-activation-v1"
RECEIPT_SCHEMA = "mcd-commercial-manual-activation-receipt-v1"


class ManualActivationError(ValueError):
    """Bounded, secret-free manual activation error."""


def _error(code: str) -> ManualActivationError:
    return ManualActivationError(str(code))


def _text(value: object) -> str:
    return str(value or "").strip()


def _require_https(value: str) -> str:
    url = _text(value).rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise _error("manual_activation_https_required")
    return url


def _atomic_put(state: CommercialState, values: Mapping[str, Any]) -> None:
    with sqlite3.connect(state.path, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        for key, value in values.items():
            db.execute(
                "INSERT INTO commercial_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), json.dumps(value, ensure_ascii=True, sort_keys=True)),
            )


def _registration_fingerprint(values: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(values))).hexdigest()


def _public_receipt(state: CommercialState, *, status: str, idempotent: bool = False, reason: str = "") -> dict[str, Any]:
    pending = state.get("commercial_pending_activation")
    pending = dict(pending) if isinstance(pending, Mapping) else {}
    payload: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "contract_version": "1.0",
        "status": status,
        "idempotent": bool(idempotent),
        "connection_state": _text(state.get("commercial_portal_connection_state")) or "unconfigured",
        "agent_id": _text(state.get("agent_id")) or None,
        "commercial_key_id": _text(state.get("commercial_key_id")) or None,
        "installation_ids": state.get("installation_ids") if isinstance(state.get("installation_ids"), Mapping) else {},
        "pending_registration_id": _text(pending.get("registration_id")) or None,
    }
    if reason:
        payload["reason"] = reason
    return payload


def stage_manual_key(
    state: CommercialState,
    *,
    portal_url: str,
    agent_id: str,
    commercial_key_id: str,
    commercial_key: str,
    local_identity: str,
    display_name: str,
    mautic_version: str,
    application_root_fingerprint: str,
    timeout_sec: int = 35,
) -> dict[str, Any]:
    """Store and passively register one MCD connection using an existing key.

    The same commercial key may be registered by other independent MCD state
    stores; this state only protects the local connection from rebinding to a
    different key. The interactive CLI reads the key with ``getpass`` and
    never echoes or prints it.
    """
    base_url = _require_https(portal_url)
    agent = _text(agent_id)
    key_id = _text(commercial_key_id)
    key = _text(commercial_key)
    identity = _text(local_identity)
    if not agent or not key_id or not key or not identity or not _text(display_name) or not _text(mautic_version):
        raise _error("manual_activation_binding_incomplete")
    if any(char in key for char in "\r\n"):
        raise _error("manual_activation_key_invalid")
    root_fingerprint = _text(application_root_fingerprint)
    if not root_fingerprint.startswith("sha256:"):
        raise _error("manual_activation_root_fingerprint_invalid")
    existing_agent = _text(state.get("agent_id"))
    if existing_agent and existing_agent != agent:
        raise _error("manual_activation_agent_mismatch")
    existing_key_id = _text(state.get("commercial_key_id"))
    if existing_key_id and existing_key_id != key_id:
        raise _error("manual_activation_key_id_mismatch")

    request_values = {
        "schema": MANUAL_KEY_SCHEMA,
        "contract_version": "1.0",
        "portal_api_base": base_url.rstrip("/") + "/api/v1/commercial",
        "agent_id": agent,
        "commercial_key_id": key_id,
        "host_fingerprint": _host_fingerprint(),
        "local_identity": identity,
        "display_name": _text(display_name),
        "mautic_version": _text(mautic_version),
        "application_root_fingerprint": root_fingerprint,
        "activation_state": "passive",
    }
    fingerprint = _registration_fingerprint(request_values)
    previous = state.get("commercial_pending_activation")
    if (
        isinstance(previous, Mapping)
        and _text(previous.get("fingerprint")) == fingerprint
        and _text(previous.get("registration_id"))
    ):
        return _public_receipt(state, status="pending_activation", idempotent=True)

    _atomic_put(
        state,
        {
            "portal_api_base": request_values["portal_api_base"],
            "portal_agent_credential": key,
            "agent_id": agent,
            "commercial_key_id": key_id,
            "host_fingerprint": request_values["host_fingerprint"],
            "commercial_manual_key_id": key_id,
            "commercial_manual_key_staged": True,
            "commercial_revoked": False,
            "commercial_detached": False,
            "commercial_portal_connection_state": "pending_activation",
            "commercial_pending_activation": {
                "schema": PENDING_REGISTRATION_SCHEMA,
                "contract_version": "1.0",
                "fingerprint": fingerprint,
                "local_identity": identity,
                "display_name": _text(display_name),
                "mautic_version": _text(mautic_version),
                "application_root_fingerprint": root_fingerprint,
                "activation_state": "passive",
            },
        },
    )
    client = PortalClient(state, timeout_sec=timeout_sec)
    try:
        result = client.register_installation(
            local_identity=identity,
            display_name=_text(display_name),
            mautic_version=_text(mautic_version),
            application_root_fingerprint=root_fingerprint,
            activation_state="passive",
        )
    except Exception as exc:
        # The key and pending request remain durable for retry; the error is
        # intentionally reduced to a type/code and never includes the key.
        return _public_receipt(state, status="pending_activation", reason=type(exc).__name__[:80])
    registration_id = (
        _text(result.get("registration_id") or result.get("installation_id"))
        if isinstance(result, Mapping)
        else ""
    )
    pending = dict(state.get("commercial_pending_activation") or {})
    pending["registration_id"] = registration_id or None
    pending["registration_response_schema"] = _text(result.get("schema")) if isinstance(result, Mapping) else ""
    _atomic_put(state, {"commercial_pending_activation": pending})
    return _public_receipt(state, status="pending_activation")


def activate_manual_key(state: CommercialState) -> dict[str, Any]:
    """Explicitly activate a staged key; repeated activation is idempotent."""
    if not state.get("commercial_manual_key_staged") or not state.get("commercial_pending_activation"):
        raise _error("manual_activation_not_staged")
    already = _text(state.get("commercial_portal_connection_state")) == "active" and not bool(state.get("commercial_detached"))
    _atomic_put(
        state,
        {
            "commercial_portal_connection_state": "active",
            "commercial_activation_state": "active",
            "commercial_revoked": False,
            "commercial_detached": False,
        },
    )
    return _public_receipt(state, status="active", idempotent=already)


def disconnect_manual_key(state: CommercialState) -> dict[str, Any]:
    """Explicitly disable the connection while retaining the key for reactivation."""
    already = bool(state.get("commercial_detached")) and _text(state.get("commercial_portal_connection_state")) == "detached"
    _atomic_put(
        state,
        {
            "commercial_portal_connection_state": "detached",
            "commercial_activation_state": "disconnected",
            "commercial_detached": True,
            "commercial_revoked": False,
        },
    )
    return _public_receipt(state, status="disconnected", idempotent=already)


def status_manual_key(state: CommercialState) -> dict[str, Any]:
    return _public_receipt(state, status=_text(state.get("commercial_portal_connection_state")) or "unconfigured")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage an existing SalesSnap commercial key without SSH")
    parser.add_argument("--state-db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="store and passively register an existing Portal key")
    stage.add_argument("--portal-url", required=True)
    stage.add_argument("--agent-id", required=True)
    stage.add_argument("--commercial-key-id", required=True)
    stage.add_argument("--local-identity", required=True)
    stage.add_argument("--display-name", required=True)
    stage.add_argument("--mautic-version", required=True)
    stage.add_argument("--application-root-fingerprint", required=True)
    stage.add_argument("--commercial-key", default="", help="existing key; prefer hidden prompt or --key-stdin")
    stage.add_argument("--key-stdin", action="store_true", help="read the key from stdin without echo")
    stage.add_argument("--json", action="store_true")
    for name in ("activate", "disconnect", "status"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    state = CommercialState(args.state_db)
    try:
        if args.command == "stage":
            if args.commercial_key:
                key = args.commercial_key
            elif args.key_stdin:
                key = sys.stdin.readline().rstrip("\r\n")
            else:
                key = getpass.getpass("SalesSnap commercial key (hidden): ")
            result = stage_manual_key(
                state,
                portal_url=args.portal_url,
                agent_id=args.agent_id,
                commercial_key_id=args.commercial_key_id,
                commercial_key=key,
                local_identity=args.local_identity,
                display_name=args.display_name,
                mautic_version=args.mautic_version,
                application_root_fingerprint=args.application_root_fingerprint,
            )
        elif args.command == "activate":
            result = activate_manual_key(state)
        elif args.command == "disconnect":
            result = disconnect_manual_key(state)
        else:
            result = status_manual_key(state)
    except ManualActivationError as exc:
        result = {"schema": RECEIPT_SCHEMA, "contract_version": "1.0", "status": "failed", "code": str(exc)}
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
