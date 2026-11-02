from __future__ import annotations

import hashlib
import re
from pathlib import Path

_COMMON_ROOT_NAMES = {"public_html", "html", "www", "web", "htdocs"}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "mautic"


def _label_from_root(root: str, fallback_name: str) -> str:
    p = Path(root)
    base = p.name.strip() or fallback_name
    if base in _COMMON_ROOT_NAMES and p.parent.name.strip():
        base = p.parent.name.strip()
    return _slug(base)


def build_instance_uid(*, root: str, name: str) -> str:
    label = _label_from_root(root, name)
    short_hash = hashlib.blake2s(root.encode("utf-8"), digest_size=4).hexdigest()
    return f"{label}-{short_hash}"


def build_domain_uid(*, domain: str | None, root: str, name: str) -> str:
    d = (domain or "").strip().lower()
    if d:
        # Keep uid human-friendly and short; fallback hash is added only on collisions by inventory.
        d = d.split()[0].strip()
        d = d.replace("*.", "")
        d = re.sub(r"[^a-z0-9.-]+", "", d).strip(".-")
        if d:
            return d
    return build_instance_uid(root=root, name=name)


def qualify_instance_uid(instance_uid: str, host_name: str | None) -> str:
    """Return the MCC wire UID without changing the local inventory UID."""
    uid = str(instance_uid or "").strip()
    host = str(host_name or "").strip()
    if not uid or not host or "@" in uid:
        return uid
    return f"{uid}@{host}"


def instance_uid_matches(requested: str, local: str, host_name: str | None) -> bool:
    """Match canonical, legacy bare, and same-host qualified instance UIDs."""
    requested_uid = str(requested or "").strip()
    local_uid = str(local or "").strip()
    host = str(host_name or "").strip()
    if not requested_uid or not local_uid:
        return False
    if "@" in local_uid:
        local_base, local_host = local_uid.split("@", 1)
    else:
        local_base, local_host = local_uid, ""
    if "@" in requested_uid:
        requested_base, requested_host = requested_uid.split("@", 1)
        if not host or requested_host.casefold() != host.casefold():
            return False
        return requested_base == local_base and (not local_host or local_host.casefold() == host.casefold())
    if requested_uid != local_base:
        return False
    return not local_host or not host or local_host.casefold() == host.casefold()
