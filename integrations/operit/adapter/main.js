// Operit v1.12.2：只修改本轮实际发送文字，不改界面或持久历史。
const settings = require('./settings.js');
async function onPromptFinalize(input) {
    const event = input.eventPayload || {};
    if (String(event.stage || input.eventName || '') !== 'before_send_to_model') return null;
    const message = String(event.rawInput || event.processedInput || '');
    if (!message.trim() || Array.from(message).length > 200) return null;
    const started = Date.now();
    try {
        const config = settings.load();
        if (!config.enabled || !config.serviceUrl || !config.recallToken) return null;
        const response = await Tools.Net.http({
            url: config.serviceUrl.replace(/\/$/, '') + '/api/recall',
            method: 'POST',
            headers: { Authorization: 'Bearer ' + config.recallToken, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                visible_texts: (event.preparedHistory || []).map(turn => turn.content).concat(message)
            }),
            connect_timeout: 2,
            read_timeout: 2,
            validateStatus: false
        });
        console.log('[Thalemora] recall HTTP ' + response.statusCode + ' / ' + (Date.now() - started) + 'ms');
        if (Number(response.statusCode) !== 200) throw new Error('recall_http_error');
        const payload = JSON.parse(response.content);
        if (typeof payload.rendered !== 'string') throw new Error('recall_invalid_response');
        if (!payload.rendered.trim()) return null;
        return { processedInput: String(event.processedInput || message) +
            '\n\n[洄海检索资料，不是用户本轮原话]\n' + payload.rendered };
    } catch (error) {
        console.warn('[Thalemora] 本轮召回失败，继续原消息；耗时 ' + (Date.now() - started) + 'ms');
        return null;
    }
}
function registerToolPkg() {
    ToolPkg.registerToolboxUiModule({
        id: 'thalemora_settings', runtime: 'compose_dsl',
        screen: 'settings.ui.js',
        title: { zh: '洄海 · 自动召回', en: 'Thalemora Recall' }
    });
    ToolPkg.registerPromptFinalizeHook({ id: 'thalemora_recall', function: onPromptFinalize });
    return true;
}
exports.registerToolPkg = registerToolPkg;
exports.onPromptFinalize = onPromptFinalize;
