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

# Senders that are always noise regardless of mode (never LLM'd, never digested).
_NEVER_WORDS = ("noreply@", "no-reply@", "mailer@", "newsletter@", "news@", "mailing@")

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


def is_never(from_addr: str) -> bool:
    addr = from_addr.casefold()
    return any(w in addr for w in _NEVER_WORDS)


def build_llm_prompt(items: List[Dict[str, Any]]) -> str:
    """Prompt asking Astra's assistant to triage messages and reply JSON only.

    Astra's chat model is not a strict JSON endpoint, so the instruction is
    blunt and parse_llm_answer() is forgiving.
    """
    lines = [
        "Разберёшь новые письма как ассистент. Ответь ТОЛЬКО валидным JSON, "
        "без пояснений и без markdown.",
        "Формат: {\"items\": [{\"n\": <номер>, \"importance\": "
        "\"high|normal|low\", \"summary\": \"1-2 предложения по-русски\", "
        "\"tasks\": [\"дело\"], \"reminders\": [\"когда и что\"]}]}",
        "importance=high: оплата, счета, сроки, документы, безопасность, "
        "бездействие ухудшит ситуацию. low: реклама, рассылки.",
        "tasks — конкретные действия (максимум 3 на письмо), reminders — "
        "только если в письме есть дата/срок (иначе []).",
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

    Names the exact core tools AND their parameters, with call examples in
    the exact JSON shape Astra parses. Why so specific: in the plugin
    conversation the tools sit behind tool-search compaction, and a small
    local model without exact ids/params fails in observed ways — answers
    "created" without calling anything, puts the tool id INSIDE arguments
    ("tool_call: missing `id`"), or invents parameter names
    ("reminder_text"/"date" instead of "text"/"time")."""
    today = time.strftime("%d.%m.%Y")
    tomorrow = time.strftime("%d.%m.%Y", time.localtime(time.time() + 86400))
    lines = [
        f"Сегодня {today}. Создай задачи и напоминания по письмам ниже.",
        "Инструменты и их параметры:",
        "— core:add_task: параметр text (текст задачи).",
    ]
    if deadline_mode == "calendar":
        lines += [
            "— core:add_calendar_event: запись в календаре. Параметры text "
            "(текст), date (\"ГГГГ-ММ-ДД\") и time (\"ЧЧ:ММ\"). Для сроков "
            "используй только его, не напоминания.",
            "Вызывай ровно в таком формате — id инструмента ОТДЕЛЬНЫМ полем id, "
            "в arguments только параметры:",
            '{"arguments":{"text":"Подготовить отчёт"},"id":"core:add_task"}',
            '{"arguments":{"text":"Собрание — подготовить отчёт",'
            f'"date":"{tomorrow}","time":"11:00"'
            '},"id":"core:add_calendar_event"}',
        ]
    elif deadline_mode == "both":
        lines += [
            "— core:add_reminder: параметры text (текст) и time (\"ЧЧ:ММ\"). "
            "Дату пиши в text, отдельного параметра даты нет.",
            "— core:add_calendar_event: запись в календаре. Параметры text, "
            "date (\"ГГГГ-ММ-ДД\") и time (\"ЧЧ:ММ\"). Для каждого срока "
            "создай И напоминание, И запись в календаре.",
            "Вызывай ровно в таком формате — id инструмента ОТДЕЛЬНЫМ полем id, "
            "в arguments только параметры:",
            '{"arguments":{"text":"Подготовить отчёт"},"id":"core:add_task"}',
            '{"arguments":{"text":"17.09 собрание — подготовить отчёт",'
            '"time":"11:00"},"id":"core:add_reminder"}',
            '{"arguments":{"text":"Собрание — подготовить отчёт",'
            f'"date":"{tomorrow}","time":"11:00"'
            '},"id":"core:add_calendar_event"}',
        ]
    else:  # reminders
        lines += [
            "— core:add_reminder: параметры text (текст) и time (\"ЧЧ:ММ\"). "
            "Дату пиши в text, отдельного параметра даты нет.",
            "Вызывай ровно в таком формате — id инструмента ОТДЕЛЬНЫМ полем id, "
            "в arguments только параметры:",
            '{"arguments":{"text":"Подготовить отчёт"},"id":"core:add_task"}',
            '{"arguments":{"text":"17.09 собрание — подготовить отчёт",'
            '"time":"11:00"},"id":"core:add_reminder"}',
        ]
    lines += [
        "Если вызов не прошёл из-за схемы аргументов — повтори вызов с "
        "аргументами строго по прикреплённой схеме.",
        "Если инструментов нет в списке — сначала найди их поиском "
        "инструментов (запрос: add_task), потом вызови найденный.",
        "НЕ отвечай «создала» текстом без вызова инструментов. Если не "
        "получилось — честно напиши, что не смогла, и почему.",
    ]
    for it in items:
        lines.append(f"— От {it.get('from_name') or it.get('from_addr')}: "
                     f"{it.get('subject')}")
        for t in it.get("tasks") or []:
            lines.append(f"    задача: {t}")
        for r in it.get("reminders") or []:
            lines.append(f"    срок: {r}")
    return "\n".join(lines)


def parse_llm_answer(text: str) -> Optional[List[Dict[str, Any]]]:
    """Extract the {"items": [...]} object from a chatty model reply."""
    if not text:
        return None
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
