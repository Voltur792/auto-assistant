"""Offline regressions for mailbox identity, state updates and native paths."""
import asyncio
import copy
from src import handoff, mailer, store
from src.plugin import AstraAutoAssistant


def configured():
    settings = copy.deepcopy(store.DEFAULT_SETTINGS)
    settings["accounts"] = [{"id": "a", "email": "old@example.test",
                              "password": "test-only", "enabled": True}]
    store.save_settings(settings)
    return AstraAutoAssistant()


def test_rewind_preserves_changes_made_during_imap_request(monkeypatch):
    plugin = configured()
    store.add_digest_item({"id": "a:1", "tasks": ["old"]})

    def rewind(*args):
        store.add_digest_item({"id": "a:2", "subject": "Concurrent letter"})
        store.mark_digest_handled(["a:1"])
        return 0, "1"

    async def poll(*args, **kwargs):
        assert not plugin._poll_lock.locked()
        return "offline"

    monkeypatch.setattr(mailer, "rewind_baseline", rewind)
    monkeypatch.setattr(plugin, "poll_once", poll)
    result = asyncio.run(plugin.ui_rewind_baseline())
    assert result["success"]
    digest = {it["id"]: it for it in store.load_state()["digest"]}
    assert "a:2" in digest and digest["a:1"]["handled"]


def test_rewind_during_poll_does_not_write_state(monkeypatch):
    plugin = configured()

    async def run():
        plugin._poll_lock = asyncio.Lock()
        async with plugin._poll_lock:
            result = await plugin.ui_rewind_baseline()
            assert "уже выполняется" in result["error"]
    asyncio.run(run())
    assert store.load_state()["acct_status"] == {}


def test_mailbox_change_starts_new_namespace_and_keeps_history(monkeypatch):
    plugin = configured()
    monkeypatch.setattr(plugin, "_ensure_poller", lambda: None)
    store.save_state({"acct_status": {"a": {"last_uid": 50000}},
                      "digest": [{"id": "a:5", "handled": True}]})
    result = asyncio.run(plugin.ui_save_settings(settings={"accounts": [
        {"id": "a", "email": "new@example.test", "password": "new-test-only"}]}))
    new_id = result["settings"]["accounts"][0]["id"]
    assert new_id != "a" and result["account_ids"] == {"a": new_id}
    assert store.load_state()["acct_status"] == {}
    assert store.load_state()["digest"][0]["handled"]
    assert "password" not in result["settings"]["accounts"][0]


def test_password_change_keeps_namespace(monkeypatch):
    plugin = configured()
    monkeypatch.setattr(plugin, "_ensure_poller", lambda: None)
    store.save_state({"acct_status": {"a": {"last_uid": 99}}, "digest": []})
    result = asyncio.run(plugin.ui_save_settings(settings={"accounts": [
        {"id": "a", "email": " OLD@example.test ", "password": "new-test-only"}]}))
    assert result["settings"]["accounts"][0]["id"] == "a"
    assert store.load_state()["acct_status"]["a"]["last_uid"] == 99


def test_saved_password_is_not_reused_for_changed_server(monkeypatch):
    plugin = configured()
    monkeypatch.setattr(plugin, "_ensure_poller", lambda: None)
    result = asyncio.run(plugin.ui_save_settings(settings={"accounts": [
        {"id": "a", "email": "old@example.test", "provider": "custom",
         "imap_host": "different.example.test"}]}))
    assert result["settings"]["accounts"][0]["has_password"] is False


def test_connection_test_requires_password_for_a_different_mailbox(monkeypatch):
    plugin = configured()

    def forbidden(*args):
        raise AssertionError("Old password must not be used with a different server")

    monkeypatch.setattr(mailer, "test_account", forbidden)
    result = asyncio.run(plugin.ui_test_account(account={
        "id": "a", "email": "old@example.test", "imap_host": "other.example.test"}))
    assert "пароль приложения" in result["error"]


def test_uidvalidity_adoption_preserves_handled_and_partial_progress():
    state = {"digest": [{"id": "a:5", "account_id": "a", "uid": "5", "handled": True,
                          "handoff_completed": {"op": "result"}}]}
    store.adopt_uidvalidity(state, "a", "7")
    assert state["digest"][0]["id"] == "a:5"
    assert state["digest"][0]["uidvalidity"] == "7"
    assert state["digest"][0]["handled"]
    store.adopt_uidvalidity(state, "a", "8")
    assert state["digest"][0]["uidvalidity"] == "7"  # known namespaces never rewritten


def test_old_digest_key_and_progress_survive_namespace_upgrade():
    store.add_digest_item({"id": "a:5", "account_id": "a", "uid": "5", "handled": True})
    store.update_state(lambda state: store.adopt_uidvalidity(state, "a", "7"))
    assert store.add_digest_item({"id": "a:7:5", "account_id": "a", "uid": "5", "uidvalidity": "7"}) is False
    assert store.load_state()["digest"][0]["id"] == "a:5"
    assert store.load_state()["digest"][0]["handled"]
    assert store.add_digest_item({"id": "a:8:5", "account_id": "a", "uid": "5", "uidvalidity": "8"}) is True
    assert len(store.load_state()["digest"]) == 2


def test_poll_does_not_restore_a_mailbox_changed_during_fetch(monkeypatch):
    plugin = configured()
    settings = store.load_settings()
    settings.update(mode="rules", llm_enabled=False)
    store.save_settings(settings)
    monkeypatch.setattr(plugin, "_ensure_poller", lambda: None)

    async def worker(fn, *args):
        await plugin.ui_save_settings(settings={"accounts": [{
            "id": "a", "email": "new@example.test", "password": "new-only"}]})
        return ([{"uid": "1", "from_name": "Sender", "from_addr": "old@example.test",
                  "subject": "Old account", "body": "", "ts": 0}], 1, "1")

    monkeypatch.setattr(asyncio, "to_thread", worker)
    asyncio.run(plugin.poll_once(auto=False))
    assert store.load_state()["digest"] == []
    assert store.load_state()["acct_status"] == {}


def test_linux_default_config_path_ignores_relative_xdg(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(handoff.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert handoff.config_dir() == Path.home() / ".config" / "astra"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert handoff.config_dir() == Path.home() / ".config" / "astra"
