from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
import imaplib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from mcd_agent.db import MauticDB


PARSER_VERSION = 1
TYPE_FEEDBACK_LOOP = "feedback_loop"
TYPE_BOUNCE = "bounce"
TYPE_UNSUBSCRIBE = "unsubscribe"
ALLOWED_TYPES = {TYPE_FEEDBACK_LOOP, TYPE_BOUNCE, TYPE_UNSUBSCRIBE}
DNC_UNSUBSCRIBED = 1
DNC_BOUNCED = 3
_EMAIL_RE = re.compile(r"(?<![A-Z0-9._%+\-])([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})(?![A-Z0-9._%+\-])", re.I)
_IGNORE_LOCAL_PARTS = {"mailer-daemon", "postmaster", "abuse", "fbl", "noreply", "no-reply", "donotreply", "do-not-reply"}


@dataclass(frozen=True)
class MonitoredEmailParserSettings:
    enabled: bool = False
    interval_sec: int = 900
    batch_size: int = 100
    force_seen: bool = False
    delete_processed: bool = False
    disable_mautic_fetch: bool = True
    types: tuple[str, ...] = (TYPE_FEEDBACK_LOOP,)
    whitelist: tuple[str, ...] = ()


@dataclass
class ParsedMonitoredMessage:
    kind: str
    emails: list[str]
    reason: int
    comments: str


@dataclass
class MonitoredEmailRunResult:
    scanned: int = 0
    matched: int = 0
    dnc_added: int = 0
    dnc_existing: int = 0
    contacts_matched: int = 0
    deleted: int = 0
    marked_seen: int = 0
    no_contact: int = 0
    whitelist_dnc_removed: int = 0
    errors: list[str] = field(default_factory=list)
    by_type: dict[str, int] = field(default_factory=dict)
    state: dict[str, object] = field(default_factory=dict)

    def bump(self, kind: str) -> None:
        self.by_type[kind] = int(self.by_type.get(kind, 0)) + 1


@dataclass(frozen=True)
class _MailboxTarget:
    role: str
    host: str
    port: int
    encryption: str
    user: str
    password: str
    folder: str

    @property
    def key(self) -> str:
        encrypted = "ssl" if "ssl" in self.encryption.lower() else "plain"
        return f"{self.user}@{self.host}:{self.port}/{encrypted}/{self.folder}"


def monitored_email_state_key(root: str, settings: MonitoredEmailParserSettings) -> str:
    root_hash = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:24]
    fp_payload = {
        "v": PARSER_VERSION,
        "types": list(settings.types),
        "force_seen": bool(settings.force_seen),
    }
    fp = hashlib.sha1(json.dumps(fp_payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"monitored_email:{root_hash}:{fp}"


def load_monitored_email_config(*, local_php_path: str | None, php_bin: str = "/usr/bin/php") -> dict[str, Any]:
    if not local_php_path:
        return {}
    path = Path(local_php_path)
    if not path.exists():
        return {}
    code = (
        "$parameters=[];"
        "include $argv[1];"
        "$m=$parameters['monitored_email'] ?? [];"
        "echo json_encode($m, JSON_UNESCAPED_SLASHES|JSON_UNESCAPED_UNICODE);"
    )
    proc = subprocess.run(
        [str(php_bin or "/usr/bin/php"), "-r", code, str(path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "local.php monitored_email read failed").strip())
    raw = (proc.stdout or "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def monitored_email_targets(monitored: dict[str, Any], enabled_types: tuple[str, ...]) -> list[_MailboxTarget]:
    general = monitored.get("general")
    if not isinstance(general, dict):
        return []
    role_keys: dict[str, str] = {
        TYPE_FEEDBACK_LOOP: "EmailBundle_unsubscribes",
        TYPE_UNSUBSCRIBE: "EmailBundle_unsubscribes",
        TYPE_BOUNCE: "EmailBundle_bounces",
    }
    targets: dict[str, _MailboxTarget] = {}
    for role in enabled_types:
        folder_key = role_keys.get(role)
        if not folder_key:
            continue
        raw_folder = monitored.get(folder_key)
        folder_cfg = raw_folder if isinstance(raw_folder, dict) else {}
        override = _to_bool(folder_cfg.get("override_settings"), False)
        cfg = dict(general)
        if override:
            cfg.update({k: v for k, v in folder_cfg.items() if v not in (None, "")})
        folder = str(folder_cfg.get("folder") or cfg.get("folder") or "INBOX").strip() or "INBOX"
        host = str(cfg.get("host") or "").strip()
        user = str(cfg.get("user") or "").strip()
        password = str(cfg.get("password") or "")
        if not host or not user or not password:
            continue
        try:
            port = int(cfg.get("port") or 993)
        except Exception:
            port = 993
        target = _MailboxTarget(
            role=role,
            host=host,
            port=port,
            encryption=str(cfg.get("encryption") or "").strip(),
            user=user,
            password=password,
            folder=folder,
        )
        targets.setdefault(target.key, target)
    return list(targets.values())


def process_monitored_email(
    *,
    db: MauticDB,
    local_php_path: str | None,
    php_bin: str,
    settings: MonitoredEmailParserSettings,
    state: dict[str, object] | None = None,
) -> MonitoredEmailRunResult:
    result = MonitoredEmailRunResult(state=dict(state or {}))
    if not settings.enabled:
        return result
    whitelist = _normalize_whitelist(settings.whitelist)
    if whitelist and hasattr(db, "remove_email_dnc_for_emails"):
        cleanup = db.remove_email_dnc_for_emails(sorted(whitelist))
        result.whitelist_dnc_removed = int(cleanup.get("removed", 0) or 0)
    monitored = load_monitored_email_config(local_php_path=local_php_path, php_bin=php_bin)
    targets = monitored_email_targets(monitored, settings.types)
    if not targets:
        result.errors.append("monitored_email_not_configured")
        return result

    state_next = dict(state or {})
    folders_state = state_next.get("folders")
    if not isinstance(folders_state, dict):
        folders_state = {}
    for target in targets:
        folder_state = folders_state.get(target.key)
        if not isinstance(folder_state, dict):
            folder_state = {}
        try:
            folder_result, next_folder_state = _process_target(db, target, settings, folder_state)
            _merge_result(result, folder_result)
            folders_state[target.key] = next_folder_state
        except Exception as e:
            result.errors.append(f"{target.folder}:{e}")
    state_next["folders"] = folders_state
    state_next["updated_at"] = time.time()
    state_next["parser_version"] = PARSER_VERSION
    result.state = state_next
    return result


def parse_monitored_message(
    raw: bytes,
    enabled_types: tuple[str, ...],
    whitelist: tuple[str, ...] | list[str] | None = None,
) -> ParsedMonitoredMessage | None:
    enabled = tuple(x for x in enabled_types if x in ALLOWED_TYPES)
    if not enabled:
        return None
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    extracted = _extract_message_text(msg)
    allowlist = _normalize_whitelist(whitelist)

    if TYPE_FEEDBACK_LOOP in enabled and _is_feedback_loop(msg, extracted):
        emails = _filter_whitelisted(_feedback_loop_emails(msg, extracted), allowlist)
        if emails:
            return ParsedMonitoredMessage(
                kind=TYPE_FEEDBACK_LOOP,
                emails=emails,
                reason=DNC_UNSUBSCRIBED,
                comments="Spam complaint",
            )

    if TYPE_BOUNCE in enabled and _is_bounce(msg, extracted):
        emails = _filter_whitelisted(_bounce_emails(msg, extracted), allowlist)
        if emails:
            return ParsedMonitoredMessage(
                kind=TYPE_BOUNCE,
                emails=emails,
                reason=DNC_BOUNCED,
                comments="Bounce",
            )

    if TYPE_UNSUBSCRIBE in enabled and _is_unsubscribe(msg, extracted):
        emails = _filter_whitelisted(_unsubscribe_emails(msg, extracted), allowlist)
        if emails:
            return ParsedMonitoredMessage(
                kind=TYPE_UNSUBSCRIBE,
                emails=emails,
                reason=DNC_UNSUBSCRIBED,
                comments="User unsubscribed.",
            )
    return None


def _process_target(
    db: MauticDB,
    target: _MailboxTarget,
    settings: MonitoredEmailParserSettings,
    folder_state: dict[str, object],
) -> tuple[MonitoredEmailRunResult, dict[str, object]]:
    result = MonitoredEmailRunResult()
    imap = _connect(target)
    deleted_any = False
    try:
        status, _data = imap.select(target.folder)
        if status != "OK":
            raise RuntimeError(f"select_failed:{target.folder}")
        criteria = "ALL" if settings.force_seen else "UNSEEN"
        typ, data = imap.uid("SEARCH", None, criteria)
        if typ != "OK":
            raise RuntimeError(f"search_failed:{criteria}")
        uids = _parse_uid_list(data)
        processed_uids = _int_set(folder_state.get("processed_uids"))
        cursor_uid = int(folder_state.get("force_cursor_uid") or 0)
        if settings.force_seen:
            uids = [uid for uid in uids if uid > cursor_uid and uid not in processed_uids]
        else:
            uids = [uid for uid in uids if uid not in processed_uids]
        uids = sorted(uids)[: min(5000, max(1, int(settings.batch_size)))]
        max_seen_uid = cursor_uid
        for uid in uids:
            max_seen_uid = max(max_seen_uid, uid)
            result.scanned += 1
            try:
                raw = _fetch_raw(imap, uid)
                parsed = parse_monitored_message(raw, settings.types, settings.whitelist)
                handled = False
                if parsed is not None:
                    result.matched += 1
                    result.bump(parsed.kind)
                    dnc = db.add_dnc_for_emails(
                        parsed.emails,
                        reason=parsed.reason,
                        comments=parsed.comments,
                    )
                    added = int(dnc.get("added", 0) or 0)
                    existing = int(dnc.get("existing", 0) or 0)
                    contacts = int(dnc.get("contacts", 0) or 0)
                    result.dnc_added += added
                    result.dnc_existing += existing
                    result.contacts_matched += contacts
                    if contacts <= 0:
                        result.no_contact += 1
                    handled = contacts > 0
                if handled and settings.delete_processed:
                    _delete_uid(imap, uid)
                    result.deleted += 1
                    deleted_any = True
                else:
                    _mark_seen(imap, uid)
                    result.marked_seen += 1
                processed_uids.add(uid)
            except Exception as e:
                result.errors.append(f"uid={uid}:{e}")
        if settings.force_seen:
            folder_state["force_cursor_uid"] = max_seen_uid
        folder_state["processed_uids"] = sorted(processed_uids)[-10000:]
        folder_state["last_run_at"] = time.time()
        if deleted_any:
            imap.expunge()
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass
    return result, folder_state


def _connect(target: _MailboxTarget) -> imaplib.IMAP4:
    if "ssl" in target.encryption.lower() or int(target.port) == 993:
        imap: imaplib.IMAP4 = imaplib.IMAP4_SSL(target.host, target.port, timeout=20)
    else:
        imap = imaplib.IMAP4(target.host, target.port, timeout=20)
    imap.login(target.user, target.password)
    return imap


def _fetch_raw(imap: imaplib.IMAP4, uid: int) -> bytes:
    typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise RuntimeError("fetch_failed")
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("empty_fetch")


def _delete_uid(imap: imaplib.IMAP4, uid: int) -> None:
    imap.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")


def _mark_seen(imap: imaplib.IMAP4, uid: int) -> None:
    imap.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Seen)")


def _parse_uid_list(data: list[bytes] | list[Any] | tuple[Any, ...] | None) -> list[int]:
    raw = b" ".join(x for x in (data or []) if isinstance(x, bytes))
    out: list[int] = []
    for item in raw.split():
        try:
            uid = int(item)
        except Exception:
            continue
        if uid > 0:
            out.append(uid)
    return out


def _int_set(raw: object) -> set[int]:
    if not isinstance(raw, list):
        return set()
    out: set[int] = set()
    for item in raw:
        try:
            value = int(item)
        except Exception:
            continue
        if value > 0:
            out.add(value)
    return out


def _extract_message_text(msg: Message) -> dict[str, str]:
    chunks: dict[str, list[str]] = {
        "headers": [_headers_text(msg)],
        "text": [],
        "feedback": [],
        "delivery": [],
        "original": [],
    }
    for part in msg.walk():
        ctype = part.get_content_type().lower()
        if ctype == "message/feedback-report":
            chunks["feedback"].append(_part_text(part))
        elif ctype == "message/delivery-status":
            chunks["delivery"].append(_delivery_status_text(part))
        elif ctype == "message/rfc822":
            chunks["original"].append(_message_rfc822_text(part))
        elif ctype in {"text/plain", "text/html"}:
            chunks["text"].append(_part_text(part))
    return {k: "\n".join(v) for k, v in chunks.items()}


def _headers_text(msg: Message) -> str:
    out = []
    for key, value in msg.items():
        out.append(f"{key}: {value}")
    return "\n".join(out)


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
    except Exception:
        pass
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if isinstance(payload, str):
        return payload
    return ""


def _delivery_status_text(part: Message) -> str:
    payload = part.get_payload()
    if isinstance(payload, list):
        return "\n".join(_headers_text(x) for x in payload if isinstance(x, Message))
    return _part_text(part)


def _message_rfc822_text(part: Message) -> str:
    payload = part.get_payload()
    if isinstance(payload, list) and payload and isinstance(payload[0], Message):
        nested = payload[0]
        return _headers_text(nested) + "\n\n" + _body_text(nested)
    return _part_text(part)


def _body_text(msg: Message) -> str:
    out: list[str] = []
    for part in msg.walk():
        if part.get_content_type().lower() in {"text/plain", "text/html"}:
            out.append(_part_text(part))
    return "\n".join(out)


def _is_feedback_loop(msg: Message, extracted: dict[str, str]) -> bool:
    report_type = str(msg.get_param("report-type", header="content-type") or "").lower()
    hay = (extracted.get("headers", "") + "\n" + extracted.get("feedback", "")).lower()
    return report_type == "feedback-report" or "feedback-type:" in hay


def _is_bounce(msg: Message, extracted: dict[str, str]) -> bool:
    report_type = str(msg.get_param("report-type", header="content-type") or "").lower()
    hay = (extracted.get("headers", "") + "\n" + extracted.get("delivery", "")).lower()
    return report_type == "delivery-status" or "final-recipient:" in hay or "x-failed-recipients:" in hay


def _is_unsubscribe(msg: Message, extracted: dict[str, str]) -> bool:
    subject = str(msg.get("Subject") or "").lower()
    hay = (subject + "\n" + extracted.get("headers", "") + "\n" + extracted.get("text", "")).lower()
    if "/email/unsubscribe/" in hay or "list-unsubscribe:" in hay:
        return True
    return any(token in hay for token in ("unsubscribe", "отпис", "одјав", "odjava"))


def _feedback_loop_emails(msg: Message, extracted: dict[str, str]) -> list[str]:
    candidates: list[str] = []
    candidates.extend(_field_emails(extracted.get("feedback", ""), ("Original-Rcpt-To", "Original-Recipient", "Final-Recipient")))
    original = extracted.get("original", "")
    candidates.extend(_field_emails(original, ("Original-Rcpt-To", "X-Original-To", "Delivered-To")))
    candidates.extend(_emails_from_unsubscribe_url(original))
    candidates.extend(_received_for_emails(original))
    candidates.extend(_header_emails(msg, ("Original-Rcpt-To", "X-Original-To", "Delivered-To")))
    return _clean_emails(candidates)


def _bounce_emails(msg: Message, extracted: dict[str, str]) -> list[str]:
    candidates: list[str] = []
    candidates.extend(_header_emails(msg, ("X-Failed-Recipients",)))
    candidates.extend(_field_emails(extracted.get("delivery", ""), ("Original-Recipient", "Final-Recipient")))
    candidates.extend(_field_emails(extracted.get("headers", ""), ("X-Failed-Recipients",)))
    candidates.extend(_emails_from_unsubscribe_url(extracted.get("original", "")))
    candidates.extend(_received_for_emails(extracted.get("original", "")))
    return _clean_emails(candidates)


def _unsubscribe_emails(msg: Message, extracted: dict[str, str]) -> list[str]:
    candidates: list[str] = []
    hay = "\n".join(extracted.values())
    candidates.extend(_emails_from_unsubscribe_url(hay))
    candidates.extend(_header_emails(msg, ("Reply-To", "From")))
    return _clean_emails(candidates)


def _field_emails(text: str, names: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for name in names:
        pattern = re.compile(rf"^\s*{re.escape(name)}\s*:\s*(.+)$", re.I | re.M)
        for match in pattern.finditer(text or ""):
            value = match.group(1).strip()
            if ";" in value:
                value = value.split(";", 1)[1].strip()
            out.extend(_emails_in_text(value))
    return out


def _header_emails(msg: Message, names: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for name in names:
        values.extend(str(v) for v in msg.get_all(name, []))
    parsed = [addr for _name, addr in getaddresses(values) if addr]
    if parsed:
        return parsed
    return _emails_in_text("\n".join(values))


def _emails_from_unsubscribe_url(text: str) -> list[str]:
    decoded = unquote(str(text or ""))
    out: list[str] = []
    for match in re.finditer(r"/email/unsubscribe/[^\s<>\"']+", decoded, flags=re.I):
        out.extend(_emails_in_text(match.group(0)))
    for line in decoded.splitlines():
        if "list-unsubscribe" in line.lower():
            out.extend(_emails_in_text(line))
    return out


def _received_for_emails(text: str) -> list[str]:
    out: list[str] = []
    for match in re.finditer(r"\bfor\s+<?([^<>\s;]+@[^<>\s;]+)>?\s*;", str(text or ""), flags=re.I):
        out.extend(_emails_in_text(match.group(1)))
    return out


def _emails_in_text(text: str) -> list[str]:
    return [m.group(1) for m in _EMAIL_RE.finditer(str(text or ""))]


def _clean_emails(candidates: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        email = str(raw or "").strip().strip("<>.,;:()[]{}'\"").lower()
        if not email or "@" not in email:
            continue
        local = email.split("@", 1)[0].lower()
        if local in _IGNORE_LOCAL_PARTS:
            continue
        if email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def _normalize_whitelist(raw: tuple[str, ...] | list[str] | None) -> set[str]:
    out: set[str] = set()
    for item in raw or ():
        email = str(item or "").strip().strip("<>.,;:()[]{}'\"").lower()
        if email and "@" in email:
            out.add(email)
    return out


def _filter_whitelisted(emails: list[str], whitelist: set[str]) -> list[str]:
    if not whitelist:
        return emails
    return [email for email in emails if str(email or "").strip().lower() not in whitelist]


def _merge_result(target: MonitoredEmailRunResult, src: MonitoredEmailRunResult) -> None:
    target.scanned += src.scanned
    target.matched += src.matched
    target.dnc_added += src.dnc_added
    target.dnc_existing += src.dnc_existing
    target.contacts_matched += src.contacts_matched
    target.deleted += src.deleted
    target.marked_seen += src.marked_seen
    target.no_contact += src.no_contact
    target.whitelist_dnc_removed += src.whitelist_dnc_removed
    target.errors.extend(src.errors)
    for key, value in src.by_type.items():
        target.by_type[key] = int(target.by_type.get(key, 0)) + int(value)


def _to_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default
