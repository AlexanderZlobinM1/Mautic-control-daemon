"""Import an already-issued Portal activation into the local MCD state.

The Portal bootstrap is the authority for issuing the credential.  This
module only validates and atomically materializes that existing delivery; it
does not redeem activation codes, register installations, create jobs, or
open a network connection.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcd_agent import __version__
from mcd_agent.commercial_portal import (
    CommercialState,
    _b64,
    _host_fingerprint,
    _public_commercial_key_metadata,
    canonical_json,
    portal_artifact_trust_keys,
    validate_portal_artifact_trust,
)


SCHEMA = "mcd-portal-activation-material-v1"
CONTRACT_VERSION = "1.0"
_ACCEPTED_SCHEMAS = frozenset({SCHEMA, "portal-mcd-registration-delivery-v1", "sales-snap-registration-v1"})
_DEFAULT_AGENT_KEY_PATH = "/etc/mcd/portal-agent.key"


class RegistrationMaterialError(ValueError):
    """Secret-free validation error for the bootstrap receipt."""


def _error(code: str) -> RegistrationMaterialError:
    return RegistrationMaterialError(str(code))


def _text(value: object) -> str:
    return str(value or "").strip()


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise _error("portal_registration_base64_invalid") from exc


def _unwrap(raw: Mapping[str, Any]) -> dict[str, Any]:
    schema = _text(raw.get("schema"))
    if schema not in _ACCEPTED_SCHEMAS:
        raise _error("portal_registration_schema_invalid")
    contract = raw.get("contract_version", CONTRACT_VERSION)
    if _text(contract) != CONTRACT_VERSION:
        raise _error("portal_registration_contract_invalid")
    for key in ("registration", "activation_material", "delivery"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            merged.setdefault("schema", schema)
            merged.setdefault("contract_version", CONTRACT_VERSION)
            return merged
    return dict(raw)


def _first(source: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in source and source.get(key) not in (None, ""):
            return source.get(key)
    return None


def _portal_api_base(source: Mapping[str, Any]) -> str:
    value = _first(source, "portal_api_base", "api_base", "base_url")
    portal = source.get("portal")
    if value in (None, "") and isinstance(portal, Mapping):
        value = _first(portal, "portal_api_base", "api_base", "base_url")
    base = _text(value).rstrip("/")
    if not base.startswith("https://"):
        raise _error("portal_registration_https_required")
    parsed = urlparse(base)
    if not parsed.hostname:
        raise _error("portal_registration_url_invalid")
    if not parsed.path.rstrip("/").endswith("/api/v1/commercial"):
        base += "/api/v1/commercial"
    return base


def _commercial_key(source: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    raw = source.get("commercial_key") or source.get("commercial_key_material")
    if isinstance(raw, Mapping):
        key_id = _text(_first(raw, "commercial_key_id", "key_id", "id"))
        secret = _text(_first(raw, "credential", "secret", "value", "key"))
        public = _public_commercial_key_metadata(raw)
        return key_id, secret, public
    if raw not in (None, ""):
        return _text(_first(source, "commercial_key_id")), _text(raw), {}
    return _text(_first(source, "commercial_key_id")), "", {}


def _signing_keys(source: Mapping[str, Any]) -> dict[str, str]:
    raw = _first(source, "portal_job_signing_keys", "job_signing_keys")
    portal = source.get("portal")
    if raw is None and isinstance(portal, Mapping):
        raw = _first(portal, "portal_job_signing_keys", "job_signing_keys")
    values: dict[str, str] = {}
    if isinstance(raw, Mapping):
        iterable = [{"key_id": key, "value": value, "algorithm": "ed25519"} for key, value in raw.items()]
    elif isinstance(raw, list):
        iterable = raw
    else:
        raise _error("portal_registration_signing_keys_invalid")
    for item in iterable:
        if not isinstance(item, Mapping) or _text(item.get("algorithm")) != "ed25519":
            raise _error("portal_registration_signing_keys_invalid")
        key_id = _text(item.get("key_id"))
        value = _text(item.get("value"))
        if not key_id or len(_b64decode(value)) != 32:
            raise _error("portal_registration_signing_keys_invalid")
        values[key_id] = value
    if not values:
        raise _error("portal_registration_signing_keys_invalid")
    return values


def _private_key_value(source: Mapping[str, Any], key_path: str) -> tuple[str, str]:
    raw = _first(source, "agent_private_key", "agent_private_key_b64", "private_key")
    private: bytes
    if raw not in (None, ""):
        text = _text(raw)
        if text.startswith("-----BEGIN"):
            try:
                loaded = serialization.load_pem_private_key(text.encode(), password=None)
                if not isinstance(loaded, Ed25519PrivateKey):
                    raise _error("portal_registration_private_key_invalid")
                private = loaded.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            except RegistrationMaterialError:
                raise
            except Exception as exc:
                raise _error("portal_registration_private_key_invalid") from exc
        else:
            private = _b64decode(text)
    else:
        path = Path(key_path)
        try:
            loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise _error("portal_registration_private_key_invalid")
            private = loaded.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        except RegistrationMaterialError:
            raise
        except Exception as exc:
            raise _error("portal_registration_private_key_unavailable") from exc
    if len(private) != 32:
        raise _error("portal_registration_private_key_invalid")
    public = Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    derived_key_id = "agent-" + _b64(public)[:22]
    supplied_key_id = _text(_first(source, "agent_key_id", "public_key_id"))
    if supplied_key_id and supplied_key_id != derived_key_id:
        raise _error("portal_registration_agent_key_mismatch")
    return _b64(private), derived_key_id


def _installation_ids(source: Mapping[str, Any]) -> dict[str, str]:
    raw = _first(source, "installation_ids")
    if isinstance(raw, Mapping):
        result = {_text(key): _text(value) for key, value in raw.items() if _text(key) and _text(value)}
    elif isinstance(raw, list):
        result = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise _error("portal_registration_installations_invalid")
            identity = _text(_first(item, "local_identity", "identity"))
            installation = _text(_first(item, "installation_id", "id"))
            if not identity or not installation:
                raise _error("portal_registration_installations_invalid")
            result[identity] = installation
    else:
        installation = _text(_first(source, "installation_id"))
        identity = _text(_first(source, "local_identity", "identity"))
        result = {identity or "default": installation} if installation else {}
    if not result:
        raise _error("portal_registration_installations_invalid")
    return result


def _artifact_trust(source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize optional Portal artifact trust from activation material."""
    raw = _first(source, "portal_artifact_trust_v1", "artifact_trust")
    portal = source.get("portal")
    if raw is None and isinstance(portal, Mapping):
        raw = _first(portal, "portal_artifact_trust_v1", "artifact_trust")
    if raw is None:
        return None
    try:
        return validate_portal_artifact_trust(raw, code="portal_registration_artifact_trust_invalid")
    except ValueError as exc:
        raise _error(str(exc)) from exc


def _atomic_put(state: CommercialState, values: Mapping[str, Any]) -> None:
    with sqlite3.connect(state.path, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        for key, value in values.items():
            db.execute(
                "INSERT INTO commercial_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), json.dumps(value, ensure_ascii=True, sort_keys=True)),
            )


def import_activation_material(
    state: CommercialState,
    material: Mapping[str, Any],
    *,
    agent_key_path: str = _DEFAULT_AGENT_KEY_PATH,
) -> dict[str, Any]:
    """Atomically install existing activation material without network I/O."""
    source = _unwrap(material)
    api_base = _portal_api_base(source)
    commercial_key_id_from_material, commercial_key_secret, commercial_key_metadata = _commercial_key(source)
    credential = _text(_first(source, "portal_agent_credential", "agent_credential", "credential")) or commercial_key_secret
    agent_id = _text(_first(source, "agent_id"))
    commercial_key_id = _text(_first(source, "commercial_key_id")) or commercial_key_id_from_material
    if not credential or not agent_id or not commercial_key_id:
        raise _error("portal_registration_binding_incomplete")
    expected_fingerprint = _host_fingerprint()
    supplied_fingerprint = _text(_first(source, "host_fingerprint"))
    if supplied_fingerprint and supplied_fingerprint != expected_fingerprint:
        raise _error("portal_registration_host_mismatch")
    private_key, agent_key_id = _private_key_value(source, agent_key_path)
    signing_keys = _signing_keys(source)
    incoming_installations = _installation_ids(source)
    artifact_trust = _artifact_trust(source)

    existing_agent = _text(state.get("agent_id"))
    if existing_agent and existing_agent != agent_id:
        raise _error("portal_registration_agent_mismatch")
    existing_installations = state.get("installation_ids")
    merged_installations = dict(existing_installations) if isinstance(existing_installations, Mapping) else {}
    for identity, installation in incoming_installations.items():
        old = _text(merged_installations.get(identity))
        if old and old != installation:
            raise _error("portal_registration_installation_mismatch")
        merged_installations[identity] = installation

    digest_payload = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "portal_api_base": api_base,
        "agent_id": agent_id,
        "host_fingerprint": expected_fingerprint,
        "agent_key_id": agent_key_id,
        "commercial_key_id": commercial_key_id,
        "portal_job_signing_keys": signing_keys,
        "installation_ids": merged_installations,
    }
    if artifact_trust is not None:
        digest_payload["portal_artifact_trust_v1"] = artifact_trust
    material_digest = hashlib.sha256(canonical_json(digest_payload)).hexdigest()
    previous_digest = _text(state.get("portal_registration_material_digest"))
    updates: dict[str, Any] = {
        "portal_api_base": api_base,
        "portal_agent_credential": credential,
        "agent_id": agent_id,
        "host_fingerprint": expected_fingerprint,
        "agent_private_key": private_key,
        "agent_key_id": agent_key_id,
        "portal_job_signing_keys": signing_keys,
        "commercial_key_id": commercial_key_id,
        "installation_ids": merged_installations,
        "agent_version": _text(_first(source, "agent_version")) or __version__,
        "portal_registration_material_schema": SCHEMA,
        "portal_registration_material_digest": material_digest,
    }
    if artifact_trust is not None:
        updates.update(
            {
                "portal_artifact_trust_v1": artifact_trust,
                "portal_artifact_signing_keys": portal_artifact_trust_keys(artifact_trust),
                "portal_artifact_trust_revision": artifact_trust["trust_revision"],
                "portal_artifact_trust_digest": artifact_trust["trust_digest"],
                "portal_artifact_trust_domain": artifact_trust["trust_domain"],
                "portal_artifact_trust_source": "portal",
            }
        )
    supplied_key_metadata = source.get("commercial_key_metadata")
    if isinstance(supplied_key_metadata, Mapping):
        commercial_key_metadata = {
            **commercial_key_metadata,
            **_public_commercial_key_metadata(supplied_key_metadata),
        }
    if commercial_key_metadata:
        updates["commercial_key_metadata"] = commercial_key_metadata
    for key in ("portal_registration_id",):
        value = source.get(key)
        if value is not None:
            updates[key] = value
    if state.get("commercial_portal_connection_state") in (None, ""):
        updates["commercial_portal_connection_state"] = "passive"
    _atomic_put(state, updates)
    return {
        "schema": "mcd-portal-registration-receipt-v1",
        "contract_version": CONTRACT_VERSION,
        "status": "configured",
        "idempotent": previous_digest == material_digest,
        "agent_id": agent_id,
        "installation_ids": merged_installations,
        "commercial_key_id": commercial_key_id,
        "agent_key_id": agent_key_id,
        "host_fingerprint": expected_fingerprint,
        "material_digest": material_digest,
    }


def _read_material(args: argparse.Namespace) -> dict[str, Any]:
    if args.registration_file:
        raw = Path(args.registration_file).read_text(encoding="utf-8")
    elif args.registration_json:
        raw = args.registration_json
    else:
        raw = os.environ.get("SALESSNAP_REGISTRATION_JSON", "") or sys.stdin.read()
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _error("portal_registration_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise _error("portal_registration_json_invalid")
    return dict(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import existing Portal activation material into MCD")
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--agent-key-path", default=_DEFAULT_AGENT_KEY_PATH)
    parser.add_argument("--registration-file", default="")
    parser.add_argument("--registration-json", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        receipt = import_activation_material(
            CommercialState(args.state_db),
            _read_material(args),
            agent_key_path=args.agent_key_path,
        )
    except RegistrationMaterialError as exc:
        receipt = {"schema": "mcd-portal-registration-receipt-v1", "contract_version": CONTRACT_VERSION, "status": "failed", "code": str(exc)}
        print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
        return 1
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
