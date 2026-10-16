// 配置使用酒馆设置，召回凭据仅留在当前标签页。
const THALEMORA_SETTINGS_KEY = 'thalemora_recall';
const THALEMORA_TOKEN_KEY = 'thalemora_recall_token';
function thalemoraSettings() {
    const settings = SillyTavern.getContext().extensionSettings;
    return settings[THALEMORA_SETTINGS_KEY] || { enabled: false, serviceUrl: '' };
}
function thalemoraStatus(message) {
    if (typeof document === 'undefined') return;
    const status = document.getElementById('thalemora-status');
    if (status) status.textContent = message;
}
function thalemoraMountSettings() {
    const container = document.getElementById('extensions_settings2');
    const panel = document.createElement('details');
    panel.id = 'thalemora-settings';
    panel.innerHTML = `
        <summary>洄海 · 自动召回</summary>
        <div class="flex-container flexFlowColumn">
            <label><input id="thalemora-enabled" type="checkbox"> 启用聊天前召回</label>
            <label for="thalemora-url">洄海服务地址</label>
            <input id="thalemora-url" class="text_pole" type="url" placeholder="https://你的洄海地址" autocomplete="off">
            <label for="thalemora-token">专用召回凭据（不是管理密钥）</label>
            <input id="thalemora-token" class="text_pole" type="password" autocomplete="off">
            <small>凭据只保存在当前标签页；刷新保留，关闭标签页后需重填。不会写入聊天或酒馆设置。</small>
            <button id="thalemora-save" class="menu_button" type="button">保存设置</button>
            <div id="thalemora-status" role="status" aria-live="polite"></div>
        </div>`;
    container.append(panel);
    const enabled = panel.querySelector('#thalemora-enabled');
    const url = panel.querySelector('#thalemora-url');
    const token = panel.querySelector('#thalemora-token');
    enabled.checked = thalemoraSettings().enabled;
    url.value = thalemoraSettings().serviceUrl;
    token.value = sessionStorage.getItem(THALEMORA_TOKEN_KEY) || '';
    thalemoraStatus(enabled.checked && !token.value ? '请填写本标签页的召回凭据。' : '尚未进行本轮召回。');
    panel.querySelector('#thalemora-save').addEventListener('click', () => {
        try {
            const serviceUrl = url.value.trim().replace(/\/+$/, '');
            const recallToken = token.value.trim();
            if (serviceUrl && !['http:', 'https:'].includes(new URL(serviceUrl).protocol)) {
                throw new Error('服务地址必须使用 http 或 https。');
            }
            if (enabled.checked && (!serviceUrl || !recallToken)) throw new Error('启用前请填服务地址与专用召回凭据。');
            const context = SillyTavern.getContext();
            sessionStorage.setItem(THALEMORA_TOKEN_KEY, recallToken);
            context.extensionSettings[THALEMORA_SETTINGS_KEY] = { enabled: enabled.checked, serviceUrl };
            context.saveSettingsDebounced();
            if (!enabled.checked) context.setExtensionPrompt('thalemora-recall', '', 1, 0, false, 0);
            thalemoraStatus(enabled.checked ? '设置已应用；下一条消息将自动召回。' : '自动召回已关闭。');
        } catch {
            thalemoraStatus('保存失败：请检查 http/https 地址及召回凭据，确认浏览器允许会话存储。');
        }
    });
}
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', thalemoraMountSettings, { once: true });
    else thalemoraMountSettings();
}

globalThis.thalemoraRecallInterceptor = async function (chat, contextSize, abort, type) {
    const context = SillyTavern.getContext();
    // 上游常量：IN_CHAT=1，SYSTEM=0；第五参 scan，第六参 role。
    const inject = value => context.setExtensionPrompt('thalemora-recall', value, 1, 0, false, 0);
    inject('');

    const { enabled, serviceUrl } = thalemoraSettings();
    if (!enabled || !serviceUrl) return;
    let recallToken;
    try {
        recallToken = sessionStorage.getItem(THALEMORA_TOKEN_KEY);
    } catch {
        thalemoraStatus('无法读取本标签页凭据，本轮继续原聊天。');
        return;
    }
    if (!recallToken) {
        thalemoraStatus('请在扩展设置中填写本标签页的召回凭据。');
        return;
    }
    const latest = chat.findLast(message => message.is_user && !message.is_system);
    if (!latest || typeof latest.mes !== 'string' || !latest.mes.trim()) return;
    // P8-3 限制 200 个 Unicode 字符；不截断或改写用户的问题。
    if (Array.from(latest.mes).length > 200) {
        console.warn('[Thalemora] 本轮消息超过召回接口 200 字符限制，已跳过记忆');
        return;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2000);
    try {
        const response = await fetch(`${serviceUrl.replace(/\/$/, '')}/api/recall`, {
            method: 'POST',
            headers: { Authorization: `Bearer ${recallToken}`, 'Content-Type': 'application/json' },
            credentials: 'omit',
            signal: controller.signal,
            body: JSON.stringify({
                message: latest.mes,
                // 仅报告本轮 Hook 收到的可见正文；不缓存上一轮引用或注入。
                visible_texts: chat.filter(message => typeof message.mes === 'string').map(message => message.mes),
            }),
        });
        if (!response.ok) throw new Error('recall_http_error');
        const payload = await response.json();
        if (typeof payload.rendered !== 'string') throw new Error('recall_invalid_response');
        inject(payload.rendered);
        thalemoraStatus(payload.rendered ? '本轮已加入相关记忆。' : '本轮没有新增相关记忆。');
    } catch {
        // 不记录凭据、聊天正文或私有响应；宿主继续原来的生成路径。
        console.warn('[Thalemora] 本轮召回失败或超时，已跳过记忆');
        thalemoraStatus('本轮召回失败或超时；聊天继续，请检查服务与凭据。');
    } finally {
        clearTimeout(timer);
    }
};
