from __future__ import annotations

import re
from pathlib import Path


_KEYS = (
    "db_host",
    "db_table_prefix",
    "db_port",
    "db_name",
    "db_user",
    "db_password",
    "site_url",
    "default_timezone",
    "timezone",
)


def parse_local_php(path: str) -> dict[str, str]:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    out: dict[str, str] = {}
    for key in _KEYS:
        pattern = rf"['\"]{re.escape(key)}['\"]\s*=>\s*['\"]([^'\"]*)['\"]"
        m = re.search(pattern, text)
        if m:
            out[key] = m.group(1).strip()
    return out
