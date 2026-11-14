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


def canonical_instance_uid(instance_uid: str) -> str:
    """Return the stable bare UID used by every public MCD interface."""
    uid = str(instance_uid or "").strip()
    return uid.split("@", 1)[0] if uid else ""


def normalize_instance_uid(instance_uid: str, host_name: str | None) -> str:
    """Normalize bare or legacy same-host input to the canonical bare UID."""
    uid = str(instance_uid or "").strip()
    if not uid or "@" not in uid:
        return uid
    base, qualifier = uid.split("@", 1)
    host = str(host_name or "").strip()
    if (not base or not qualifier or "@" in qualifier or not host
            or qualifier.casefold() != host.casefold()):
        raise ValueError("instance_uid_host_mismatch")
    return base


def instance_uid_matches(requested: str, local: str, host_name: str | None) -> bool:
    """Match bare or legacy same-host input without emitting a qualified UID."""
    try:
        requested_uid = normalize_instance_uid(requested, host_name)
        local_uid = normalize_instance_uid(local, host_name)
    except ValueError:
        return False
    return bool(requested_uid and local_uid and requested_uid == local_uid)
