"""Allow-list and conservative rules. No conversation or action prompts."""
from typing import Any, Dict
from .triage import rules

DEADLINE_MODES = ("calendar", "reminders", "both")
_BULK_WORDS = ("newsletter@", "mailing@", "promo@", "marketing@", "ads@", "sales-notif@", "subscribe@")


def classify_rules(subject: str, body: str) -> Dict[str, Any]:
    return rules(subject, body)


def match_whitelist(account: Dict[str, Any], from_addr: str,
                    from_name: str) -> bool:
    """True when the sender matches any allow-list entry (substring, case-insensitive).

    An empty whitelist means "allow" — the caller decides what an empty list
    implies for the chosen mode.
    """
    patterns = [p.strip().casefold() for p in (account.get("whitelist") or [])
                if p and p.strip()]
    if not patterns:
        return True
    hay = f"{from_addr} {from_name}".casefold()
    return any(p in hay for p in patterns)


def is_bulk(from_addr: str) -> bool:
    """True for addresses that are bulk mail by construction (newsletters)."""
    addr = from_addr.casefold()
    return any(w in addr for w in _BULK_WORDS)
