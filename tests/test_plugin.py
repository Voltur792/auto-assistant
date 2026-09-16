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
