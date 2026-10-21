"""Idempotent native-widget jobs. This module never calls the Astra chat."""
import time
import uuid
import re
from datetime import date, datetime, time as day_time
from typing import Any, Dict, List

from . import store, triage


def _appointment_key(item, event, related):
    body = triage.clean(item.get("body", ""))
    if not re.search(r"врач|доктор", body, re.I) or not event.get("time"):
        return None
    address = re.search(r"Адрес:\s*(.+?)(?:\s+Статус:|\s+Подробнее|$)", body, re.I)
    if not address:
        return None
    # Merge a confirmation/reminder only with an explicit common patient,
    # clinic address, recipient, date and time. Different family bookings stay separate.
    for reference in [item, *related]:
        patient = re.search(r"Вы записаны на при[её]м\s+(.+?)\s+Запись подтверждена",
                            triage.clean(reference.get("body", "")), re.I)
        if patient and re.search(r"(?<!\w)" + re.escape(patient.group(1)) + r"(?!\w)", body, re.I):
            return "|".join(["appointment", item.get("account_id", ""), patient.group(1).casefold(),
                             address.group(1).casefold(), event["date"], event["time"]])
    return None


def operations(item: Dict[str, Any], mode: str, related=()) -> List[Dict[str, Any]]:
    if (item.get("triage_version") != triage.VERSION or not item.get("action_required")
            or item.get("importance") != "high" or not item.get("tasks")):
        return []
    identity = item.get("id")
    if not isinstance(identity, str) or not identity:
        return []
    out = []
    events = item.get("deadlines") or []
    appointment = (_appointment_key(item, events[0], related)
                   if len(item["tasks"]) == 1 and len(events) == 1 else None)

    def add(kind: str, fields: Dict[str, Any]) -> None:
        canonical = "|".join(str(fields[k]) for k in sorted(fields))
        seed = (f"auto-assistant:appointment:{appointment}:{kind}" if appointment
                else f"auto-assistant:v3:{identity}:{kind}:{canonical}")
        ident = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
        out.append({"id": ident, "mail_id": identity, "kind": kind,
                    "mail_ids": [identity],
                    "event_key": appointment,
                    "record": {"id": ident, **fields}, "status": "pending", "created_at": time.time()})

    for text in item["tasks"]:
        if not isinstance(text, str) or not 1 <= len(text) <= 500:
            raise ValueError("Неверный текст задачи")
        add("tasks", {"text": text, "done": False, "list_id": None})
    for event in events:
        day = date.fromisoformat(event["date"])
        at = event["time"]
        if at:
            datetime.strptime(at, "%H:%M")
        if day < date.today():
            continue
        text = event.get("text") or item["tasks"][0]
        if mode in ("calendar", "both"):
            add("calendar", {"text": text, "date": day.isoformat(), "time": at})
            out[-1]["expires_at"] = datetime.combine(day, day_time.fromisoformat(at) if at
                else day_time(23, 59, 59)).timestamp()
        if mode in ("reminders", "both"):
            # Native reminders have a time-only contract. Date remains visible
            # in text; future reminders are released only on their due day.
            add("reminders", {"text": f"{day.isoformat()} — {text}", "time": at or "09:00"})
            out[-1]["not_before"] = datetime.combine(day, datetime.min.time()).timestamp()
            out[-1]["expires_at"] = datetime.combine(day, day_time.fromisoformat(at)
                if at else day_time(9, 0)).timestamp()
    return out


def enqueue(items: List[Dict[str, Any]], mode: str) -> int:
    count = 0

    def change(state):
        nonlocal count
        existing = {job["id"]: job for job in state.get("widget_jobs") or []}
        related = state.get("digest") or items
        by_mail = {item["id"]: item for item in related}
        for item in items:
            for job in operations(item, mode, related):
                old = existing.get(job["id"])
                if not old and job.get("event_key"):
                    # Adopt an already delivered pre-upgrade booking, retaining
                    # the native UUID and the user's existing record text.
                    for candidate in existing.values():
                        if candidate["kind"] != job["kind"] or candidate["status"] not in ("pending", "running", "done", "failed"):
                            continue
                        prior = by_mail.get(candidate["mail_id"], {})
                        dates = prior.get("deadlines") or []
                        key = candidate.get("event_key")
                        if not key and len(dates) == 1:
                            key = _appointment_key(prior, dates[0], related)
                        if key == job["event_key"]:
                            candidate["event_key"] = key
                            old = candidate
                            break
                if old and old["status"] in ("pending", "running", "done", "failed"):
                    old["mail_ids"] = list(dict.fromkeys([*old.get("mail_ids", [old["mail_id"]]), item["id"]]))
                    if old["status"] == "failed":
                        old.update(status="pending", error="", created_at=time.time())
                        count += 1
                    continue
                existing[job["id"]] = job
                count += 1
        queued_ids = {item["id"] for item in items}
        for item in state.get("digest") or []:
            if item["id"] in queued_ids:
                item["auto_pending"] = False
                group = [j for j in existing.values() if item["id"] in j.get("mail_ids", [j["mail_id"]])]
                if group and all(j["status"] == "done" for j in group):
                    item.update(handled=True, native_verified=True)
        state["widget_jobs"] = list(existing.values())
        state["handoff_error"] = ""
        state["handoff_answer"] = f"Подготовлено записей для Astra: {count}."
        state["handoff_pending"] = any(j["status"] in ("pending", "running") for j in existing.values())
        state["handoff_ts"] = time.time()
    store.update_state(change)
    return count


def claim(worker: str) -> List[Dict[str, Any]]:
    now = time.time()
    state = store.load_state()
    if not any((j["status"] == "pending" or j["status"] == "running" and j.get("lease_until", 0) < now)
               and j.get("not_before", 0) <= now for j in state.get("widget_jobs") or []):
        return []
    result = []

    def change(state):
        for job in state.get("widget_jobs") or []:
            if job.get("not_before", 0) > now:
                continue
            if job["status"] == "pending" or (job["status"] == "running" and job.get("lease_until", 0) < now):
                if job.get("expires_at", float("inf")) < now:
                    job.update(status="failed", error="Срок истёк до записи в Astra", finished_at=now)
                    state["handoff_error"] = "Некоторые сроки истекли до записи в Astra."
                    continue
                job.update(status="running", worker=worker, lease=str(uuid.uuid4()), lease_until=now + 90)
                result.append(dict(job))
                if len(result) == 3:
                    break
        state["handoff_pending"] = any(j["status"] in ("pending", "running")
                                       for j in state.get("widget_jobs") or [])
    store.update_state(change)
    return result


def finish(ident: str, lease: str, success: bool, error: str = "") -> bool:
    accepted = False

    def change(state):
        nonlocal accepted
        jobs = state.get("widget_jobs") or []
        for job in jobs:
            if job["id"] == ident and job.get("lease") == lease and job["status"] == "running":
                accepted = True
                job.update(status="done" if success else "failed", error=str(error)[:250], finished_at=time.time())
                break
        if not accepted:
            return
        for item in state.get("digest") or []:
            group = [j for j in jobs if item["id"] in j.get("mail_ids", [j["mail_id"]])]
            if group:
                item["handled"] = all(j["status"] == "done" for j in group)
                item["native_verified"] = item["handled"]
        pending = sum(j["status"] in ("pending", "running") for j in jobs)
        failed = sum(j["status"] == "failed" for j in jobs)
        done = sum(j["status"] == "done" for j in jobs)
        state["handoff_pending"] = bool(pending)
        state["handoff_error"] = str(error)[:250] if not success else (f"Не выполнено записей: {failed}." if failed else "")
        state["handoff_answer"] = f"Astra подтвердила записей: {done}. В очереди: {pending}."
        state["handoff_ts"] = time.time()
    store.update_state(change)
    return accepted
