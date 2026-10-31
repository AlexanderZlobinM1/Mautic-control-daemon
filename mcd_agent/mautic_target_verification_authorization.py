"""Online-only check-only admission; no signing key or mutation authority."""

from mcd_agent.mcc_transport import mcc_request
import json
import time
import re
import urllib.request
from urllib.parse import urlsplit

from mcd_agent.mautic_patch_resolution import canonical_json_sha256
from mcd_agent.mautic_check_only_inputs import strict_json

PURPOSE = "verify_target_excluded_patches"
BINDINGS = ("job_id", "run_id", "prepared_target_id", "original_patch_plan_sha256",
            "catalog_revision", "catalog_sha256", "target_artifact_sha256", "execution_context",
            "identity_association", "root_mapping")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authorize_target_verification(*, snapshot, authorization_context, mcc_url, token):
    plan_hash = canonical_json_sha256(snapshot)
    claims = authorization_context
    fields = set(BINDINGS) | {"schema", "purpose", "verification_plan_sha256",
                             "issued_at", "expires_at", "nonce", "signature"}
    if not isinstance(claims, dict) or set(claims) != fields:
        raise ValueError("target_verification_authorization_fields_invalid")
    if (type(claims["nonce"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", claims["nonce"])
            or type(claims["signature"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", claims["signature"])):
        raise ValueError("target_verification_authorization_signature_shape_invalid")
    if (claims["schema"] != "mcc-mautic-target-patch-verification-authorization-v1"
            or claims["purpose"] != PURPOSE or claims["verification_plan_sha256"] != plan_hash
            or any(claims[key] != snapshot[key] for key in BINDINGS)):
        raise ValueError("target_verification_authorization_binding_mismatch")
    issued, expires = claims["issued_at"], claims["expires_at"]
    if (type(issued) is not int or type(expires) is not int
            or not 0 < expires - issued <= 180 or not issued <= time.time() < expires):
        raise ValueError("target_verification_authorization_expired")
    parsed = urlsplit(mcc_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("target_verification_endpoint_invalid")
    if type(token) is not str or not token or any(c in token for c in "\r\n"):
        raise ValueError("target_verification_token_invalid")
    context_hash = canonical_json_sha256(claims)
    request = mcc_request(mcc_url.rstrip("/") + "/api/v1/mautic/patches/authorize-target-verification",
        data=json.dumps(dict(authorization_context=claims, authorization_context_sha256=context_hash),
            sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
    expected = {key: claims[key] for key in BINDINGS}
    expected.update(schema="mcc-mautic-target-patch-verification-admission-v1", status="accepted",
        purpose=PURPOSE, verification_plan_sha256=plan_hash, authorization_context_sha256=context_hash,
        issued_at=issued, expires_at=expires)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError("rejected")
            raw = response.read(1048577)
        accepted = strict_json(raw, max_bytes=1048576)
        if accepted != expected or not issued <= time.time() < expires:
            raise ValueError("mismatch")
    except Exception:
        raise ValueError("target_verification_authorization_rejected") from None
    return accepted
