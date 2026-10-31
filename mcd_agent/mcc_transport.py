"""Corporate HTTP request identity; authentication and TLS remain at callers."""

from __future__ import annotations

from typing import Mapping
from urllib import request

from mcd_agent import __version__


MCC_USER_AGENT = f"mcd-agent/{__version__} corporate-managed/1.0"


def mcc_request(url: str, data: bytes | None = None, headers: Mapping[str, str] | None = None,
                origin_req_host: str | None = None, unverifiable: bool = False,
                method: str | None = None) -> request.Request:
    """Identify the real MCD client without changing its URL, body or bearer."""
    outgoing = dict(headers or {})
    if not any(key.lower() == "user-agent" for key in outgoing):
        outgoing["User-Agent"] = MCC_USER_AGENT
    return request.Request(url, data=data, headers=outgoing, origin_req_host=origin_req_host,
                           unverifiable=unverifiable, method=method)
