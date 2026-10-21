// Run with: node --test tests/test_ui.cjs. No browser or Astra is required.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function ui() {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            classList: { add() {}, remove() {} }, removeAttribute() {},
            innerHTML: '', textContent: '', disabled: false,
        });
        return elements.get(id);
    };
    const context = vm.createContext({
        document: {
            getElementById: element, querySelectorAll: () => [],
            querySelector: () => ({ dataset: { tab: 'digest' } }),
            addEventListener() {},
        },
        window: {}, setInterval() {}, setTimeout() {},
    });
    const script = fs.readFileSync(path.join(__dirname, '../ui/script.js'), 'utf8');
    vm.runInContext(script.slice(0, script.indexOf("refresh().catch((e) => {")), context);
    vm.runInContext(`
        loadedOnce = true;
        renderDigest = renderHandoffNote = renderStatus = updateAcctStatuses = () => {};
        fillGlobalForm = renderAccounts = collectGlobal = syncAllFromDom = () => {};
        S.settings = {mode: 'rules', accounts: [{id:'new', email:'unsaved@example.test'}]};
    `, context);
    return { context, element, run: code => vm.runInContext(code, context) };
}

test('refresh on Digest keeps the new mailbox and global settings draft', async () => {
    const h = ui();
    await h.run(`
        call = async () => ({settings: {mode: 'llm', accounts: []}, digest: [], status: {}});
        refresh();
    `);
    assert.equal(h.run('S.settings.accounts[0].email'), 'unsaved@example.test');
    assert.equal(h.run('S.settings.mode'), 'rules');
});

test('refresh updates status by id without replacing credentials or draft', async () => {
    const h = ui();
    await h.run(`
        S.settings.accounts[0].password = 'draft-only';
        call = async () => ({settings: {accounts: [
            {id:'other', status:{new_count:99}}, {id:'new', status:{new_count:3}}
        ]}, digest: [], status: {}});
        refresh();
    `);
    assert.equal(h.run('S.settings.accounts[0].status.new_count'), 3);
    assert.equal(h.run('S.settings.accounts[0].password'), 'draft-only');
});

test('successful save adopts server ids and clears the saved password from the draft', async () => {
    const h = ui();
    h.run(`
        S.settings.accounts[0].password = 'draft-only';
        call = async method => method === 'aa_save_settings'
            ? {success:true, settings:{accounts:[{id:'saved', email:'unsaved@example.test', has_password:true}]}}
            : {settings:{accounts:[]}, digest:[], status:{}};
    `);
    await h.element('saveBtn').onclick({ target: h.element('saveBtn') });
    assert.equal(h.run('S.settings.accounts[0].id'), 'saved');
    assert.equal(h.run('S.settings.accounts[0].password'), undefined);
    assert.equal(h.element('saveBtn').disabled, false);
});

test('edits made while save is pending survive and adopt changed account ids', async () => {
    const h = ui();
    h.run(`
        call = async method => {
            if (method === 'aa_save_settings') {
                S.settings.accounts[0].email = 'edited-during-save@example.test';
                return {success:true, settings:{accounts:[{id:'saved'}]}, account_ids:{new:'saved'}};
            }
            return {settings:{accounts:[]}, digest:[], status:{}};
        };
    `);
    await h.element('saveBtn').onclick({ target: h.element('saveBtn') });
    assert.equal(h.run('S.settings.accounts[0].id'), 'saved');
    assert.equal(h.run('S.settings.accounts[0].email'), 'edited-during-save@example.test');
    assert.match(h.element('digestMsg').textContent, /ещё не сохранены/);
});

test('failed save preserves the draft', async () => {
    const h = ui();
    h.run(`call = async method => method === 'aa_save_settings'
        ? {error:'Cannot save'} : {settings:{accounts:[]}, digest:[], status:{}};`);
    await h.element('saveBtn').onclick({ target: h.element('saveBtn') });
    assert.equal(h.run('S.settings.accounts[0].id'), 'new');
    assert.equal(h.element('digestMsg').textContent, 'Cannot save');
});

test('handoff errors are visible on Digest instead of the hidden Mail tab', () => {
    const h = ui();
    h.run("flash('Нет подтверждённых действий', false)");
    assert.equal(h.element('digestMsg').hidden, false);
    assert.equal(h.element('digestMsg').textContent, 'Нет подтверждённых действий');
    assert.equal(h.element('globalMsg').textContent, '');
});
