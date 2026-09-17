/* Логика вкладки «Помощник». Всё тяжёлое — в бэкенде плагина;
   здесь только запросы через astra.callBackend и отрисовка. */
'use strict';

const S = { settings: null, digest: [], status: {} };
let loadedOnce = false;
let statusDeadline = 0;   // локальный отсчёт до следующей проверки

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function call(method, params = {}) {
    if (!window.astra || !astra.callBackend) throw new Error('Мост Astra недоступен');
    return await astra.callBackend(method, params);
}

/* ---------- Кастомный дропдаун (нативный select рисует ОС) ---------- */
function dropdown(container, options, value, onChange) {
    container.innerHTML = '';
    container.classList.add('dd');
    const btn = document.createElement('button');
    btn.className = 'dd-btn';
    btn.type = 'button';
    const menu = document.createElement('div');
    menu.className = 'dd-menu';
    container.appendChild(btn);
    container.appendChild(menu);

    const label = document.createElement('span');
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = '▾';
    btn.append(label, caret);

    let current = value;
    const render = () => {
        const opt = options.find((o) => o[0] === current);
        label.textContent = opt ? opt[1] : String(current || '—');
        menu.innerHTML = '';
        for (const [v, l] of options) {
            const it = document.createElement('div');
            it.className = 'dd-item' + (v === current ? ' sel' : '');
            it.textContent = l;
            it.onclick = (e) => {
                e.stopPropagation();
                current = v;
                container.classList.remove('open');
                render();
                onChange(v);
            };
            menu.appendChild(it);
        }
    };
    btn.onclick = (e) => {
        e.stopPropagation();
        document.querySelectorAll('.dd.open').forEach((d) => { if (d !== container) d.classList.remove('open'); });
        container.classList.toggle('open');
    };
    render();
    return { set(v) { current = v; render(); } };
}
document.addEventListener('click', () => {
    document.querySelectorAll('.dd.open').forEach((d) => d.classList.remove('open'));
});

/* ---------- Вкладки ---------- */
document.querySelectorAll('.tab-btn').forEach((b) => {
    b.onclick = () => {
        document.querySelectorAll('.tab-btn').forEach((x) => x.classList.toggle('active', x === b));
        document.querySelectorAll('.tab-content').forEach((s) =>
            s.classList.toggle('active', s.id === 'tab-' + b.dataset.tab));
        if (b.dataset.tab !== 'mail') refresh();   // форму почты не перезаписываем
    };
});

/* ---------- Статус-пилюля ---------- */
function renderStatus() {
    const pill = $('statusPill');
    const st = S.status || {};
    const s = S.settings || {};
    if (!s.enabled) {
        pill.className = 'status idle';
        pill.textContent = 'Пауза';
        return;
    }
    if (st.last_error) {
        pill.className = 'status err';
        pill.textContent = st.last_error.slice(0, 90);
        return;
    }
    if (s.llm_enabled && st.llm_error) {
        // LLM включён, но не работает (обычно — право отказано при установке
        // из файла). Показываем причину, а не молчим.
        pill.className = 'status err';
        pill.title = st.llm_error;
        pill.textContent = 'LLM: ' + st.llm_error.replace(/\s+/g, ' ').slice(0, 80);
        return;
    }
    if (st.handoff_error) {
        // «Создать задачи через Астра» не сработал — говорим почему.
        pill.className = 'status err';
        pill.title = st.handoff_error;
        pill.textContent = 'Хэндофф: ' + st.handoff_error.replace(/\s+/g, ' ').slice(0, 80);
        return;
    }
    const secs = Math.max(0, Math.round(statusDeadline - Date.now() / 1000));
    const m = Math.floor(secs / 60), sec = secs % 60;
    pill.className = 'status ok';
    pill.textContent = loadedOnce ? `След. проверка через ${m}:${String(sec).padStart(2, '0')}` : 'Загрузка…';
}
setInterval(renderStatus, 1000);

/* ---------- Дайджест ---------- */
function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}
const IMP_LABEL = { high: 'Важно', normal: 'Обычное', low: 'Меньше' };

function renderDigest() {
    const box = $('digestList');
    if (!S.digest.length) {
        const hasAcct = (((S.settings || {}).accounts) || []).length;
        const hint = hasAcct
            ? 'Пока пусто. Сообщаю только о письмах, пришедших после первой проверки — нажми «⟲ Последние письма», чтобы разобрать то, что уже лежит непрочитанным.'
            : 'Пока пусто. Добавь почтовый ящик во вкладке «Почта» — помощник разберёт новые письма.';
        box.innerHTML = `<div class="placeholder"><div class="big">✉️</div>${hint}</div>`;
        return;
    }
    box.innerHTML = S.digest.map((it) => {
        const lists = [];
        (it.tasks || []).forEach((t) => lists.push(`<div>✓ ${esc(t)}</div>`));
        (it.reminders || []).forEach((r) => lists.push(`<div>⏰ ${esc(r)}</div>`));
        return `<div class="card ${it.importance === 'high' ? 'important' : ''}${it.handled ? ' handled' : ''}">
            <div class="item-head">
                <div class="item-title">
                    <div class="item-subject">${esc(it.subject || '(без темы)')}</div>
                    <div class="item-from">${esc(it.from_name || it.from_addr)} · ${esc(it.account_email)}</div>
                </div>
                <span class="badge ${esc(it.importance)}">${IMP_LABEL[it.importance] || ''}</span>
                ${it.handled ? '<span class="badge handled" title="Задачи по этому письму уже запрошены у Астры — повторно не отправляется">✓ задачи запрошены</span>' : ''}
            </div>
            ${it.summary ? `<div class="item-summary">${esc(it.summary)}</div>` : ''}
            ${lists.length ? `<div class="item-lists">${lists.join('')}</div>` : ''}
            <div class="item-meta">${fmtTime(it.mail_ts || it.ts)} · ${it.source === 'llm' ? 'разбор: модель Astra' : 'разбор: правила'}</div>
        </div>`;
    }).join('');
}

/* ---------- Ответ Астры на «создай задачи» ---------- */
function renderHandoffNote() {
    const el = $('handoffNote');
    const st = S.status || {};
    if (st.handoff_error) {
        el.hidden = false;
        el.className = 'message error';
        el.textContent = 'Создание задач не удалось: ' + st.handoff_error;
    } else if (st.handoff_answer) {
        el.hidden = false;
        el.className = 'message success';
        el.textContent = 'Астра ответила: ' + st.handoff_answer.replace(/\s+/g, ' ').slice(0, 300);
    } else {
        el.hidden = true;
    }
}

/* ---------- Форма почты ---------- */
const PROVIDERS = [
    ['yandex', 'Яндекс'],
    ['gmail', 'Gmail'],
    ['mailru', 'Mail.ru'],
    ['custom', 'Другой (свой сервер)'],
];
const MODES = [
    ['whitelist_llm', 'Белый список + умный разбор (LLM)'],
    ['whitelist', 'Только белый список (правила)'],
    ['llm', 'Все письма + умный разбор (LLM)'],
    ['rules', 'Все письма (правила, без LLM)'],
];
const DEADLINES = [
    ['calendar', 'Записи в календаре Astra'],
    ['reminders', 'Напоминания Astra'],
    ['both', 'И календарь, и напоминания'],
];

let modeDD = null;
let deadlineDD = null;
const acctExpanded = new Set();   // ids of mailboxes with the settings open

function providerName(p) {
    const o = PROVIDERS.find((x) => x[0] === (p || 'yandex'));
    return o ? o[1] : String(p || '—');
}

function acctStatusText(acct) {
    if (acct.enabled === false) return ['off', 'На паузе'];
    if (!acct.email || !acct.has_password) return ['warn', 'Нужны адрес и пароль'];
    const s = acct.status || {};
    if (!s.last_poll) return ['idle', 'Ещё не проверялся'];
    if (s.last_error) return ['err', String(s.last_error)];
    return ['ok', 'Проверен ' + fmtTime(s.last_poll) +
        (s.new_count ? ' · новых ' + s.new_count : '')];
}

function fillGlobalForm() {
    const s = S.settings;
    $('sEnabled').checked = !!s.enabled;
    $('sInterval').value = s.poll_interval_minutes ?? 5;
    $('sMax').value = s.max_messages ?? 20;
    $('sLlm').checked = !!s.llm_enabled;
    $('sHandoff').checked = !!s.auto_handoff;
    $('sSkipAds').checked = s.skip_ads_llm !== false;
    $('sVoice').checked = s.voice_announce !== false;
    if (!modeDD) modeDD = dropdown($('sModeDD'), MODES, s.mode || 'whitelist_llm', (v) => { S.settings.mode = v; });
    else modeDD.set(s.mode || 'whitelist_llm');
    const dm = s.deadline_mode || 'calendar';
    if (!deadlineDD) deadlineDD = dropdown($('sDeadlineDD'), DEADLINES, dm, (v) => { S.settings.deadline_mode = v; });
    else deadlineDD.set(dm);
}

function renderAccounts() {
    const box = $('acctList');
    box.innerHTML = '';
    const list = S.settings.accounts || [];
    if (!list.length) {
        box.innerHTML = `<div class="placeholder">Нет ящиков. Нажми «+ Добавить ящик» — все включённые проверяются одновременно.</div>`;
        return;
    }
    list.forEach((acct, idx) => box.appendChild(accountCard(acct, idx)));
}

// Чтение полей одной карточки обратно в модель (DOM всегда живёт, даже
// когда настройки свёрнуты).
function syncOne(card, acct) {
    if (!acct) return;
    acct.email = card.querySelector('.f-email').value.trim();
    const pass = card.querySelector('.f-pass').value;
    if (pass.trim()) { acct.password = pass.trim(); acct.has_password = true; }
    const h = card.querySelector('.f-host'), p = card.querySelector('.f-port');
    if (h) acct.imap_host = h.value.trim();
    if (p) acct.imap_port = parseInt(p.value, 10) || 993;
    acct.whitelist = card.querySelector('.f-wl').value.split('\n')
        .map((x) => x.trim()).filter(Boolean);
    acct.enabled = card.querySelector('.acct-row input[type=checkbox]').checked;
}

function syncAllFromDom() {
    document.querySelectorAll('#acctList .acct-card').forEach((card) => {
        syncOne(card, (S.settings.accounts || [])[+card.dataset.idx]);
    });
}

function updateAcctStatuses() {
    document.querySelectorAll('#acctList .acct-card').forEach((card) => {
        const acct = (S.settings.accounts || [])[+card.dataset.idx];
        if (!acct) return;
        const el = card.querySelector('.acct-state');
        const [cls, text] = acctStatusText(acct);
        el.className = 'acct-state ' + cls;
        el.textContent = text;
        if (card.querySelector('.acct-details').hidden) {
            card.querySelector('.acct-mail').textContent = acct.email || 'Адрес не указан';
        }
    });
}

function accountCard(acct, idx) {
    const card = document.createElement('div');
    card.className = 'card acct-card';
    card.dataset.idx = String(idx);
    const isCustom = acct.provider === 'custom';
    // Новый (незаполненный) ящик раскрываем сразу, готовые — в компактную строку.
    let open = acctExpanded.has(acct.id) || !(acct.email && acct.has_password);
    if (open) acctExpanded.add(acct.id);
    card.innerHTML = `
        <div class="acct-row">
            <label class="switch" title="Включить/выключить ящик">
                <input type="checkbox" ${acct.enabled !== false ? 'checked' : ''}>
                <span class="slider"></span>
            </label>
            <span class="acct-prov">${esc(providerName(acct.provider))}</span>
            <span class="acct-mail">${esc(acct.email || 'Адрес не указан')}</span>
            <span class="acct-state"></span>
            <button class="icon-btn acct-edit" title="Настройки ящика">⚙</button>
        </div>
        <div class="acct-details"${open ? '' : ' hidden'}>
            <div class="form-group"><label>Провайдер</label><div class="dd-slot"></div></div>
            <div class="form-group"><label>Адрес почты</label>
                <input type="text" class="f-email" value="${esc(acct.email)}" placeholder="ivan@ya.ru" autocomplete="off"></div>
            <div class="form-group"><label>Пароль приложения</label>
                <input type="password" class="f-pass" value="" placeholder="${acct.has_password ? '— сохранён —' : 'пароль приложения'}" autocomplete="new-password"></div>
            <div class="custom-fields" style="${isCustom ? '' : 'display:none'}">
                <div class="grid2">
                    <div class="form-group"><label>IMAP-сервер</label>
                        <input type="text" class="f-host" value="${esc(acct.imap_host)}" placeholder="imap.example.com"></div>
                    <div class="form-group"><label>Порт</label>
                        <input type="number" class="f-port" value="${esc(acct.imap_port || 993)}"></div>
                </div>
            </div>
            <div class="form-group"><label>Белый список (подстроки, по одному в строке; пусто = все)</label>
                <textarea class="f-wl" placeholder="bank.ru&#10;ivanov@company.com">${esc((acct.whitelist || []).join('\n'))}</textarea></div>
            <div class="acct-foot">
                <span class="message"></span>
                <button class="btn f-test">Проверить</button>
                <button class="btn f-del">Удалить</button>
            </div>
        </div>`;

    const details = card.querySelector('.acct-details');
    const stateEl = card.querySelector('.acct-state');
    const [cls, text] = acctStatusText(acct);
    stateEl.className = 'acct-state ' + cls;
    stateEl.textContent = text;

    card.querySelector('.acct-row input[type=checkbox]').onchange = (e) => {
        acct.enabled = e.target.checked;
        const [c2, t2] = acctStatusText(acct);
        stateEl.className = 'acct-state ' + c2;
        stateEl.textContent = t2;
    };
    card.querySelector('.acct-edit').onclick = () => {
        if (details.hidden) {
            details.hidden = false;
            acctExpanded.add(acct.id);
        } else {
            syncOne(card, acct);
            details.hidden = true;
            acctExpanded.delete(acct.id);
        }
    };
    card.querySelector('.f-email').addEventListener('input', (e) => {
        card.querySelector('.acct-mail').textContent = e.target.value.trim() || 'Адрес не указан';
    });
    dropdown(card.querySelector('.dd-slot'), PROVIDERS, acct.provider || 'yandex', (v) => {
        syncOne(card, acct);
        acct.provider = v;
        renderAccounts();
    });
    card.querySelector('.f-test').onclick = async (e) => {
        syncOne(card, acct);
        const msg = card.querySelector('.message');
        msg.className = 'message';
        msg.textContent = 'Подключаюсь…';
        e.target.disabled = true;
        try {
            const r = await call('aa_test_account', { account: {
                provider: acct.provider, email: acct.email, password: acct.password || '',
                imap_host: acct.imap_host, imap_port: acct.imap_port, id: acct.id } });
            if (r && r.success) { msg.className = 'message success'; msg.textContent = r.message || 'Ок'; }
            else { msg.className = 'message error'; msg.textContent = (r && r.error) || 'Ошибка'; }
        } catch (err) {
            msg.className = 'message error';
            msg.textContent = String(err);
        } finally { e.target.disabled = false; }
    };
    card.querySelector('.f-del').onclick = () => {
        syncAllFromDom();
        S.settings.accounts.splice(idx, 1);
        acctExpanded.delete(acct.id);
        renderAccounts();
    };
    return card;
}

function collectGlobal() {
    S.settings.enabled = $('sEnabled').checked;
    S.settings.poll_interval_minutes = parseInt($('sInterval').value, 10) || 5;
    S.settings.max_messages = parseInt($('sMax').value, 10) || 20;
    S.settings.llm_enabled = $('sLlm').checked;
    S.settings.auto_handoff = $('sHandoff').checked;
    S.settings.skip_ads_llm = $('sSkipAds').checked;
    S.settings.voice_announce = $('sVoice').checked;
    S.settings.deadline_mode = (S.settings.deadline_mode || 'calendar');
}

function flash(text, ok) {
    const m = $('globalMsg');
    m.className = 'message ' + (ok ? 'success' : 'error');
    m.textContent = text;
    setTimeout(() => { if (m.textContent === text) m.textContent = ''; }, 6000);
}

$('addAcctBtn').onclick = () => {
    collectGlobal();
    syncAllFromDom();   // не потерять ввод в остальных карточках при перерисовке
    S.settings.accounts = S.settings.accounts || [];
    S.settings.accounts.push({
        id: 'acct' + Date.now(), provider: 'yandex', email: '', password: '',
        imap_host: '', imap_port: 993, enabled: true, whitelist: [], has_password: false,
    });
    renderAccounts();
};

$('saveBtn').onclick = async (e) => {
    collectGlobal();
    syncAllFromDom();   // перечитать поля всех ящиков из DOM
    e.target.disabled = true;
    try {
        const r = await call('aa_save_settings', { settings: S.settings });
        if (r && r.success) flash('Сохранено ✓', true);
        else flash((r && r.error) || 'Не удалось сохранить', false);
        await refresh();
        renderAccounts();   // строки должны показать сохранённые адреса и статусы
    } catch (err) {
        flash(String(err), false);
    } finally { e.target.disabled = false; }
};

/* ---------- Кнопки действий ---------- */
$('refreshBtn').onclick = async () => {
    const btn = $('refreshBtn');
    btn.classList.add('spinning');
    btn.disabled = true;
    try {
        const r = await call('aa_run_digest');
        if (r && r.report) flash(r.report, true);
        await refresh();
    } catch (err) { flash(String(err), false); }
    finally { btn.classList.remove('spinning'); btn.disabled = false; }
};

$('handoffBtn').onclick = async (e) => {
    e.target.disabled = true;
    const old = e.target.textContent;
    e.target.textContent = 'Астра думает…';
    try {
        const r = await call('aa_handoff');
        if (r && r.error) flash(r.error, false);
        else if (r && r.answer) flash('Передано Астра: ' + r.answer.slice(0, 160), true);
        else flash('Передано Астра ✓', true);
    } catch (err) { flash(String(err), false); }
    finally { e.target.disabled = false; e.target.textContent = old; }
};

$('rewindBtn').onclick = async (e) => {
    const btn = e.target;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Разбираю…';
    try {
        const r = await call('aa_rewind_baseline', { back: 20 });
        if (r && r.error) flash(r.error, false);
        else if (r && r.report) flash(r.report, true);
        await refresh();
    } catch (err) { flash(String(err), false); }
    finally { btn.disabled = false; btn.textContent = old; }
};

$('clearDigestBtn').onclick = async () => { try { await call('aa_clear_digest'); await refresh(); } catch (e) {} };

/* ---------- Загрузка состояния ---------- */
async function refresh() {
    const r = await call('aa_get_state');
    const mailActive = loadedOnce &&
        document.querySelector('.tab-btn.active') &&
        document.querySelector('.tab-btn.active').dataset.tab === 'mail';
    if (mailActive) {
        // На вкладке «Почта» форму не перезаписываем — только подтягиваем
        // статусы ящиков, чтобы не затереть несохранённый ввод.
        const srv = {};
        (r.settings.accounts || []).forEach((a) => { srv[a.id] = a.status || {}; });
        (S.settings.accounts || []).forEach((a) => { a.status = srv[a.id] || {}; });
    } else {
        S.settings = r.settings;
    }
    S.digest = r.digest || [];
    S.status = r.status || {};
    statusDeadline = Date.now() / 1000 + (S.status.next_in_seconds || 0);
    if (!loadedOnce) {
        loadedOnce = true;
        fillGlobalForm();
        renderAccounts();
    } else {
        updateAcctStatuses();
    }
    renderDigest();
    renderHandoffNote();
    renderStatus();
}

/* Пуш из бэкенда (push_to_ui) — обновляемся без перезагрузки вкладки. */
function onPush(a, b) {
    const name = typeof a === 'string' ? a : (a && a.name);
    if (name === 'assistant') refresh();
}
try { if (window.astra && astra.onBackendMessage) astra.onBackendMessage(onPush); } catch (e) {}
try { if (window.astra && astra.on) astra.on('assistant', () => refresh()); } catch (e) {}

refresh().catch((e) => {
    $('statusPill').className = 'status err';
    $('statusPill').textContent = 'Бэкенд недоступен';
    $('digestList').innerHTML = `<div class="placeholder error">${esc(String(e))}</div>`;
});
setInterval(() => { refresh().catch(() => {}); }, 30000);
