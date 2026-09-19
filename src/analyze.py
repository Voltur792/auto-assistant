"""Mail triage: whitelist filter, keyword rules, and the Astra-LLM prompt.

Three ways to decide what matters (the user picks in the tab):

* whitelist   — only senders from the allow-list enter the digest (rules score);
* rules       — every mail enters, keyword scoring decides importance;
* llm         — every mail goes to Astra's own model (via SendChatMessage,
                no extra API keys) which returns strict JSON;
* whitelist_llm — whitelist gates who is analyzed, the LLM scores the rest.

The LLM call itself lives in plugin.py (it needs the host channel); here we
only build the prompt and parse the answer, so it is testable offline.
"""

import json
import re
import time
from typing import Any, Dict, List, Optional

# Keyword scoring: the body/subject is lowercased and matched per word stem.
_HIGH_WORDS = (
    "счёт", "счет", "оплат", "задолжен", "просроч", "начисл", "штраф",
    "срок", "до ", "договор", "соглашени", "подтверд", "код", "пропуск",
    "паспорт", "виза", "документ", "доставк", "заказ", "получени",
    "требует", "срочно", "важно", "отключ", "блокир", "заявк", "счет-фактура",
    "сдать", "сдай", "сдач", "презентац", "выполн", "подготов", "задани",
    "дедлайн", "напомин", "встреч", "звонок", "митинг", "отчёт", "отчет",
    "зачёт", "зачет", "экзамен", "проект", "план ", "созвон",
    "invoice", "payment", "due", "urgent", "verify", "confirmation",
    "password", "security", "expiry", "expiring", "action required",
    "deadline", "meeting", "report", "submit",
)
_LOW_WORDS = (
    "скидк", "акци", "распродаж", "промо", "подписк", "рассылк", "отписаться",
    "новост", "обзор", "реклам", "бонус", "кэшбек", "кино", "мемы",
    "sale", "off ", "newsletter", "unsubscribe", "promo", "deal", "coupon",
)

# A date in the text turns "one high word" into high: "сдать до 20.09",
# "оплатить до 31.12.2026" — a deadline is the point of the letter.
_DATE_RE = re.compile(r"\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?")

# Bulk-mail senders: forced to "low" (so skip_ads_llm keeps them out of the
# LLM) but still shown. They are NOT dropped — an address like noreply@ says
# nothing about importance (banks, tickets and government write from it), and a
# filter that silently deletes mail is indistinguishable from a broken poller.
_BULK_WORDS = ("newsletter@", "mailing@", "promo@", "marketing@", "ads@",
               "sales-notif@", "subscribe@")

_IMPORTANCE_ORDER = {"high": 2, "normal": 1, "low": 0}


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


def classify_rules(subject: str, body: str) -> Dict[str, Any]:
    """Keyword-score one message into {importance, summary}."""
    text = f"{subject}\n{body}".casefold()
    high = sum(1 for w in _HIGH_WORDS if w in text)
    low = sum(1 for w in _LOW_WORDS if w in text)
    has_date = _DATE_RE.search(text) is not None
    # Ad beats a tie: "скидка до 31.12" scores one low and one high word.
    if low > 0 and low >= high:
        importance = "low"
    elif high >= 2 or (high >= 1 and has_date):
        importance = "high"
    elif high >= 1:
        importance = "normal"
    else:
        importance = "normal"
    first_line = (body.strip().split(". ")[0] if body.strip() else "")
    summary = first_line[:200] if first_line else subject[:200]
    return {"importance": importance, "summary": summary}


def is_bulk(from_addr: str) -> bool:
    """True for addresses that are bulk mail by construction (newsletters)."""
    addr = from_addr.casefold()
    return any(w in addr for w in _BULK_WORDS)


def is_own_mail(from_addr: str, accounts: Any) -> bool:
    """True when the letter was sent from one of the user's own accounts.

    Such mail is a note to self or a test. The keyword rules have no score for
    «тестовое собрание» (and the LLM calls it normal too), so a test letter
    used to sink into the middle of the digest — even though it is mail from
    the sender the user watches most: themselves.
    """
    addr = (from_addr or "").casefold()
    return bool(addr) and any(
        isinstance(a, dict) and (a.get("email") or "").strip().casefold() == addr
        for a in (accounts or []))


def build_llm_prompt(items: List[Dict[str, Any]]) -> str:
    """Prompt asking Astra's assistant to triage messages, one line each.

    The reply format is plain pipe-separated LINES, not JSON. Why: every
    JSON reply in this plugin's conversation teaches the model that
    "messages from this plugin get JSON answers" — and it then answers the
    HANDOFF prompt with tool-call JSON as text instead of calling the
    tools (observed in the wild: {"arguments":…} parroted as the reply).
    parse_llm_answer() still accepts JSON as a fallback for chatty models.
    """
    lines = [
        "Разберёшь новые письма. Ответь по ОДНОЙ строке на письмо, поля "
        "через «|», без JSON и без пояснений:",
        "номер | важность | сводка | задачи | сроки",
        "важность: high (оплата, счета, сроки, документы, безопасность, "
        "бездействие ухудшит ситуацию), normal, low (реклама, рассылки).",
        "сводка: 1-2 предложения по-русски.",
        "задачи: конкретные действия через «;» (максимум 3), «-» если нет. "
        "Для high обязательно предложи хотя бы одну задачу.",
        "сроки: даты и время из письма через «;», «-» если нет.",
        "Пример:",
        "1 | high | Счёт за электричество, оплатить до 20.09 | Оплатить счёт | 20.09",
        "2 | low | Рассылка магазина об акциях | - | -",
        "Письма:",
    ]
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}) От: {it.get('from_name', '')} <{it.get('from_addr', '')}>; "
            f"Тема: {it.get('subject', '')}; Текст: {it.get('body', '')[:500]}"
        )
    return "\n".join(lines)


# Where deadlines land: Astra's calendar (core:add_calendar_event) or
# reminders (core:add_reminder). calendar.json holds {date, text, time}.
DEADLINE_MODES = ("calendar", "reminders", "both")


def build_handoff_prompt(items: List[Dict[str, Any]],
                         deadline_mode: str = "calendar") -> str:
    """Prompt asking Astra to create real tasks/reminders from digest items.

    Strictly imperative, with NO JSON call examples. Why: in the plugin's
    conversation the tools sit behind tool-search compaction and the model
    never sees core:add_task in its visible list — so it needs to be TOLD
    to call the tools by id directly. And every JSON-shaped thing in the
    prompt (or in the conversation's history — the triage used to ask for
    JSON) gets parroted back as a text answer instead of a tool call
    (observed twice in the wild). Astra dispatches a call by id even for a
    deferred tool, so naming the ids is enough.
    """
    today = time.strftime("%d.%m.%Y")
    today_iso = time.strftime("%Y-%m-%d")
    lines = [
        f"Сегодня {today} (в формате ISO: {today_iso}).",
        "По письмам ниже создай задачи, ВЫЗВАВ инструменты (не текстом):",
        "— core:add_task — параметр text: текст задачи.",
    ]
    if deadline_mode == "calendar":
        lines += [
            "— core:add_calendar_event — параметры text, date (ГГГГ-ММ-ДД), "
            "time (ЧЧ:ММ). Каждый срок из письма — запись в календаре.",
        ]
    elif deadline_mode == "both":
        lines += [
            "— core:add_reminder — параметры text (дату пиши в text), "
            "time (ЧЧ:ММ).",
            "— core:add_calendar_event — параметры text, date (ГГГГ-ММ-ДД), "
            "time (ЧЧ:ММ). Каждый срок — И напоминание, И запись в календаре.",
        ]
    else:  # reminders
        lines += [
            "— core:add_reminder — параметры text (дату пиши в text), "
            "time (ЧЧ:ММ).",
        ]
    lines += [
        "Эти инструменты доступны всегда — вызывай их напрямую по id, даже "
        "если их нет в твоём списке инструментов.",
        "Если вызов отвергнут из-за схемы аргументов — повтори вызов строго "
        "по прикреплённой схеме.",
        "Текстом не отвечай, JSON не пиши — только вызови инструменты.",
    ]
    for it in items:
        lines.append(f"Письмо от {it.get('from_name') or it.get('from_addr')}: "
                     f"{it.get('subject') or '(без темы)'}")
        for t in it.get("tasks") or []:
            lines.append(f"  вызови core:add_task, text: {t}")
        for r in it.get("reminders") or []:
            if deadline_mode == "calendar":
                lines.append(f"  срок «{r}» — вызови core:add_calendar_event, "
                             "разобрав дату и время")
            elif deadline_mode == "both":
                lines.append(f"  срок «{r}» — вызови core:add_reminder и "
                             "core:add_calendar_event, разобрав дату и время")
            else:
                lines.append(f"  срок «{r}» — вызови core:add_reminder, "
                             "разобрав дату и время")
        # A high letter can arrive with NO extracted tasks (own-mail and
        # rule overrides set high over a verdict that found nothing, and a
        # small model often returns no tasks). Without the letter's own
        # text the model had nothing to create from and honestly answered
        # that there is nothing to do. Give it the body, or the summary.
        if not (it.get("tasks") or it.get("reminders")):
            body = (it.get("body") or "").strip()
            if body:
                lines.append(f"  текст письма: {body[:400]}")
            elif it.get("summary"):
                lines.append(f"  суть письма: {it['summary']}")
            extra = (" и core:add_calendar_event для сроков"
                     if deadline_mode != "reminders" else "")
            lines.append(f"  сформулируй задачи сам и вызови core:add_task "
                         f"для каждой{extra}")
    return "\n".join(lines)


_IMP_SYNONYMS = {
    "high": "high", "высокая": "high", "высокое": "high", "важное": "high",
    "важная": "high", "срочно": "high",
    "normal": "normal", "обычное": "normal", "обычная": "normal",
    "среднее": "normal",
    "low": "low", "низкое": "low", "низкая": "low",
}


def _split_field(s: str) -> List[str]:
    """Split a «;»-joined prompt field; «-» means empty."""
    return [x.strip() for x in s.split(";")
            if x.strip() and x.strip() != "-"]


def _parse_line_answer(text: str) -> Optional[List[Dict[str, Any]]]:
    """Parse the line-format triage reply: «n | importance | summary |
    tasks | deadlines». Lines that do not match the shape are skipped, so
    chatty preambles and examples quoted back do no harm."""
    items: List[Dict[str, Any]] = []
    for ln in text.splitlines():
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        imp = _IMP_SYNONYMS.get(parts[1].casefold())
        if imp is None:
            continue
        items.append({
            "n": int(parts[0]),
            "importance": imp,
            "summary": parts[2][:400],
            "tasks": _split_field(parts[3]) if len(parts) > 3 else [],
            "reminders": _split_field(parts[4]) if len(parts) > 4 else [],
        })
    return items or None


def parse_llm_answer(text: str) -> Optional[List[Dict[str, Any]]]:
    """Parse the model's triage reply: line format first, JSON as a
    fallback (older prompts asked for JSON; a chatty model may still
    produce it)."""
    if not text:
        return None
    parsed = _parse_line_answer(text)
    if parsed:
        return parsed
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        imp = str(it.get("importance", "normal")).casefold()
        if imp not in _IMPORTANCE_ORDER:
            imp = "normal"
        out.append({
            "n": it.get("n"),
            "importance": imp,
            "summary": str(it.get("summary", ""))[:400],
            "tasks": _str_list(it.get("tasks")),
            "reminders": _str_list(it.get("reminders")),
        })
    return out or None


def _str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip()[:300] for v in value if str(v).strip()][:5]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:300]]
    return []


def merge_llm(items: List[Dict[str, Any]],
              parsed: Optional[List[Dict[str, Any]]],
              llm_idx: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """Attach LLM verdicts to the original messages by index `n`.

    The model may drop or renumber items — anything unmatched falls back to
    keyword rules so every message still gets a verdict.

    `llm_idx` lists the positions (0-based) that were actually sent to the
    LLM, in prompt order; the rest were prefiltered (e.g. obvious ads) and
    always keep their rules verdict. None means "everything was sent"."""
    by_n = {}
    if parsed:
        for p in parsed:
            try:
                by_n[int(p["n"])] = p
            except (KeyError, TypeError, ValueError):
                continue
    in_llm = set(llm_idx) if llm_idx is not None else set(range(len(items)))
    out = []
    for i, it in enumerate(items):
        verdict = None
        if i in in_llm and llm_idx is not None:
            prompt_n = llm_idx.index(i) + 1
            verdict = by_n.get(prompt_n)
        elif llm_idx is None:
            verdict = by_n.get(i + 1)
        if verdict is None:
            verdict = classify_rules(it.get("subject", ""), it.get("body", ""))
            verdict = {**verdict, "tasks": [], "reminders": []}
        out.append({**it, **{k: verdict[k] for k in
                             ("importance", "summary", "tasks", "reminders")
                             if k in verdict}})
    return out
