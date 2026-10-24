import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const sources = Object.fromEntries(await Promise.all(['settings.js', 'settings.ui.js', 'main.js'].map(async n => [n, await readFile(new URL(n, import.meta.url), 'utf8')])));
function host(store = new Map(), failSave = false, response = { rendered: '[记忆 1] 我喜欢乌龙茶' }) {
    const modules = {}, registrations = {}, requests = [], warnings = [];
    const prefs = { getString: (key, fallback) => store.get(key) || fallback, edit: () => ({
        putString(key, value) { this.key = key; this.value = value; return this; },
        commit() { if (failSave) return false; store.set(this.key, this.value); return true; }
    }) };
    function load(name) {
        if (modules[name]) return modules[name];
        const exports = {};
        vm.runInNewContext(sources[name], {
            exports, require: name => load(name.replace('./', '')),
            Java: { getApplicationContext: () => ({ getSharedPreferences: () => prefs }) },
            Tools: { Net: { http: async options => { requests.push(options); return { statusCode: 200, content: JSON.stringify(response) }; } } },
            ToolPkg: { registerToolboxUiModule: def => { registrations.ui = def; }, registerPromptFinalizeHook: def => { registrations.hook = def; } },
            console: { log() {}, warn: line => warnings.push(line) }
        });
        modules[name] = exports;
        return exports;
    }
    return { load, registrations, requests, store, warnings };
}
const config = { enabled: true, serviceUrl: ' https://recall.invalid/ ', recallToken: ' synthetic-token ' };
const input = { eventPayload: { stage: 'before_send_to_model', rawInput: '茶', processedInput: '茶', preparedHistory: [] } };

test('配置持久化后新宿主读取，开关控制实际召回和当轮注入', async () => {
    const first = host();
    first.load('settings.js').save(config);
    const restarted = host(first.store), main = restarted.load('main.js');
    main.registerToolPkg();
    assert.equal(restarted.registrations.ui.runtime, 'compose_dsl');
    assert.equal(restarted.registrations.ui.screen, 'settings.ui.js');
    const result = await restarted.registrations.hook.function(input);
    assert.match(result.processedInput, /乌龙茶/);
    assert.equal(restarted.requests[0].url, 'https://recall.invalid/api/recall');
    assert.equal(restarted.requests[0].headers.Authorization, 'Bearer synthetic-token');
    assert.equal(input.eventPayload.processedInput, '茶');
    restarted.load('settings.js').save({ ...config, enabled: false });
    assert.equal(await main.onPromptFinalize(input), null);
    assert.equal(restarted.requests.length, 1);
});

test('Embedding 降级 warning 兼容：词法资料照常注入，空结果跳过，无重试', async () => {
    for (const rendered of ['[记忆 1] 合成词法资料', '']) {
        const adapter = host(new Map(), false, { rendered, memories: [], archive: [], empty: !rendered,
            errors: ['embedding_unavailable', 'rerank_unavailable'] });
        adapter.load('settings.js').save(config);
        const result = await adapter.load('main.js').onPromptFinalize(input);
        if (rendered) assert.match(result.processedInput, /合成词法资料/);
        else assert.equal(result, null);
        assert.equal(adapter.requests.length, 1);
        assert.equal(adapter.warnings.length, 0);
    }
});

test('默认关闭；非发送前阶段不读取记忆', async () => {
    const adapter = host(), main = adapter.load('main.js');
    assert.equal(await main.onPromptFinalize(input), null);
    adapter.load('settings.js').save(config);
    assert.equal(await main.onPromptFinalize({ eventPayload: { stage: 'before_finalize_prompt' } }), null);
    assert.equal(adapter.requests.length, 0);
});

test('非法地址、缺凭据与保存失败不假成功，不覆盖原设置', () => {
    const adapter = host();
    for (const patch of [{ serviceUrl: 'file:///tmp' }, { serviceUrl: '' }, { recallToken: '' }]) {
        assert.throws(() => adapter.load('settings.js').save({ ...config, ...patch }));
    }
    assert.equal(adapter.store.size, 0);
    assert.throws(() => host(new Map(), true).load('settings.js').save(config), /未保存/);
});

test('原生设置页表单保存、重开和关闭可回读；密码字段遮挡', () => {
    const adapter = host(), state = new Map();
    const UI = new Proxy({}, { get: (_target, name) => (props, children = []) => ({ name, props, children }) });
    const ctx = { UI, useState: (key, initial) => {
        if (!state.has(key)) state.set(key, initial);
        return [state.get(key), value => state.set(key, value)];
    } };
    const screen = adapter.load('settings.ui.js').default;
    const render = () => screen(ctx).children;
    let nodes = render();
    nodes.find(n => n.name === 'TextField' && n.props.isPassword).props.onValueChange('synthetic-token');
    nodes.find(n => n.name === 'TextField' && !n.props.isPassword).props.onValueChange('https://recall.invalid');
    nodes.find(n => n.name === 'Row').children.find(n => n.name === 'Switch').props.onCheckedChange(true);
    render().find(n => n.name === 'Button').props.onClick();
    assert.match(state.get('status'), /已保存/);
    assert.equal(host(adapter.store).load('settings.js').load().enabled, true);
    render().find(n => n.name === 'Row').children.find(n => n.name === 'Switch').props.onCheckedChange(false);
    render().find(n => n.name === 'Button').props.onClick();
    assert.equal(host(adapter.store).load('settings.js').load().enabled, false);
});
