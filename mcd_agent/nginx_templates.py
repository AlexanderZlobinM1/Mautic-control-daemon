from __future__ import annotations

import re
from importlib import resources
from typing import Any


_TOKEN_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")


def render_nginx_template(name: str, **values: Any) -> str:
    template = resources.files("mcd_agent.templates.nginx").joinpath(name).read_text(encoding="utf-8")
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    missing = sorted(set(_TOKEN_RE.findall(rendered)))
    if missing:
        raise RuntimeError(f"nginx template {name} has unresolved placeholders: {', '.join(missing)}")
    return rendered
