"""The local model returns data; only native widget jobs can create records."""
import asyncio
import json
import time
from src import inference, mailer, store, triage
from src.plugin import AstraAutoAssistant


class NoChatHost:
    def send_chat_message(self, *args, **kwargs):
        raise AssertionError('Mail must NEVER enter an action-capable chat')


def setup_poll(monkeypatch, *, old=False):
    plugin = AstraAutoAssistant()
    plugin.host = NoChatHost()
    settings = dict(store.DEFAULT_SETTINGS)
    settings.update(mode='llm', auto_handoff=True, accounts=[{
        'id': 'a', 'email': 'me@example.test', 'password': 'test-only'}])
    store.save_settings(settings)
    letter = {'uid': '1', 'from_name': 'Test Billing', 'from_addr': 'billing@example.test',
              'subject': 'Счёт к оплате', 'body': 'Необходимо оплатить выставленный счёт.',
              'ts': time.time() - (10 * 86400 if old else 30)}
    monkeypatch.setattr(mailer, 'fetch_new_emails', lambda *args: ([letter], 1, '7'))
    return plugin


def valid_reply(items, settings, now):
    return json.dumps({'items': [{'n': i, 'importance': 'high', 'summary': 'Оплатить счёт',
        'reason': 'Выставлен реальный счёт', 'action_required': True, 'confidence': 0.95,
        'tasks': ['Оплатить счёт за услуги'], 'deadlines': []}
        for i, _ in enumerate(items, 1)]}, ensure_ascii=False)


def test_new_actionable_mail_queues_native_records_without_chat(monkeypatch):
    plugin = setup_poll(monkeypatch)
    monkeypatch.setattr(inference, 'complete', valid_reply)
    asyncio.run(plugin.poll_once(auto=True))
    state = store.load_state()
    assert state['digest'][0]['action_required'] is True
    assert state['widget_jobs'][0]['kind'] == 'tasks'
    assert state['widget_jobs'][0]['status'] == 'pending'
    assert not state['digest'][0].get('handled')


def test_history_read_never_creates_tasks(monkeypatch):
    plugin = setup_poll(monkeypatch)
    monkeypatch.setattr(inference, 'complete', valid_reply)
    asyncio.run(plugin.poll_once(auto=False, historical=True))
    assert store.load_state().get('widget_jobs', []) == []


def test_old_mail_without_future_or_ongoing_obligation_is_not_auto_created(monkeypatch):
    plugin = setup_poll(monkeypatch, old=True)
    original = mailer.fetch_new_emails
    def old_letter(*args):
        messages, high, validity = original(*args)
        messages[0].update(subject='Материалы встречи', body='Материалы прошедшей встречи.')
        return messages, high, validity
    monkeypatch.setattr(mailer, 'fetch_new_emails', old_letter)
    def respond(*args):
        data = json.loads(valid_reply(*args))
        data['items'][0].update(tasks=['Изучить материалы встречи'], reason='Материалы по работе')
        return json.dumps(data)
    monkeypatch.setattr(inference, 'complete', respond)
    asyncio.run(plugin.poll_once(auto=True))
    assert store.load_state().get('widget_jobs', []) == []


def test_failed_analysis_does_not_auto_create_or_retry_immediately(monkeypatch):
    plugin = setup_poll(monkeypatch)
    calls = []
    def fail(*args):
        calls.append(1)
        raise inference.InferenceError('offline')
    monkeypatch.setattr(inference, 'complete', fail)
    asyncio.run(plugin.poll_once(auto=True))
    asyncio.run(plugin.poll_once(auto=True))
    assert len(calls) == 1
    assert store.load_state().get('widget_jobs', []) == []
    assert plugin._llm_error


def test_transient_model_failure_does_not_lose_letter_after_uid_advances(monkeypatch):
    plugin = setup_poll(monkeypatch)
    def fail(*args):
        raise inference.InferenceError('offline')
    monkeypatch.setattr(inference, 'complete', fail)
    asyncio.run(plugin.poll_once(auto=True))
    assert store.load_state()['digest'][0]['analysis_pending']
    # IMAP cursor has advanced, so the server no longer returns this letter.
    monkeypatch.setattr(mailer, 'fetch_new_emails', lambda *args: ([], 1, '7'))
    monkeypatch.setattr(inference, 'complete', valid_reply)
    plugin._inference_retry_after = 0
    asyncio.run(plugin.poll_once(auto=True))
    state = store.load_state()
    assert not state['digest'][0]['analysis_pending']
    assert not state['digest'][0]['auto_pending']
    assert len(state['widget_jobs']) == 1


def test_pending_analysis_survives_refetch_after_interrupted_cursor_save(monkeypatch):
    plugin = setup_poll(monkeypatch)
    def fail(*args):
        raise inference.InferenceError('offline')
    monkeypatch.setattr(inference, 'complete', fail)
    asyncio.run(plugin.poll_once(auto=True))
    # The same UID is fetched again, as after a crash before cursor persistence.
    monkeypatch.setattr(inference, 'complete', valid_reply)
    plugin._inference_retry_after = 0
    asyncio.run(plugin.poll_once(auto=True))
    assert len(store.load_state()['widget_jobs']) == 1


def test_partial_model_failure_cancels_auto_creation_for_entire_poll(monkeypatch):
    plugin = setup_poll(monkeypatch)
    original = mailer.fetch_new_emails
    monkeypatch.setattr(mailer, 'fetch_new_emails', lambda *args: (
        [{**original(*args)[0][0], 'uid': str(n)} for n in range(4)], 4, '7'))
    calls = []
    def respond(*args):
        calls.append(1)
        if len(calls) > 1: raise inference.InferenceError('timeout')
        return valid_reply(*args)
    monkeypatch.setattr(inference, 'complete', respond)
    asyncio.run(plugin.poll_once(auto=True))
    assert len(calls) == 2
    assert store.load_state().get('widget_jobs', []) == []


def test_native_confirmation_controls_handled_flag():
    plugin = AstraAutoAssistant()
    item = {'id': 'a:7:1', 'importance': 'high', 'action_required': True,
            'triage_version': triage.VERSION, 'tasks': ['Подготовить отчёт'], 'deadlines': []}
    store.add_digest_item(item)
    result = asyncio.run(plugin._handoff([item]))
    assert result['pending'] is True
    claimed = asyncio.run(plugin.ui_widget_jobs(worker='test-window'))['jobs']
    assert len(claimed) == 1
    result = asyncio.run(plugin.ui_widget_result(id=claimed[0]['id'], lease=claimed[0]['lease'], success=True))
    assert result['success']
    assert store.load_state()['digest'][0]['handled'] is True


def test_controller_is_a_trusted_dom_effect():
    contributions = asyncio.run(AstraAutoAssistant().get_ui_contributions())
    assert len(contributions) == 2
    assert contributions[0].url == 'controller.js'
    assert contributions[0].slot == 'background.behind'
    assert contributions[1].slot == 'page.custom'
    assert contributions[1].label == 'Помощник' and contributions[1].url == 'page.js'


def test_manual_ui_check_returns_before_slow_model_and_rejects_duplicate(monkeypatch):
    plugin = AstraAutoAssistant()
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def slow_poll(*args, **kwargs):
            entered.set()
            await release.wait()
            return 'done'
        monkeypatch.setattr(plugin, 'poll_once', slow_poll)
        result = await plugin.ui_run_digest()
        assert result['success']
        await entered.wait()
        duplicate = await plugin.ui_run_digest()
        assert duplicate['success'] is False
        release.set()
        await plugin._manual_task
    asyncio.run(run())


def test_old_future_obligation_is_available_to_manual_creation():
    from datetime import datetime, timedelta
    tomorrow = (datetime.now()+timedelta(days=1)).date().isoformat()
    item = {'id': 'a:7:future', 'triage_version': triage.VERSION, 'source': 'llm',
        'importance': 'high', 'action_required': True, 'confidence': .98,
        'mail_ts': time.time()-11*86400, 'tasks': ['Вывести остаток средств'],
        'deadlines': [{'text': 'Деактивация', 'date': tomorrow, 'time': ''}]}
    store.add_digest_item(item)
    plugin = AstraAutoAssistant()
    result = asyncio.run(plugin.ui_handoff())
    assert result['success'] and result['pending']
    assert {j['kind'] for j in store.load_state()['widget_jobs']} == {'tasks', 'calendar'}
