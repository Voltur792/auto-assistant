/* page.custom entry: mount only inside the container supplied by Astra. */
(function () {
    'use strict';
    const id = 'auto-assistant';
    const root = document.currentScript?.parentElement
        || document.querySelector('[data-contribution-id="auto-assistant"]');
    const bridge = window.__astraPluginBridge?.[id];
    if (!root || !bridge) return;
    root.querySelectorAll('[data-auto-assistant-page]').forEach(element => element.remove());
    const channel = crypto.randomUUID();
    const host = document.createElement('div');
    host.dataset.autoAssistantPage = '';
    host.style.cssText = 'width:100%;height:100%;min-height:0;display:flex;flex:1';
    const iframe = document.createElement('iframe');
    iframe.title = 'Авто-Помощник';
    iframe.setAttribute('sandbox', 'allow-scripts');
    iframe.style.cssText = 'display:block;border:0;width:100%;height:100%;flex:1;min-height:0';
    iframe.src = 'http://astra-plugin.localhost/auto-assistant/assistant.html#native-channel=' + channel;
    host.append(iframe);
    root.append(host);
    let stopped = false;
    const post = payload => iframe.contentWindow?.postMessage({ channel, ...payload }, '*');
    const theme = () => post({ type: 'theme', theme: bridge.getTheme(), variables: bridge.getCssVariables() });
    const updated = () => post({ type: 'updated' });
    const methods = new Set(['aa_get_state', 'aa_get_icon', 'aa_save_settings', 'aa_test_account',
        'aa_run_digest', 'aa_handoff', 'aa_rewind_baseline', 'aa_clear_digest', 'aa_retriage']);
    async function onMessage(event) {
        if (stopped || event.source !== iframe.contentWindow || event.data?.channel !== channel) return;
        const message = event.data;
        if (message.type === 'ready') theme();
        else if (message.type === 'call' && methods.has(message.method)) {
            try { post({ type: 'reply', id: message.id, result: await bridge.callBackend(message.method, message.params) }); }
            catch (_) { post({ type: 'reply', id: message.id, error: 'Не удалось выполнить запрос к помощнику.' }); }
        }
    }
    window.addEventListener('message', onMessage);
    window.addEventListener('auto-assistant-updated', updated);
    const untheme = bridge.onThemeChange?.(theme);
    function stop() {
        if (stopped) return;
        stopped = true;
        observer.disconnect();
        window.removeEventListener('message', onMessage);
        window.removeEventListener('auto-assistant-updated', updated);
        untheme?.();
        host.remove();
    }
    const observer = new MutationObserver(() => { if (!root.isConnected || !host.isConnected) stop(); });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    if (window.__autoAssistantController)
        window.__astraPluginCleanup[id] = window.__autoAssistantController.cleanup;
})();
