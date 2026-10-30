"""07 本地合成 Gate；假 Context/Provider 不代表现役 Runtime 验收。"""
import json
import unittest
from unittest.mock import patch

from thalemora.adapters.niannian_vnext import recall_vnext
from thalemora.identity import AccessDenied
from thalemora.identity.registry import InvalidIdentity
from test_p05_intent_recall import IntentRecallTests as _Fixtures
from test_p06_continuity import ContinuityTests as _Continuity


def final_messages(result, user_text):
    # 简化模拟现役 normal_projection：文本层模拟 system，API 仍为 user；
    # 实际 Runtime / Gemini 转换另在隔离服务测，不以此冒充线上 Provider 验收。
    blocks = []
    if result['rendered']:
        blocks.append(dict(id='memory_vnext_prelude',
                           content=result['rendered'], trace=result['trace']))
    user = user_text
    if blocks:
        # 与现役 context_assembly._NORMAL_REFERENCE_MARKER 的字面值对齐。
        user += ('\n\n【本轮参考资料｜系统提供，非用户发言】\n\n'
                 '<message role="system">\n'
                 + '\n\n'.join(b['content'] for b in blocks)
                 + '\n</message>')
    return blocks, [dict(role='system', content='合成固定 Persona'),
                    dict(role='user', content=user)]


class BridgeGateTests(unittest.TestCase):
    setUp = _Fixtures.setUp
    event = _Fixtures.event
    insert = _Fixtures.insert
    card = _Fixtures.card

    def bridge(self, surface='normal', **request):
        return recall_vnext(self.app, self.agent, 'demo', request, surface=surface,
                            raw_to_archive={}, preaction_context={'needs_precheck': True})

    def ref(self, source):
        return dict(agent_id=self.agent, app_id='demo', channel='main',
                    source_event_id=source, version=1)

    def test_surfaces_final_messages_single_recall(self):
        card = self.card('rule', '青岚工程只允许在预览分支修改')
        for surface, payload in (
            ('normal', dict(user_text='青岚工程')),
            ('contact', dict(open_loops=[{'content': '青岚工程', 'status': 'open'}])),
            ('activity', dict(recent_activity=['青岚工程'])),
            ('preaction', dict(tool_name='edit_file', arguments={'project': '青岚工程'}, provider_round=1))):
            with self.subTest(surface=surface), patch.object(
                    self.app, 'recall_for_turn', wraps=self.app.recall_for_turn) as call:
                result = self.bridge(surface, generation_key='synthetic-turn', **payload)
                self.assertEqual(call.call_count, 1)
                blocks, messages = final_messages(result, '合成当前用户消息')
                self.assertEqual(blocks[0]['id'], 'memory_vnext_prelude')
                self.assertEqual(messages[0]['role'], 'system')
                self.assertEqual(messages[-1]['role'], 'user')
                self.assertEqual(messages[-1]['content'].count('<message role="system">'), 1)
                self.assertTrue(messages[-1]['content'].endswith('</message>'))
                self.assertIn('预览分支', messages[-1]['content'])
                self.assertIn('合成固定 Persona', messages[0]['content'])
                memory, = blocks[0]['trace']['memories']
                self.assertEqual(memory['id'], card['card_id'])
                self.assertEqual(memory['revision_id'], card['current_revision_id'])
                self.assertTrue(memory['source_refs'])
                self.assertEqual(blocks[0]['trace']['related']['memory_refs'],
                                 [dict(card_id=card['card_id'],
                                       revision_id=card['current_revision_id'])])
                self.assertLessEqual(len(result['rendered']), 700)
        self.assertEqual(self.app.providers.audit(), [])

    def test_seen_mapping_and_missing_mapping(self):
        self.card('archive-rule', '青岚工程只允许预览')
        payload = dict(user_text='青岚工程', visible_raw_event_ids=[71])
        for mapping in (None, {}, {71: self.ref('archive-rule')}):
            result = recall_vnext(self.app, self.agent, 'demo', payload, raw_to_archive=mapping)
            self.assertEqual(result['rendered'], '')
            mapping_failed = any(e.get('code') == 'unmapped_raw_events'
                                 for e in result['trace'].get('errors', []))
            self.assertEqual(mapping_failed, mapping != {71: self.ref('archive-rule')})
        wrong = self.ref('archive-rule') | {'agent_id': self.other}
        self.assertFalse(recall_vnext(self.app, self.agent, 'demo', payload,
                                     raw_to_archive={71: wrong})['rendered'])
        # 相似的整数和字符串不是来源映射。
        self.assertFalse(recall_vnext(self.app, self.agent, 'demo', payload,
                                     raw_to_archive={'71': self.ref('archive-rule')})['rendered'])

    def test_scope_empty_and_errors(self):
        self.card('other', '青岚工程秘密', agent=self.other)
        self.app.grant_source(self.agent, 'private-app')
        self.card('private', '青岚工程秘密', app_id='private-app')
        self.app.grant_source(self.agent, 'demo', channel='private')
        self.card('channel', '青岚工程秘密', channel='private')
        self.assertFalse(self.bridge(user_text='青岚工程')['rendered'])
        self.assertFalse(self.bridge(user_text='紫杉花园浇水')['rendered'])
        with self.assertRaises((AccessDenied, InvalidIdentity)):
            recall_vnext(self.app, self.agent, 'demo', {}, channel='ungranted')
        with patch.object(self.app, 'recall_for_turn', side_effect=RuntimeError('合成失败')):
            with self.assertRaisesRegex(RuntimeError, '合成失败'):
                self.bridge(user_text='青岚工程')

    def test_revision_and_excluded_card(self):
        card = self.card('rule', '青岚工程只能预览')
        self.assertFalse(self.bridge(user_text='青岚工程',
                         exclude_memory_card_ids=[card['card_id']])['rendered'])
        self.app.revise_memory(self.agent, 'demo', card['card_id'], '青岚工程允许编辑',
                               card['current_revision_id'])
        result = self.bridge(user_text='青岚工程')
        self.assertIn('允许编辑', result['rendered'])
        self.assertNotIn('只能预览', result['rendered'])
        self.assertNotEqual(result['trace']['memories'][0]['revision_id'], card['current_revision_id'])
        self.card('stale', '绀青文档禁止公开')
        self.insert('stale', '', version=2, deleted=True)
        self.assertFalse(self.bridge(user_text='绀青文档')['rendered'])

    def test_previous_assistant_requires_mapping_and_current_version(self):
        self.card('tea', '乌龙茶适合周末冲泡')
        event = self.event('prior', '1、茉莉花茶\n2、乌龙茶')
        event.update(role='assistant', epoch_id='epoch')
        self.app.import_history(self.agent, 'demo', [event])
        prior = self.ref('prior') | {'text': event['text']}
        payload = dict(user_text='第二个呢？', previous_assistant_event_id='assistant-nine', current_epoch_id='epoch')
        def run(mapping):
            return recall_vnext(self.app, self.agent, 'demo', payload, raw_to_archive=mapping,
                                previous_assistant_ref=prior, conversation_id='synthetic')
        self.assertIn('乌龙茶', run({'assistant-nine': self.ref('prior')})['rendered'])
        self.assertFalse(run({})['rendered'])
        event.update(version=2, text='已更正的上文')
        self.app.import_history(self.agent, 'demo', [event])
        self.assertFalse(run({'assistant-nine': self.ref('prior')})['rendered'])

    def test_preaction_host_decides_and_no_action(self):
        self.card('rule', '青岚工程只允许预览')
        with patch.object(self.app, 'recall_for_turn', side_effect=AssertionError('不应检索')):
            result = recall_vnext(self.app, self.agent, 'demo',
                dict(tool_name='edit_file', arguments={'project': '青岚工程'}),
                surface='preaction', raw_to_archive={})
        self.assertFalse(result['rendered'])


class MaintenanceBridgeGateTests(unittest.TestCase):
    setUp = _Continuity.setUp
    source = _Continuity.source
    tick = _Continuity.tick
    cards = _Continuity.cards
    revisions = _Continuity.revisions
    seed = _Continuity.seed

    def test_full_clarify_confirm_revision_then_normal_only_current(self):
        before, _ = self.seed('角色A的居住地是成都')
        question = '角色A的居住地是重庆'
        first = recall_vnext(self.app, self.agent, 'synthetic-app',
                             dict(user_text=question), raw_to_archive={})
        hints = first['trace'].get('fact_hints') or []
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]['status'], 'needs_clarification')
        self.assertEqual(hints[0]['id'], before['id'])
        self.assertIn('待用户确认', first['rendered'])
        self.assertEqual('角色A的居住地是成都', self.cards()[0]['content'])
        self.assertEqual(1, len(self.revisions()))

        # Agent 是否自然追问归宿主模型；本测试只证明其有可追溯澄清依据。
        self.source('我之前记错了，角色A的居住地是重庆',
                    '角色A的居住地是重庆', 'correct')
        self.assertEqual([], self.tick()['errors'])
        after, = self.cards()
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['content'], '角色A的居住地是重庆')
        self.assertEqual(2, len(self.revisions()))
        normal = recall_vnext(self.app, self.agent, 'synthetic-app',
                              dict(user_text=question), raw_to_archive={})
        self.assertNotIn('成都', normal['rendered'])
        self.assertFalse(normal['trace'].get('fact_hints'))
        old = recall_vnext(self.app, self.agent, 'synthetic-app',
                           dict(user_text='角色A以前居住地成都，后来怎么更正的？'),
                           raw_to_archive={})
        self.assertIn('成都', old['rendered'])
        self.assertIn('旧原话', old['rendered'])
        n = len(self.provider.calls)
        self.tick()
        self.assertEqual(n, len(self.provider.calls))

    def test_correction_same_memory_current_and_history_sources(self):
        before, old = self.seed()
        new = self.source('我之前记错，角色A现在居住地重庆', '合成角色A居住地重庆', 'correct')
        self.assertEqual(self.tick()['errors'], [])
        after, = self.cards()
        self.assertEqual(before['id'], after['id'])
        self.assertNotEqual(before['current_revision_id'], after['current_revision_id'])
        calls = len(self.provider.calls)
        for query, historical in (('合成角色A居住地', False),
                                  ('合成角色A以前居住地成都，后来怎么更正的？', True)):
            result = recall_vnext(self.app, self.agent, 'synthetic-app',
                                  dict(user_text=query), raw_to_archive={})
            _, messages = final_messages(result, query)
            self.assertIn('重庆', messages[-1]['content'])
            if historical:
                self.assertIn('修订', messages[-1]['content'])
                self.assertIn('成都', messages[-1]['content'])
                self.assertIn('旧原话', messages[-1]['content'])
                self.assertIn('不代表当前事实', messages[-1]['content'])
                self.assertEqual(result['trace']['history_revisions'][0]['source_event_id'], old)
            else:
                self.assertNotIn('成都', messages[-1]['content'])
                self.assertNotIn('历史修订', messages[-1]['content'])
                self.assertEqual(result['trace']['memories'][0]['revision_id'], after['current_revision_id'])
        evidence = self.app.evidence(self.agent, 'synthetic-app', after['id'])
        self.assertEqual({old, new}, {json.loads(e['source_ref'])['source_event_id'] for e in evidence})
        self.assertEqual(len(self.provider.calls), calls)
        self.tick()
        self.assertEqual(len(self.provider.calls), calls)
        # 用户删除旧原话后，不得借助历史修订缓存把已删除来源再次透露。
        self.app.import_history(self.agent, 'synthetic-app', [dict(
            app='synthetic-app', channel='main', source_event_id=old, version=2,
            role='user', text='', occurred_at='2001-01-01T00:00:00+00:00',
            deleted=True, hash_verification='legacy_unverified')])
        after_deletion = recall_vnext(self.app, self.agent, 'synthetic-app',
            dict(user_text='合成角色A以前居住地成都，后来怎么更正的？'), raw_to_archive={})
        self.assertNotIn('旧原话 ' + old, after_deletion['rendered'])


del _Fixtures, _Continuity
