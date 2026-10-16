// 复用 Operit v1.12.2 官方 message_insert 的非持久化发送前入口。
// 本探针仅验证宿主分发与实际 Context，不连接洄海。
function onPromptFinalize(input) {
    const event = input.eventPayload || {};
    const stage = String(event.stage || input.eventName || '');
    if (stage !== 'before_send_to_model') return null;
    console.log('[ThalemoraProbe] before_send_to_model');
    return { processedInput: String(event.processedInput || event.rawInput || '') +
        '\n\n[检索资料：合成测试，不是用户原话]\nTHALEMORA_OPERIT_PROBE_20261016' };
}
function registerToolPkg() {
    ToolPkg.registerPromptFinalizeHook({
        id: 'thalemora_fixed_marker',
        function: onPromptFinalize
    });
    return true;
}
exports.registerToolPkg = registerToolPkg;
exports.onPromptFinalize = onPromptFinalize;
