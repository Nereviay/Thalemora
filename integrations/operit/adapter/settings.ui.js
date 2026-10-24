const settings = require('./settings.js');
function Screen(ctx) {
    const initial = settings.load();
    const [enabled, setEnabled] = ctx.useState('enabled', initial.enabled);
    const [serviceUrl, setServiceUrl] = ctx.useState('serviceUrl', initial.serviceUrl);
    const [recallToken, setRecallToken] = ctx.useState('recallToken', initial.recallToken);
    const [status, setStatus] = ctx.useState('status', '');
    return ctx.UI.Column({ fillMaxWidth: true, padding: 16, spacing: 12 }, [
        ctx.UI.Text({ text: '洄海 · 自动召回', style: 'headlineSmall' }),
        ctx.UI.Text({ text: '发送前查找相关记忆，只加入本轮模型请求，界面保留你的原话。', style: 'bodyMedium' }),
        ctx.UI.Row({ fillMaxWidth: true, verticalAlignment: 'center' }, [
            ctx.UI.Text({ text: '启用聊天前召回' }),
            ctx.UI.Switch({ checked: enabled, onCheckedChange: setEnabled })
        ]),
        ctx.UI.TextField({ label: '洄海服务地址', placeholder: 'https://你的洄海地址', value: serviceUrl, onValueChange: setServiceUrl, singleLine: true }),
        ctx.UI.TextField({ label: '专用召回凭据（不是管理密钥）', value: recallToken, onValueChange: setRecallToken, singleLine: true, isPassword: true }),
        ctx.UI.Text({ text: '设置保存在本机 Operit 私有配置中，不随插件包或聊天导出。请连接洄海 v0.8.0 服务。', style: 'bodySmall' }),
        ctx.UI.Button({ text: '保存设置', fillMaxWidth: true, onClick: () => {
            try {
                const saved = settings.save({ enabled, serviceUrl, recallToken });
                setServiceUrl(saved.serviceUrl);
                setRecallToken(saved.recallToken);
                setStatus(saved.enabled ? '已保存；下一条消息将自动召回。' : '已保存，自动召回已关闭。');
            } catch (error) {
                setStatus(error.message);
            }
        } }),
        ctx.UI.Text({ text: status, style: 'bodyMedium' })
    ]);
}
exports.default = Screen;
