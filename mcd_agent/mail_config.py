from __future__ import annotations

import os
from typing import Any
from urllib import parse

from mcd_agent.amazon_mailer_dep import (
    AMAZON_MAILER_PACKAGE,
    HTTP_CLIENT_PACKAGE,
    SENDGRID_MAILER_PACKAGE,
    _read_php_array_string,
    ensure_mailer_packages,
)
from mcd_agent.config import AgentConfig
from mcd_agent.local_mail import (
    _clear_mautic_cache,
    _host_name,
    _local_php,
    _mcc_json,
    _send_test_message,
    _set_php_parameter,
    _set_php_parameter_if_present,
    _symfony_config_escape,
    _write_atomic,
    configure_local_mail,
    disable_local_mail,
)


_PRESERVE_CURRENT_CREDENTIALS = "_preserve_current_credentials"
_SES_API_METHODS = {"mautic+ses+api", "ses+api", "ses+https"}


def fetch_mail_profile(cfg: AgentConfig, profile_id: str) -> dict[str, Any]:
    return _mcc_json(
        cfg,
        "/api/v1/agent/mail-config/material",
        query={"profile_id": str(profile_id or "").strip(), "host_name": _host_name(cfg)},
    )


def _profile_status(
    cfg: AgentConfig,
    profile_id: str,
    *,
    status: str,
    error: str = "",
    tested: bool = False,
    credentials: dict[str, str] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "profile_id": str(profile_id),
        "mcc_host_name": _host_name(cfg),
        "status": status,
        "error": error,
        "tested": bool(tested),
    }
    if credentials:
        payload["credentials"] = {str(key): str(value) for key, value in credentials.items() if str(value)}
    _mcc_json(
        cfg,
        "/api/v1/agent/mail-config/status",
        payload=payload,
        timeout_sec=10,
    )


def _required_credential(credentials: dict[str, Any], key: str) -> str:
    value = str(credentials.get(key) or "")
    if not value:
        raise RuntimeError(f"mail profile credential is missing: {key}")
    return value


def _current_mailer_dsn(root: str) -> str:
    text = _local_php(root).read_text(encoding="utf-8", errors="replace")
    return str(_read_php_array_string(text, "mailer_dsn") or "").replace("%%", "%").strip()


def _credentials_for_apply(
    root: str,
    kind: str,
    settings: dict[str, Any],
    credentials: dict[str, Any],
) -> dict[str, str]:
    effective = {str(key): str(value or "") for key, value in credentials.items() if str(value or "")}
    if not bool(settings.get(_PRESERVE_CURRENT_CREDENTIALS)):
        return effective

    current_dsn = _current_mailer_dsn(root)
    try:
        current = parse.urlsplit(current_dsn)
    except ValueError:
        return effective
    current_method = str(current.scheme or "").strip().lower()
    target_method = str(settings.get("delivery_method") or "").strip().lower()
    if kind == "amazon_ses_api":
        if current_method in _SES_API_METHODS and target_method in _SES_API_METHODS:
            current_values = {
                "access_key": parse.unquote(str(current.username or "")),
                "secret_key": parse.unquote(str(current.password or "")),
            }
        elif current_method == target_method == "ses+smtp":
            current_values = {
                "smtp_username": parse.unquote(str(current.username or "")),
                "smtp_password": parse.unquote(str(current.password or "")),
            }
        else:
            current_values = {}
    elif kind == "smtp" and current_method in {"smtp", "smtps"}:
        current_values = {
            "username": parse.unquote(str(current.username or "")),
            "password": parse.unquote(str(current.password or "")),
        }
    elif kind == "sendgrid_api" and current_method == "sendgrid+api":
        current_values = {"api_key": parse.unquote(str(current.username or ""))}
    else:
        current_values = {}
    return {**current_values, **effective}


def _external_dsn(kind: str, settings: dict[str, Any], credentials: dict[str, Any]) -> tuple[str, set[str]]:
    if kind == "smtp":
        user = parse.quote(_required_credential(credentials, "username"), safe="")
        password = parse.quote(_required_credential(credentials, "password"), safe="")
        host = str(settings.get("host") or "").strip()
        port = int(settings.get("port") or 587)
        scheme = "smtps" if str(settings.get("encryption") or "starttls") == "tls" else "smtp"
        return f"{scheme}://{user}:{password}@{host}:{port}", set()
    if kind == "amazon_ses_api":
        method = str(settings.get("delivery_method") or "ses+api").strip().lower()
        if method == "ses+smtp":
            access = parse.quote(_required_credential(credentials, "smtp_username"), safe="")
            secret = parse.quote(_required_credential(credentials, "smtp_password"), safe="")
        else:
            access = parse.quote(_required_credential(credentials, "access_key"), safe="")
            secret = parse.quote(_required_credential(credentials, "secret_key"), safe="")
        region = parse.quote(str(settings.get("region") or "eu-central-1"), safe="")
        if method not in {"mautic+ses+api", "ses+api", "ses+https", "ses+smtp"}:
            raise RuntimeError(f"unsupported Amazon SES delivery method: {method}")
        packages = {AMAZON_MAILER_PACKAGE}
        if method != "ses+smtp":
            packages.add(HTTP_CLIENT_PACKAGE)
        return f"{method}://{access}:{secret}@default?region={region}", packages
    if kind == "sendgrid_api":
        key = parse.quote(_required_credential(credentials, "api_key"), safe="")
        return f"sendgrid+api://{key}@default", {SENDGRID_MAILER_PACKAGE}
    raise RuntimeError(f"unsupported external mail transport: {kind}")


def _configure_external_mautic(root: str, settings: dict[str, Any], dsn: str) -> None:
    path = _local_php(root)
    text = path.read_text(encoding="utf-8", errors="replace")
    text = _set_php_parameter(text, "mailer_dsn", _symfony_config_escape(dsn))
    text = _set_php_parameter(text, "mailer_from_email", str(settings.get("from_email") or ""))
    text = _set_php_parameter_if_present(text, "mailer_from_name", str(settings.get("from_name") or "Sales Snap"))
    text = _set_php_parameter(text, "mailer_return_path", str(settings.get("return_path") or ""))
    stat = path.stat()
    _write_atomic(path, text, mode=stat.st_mode & 0o777)
    os.chown(path, stat.st_uid, stat.st_gid)


def apply_mail_profile(
    cfg: AgentConfig,
    *,
    profile_id: str,
    domain: str,
    root: str,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("mail-config apply must run as root")
    material = fetch_mail_profile(cfg, profile_id)
    if str(material.get("instance_domain") or "").strip().lower() != str(domain or "").strip().lower():
        raise RuntimeError("mail profile belongs to another instance")
    # MCC resolves local hostname aliases and validates canonical host ownership
    # plus the agent source IP before returning private profile material.
    kind = str(material.get("transport_type") or "").strip().lower()
    settings = material.get("settings") if isinstance(material.get("settings"), dict) else {}
    credentials = material.get("credentials") if isinstance(material.get("credentials"), dict) else {}
    recipient = str(material.get("last_test_recipient") or "").strip()
    name = str(material.get("name") or "Mail profile").strip()
    try:
        try:
            _profile_status(cfg, profile_id, status="applying")
        except Exception:
            pass
        if kind == "own_host":
            result = configure_local_mail(
                cfg,
                domain=domain,
                root=root,
                test_recipient=recipient,
                profile_name=name,
            )
        else:
            path = _local_php(root)
            before = path.read_text(encoding="utf-8", errors="replace")
            stat = path.stat()
            credentials = _credentials_for_apply(root, kind, settings, credentials)
            dsn, packages = _external_dsn(kind, settings, credentials)
            try:
                if packages:
                    ensure_mailer_packages(
                        config=cfg,
                        root=root,
                        packages=packages,
                        reason=f"MCC mail profile {name}",
                    )
                _configure_external_mautic(root, settings, dsn)
                _clear_mautic_cache(cfg, root)
                test_result = _send_test_message(
                    cfg,
                    root=root,
                    recipient=recipient,
                    profile_name=name,
                )
                disable_result = disable_local_mail(cfg, domain=domain)
            except Exception:
                _write_atomic(path, before, mode=stat.st_mode & 0o777)
                os.chown(path, stat.st_uid, stat.st_gid)
                try:
                    _clear_mautic_cache(cfg, root)
                except Exception:
                    pass
                raise
            result = {
                "status": "ok",
                "instance_domain": domain,
                "transport_type": kind,
                "packages": sorted(packages),
                "test_email": test_result,
                "own_host_disable": disable_result,
            }
        try:
            _profile_status(
                cfg,
                profile_id,
                status="tested",
                tested=True,
                credentials=credentials if kind != "own_host" else None,
            )
        except Exception:
            pass
        return {"profile_id": profile_id, "transport_type": kind, **result}
    except Exception as exc:
        try:
            _profile_status(cfg, profile_id, status="error", error=str(exc))
        except Exception:
            pass
        raise


def preflight_mail_profile(
    cfg: AgentConfig,
    *,
    profile_id: str,
    domain: str,
    root: str,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("mail-config preflight must run as root")
    material = fetch_mail_profile(cfg, profile_id)
    if str(material.get("instance_domain") or "").strip().lower() != str(domain or "").strip().lower():
        raise RuntimeError("mail profile belongs to another instance")
    kind = str(material.get("transport_type") or "").strip().lower()
    settings = material.get("settings") if isinstance(material.get("settings"), dict) else {}
    credentials = material.get("credentials") if isinstance(material.get("credentials"), dict) else {}
    _dsn, packages = _external_dsn(kind, settings, credentials)
    changed = ensure_mailer_packages(
        config=cfg,
        root=root,
        packages=packages,
        reason=f"MCC mail profile preflight {profile_id}",
    )
    return {
        "status": "ok",
        "profile_id": profile_id,
        "transport_type": kind,
        "packages": sorted(packages),
        "changed": bool(changed),
    }
