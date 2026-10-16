// 使用 Operit 官方示例相同的 App 私有 SharedPreferences；不写入插件包或聊天。
const PREFS = 'org_thalemora_recall';
function prefs() {
    return Java.getApplicationContext().getSharedPreferences(PREFS, 0);
}
function load() {
    const raw = String(prefs().getString('settings', '') || '');
    return raw ? JSON.parse(raw) : { enabled: false, serviceUrl: '', recallToken: '' };
}
function save(config) {
    const serviceUrl = config.serviceUrl.trim().replace(/\/+$/, '');
    const recallToken = config.recallToken.trim();
    if (serviceUrl && !/^https?:\/\/[^\s/]+(?:\/[^\s]*)?$/.test(serviceUrl)) {
        throw new Error('服务地址必须是完整的 http/https 地址。');
    }
    if (config.enabled && (!serviceUrl || !recallToken)) {
        throw new Error('启用前请填写服务地址与专用召回凭据。');
    }
    const next = { enabled: config.enabled, serviceUrl, recallToken };
    if (!prefs().edit().putString('settings', JSON.stringify(next)).commit()) {
        throw new Error('设置未保存，请重试。');
    }
    return next;
}
exports.load = load;
exports.save = save;
