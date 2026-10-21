/* Persistent native-widget worker. The assistant UI is a separate page.custom. */
(async function () {
    'use strict';
    const id = 'auto-assistant';
    const root = document.currentScript?.parentElement;
    const bridge = window.__astraPluginBridge?.[id];
    const native = window.__TAURI_INTERNALS__ || window.electronBridge;
    if (!root || !bridge || typeof native?.invoke !== 'function') return;
    if (window.__autoAssistantController) await window.__autoAssistantController.stop();
    const base = 'http://astra-plugin.localhost/auto-assistant/';
    if (!window.AutoAssistantWidgets) {
        await new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = base + 'native-widgets.js';
            script.onload = resolve;
            script.onerror = () => reject(new Error('Не загружен модуль штатных виджетов.'));
            document.head.append(script);
        });
    }
    let stopped = false, busy = false, revision = '';
    const worker = crypto.randomUUID();
    const call = (method, params = {}) => bridge.callBackend(method, params);
    const updated = () => window.dispatchEvent(new CustomEvent('auto-assistant-updated'));
    async function tick() {
        if (stopped || busy) return;
        if (!root.isConnected) { controller.stop(); return; }
        busy = true;
        try {
            const result = await call('aa_widget_jobs', { worker });
            const nextRevision = JSON.stringify([result.revision, result.polling]);
            if (revision !== nextRevision) { revision = nextRevision; updated(); }
            for (const job of result.jobs || []) {
                if (stopped || !root.isConnected) break;
                let success = false, error = '';
                try { await window.AutoAssistantWidgets.execute(native, job); success = true; }
                catch (err) { error = String(err.message || err).slice(0, 250); }
                await call('aa_widget_result', { id: job.id, lease: job.lease, success, error });
                updated();
            }
        } catch (_) { /* A stopped/restarting backend is retried on the next tick. */ }
        finally { busy = false; }
    }
    const timer = setInterval(tick, 3000);
    const controller = {
        stop: async () => { stopped = true; clearInterval(timer); },
        // Astra's cleanup registry is keyed by plugin, not by page. Waiting
        // until the DOM commit lets a page unmount leave this worker alive.
        cleanup: () => queueMicrotask(() => { if (!root.isConnected) controller.stop(); }),
    };
    window.__autoAssistantController = controller;
    window.__astraPluginCleanup[id] = controller.cleanup;
    await tick();
})();
