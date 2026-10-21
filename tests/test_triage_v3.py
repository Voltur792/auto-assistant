import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from src import jobs, store, triage

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
LETTER = {"id": "a:7:1", "subject": "Счёт к оплате", "body": "Необходимо оплатить счёт за услуги."}


def proposal(**changes):
    return {"n": 1, "importance": "high", "summary": "Оплатить счёт", "reason": "Выставлен счёт",
            "tasks": ["Оплатить счёт за услуги"], "deadlines": [],
            "action_required": True, "confidence": .95, **changes}


def parse(row=None, letter=None):
    return triage.parse(json.dumps({"items": [row or proposal()]}), [letter or LETTER], NOW)[0]


@pytest.mark.parametrize("subject,body", [
    ("Оформите Каско Драйв", "Полис от 700 рублей в месяц. Оформите полис сейчас."),
    ("Код подтверждения", "Ваш одноразовый код входа: 123456"),
    ("Новая попытка входа", "Если это были не вы, смените пароль"),
    ("Скидка на подписку", "Купите до завтра"),
])
def test_model_cannot_turn_marketing_or_login_codes_into_tasks(subject, body):
    result = parse(letter={"subject": subject, "body": body})
    assert not result["action_required"] and result["tasks"] == []


def test_unicode_obfuscation_in_ad_is_normalized():
    assert triage.rules("Офор\u200bмите Каско", "По\u034fпробуйте полис")['importance'] == 'low'
    assert triage.clean("A&nbsp;B\u200bC") == 'A BC'


def test_invoice_with_unsubscribe_footer_still_actionable():
    assert parse(letter={**LETTER, "body": "Счёт к оплате. Отписаться от рассылки."})["action_required"]


@pytest.mark.parametrize("changes", [
    {"confidence": .84}, {"action_required": False}, {"tasks": []}, {"importance": "normal"},
])
def test_unconfirmed_proposals_never_create_actions(changes):
    result = parse(proposal(**changes))
    assert not result['action_required'] and result['tasks'] == []


@pytest.mark.parametrize("changes", [
    {"n": True}, {"confidence": True}, {"tasks": ["\u200b"]}, {"tasks": "run"},
    {"deadlines": [{"text": "due", "date": "2026-02-31", "time": ""}]},
    {"deadlines": [{"text": "due", "date": "2026-10-22", "time": "9:0"}]},
    {"deadlines": [{"text": "due", "date": "20261022", "time": ""}]},
    {"deadlines": [{"text": "due", "date": "2026-10-22", "time": "18:00", "command": "run"}]},
])
def test_invalid_or_ambiguous_proposals_rejected(changes):
    with pytest.raises(ValueError):
        parse(proposal(**changes))


def test_missing_letter_rejects_whole_batch():
    with pytest.raises(ValueError):
        triage.parse(json.dumps({"items": [proposal()]}), [LETTER, LETTER], NOW)


def test_expired_parcel_is_not_actionable_but_debt_is():
    row = proposal(deadlines=[{"text": "Срок", "date": "2026-10-10", "time": "18:00"}])
    assert not parse(row, {"subject": "Заберите заказ", "body": "Заказ до 2026-10-10"})['action_required']
    debt = parse(row, {"subject": "Задолженность", "body": "Неоплаченный счёт"})
    assert debt['action_required'] and debt['deadlines'] == []


def test_age_uses_received_date_not_import_date():
    item = {"ts": NOW.timestamp(), "mail_ts": (NOW - timedelta(days=10)).timestamp()}
    assert not triage.is_recent(item, 24, NOW.timestamp())
    assert not triage.is_recent({"mail_ts": NOW.timestamp() + 3600}, 24, NOW.timestamp())


def actionable():
    return {**LETTER, **parse()}


def test_queue_is_idempotent_and_leases_exclusive():
    item = actionable()
    assert jobs.enqueue([item], 'calendar') == 1
    assert jobs.enqueue([item], 'calendar') == 0
    first = jobs.claim('window-a')
    assert len(first) == 1 and jobs.claim('window-b') == []
    assert not jobs.finish(first[0]['id'], 'wrong-lease', True)
    assert jobs.finish(first[0]['id'], first[0]['lease'], True)
    assert jobs.enqueue([item], 'calendar') == 0


def test_lost_ack_is_retried_with_same_id_new_lease(monkeypatch):
    jobs.enqueue([actionable()], 'calendar')
    first = jobs.claim('a')[0]
    monkeypatch.setattr(jobs.time, 'time', lambda: first['lease_until'] + 1)
    retry = jobs.claim('b')[0]
    assert retry['id'] == first['id'] and retry['lease'] != first['lease']
    assert not jobs.finish(first['id'], first['lease'], True)


def test_future_time_only_reminder_not_written_on_wrong_day():
    tomorrow = datetime.now() + timedelta(days=1)
    item = {**actionable(), 'deadlines': [{'text': 'Оплата', 'date': tomorrow.date().isoformat(), 'time': '18:00'}]}
    jobs.enqueue([item], 'both')
    assert {j['kind'] for j in store.load_state()['widget_jobs']} == {'tasks', 'calendar', 'reminders'}
    assert {j['kind'] for j in jobs.claim('a')} == {'tasks', 'calendar'}


def test_expired_queued_deadline_fails_visibly_without_creating(monkeypatch):
    tomorrow = datetime.now() + timedelta(days=1)
    item = {**actionable(), 'deadlines': [{'text': 'Оплата', 'date': tomorrow.date().isoformat(), 'time': '18:00'}]}
    jobs.enqueue([item], 'both')
    monkeypatch.setattr(jobs.time, 'time', lambda: (tomorrow + timedelta(days=1)).timestamp())
    assert {j['kind'] for j in jobs.claim('a')} == {'tasks'}
    assert store.load_state()['handoff_error']


def test_legacy_chat_results_are_not_trusted_after_upgrade():
    settings = store.load_settings()
    settings['accounts'] = [{'password': 'test-only', 'email': 'a@example.test'}]
    store.save_settings(settings)
    store.save_state({'digest': [{**LETTER, 'importance': 'high', 'tasks': ['old guess'], 'handoff_plan': {}}],
                      'handoff_error': 'old chat error'})
    store.upgrade_workflow()
    state = store.load_state()
    assert state['digest'][0]['tasks'] == [] and state['digest'][0]['requires_review']
    assert not state['handoff_error'] and 'handoff_plan' not in state['digest'][0]
    assert store.load_settings()['accounts'][0]['password'] == 'test-only'


@pytest.mark.parametrize('subject,body', [
    ('В ваш аккаунт выполнен вход из игры War Thunder',
     'Мы не смогли определить устройство. Если Вы не производили подобных действий, возможно, аккаунт был взломан. Вы можете заблокировать его.'),
    ('Оповещение системы безопасности', 'Вход в аккаунт на устройстве Windows. Если это вы, ничего делать не нужно.'),
    ('Ваша учётная запись — успешный вход', 'Если же вы НЕ выполняли вход, немедленно измените пароль.'),
    ('Разделы «Чаты» и «Статистика» временно недоступны', 'Следим за ситуацией и сообщим о компенсации.'),
    ('О ситуации с сервисами', 'Наши сервисы могут временно работать с перебоями. Мы восстанавливаем доступ.'),
    ('Ваш аккаунт Google восстановлен', 'С возвращением! Если потеряли доступ из-за посторонних, проверьте настройки.'),
    ('Открытый вебинар по офисным приложениям', 'Подключайтесь на демонстрацию. Зарегистрироваться.'),
    ('Как искать работу?', 'Приглашаем на кадровый форум. Регистрация на сайте.'),
])
def test_real_world_false_positives_are_blocked_even_with_confident_model(subject, body):
    result = parse(proposal(confidence=.99), {'subject': subject, 'body': body})
    assert not result['action_required'] and not result['tasks']
    assert 'критич' not in result['reason'].lower()


def test_confirmed_appointment_and_actual_blocked_account_stay_actionable():
    appointment = {'subject': 'Вы записаны на приём', 'body': 'Запись подтверждена. Приходите 22.10.2026 в 19:20.'}
    assert parse(proposal(tasks=['Посетить приём']), appointment)['action_required']
    blocked = {'subject': 'Ваш аккаунт заблокирован', 'body': 'Для доступа необходимо подтвердить личность.'}
    assert parse(proposal(tasks=['Подтвердить личность']), blocked)['action_required']


def test_registered_event_is_not_blocked_by_promotional_footer():
    letter = {'subject': 'Вы зарегистрированы на открытый вебинар',
              'body': 'Регистрация подтверждена. Ваш билет. Приглашаем на другие вебинары.'}
    assert parse(proposal(tasks=['Принять участие']), letter)['action_required']


def test_passive_and_conditional_tasks_are_not_obligations():
    assert not parse(proposal(tasks=['Следить за восстановлением сервиса', 'Ожидать компенсацию']))['action_required']
    result = parse(proposal(tasks=['Вывести средства', 'При необходимости продолжения майнинга: пополнить баланс']))
    assert result['tasks'] == ['Вывести средства']


def test_old_letter_with_future_deadline_is_still_current():
    item = {**actionable(), 'subject': 'Подтверждение встречи', 'body': 'Назначена встреча',
            'tasks': ['Посетить встречу'], 'mail_ts': (NOW-timedelta(days=11)).timestamp(),
            'deadlines': [{'text': 'Деактивация', 'date': '2026-10-31', 'time': ''}]}
    assert triage.has_current_action(item, 48, NOW)
    assert not triage.has_current_action({**item, 'deadlines': []}, 48, NOW)


def test_html_padding_is_removed_before_preview_truncation():
    assert triage.clean('Начало ' + '\u2800'*500 + ' конец') == 'Начало конец'


def test_upgrade_revalidates_old_proposals_without_creating_jobs():
    item = {**LETTER, **parse(), 'triage_version': 3, 'subject': 'Выполнен вход в аккаунт',
            'body': 'Вход в аккаунт на устройстве Windows. Если это вы, ничего делать не нужно.'}
    store.save_state({'workflow_version': 3, 'digest': [item], 'widget_jobs': []})
    store.upgrade_workflow()
    state = store.load_state()
    assert state['workflow_version'] == triage.VERSION
    assert not state['digest'][0]['action_required'] and state['widget_jobs'] == []


def test_upgrade_recovers_future_obligation_excluded_by_old_age_gate():
    tomorrow = (datetime.now()+timedelta(days=1)).date().isoformat()
    settings = store.load_settings(); settings['auto_handoff'] = True; store.save_settings(settings)
    item = {**LETTER, **parse(), 'triage_version': 3, 'mail_ts': (datetime.now()-timedelta(days=11)).timestamp(),
            'deadlines': [{'text': 'Деактивация', 'date': tomorrow, 'time': ''}], 'auto_pending': False}
    store.save_state({'workflow_version': 3, 'digest': [item], 'widget_jobs': []})
    store.upgrade_workflow()
    assert store.load_state()['digest'][0]['auto_pending'] is True
    assert store.load_state()['widget_jobs'] == []  # Normal poll still performs creation.


def appointment_items():
    tomorrow = (datetime.now()+timedelta(days=1)).date().isoformat()
    first = {**actionable(), 'id': 'booking', 'account_id': 'a',
        'body': 'Запись на приём к врачу. Вы записаны на приём Тест Тестович Запись подтверждена Адрес: Тестовая улица, 1 Статус: Запись подтверждена',
        'tasks': ['Посетить приём'], 'deadlines': [{'text': 'Приём у врача', 'date': tomorrow, 'time': '19:20'}]}
    second = {**first, 'id': 'reminder',
        'body': 'Напоминаем о приёме у врача. Тест Тестович, ждём вас. Адрес: Тестовая улица, 1 Статус: Запись подтверждена',
        'tasks': ['Прийти на приём к врачу'], 'deadlines': [{'text': 'Приём к врачу', 'date': tomorrow, 'time': '19:20'}]}
    return first, second


def test_confirmation_and_reminder_of_same_explicit_booking_share_records():
    items = appointment_items()
    for item in items: store.add_digest_item(item)
    assert jobs.enqueue(list(items), 'calendar') == 2
    claimed = jobs.claim('window')
    assert len(claimed) == 2
    for job in claimed:
        assert set(job['mail_ids']) == {'booking', 'reminder'}
        jobs.finish(job['id'], job['lease'], True)
    assert all(i['handled'] for i in store.load_state()['digest'])


def test_same_time_and_address_do_not_merge_different_patients():
    first, second = appointment_items()
    second['body'] = second['body'].replace('Тест Тестович', 'Другой Пациент')
    for item in [first, second]: store.add_digest_item(item)
    assert jobs.enqueue([first, second], 'calendar') == 4


@pytest.mark.parametrize('status', ['done', 'failed'])
def test_new_booking_identity_adopts_existing_legacy_records(status):
    first, second = appointment_items()
    # Older worker generated mail-specific IDs for a reminder without a patient label.
    old_jobs = jobs.operations(second, 'calendar')
    for job in old_jobs: job.update(status=status)
    for item in [first, second]: store.add_digest_item(item)
    store.update_state(lambda state: state.update(widget_jobs=old_jobs))
    assert jobs.enqueue([first], 'calendar') == (0 if status=='done' else 2)
    assert {j['id'] for j in store.load_state()['widget_jobs']} == {j['id'] for j in old_jobs}
    assert bool(next(i for i in store.load_state()['digest'] if i['id']=='booking').get('handled')) == (status=='done')


@pytest.mark.parametrize('subject', ['Квитанция на оплату услуг', 'У вас есть непрочитанное сообщение от органа власти'])
def test_confirmed_unfinished_obligation_is_not_lost_after_two_days(subject):
    item = {**actionable(), 'subject': subject, 'mail_ts': (NOW-timedelta(days=5)).timestamp()}
    assert triage.has_current_action(item, 48, NOW)
    assert not triage.has_current_action({**item, 'action_required': False}, 48, NOW)
