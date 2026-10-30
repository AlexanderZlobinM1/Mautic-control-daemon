"""Plain DNS identity for new Portal enrollments; legacy coordinates stay separate."""
from typing import Any, Mapping
import re


def dns_identity(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value):
        raise ValueError("portal_dns_identity_invalid")
    return value


def identity_coordinate(value: Mapping[str, Any]) -> dict[str, str]:
    if value.get("identity_kind") == "dns":
        if "host_fingerprint" in value:
            raise ValueError("portal_dns_fingerprint_forbidden")
        return {"identity_kind": "dns", "local_identity": dns_identity(value.get("local_identity"))}
    if value.get("identity_kind") not in (None, "") or "local_identity" in value:
        raise ValueError("portal_identity_kind_invalid")
    fingerprint = value.get("host_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("portal_legacy_identity_invalid")
    return {"host_fingerprint": fingerprint}


def channel_binding(value: Mapping[str, Any]) -> dict[str, str]:
    result = {}
    for key in ("agent_id", "installation_id", "commercial_key_id"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError("portal_channel_binding_invalid")
        result[key] = value[key]
    return {**result, **identity_coordinate(value)}
