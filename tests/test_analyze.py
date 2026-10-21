"""Rules, allow-list and persistence: offline and isolated."""
import json
import time
import pytest
from src import analyze, store


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


def test_bulk_senders_are_narrow():
    assert analyze.is_bulk("newsletter@shop.ru")
    assert analyze.is_bulk("Promo@Shop.RU")
    assert not analyze.is_bulk("boss@company.com")
    # Регресс: noreply@ раньше означал «выбросить не глядя», хотя с таких
    # адресов пишут банки и госуслуги — письма молча пропадали.
    for addr in ("noreply@bank.ru", "no-reply@company.com", "news@steam.com",
                 "mailer@service.io"):
        assert not analyze.is_bulk(addr)


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AA_DATA_DIR", raising=False)
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


def test_explicit_data_dir_does_not_import_legacy_passwords(store_env, monkeypatch):
    monkeypatch.setenv("AA_DATA_DIR", str(store_env))
    store._LEGACY_DATA_DIR.mkdir()
    (store._LEGACY_DATA_DIR / "settings.json").write_text(
        '{"accounts": [{"password": "private"}]}', encoding="utf-8")
    assert store.load_settings()["accounts"] == []


def test_migrates_legacy_settings_once(store_env):
    # <=0.1.0 kept data inside the plugin folder; an upgrade must carry the
    # saved app passwords over to the stable dir.
    legacy = store._LEGACY_DATA_DIR
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text(
        json.dumps({"accounts": [{"email": "a@b.c", "password": "secret"}]}),
        encoding="utf-8")
    (legacy / "state.json").write_text(
        json.dumps({"acct_status": {"x": {"last_uid": 123}}}), encoding="utf-8")
    s = store.load_settings()
    assert s["accounts"][0]["password"] == "secret"
    assert (store_env / "settings.json").exists()
    assert store.load_state()["acct_status"]["x"]["last_uid"] == 123


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


def test_digest_cap_and_legacy_keys_dropped(store_env):
    for i in range(250):
        store.add_digest_item({"id": f"d{i}", "ts": time.time()})
    state = store.load_state()
    assert len(state["digest"]) == 200
    assert state["digest"][0]["id"] == "d249"     # newest first
    # Old state files carried seen_uids (IMAP sequence numbers, meaningless
    # now) and events (removed Telegram bridge) — they must not survive a save.
    state["seen_uids"] = {"acct1": ["1", "2"]}
    state["events"] = []
    store.save_state(state)
    again = store.load_state()
    assert "seen_uids" not in again and "events" not in again


def test_add_digest_item_replaces_a_refetched_letter(store_env):
    # A rewind or a refetch re-delivers a letter that is already in the
    # digest: it must replace the old entry, not duplicate it, and keep the
    # handled mark so Astra is not asked for the same tasks twice.
    assert store.add_digest_item({"id": "a:5", "importance": "normal"}) is True
    store.mark_digest_handled(["a:5"])
    assert store.add_digest_item({"id": "a:5", "importance": "high"}) is False
    state = store.load_state()
    assert len(state["digest"]) == 1
    assert state["digest"][0]["importance"] == "high"
    assert state["digest"][0]["handled"] is True


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
