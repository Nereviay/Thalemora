"""P4 集中合成验收：临时目录、本机 mock，无真实模型或生产连接。"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from thalemora.app import Application
from thalemora.app.backup import snapshot, restore
from thalemora.app.http import create_http
from thalemora.app.mcp import create_mcp
from thalemora.app.providers import ProviderUnavailable
from thalemora.identity import AccessDenied
from thalemora.inner_world.host import AgentRequired
from test_p3_inner_world import SyntheticHost, NOW


class ProviderMock(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append((self.path, self.headers.get('Authorization'), body))
        if body['model'] == 'failure':
            self.send_response(503)
            self.end_headers()
            return
        usage = dict(prompt_tokens=5, completion_tokens=3, total_tokens=8)
        output = dict(data=[dict(embedding=[0.6, 0.8])]) if self.path.endswith('embeddings') else dict(choices=[dict(message=dict(content='合成 mock 回复'))])
        if body['model'] != 'missing-usage':
            output['usage'] = usage
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(output).encode())


class P4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = ThreadingHTTPServer(('127.0.0.1', 0), ProviderMock)
        cls.thread = Thread(target=cls.mock.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.mock.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.mock.shutdown()
        cls.mock.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = TemporaryDirectory(dir=os.environ['TMPDIR'], prefix='p4-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = Application(self.root / 'state')
        self.a = self.service.register_agent('同名合成 Agent')['agent_id']
        self.b = self.service.register_agent('同名合成 Agent')['agent_id']
        self.app = 'demo-app'
        for agent in (self.a, self.b):
            self.service.grant_source(agent, self.app)
        self.client = TestClient(create_http(self.service, 'synthetic-management-token'))
        self.headers = {'Authorization': 'Bearer synthetic-management-token'}
        self.env = patch.dict(os.environ, {'P4_SYNTHETIC_PROVIDER_KEY': 'synthetic-mock-only'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def event(self, **updates):
        value = dict(app=self.app, channel='main', source_event_id='synthetic-1', version=1,
                     role='user', text='纯合成旅行计划\n原文  保留🙂', occurred_at=NOW,
                     conversation_id='synthetic-conversation', epoch_id=None, deleted=False,
                     content_hash=None, hash_verification='unknown', raw_event_id=None)
        return dict(value, **updates)

    def insert(self, **updates):
        event = self.event(**updates)
        return self.service.import_history(self.a, self.app, [event], event['channel'])

    def get(self, operation, **args):
        return self.client.get('/api/' + operation, params=args, headers=self.headers)

    def post(self, operation, **args):
        return self.client.post('/api/' + operation, json=args, headers=self.headers)

    def config(self, module='writer', **updates):
        values = dict(enabled=True, base_url=self.base, model='synthetic-model',
                      key_env='P4_SYNTHETIC_PROVIDER_KEY', capability='chat', call_budget=2, token_budget=100)
        return self.service.providers.configure(module, **dict(values, **updates))

    def host(self):
        host = SyntheticHost(self.root / 'synthetic-host.sqlite')
        host.confirm(self.a)
        self.service.host_reader = host
        return host

    def test_empty_instance_no_key_and_no_network(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('无 Key 不得联网')):
            empty = Application(self.root / 'empty')
            self.assertEqual(empty.list_agents(), [])
            self.assertEqual({m['state'] for m in empty.capabilities()['models']}, {'unconfigured'})
            self.assertEqual(self.service.inner_state(self.a, self.app, 'diary'), [])

    def test_persistent_two_agents_same_source_ids_isolated(self):
        self.insert()
        self.service.import_history(self.b, self.app, [self.event(text='另一个 Agent 的事实')])
        restarted = Application(self.root / 'state')
        self.assertEqual(len(restarted.list_agents()), 2)
        self.assertEqual(restarted.history(self.a, self.app), [self.event()])
        self.assertEqual(restarted.search(self.b, self.app, '旅行计划'), [])

    def test_private_and_ungranted_sources_denied(self):
        for operation in ('history', 'search', 'memory', 'water', 'inner_state'):
            args = dict(agent_id=self.a, app_id=self.app, channel='private')
            if operation == 'search': args['query'] = '旅行计划'
            if operation == 'inner_state': args['kind'] = 'diary'
            self.assertEqual(self.get(operation, **args).status_code, 403)
        self.service.grant_source(self.a, self.app, 'private')
        self.insert(channel='private', text='私有合成记忆')
        self.assertEqual(self.service.history(self.a, self.app), [])
        self.assertEqual(len(self.service.history(self.a, self.app, 'private')), 1)
        with self.assertRaises(AccessDenied):
            self.service.history(self.b, self.app, 'private')

    def test_authentication_body_role_and_agent_spoofing(self):
        self.assertEqual(self.client.get('/api/list_agents').status_code, 401)
        self.assertEqual(self.client.get('/api/list_agents', headers={'Authorization':'Bearer wrong'}).status_code, 401)
        denied = self.post('register_agent', display_name='假身份', principal='another-owner')
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(len(self.service.list_agents()), 2)
        self.assertEqual(self.post('publish', actor='agent', role='agent').status_code, 404)
        self.assertEqual(self.get('history', agent_id='00000000-0000-0000-0000-000000000001', app_id=self.app).status_code, 403)

    def test_http_real_create_grant_import_search_export(self):
        agent = self.post('register_agent', display_name='HTTP 合成').json()['result']['agent_id']
        self.assertEqual(self.post('grant_source', agent_id=agent, app_id=self.app, channel='main').status_code, 200)
        imported = self.post('import_history', agent_id=agent, app_id=self.app, events=[self.event()])
        self.assertEqual(imported.json()['result'][0]['status'], 'inserted')
        self.assertEqual(self.get('search', agent_id=agent, app_id=self.app, query='旅行计划').json()['result'][0]['text'], self.event()['text'])
        self.assertEqual(self.get('history', agent_id=agent, app_id=self.app).json()['result'], [self.event()])

    def test_lexical_index_tracks_edit_and_tombstone(self):
        self.insert()
        self.assertEqual(len(self.service.search(self.a, self.app, '旅行计划')), 1)
        self.insert(version=2, text='合成城市漫步')
        self.assertEqual(self.service.search(self.a, self.app, '旅行计划'), [])
        self.assertEqual(len(self.service.search(self.a, self.app, '城市漫步')), 1)
        self.insert(version=3, deleted=True)
        self.assertEqual(self.service.search(self.a, self.app, '城市漫步'), [])
        self.assertIsNone(self.service.history(self.a, self.app, include_deleted=True)[0]['text'])

    def test_pagination_is_bounded_and_mixed_import_rejected(self):
        events = [self.event(source_event_id=f'e-{i:03}') for i in range(45)]
        self.service.import_history(self.a, self.app, events)
        self.assertEqual(len(self.service.history(self.a, self.app, limit=20, offset=40)), 5)
        for limit in (0, 101):
            self.assertEqual(self.get('history', agent_id=self.a, app_id=self.app, limit=limit).status_code, 400)
        with self.assertRaises(ValueError):
            self.service.import_history(self.a, self.app, [self.event(channel='private')])
        with self.assertRaises(ValueError):
            self.service.import_history(self.a, self.app, [self.event()] * 101)

    def test_memory_revision_evidence_change_and_delete_resolution(self):
        self.insert()
        saved = self.service.remember(self.a, self.app, 'synthetic-1')
        card = self.service.memory(self.a, self.app)[0]
        self.assertEqual(card['current_revision_id'], saved['current_revision_id'])
        evidence = self.service.evidence(self.a, self.app, card['id'])[0]
        self.assertEqual(evidence['resolution'], 'current')
        self.assertFalse(evidence['verified'])
        self.insert(version=2, text='修订后的合成正文')
        self.assertEqual(self.service.evidence(self.a, self.app, card['id'])[0]['resolution'], 'source_changed')
        self.insert(version=3, deleted=True)
        evidence = self.service.evidence(self.a, self.app, card['id'])[0]
        self.assertEqual(evidence['resolution'], 'deleted')
        self.assertIsNone(evidence['current_source_text'])
        self.assertEqual(self.service.memory(self.b, self.app), [])

    def test_water_real_nodes_membership_and_core_constraints(self):
        self.insert()
        card = self.service.remember(self.a, self.app, 'synthetic-1')['card_id']
        self.service.water_node(self.a, self.app, 'lake-1', 'lake', '合成湖')
        self.service.water_node(self.a, self.app, 'stream-1', 'stream', '合成溪', parent_node_id='lake-1')
        self.service.water_move(self.a, self.app, card, 'stream-1', '合成归属')
        self.assertEqual(len(self.service.water(self.a, self.app)), 2)
        self.assertEqual(self.service.water_memberships(self.a, self.app)[0]['container_node_id'], 'stream-1')
        with self.assertRaises(ValueError):
            self.service.water_move(self.a, self.app, card, 'lake-1', '不允许归属湖')

    def test_no_host_autonomous_writes_and_epoch_candidate_fail_honestly(self):
        with self.assertRaises(AgentRequired):
            self.service.publish(self.a, self.app, 'diary', 'untrusted-reference')
        with self.assertRaises(AgentRequired):
            self.service.continuity(self.a, self.app, 'fake-epoch', [], NOW)

    def test_synthetic_host_inner_domains_continuity_and_private_gate(self):
        host = self.host()
        diary = None
        for kind in ('diary', 'dream'):
            ref = host.add(self.a, kind, {'text':'合成本人创作'}, app=self.app)
            saved = self.service.publish(self.a, self.app, kind, ref)
            self.assertEqual(self.service.inner_state(self.a, self.app, kind)[0]['source_ref'], ref)
            if kind == 'diary': diary = saved
        from thalemora.emotion import DIM_KEYS
        payload = dict(dims={k:0 for k in DIM_KEYS}, label='平静', emoji='🙂', note='合成', source='合成对话', source_type='conversation')
        self.service.publish(self.a, self.app, 'emotion', host.add(self.a, 'emotion', payload, app=self.app))
        candidate = self.service.continuity(self.a, self.app, 'epoch-1', [('diary', diary['item_id'], 1)], NOW)
        self.assertFalse(candidate['payload']['adopted'])
        self.service.grant_source(self.a, self.app, 'private')
        host.confirm(self.a, 'private')
        private_ref = host.add(self.a, 'whisper', {'text':'合成私有低语'}, app=self.app, channel='private')
        with self.assertRaises(AccessDenied):
            self.service.publish(self.a, self.app, 'whisper', private_ref)
        self.service.publish(self.a, self.app, 'whisper', private_ref, 'private')
        self.assertEqual(len(self.service.inner_state(self.a, self.app, 'whisper', 'private')), 1)

    def test_synthetic_host_bad_evidence_and_cross_agent_rejected(self):
        host = self.host()
        bad = host.add(self.a, 'diary', {'text':'非完成'}, app=self.app, status='running')
        with self.assertRaises(AccessDenied):
            self.service.publish(self.a, self.app, 'diary', bad)
        other = host.add(self.b, 'diary', {'text':'其他 Agent'}, app=self.app)
        with self.assertRaises(AccessDenied):
            self.service.publish(self.a, self.app, 'diary', other)

    def test_self_model_real_core_stage_finalize_and_browse(self):
        host = self.host()
        tool = 'self_model_observe'
        args = dict(statement='我会确认合成来源', domain='habit', topic_key='check_source', scope='global', requested_maturity='established')
        ref = host.add(self.a, 'self_model', dict(tool_name=tool,args=args), app=self.app,
                       source_type='canonical_tool_call', tool_name=tool)
        receipt = self.service.publish(self.a, self.app, 'self_model', ref)
        self.assertTrue(receipt)
        claims = self.service.inner_state(self.a, self.app, 'self_model')
        self.assertEqual(claims[0]['statement'], args['statement'])
        self.assertEqual(self.service.inner_state(self.b, self.app, 'self_model'), [])

    def test_persona_and_reflection_are_real_versions(self):
        result = self.service.configure_persona(self.a, self.app, authority='managed', expected_version=0,
                                               reason='合成配置', recorded_at=NOW, content='合成人格配置')
        self.assertEqual(result['version'], 1)
        self.service.reflect(self.a, self.app, text='人工建议', target_kind='persona', target_id='persona', target_version=1, recorded_at=NOW)
        self.assertEqual(len(self.service.inner_state(self.a, self.app, 'reflection')), 1)
        self.assertEqual(self.service.inner_state(self.a, self.app, 'persona')[0]['content'], '合成人格配置')

    def test_mcp_tools_share_same_service_and_private_denial(self):
        self.insert()
        server = create_mcp(self.service)
        async def run():
            self.assertEqual({t.name for t in await server.list_tools()}, {'list_agents','history','search','inner_state'})
            result = await server.call_tool('history', dict(agent_id=self.a, app_id=self.app))
            self.assertIn('旅行计划', str(result))
            from mcp.server.fastmcp.exceptions import ToolError
            with self.assertRaises(ToolError):
                await server.call_tool('history', dict(agent_id=self.b, app_id=self.app, channel='private'))
        asyncio.run(run())

    def test_provider_default_disabled_unsupported_and_no_key_no_request(self):
        before = len(ProviderMock.requests)
        with self.assertRaises(ProviderUnavailable): self.service.providers.call('writer', '无 Key 合成')
        self.assertEqual(self.config(enabled=False)['state'], 'disabled')
        with self.assertRaises(ProviderUnavailable): self.service.providers.call('writer', '未启用')
        self.assertEqual(self.config(capability='embedding')['state'], 'unsupported')
        with self.assertRaises(ProviderUnavailable): self.service.providers.call('writer', '能力不符')
        self.assertEqual(len(ProviderMock.requests), before)
        self.assertEqual(self.service.providers.audit()[0]['status'], 'blocked')

    def test_provider_real_http_actual_usage_and_separate_module_budget(self):
        self.config(call_budget=1)
        reply = self.post('call_provider', module='writer', text='合成人工显式请求')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()['result']['usage']['total_tokens'], 8)
        path, authorization, body = ProviderMock.requests[-1]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(authorization, 'Bearer synthetic-mock-only')
        self.assertEqual(body['messages'][0]['content'], '合成人工显式请求')
        with self.assertRaisesRegex(ProviderUnavailable, 'budget_exceeded'):
            self.service.providers.call('writer', '超过预算')
        self.assertEqual(self.service.providers.status('writer')['state'], 'budget_exceeded')
        self.config('reflection', call_budget=1)
        self.assertEqual(self.service.providers.call('reflection', '独立预算')['usage']['total_tokens'], 8)
        self.assertIsNone(self.service.providers.status('writer')['actual_cost'])

    def test_provider_embedding_actual_request_and_token_budget(self):
        self.config('embedding', capability='embedding', token_budget=8)
        self.assertEqual(self.service.providers.call('embedding','合成向量')['output'], [0.6,0.8])
        self.assertEqual(ProviderMock.requests[-1][0], '/v1/embeddings')
        with self.assertRaisesRegex(ProviderUnavailable, 'budget_exceeded'):
            self.service.providers.call('embedding','超过 token 阈值')

    def test_provider_failure_and_missing_usage_are_audited(self):
        self.config(model='failure')
        with self.assertRaises(ProviderUnavailable): self.service.providers.call('writer','合成失败')
        self.assertEqual(self.service.providers.status('writer')['state'], 'error')
        self.assertIsNone(self.service.providers.audit()[0]['tokens'])
        self.assertIsNone(self.service.providers.status('writer')['actual_tokens'])
        self.config('reflection', model='missing-usage')
        with self.assertRaises(ProviderUnavailable): self.service.providers.call('reflection','无 usage')
        self.assertEqual(self.service.providers.audit()[0]['status'], 'error')
        self.assertIsNone(self.service.providers.audit()[0]['tokens'])

    def test_explicit_local_embedder_no_model_download(self):
        import numpy as np
        class LocalEmbedder:
            def encode_many(self, texts, *, batch_size=16): return np.array([[0.6,0.8] for _ in texts], dtype=np.float32)
            def encode_one(self, text): return np.array([0.6,0.8], dtype=np.float32)
        with self.assertRaises(ValueError): self.service.build_local_index(self.a, self.app)
        self.insert()
        self.service.remember(self.a, self.app, 'synthetic-1')
        self.service.embedder = ('synthetic-local',LocalEmbedder())
        self.service.build_local_index(self.a, self.app)
        self.assertEqual(len(self.service.local_search(self.a, self.app, '旅行计划')), 1)

    def test_snapshot_restore_all_domains_identity_and_usage(self):
        self.insert()
        self.service.remember(self.a, self.app, 'synthetic-1')
        self.service.water_node(self.a, self.app, 'lake-1','lake','合成湖')
        self.service.configure_persona(self.a, self.app, authority='managed', expected_version=0,
                                      reason='合成', recorded_at=NOW, content='合成人格')
        host = self.host()
        self.service.publish(self.a, self.app, 'diary', host.add(self.a, 'diary', {'text':'合成创作'}, app=self.app))
        self.service.inner_state(self.a, self.app, 'self_model')
        self.service.grant_source(self.a,self.app,'private')
        self.insert(channel='private',text='恢复时须保留的合成私有正文')
        self.config()
        self.service.providers.call('writer', '合成用量')
        result = snapshot(self.root/'state', self.root/'snapshot')
        self.assertGreaterEqual(result['databases'], 6)
        restore(self.root/'snapshot', self.root/'restored')
        new = Application(self.root/'restored')
        self.assertEqual(new.history(self.a,self.app), self.service.history(self.a,self.app))
        self.assertEqual(new.memory(self.a,self.app), self.service.memory(self.a,self.app))
        self.assertEqual(new.evidence(self.a,self.app,1)[0]['resolution'], 'current')
        self.assertEqual(len(new.inner_state(self.a,self.app,'diary')),1)
        self.assertEqual(new.providers.status('writer')['actual_tokens'],8)
        self.assertEqual(new.history(self.a,self.app,'private')[0]['text'],'恢复时须保留的合成私有正文')
        with self.assertRaises(AccessDenied): new.history(self.b,self.app,'private')
        self.assertTrue(str(new.registry.archive_db_path(new.owner,self.a)).startswith(str(self.root/'restored')))
        with self.assertRaises(ValueError): restore(self.root/'snapshot', self.root/'restored')

    def test_cli_full_history_export_import_over_page_boundary(self):
        events=[self.event(source_event_id=f'e-{i:03}') for i in range(100)]
        self.service.import_history(self.a,self.app,events)
        self.service.import_history(self.a,self.app,[self.event(source_event_id='e-100',deleted=True)])
        file=self.root/'history.jsonl'
        base=[sys.executable,'-m','thalemora.app','--data',str(self.root/'state')]
        subprocess.run(base+['export-history',self.a,self.app,str(file)],check=True,capture_output=True)
        self.assertEqual(len(file.read_text().splitlines()),101)
        subprocess.run(base+['import-history',self.b,self.app,str(file)],check=True,capture_output=True)
        self.assertEqual(len(self.service.history(self.b,self.app,limit=100,offset=100,include_deleted=True)),1)
        subprocess.run([sys.executable,'-m','thalemora.app','backup',str(self.root/'state'),str(self.root/'cli-backup')],check=True,capture_output=True)

    def test_static_console_health_and_request_contract(self):
        self.assertEqual(self.client.get('/health').json(),{'status':'ok'})
        html=self.client.get('/').text
        self.assertIn('lang="zh-CN"',html)
        self.assertIn('/app.js',html)
        js=self.client.get('/app.js').text
        self.assertIn('fetch(',js)
        self.assertNotIn('localStorage',js)
        self.assertNotIn('innerHTML',js)
        self.assertEqual(self.client.get('/../service.py').status_code,404)
        self.assertEqual(self.post('import_history',agent_id=self.a,app_id=self.app,events=[]).status_code,400)
        response=self.client.post('/api/import_history',content=b'x'*(1024*1024+1),headers=self.headers)
        self.assertEqual(response.status_code,413)

    def test_official_mcp_stdio_wire(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        self.insert()
        async def run():
            params=StdioServerParameters(command=sys.executable,args=['-m','thalemora.app','--data',str(self.root/'state'),'mcp'],
                                         env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')})
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    tools=await session.list_tools()
                    self.assertEqual(len(tools.tools),4)
                    reply=await session.call_tool('search',dict(agent_id=self.a,app_id=self.app,query='旅行计划'))
                    self.assertFalse(reply.isError)
                    self.assertIn('旅行计划',reply.content[0].text)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
