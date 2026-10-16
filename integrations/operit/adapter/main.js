// Operit v1.12.2：只修改本轮实际发送文字，不改界面或持久历史。
const config = require('./config.js');
async function onPromptFinalize(input) {
    const event = input.eventPayload || {};
    if (String(event.stage || input.eventName || '') !== 'before_send_to_model') return null;
    if (!config.serviceUrl || !config.recallToken) return null;
    const message = String(event.rawInput || event.processedInput || '');
    if (!message.trim() || Array.from(message).length > 200) return null;
    const started = Date.now();
    try {
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
    ToolPkg.registerPromptFinalizeHook({ id: 'thalemora_recall', function: onPromptFinalize });
    return true;
}
exports.registerToolPkg = registerToolPkg;
exports.onPromptFinalize = onPromptFinalize;
