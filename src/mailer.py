r"""IMAP polling with the standard library only (imaplib + email).

The plugin reads mail itself: Astra's built-in inbox has no plugin-facing API,
its cache holds no bodies, and reading its private files from a plugin would
be wrong. Each account uses its own *app password* (Яндекс: «Пароль приложения»
с включённым IMAP; Gmail: «app password» при включённом 2FA).

Messages are fetched with BODY.PEEK — the \Seen flag is never touched, so the
plugin never marks anything as read.
"""

import email
import email.policy
import imaplib
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Set

PROVIDER_HOSTS: Dict[str, "tuple[str, int]"] = {
    "yandex": ("imap.yandex.ru", 993),
    "gmail": ("imap.gmail.com", 993),
    "mailru": ("imap.mail.ru", 993),
    "custom": ("", 993),
}

_CONNECT_TIMEOUT = 20   # seconds
_BODY_PREVIEW_CHARS = 700


def resolve_host(account: Dict[str, Any]) -> "tuple[str, int]":
    """IMAP (host, port) for an account: explicit host wins, then provider."""
    host = (account.get("imap_host") or "").strip()
    provider = (account.get("provider") or "yandex").strip().lower()
    default_host, default_port = PROVIDER_HOSTS.get(provider, ("", 993))
    host = str(host or default_host)
    try:
        port = int(account.get("imap_port") or default_port)
    except (TypeError, ValueError):
        port = default_port
    return host, port


def _connect(account: Dict[str, Any]) -> imaplib.IMAP4_SSL:
    host, port = resolve_host(account)
    if not host:
        raise ValueError("Не указан IMAP-сервер для аккаунта")
    email_addr = (account.get("email") or "").strip()
    password = account.get("password") or ""
    if not email_addr or not password:
        raise ValueError("Не указан адрес или пароль приложения")
    conn = imaplib.IMAP4_SSL(host, port, timeout=_CONNECT_TIMEOUT)
    try:
        conn.login(email_addr, password)
    except Exception:
        try:
            conn.logout()
        except Exception:
            pass
        raise
    return conn


def _decode_body(msg: Any) -> str:
    """Plain-text preview of a message (html stripped to a rough text)."""
    try:
        if msg.is_multipart():
            part = None
            for p in msg.walk():
                if p.get_content_type() == "text/plain":
                    part = p
                    break
            if part is None:
                for p in msg.walk():
                    if p.get_content_type() == "text/html":
                        part = p
                        break
        else:
            part = msg
        if part is None:
            return ""
        content = part.get_content()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            import re
            content = re.sub(r"<[^>]+>", " ", content)
        # Collapse all whitespace: the preview is fed to rules/LLM, not shown raw.
        return " ".join(content.split())[:_BODY_PREVIEW_CHARS]
    except Exception:
        return ""


def fetch_new_emails(account: Dict[str, Any], seen_ids: Set[str],
                     limit: int = 20) -> List[Dict[str, Any]]:
    """Newest unread messages of INBOX that are not in `seen_ids`.

    Blocking — run it in a worker thread. Fetches candidates newest-first and
    stops after `limit` new ones (or after `limit + 40` candidates, so a
    mailbox with hundreds of old unread letters does not stall the poller).
    """
    conn = _connect(account)
    out: List[Dict[str, Any]] = []
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return out
        ids = data[0].split()
        candidates = ids[::-1][: limit + 40]  # newest first
        for num in candidates:
            if len(out) >= limit:
                break
            uid = num.decode(errors="replace")
            if uid in seen_ids:
                continue
            typ, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data:
                continue
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            if raw is None:
                continue
            try:
                msg = email.message_from_bytes(raw, policy=email.policy.default)
            except Exception:
                continue
            from_name, from_addr = parseaddr(msg.get("From", ""))
            try:
                dt = parsedate_to_datetime(msg.get("Date", ""))
                ts = dt.timestamp() if dt else 0.0
            except Exception:
                ts = 0.0
            out.append({
                "uid": uid,
                "from_name": from_name or from_addr,
                "from_addr": from_addr.lower(),
                "subject": (msg.get("Subject") or "").strip(),
                "ts": ts,
                "body": _decode_body(msg),
            })
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def test_account(account: Dict[str, Any]) -> str:
    """Login + INBOX status; returns a human-readable success line.

    Raises on any failure with a readable message.
    """
    conn = _connect(account)
    try:
        typ, data = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise ConnectionError("Не удалось открыть папку INBOX")
        total = int(data[0]) if data and data[0] else 0
        typ, unseen = conn.search(None, "UNSEEN")
        unread = len(unseen[0].split()) if typ == "OK" and unseen and unseen[0] else 0
        return f"Вход выполнен, писем в INBOX: {total}, непрочитанных: {unread}"
    finally:
        try:
            conn.logout()
        except Exception:
            pass
