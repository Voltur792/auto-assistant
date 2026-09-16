"""Offline tests for the triage layer and the store (no daemon, no sockets)."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import analyze, store


# ---------------------------------------------------------------------------
# analyze.py
# ---------------------------------------------------------------------------

def test_whitelist_substring_case_insensitive():
    acct = {"whitelist": ["Bank.RU", "ivanov"]}
    assert analyze.match_whitelist(acct, "news@bank.ru", "Банк")
    assert analyze.match_whitelist(acct, "x@y.z", "Ivanov I.")
    assert not analyze.match_whitelist(acct, "spam@other.org", "Other")


def test_whitelist_empty_allows_everything():
    assert analyze.match_whitelist({"whitelist": []}, "a@b.c", "Any")


def test_rules_importance_scoring():
    hi = analyze.classify_rules("Счёт к оплате", "Оплатите задолженность в срок")
    assert hi["importance"] == "high"
    lo = analyze.classify_rules("Скидка 50%!", "Только сегодня распродажа unsubscribe")
    assert lo["importance"] == "low"
    mid = analyze.classify_rules("Привет", "Как дела")
    assert mid["importance"] == "normal"


def test_rules_catch_the_user_test_letter():
    # Реальное тестовое письмо пользователя: без LLM правила должны дать high.
    v = analyze.classify_rules("", "Нужно сдать презентацию до 17.09.2026")
    assert v["importance"] == "high"
    assert "сдать презентацию" in v["summary"].casefold()


def test_rules_date_plus_one_high_word_is_high():
    v = analyze.classify_rules("Оплата", "Погасите задолженность до 20.09")
    assert v["importance"] == "high"
    # Дата не спасает рекламу: low-слова перевешивают.
    v2 = analyze.classify_rules("Скидки", "Скидка 50% только до 31.12")
    assert v2["importance"] == "low"


def test_never_noise_senders():
    assert analyze.is_never("newsletter@shop.ru")
    assert not analyze.is_never("boss@company.com")


def test_parse_llm_answer_plain_and_chatty():
    payload = '{"items": [{"n": 1, "importance": "high", "summary": "Оплатить счёт", "tasks": ["Оплатить"], "reminders": ["до пятницы"]}]}'
    assert analyze.parse_llm_answer(payload)[0]["importance"] == "high"
    wrapped = f"Вот результат:\n```json\n{payload}\n```\nГотово!"
    assert analyze.parse_llm_answer(wrapped)[0]["tasks"] == ["Оплатить"]
    assert analyze.parse_llm_answer("никакого json") is None
    assert analyze.parse_llm_answer("") is None


def test_parse_llm_answer_sanitizes():
    bad = '{"items": [{"n": "x", "importance": "MEGA", "summary": 42, "tasks": "одна задача", "reminders": null}]}'
    parsed = analyze.parse_llm_answer(bad)
    assert parsed[0]["importance"] == "normal"
    assert parsed[0]["tasks"] == ["одна задача"]
    assert parsed[0]["reminders"] == []


def test_merge_llm_falls_back_per_item():
    items = [{"subject": "Счёт", "body": "оплатить в срок счёт"},
             {"subject": "Мим", "body": "мим"}]
    parsed = [{"n": 2, "importance": "low", "summary": "мем", "tasks": [], "reminders": []}]
    merged = analyze.merge_llm(items, parsed)
    assert merged[0]["importance"] == "high"      # rules filled the gap
    assert merged[1]["importance"] == "low"       # LLM verdict kept
    merged_none = analyze.merge_llm(items, None)  # LLM totally failed
    assert len(merged_none) == 2


def test_merge_llm_partial_batch_prefiltered_ads():
    # Token economy: obvious-ads items are prefiltered by rules and never
    # reach the LLM; prompt numbering only covers the LLM batch.
    items = [{"subject": "Счёт", "body": "оплатить задолженность в срок"},
             {"subject": "Скидки", "body": "скидка 50% распродажа"},
             {"subject": "Отчёт", "body": "нужно сдать отчёт до 20.09"}]
    parsed = [{"n": 1, "importance": "high", "summary": "счёт", "tasks": ["оплатить"], "reminders": []},
              {"n": 2, "importance": "high", "summary": "отчёт", "tasks": [], "reminders": ["до 20.09"]}]
    merged = analyze.merge_llm(items, parsed, llm_idx=[0, 2])  # ad (idx 1) skipped
    assert merged[0]["summary"] == "счёт"          # LLM n=1 -> items[0]
    assert merged[1]["importance"] == "low"        # rules verdict, no LLM call
    assert merged[2]["reminders"] == ["до 20.09"]  # LLM n=2 -> items[2]
    # Nothing sent to LLM at all (all ads): everything falls back to rules.
    all_ads = analyze.merge_llm(items, None, llm_idx=[])
    assert all_ads[0]["importance"] == "high"
    assert all_ads[1]["importance"] == "low"


# ---------------------------------------------------------------------------
# store.py (isolated via monkeypatched paths)
# ---------------------------------------------------------------------------

@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(store, "_LEGACY_DATA_DIR", tmp_path / "legacy")
    monkeypatch.setattr(store, "_LEGACY_ID_DATA_DIRS", [tmp_path / "old-id"])
    monkeypatch.setattr(store, "_migrated", False)
    return tmp_path


def test_data_dir_env_override(store_env, monkeypatch):
    monkeypatch.setenv("AA_DATA_DIR", str(store_env / "envdir"))
    assert store._default_data_dir() == store_env / "envdir"


def test_migrates_legacy_settings_once(store_env):
    # <=0.1.0 kept data inside the plugin folder; an upgrade must carry the
    # saved app passwords over to the stable dir.
    legacy = store._LEGACY_DATA_DIR
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text(
        json.dumps({"accounts": [{"email": "a@b.c", "password": "secret"}]}),
        encoding="utf-8")
    (legacy / "state.json").write_text(
        json.dumps({"seen_uids": {"x": ["1"]}}), encoding="utf-8")
    s = store.load_settings()
    assert s["accounts"][0]["password"] == "secret"
    assert (store_env / "settings.json").exists()
    assert store.load_state()["seen_uids"]["x"] == ["1"]


def test_migrates_old_id_data_dir(store_env):
    # The pre-release plugin id was astra-auto-assistant and its stable dir
    # was named after it; the rename must not cost the user their passwords.
    old = store_env / "old-id"
    old.mkdir(parents=True)
    (old / "settings.json").write_text(
        json.dumps({"accounts": [{"email": "a@b.c", "password": "pw"}]}),
        encoding="utf-8")
    s = store.load_settings()
    assert s["accounts"][0]["password"] == "pw"
    assert (store_env / "settings.json").exists()


def test_no_migration_when_stable_dir_has_data(store_env):
    # The stable dir wins: legacy leftovers must not overwrite it.
    (store_env / "settings.json").write_text('{"accounts": []}', encoding="utf-8")
    legacy = store._LEGACY_DATA_DIR
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text(
        json.dumps({"accounts": [{"email": "old@x.y"}]}), encoding="utf-8")
    assert store.load_settings()["accounts"] == []


def test_settings_roundtrip_and_defaults(store_env):
    s = store.load_settings()
    assert s["enabled"] is True and s["accounts"] == []
    s["accounts"] = [{"email": "a@b.c"}]
    store.save_settings(s)
    again = store.load_settings()
    assert again["accounts"][0]["email"] == "a@b.c"
    assert again["accounts"][0]["id"]  # id was assigned on normalize


def test_digest_and_seen_caps(store_env):
    for i in range(250):
        store.add_digest_item({"id": f"d{i}", "ts": time.time()})
    state = store.load_state()
    assert len(state["digest"]) == 200
    assert state["digest"][0]["id"] == "d249"     # newest first
    store.mark_seen("acct1", [str(i) for i in range(1000)])
    state = store.load_state()
    assert len(state["seen_uids"]["acct1"]) == store._MAX_SEEN_PER_ACCOUNT


def test_recent_digest(store_env):
    store.add_digest_item({"id": "now", "ts": time.time()})
    store.add_digest_item({"id": "old", "ts": time.time() - 100 * 3600})
    recent = store.recent_digest(24)
    assert [i["id"] for i in recent] == ["now"]
    store.clear_digest()
    assert store.load_state()["digest"] == []


def test_acct_status_roundtrip(store_env):
    state = store.load_state()
    assert state["acct_status"] == {}
    state["acct_status"]["a1"] = {"last_poll": 123.0, "last_error": "", "new_count": 2}
    state["acct_status"]["a2"] = {"last_poll": 123.0, "last_error": "auth failed", "new_count": 0}
    store.save_state(state)
    again = store.load_state()
    assert again["acct_status"]["a1"]["new_count"] == 2
    assert again["acct_status"]["a2"]["last_error"] == "auth failed"


def test_handoff_prompt_names_core_tools():
    # In the plugin conversation core task tools are deferred by tool-search
    # compaction; without the exact ids the model answers "created" without
    # calling anything (seen in the daemon log). Observed failure modes the
    # prompt must guard against: tool id inside arguments ("missing id"),
    # invented params ("reminder_text"/"date" instead of "text"/"time").
    prompt = analyze.build_handoff_prompt(
        [{"from_name": "Иван", "subject": "Отчёт",
          "tasks": ["Сдать отчёт"], "reminders": ["до 18:00"]}],
        deadline_mode="reminders")
    assert "core:add_task" in prompt
    assert "core:add_reminder" in prompt
    # Correct parameter names (from real successful calls in the daemon log).
    assert '"text"' in prompt
    assert '"time"' in prompt
    assert "reminder_text" not in prompt
    # Call examples in the exact JSON shape: id on the TOP level.
    assert '{"arguments":{"text"' in prompt
    assert '"id":"core:add_task"}' in prompt
    assert '"id":"core:add_reminder"}' in prompt
    # Today's date so the model does not guess "tomorrow".
    import time as _time
    assert _time.strftime("%d.%m.%Y") in prompt
    assert "Отчёт" in prompt
    assert "Сдать отчёт" in prompt
    assert "до 18:00" in prompt


def test_handoff_prompt_calendar_mode():
    # calendar.json holds {date, text, time}; the calendar mode must use
    # core:add_calendar_event with an ISO date example and no reminders.
    prompt = analyze.build_handoff_prompt(
        [{"from_name": "Иван", "subject": "Собрание",
          "tasks": ["Отчёт"], "reminders": ["завтра в 11:00 собрание"]}],
        deadline_mode="calendar")
    assert "core:add_calendar_event" in prompt
    assert '"id":"core:add_calendar_event"}' in prompt
    assert '"date":"' in prompt          # ISO date parameter in the example
    assert "не напоминания" in prompt
    assert "core:add_reminder" not in prompt
    prompt_both = analyze.build_handoff_prompt([], deadline_mode="both")
    assert "core:add_calendar_event" in prompt_both
    assert "core:add_reminder" in prompt_both


def test_mark_digest_handled(store_env):
    # After a successful handoff the items get a `handled` flag and are never
    # re-sent to the LLM; clearing the digest resets the flags.
    store.add_digest_item({"id": "a:1", "ts": time.time(), "importance": "high"})
    store.add_digest_item({"id": "a:2", "ts": time.time(), "importance": "high"})
    store.mark_digest_handled(["a:1", "missing"])
    digest = store.load_state()["digest"]
    by_id = {i["id"]: i for i in digest}
    assert by_id["a:1"]["handled"] is True
    assert by_id["a:2"].get("handled") is None
    store.mark_digest_handled([])  # no-op, no crash
    store.clear_digest()
    assert store.load_state()["digest"] == []
