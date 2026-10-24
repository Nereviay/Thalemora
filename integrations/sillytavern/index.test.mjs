import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('./index.js', import.meta.url), 'utf8');
const manifest = JSON.parse(await readFile(new URL('./manifest.json', import.meta.url), 'utf8'));

function host(fetch, immediateTimeout = false) {
    const prompts = {};
    const calls = [];
    const warnings = [];
    const settings = { thalemora_recall: { enabled: true, serviceUrl: 'https://recall.invalid/' } };
    const session = new Map([['thalemora_recall_token', 'synthetic-recall-token']]);
    let timerCleared = false;
    const sandbox = vm.createContext({
        AbortController,
        sessionStorage: { getItem: key => session.get(key) || null },
        fetch: async (...args) => { calls.push(args); return fetch(...args); },
        console: { warn: message => warnings.push(message) },
        setTimeout: (callback, delay) => {
            assert.equal(delay, 2000);
            if (immediateTimeout) queueMicrotask(callback);
            return 1;
        },
        clearTimeout: () => { timerCleared = true; },
        SillyTavern: { getContext: () => ({
            extensionSettings: settings,
            setExtensionPrompt(key, value, position, depth, scan = false, role = 0, filter = null) {
                prompts[key] = { value: String(value), position, depth, scan, role, filter };
            },
        }) },
    });
    vm.runInContext(source, sandbox);
    return {
        calls, prompts, warnings, settings, session,
        get timerCleared() { return timerCleared; },
        async generate(chat) {
            const before = structuredClone(chat);
            // 模拟官方 await interceptor → 组装模型消息 → IN_CHAT 深度 0 注入。
            await sandbox[manifest.generate_interceptor](chat, 4096,
                () => assert.fail('召回不得中止生成'), 'normal');
            assert.deepEqual(chat, before, '不得修改可持久化聊天数组或消息');
            const outgoing = chat.map(message => ({
                role: message.is_user ? 'user' : 'assistant', content: message.mes,
            }));
            for (const prompt of Object.values(prompts)) {
                if (prompt.value && prompt.position === 1 && prompt.depth === 0 && prompt.role === 0) {
                    outgoing.push({ role: 'system', content: prompt.value });
                }
            }
            return outgoing;
        },
    };
}

const user = mes => ({ is_user: true, mes });
const ok = rendered => ({ ok: true, json: async () => ({ rendered }) });

test('Embedding 降级 warning 兼容：继续插入词法资料，空结果清槽，不重试', async () => {
    for (const rendered of ['[记忆 1] 合成词法资料', '']) {
        const adapter = host(async () => ({ ok: true, json: async () => ({
            rendered, memories: [], archive: [], empty: !rendered,
            errors: ['embedding_unavailable', 'rerank_unavailable'],
        }) }));
        const outgoing = await adapter.generate([user('茶')]);
        assert.equal(adapter.calls.length, 1);
        assert.equal(adapter.prompts['thalemora-recall'].value, rendered);
        assert.equal(outgoing.length, rendered ? 2 : 1);
        assert.equal(adapter.warnings.length, 0);
    }
});

test('相关召回：最新用户消息、专用 token、异步结果进入模拟模型 Context', async () => {
    const adapter = host(async () => { await Promise.resolve(); return ok('[记忆 1] 喜欢乌龙茶'); });
    const outgoing = await adapter.generate([user('旧问题'), user('喜欢什么茶？'), { mes: '助手旧回答' }]);
    assert.deepEqual(outgoing.at(-1), { role: 'system', content: '[记忆 1] 喜欢乌龙茶' });
    const [url, request] = adapter.calls[0];
    assert.equal(url, 'https://recall.invalid/api/recall');
    assert.equal(request.method, 'POST');
    assert.equal(request.headers.Authorization, 'Bearer synthetic-recall-token');
    assert.equal(request.credentials, 'omit');
    assert.equal(JSON.parse(request.body).message, '喜欢什么茶？');
    assert.deepEqual(adapter.prompts['thalemora-recall'], {
        value: '[记忆 1] 喜欢乌龙茶', position: 1, depth: 0, scan: false, role: 0, filter: null,
    });
    assert.equal(adapter.timerCleared, true);
});

test('无关空结果：清掉上一轮注入', async () => {
    let round = 0;
    const adapter = host(async () => ok(round++ === 0 ? '喜欢乌龙茶' : ''));
    await adapter.generate([user('茶')]);
    const outgoing = await adapter.generate([user('今天天气')]);
    assert.equal(adapter.prompts['thalemora-recall'].value, '');
    assert.deepEqual(outgoing, [{ role: 'user', content: '今天天气' }]);
});

test('失败与短超时：移除旧记忆，继续发送用户消息', async () => {
    for (const failure of ['http', 'network', 'json', 'shape', 'timeout']) {
        let round = 0;
        const adapter = host(async (_url, request) => {
            if (round++ === 0) return ok('上一轮记忆');
            if (failure === 'http') return { ok: false, status: 401 };
            if (failure === 'network') throw new TypeError('synthetic network failure');
            if (failure === 'json') return { ok: true, json: async () => { throw new SyntaxError(); } };
            if (failure === 'shape') return { ok: true, json: async () => ({}) };
            return new Promise((_resolve, reject) => {
                request.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
            });
        }, failure === 'timeout');
        await adapter.generate([user('茶')]);
        const outgoing = await adapter.generate([user('继续')]);
        assert.deepEqual(outgoing, [{ role: 'user', content: '继续' }], failure);
        assert.equal(adapter.warnings.length, 1, failure);
        assert.equal(adapter.timerCleared, true, failure);
    }
});

test('去重：提交实际可见正文，固定槽位替换，重复生成不累积', async () => {
    const adapter = host(async (_url, request) => {
        const body = JSON.parse(request.body);
        return ok(body.visible_texts.includes('喜欢乌龙茶') ? '' : '喜欢乌龙茶');
    });
    const chat = [user('茶')];
    await adapter.generate(chat);
    assert.equal((await adapter.generate(chat)).filter(message => message.content === '喜欢乌龙茶').length, 1);
    assert.equal(Object.keys(adapter.prompts).length, 1);
    const outgoing = await adapter.generate([{ mes: '喜欢乌龙茶' }, user('茶')]);
    assert.equal(outgoing.filter(message => message.content === '喜欢乌龙茶').length, 1);
    assert.equal(adapter.prompts['thalemora-recall'].value, '');
    assert.deepEqual(JSON.parse(adapter.calls.at(-1)[1].body).visible_texts, ['喜欢乌龙茶', '茶']);
});

test('未配置、无用户消息和超过接口长度时跳过，不截断问题', async () => {
    const adapter = host(() => assert.fail('不应请求召回'));
    await adapter.generate([{ mes: '助手' }]);
    await adapter.generate([user('字'.repeat(201))]);
    // 新页面默认未配置，仍可正常聊天。
    const sandbox = vm.createContext({
        SillyTavern: { getContext: () => ({ extensionSettings: {}, setExtensionPrompt() {} }) },
    });
    vm.runInContext(source, sandbox);
    await sandbox[manifest.generate_interceptor]([user('茶')], 4096, () => assert.fail(), 'normal');
    assert.equal(adapter.calls.length, 0);
});

test('保存开关和标签页凭据决定召回，不使用管理设置存储 token', async () => {
    const adapter = host(async () => ok('乌龙茶'));
    adapter.settings.thalemora_recall.enabled = false;
    await adapter.generate([user('茶')]);
    assert.equal(adapter.calls.length, 0);
    adapter.settings.thalemora_recall.enabled = true;
    adapter.session.clear();
    await adapter.generate([user('茶')]);
    assert.equal(adapter.calls.length, 0);
    adapter.session.set('thalemora_recall_token', 'new-synthetic-token');
    await adapter.generate([user('茶')]);
    assert.equal(adapter.calls.at(-1)[1].headers.Authorization, 'Bearer new-synthetic-token');
    assert.equal('recallToken' in adapter.settings.thalemora_recall, false);
});
