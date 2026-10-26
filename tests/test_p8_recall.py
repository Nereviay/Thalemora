"""P8-2 隔离合成验收；模型/API 全部使用替身。"""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from thalemora.app import Application
from thalemora.app.providers import ProviderUnavailable
from thalemora.identity import AccessDenied
from thalemora.retrieval.recall import prior_assistant_query


class Embedder:
    def __init__(self, dim=2):
        self.dim = dim
        self.inputs = []

    def encode_one(self, text):
        self.inputs.append(text)
        return [1.0] + [0.0] * (self.dim - 1)

    def encode_many(self, texts, *, batch_size=16):
        return [self.encode_one(t) for t in texts]


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="p8-recall-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.app = Application(self.root)
        self.agent = self.app.register_agent("合成 Agent")["agent_id"]
        self.other = self.app.register_agent("其他 Agent")["agent_id"]
        for agent in (self.agent, self.other):
            self.app.grant_source(agent, "demo")

    def event(self, event_id, text, *, app_id="demo", channel="main", version=1, deleted=False):
        return dict(app=app_id, channel=channel, source_event_id=event_id, version=version,
                    role="user", text=text, occurred_at="2026-10-16T01:00:00Z",
                    conversation_id="synthetic", epoch_id=None, deleted=deleted,
                    content_hash=None, hash_verification="unknown", raw_event_id=None)

    def insert(self, event_id, text, *, agent=None, app_id="demo", channel="main", **kwargs):
        self.app.import_history(agent or self.agent, app_id,
                                [self.event(event_id, text, app_id=app_id, channel=channel, **kwargs)], channel)

    def card(self, event_id, text, **kwargs):
        self.insert(event_id, text, **kwargs)
        return self.app.remember(kwargs.get("agent", self.agent), kwargs.get("app_id", "demo"),
                                 event_id, kwargs.get("channel", "main"))

    def recall(self, query="以前喜欢什么茶？", **kwargs):
        return self.app.recall_for_turn(self.agent, "demo", query, **kwargs)

    def test_birthday_conflict_read_only_and_revision(self):
        card = self.card('birthday', '林小明的生日是8月')
        path = self.app.memory_path(self.agent, 'demo', 'main')
        before = path.read_bytes()
        result = self.recall('林小明的生日是9月')
        self.assertIn('待用户确认，不自动更新', result['rendered'])
        self.assertIn(str(card['card_id']), result['rendered'])
        self.assertIn(str(card['current_revision_id']), result['rendered'])
        self.assertIn('林小明的生日是9月', result['rendered'])
        self.assertLessEqual(len(result['rendered']), 700)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(set(result), {'rendered', 'memories', 'archive', 'errors'})
        self.assertTrue(all('9月' not in m['text'] for m in result['memories']))
        self.assertIn('正式修订流程（06）', self.recall('我之前记错了，林小明的生日是9月')['rendered'])

    def test_birthday_requires_explicit_current_claim(self):
        self.card('birthday', '林小明的生日是8月')
        for query in ('他的生日是9月', '林小明的生日', '林小明的生日是8月',
                      '王小明的生日是9月', '假设林小明的生日是9月',
                      '听说林小明的生日是9月', '以前林小明的生日是9月',
                      '例如林小明的生日是9月', '林小明的生日是9月吗？'):
            with self.subTest(query=query):
                self.assertNotIn('待用户确认', self.recall(query)['rendered'])

    def test_birthday_visible_filters_and_ambiguity(self):
        card = self.card('birthday', '林小明的生日是8月')
        for kwargs in (dict(visible_memory_ids=[card['card_id']]),
                       dict(visible_archive_ids=['birthday']),
                       dict(visible_texts=['林小明的生日是8月'])):
            self.assertNotIn('待用户确认', self.recall('林小明的生日是9月', **kwargs)['rendered'])
        self.card('duplicate', '林小明的生日是7月')
        self.assertNotIn('待用户确认', self.recall('林小明的生日是9月')['rendered'])
        self.assertNotIn('待用户确认', self.recall(
            '林小明的生日是9月', visible_memory_ids=[card['card_id']])['rendered'])

    def test_birthday_scope_and_stale_sources(self):
        self.card('other-person', '林小明的生日是8月', agent=self.other)
        self.app.grant_source(self.agent, 'demo', channel='private')
        self.card('other-channel', '林小明的生日是8月', channel='private')
        self.app.grant_source(self.agent, 'elsewhere')
        self.card('other-app', '林小明的生日是8月', app_id='elsewhere')
        self.assertNotIn('待用户确认', self.recall('林小明的生日是9月')['rendered'])
        self.card('birthday', '林小明的生日是8月')
        self.insert('birthday', '林小明的生日是7月', version=2)
        self.assertNotIn('待用户确认', self.recall('林小明的生日是9月')['rendered'])
        self.insert('birthday', '', version=3, deleted=True)
        self.assertNotIn('待用户确认', self.recall('林小明的生日是9月')['rendered'])

    def test_birthday_uses_only_current_memory_revision(self):
        card = self.card('birthday', '林小明的生日是8月')
        self.app.revise_memory(self.agent, 'demo', card['card_id'],
                               '林小明的生日是9月', card['current_revision_id'])
        self.assertNotIn('待用户确认', self.recall('林小明的生日是9月')['rendered'])

    def test_m07_true_user_quote_without_models_or_memory_merge(self):
        self.card('summary', '我喜欢合成红茶')
        self.insert('quote', '我和那个朋友认识十年，仍然常聊合成天文。')
        query = '我当时原话怎么说，关于“认识十年”的那个朋友？'
        with patch('thalemora.retrieval.recall.configured_model', side_effect=AssertionError('no model')):
            result = self.recall(query)
        self.assertEqual(result['memories'], [])
        self.assertEqual(result['archive'][0]['id'], 'quote')
        self.assertEqual(result['archive'][0]['version'], 1)
        self.assertEqual(result['archive'][0]['role'], 'user')
        self.assertIn('2026-10-16T01:00:00Z', result['rendered'])
        self.assertIn('用户原话', result['rendered'])
        self.assertLessEqual(len(result['rendered']), 700)
        self.assertEqual(len(result['archive']), 1)  # 缺 epoch 不补邻居
        self.assertNotIn('用户原话', self.recall(query, visible_archive_ids=['quote'])['rendered'])
        self.assertNotIn('用户原话', self.recall(query, visible_texts=[result['archive'][0]['text']])['rendered'])

    def test_m07_month_people_role_and_missing_evidence(self):
        event = self.event('plural', '我和朋友们认识十年了')
        self.app.import_history(self.agent, 'demo', [event])
        event = self.event('assistant', '我和朋友认识十年了')
        event['role'] = 'assistant'
        self.app.import_history(self.agent, 'demo', [event])
        query = '我七月当时原话怎么说，关于“认识十年”的朋友？'
        self.assertNotIn('用户原话', self.recall(query)['rendered'])
        event = self.event('july', '我和朋友认识十年了')
        event['occurred_at'] = '2026-07-16T01:00:00Z'
        self.app.import_history(self.agent, 'demo', [event])
        result = self.recall(query)
        self.assertEqual(result['archive'][0]['id'], 'july')
        self.assertNotIn('用户原话', self.recall(query.replace('七月', '八月'))['rendered'])
        self.assertNotIn('用户原话', self.recall(query.replace('七月', '上个月'))['rendered'])
        self.assertNotIn('用户原话', self.recall('我当时原话怎么说？')['rendered'])
        self.assertNotIn('用户原话', self.recall('我当时原话怎么说，“不存在的锚点”？')['rendered'])

    def test_m07_rechecks_canonical_revision_after_search(self):
        self.insert('quote', '我和朋友认识十年了')
        search = self.app.search
        def changed(*args, **kwargs):
            hits = search(*args, **kwargs)
            self.insert('quote', '更正后的合成记录', version=2)
            return hits
        with patch.object(self.app, 'search', side_effect=changed):
            result = self.recall('我当时原话怎么说，“认识十年”？')
        self.assertNotIn('用户原话', result['rendered'])
        self.assertNotIn('我和朋友认识十年了', result['rendered'])

    def test_m07_update_delete_and_memory_summary_not_quote(self):
        self.insert('quote', '我提过合成紫砂茶壶很漂亮')
        query = '我那时提过合成紫砂茶壶什么？'
        self.assertIn('用户原话', self.recall(query)['rendered'])
        self.insert('quote', '我已改口只喜欢玻璃杯', version=2)
        result = self.recall(query)
        self.assertNotIn('用户原话', result['rendered'])
        self.assertNotIn('紫砂茶壶很漂亮', result['rendered'])
        self.insert('quote', '我提过合成紫砂茶壶很漂亮', version=3)
        self.insert('quote', '', version=4, deleted=True)
        self.assertNotIn('用户原话', self.recall(query)['rendered'])
        card = self.card('summary', '合成记录仅说明喜欢茶具')
        self.app.revise_memory(self.agent, 'demo', card['card_id'],
                               '合成紫砂茶壶是用户偏好', card['current_revision_id'])
        self.assertNotIn('用户原话', self.recall(query)['rendered'])

    def prior_context(self):
        text = "1、茉莉花茶\n2、乌龙茶"
        event = self.event("prior", text)
        event.update(role="assistant", epoch_id="epoch")
        self.app.import_history(self.agent, "demo", [event])
        return dict(conversation_id="synthetic", current_epoch_id="epoch",
                    previous_assistant_ref=dict(agent_id=self.agent, app_id="demo", channel="main",
                                                source_event_id="prior", version=1, text=text))

    def keyword_card(self, event_id, keyword, **kwargs):
        card = self.card(event_id, '合成证据只描述一次远足', **kwargs)
        path = self.app.memory_path(kwargs.get('agent', self.agent),
                                    kwargs.get('app_id', 'demo'), kwargs.get('channel', 'main'))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE cards SET keywords=? WHERE id=?',
                       (json.dumps([keyword], ensure_ascii=False), card['card_id']))
        return card

    def test_exact_short_keyword_unique_and_current_without_extra_models(self):
        card = self.keyword_card('walk', '星野')
        self.recall('星野')  # 预热相同修订
        with patch('thalemora.retrieval.recall.RetrievalIndex._replace_lane',
                   side_effect=AssertionError('不得重建')), \
                patch('thalemora.retrieval.recall.configured_model', return_value=('disabled', None)) as model:
            result = self.recall('星野')
        self.assertEqual([m['id'] for m in result['memories']], [card['card_id']])
        self.assertEqual(result['memories'][0]['revision_id'], card['current_revision_id'])
        self.assertEqual([c.args[1] for c in model.call_args_list], ['embedding'])
        self.assertLessEqual(len(result['rendered']), 700)
        self.insert('walk', '来源已更新', version=2)
        self.assertEqual(self.recall('星野')['rendered'], '')

    def test_exact_short_keyword_duplicate_or_partial_is_not_unique(self):
        first = self.keyword_card('a', '星野')
        self.keyword_card('b', '星野')
        self.assertEqual(self.recall('星野')['rendered'], '')
        self.assertEqual(self.recall('星野', visible_memory_ids=[first['card_id']])['rendered'], '')
        self.keyword_card('partial', '月岚祭')
        self.assertEqual(self.recall('月岚')['rendered'], '')

    def test_exact_short_keyword_generic_and_seen_are_excluded(self):
        card = self.keyword_card('walk', '星野')
        for visible in (dict(visible_memory_ids=[card['card_id']]),
                        dict(visible_archive_ids=['walk']),
                        dict(visible_texts=['合成证据只描述一次远足'])):
            self.assertEqual(self.recall('星野', **visible)['rendered'], '')
        self.keyword_card('generic', '朋友')
        self.assertEqual(self.recall('朋友')['rendered'], '')
        self.assertEqual(self.recall('星 野')['rendered'], '')

    def test_exact_short_keyword_agent_source_isolation_and_normal_hit(self):
        self.keyword_card('other', '星野', agent=self.other)
        self.app.grant_source(self.agent, 'elsewhere')
        self.keyword_card('private', '星野', app_id='elsewhere')
        self.assertEqual(self.recall('星野')['rendered'], '')
        card = self.keyword_card('local', '星野')
        self.assertEqual([m['id'] for m in self.recall('星野')['memories']], [card['card_id']])
        self.insert('raw', '星野是当前原话中的名字')
        result = self.recall('星野')
        self.assertEqual(result['memories'], [])
        self.assertEqual(result['archive'][0]['id'], 'raw')
        with self.assertRaises(AccessDenied):
            self.app.recall_for_turn(self.other, 'elsewhere', '星野')

    def test_shared_keywords_derived_relation_requires_two_independent_sources(self):
        first = self.card('old', '合成红茶和合成茶壶：旧计划')
        second = self.card('new', '合成红茶和合成茶壶：新计划')
        path = self.app.memory_path(self.agent, 'demo', 'main')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE cards SET keywords=? WHERE id IN (?,?)",
                       ('合成红茶,合成茶壶', first['card_id'], second['card_id']))
        result = self.recall('比较合成红茶')
        self.assertEqual({m['id'] for m in result['memories']},
                         {first['card_id'], second['card_id']})
        derived = [m for m in result['memories'] if m.get('related_evidence')]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]['related_evidence']['shared_keywords'],
                         ['合成红茶', '合成茶壶'])
        self.assertIn('未保存关联图', result['rendered'])
        self.assertLessEqual(len(result['rendered']), 700)
        self.assertEqual(len(self.recall('合成红茶')['memories']), 1)
        self.assertEqual(len(self.recall(
            '比较合成红茶', visible_memory_ids=[second['card_id']])['memories']), 1)
        self.assertEqual(len(self.recall(
            '比较合成红茶', visible_archive_ids=['new'])['memories']), 1)

        # 只有一个共享关键词，不能把普通检索相似性冒充关联证据。
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE cards SET keywords=? WHERE id=?",
                       ('合成红茶', second['card_id']))
        self.assertNotIn('未保存关联图', self.recall('比较合成红茶')['rendered'])

    def test_stored_related_graph_checks_current_revision_and_real_sources(self):
        first = self.card('first', '合成红茶：甲城的方案')
        second = self.card('second', '合成红茶：乙城的方案')
        self.assertEqual(len(self.recall('比较合成红茶')['memories']), 1)
        memory_path = self.app.memory_path(self.agent, 'demo', 'main')
        with closing(sqlite3.connect(self.app.registry.registry_path)) as db:
            scope = db.execute(
                "SELECT rowid FROM sources WHERE agent_id=? AND app_id=? AND channel=?",
                (self.agent, 'demo', 'main')).fetchone()[0]
        index_path = memory_path.with_name(f'recall_{scope}.sqlite')
        with closing(sqlite3.connect(index_path)) as db, db:
            db.execute('''CREATE TABLE memory_related_edges (
                card_a_id INTEGER, revision_a_id INTEGER,
                card_b_id INTEGER, revision_b_id INTEGER,
                relation TEXT, policy_version TEXT, weight REAL,
                shared_keywords_json TEXT, reason TEXT)''')
            db.execute("INSERT INTO memory_related_edges VALUES(?,?,?,?,?,?,?,?,?)",
                       (first['card_id'], first['current_revision_id'],
                        second['card_id'], second['current_revision_id'],
                        'related', 'memory_related_graph:v1', 0.9,
                        json.dumps(['合成红茶']), 'synthetic'))
        found = self.recall('比较合成红茶')
        self.assertEqual({m['id'] for m in found['memories']},
                         {first['card_id'], second['card_id']})
        self.assertIn('已记录关联候选', found['rendered'])
        graph = [m for m in found['memories'] if m.get('related_evidence')]
        self.assertEqual(len(graph), 1)
        self.assertEqual(graph[0]['related_evidence']['policy_version'],
                         'memory_related_graph:v1')
        self.assertLessEqual(len(found['rendered']), 700)
        self.assertEqual(len(self.recall(
            '比较合成红茶', visible_memory_ids=[second['card_id']])['memories']), 1)
        # 正式修订变动后旧图边版本失效，不能继续关联。
        self.app.revise_memory(self.agent, 'demo', second['card_id'],
                               '合成红茶：乙城修改后的方案',
                               second['current_revision_id'])
        self.assertNotIn('已记录关联候选', self.recall('比较合成红茶')['rendered'])

    def test_derived_relation_scope_and_stale_revision(self):
        first = self.card('root', '合成红茶和合成茶壶：主来源')
        self.app.grant_source(self.agent, 'other-app')
        self.app.grant_source(self.agent, 'demo', 'private')
        self.card('other-app', '合成红茶和合成茶壶：其他应用', app_id='other-app')
        self.card('private', '合成红茶和合成茶壶：私人频道', channel='private')
        self.card('other-agent', '合成红茶和合成茶壶：其他 Agent', agent=self.other)
        path = self.app.memory_path(self.agent, 'demo', 'main')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE cards SET keywords=? WHERE id=?",
                       ('合成红茶,合成茶壶', first['card_id']))
        self.assertNotIn('未保存关联图', self.recall('比较合成红茶')['rendered'])
        second = self.card('second', '合成红茶和合成茶壶：补充来源')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE cards SET keywords=? WHERE id=?",
                       ('合成红茶,合成茶壶', second['card_id']))
        self.assertIn('未保存关联图', self.recall('比较合成红茶')['rendered'])
        # 来源失效后，缓存/派生候选绝不能继续带出。
        self.insert('second', '', version=2, deleted=True)
        self.assertNotIn('未保存关联图', self.recall('比较合成红茶')['rendered'])

    def test_two_facts_keep_independent_sources_and_old_limit(self):
        first = self.card('old', '合成旅行以前选择甲城')
        second = self.card('new', '合成旅行后来选择乙城')
        self.card('unrelated', '我喜欢红茶')
        result = self.recall('合成旅行前后变化两件事？')
        self.assertEqual({m['id'] for m in result['memories']},
                         {first['card_id'], second['card_id']})
        self.assertIn('独立来源未合并', result['rendered'])
        self.assertIn('关联与冲突均未据此判定', result['rendered'])
        self.assertLessEqual(len(result['rendered']), 700)
        self.assertEqual(len(self.recall('合成旅行')['memories']), 1)
        self.assertEqual(len(self.recall('合成旅行前后变化',
                                        visible_memory_ids=[first['card_id']])['memories']), 1)
        self.assertEqual(len(self.recall('合成旅行两件事',
                                        visible_archive_ids=['old'])['memories']), 1)

    def test_two_facts_recheck_revision_after_single_rerank(self):
        self.card('old', '合成旅行以前选择甲城')
        second = self.card('new', '合成旅行后来选择乙城')
        calls = []
        def rerank(query, docs):
            calls.append(query)
            self.insert('new', '合成旅行来源已更正', version=2)
            return [1.] * len(docs)
        self.app.providers.local_models['rerank'] = ('fake', rerank)
        self.app.providers.configure('rerank', mode='local', model='fake')
        result = self.recall('合成旅行前后变化')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result['memories']), 1)
        self.assertNotIn(second['card_id'], [m['id'] for m in result['memories']])

    def test_temporal_hint_only_recorded_date_current_revision_and_budget(self):
        card = self.card('trip', '合成旅行选择甲城')
        path = self.app.memory_path(self.agent, 'demo', 'main')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE cards SET happened_at=? WHERE id=?',
                       ('2020-03-04', card['card_id']))
            db.execute('UPDATE card_revisions SET happened_at=? WHERE id=?',
                       ('2020-03-04', card['current_revision_id']))
        result = self.recall('合成旅行以前？')
        self.assertIn('事件日期：2020-03-04（不代表现状）', result['rendered'])
        self.assertIn(f"修订 {card['current_revision_id']}", result['rendered'])
        self.assertNotIn('事件日期', self.recall('合成旅行')['rendered'])
        self.assertNotIn('本卡有修订', result['rendered'])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE cards SET happened_at=NULL WHERE id=?', (card['card_id'],))
        self.assertNotIn('事件日期', self.recall('合成旅行后来修改？')['rendered'])
        self.assertLessEqual(len(result['rendered']), 700)
        self.app.revise_memory(self.agent, 'demo', card['card_id'],
                               '合成旅行选择乙城', card['current_revision_id'])
        result = self.recall('合成旅行以前后来修改？')
        self.assertEqual(len(result['memories']), 1)
        self.assertNotEqual(result['memories'][0]['revision_id'], card['current_revision_id'])
        self.assertIn('乙城', result['rendered'])
        self.assertNotIn('甲城', result['rendered'])
        self.assertNotIn('本卡有修订', result['rendered'])
        self.assertNotIn('事件日期', result['rendered'])

    def test_two_facts_budget_keeps_original_selection(self):
        self.card('a', '合成旅行甲城' + '山' * 310)
        self.card('b', '合成旅行乙城' + '海' * 310)
        self.insert('raw', '合成旅行原话' + '水' * 310)
        base = self.recall('合成旅行')
        result = self.recall('合成旅行两件事')
        self.assertEqual([m['id'] for m in result['memories']], [m['id'] for m in base['memories']])
        self.assertLessEqual(len(result['rendered']), 700)

    def test_short_reference_uses_verified_prior_without_extra_model_calls(self):
        tea = self.card("tea", "我喜欢乌龙茶，通常周末冲泡")
        self.card("flower", "我喜欢茉莉花茶，在春天购买")
        context = self.prior_context()
        embedder = Embedder()
        # 正交向量使不相关 Memory 无法靠合成高余弦闯过原有门槛。
        embedder.encode_one = lambda text: ([1., 0.] if "乌龙茶" in text else [0., 1.])
        self.app.providers.local_models["embedding"] = ("fake", embedder)
        self.app.providers.configure("embedding", mode="local", model="fake")
        calls = []
        def rerank(query, docs):
            calls.append((query, docs))
            return [1.] * len(docs)
        self.app.providers.local_models["rerank"] = ("fake", rerank)
        self.app.providers.configure("rerank", mode="local", model="fake")
        with patch("thalemora.app.providers.urlopen", side_effect=AssertionError("禁止联网")), \
                patch.object(embedder, "encode_one", wraps=embedder.encode_one) as encode:
            self.recall("乌龙茶")  # 预热已有索引
            calls.clear()
            encode.reset_mock()
            result = self.recall("第二个呢？", host_context=context)
            self.assertEqual(encode.call_count, 1)
            self.assertEqual(len(calls), 1)
        self.assertEqual([m['id'] for m in result['memories']], [tea['card_id']])
        self.assertEqual(result['archive'], [])
        self.assertNotIn("茉莉", result['rendered'])
        self.assertLessEqual(len(result['rendered']), 700)

    def test_short_reference_missing_prior_and_existing_recall_unchanged(self):
        self.card("tea", "我喜欢乌龙茶，通常周末冲泡")
        before = self.recall("乌龙茶")
        context = self.prior_context()
        self.assertEqual(self.recall("第二个呢？")['rendered'], "")
        self.assertEqual(self.recall("第二个呢？", host_context={}), self.recall("第二个呢？"))
        self.assertEqual(self.recall("乌龙茶", host_context=context), before)

    def test_short_reference_seen_memory_raw_and_prior_are_not_candidates(self):
        tea = self.card("tea", "我喜欢乌龙茶，通常周末冲泡")
        self.insert("raw", "上次的乌龙茶来自合成店铺")
        context = self.prior_context()
        result = self.recall("第二个呢？", host_context=context,
                             visible_memory_ids=[tea['card_id']], visible_archive_ids=["raw"])
        self.assertEqual(result['rendered'], "")
        self.assertEqual(self.recall("第二个呢？", host_context=context,
                                    visible_texts=["我喜欢乌龙茶，通常周末冲泡", "上次的乌龙茶来自合成店铺"])
                         ['rendered'], "")

    def test_short_reference_rejects_wrong_identity_window_version_or_text(self):
        self.card("tea", "我喜欢乌龙茶，通常周末冲泡")
        context = self.prior_context()
        baseline = self.recall("第二个呢？")
        for field, value in (("agent_id", self.other), ("app_id", "elsewhere"),
                             ("channel", "private"), ("version", 2), ("text", "猜测的乌龙茶")):
            with self.subTest(field=field):
                invalid = dict(context, previous_assistant_ref=dict(context['previous_assistant_ref'], **{field: value}))
                self.assertEqual(self.recall("第二个呢？", host_context=invalid), baseline)
        for field in ('conversation_id', 'current_epoch_id'):
            self.assertEqual(self.recall("第二个呢？", host_context=dict(context, **{field: 'other'})), baseline)
        path = self.app.archive(self.agent, "demo", "main").path
        for update in ("epoch_id=NULL", "epoch_id=''", "epoch_id='other'", "role='user'",
                       "deleted=1, text=NULL"):
            with self.subTest(update=update):
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute(f"UPDATE history_events SET {update} WHERE source_event_id='prior'")
                self.assertEqual(self.recall("第二个呢？", host_context=context), baseline)

    def test_short_reference_seen_quote_does_not_become_new_memory(self):
        context = self.prior_context()
        self.assertEqual(self.recall("第二个呢？", host_context=context)['rendered'], "")
        self.card("tea", "乌龙茶")
        self.assertEqual(self.recall("第二个呢？", host_context=context)['rendered'], "")

    def test_short_reference_prior_invalidated_during_rerank(self):
        self.card("tea", "我喜欢乌龙茶，通常周末冲泡")
        context = self.prior_context()
        def rerank(query, docs):
            event = self.event("prior", "更正后的上一答", version=2)
            event.update(role="assistant", epoch_id="epoch")
            self.app.import_history(self.agent, "demo", [event])
            return [1.] * len(docs)
        self.app.providers.local_models['rerank'] = ('fake', rerank)
        self.app.providers.configure('rerank', mode='local', model='fake')
        result = self.recall("第二个呢？", host_context=context)
        self.assertEqual(result['rendered'], "")
        self.assertEqual(result['memories'], [])
        self.assertEqual(result['archive'], [])

    def test_prior_options_denial_uses_topic_not_denied_choices(self):
        card = self.card("game", "我平常玩合成丙游戏")
        context = self.prior_context()
        event = self.event("prior", "你玩合成甲游戏还是合成乙游戏？", version=2)
        event.update(role='assistant', epoch_id='epoch')
        self.app.import_history(self.agent, "demo", [event])
        context['previous_assistant_ref'].update(version=2, text=event['text'])
        result = self.recall("我没玩过那两个游戏", host_context=context)
        self.assertEqual([m['id'] for m in result['memories']], [card['card_id']])
        self.assertNotIn("合成甲", result['rendered'])
        self.assertEqual(result['archive'], [])

    def test_short_reference_pure_decision_is_conservative(self):
        prior = "1、茉莉花茶\n2、乌龙茶"
        self.assertEqual(prior_assistant_query("第二个呢？", prior), "乌龙茶")
        for query, text in (("那个呢？", prior), ("第三个呢？", prior),
                            ("第二个呢？", "茉莉花茶或者乌龙茶"),
                            ("第二个呢？", "1、甲\n2、乙\n2、丙")):
            self.assertEqual(prior_assistant_query(query, text), query)
        self.assertEqual(prior_assistant_query("我没玩过那两个游戏", "你玩甲游戏还是乙游戏？"), "我平常玩什么游戏？")
        self.assertEqual(prior_assistant_query("我没玩过那两个游戏", "猜测的上文"), "我没玩过那两个游戏")

    def test_lexical_related_unrelated_low_information_and_visible(self):
        card = self.card("tea", "我喜欢乌龙茶")
        self.insert("raw", "上次买的乌龙茶来自合成茶店")
        with patch("thalemora.app.providers.urlopen", side_effect=AssertionError("禁止联网")):
            result = self.recall()
            self.assertIn("乌龙茶", result["rendered"])
            self.assertEqual(len(result["memories"]), 1)
            self.assertEqual(result["archive"][0]["id"], "raw")
            self.assertLessEqual(len(result["rendered"]), 700)
            with patch("thalemora.retrieval.recall.RetrievalIndex._replace_lane",
                       side_effect=AssertionError("unchanged turn must not rebuild index")):
                self.assertTrue(self.recall()["rendered"])
            for query in ("", " ", "好的", "嗯", "谢谢", "继续", "我想问一下", "讲讲二分查找"):
                self.assertEqual(self.recall(query)["rendered"], "", query)
            self.assertEqual(self.recall(visible_memory_ids=[card["card_id"]],
                                         visible_archive_ids=["raw"])["rendered"], "")
            self.assertEqual(self.recall(visible_archive_ids=["tea", "raw"])["rendered"], "")
            self.assertEqual(self.recall(visible_texts=["我喜欢乌龙茶", "上次买的乌龙茶来自合成茶店"])["rendered"], "")
            self.assertEqual(self.app.providers.audit(), [])

    def test_archive_neighbors_roles_and_visible_suppression(self):
        for event_id, text in (("before", "先把杯子摆好"), ("anchor", "我喜欢乌龙茶"),
                               ("after", "好的我去烧水")):
            self.insert(event_id, text)
        with closing(sqlite3.connect(self.app.archive(self.agent, "demo", "main").path)) as db, db:
            for event_id, role, minute in (("before", "assistant", "00"),
                                            ("anchor", "user", "01"), ("after", "assistant", "02")):
                db.execute("UPDATE history_events SET epoch_id='epoch', role=?, occurred_at=? "
                           "WHERE source_event_id=?", (role, f"2026-10-16T01:{minute}:00Z", event_id))
        result = self.recall()
        self.assertEqual([item["id"] for item in result["archive"]], ["anchor", "before", "after"])
        self.assertIn("[历史原话 anchor] 我喜欢乌龙茶", result["rendered"])
        self.assertIn("[原话 anchor (user) 的前文 before v1 (assistant)]", result["rendered"])
        self.assertIn("[原话 anchor (user) 的后文 after v1 (assistant)]", result["rendered"])
        hidden = self.recall(visible_archive_ids=["before"], visible_texts=["好的我去烧水"])
        self.assertEqual([item["id"] for item in hidden["archive"]], ["anchor"])
        self.assertNotIn("先把杯子摆好", hidden["rendered"])
        self.assertNotIn("好的我去烧水", hidden["rendered"])
        self.assertEqual(self.recall(visible_archive_ids=["anchor"])["rendered"], "")

    def test_archive_neighbor_boundaries_and_tombstone_position(self):
        for event_id, text in (("far", "更早的位置"), ("before", "先把杯子摆好"),
                               ("anchor", "我喜欢乌龙茶"), ("after", "好的我去烧水")):
            self.insert(event_id, text)
        path = self.app.archive(self.agent, "demo", "main").path
        # 每次恢复同一组合成窗口，只改变边界条件。
        for boundary in ("conversation", "epoch", "deleted", "duplicate", "empty"):
            with self.subTest(boundary=boundary):
                with closing(sqlite3.connect(path)) as db, db:
                    for event_id, minute in (("far", "00"), ("before", "01"),
                                              ("anchor", "02"), ("after", "03")):
                        db.execute("UPDATE history_events SET conversation_id='synthetic', epoch_id='epoch', "
                                   "deleted=0, text=?, occurred_at=? WHERE source_event_id=?",
                                   ({"far": "更早的位置", "before": "先把杯子摆好",
                                     "anchor": "我喜欢乌龙茶", "after": "好的我去烧水"}[event_id],
                                    f"2026-10-16T01:{minute}:00Z", event_id))
                    if boundary == "conversation":
                        db.execute("UPDATE history_events SET conversation_id='other' WHERE source_event_id!='anchor'")
                    elif boundary == "epoch":
                        db.execute("UPDATE history_events SET epoch_id='other' WHERE source_event_id!='anchor'")
                    elif boundary == "deleted":
                        db.execute("UPDATE history_events SET deleted=1, text=NULL "
                                   "WHERE source_event_id IN ('before','after')")
                    elif boundary == "duplicate":
                        db.execute("UPDATE history_events SET occurred_at='2026-10-16T01:02:00Z' "
                                   "WHERE source_event_id='before'")
                    else:
                        db.execute("UPDATE history_events SET epoch_id='' ")
                result = self.recall()
                self.assertEqual([item["id"] for item in result["archive"]], ["anchor"])
                self.assertIn("我喜欢乌龙茶", result["rendered"])

    def test_archive_neighbor_time_gap_and_render_budget(self):
        for event_id, text in (("before", "先把杯子摆好"), ("anchor", "我喜欢乌龙茶"),
                               ("after", "好的我去烧水")):
            self.insert(event_id, text)
        path = self.app.archive(self.agent, "demo", "main").path
        with closing(sqlite3.connect(path)) as db, db:
            for event_id, instant in (("before", "2026-10-15T23:59:00Z"),
                                       ("anchor", "2026-10-16T01:00:00Z"),
                                       ("after", "2026-10-16T01:30:00Z")):
                db.execute("UPDATE history_events SET epoch_id='epoch', occurred_at=? "
                           "WHERE source_event_id=?", (instant, event_id))
        result = self.recall()
        self.assertEqual([item["id"] for item in result["archive"]], ["anchor", "after"])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE history_events SET occurred_at='2026-10-16T01:30:01Z' "
                       "WHERE source_event_id='after'")
        self.assertEqual([item["id"] for item in self.recall()["archive"]], ["anchor"])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE history_events SET occurred_at='2026-10-16T00:59:00Z', text=? "
                       "WHERE source_event_id='before'", ("杯" * 400,))
            db.execute("UPDATE history_events SET occurred_at='2026-10-16T01:01:00Z', text=? "
                       "WHERE source_event_id='after'", ("水" * 400,))
        result = self.recall()
        self.assertLessEqual(len(result["rendered"]), 700)
        self.assertEqual([item["id"] for item in result["archive"]], ["anchor", "before"])
        self.assertIn("杯" * 400, result["rendered"])
        self.assertNotIn("水" * 400, result["rendered"])

    def test_revision_refresh_tombstone_and_lineage(self):
        card = self.card("home", "我住甲城")
        self.assertIn("甲城", self.recall("我现在住哪？")["rendered"])
        self.app.revise_memory(self.agent, "demo", card["card_id"], "我住乙城", card["current_revision_id"])
        result = self.recall("我现在住哪？")
        self.assertIn("乙城", result["rendered"])
        self.assertNotIn("甲城", result["rendered"])
        path = self.app.memory_path(self.agent, "demo", "main")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE cards SET status='trashed' WHERE id=?", (card["card_id"],))
        self.assertEqual(self.recall("我现在住哪？")["rendered"], "")
        other = self.card("home2", "我住丙城")
        target = self.card("replacement", "合成占位")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO memory_card_lineage(from_card_id,to_card_id,from_revision_id,to_revision_id,"
                       "relation_type,origin,created_at) VALUES(?,?,?,?,'superseded_by','synthetic','2026-10-16')",
                       (other["card_id"], target["card_id"], other["current_revision_id"], target["current_revision_id"]))
        self.assertEqual(self.recall("我现在住哪？")["rendered"], "")

    def test_agent_app_channel_scope_before_model_and_empty_authorization(self):
        self.card("a", "我喜欢乌龙茶")
        self.card("b", "其他 Agent 喜欢红茶", agent=self.other)
        self.app.grant_source(self.agent, "other-app")
        self.card("x", "别的来源喜欢秘密茶", app_id="other-app")
        self.app.grant_source(self.agent, "demo", "private")
        self.card("p", "私密通道喜欢隐藏茶", channel="private")
        embedder = Embedder()
        self.app.providers.local_models["embedding"] = ("fake", embedder)
        self.app.providers.configure("embedding", mode="local", model="fake")
        text = self.recall()["rendered"]
        self.assertIn("乌龙茶", text)
        self.assertNotIn("秘密", text)
        self.assertNotIn("隐藏", text)
        self.assertFalse(any("秘密" in t or "隐藏" in t or "红茶" in t for t in embedder.inputs))
        with self.assertRaises(AccessDenied):
            self.app.recall_for_turn(self.other, "demo", "", "private")
        with self.assertRaises(AccessDenied):
            self.app.recall_for_turn(self.agent, "ungranted", "")

    def test_independent_local_modes_model_change_and_disable(self):
        self.card("tea", "我喜欢乌龙茶")
        self.insert("raw", "乌龙茶在合成店铺有售")
        embedder = Embedder()
        calls = []
        def rerank(query, docs):
            calls.append((query, docs))
            return list(range(len(docs)))
        local = Application(self.root, embedder=("e1", embedder), reranker=("r1", rerank))
        local.providers.configure("embedding", mode="local", model="e1")
        local.providers.configure("rerank", mode="local", model="r1")
        result = local.recall_for_turn(self.agent, "demo", "喜欢什么茶？")
        self.assertTrue(result["rendered"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(local.providers.status("embedding")["state"], "ready")
        self.assertEqual(local.providers.status("rerank")["state"], "ready")
        first_count = len(embedder.inputs)
        self.assertEqual(local.recall_for_turn(self.agent, "demo", "我想问一下")["rendered"], "")
        self.assertEqual(len(embedder.inputs), first_count)
        local.recall_for_turn(self.agent, "demo", "喜欢什么茶？")
        self.assertEqual(len(embedder.inputs), first_count + 1)  # 只新增 query，复用卡片向量
        newer = Embedder(dim=3)
        local.providers.local_models["embedding"] = ("e2", newer)
        local.providers.configure("embedding", mode="local", model="e2")
        local.recall_for_turn(self.agent, "demo", "喜欢什么茶？")
        self.assertTrue(any("乌龙茶" in t for t in newer.inputs))
        with closing(sqlite3.connect(local.memory_path(self.agent, "demo", "main").with_name("recall_1.sqlite"))) as db:
            self.assertEqual(db.execute("SELECT DISTINCT vector_dim FROM chunks").fetchall(), [(3,)])
        local.providers.configure("embedding", mode="disabled", model="e2")
        before = len(newer.inputs)
        self.assertTrue(local.recall_for_turn(self.agent, "demo", "乌龙茶")["rendered"])
        self.assertEqual(len(newer.inputs), before)
        self.assertEqual(local.providers.status("embedding")["state"], "disabled")
        restarted = Application(self.root)
        self.assertEqual(restarted.providers.status("embedding")["config"]["mode"], "disabled")
        local.providers.configure("rerank", mode="disabled", model="r1")
        count = len(calls)
        local.recall_for_turn(self.agent, "demo", "乌龙茶")
        self.assertEqual(len(calls), count)

    def test_api_embedding_rerank_endpoints_budget_and_errors(self):
        self.card("tea", "我喜欢乌龙茶")
        requests = []
        def urlopen(request, timeout):
            body = json.loads(request.data)
            requests.append((request.full_url, body))
            if request.full_url.endswith("/embeddings"):
                output = {"data": [{"embedding": [1.0, 0.0]}]}
            else:
                self.assertTrue(request.full_url.endswith("/rerank"))
                output = {"results": [{"index": i, "relevance_score": 0.9} for i in range(len(body["documents"]))]}
            output["usage"] = {"total_tokens": 8}
            return io.BytesIO(json.dumps(output).encode())
        with patch.dict(os.environ, {"P8_FAKE_KEY": "synthetic-only"}), patch("thalemora.app.providers.urlopen", urlopen):
            for module in ("embedding", "rerank"):
                self.app.providers.configure(module, mode="api", model="fake", base_url="https://synthetic.invalid/v1",
                                             key_env="P8_FAKE_KEY", call_budget=10, token_budget=1000)
            result = self.recall()
            self.assertTrue(result["rendered"])
            self.assertEqual(len(requests), 3)  # 卡片、query、rerank
            self.assertEqual([r[0].split("/")[-1] for r in requests], ["embeddings", "embeddings", "rerank"])
            self.assertEqual(self.app.providers.status("embedding")["known_tokens"], 16)
            self.app.providers.configure("rerank", mode="api", model="fake", base_url="https://synthetic.invalid/v1",
                                         key_env="P8_FAKE_KEY", call_budget=1, token_budget=1000)
            result = self.recall()
            self.assertTrue(result["errors"])
            self.assertTrue(result["rendered"])
            self.assertEqual(self.app.providers.status("rerank")["state"], "budget_exceeded")
            self.app.providers.configure("embedding", mode="disabled")
            self.app.providers.configure("rerank", mode="api", model="fake", base_url="https://synthetic.invalid/v1",
                                         key_env="P8_FAKE_KEY", call_budget=10, token_budget=1000)
            with patch("thalemora.app.providers.urlopen", side_effect=TimeoutError("synthetic timeout")):
                result = self.recall()
            self.assertTrue(result["errors"])
            self.assertEqual(self.app.providers.status("rerank")["state"], "error")
            self.assertEqual(self.app.providers.audit()[0]["status"], "error")
            with patch("thalemora.app.providers.urlopen", return_value=io.BytesIO(b'{"results":[]}')):
                self.assertTrue(self.recall()["errors"])

    def test_unconfigured_local_and_duplicate_text(self):
        self.card("tea", "我喜欢乌龙茶")
        self.insert("duplicate", "我喜欢乌龙茶")
        result = self.recall()
        self.assertEqual(len(result["memories"]) + len(result["archive"]), 1)
        self.assertEqual(self.app.providers.configure("embedding", mode="local", model="missing-local-model")["state"], "unconfigured")
        unavailable = self.recall()
        self.assertTrue(unavailable["rendered"])
        self.assertEqual(unavailable["errors"][0]["code"], "embedding_unavailable")
        self.app.providers.configure("embedding", mode="disabled")
        def broken(query, docs):
            raise RuntimeError("synthetic local failure")
        self.app.providers.local_models["rerank"] = ("broken", broken)
        self.app.providers.configure("rerank", mode="local", model="broken")
        result = self.recall()
        self.assertTrue(result["rendered"])
        self.assertTrue(result["errors"])
        self.assertEqual(self.app.providers.status("rerank")["state"], "error")


if __name__ == "__main__":
    unittest.main()
