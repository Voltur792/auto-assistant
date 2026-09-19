"""Tests for AstraAutoAssistant.

Run: `pytest`.

This is level 1: in process, no daemon, no socket, fast enough to run on every
save. It still goes through the real gRPC servicer, so a tool that is declared
but not routed fails here. When you want the other level — a real handshake, a
real session token, real protobuf encoding — reach for `WireHarness` from the
same module.
"""

import sys
from pathlib import Path

# The daemon puts the bundle root on `sys.path` before importing `src.plugin`;
# do the same so `pytest` from the project root finds it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astra_plugin_sdk.testing import Harness, fuzz_configs  # noqa: E402

from src.plugin import AstraAutoAssistant  # noqa: E402

def test_the_plugin_starts_and_answers_a_health_check():
    with Harness(AstraAutoAssistant()) as h:
        healthy, _status = h.health()
        assert healthy


def test_no_config_the_daemon_can_deliver_crashes_this_plugin():
    # The daemon delivers config it did not author: the user's typing, and an
    # older version of this plugin's own schema. `{}` — a fresh install — is
    # the first payload every plugin ever sees. None of it may throw.
    with Harness(AstraAutoAssistant()) as h:
        for payload in fuzz_configs():
            h.set_config(payload)


def test_clean_handoff_answer_replaces_parroted_tool_calls():
    # The model once answered the handoff with the prompt's call examples as
    # TEXT (two {"arguments":…} lines) instead of calling the tools — the
    # tab showed raw JSON. The cleaner keeps human text and collapses every
    # pure-JSON line (and code fences) into one readable marker.
    raw = ('{"arguments":{"text":"Собрание","date":"20.09.2026",'
           '"time":"18:00"},"id":"core:add_calendar_event"}\n'
           '{"arguments":{"text":"Подготовиться","id":"core:add_task"}')
    cleaned = AstraAutoAssistant._clean_handoff_answer(raw)
    assert '{"arguments"' not in cleaned
    assert "JSON-текстом" in cleaned
    # Fenced triage-style answers shrink to the marker too.
    fenced = '```json\n{"items": []}\n```'
    cleaned2 = AstraAutoAssistant._clean_handoff_answer(fenced)
    assert "```" not in cleaned2 and "items" not in cleaned2
    # A normal human reply passes through untouched.
    human = "Создала задачу «Подготовить отчёт» и запись в календаре."
    assert AstraAutoAssistant._clean_handoff_answer(human) == human


def test_clean_handoff_answer_replaces_triage_lines():
    # After the triage switched to line format, the model answered the
    # HANDOFF with a triage line («1 | normal | … | … | …») as its final
    # text — the tab and the voice read it out. Triage-shaped lines are
    # collapsed into a marker; human text around them survives.
    raw = ("1 | normal | Запланировано собрание на 21 сентября 2026 года "
           "в 17:00. | Подготовиться к собранию | 21.09.2026; 17:00")
    cleaned = AstraAutoAssistant._clean_handoff_answer(raw)
    assert "|" not in cleaned
    assert "строкой разбора" in cleaned
    mixed = "Готово.\n2 | high | Счёт | Оплатить | 20.09"
    cleaned2 = AstraAutoAssistant._clean_handoff_answer(mixed)
    assert "Готово." in cleaned2 and "|" not in cleaned2
    # A sentence that merely contains a number and one pipe is NOT triage.
    assert AstraAutoAssistant._clean_handoff_answer(
        "Задача 3 | создана") == "Задача 3 | создана"


def test_handoff_verified_with_tech_answer_gets_readable_message(
        tmp_path, monkeypatch):
    # Regression (19.09, real): the model created the task AND answered
    # with a triage line as text — the tab showed the raw line. A verified
    # handoff whose reply is a parroted format gets a synthesized readable
    # message instead.
    import asyncio

    from src import store

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(store, "_migrated", False)
    store.save_settings(store.DEFAULT_SETTINGS)
    store.add_digest_item({"id": "a1:10", "importance": "high"})

    plugin = AstraAutoAssistant()
    plugin.host = object()

    async def fake_ask(prompt, voice=True):
        return ("1 | normal | Запланировано собрание | "
                "Подготовиться | 21.09.2026; 17:00")

    counts = iter([[3, 0, 2], [4, 0, 3]])   # +1 task, +1 calendar event
    monkeypatch.setattr(plugin, "_ask_astra", fake_ask)
    monkeypatch.setattr(AstraAutoAssistant, "_astra_task_counts",
                        staticmethod(lambda: next(counts)))

    result = asyncio.run(plugin._handoff(
        [{"account_id": "a1", "uid": "10", "from_name": "Я",
          "subject": "собрание", "tasks": [], "reminders": []}]))
    assert result["success"] is True and result["verified"] is True
    assert "|" not in result["answer"]
    assert "создала задачи" in result["answer"]
    assert "создано задач 1" in result["answer"]
    state = store.load_state()
    assert state["digest"][0]["handled"] is True
    assert "|" not in state["handoff_answer"]


def test_handoff_empty_reply_with_created_tasks_is_success(tmp_path, monkeypatch):
    # Regression (19.09, real): the model called core:add_task and
    # core:add_calendar_event but answered with NO final text — the plugin
    # recorded "Astra не ответила (пустой ответ)" and the tab claimed the
    # handoff failed while the task existed. Grown task counters outrank
    # the reply text: an empty reply with creation verified is a success
    # and marks the letters handled.
    import asyncio

    from src import store

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(store, "_migrated", False)
    store.save_settings(store.DEFAULT_SETTINGS)
    store.add_digest_item({"id": "a1:9", "importance": "high"})

    plugin = AstraAutoAssistant()
    plugin.host = object()          # chat channel "available"

    async def fake_ask(prompt, voice=True):
        return ""                   # tools ran, model said nothing

    counts = iter([[2, 0, 1], [3, 0, 2]])   # +1 task, +1 calendar event
    monkeypatch.setattr(plugin, "_ask_astra", fake_ask)
    monkeypatch.setattr(AstraAutoAssistant, "_astra_task_counts",
                        staticmethod(lambda: next(counts)))

    result = asyncio.run(plugin._handoff(
        [{"account_id": "a1", "uid": "9", "from_name": "Я",
          "subject": "собрание", "tasks": [], "reminders": []}]))
    assert result["success"] is True
    assert result["verified"] is True
    assert "создано задач 1" in result["answer"]
    state = store.load_state()
    assert state["handoff_error"] == ""
    assert "создала задачи" in state["handoff_answer"]
    assert state["digest"][0]["handled"] is True


def test_handoff_empty_reply_without_creation_is_failure(tmp_path, monkeypatch):
    # Empty reply and nothing created (or verification impossible) stays a
    # failure: the letters are NOT marked handled and stay re-sendable.
    import asyncio

    from src import store

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(store, "_migrated", False)
    store.save_settings(store.DEFAULT_SETTINGS)
    store.add_digest_item({"id": "a1:9", "importance": "high"})

    plugin = AstraAutoAssistant()
    plugin.host = object()

    async def fake_ask(prompt, voice=True):
        return ""

    counts = iter([[2, 0, 1], [2, 0, 1]])   # nothing created
    monkeypatch.setattr(plugin, "_ask_astra", fake_ask)
    monkeypatch.setattr(AstraAutoAssistant, "_astra_task_counts",
                        staticmethod(lambda: next(counts)))

    result = asyncio.run(plugin._handoff(
        [{"account_id": "a1", "uid": "9", "from_name": "Я",
          "subject": "собрание", "tasks": [], "reminders": []}]))
    assert result["success"] is False
    assert "пустой ответ" in result["error"]
    state = store.load_state()
    assert state["handoff_error"]
    assert "handled" not in state["digest"][0]


def test_astra_task_counts_best_effort(tmp_path, monkeypatch):
    # Verification reads Astra's own tasks/reminders/calendar; a missing or
    # unreadable file means "skip verification", never an exception.
    import json as _json

    cfg = tmp_path / "Astra" / "astra" / "config"
    cfg.mkdir(parents=True)
    (cfg / "tasks.json").write_text(
        _json.dumps([{"text": "a"}, {"text": "b"}]), encoding="utf-8")
    (cfg / "reminders.json").write_text("[]", encoding="utf-8")
    (cfg / "calendar.json").write_text(
        _json.dumps([{"date": "2026-09-17", "text": "собрание",
                      "time": "11:00"}]), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert AstraAutoAssistant._astra_task_counts() == [2, 0, 1]
    (cfg / "calendar.json").write_text("not json at all", encoding="utf-8")
    assert AstraAutoAssistant._astra_task_counts() is None
    (cfg / "calendar.json").unlink()
    assert AstraAutoAssistant._astra_task_counts() is None


def test_own_mail_is_high_in_digest_summary_and_handoff_list(tmp_path, monkeypatch):
    # Regression (18.09, real): a letter from the user's own account was
    # marked high only in the digest COPY of the verdict — the trigger fired,
    # but the handoff list was built from the raw LLM/rules verdict ("normal"),
    # so no tasks were ever requested. Digest, summary and the handoff list
    # must all see the same final verdict.
    import asyncio
    import time as _time

    from src import mailer, store

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(store, "_migrated", False)
    acct = {"id": "a1", "provider": "yandex", "email": "me@ya.ru",
            "password": "pw", "imap_host": "", "imap_port": 993,
            "enabled": True, "whitelist": []}
    store.save_settings({**store.DEFAULT_SETTINGS, "accounts": [acct],
                          "mode": "rules", "llm_enabled": False,
                          "auto_handoff": False})
    letters = [{"uid": "5", "from_name": "Я", "from_addr": "me@ya.ru",
                "subject": "Тестовое собрание", "ts": _time.time(),
                "body": "тест"}]
    monkeypatch.setattr(mailer, "fetch_new_emails",
                        lambda a, base, limit: (letters, 5))

    plugin = AstraAutoAssistant()
    plugin._poll_lock = asyncio.Lock()
    report = asyncio.run(plugin.poll_once(auto=False))

    state = store.load_state()
    assert state["digest"][0]["importance"] == "high"
    assert "важных 1" in report       # the summary counts it too
    assert state["acct_status"]["a1"]["new_count"] == 1
