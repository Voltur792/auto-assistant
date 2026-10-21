/* The settings panel is sandboxed; only its own backend is reachable. */
(function () {
    'use strict';
    const channel = new URLSearchParams(location.hash.slice(1)).get('native-channel');
    if (!channel) return;
    let next = 0;
    const pending = new Map();
    const listeners = [];
    window.addEventListener('message', event => {
        if (event.source !== parent || event.data?.channel !== channel) return;
        const message = event.data;
        if (message.type === 'reply') {
            const entry = pending.get(message.id);
            if (!entry) return;
            pending.delete(message.id);
            clearTimeout(entry.timer);
            if (message.error) entry.reject(new Error(message.error));
            else entry.resolve(message.result);
        } else if (message.type === 'theme') {
            document.documentElement.dataset.theme = message.theme;
            for (const [key, value] of Object.entries(message.variables || {}))
                if (key.startsWith('--')) document.documentElement.style.setProperty(key, value);
        } else if (message.type === 'updated') {
            for (const callback of listeners) callback('assistant', {});
        }
    });
    window.astra = {
        callBackend(method, params) {
            return new Promise((resolve, reject) => {
                const id = ++next;
                const timer = setTimeout(() => {
                    pending.delete(id);
                    reject(new Error('Помощник не ответил вовремя.'));
                }, 180000);
                pending.set(id, { resolve, reject, timer });
                parent.postMessage({ channel, type: 'call', id, method, params }, '*');
            });
        },
        onBackendMessage(callback) { listeners.push(callback); },
    };
    parent.postMessage({ channel, type: 'ready' }, '*');
})();
