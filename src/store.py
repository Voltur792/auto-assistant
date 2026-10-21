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
from typing import Any, Callable, Dict, List, Optional

# Plugin root = parent of the src package.
PLUGIN_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _default_data_dir() -> Path:
    """Stable per-user data dir (survives plugin reinstalls)."""
    env = os.environ.get("AA_DATA_DIR")
    if env:
        return Path(env)
    if __import__("sys").platform == "darwin":
        from .platform_support import user_data
        return user_data() / "auto-assistant"
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

# Cap the digest so state.json stays small.
_MAX_DIGEST = 200

_LOCK = threading.RLock()

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "poll_interval_minutes": 5,
    "llm_enabled": True,
    "llm_endpoint": "",  # Empty: use Astra's selected localhost LM Studio profile.
    "llm_model": "",
    "mode": "whitelist_llm",   # whitelist_llm | whitelist | llm | rules
    "auto_handoff": False,
    "max_messages": 20,
    "handoff_hours": 24,
    # Token economy: mail the keyword rules call obvious ads never reaches
    # the LLM — most mail is ads, and the rules verdict for them is enough.
    "skip_ads_llm": True,
    # Publish important_email for the user's voice automation.
    "voice_announce": True,
    # Native widgets for deadlines: calendar | reminders | both.
    "deadline_mode": "calendar",
    "accounts": [],
}

DEFAULT_STATE: Dict[str, Any] = {
    # account_id -> {last_poll, last_error, new_count, last_uid}. last_uid is
    # the highest IMAP UID the plugin has accounted for — the mail baseline.
    # It replaces the old seen_uids list, which held IMAP *sequence numbers*:
    # those are positions that shift when mail is deleted or archived, so new
    # letters kept matching "already seen" and the poller reported nothing.
    "acct_status": {},  # Each cursor includes the INBOX uidvalidity namespace.
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
    if os.environ.get("AA_DATA_DIR"):
        return  # An explicit isolated directory must never import real passwords.
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
        # Legacy keys, dropped on write: seen_uids held IMAP sequence numbers
        # (unstable — see mailer.fetch_new_emails) and events belonged to the
        # removed Telegram bridge.
        state.pop("seen_uids", None)
        state.pop("events", None)
        if isinstance(state.get("digest"), list):
            state["digest"] = state["digest"][:_MAX_DIGEST]
        _write(STATE_FILE, state)


def update_state(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Read, change and persist the latest state under ONE lock.

    Callers must do network work before entering this short transaction.
    """
    with _LOCK:
        state = load_state()
        change(state)
        save_state(state)
        return state


def upgrade_workflow() -> None:
    """Revalidate cached proposals with current guards without creating records."""
    from . import triage
    from datetime import datetime
    with _LOCK:
        state = load_state()
        if state.get("workflow_version", 0) >= triage.VERSION:
            return
        rejected_ids = set()
        settings = load_settings()
        for item in state.get("digest") or []:
            if item.get("source") == "llm" and item.get("triage_version") in (3, triage.VERSION):
                row = {k: item.get(k) for k in ("importance", "summary", "reason", "tasks", "deadlines",
                                               "action_required", "confidence")}
                try:
                    refreshed = triage.parse(json.dumps({"items": [{"n": 1, **row}]}), [item],
                                             datetime.now().astimezone())[0]
                    item.update(refreshed)
                    item["requires_review"] = False
                    if not item.get("action_required"):
                        item["auto_pending"] = False
                        rejected_ids.add(item["id"])
                    elif (settings.get("auto_handoff") and not item.get("handled")
                            and not item.get("historical")
                            and triage.has_current_action(item, 0, datetime.now().astimezone())):
                        # Recover future/ongoing obligations previously excluded
                        # solely because their email was older than 48h.
                        item["auto_pending"] = True
                except ValueError:
                    item.update(action_required=False, tasks=[], reminders=[], deadlines=[],
                                importance="normal", requires_review=True, auto_pending=False)
                    rejected_ids.add(item["id"])
            elif item.get("source") == "rules" and item.get("triage_version") == 3:
                item.update(triage.rules(item.get("subject", ""), item.get("body", "")))
                item["requires_review"] = item["importance"] == "high"
                item["auto_pending"] = False
            elif not item.get("handled") and item.get("triage_version") != triage.VERSION:
                item.update(importance="normal", action_required=False,
                            tasks=[], reminders=[], deadlines=[], requires_review=True,
                            reason="Старый разбор: требуется повторная оценка")
                item.setdefault("mail_ts", 0)  # Old import timestamps are not receive dates.
            item.pop("handoff_plan", None)
            item.pop("handoff_completed", None)
        for job in state.get("widget_jobs") or []:
            if job.get("mail_id") in rejected_ids and job.get("status") in ("pending", "running"):
                job.update(status="cancelled", error="Действие не подтверждено повторной проверкой")
        for item in state.get("digest") or []:
            if (item.get("id") in rejected_ids and (item.get("importance") == "low"
                    or triage.information_reason(item.get("subject", ""), item.get("body", "")))):
                item["native_review"] = any(j.get("status") == "done" and item["id"] in
                    j.get("mail_ids", [j.get("mail_id")]) for j in state.get("widget_jobs") or [])
        state.update(workflow_version=triage.VERSION)
        state["handoff_pending"] = any(j.get("status") in ("pending", "running")
                                      for j in state.get("widget_jobs") or [])
        if not state.get("widget_jobs"):
            state.update(handoff_error="", handoff_answer="", handoff_pending=False)
        save_state(state)


def adopt_uidvalidity(state: Dict[str, Any], account_id: str, validity: str) -> None:
    """Adopt a namespace for old mail while keeping stable ids and progress."""
    for item in state.get("digest") or []:
        if item.get("account_id") == account_id and not item.get("uidvalidity"):
            uid = str(item.get("uid") or str(item.get("id", "")).rsplit(":", 1)[-1])
            if uid.isdigit():
                # Keep the stable id: an in-flight handoff may reference it.
                item.update(uid=uid, uidvalidity=validity)


def add_digest_item(item: Dict[str, Any]) -> bool:
    """Prepend a digest item (newest first) and persist.

    Returns True when the item is new. A letter re-fetched after a rewind or
    a failed fetch arrives with the same id — it REPLACES the old entry in
    place (the digest must not show one letter twice) and keeps `handled`, so
    a re-triaged letter does not ask Astra for the same tasks twice.
    """
    with _LOCK:
        state = load_state()
        digest = state.get("digest")
        if not isinstance(digest, list):
            digest = []
        for i, old in enumerate(digest):
            same_mail = (bool(item.get("uidvalidity"))
                         and old.get("account_id") == item.get("account_id")
                         and old.get("uidvalidity") == item.get("uidvalidity")
                         and str(old.get("uid")) == str(item.get("uid")))
            if old.get("id") == item.get("id") or same_mail:
                item["id"] = old["id"]
                if old.get("handled"):
                    item["handled"] = True
                for key in ("handoff_plan", "handoff_completed"):
                    if key in old:
                        item[key] = copy.deepcopy(old[key])
                digest[i] = item
                state["digest"] = digest
                save_state(state)
                return False
        digest.insert(0, item)
        state["digest"] = digest
        save_state(state)
        return True


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
