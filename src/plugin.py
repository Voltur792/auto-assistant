"""Авто-Помощник — Astra plugin: an automatic mail-aware assistant.

What it does:
* polls IMAP mailboxes (Yandex / Gmail / custom) on its own schedule;
* triages new mail — allow-list, keyword rules, or Astra's own LLM asked
  through SendChatMessage (no extra API keys — the model Astra is configured
  with does the thinking);
* keeps a digest with proposed tasks/reminders, exposes it to Astra as tools;
* asks Astra (in its own plugin conversation) to create real tasks/reminders
  when auto-handoff is on, or when the user presses the button;
* fires the `important_email` trigger so the user's automations can react
  (for example, a voice announcement).
"""

import ast
import asyncio
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from astra_plugin_sdk import (
    Field,
    Plugin,
    UiContribution,
    action,
    tool,
    trigger,
    ui_call,
    ui_page,
)

from . import analyze, mailer, store

logger = logging.getLogger(__name__)

_MODES = ("whitelist", "rules", "llm", "whitelist_llm")
_TICK_SECONDS = 20          # poller wake-up granularity
_LLM_TIMEOUT_SECONDS = 180  # one Astra round-trip


@ui_page(
    "auto-assistant",
    "Помощник",
    "assistant.html",
    icon_svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
             '<path fill="currentColor" d="M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16'
             'a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 4-8 5-8-5V6l8 5 8-5z"/>'
             '<path fill="currentColor" d="m17.5 1.5.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1'
             '-2.1-.9 2.1-.9z"/></svg>',
)
class AstraAutoAssistant(Plugin):
    """Astra plugin: auto-assistant."""

    def __init__(self) -> None:
        super().__init__()
        self._poller_task: Optional[asyncio.Task] = None
        self._poll_lock: Optional[asyncio.Lock] = None  # created inside the loop
        self._last_summary = ""
        self._llm_error = ""   # human-readable reason the LLM triage is off

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_config_changed(self, config: Dict[str, Any]) -> None:
        # The daemon delivers the initial config on start — the earliest
        # reliable place to start background work.
        self._ensure_poller()

    async def health_check(self):
        # The daemon probes health periodically; a second chance to (re)start
        # the poller after a plugin reload.
        self._ensure_poller()
        return True, "ok"

    async def on_shutdown(self) -> None:
        if self._poller_task is not None and not self._poller_task.done():
            self._poller_task.cancel()

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

    async def poll_once(self, auto: bool) -> str:
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
                    msgs, uid_high = await asyncio.to_thread(
                        mailer.fetch_new_emails, acct, baseline, max(limit, 1))
                except Exception as e:
                    errors.append(f"{acct.get('email')}: {e}")
                    acct_updates[acct_id] = {
                        **prev,
                        "last_poll": time.time(),
                        "last_error": str(e)[:300],
                        "new_count": 0,
                    }
                    continue
                if baseline is None:
                    baselined.append(acct.get("email", ""))
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
                    m["account_email"] = acct.get("email", "")
                    fresh.append(m)
                new_items.extend(fresh)
                acct_updates[acct_id] = {
                    **prev,
                    "last_poll": time.time(),
                    "last_error": "",
                    "new_count": len(fresh),
                    "last_uid": uid_high,
                }

            triaged: List[Dict[str, Any]] = []
            source = "rules"
            if new_items:
                use_llm = bool(settings.get("llm_enabled", True)) and \
                    mode in ("llm", "whitelist_llm") and self.host is not None
                llm_idx: List[int] = []
                parsed = None
                if use_llm:
                    # Token economy: most mail is ads. Items the keyword
                    # rules call low never reach the LLM — the rules verdict
                    # is good enough for "скидки, распродажи, рассылки".
                    skip_ads = bool(settings.get("skip_ads_llm", True))
                    rules_first = [analyze.classify_rules(
                        it.get("subject", ""), it.get("body", ""))
                        for it in new_items]
                    llm_idx = [i for i, v in enumerate(rules_first)
                               if not (skip_ads and (
                                   v["importance"] == "low"
                                   or new_items[i].get("bulk")))]
                    if llm_idx:
                        answer = await self._ask_astra(analyze.build_llm_prompt(
                            [new_items[i] for i in llm_idx]))
                        parsed = analyze.parse_llm_answer(answer)
                        # Honest source: the LLM counts only when it actually
                        # returned a parseable verdict; otherwise rules decided.
                        source = "llm" if parsed else "rules"
                    else:
                        self._llm_error = ""
                else:
                    if not settings.get("llm_enabled", True):
                        self._llm_error = "LLM выключен в настройках"
                    elif mode not in ("llm", "whitelist_llm"):
                        self._llm_error = f"режим «{mode}» не использует LLM"
                triaged = analyze.merge_llm(new_items, parsed, llm_idx=llm_idx)

                # Final verdict, strongest first: mail from the user's own
                # account is always high (a note to self / a test — neither
                # the keyword rules nor the model scores it), bulk senders
                # are never high. Applied to the triaged item ITSELF: an
                # override that only touched the digest copy left the letter
                # "important" in the tab while the handoff list (built from
                # the raw verdicts) stayed empty — the trigger fired and no
                # tasks were ever requested.
                own_addrs = {(a.get("email") or "").strip().casefold()
                             for a in settings.get("accounts") or []} - {""}
                for it in triaged:
                    if it.get("from_addr", "").casefold() in own_addrs:
                        it["importance"] = "high"
                    elif it.get("bulk"):
                        it["importance"] = "low"

                for it in triaged:
                    digest_item = {
                        "id": f"{it.get('account_id')}:{it['uid']}",
                        "ts": time.time(),
                        "mail_ts": it.get("ts") or 0,
                        "account_id": it.get("account_id"),
                        "account_email": it.get("account_email", ""),
                        "from_name": it.get("from_name", ""),
                        "from_addr": it.get("from_addr", ""),
                        "subject": it.get("subject", ""),
                        "importance": it.get("importance", "normal"),
                        "summary": it.get("summary", ""),
                        "tasks": it.get("tasks", []),
                        "reminders": it.get("reminders", []),
                        "source": source,
                    }
                    # Only a NEW digest entry fires the trigger: a letter
                    # re-fetched after a rewind or a failed fetch must not
                    # re-announce itself.
                    if store.add_digest_item(digest_item) and \
                            digest_item["importance"] == "high":
                        await self._fire_important(digest_item)

            state = store.load_state()
            state["last_poll"] = time.time()
            state["last_error"] = "; ".join(errors)[:500]
            # Per-account status, pruned to the accounts that still exist.
            live_ids = {a.get("id") or a.get("email")
                        for a in settings.get("accounts") or []}
            merged_status = {k: v for k, v in (state.get("acct_status") or {}).items()
                             if k in live_ids}
            merged_status.update(acct_updates)
            state["acct_status"] = merged_status
            store.save_state(state)

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
            # Auto-handoff runs on ANY poll that found high letters — the
            # manual "check now" button included. Gating it on `auto` only
            # made the tab look broken: the letter got triaged and the
            # trigger fired, but no task request ever left the plugin.
            if high and settings.get("auto_handoff"):
                await self._handoff(high)
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
    # Astra as the LLM (SendChatMessage -> the assistant's configured model)
    # ------------------------------------------------------------------

    async def _ask_astra(self, prompt: str, voice: bool = False) -> str:
        """Send one message into the plugin's own Astra conversation and
        collect the streamed reply. Empty string on any failure.

        `voice=True` makes Astra SPEAK the reply aloud (PluginChatRequest.
        voice_enabled) — that is the plugin's only voice channel.

        Fills self._llm_error with a human-readable reason so the tab can
        say WHY the smart triage is off (a permission refused at install
        time is the common case — see the README, "LLM не работает")."""
        if self.host is None:
            self._llm_error = "нет канала к чату Astra"
            return ""
        parts: List[str] = []
        try:
            async with asyncio.timeout(_LLM_TIMEOUT_SECONDS):
                async for chunk in self.host.send_chat_message(
                        prompt, voice_enabled=voice):
                    kind = chunk.WhichOneof("content")
                    if kind == "text":
                        parts.append(chunk.text)
                    elif kind == "error":
                        logger.warning(f"Astra chat error: {chunk.error}")
                        self._llm_error = str(chunk.error)[:300]
                        break
                    elif kind == "done":
                        break
        except asyncio.TimeoutError:
            self._llm_error = "Astra не ответила вовремя (таймаут 180 с)"
            logger.warning("Astra chat timed out")
        except Exception as e:
            msg = str(e)
            if "send_chat_message" in msg and "PERMISSION_DENIED" in msg:
                # Installing from a local .astraplugin file (tier 2) refuses
                # this permission outright — only a sideload or a listed
                # plugin can have it. Say so instead of a raw gRPC dump.
                self._llm_error = ("право send_chat_message отказано: плагин "
                                   "установлен из файла. Переустанови его "
                                   "sideload-ом (astra-plugin dev) — там это "
                                   "право доступно")
            else:
                self._llm_error = msg[:300]
            logger.warning(f"Astra chat failed: {e}")
        else:
            self._llm_error = ""
        return "".join(parts)

    @staticmethod
    def _unhandled_high(hours: float) -> List[Dict[str, Any]]:
        """High digest items whose tasks were not requested from Astra yet."""
        return [i for i in store.recent_digest(hours)
                if i.get("importance") == "high" and not i.get("handled")]

    @staticmethod
    def _astra_task_counts() -> Optional[List[int]]:
        """Best-effort [tasks, reminders, calendar] counts from Astra's
        own config.

        The model in the plugin chat sometimes answers "created" without
        calling any tool (the daemon log proves it: tool_calls=4 — all
        tool-search lookups — zero dispatches). The only honest check is
        Astra's tasks.json/reminders.json/calendar.json. None = files not
        found there — verification is skipped, not fatal."""
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        cfg = Path(appdata) / "Astra" / "astra" / "config"
        counts: List[int] = []
        for name in ("tasks.json", "reminders.json", "calendar.json"):
            try:
                data = json.loads((cfg / name).read_text(encoding="utf-8"))
            except Exception:
                return None
            counts.append(len(data) if isinstance(data, list) else 0)
        return counts

    @staticmethod
    def _clean_handoff_answer(answer: str) -> str:
        """Make the model's reply readable in the tab.

        A small model answers the handoff with whatever format it answered
        the last prompts in: tool-call JSON as text (parroting old call
        examples), or a TRIAGE LINE («1 | normal | … | … | …») learned
        from the triage prompt in the same conversation — both seen in the
        wild as the whole reply. Raw formats in the tab are noise; the
        verification note appended after it already says what actually
        happened. Code fences, pure-JSON lines and triage-shaped lines are
        collapsed into short markers, human text is kept.
        """
        t = (answer or "").strip()
        lines = []
        json_seen = False
        triage_seen = False
        for ln in t.splitlines():
            s = ln.strip()
            if s.startswith("```"):        # code fence, drop the line
                continue
            if s.startswith("{") and s.endswith("}"):
                if not json_seen:
                    lines.append("⟨ответила JSON-текстом вместо вызова "
                                 "инструментов⟩")
                    json_seen = True
                continue
            head = s.split("|", 1)[0].strip()
            if head.isdigit() and s.count("|") >= 2:
                if not triage_seen:
                    lines.append("⟨ответила строкой разбора⟩")
                    triage_seen = True
                continue
            lines.append(ln)
        cleaned = "\n".join(lines).strip()
        return cleaned or t

    async def _handoff(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Ask Astra to turn digest findings into real tasks/reminders.

        Returns {"success": False, "error": …} when the chat channel is
        unavailable (the tier-2 install ceiling is the common case) — the
        caller shows the error instead of failing silently. On a reply the
        outcome is VERIFIED against Astra's tasks.json: the model may claim
        creation without calling a tool, and an unconfirmed claim stays
        re-sendable instead of being marked handled."""
        if self.host is None:
            return {"success": False,
                    "error": "нет канала к чату Astra (плагин не зарегистрирован)"}
        settings = store.load_settings()
        voice = bool(settings.get("voice_announce", True))
        mode = settings.get("deadline_mode", "calendar")
        if mode not in analyze.DEADLINE_MODES:
            mode = "calendar"
        before = self._astra_task_counts()
        answer = await self._ask_astra(
            analyze.build_handoff_prompt(items, deadline_mode=mode),
            voice=voice)
        # Verify FIRST, against Astra's own task files: the model may call
        # the tools and then answer with NO final text (seen in the wild:
        # add_task + add_calendar_event executed, empty reply) — an empty
        # reply with grown counters is a SUCCESS, not a failure. The
        # counters outrank both the reply text and its absence.
        note = ""
        verified: Optional[bool] = None
        after = self._astra_task_counts()
        if before is not None and after is not None:
            d_tasks = after[0] - before[0]
            d_rems = after[1] - before[1]
            d_cal = after[2] - before[2]
            verified = (d_tasks + d_rems + d_cal) > 0
            note = (f" (проверка: создано задач {d_tasks}, напоминаний "
                    f"{d_rems}, записей в календаре {d_cal})")
        if not answer:
            if verified:
                full = ("Астра создала задачи, не ответив текстом." + note)
                self._save_handoff_result(True, "", full, items, True)
                return {"success": True, "answer": full, "verified": True}
            reason = self._llm_error or "Astra не ответила (пустой ответ)"
            self._save_handoff_result(False, reason, "", items, None)
            return {"success": False, "error": reason}
        if not verified and before is not None and after is not None:
            note = (" (проверка: новых задач, напоминаний и записей в "
                    "календаре НЕ появилось — Астра ответила текстом, не "
                    "вызвав инструменты; нажми кнопку ещё раз)")
        clean = self._clean_handoff_answer(answer)
        if verified and (not clean or "⟨" in clean):
            # The reply was a parroted format (triage line / JSON), not a
            # human sentence — but the counters say the tasks exist. The
            # tab and the voice announcement get a synthesized readable
            # message instead of the model's noise.
            full = "Астра создала задачи по письмам." + note
        else:
            full = clean[:2000] + note
        self._save_handoff_result(True, "", full, items, verified)
        return {"success": True, "answer": full, "verified": verified}

    def _save_handoff_result(self, ok: bool, error: str, answer: str,
                             items: List[Dict[str, Any]],
                             verified: Optional[bool]) -> None:
        """Persist what the handoff did so the tab can show it (and so the
        same letters are not re-sent on the next handoff)."""
        try:
            # Handled only when creation is confirmed (or unverifiable):
            # a hallucinated "created" must stay re-sendable.
            if ok and verified is not False:
                store.mark_digest_handled(
                    [f"{it.get('account_id')}:{it['uid']}" for it in items])
        except Exception:
            pass
        try:
            state = store.load_state()
            state["handoff_error"] = ("" if ok else str(error)[:300])
            state["handoff_answer"] = answer[:2000]
            state["handoff_ts"] = time.time()
            store.save_state(state)
        except Exception:
            pass

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
        # super() carries the @ui_page tab — dropping this call removes the
        # tab from navigation (SDK >= 0.6 keeps pages only in the base method).
        contributions = await super().get_ui_contributions()
        for c in contributions:
            if c.slot == "page.custom":
                # Glass theme needs the iframe itself transparent.
                c.transparent = True
        return contributions

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
                "handoff_error": state.get("handoff_error") or "",
                "handoff_answer": state.get("handoff_answer") or "",
                "next_in_seconds": round(next_in),
                "poller_alive": self._poller_task is not None
                                and not self._poller_task.done(),
                "last_summary": self._last_summary,
            },
        }

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

        old_accounts = {a.get("id"): a for a in old.get("accounts") or []}
        accounts = []
        for i, a in enumerate(new.get("accounts") or []):
            if not isinstance(a, dict):
                continue
            acct_id = a.get("id") or f"acct{i}"
            password = (a.get("password") or "").strip()
            if not password:
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
        self._ensure_poller()
        return {"success": True}

    @ui_call("aa_test_account")
    async def ui_test_account(self, **params: Any) -> Dict[str, Any]:
        acct = params.get("account") or {}
        if not (acct.get("password") or "").strip():
            saved = {a.get("id"): a for a in
                     store.load_settings().get("accounts") or []}
            acct["password"] = (saved.get(acct.get("id")) or {}).get("password", "")
        try:
            message = await asyncio.to_thread(mailer.test_account, acct)
            return {"success": True, "message": message}
        except Exception as e:
            return {"error": str(e)}

    @ui_call("aa_run_digest")
    async def ui_run_digest(self, **params: Any) -> Dict[str, Any]:
        report = await self.poll_once(auto=False)
        return {"success": True, "report": report}

    @ui_call("aa_handoff")
    async def ui_handoff(self, **params: Any) -> Dict[str, Any]:
        items = self._unhandled_high(48)
        if not items:
            return {"error": "Нет необработанных важных писем за последние "
                             "48 часов — задачи по ним уже запрошены у Астры"}
        return await self._handoff(items)

    @ui_call("aa_rewind_baseline")
    async def ui_rewind_baseline(self, **params: Any) -> Dict[str, Any]:
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
        state = store.load_state()
        status = state.setdefault("acct_status", {})
        moved: List[str] = []
        errors: List[str] = []
        for acct in settings.get("accounts") or []:
            if not acct.get("enabled", True):
                continue
            if not (acct.get("email") and acct.get("password")):
                continue
            acct_id = acct.get("id") or acct.get("email")
            try:
                base = await asyncio.to_thread(mailer.rewind_baseline, acct, back)
            except Exception as e:
                errors.append(f"{acct.get('email')}: {str(e)[:200]}")
                continue
            if base is None:
                continue
            entry = dict(status.get(acct_id) or {})
            entry["last_uid"] = int(base)
            status[acct_id] = entry
            moved.append(acct.get("email", ""))
        if not moved:
            note = f" Ошибки: {errors[0]}" if errors else ""
            return {"error": "Нечего показывать — непрочитанных писем нет." + note}
        store.save_state(state)
        report = await self.poll_once(auto=False)
        return {"success": True, "report": report, "accounts": moved,
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
