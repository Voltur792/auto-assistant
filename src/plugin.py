"""Авто-Помощник — Astra plugin: an automatic mail-aware assistant.

What it does:
* polls IMAP mailboxes (Yandex / Gmail / custom) on its own schedule;
* triages new mail using conservative rules and tool-free local inference;
* keeps a digest with proposed tasks/reminders, exposes it to Astra as tools;
* queues validated native widget records for the dom_access controller;
* fires the `important_email` trigger so the user's automations can react
  (for example, a voice announcement).
"""

import ast
import asyncio
import base64
import json
import logging
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from astra_plugin_sdk import (
    Plugin,
    UiContribution,
    action,
    tool,
    trigger,
    ui_call,
)

from . import analyze, inference, jobs, mailer, store, triage

logger = logging.getLogger(__name__)

_MODES = ("whitelist", "rules", "llm", "whitelist_llm")
_TICK_SECONDS = 20          # poller wake-up granularity


class AstraAutoAssistant(Plugin):
    """Astra plugin: auto-assistant."""

    def __init__(self) -> None:
        super().__init__()
        self._poller_task: Optional[asyncio.Task] = None
        self._manual_task: Optional[asyncio.Task] = None
        self._poll_lock: Optional[asyncio.Lock] = None  # created inside the loop
        self._last_summary = ""
        self._llm_error = ""   # human-readable reason the LLM triage is off
        self._inference_lock = asyncio.Lock()
        self._inference_retry_after = 0.0
        self._bridge_last_seen = 0.0
        self._handoff_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_config_changed(self, config: Dict[str, Any]) -> None:
        # The daemon delivers the initial config on start — the earliest
        # reliable place to start background work.
        store.upgrade_workflow()
        self._ensure_poller()

    async def health_check(self):
        # The daemon probes health periodically; a second chance to (re)start
        # the poller after a plugin reload.
        self._ensure_poller()
        return True, "ok"

    async def on_shutdown(self) -> None:
        if self._poller_task is not None and not self._poller_task.done():
            self._poller_task.cancel()
        if self._manual_task is not None and not self._manual_task.done():
            self._manual_task.cancel()

    def _ensure_poller(self) -> None:
        if self._poll_lock is None:
            self._poll_lock = asyncio.Lock()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._poller_task is None or self._poller_task.done():
            self._poller_task = loop.create_task(self._poll_loop())

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_TICK_SECONDS)
                settings = store.load_settings()
                if not settings.get("enabled", True):
                    continue
                try:
                    interval = float(settings.get("poll_interval_minutes", 5))
                except (TypeError, ValueError):
                    interval = 5.0
                interval = min(max(interval, 1.0), 1440.0)
                state = store.load_state()
                if time.time() - float(state.get("last_poll") or 0) >= interval * 60:
                    await self.poll_once(auto=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the loop die
                logger.error(f"poll loop tick failed: {e}")

    async def poll_once(self, auto: bool, historical: bool = False) -> str:
        """Check every enabled account once; triage what is new."""
        if self._poll_lock is None:
            # Normally created by _ensure_poller(); a UI call that wins the
            # race against the first config delivery must not crash.
            self._poll_lock = asyncio.Lock()
        if self._poll_lock.locked():
            return "Проверка почты уже выполняется — подожди немного."
        async with self._poll_lock:
            settings = store.load_settings()
            state = store.load_state()
            mode = settings.get("mode", "whitelist_llm")
            if mode not in _MODES:
                mode = "whitelist_llm"
            try:
                limit = int(settings.get("max_messages", 20))
            except (TypeError, ValueError):
                limit = 20

            new_items: List[Dict[str, Any]] = []
            errors: List[str] = []
            acct_updates: Dict[str, Any] = {}
            status_prev = state.get("acct_status") or {}
            baselined: List[str] = []
            for acct in settings.get("accounts") or []:
                if not acct.get("enabled", True):
                    continue
                acct_id = acct.get("id") or acct.get("email")
                prev = status_prev.get(acct_id) or {}
                if not (acct.get("email") and acct.get("password")):
                    acct_updates[acct_id] = {
                        **prev,
                        "last_poll": time.time(),
                        "last_error": "Не заполнен адрес или пароль приложения",
                        "new_count": 0,
                    }
                    continue
                # Baseline = highest IMAP UID already accounted for. Missing
                # means "never polled" and the first poll only learns where the
                # mailbox ends, so an install does not triage its whole history.
                baseline = prev.get("last_uid")
                try:
                    baseline = None if baseline is None else int(baseline)
                except (TypeError, ValueError):
                    baseline = None
                try:
                    msgs, uid_high, validity = await asyncio.to_thread(
                        mailer.fetch_new_emails, acct, baseline, max(limit, 1),
                        prev.get("uidvalidity"))
                except Exception as e:
                    errors.append(f"{acct.get('email')}: {e}")
                    acct_updates[acct_id] = {
                        **prev,
                        "last_poll": time.time(),
                        "last_error": str(e)[:300],
                        "new_count": 0,
                    }
                    continue
                if baseline is None or (prev.get("uidvalidity") is not None
                                        and str(prev["uidvalidity"]) != validity):
                    baselined.append(acct.get("email", ""))
                if prev.get("uidvalidity") is None:
                    # Adopt the first observed namespace without losing the
                    # handled/progress flags on existing, pre-upgrade mail.
                    def adopt(state, account_id=acct_id, uidvalidity=validity):
                        store.adopt_uidvalidity(state, account_id, uidvalidity)
                    store.update_state(adopt)
                fresh: List[Dict[str, Any]] = []
                for m in msgs:
                    # Bulk mail is marked, never dropped: a sender that looks
                    # automated is not a reason to hide a letter, and a filter
                    # that deletes mail silently reads as a broken poller.
                    m["bulk"] = analyze.is_bulk(m["from_addr"])
                    if mode in ("whitelist", "whitelist_llm") and \
                            not analyze.match_whitelist(acct, m["from_addr"],
                                                        m["from_name"]):
                        continue
                    m["account_id"] = acct_id
                    m["uidvalidity"] = validity
                    m["account_email"] = acct.get("email", "")
                    fresh.append(m)
                new_items.extend(fresh)
                acct_updates[acct_id] = {
                    **prev,
                    "last_poll": time.time(),
                    "last_error": "",
                    "new_count": len(fresh),
                    "last_uid": uid_high,
                    "uidvalidity": validity,
                }

            def mail_key(item):
                return (item.get("account_id"), str(item.get("uidvalidity")), str(item.get("uid")))
            fresh_keys = {mail_key(i) for i in new_items}
            live_ids = {a["id"] for a in settings.get("accounts") or [] if a.get("enabled", True)}
            pending_items = [] if historical else [i for i in store.load_state().get("digest") or []
                if (i.get("analysis_pending") or i.get("auto_pending")) and not i.get("handled")
                and not i.get("historical") and i.get("account_id") in live_ids
                and (triage.is_recent(i, float(settings.get("handoff_hours", 24)), time.time())
                     or triage.has_current_action(i, 0, datetime.now().astimezone()))]
            retry_keys = {mail_key(i) for i in pending_items}
            retries = [i for i in pending_items if mail_key(i) not in fresh_keys][:6]
            work_items = new_items + retries
            triaged = await self._triage_items(work_items, settings) if work_items else []
            new_actionable = []
            for it in triaged:
                if it["account_id"] not in {a["id"] for a in store.load_settings()["accounts"]}:
                    continue
                digest_item = {
                    **it, "id": f"{it['account_id']}:{it['uidvalidity']}:{it['uid']}",
                    "mail_ts": it.get("mail_ts", it.get("ts")) or 0, "ts": time.time(),
                    "historical": historical,
                }
                eligible = (not historical and triage.has_current_action(digest_item,
                    float(settings.get("handoff_hours", 24)), datetime.now().astimezone()))
                digest_item["auto_pending"] = bool(eligible and settings.get("auto_handoff")
                    and (it.get("analysis_pending") or it.get("action_required")))
                is_new = store.add_digest_item(digest_item)
                if ((is_new or mail_key(it) in retry_keys) and digest_item.get("action_required") and eligible):
                    new_actionable.append(digest_item)

            def finish_poll(state):
                state["last_poll"] = time.time()
                state["last_error"] = "; ".join(errors)[:500]
                live_ids = {a["id"] for a in store.load_settings()["accounts"]}
                status = state.setdefault("acct_status", {})
                status.update({k: v for k, v in acct_updates.items() if k in live_ids})
                state["acct_status"] = {k: v for k, v in status.items() if k in live_ids}
            store.update_state(finish_poll)

            high = [t for t in triaged if t.get("importance") == "high"]
            note = ""
            if baselined:
                note = (" Точка отсчёта установлена — дальше сообщаю только о "
                        "письмах, пришедших после неё.")
            self._last_summary = (
                f"Почта проверена: новых писем {len(new_items)}, "
                f"важных {len(high)}." + note + (f" Ошибки: {errors[0]}" if errors else "")
            )
            try:
                await self.push_to_ui("assistant", {"type": "updated"})
            except Exception:
                pass
            if (new_actionable and settings.get("auto_handoff")
                    and not self._llm_error and not historical):
                await self._handoff(new_actionable)
            if new_actionable and not self._llm_error and settings.get("voice_announce", True):
                for item in new_actionable:
                    await self._fire_important(item)
            return self._last_summary

    async def _fire_important(self, item: Dict[str, Any]) -> None:
        """Start the user's automations bound to our trigger."""
        try:
            await self.fire_trigger("important_email", {
                "subject": item.get("subject", ""),
                "from": item.get("from_name", "") or item.get("from_addr", ""),
                "from_addr": item.get("from_addr", ""),
                "summary": item.get("summary", ""),
                "importance": item.get("importance", "high"),
                "account": item.get("account_email", ""),
            })
        except Exception as e:
            logger.warning(f"fire_trigger(important_email) failed: {e}")

    # ------------------------------------------------------------------
    # Tool-free localhost inference
    # ------------------------------------------------------------------

    async def _triage_items(self, items: List[Dict[str, Any]], settings: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Read-only inference; an HTTP result is data and never a tool call."""
        out = [{**it, **triage.rules(it.get("subject", ""), it.get("body", "")),
                "analysis_pending": False} for it in items]
        use_llm = settings.get("llm_enabled", True) and settings.get("mode") in ("llm", "whitelist_llm")
        self._llm_error = ""
        if not use_llm:
            return out
        candidates = [i for i, item in enumerate(out)
                      if not (settings.get("skip_ads_llm", True) and item["importance"] == "low")]
        if not candidates:
            return out
        for i in candidates:
            out[i]["analysis_pending"] = True
        if time.time() < self._inference_retry_after:
            self._llm_error = "Разбор моделью временно приостановлен после сбоя."
            return out
        async with self._inference_lock:
            if time.time() < self._inference_retry_after:
                self._llm_error = "Разбор моделью временно приостановлен после сбоя."
                return out
            for offset in range(0, len(candidates), 3):
                indices = candidates[offset:offset + 3]
                now = datetime.now().astimezone()
                batch = []
                for i in indices:
                    item = dict(items[i])
                    stamp = item.get("mail_ts", item.get("ts"))
                    item["received_at"] = datetime.fromtimestamp(stamp).astimezone().isoformat() if stamp else "unknown"
                    batch.append(item)
                try:
                    answer = await asyncio.wait_for(asyncio.to_thread(
                        inference.complete, batch, settings, now.isoformat()), timeout=inference.TIMEOUT + 5)
                    parsed = triage.parse(answer, batch, now)
                except (inference.InferenceError, ValueError, asyncio.TimeoutError):
                    self._llm_error = ("Локальная модель не завершила корректный разбор. "
                                       "Автосоздание по этой проверке отменено; повтор через 5 минут.")
                    self._inference_retry_after = time.time() + 300
                    break
                for i, item in zip(indices, parsed):
                    out[i] = {**item, "analysis_pending": False, "requires_review": False}
        return out

    @staticmethod
    def _unhandled_high(hours: float) -> List[Dict[str, Any]]:
        """High digest items whose tasks were not requested from Astra yet."""
        return [i for i in store.load_state().get("digest", [])
                if i.get("importance") == "high" and not i.get("handled")
                and triage.has_current_action(i, hours, datetime.now().astimezone())]

    async def _handoff(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Queue validated records for the explicitly authorised DOM controller."""
        if self._handoff_lock.locked():
            return {"success": False, "error": "Создание записей уже выполняется"}
        async with self._handoff_lock:
            settings = store.load_settings()
            candidates = [i for i in items if not i.get("handled")]
            if any(i.get("triage_version") != triage.VERSION for i in candidates):
                candidates = await self._triage_items(candidates[:6], settings)
                for item in candidates:
                    store.add_digest_item(item)
                if self._llm_error:
                    return {"success": False, "error": self._llm_error}
            ready = [i for i in candidates if i.get("action_required") and i.get("tasks")
                     and i.get("triage_version") == triage.VERSION]
            if not ready:
                return {"success": False, "error": "Нет подтверждённых актуальных действий по письмам."}
            mode = settings.get("deadline_mode", "calendar")
            count = jobs.enqueue(ready, mode if mode in analyze.DEADLINE_MODES else "calendar")
            return {"success": True, "pending": bool(store.load_state().get("handoff_pending")),
                    "answer": f"Подготовлено записей для штатных виджетов Astra: {count}."}

    @ui_call("aa_widget_jobs")
    async def ui_widget_jobs(self, worker: str = "", **params: Any) -> Dict[str, Any]:
        if not worker or len(worker) > 80:
            return {"jobs": []}
        self._bridge_last_seen = time.time()
        claimed = jobs.claim(worker)
        try:
            revision = store.STATE_FILE.stat().st_mtime_ns
        except OSError:
            revision = 0
        return {"jobs": claimed, "revision": revision,
                "polling": self._poll_lock is not None and self._poll_lock.locked()}

    @ui_call("aa_widget_result")
    async def ui_widget_result(self, id: str = "", lease: str = "",
                               success: bool = False, error: str = "", **params: Any) -> Dict[str, Any]:
        accepted = jobs.finish(id, lease, success is True, error)
        return {"success": accepted}

    @ui_call("aa_retriage")
    async def ui_retriage(self, **params: Any) -> Dict[str, Any]:
        items = [i for i in store.load_state().get("digest", []) if not i.get("handled")][:6]
        parsed = await self._triage_items(items, store.load_settings())
        for item in parsed:
            store.add_digest_item(item)
        return {"success": not bool(self._llm_error), "error": self._llm_error,
                "report": f"Переразобрано писем: {len(parsed)}. Создание записей не запускалось."}

    # ------------------------------------------------------------------
    # Tools (Astra calls these in chat)
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_args(kwargs: str, named: Dict[str, Any]) -> Dict[str, Any]:
        """Parse the legacy/bare `kwargs` string the Astra model sometimes
        sends instead of named arguments (JSON object, python literal, bare
        value). Named arguments win."""
        merged = {k: v for k, v in named.items() if v not in (None, "")}
        if kwargs:
            parsed: Any = None
            try:
                parsed = json.loads(kwargs)
            except (json.JSONDecodeError, TypeError):
                try:
                    parsed = ast.literal_eval(kwargs)
                except (ValueError, SyntaxError, TypeError, MemoryError):
                    parsed = None
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    merged.setdefault(k, v)
            elif isinstance(parsed, str):
                merged.setdefault("_raw", parsed)
            elif parsed is None:
                merged.setdefault("_raw", kwargs)
        return merged

    @tool("Get the assistant's mail digest: important letters from the last "
          "`hours` hours with summaries, proposed tasks and reminders (JSON: "
          "{'items': [...]}). Call this when the user asks what came by mail, "
          "what is important, or what to do today.")
    async def assistant_get_digest(self, hours: int = 24,
                                   kwargs: str = "") -> Dict[str, Any]:
        args = self._merge_args(kwargs, {"hours": hours})
        try:
            hours_f = float(args.get("hours", 24))
        except (TypeError, ValueError):
            hours_f = 24.0
        items = store.recent_digest(min(max(hours_f, 0.1), 720))
        out = [{
            "from": i.get("from_name") or i.get("from_addr"),
            "subject": i.get("subject"),
            "importance": i.get("importance"),
            "summary": i.get("summary"),
            "tasks": i.get("tasks"),
            "reminders": i.get("reminders"),
            "handled": bool(i.get("handled")),
            "time": time.strftime("%d.%m %H:%M", time.localtime(i.get("ts", 0))),
        } for i in items[:30]]
        return {"items": out, "count": len(out)}

    @tool("Check all connected mailboxes right now, triage new mail and "
          "return a short text report. Call when the user asks to refresh "
          "mail or check for important letters.")
    async def assistant_check_mail(self, kwargs: str = "") -> str:
        return await self.poll_once(auto=False)

    @tool("Hand the current digest of important letters to the assistant "
          "logic: asks to create tasks and reminders from them. Letters "
          "already handed off are skipped (their tasks were already "
          "requested). Returns what was created. Call when the user says "
          "'создай задачи из почты'.")
    async def assistant_handoff(self, kwargs: str = "") -> Dict[str, Any]:
        items = self._unhandled_high(48)
        if not items:
            return {"success": False,
                    "error": "Нет необработанных важных писем за последние "
                             "48 часов (задачи по остальным уже запрошены)"}
        return await self._handoff(items)

    # ------------------------------------------------------------------
    # Actions (steps in the user's automations)
    # ------------------------------------------------------------------

    @action("Дайджест почты сейчас",
            icon_svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                     '<path fill="currentColor" d="M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16'
                     'a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 4-8 5-8-5V6l8 5 8-5z"/></svg>',
            fields=[],
            ai_available=True,
            ai_description="Check mail now and return the assistant's digest report",
            ai_primary_field="")
    async def digest_now(self, **extra: Any) -> Dict[str, Any]:
        return {"report": await self.poll_once(auto=False)}

    # ------------------------------------------------------------------
    # Triggers (a user automation can start from)
    # ------------------------------------------------------------------

    @trigger("Важное письмо найдено",
             icon_svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                      '<path fill="currentColor" d="M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16'
                      'a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 4-8 5-8-5V6l8 5 8-5z"/></svg>',
             fields=[])
    def important_email(self) -> None:
        """Holds the trigger metadata; fired from poll_once()."""

    # ------------------------------------------------------------------
    # UI page + calls
    # ------------------------------------------------------------------

    async def get_ui_contributions(self) -> List[UiContribution]:
        return [self.ui_effect("controller.js", id="native-mail-assistant"),
                self.ui_page("auto-assistant", "Помощник", "page.js", icon_svg=
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                    '<path fill="none" stroke="currentColor" stroke-width="1.8" '
                    'd="M3 5h18v14H3zM3 6l9 7 9-7"/></svg>')]

    @ui_call("aa_get_state")
    async def ui_get_state(self, **params: Any) -> Dict[str, Any]:
        settings = store.load_settings()
        state = store.load_state()
        acct_status = state.get("acct_status") or {}
        accounts = []
        for a in settings.get("accounts") or []:
            b = dict(a)
            b["has_password"] = bool(b.get("password"))
            b.pop("password", None)
            b["status"] = acct_status.get(b.get("id")) or {}
            accounts.append(b)
        interval = settings.get("poll_interval_minutes", 5)
        try:
            next_in = max(0.0, float(state.get("last_poll") or 0) +
                          float(interval) * 60 - time.time())
        except (TypeError, ValueError):
            next_in = 0.0
        return {
            "settings": {**settings, "accounts": accounts},
            "digest": (state.get("digest") or [])[:50],
            "status": {
                "last_poll": state.get("last_poll") or 0,
                "last_error": state.get("last_error") or "",
                "llm_error": self._llm_error,
                "native_bridge_ready": time.time() - self._bridge_last_seen < 15,
                "handoff_pending": bool(state.get("handoff_pending")),
                "handoff_error": state.get("handoff_error") or "",
                "handoff_answer": state.get("handoff_answer") or "",
                "next_in_seconds": round(next_in),
                "poller_alive": self._poller_task is not None
                                and not self._poller_task.done(),
                "polling": self._poll_lock is not None and self._poll_lock.locked(),
                "last_summary": self._last_summary,
            },
        }

    @ui_call("aa_get_icon")
    async def ui_get_icon(self, **params: Any) -> Dict[str, Any]:
        # UI assets are served from ui/; ../icon.png is outside that route.
        icon_path = Path(__file__).resolve().parent.parent / "icon.png"
        data = base64.b64encode(icon_path.read_bytes()).decode("ascii")
        return {"data_uri": f"data:image/png;base64,{data}"}

    @ui_call("aa_save_settings")
    async def ui_save_settings(self, **params: Any) -> Dict[str, Any]:
        new = params.get("settings") or {}
        old = store.load_settings()
        settings = dict(old)
        settings["enabled"] = bool(new.get("enabled", old.get("enabled", True)))
        mode = new.get("mode", old.get("mode", "whitelist_llm"))
        settings["mode"] = mode if mode in _MODES else "whitelist_llm"
        settings["llm_enabled"] = bool(new.get("llm_enabled",
                                               old.get("llm_enabled", True)))
        settings["auto_handoff"] = bool(new.get("auto_handoff",
                                                 old.get("auto_handoff", False)))
        settings["skip_ads_llm"] = bool(new.get("skip_ads_llm",
                                                old.get("skip_ads_llm", True)))
        settings["voice_announce"] = bool(new.get("voice_announce",
                                                  old.get("voice_announce", True)))
        dm = new.get("deadline_mode", old.get("deadline_mode", "calendar"))
        settings["deadline_mode"] = dm if dm in analyze.DEADLINE_MODES \
            else "calendar"
        try:
            interval = int(float(new.get("poll_interval_minutes",
                                         old.get("poll_interval_minutes", 5))))
        except (TypeError, ValueError):
            interval = 5
        settings["poll_interval_minutes"] = min(max(interval, 1), 1440)
        try:
            mx = int(float(new.get("max_messages", old.get("max_messages", 20))))
        except (TypeError, ValueError):
            mx = 20
        settings["max_messages"] = min(max(mx, 1), 100)

        for key in ("llm_endpoint", "llm_model"):
            settings[key] = str(new.get(key, old.get(key, ""))).strip()[:300]
        old_accounts = {a.get("id"): a for a in old.get("accounts") or []}
        accounts = []
        id_changes = {}
        for i, a in enumerate(new.get("accounts") or []):
            if not isinstance(a, dict):
                continue
            acct_id = a.get("id") or f"acct{i}"
            previous = old_accounts.get(acct_id) or {}
            changed_mailbox = bool(previous) and (
                mailer.mailbox_identity(previous) != mailer.mailbox_identity(a))
            password = (a.get("password") or "").strip()
            if changed_mailbox:
                new_id = "acct" + uuid.uuid4().hex
                id_changes[acct_id] = new_id
                acct_id = new_id
            if not password and not changed_mailbox:
                # Empty field in the UI means "keep the saved password".
                password = (old_accounts.get(acct_id) or {}).get("password", "")
            wl = a.get("whitelist")
            if isinstance(wl, str):
                wl = [x.strip() for x in wl.replace(",", "\n").splitlines()
                      if x.strip()]
            accounts.append({
                "id": acct_id,
                "provider": a.get("provider") or "yandex",
                "email": (a.get("email") or "").strip(),
                "password": password,
                "imap_host": (a.get("imap_host") or "").strip(),
                "imap_port": a.get("imap_port") or 993,
                "enabled": bool(a.get("enabled", True)),
                "whitelist": list(wl or []),
            })
        settings["accounts"] = accounts
        store.save_settings(settings)
        def prune_status(state):
            live_ids = {a["id"] for a in accounts}
            state["acct_status"] = {k: v for k, v in state.get("acct_status", {}).items()
                                    if k in live_ids}
        store.update_state(prune_status)
        self._ensure_poller()
        # The client must adopt new ids and discard successfully saved passwords.
        return {"success": True, "settings": (await self.ui_get_state())["settings"],
                "account_ids": id_changes}

    @ui_call("aa_test_account")
    async def ui_test_account(self, **params: Any) -> Dict[str, Any]:
        acct = params.get("account") or {}
        if not (acct.get("password") or "").strip():
            saved = {a.get("id"): a for a in
                     store.load_settings().get("accounts") or []}
            previous = saved.get(acct.get("id")) or {}
            if previous and mailer.mailbox_identity(previous) != mailer.mailbox_identity(acct):
                return {"error": "Для нового адреса или IMAP-сервера укажи пароль приложения."}
            acct["password"] = previous.get("password", "")
        try:
            message = await asyncio.to_thread(mailer.test_account, acct)
            return {"success": True, "message": message}
        except Exception as e:
            return {"error": str(e)}

    @ui_call("aa_run_digest")
    async def ui_run_digest(self, **params: Any) -> Dict[str, Any]:
        return self._start_manual_poll()

    def _start_manual_poll(self, historical: bool = False) -> Dict[str, Any]:
        if ((self._manual_task is not None and not self._manual_task.done())
                or self._poll_lock is not None and self._poll_lock.locked()):
            return {"success": False, "error": "Проверка почты уже выполняется."}
        async def run():
            try:
                await self.poll_once(auto=False, historical=historical)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Manual mail check failed: %s", type(exc).__name__)
                store.update_state(lambda state: state.update(last_error="Проверка прервана; см. журнал плагина."))
        self._manual_task = asyncio.create_task(run())
        return {"success": True, "report": "Проверка запущена. Результат появится в дайджесте."}

    @ui_call("aa_handoff")
    async def ui_handoff(self, **params: Any) -> Dict[str, Any]:
        items = self._unhandled_high(48)
        if not items:
            return {"error": "Нет подтверждённых действий из свежих писем или писем с будущим сроком. "
                             "При необходимости разбери последние письма заново."}
        return await self._handoff(items)

    @ui_call("aa_rewind_baseline")
    async def ui_rewind_baseline(self, **params: Any) -> Dict[str, Any]:
        if self._poll_lock is None:
            self._poll_lock = asyncio.Lock()
        if (self._poll_lock.locked() or self._manual_task is not None and not self._manual_task.done()):
            return {"error": "Проверка почты уже выполняется — подожди немного."}
        async with self._poll_lock:
            result = await self._rewind_baseline(**params)
        # poll_once acquires this same lock, so run it only after releasing it.
        if result.get("success"):
            result["report"] = self._start_manual_poll(historical=True).get("report", "")
        return result

    async def _rewind_baseline(self, **params: Any) -> Dict[str, Any]:
        """Re-triage the newest `back` unread letters of every enabled account.

        The mail baseline sits at the end of the mailbox (see
        mailer.fetch_new_emails), which is right for an install but means mail
        that predates the plugin is never mentioned. Rewinding is bounded, so a
        mailbox with twenty thousand unread letters cannot become twenty
        thousand LLM calls.
        """
        try:
            back = int(params.get("back") or 20)
        except (TypeError, ValueError):
            back = 20
        settings = store.load_settings()
        updates: Dict[str, Any] = {}
        moved: List[str] = []
        errors: List[str] = []
        for acct in settings.get("accounts") or []:
            if not acct.get("enabled", True):
                continue
            if not (acct.get("email") and acct.get("password")):
                continue
            acct_id = acct.get("id") or acct.get("email")
            try:
                base, validity = await asyncio.to_thread(mailer.rewind_baseline, acct, back)
            except Exception as e:
                errors.append(f"{acct.get('email')}: {str(e)[:200]}")
                continue
            if base is None:
                continue
            updates[acct_id] = {"last_uid": int(base), "uidvalidity": validity}
            moved.append(acct.get("email", ""))
        if not moved:
            note = f" Ошибки: {errors[0]}" if errors else ""
            return {"error": "Нечего показывать — непрочитанных писем нет." + note}
        def rewind(state):
            live_ids = {a["id"] for a in store.load_settings()["accounts"]}
            status = state.setdefault("acct_status", {})
            for acct_id, update in updates.items():
                if acct_id in live_ids:
                    if status.get(acct_id, {}).get("uidvalidity") is None:
                        store.adopt_uidvalidity(state, acct_id, update["uidvalidity"])
                    status[acct_id] = {**status.get(acct_id, {}), **update}
        store.update_state(rewind)
        return {"success": True, "accounts": moved,
                "errors": errors}

    @ui_call("aa_clear_digest")
    async def ui_clear_digest(self, **params: Any) -> Dict[str, Any]:
        store.clear_digest()
        return {"success": True}


if __name__ == "__main__":
    try:
        print("Starting AstraAutoAssistant plugin...", file=sys.stdout, flush=True)
        AstraAutoAssistant().run()
    except Exception as e:
        print(f"Failed to start plugin: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(1)
