// 仅在当前页面会话配置；不写入聊天、宿主设置或浏览器存储。
globalThis.thalemoraRecallConfig = { serviceUrl: '', recallToken: '' };

globalThis.thalemoraRecallInterceptor = async function (chat, contextSize, abort, type) {
    const context = SillyTavern.getContext();
    // 上游常量：IN_CHAT=1，SYSTEM=0；第五参 scan，第六参 role。
    const inject = value => context.setExtensionPrompt('thalemora-recall', value, 1, 0, false, 0);
    inject('');

    const { serviceUrl, recallToken } = globalThis.thalemoraRecallConfig;
    if (!serviceUrl || !recallToken) return;
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
    } catch {
        // 不记录凭据、聊天正文或私有响应；宿主继续原来的生成路径。
        console.warn('[Thalemora] 本轮召回失败或超时，已跳过记忆');
    } finally {
        clearTimeout(timer);
    }
};
