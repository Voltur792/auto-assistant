"""Conservative mail decisions and validated, dated task proposals."""
import html
import json
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, List

VERSION = 4


def clean(value: str) -> str:
    return " ".join("".join(" " if c in "\u2800\u3164\uffa0" else c
                            for c in html.unescape(value)
                            if unicodedata.category(c) != "Cf" and c != "\u034f").split())


_OBLIGATION = re.compile(
    r"задолжен|неоплачен|просроч|сч[её]т.{0,25}(?:оплат|выстав)|оплат.{0,25}сч[её]т|"
    r"забер(?:ите|и).{0,60}(?:заказ|посылк)|(?:заказ|посылк).{0,50}(?:готов|пункт.{0,10}выдач)|"
    r"(?:нужно|необходимо|прошу|требуется).{0,80}(?:сдать|подготов|отправ|подпис|ответ|оплат)|"
    r"дедлайн|запланирован.{0,20}(?:встреч|собран|созвон)|аккаунт.{0,30}(?:заблокир|деактивир)|"
    r"invoice.{0,30}(?:due|payment)|action required|payment overdue", re.I)
_PROMO = re.compile(
    r"скидк|распродаж|промокод|к[эе]шб[эе]к|бонус.{0,15}(?:балл|руб)|"
    r"(?:оформите|попробуйте|предлагаем).{0,35}(?:кредит|каско|полис|страховк|подписк)|"
    r"защит.{0,20}(?:от\s*\d|руб.{0,15}месяц)|покупайте|выгодн.{0,15}предложен|"
    r"обновите.{0,35}(?:верси|приложен|телемост)|открытый вебинар|кадров\w*.{0,20}форум|"
    r"(?:приглашаем|зарегистрируйтесь|запишитесь).{0,60}(?:вебинар|конференц|форум|демо)|"
    r"newsletter|coupon|special offer", re.I)
_CONFIRMED_EVENT = re.compile(r"вы (?:уже )?(?:записаны|зарегистрированы)|запись подтверждена|"
                              r"регистрация подтверждена|ваше участие подтверждено|ваш билет", re.I)
_INFORMATION = re.compile(r"код (?:входа|подтверждения)|одноразов.{0,10}(?:код|пароль)|"
                          r"новая попытка входа|new (?:login|sign.in)|если.{0,15}(?:это были|вошли) вы", re.I)
_LOGIN = re.compile(r"(?:выполнен|осуществл[её]н|успешн\w*|нов\w*).{0,35}вход|"
                    r"вход.{0,45}(?:аккаунт|устройств|уч[её]тн)|new (?:login|sign.in)", re.I)
_SECURITY_ACTION = re.compile(r"(?:аккаунт|уч[её]тная запись|доступ).{0,30}(?:заблокирован|скомпрометирован)|"
                            r"(?:подтвердите|необходимо подтвердить|требуется подтвердить).{0,50}(?:доступ|данные|личность)|"
                            r"account.{0,20}(?:suspended|compromised)", re.I)
_OUTAGE = re.compile(r"временно недоступ|сервис\w*.{0,40}перебо|перебо.{0,35}сервис|"
                     r"ситуаци.{0,20}с сервис|восстановлен.{0,25}(?:работ|сервис|доступ)|"
                     r"разделы.{0,45}недоступ|service outage", re.I)
_PASSIVE_TASK = re.compile(r"^(?:следить|ожидать|мониторить|учитывать|ознакомиться|прочитать|"
                           r"проверить статус восстановлен|ждать|дождаться)\b", re.I)
_CONDITIONAL_TASK = re.compile(r"^(?:если\b|при необходимости\b|при желании\b|по желанию\b|в случае\b)", re.I)


def information_reason(subject: str, body: str) -> str:
    """Ground information-only decisions in the letter, not model confidence."""
    subject, body = clean(subject), clean(body)
    text = subject + " " + body
    explicit = bool(_SECURITY_ACTION.search(subject)) or any(
        _SECURITY_ACTION.search(sentence) and not re.search(r"если|возможно|можете|вероятност", sentence, re.I)
        for sentence in re.split(r"[.!?\n]", body))
    if _INFORMATION.search(text) and not explicit:
        return "Уведомление или код: обязательное действие не подтверждено"
    if _LOGIN.search(text) and not explicit:
        return "Уведомление о входе: действие требуется только если вход был не вашим"
    if re.search(r"(?:аккаунт|уч[её]тная запись|доступ).{0,30}восстановлен", subject, re.I) and not explicit:
        return "Доступ уже восстановлен; обязательное действие не указано"
    if _OUTAGE.search(text) and not _OBLIGATION.search(text) and not explicit and not re.search(
            r"обновите|замените|переподключите|восстановите|подтвердите|требуется (?:вам|от вас)", text, re.I):
        return "Сообщение о работе сервиса: обязательного действия получателя нет"
    return ""


def rules(subject: str, body: str) -> Dict[str, Any]:
    subject, body = clean(subject), clean(body)
    text = subject + "\n" + body
    obligation = bool(_OBLIGATION.search(text))
    promo = bool(_PROMO.search(text))
    # An explicit invoice is still an invoice even with an unsubscribe footer.
    if promo and not (re.search(r"задолжен|неоплачен|сч[её]т к оплате|payment overdue", text, re.I)
                          or _CONFIRMED_EVENT.search(text)):
        importance, reason = "low", "Рекламное предложение или рассылка"
    elif information_reason(subject, body):
        importance, reason = "normal", information_reason(subject, body)
    elif obligation:
        importance, reason = "high", "Есть признаки обязательства; требуется проверка"
    else:
        importance, reason = "normal", "Не найдено конкретное обязательное действие"
    return {"importance": importance, "summary": (body or subject)[:300],
            "reason": reason, "tasks": [], "reminders": [], "deadlines": [],
            "action_required": False, "confidence": 0.0, "source": "rules",
            "triage_version": VERSION}


def is_recent(item: Dict[str, Any], hours: float, now: float) -> bool:
    try:
        age = now - float(item.get("mail_ts", item.get("ts", 0)) or 0)
    except (TypeError, ValueError):
        return False
    return -300 <= age <= hours * 3600


def has_current_action(item: Dict[str, Any], hours: float, now: datetime) -> bool:
    """Old mail can still carry a future obligation; import age is not its due date."""
    if not item.get("action_required") or not item.get("tasks") or item.get("importance") != "high":
        return False
    for event in item.get("deadlines") or []:
        try:
            day = date.fromisoformat(event["date"])
            if event.get("time"):
                if datetime.combine(day, datetime.strptime(event["time"], "%H:%M").time(), tzinfo=now.tzinfo) >= now:
                    return True
            elif day >= now.date():
                return True
        except (KeyError, TypeError, ValueError):
            continue
    text = clean(item.get("subject", "") + " " + item.get("body", ""))
    if (re.search(r"задолжен|неоплачен|сч[её]т к оплате|квитанц.{0,25}на оплату|"
                  r"непрочитанн.{0,25}сообщени.{0,25}органа власти|"
                  r"недостаточно средств|баланс.{0,25}(?:недостат|исчерпан)|"
                  r"(?:аккаунт|доступ).{0,30}(?:заблокирован|приостановлен)", text, re.I)
            and is_recent(item, 30 * 24, now.timestamp())):
        return True
    return is_recent(item, hours, now.timestamp())


def parse(text: str, items: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    try:
        raw = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Модель не вернула JSON-разбор почты.") from exc
    rows = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or len(rows) != len(items):
        raise ValueError("В ответе модели отсутствует часть писем.")
    numbered = {}
    for row in rows:
        n = row.get("n") if isinstance(row, dict) else None
        if type(n) is not int or n not in range(1, len(items) + 1) or n in numbered:
            raise ValueError("Неверные или повторные номера писем в ответе модели.")
        numbered[n] = row
    result = []
    for n, original in enumerate(items, 1):
        row = numbered[n]
        importance = row.get("importance")
        if importance not in ("high", "normal", "low"):
            raise ValueError("Неизвестная важность письма в ответе модели.")
        tasks = row.get("tasks")
        confidence = row.get("confidence")
        if (not isinstance(tasks, list) or len(tasks) > 3
                or any(not isinstance(t, str) or not 1 <= len(clean(t)) <= 500 for t in tasks)
                or type(confidence) not in (int, float) or not 0 <= confidence <= 1
                or type(row.get("action_required")) is not bool
                or not isinstance(row.get("summary"), str) or not isinstance(row.get("reason"), str)):
            raise ValueError("Модель вернула некорректные предложения задач.")
        deadlines = row.get("deadlines")
        if not isinstance(deadlines, list) or len(deadlines) > 5:
            raise ValueError("Некорректный список сроков.")
        valid_dates = []
        past_only = bool(deadlines)
        for event in deadlines:
            if not isinstance(event, dict) or set(event) != {"text", "date", "time"}:
                raise ValueError("Некорректный формат срока.")
            if any(not isinstance(event[k], str) for k in event):
                raise ValueError("Некорректный формат срока.")
            if (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", event["date"])
                    or event["time"] and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", event["time"])):
                raise ValueError("Некорректный формат даты или времени.")
            try:
                day = date.fromisoformat(event["date"])
                if event["time"]:
                    at = datetime.strptime(event["time"], "%H:%M").time()
                    expired = datetime.combine(day, at, tzinfo=now.tzinfo) < now
                else:
                    expired = day < now.date()
            except ValueError as exc:
                raise ValueError("Модель вернула несуществующую дату или время.") from exc
            if not expired:
                past_only = False
                valid_dates.append({k: clean(event[k])[:500] for k in event})
        verdict = rules(original.get("subject", ""), original.get("body", ""))
        # Strong ad / one-time-code guards cannot be overridden by the model.
        info = information_reason(original.get("subject", ""), original.get("body", ""))
        if verdict["importance"] == "low":
            importance = "low"
        proposed_tasks = list(dict.fromkeys(clean(t) for t in tasks
            if not _PASSIVE_TASK.search(clean(t)) and not _CONDITIONAL_TASK.search(clean(t))))
        # A debt remains actionable after the due date; an expired parcel does not.
        debt = re.search(r"задолжен|неоплачен|payment overdue", clean(
            original.get("subject", "") + " " + original.get("body", "")), re.I)
        actionable = (importance == "high" and row["action_required"]
                      and confidence >= 0.85 and bool(proposed_tasks) and (not past_only or bool(debt))
                      and not info)
        if importance == "high" and not actionable:
            importance = "normal"
        reason, summary = clean(row["reason"])[:300], clean(row["summary"])[:400]
        if info:
            reason = info
            summary = clean(original.get("subject", ""))[:300] + ". " + info
        elif verdict["importance"] == "low":
            reason = verdict["reason"]
        elif past_only and not debt:
            reason = "Указанный разовый срок уже прошёл; новое действие не подтверждено"
            summary = "В письме указан уже прошедший срок. " + clean(original.get("subject", ""))[:250]
        elif tasks and not proposed_tasks:
            reason = "Ожидание, наблюдение или условный совет не являются обязательной задачей"
        elif importance == "normal" and not actionable and re.search(
                r"критич|ж[её]стк.{0,10}дедлайн|немедлен|обязательств|срочн|риск", reason, re.I):
            reason = "Обязательное актуальное действие по письму не подтверждено"
        result.append({**original, "importance": importance,
            "summary": summary, "reason": reason,
            "tasks": proposed_tasks if actionable else [],
            "deadlines": valid_dates if actionable else [],
            "reminders": [f"{e['date']} {e['time']}".strip() for e in valid_dates] if actionable else [],
            "action_required": actionable, "confidence": float(confidence),
            "source": "llm", "triage_version": VERSION})
    return result
