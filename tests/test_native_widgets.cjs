const test = require('node:test');
const assert = require('node:assert/strict');
const { execute } = require('../ui/native-widgets.js');
const id = '15d8c36a-8854-4f27-9294-d3a71d9fd731';
const job = { id, kind: 'tasks', record: { id, text: 'Оплатить счёт', done: false, list_id: null } };

function host(initial = [], options = {}) {
    let data = structuredClone(initial);
    const calls = [];
    return { calls, get data() { return data; }, async invoke(command, args) {
        calls.push({ command, args });
        if (command === 'get_widget_data') return JSON.stringify(data);
        assert.equal(command, 'save_widget_data');
        assert.equal(args.widgetType, 'tasks');
        const before = JSON.parse(args.baseData);
        assert.deepEqual(before, initial);
        if (options.failSave) return;
        const proposed = JSON.parse(args.data);
        // Simulate Astra's three-way merge with a concurrent user edit/addition.
        data = options.concurrent ? [...options.concurrent, ...proposed.filter(x => !before.some(y => y.id === x.id))] : proposed;
    }};
}

test('writes native widget record and verifies exact saved content', async () => {
    const native = host([{ id: 'existing', text: 'User task', done: true }]);
    assert.equal((await execute(native, job)).existing, false);
    assert.deepEqual(native.data[0], { id: 'existing', text: 'User task', done: true });
    assert.deepEqual(native.data[1], job.record);
    assert.deepEqual(native.calls.map(c => c.command), ['get_widget_data', 'save_widget_data', 'get_widget_data']);
});

test('lost backend acknowledgement does not duplicate a native entry', async () => {
    const native = host([job.record]);
    assert.equal((await execute(native, job)).existing, true);
    assert.equal(native.calls.length, 1);
});

test('user edited existing record is not overwritten', async () => {
    const native = host([{ ...job.record, text: 'Edited by user' }]);
    await assert.rejects(execute(native, job), /изменена/);
    assert.equal(native.calls.length, 1);
});

test('baseData preserves concurrent user edits and new entries', async () => {
    const concurrent = [{ id: 'old', text: 'Edited concurrently', done: true }, { id: 'new-user', text: 'Added by user' }];
    const native = host([{ id: 'old', text: 'Before', done: false }], { concurrent });
    await execute(native, job);
    assert.deepEqual(native.data, [...concurrent, job.record]);
});

test('a successful save response alone never counts as success', async () => {
    await assert.rejects(execute(host([], { failSave: true }), job), /не подтвердила/);
});

test('model data cannot select other native commands or fields', async () => {
    const native = host();
    await assert.rejects(execute(native, { ...job, kind: 'execute_command' }), /Неизвестный/);
    await assert.rejects(execute(native, { ...job, record: { ...job.record, command: 'music' } }), /Неожиданные/);
    assert.equal(native.calls.length, 0);
});

test('malformed native storage is not overwritten', async () => {
    const native = { async invoke(command) { assert.equal(command, 'get_widget_data'); return '{}'; } };
    await assert.rejects(execute(native, job), /неизвестный формат/);
});
