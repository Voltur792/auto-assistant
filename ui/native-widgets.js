/* Only the DOM controller uses this module, under Astra's dom_access grant. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AutoAssistantWidgets = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
    'use strict';
    const TYPES = new Set(['tasks', 'reminders', 'calendar']);
    function decode(value) {
        const data = typeof value === 'string' ? JSON.parse(value)
            : value && typeof value.data === 'string' ? JSON.parse(value.data) : value;
        if (!Array.isArray(data) || data.some(x => !x || typeof x !== 'object' || Array.isArray(x)))
            throw new Error('Astra вернула неизвестный формат списка виджета.');
        return data;
    }
    function validate(job) {
        if (!job || !TYPES.has(job.kind) || !/^[0-9a-f-]{36}$/.test(job.id))
            throw new Error('Неизвестный тип записи.');
        const record = job.record;
        if (!record || record.id !== job.id || typeof record.text !== 'string'
            || !record.text.trim() || record.text.length > 600)
            throw new Error('Неверная запись виджета.');
        const fields = job.kind === 'tasks' ? ['id', 'text', 'done', 'list_id']
            : job.kind === 'calendar' ? ['id', 'text', 'date', 'time'] : ['id', 'text', 'time'];
        if (Object.keys(record).some(key => !fields.includes(key)))
            throw new Error('Неожиданные поля записи.');
        if (job.kind === 'tasks' && (record.done !== false || record.list_id !== null))
            throw new Error('Неверные свойства задачи.');
        if (job.kind === 'calendar' && !/^\d{4}-\d{2}-\d{2}$/.test(record.date))
            throw new Error('Неверная дата события.');
        if (job.kind !== 'tasks' && record.time !== '' && !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(record.time))
            throw new Error('Неверное время.');
        return record;
    }
    function matches(expected, actual) {
        return actual && Object.keys(expected).every(k => expected[k] === actual[k]);
    }
    async function execute(native, job) {
        const expected = validate(job);
        const before = decode(await native.invoke('get_widget_data', { widgetType: job.kind }));
        const found = before.find(x => x.id === expected.id);
        if (found) {
            if (!matches(expected, found)) throw new Error('Существующая запись изменена; перезапись отменена.');
            return { id: expected.id, existing: true };
        }
        // baseData enables Astra's three-way merge, preserving concurrent edits.
        await native.invoke('save_widget_data', {
            widgetType: job.kind, data: JSON.stringify([...before, expected]), baseData: JSON.stringify(before),
        });
        const after = decode(await native.invoke('get_widget_data', { widgetType: job.kind }));
        if (!matches(expected, after.find(x => x.id === expected.id)))
            throw new Error('Astra не подтвердила сохранение записи.');
        return { id: expected.id, existing: false };
    }
    return { execute, decode, validate };
});
