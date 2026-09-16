"""Persistence for settings and the mail digest.

The SDK has no KV store (see the Astra plugin guide), so everything lives as
JSON files. WHERE they live matters: the plugin folder is wiped on every
reinstall, and mail providers show an app password exactly once — losing it
means creating a new one per mailbox. So data lives OUTSIDE the plugin, in a
stable per-user dir that survives reinstalls:

* Windows: %APPDATA%\auto-assistant
* Linux:   $XDG_DATA_HOME/auto-assistant (or ~/.local/share/...)
* AA_DATA_DIR env var overrides everything (tests, portable setups).

Migrations on first run, best-effort, never overwriting existing data:
* <= 0.1.0 wrote into the plugin's own ``data/`` folder;
* the pre-release id was ``astra-auto-assistant`` and its stable dir
  ``%APPDATA%\astra-auto-assistant`` — both are migrated so an upgrade
  keeps saved passwords.
"""

import copy
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Plugin root = parent of the src package.
PLUGIN_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _default_data_dir() -> Path:
    """Stable per-user data dir (survives plugin reinstalls)."""
    env = os.environ.get("AA_DATA_DIR")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "auto-assistant"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "auto-assistant"
    return Path.home() / ".local" / "share" / "auto-assistant"


DATA_DIR = _default_data_dir()
SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "state.json"

# Older data locations, migrated on first run (never overwriting):
# * <= 0.1.0 kept settings inside the plugin's own ``data/`` folder;
# * the pre-release plugin id was ``astra-auto-assistant`` and its stable
#   dir was named after it — the rename must not cost the user their
#   app passwords.
_LEGACY_DATA_DIR = PLUGIN_ROOT / "data"
_LEGACY_ID_DATA_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "astra-auto-assistant"
    if os.environ.get("APPDATA") else None,
    Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    / "astra-auto-assistant",
]
_migrated = False

# Cap growing lists so the JSON files stay small.
_MAX_SEEN_PER_ACCOUNT = 500
_MAX_DIGEST = 200

_LOCK = threading.RLock()

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "poll_interval_minutes": 5,
    "llm_enabled": True,
    "mode": "whitelist_llm",   # whitelist_llm | whitelist | llm | rules
    "auto_handoff": False,
    "max_messages": 20,
    "handoff_hours": 24,
    # Token economy: mail the keyword rules call obvious ads never reaches
    # the LLM — most mail is ads, and the rules verdict for them is enough.
    "skip_ads_llm": True,
    # Speak Astra's handoff reply aloud (PluginChatRequest.voice_enabled) —
    # the voice announcement the user asked for.
    "voice_announce": True,
    # Where deadlines land: calendar (core:add_calendar_event) | reminders
    # (core:add_reminder) | both. calendar.json holds {date, text, time}.
    "deadline_mode": "calendar",
    "accounts": [],
}

DEFAULT_STATE: Dict[str, Any] = {
    "seen_uids": {},     # account_id -> [uid, ...]
    "acct_status": {},   # account_id -> {last_poll, last_error, new_count}
    "digest": [],        # [{id, account_id, account_email, from_addr, from_name, subject, ts, importance, summary, tasks[], reminders[], source}]
    "last_poll": 0.0,
    "last_error": "",
}


def _read(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    # Deep copy: a shallow dict(default) would share the nested lists with the
    # module-level DEFAULT_* constants, and callers mutate them in place.
    merged = copy.deepcopy(default)
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                merged.update(data)
    except Exception:
        pass
    return merged


def _write(path: Path, data: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows/antivirus sometimes holds the .tmp file for a moment (WinError 5
    # on replace) — retry a few times before giving up.
    last_exc: Optional[Exception] = None
    for attempt in range(4):
        try:
            tmp.replace(path)
            return
        except PermissionError as e:
            last_exc = e
            time.sleep(0.15 * (attempt + 1))
    if last_exc is not None:
        raise last_exc


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Small cache so the poller does not re-read settings.json every cycle.
_settings_cache: Optional[Dict[str, Any]] = None


def _ensure_migrated() -> None:
    """One-time move of older data locations into the stable dir.
    Never overwrites data already there."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        if SETTINGS_FILE.exists() or STATE_FILE.exists():
            return  # stable dir already has data — it wins
        sources = [_LEGACY_DATA_DIR] + [
            d for d in _LEGACY_ID_DATA_DIRS if d
        ]
        for src_dir in sources:
            if not src_dir.exists():
                continue
            for name in ("settings.json", "state.json"):
                src = src_dir / name
                if src.exists() and not (DATA_DIR / name).exists():
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, DATA_DIR / name)
    except Exception:
        pass  # migration is best-effort; defaults apply on failure


def load_settings() -> Dict[str, Any]:
    global _settings_cache
    with _LOCK:
        _ensure_migrated()
        s = _read(SETTINGS_FILE, DEFAULT_SETTINGS)
        # Normalize: every account gets an id and sane defaults.
        accounts = []
        for i, a in enumerate(s.get("accounts") or []):
            if not isinstance(a, dict):
                continue
            a.setdefault("id", f"acct{i}")
            a.setdefault("provider", "yandex")
            a.setdefault("email", "")
            a.setdefault("password", "")
            a.setdefault("imap_host", "")
            a.setdefault("imap_port", 993)
            a.setdefault("enabled", True)
            a.setdefault("whitelist", [])
            accounts.append(a)
        s["accounts"] = accounts
        _settings_cache = s
        return s


def save_settings(settings: Dict[str, Any]) -> None:
    global _settings_cache
    with _LOCK:
        _write(SETTINGS_FILE, settings)
        _settings_cache = settings


# ---------------------------------------------------------------------------
# State (seen uids, digest)
# ---------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    with _LOCK:
        _ensure_migrated()
        return _read(STATE_FILE, DEFAULT_STATE)


def save_state(state: Dict[str, Any]) -> None:
    with _LOCK:
        # Cap the growing lists so state.json stays small.
        seen = state.get("seen_uids") or {}
        for acct, uids in seen.items():
            if isinstance(uids, list) and len(uids) > _MAX_SEEN_PER_ACCOUNT:
                seen[acct] = uids[-_MAX_SEEN_PER_ACCOUNT:]
        state["seen_uids"] = seen
        if isinstance(state.get("digest"), list):
            state["digest"] = state["digest"][:_MAX_DIGEST]
        _write(STATE_FILE, state)


def add_digest_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Prepend a digest item (newest first) and persist."""
    with _LOCK:
        state = load_state()
        digest = state.get("digest")
        if not isinstance(digest, list):
            digest = []
        digest.insert(0, item)
        state["digest"] = digest
        save_state(state)
        return state


def clear_digest() -> None:
    with _LOCK:
        state = load_state()
        state["digest"] = []
        save_state(state)


def mark_digest_handled(ids: List[str]) -> None:
    """Mark digest items as handled (tasks already requested) so a later
    handoff does not re-send them."""
    with _LOCK:
        state = load_state()
        idset = set(ids)
        changed = False
        for item in state.get("digest") or []:
            if item.get("id") in idset and not item.get("handled"):
                item["handled"] = True
                changed = True
        if changed:
            save_state(state)


def is_seen(account_id: str, uid: str) -> bool:
    with _LOCK:
        state = load_state()
        return uid in (state.get("seen_uids", {}).get(account_id) or [])


def mark_seen(account_id: str, uids: List[str]) -> None:
    with _LOCK:
        state = load_state()
        seen = state.setdefault("seen_uids", {})
        cur = set(seen.get(account_id) or [])
        cur.update(uids)
        seen[account_id] = list(cur)[-_MAX_SEEN_PER_ACCOUNT:]
        save_state(state)


def recent_digest(hours: float, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    cutoff = time.time() - hours * 3600
    with _LOCK:
        state = load_state()
    out = []
    for item in state.get("digest", []):
        if item.get("ts", 0) >= cutoff:
            if account_id is None or item.get("account_id") == account_id:
                out.append(item)
    return out
