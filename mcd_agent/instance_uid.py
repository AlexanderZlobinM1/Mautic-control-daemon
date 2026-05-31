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
