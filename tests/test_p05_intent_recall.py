"""05：只用临时合成库，禁止网络与动作执行。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from thalemora.app import Application
from thalemora.identity import AccessDenied
from thalemora.identity.registry import InvalidIdentity
from thalemora.retrieval.intent import recall_for_intent
from test_p8_recall import RecallTests as _Fixtures


class IntentRecallTests(unittest.TestCase):
    event = _Fixtures.event
    insert = _Fixtures.insert
    card = _Fixtures.card

    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='p05-synthetic-')
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(Path(self.tmp.name) / 'state')
        self.agent = self.app.register_agent('合成甲')['agent_id']
        self.other = self.app.register_agent('合成乙')['agent_id']
        for agent in (self.agent, self.other):
            self.app.grant_source(agent, 'demo')
        net = patch('thalemora.app.providers.urlopen', side_effect=AssertionError('禁止联网'))
        net.start()
        self.addCleanup(net.stop)

    def recall(self, surface='contact', context=None, **kwargs):
        return recall_for_intent(self.app, self.agent, 'demo', surface=surface,
                                 context=context or {}, **kwargs)

    def test_contact_open_loop_single_call(self):
        card = self.card('ticket', '青岚展览门票尚未预约')
        with patch.object(self.app, 'recall_for_turn', wraps=self.app.recall_for_turn) as call:
            result = self.recall(context={'open_loops': [{'title': '青岚展览门票', 'status': 'open'}],
                                          'now_text': '周末', 'life_state': {'location': '青岚展馆'}})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['memories'][0]['id'], card['card_id'])
        self.assertNotIn('contact', call.call_args.args[2])
        self.assertLessEqual(len(call.call_args.args[2]), 200)

    def test_proactive_loop_content_and_summary_fields(self):
        self.card('loop', '青岚展览门票尚未预约')
        for item in ({'content': '青岚展览门票', 'status': 'open'},
                     {'summary': '青岚展览门票', 'status': 'open'},
                     {'description': '青岚展览门票', 'status': 'open'}):
            with self.subTest(item=item):
                self.assertIn('尚未预约',
                              self.recall(context={'open_loops': [item]})['rendered'])

    def test_activity_history_and_unfinished_task(self):
        self.insert('walk', '青岚步道散步很舒服')
        for surface in ('activity', 'find_activity'):
            result = self.recall(surface, {'recent_activity': ['青岚步道散步'],
                                           'unfinished_tasks': ['青岚步道摄影']})
            self.assertIn('walk', [r['id'] for r in result['archive']])

    def test_preaction_constraint_and_no_execution(self):
        card = self.card('rule', '青岚工程只允许在预览分支修改')
        path = self.app.memory_path(self.agent, 'demo', 'main')
        before = path.read_bytes()
        result = self.recall('preaction', {'needs_precheck': True, 'tool_name': 'edit_file',
                                          'args': {'project': '青岚工程'}})
        self.assertEqual([r['id'] for r in result['memories']], [card['card_id']])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(result), {'rendered', 'memories', 'archive', 'errors'})
        self.assertLessEqual(len(result['archive']), 1)

    def test_host_skips_and_no_anchor_zero_retrieval(self):
        with patch.object(self.app, 'recall_for_turn', side_effect=AssertionError('不应检索')):
            for flag in ('read_only', 'routine', 'already_checked'):
                self.assertFalse(self.recall('preaction', {'needs_precheck': True, flag: True,
                                  'args': {'project': '青岚工程'}})['rendered'])
            for surface, context in [('preaction', {'needs_precheck': True, 'tool_name': 'shell'}),
                                     ('preaction', {'args': {'project': '青岚工程'}}),
                                     ('contact', {'now_text': '2026-10-27'}),
                                     ('activity', {'recent_user_text': '你好'}),
                                     ('contact', {'open_loops': [{'title': '青岚工程', 'status': 'done'}]})]:
                self.assertFalse(self.recall(surface, context)['rendered'])

    def test_scope(self):
        self.card('other', '青岚工程独有约束', agent=self.other)
        self.app.grant_source(self.agent, 'private-app')
        self.card('private', '青岚工程独有约束', app_id='private-app')
        self.app.grant_source(self.agent, 'demo', channel='private')
        self.card('channel', '青岚工程独有约束', channel='private')
        self.assertFalse(self.recall(context={'open_loops': ['青岚工程']})['rendered'])
        with self.assertRaises((AccessDenied, InvalidIdentity)):
            self.recall(context={'open_loops': ['青岚工程']}, channel='ungranted')

    def test_visible_and_stale_revision(self):
        card = self.card('rule', '青岚工程只能预览')
        context = {'open_loops': ['青岚工程']}
        for visible in ({'visible_memory_ids': [card['card_id']]},
                        {'visible_archive_ids': ['rule']},
                        {'visible_texts': ['青岚工程只能预览']}):
            self.assertFalse(self.recall(context=context, **visible)['rendered'])
        self.app.revise_memory(self.agent, 'demo', card['card_id'], '青岚工程允许本地编辑',
                               card['current_revision_id'])
        result = self.recall(context=context)
        self.assertNotIn('只能预览', result['rendered'])
        self.assertIn('允许本地编辑', result['rendered'])
        # 人工正式修订本身仍有效，不因原始聊天后来被删除而抹掉。
        self.insert('rule', '', version=2, deleted=True)
        result = self.recall(context=context)
        self.assertNotIn('只能预览', result['rendered'])
        self.assertIn('允许本地编辑', result['rendered'])
        # 未经过人工修订的另一条来源删除后不应成为陈旧候选。
        extra = self.card('unrevised', '绀青文档不允许公开')
        self.insert('unrevised', '', version=2, deleted=True)
        self.assertFalse(self.recall(context={'open_loops': ['绀青文档']})['rendered'])

    def test_sensitive_fields_are_not_sent_or_returned(self):
        marker = 'synthetic-credential-value'
        with patch.object(self.app, 'recall_for_turn', wraps=self.app.recall_for_turn) as call:
            self.recall('preaction', {'needs_precheck': True, 'tool_name': 'edit_file',
                'args': {'project': '青岚工程', 'api_key': marker,
                         'nested': {'token': marker}, 'command': 'authorization=' + marker}})
        query = call.call_args.args[2]
        self.assertTrue(marker not in query)
        self.assertTrue(all(k not in query for k in ('api_key', 'token', 'authorization')))
        self.insert('unsafe', '青岚工程 password=' + marker)
        result = self.recall('preaction', {'needs_precheck': True, 'args': {'project': '青岚工程'}})
        self.assertTrue(marker not in json.dumps(result))
        self.assertFalse(result['rendered'])

    def test_counts_budget_and_unrelated(self):
        for i, text in enumerate(('青岚工程发布约束', '青岚工程构建限制')):
            self.card('m' + str(i), text)
        for i, text in enumerate(('青岚工程原话甲', '青岚工程原话乙')):
            self.insert('a' + str(i), text)
        result = self.recall(context={'open_loops': ['青岚工程']})
        self.assertEqual(len(result['memories']), 2)
        self.assertEqual(len(result['archive']), 2)
        for i in range(4):
            self.insert('long' + str(i), '青岚工程' + str(i) * 320)
        result = self.recall(context={'recent_user_text': '青岚工程' * 100})
        self.assertLessEqual(len(result['rendered']), 700)
        self.assertLessEqual(len(result['memories']), 2)
        self.assertLessEqual(len(result['archive']), 2)
        self.assertFalse(self.recall(context={'open_loops': ['紫杉花园浇水']})['rendered'])


# 仅复用夹具方法，不让 unittest 发现旧测试类。
del _Fixtures
