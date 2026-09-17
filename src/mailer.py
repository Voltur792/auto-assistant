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
from typing import Any, Dict, List, Optional

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
            # Style/script blocks and comments are not text: stripping tags
            # alone leaves the CSS in the preview (and in the LLM prompt).
            import re
            content = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ",
                             content)
            content = re.sub(r"(?s)<!--.*?-->", " ", content)
            content = re.sub(r"<[^>]+>", " ", content)
        # Collapse all whitespace: the preview is fed to rules/LLM, not shown raw.
        return " ".join(content.split())[:_BODY_PREVIEW_CHARS]
    except Exception:
        return ""


def _uids(typ: Any, data: Any) -> List[int]:
    """Parse a SEARCH/UID SEARCH response into sorted ints."""
    if typ != "OK" or not data or not data[0]:
        return []
    try:
        return sorted(int(x) for x in data[0].split())
    except ValueError:
        return []


def fetch_new_emails(account: Dict[str, Any], since_uid: Optional[int],
                     limit: int = 20) -> "tuple[List[Dict[str, Any]], int]":
    """Unread INBOX messages with UID > `since_uid`, oldest first.

    Blocking — run it in a worker thread.

    UIDs, never sequence numbers: a sequence number is a *position* in the
    mailbox and it shifts down every time anything is deleted or archived, so
    a "already seen" list built from them quietly swallows new mail — with a
    mailbox of ~20k messages the newest letters kept landing on numbers the
    plugin had stored weeks earlier, and every poll reported "nothing new".
    A UID is permanent for the life of the mailbox.

    `since_uid=None` means "never polled": nothing is returned and the second
    element is the mailbox's current highest UID, which the caller stores as
    the baseline so a fresh install does not spend its first polls triaging
    years of old unread mail.

    Returns `(messages, uid_high)` — `uid_high` is what to store as the new
    baseline. At most `limit` messages come back, oldest first, so a flood is
    worked through over several polls instead of the older half being dropped.
    Messages are read with BODY.PEEK, so nothing is ever marked as read.
    """
    conn = _connect(account)
    out: List[Dict[str, Any]] = []
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("search", "ALL")
        mailbox_max = max(_uids(typ, data), default=0)
        if since_uid is None:
            return out, mailbox_max
        since = int(since_uid)
        typ, data = conn.uid("search", "UNSEEN")
        fresh = [u for u in _uids(typ, data) if u > since][: max(limit, 1)]
        for uid in fresh:
            typ, msg_data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
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
                "uid": str(uid),
                "from_name": from_name or from_addr,
                "from_addr": from_addr.lower(),
                "subject": (msg.get("Subject") or "").strip(),
                "ts": ts,
                "body": _decode_body(msg),
            })
        return out, (max(fresh) if fresh else since)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def rewind_baseline(account: Dict[str, Any], back: int = 20) -> Optional[int]:
    """A baseline that makes the newest `back` unread letters look new again.

    The mail baseline sits at the end of the mailbox, so letters that were
    already there when the plugin started are never reported — correct for an
    install, but it leaves the tab empty after a fix or a "clear". This
    returns the UID to rewind to so exactly the newest `back` unread letters
    come back (bounded by design: a mailbox with 20k unread letters must not
    turn into 20k LLM calls). None when the mailbox has no unread letters.
    """
    conn = _connect(account)
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("search", "UNSEEN")
        unseen = _uids(typ, data)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    if not unseen:
        return None
    back = max(1, min(int(back), 100))
    return unseen[0] - 1 if len(unseen) <= back else unseen[-back - 1]


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
