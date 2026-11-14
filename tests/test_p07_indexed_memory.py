"""后台旧索引只读召回：合成数据、词法模式、禁止网络。"""
import json
import sqlite3
import unittest
from contextlib import closing, contextmanager
from unittest.mock import patch

import test_p07_legacy_memory as fixture
from thalemora.adapters.niannian_vnext import NiannianReadOnlyRecall
from thalemora.retrieval.core import RetrievalIndex, RetrievalShadowError, _ro


class IndexedMemoryTests(unittest.TestCase):
    setUp = fixture.LegacyMemoryTests.setUp
    card = fixture.LegacyMemoryTests.card
    event = fixture.LegacyMemoryTests.event
    recall = fixture.LegacyMemoryTests.recall

    def indexed(self):
        path = self.root / 'vnext_retrieval.sqlite'
        writer = RetrievalIndex(str(path), memory_db_path=str(self.memory),
                               embedding_model='synthetic', embedder=None)
        writer.rebuild_memory()
        self.app = NiannianReadOnlyRecall(binding=self.binding, owner=self.owner,
            agent_id='chenlin', app_id='niannian', memory_path=self.memory,
            host_index_path=path, providers=self.providers)
        return writer

    def test_weak_lexical_overlap_is_not_a_memory_match(self):
        from thalemora.retrieval.recall import query_terms
        self.card()
        self.indexed()
        query = '青岚工程和晴朗天气以及假想城市的讨论'
        terms = query_terms(query)
        self.assertLess(sum(t in '青岚工程只能在预览分支修改' for t in terms) / len(terms), .5)
        self.assertEqual([], self.app.recall_for_turn('chenlin', 'niannian', query)['memories'])
        self.assertEqual(1, len(self.recall()['memories']))

    def test_host_raw_history_rejects_single_generic_term_match(self):
        # 合成复现：问广告投放，却误召回只包含「平台」的旧账号申诉。
        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id='canonical:appeal',
            version=1, role='assistant', text='此前在平台提交账号申诉并等待复核',
        )])
        self.indexed()
        off_topic = self.app.recall_for_turn('chenlin', 'niannian', '之前 投流 广告位 平台')
        self.assertEqual([], off_topic['archive'])
        self.assertNotIn('账号申诉', off_topic['rendered'])
        on_topic = self.app.recall_for_turn('chenlin', 'niannian', '上次 账号 申诉')
        self.assertEqual([a['id'] for a in on_topic['archive']], ['canonical:appeal'])
        self.assertEqual([], self.providers.audit())

    def test_host_long_raw_history_rejects_tail_only_match(self):
        text = '这是合成的无关旧记录。' * 30 + '账号 申诉 平台'
        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id='canonical:tail',
            version=1, role='assistant', text=text,
        )])
        self.indexed()
        result = self.app.recall_for_turn('chenlin', 'niannian', '之前 账号 申诉 平台')
        self.assertEqual([], result['archive'])
        self.assertEqual('', result['rendered'])
        self.assertEqual([], self.providers.audit())

    def test_host_long_raw_history_keeps_relevant_prefix_and_full_source(self):
        text = '账号申诉正在等待复核。' + '这是合成的旧记录。' * 40 + '尾部核验标记'
        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id='canonical:prefix',
            version=1, role='assistant', text=text,
        )])
        self.indexed()
        result = self.app.recall_for_turn('chenlin', 'niannian', '之前 账号 申诉 平台')
        self.assertEqual(['canonical:prefix'], [a['id'] for a in result['archive']])
        self.assertEqual(text, result['archive'][0]['text'])
        self.assertEqual(2 / 3, result['archive'][0]['score'])
        self.assertIn(text[:300], result['rendered'])
        self.assertNotIn('尾部核验标记', result['rendered'])
        self.assertEqual([], self.providers.audit())

    def test_host_long_raw_history_relevant_prefix_wins_over_tail_match(self):
        tail = '平台上的无关旧记录。' * 30 + '账号 申诉'
        prefix = '账号申诉正在等待复核。' + '这是合成的旧记录。' * 40
        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id=event_id,
            version=1, role='assistant', text=text,
        ) for event_id, text in (('canonical:tail', tail), ('canonical:prefix', prefix))])
        self.indexed()
        result = self.app.recall_for_turn('chenlin', 'niannian', '之前 账号 申诉 平台')
        self.assertEqual(['canonical:prefix'], [a['id'] for a in result['archive']])
        self.assertEqual(2 / 3, result['archive'][0]['score'])
        self.assertIn(prefix[:300], result['rendered'])
        self.assertNotIn('无关旧记录', result['rendered'])
        self.assertEqual([], self.providers.audit())

    def test_host_normal_statements_skip_archive_even_with_exact_overlap(self):
        queries = ('我讨厌你', '十一点你妈', '账号 申诉', '今天 心情 时间',
                   '明明同窗口才提过的事，下一句又忘了', '你刚才说过的全忘了吗',
                   '昨天聊得挺开心', '昨天聊过的事情我还不想管')
        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id=f'old:{i}',
            version=1, role='assistant', text=query,
        ) for i, query in enumerate(queries)])
        self.indexed()
        with patch.object(self.app, 'search', wraps=self.app.search) as search:
            for query in queries:
                with self.subTest(query=query):
                    result = self.app.recall_for_turn('chenlin', 'niannian', query)
                    self.assertEqual([], result['archive'])
                    self.assertEqual('', result['rendered'])
            search.assert_not_called()

    def test_host_explicit_history_keeps_archive(self):
        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id='old:rule',
            version=1, role='assistant', text='青岚工程原话说过提过的约定是仅能预览',
        )])
        self.indexed()
        for query in ('上次青岚工程的约定是什么？', '青岚工程原话是什么？',
                      '青岚工程说过什么？', '青岚工程提过什么？',
                      '昨天聊的青岚工程约定是什么？', '刚才谈的青岚工程约定呢？'):
            with self.subTest(query=query):
                result = self.app.recall_for_turn('chenlin', 'niannian', query)
                self.assertEqual(['old:rule'], [a['id'] for a in result['archive']])

    def test_host_normal_keeps_memory_card_while_blocking_archive(self):
        card = self.card(text='青岚工程只能在预览分支修改')
        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id='old:rule',
            version=1, role='assistant', text='青岚工程原始约定',
        )])
        self.indexed()
        result = self.recall()
        self.assertEqual([card['card_id']], [m['id'] for m in result['memories']])
        self.assertEqual([], result['archive'])

    def test_host_intent_archive_does_not_require_history_cue(self):
        from thalemora.adapters.niannian_vnext import recall_vnext

        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id='old:rule',
            version=1, role='assistant', text='青岚工程原始约定 edit_file',
        )])
        self.indexed()
        for surface, request in (
            ('contact', dict(open_loops=['青岚工程'])),
            ('activity', dict(recent_activity=['青岚工程'])),
            ('find_activity', dict(recent_activity=['青岚工程'])),
            ('preaction', dict(tool_name='edit_file', arguments={'project': '青岚工程'})),
        ):
            with self.subTest(surface=surface):
                result = recall_vnext(self.app, 'chenlin', 'niannian', request,
                    surface=surface, raw_to_archive={},
                    preaction_context={'needs_precheck': True})
                self.assertEqual(['old:rule'], [a['id'] for a in result['trace']['archive']])

    def test_host_local_vectors_reuse_existing_model_identity(self):
        import numpy as np

        class SyntheticEmbedding:
            def encode_one(self, text):
                return np.asarray([1.0, 0.0], dtype=np.float32)

            def encode_many(self, texts, *, batch_size=16):
                return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)

        card = self.card()
        embedder = SyntheticEmbedding()
        self.providers.configure('embedding', mode='local', model='synthetic')
        self.providers.local_models['embedding'] = ('synthetic', embedder)
        path = self.root / 'vnext_retrieval.sqlite'
        writer = RetrievalIndex(str(path), memory_db_path=str(self.memory),
                                embedding_model='synthetic', embedder=embedder)
        writer.rebuild_memory()
        self.app = NiannianReadOnlyRecall(
            binding=self.binding, owner=self.owner, agent_id='chenlin',
            app_id='niannian', memory_path=self.memory,
            host_index_path=path, providers=self.providers)
        result = self.recall()
        self.assertEqual([row['id'] for row in result['memories']], [card['card_id']])
        self.assertFalse(result['memories'][0]['verified'])
        self.assertEqual(self.providers.audit(), [])

    def test_host_ambient_uses_verified_cards_and_existing_reflection_signals(self):
        from datetime import datetime, timezone
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from thalemora.lifecycle import AmbientEngine

        card = self.card()
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.execute('UPDATE cards SET importance=10,keywords=? WHERE id=?',
                       ('青岚', card['card_id']))
        self.indexed()
        engine = AmbientEngine(self.root / 'ambient.sqlite')
        now = datetime(2026, 11, 1, tzinfo=timezone.utc).isoformat()
        args = dict(candidate_ids=[card['card_id']], epoch='test-epoch',
                    now=now, context_text='突然想起青岚', enabled=True, seed=20,
                    reflective_bias_by_card_id={card['card_id']: .07},
                    reflective_affinity_by_card_id={card['card_id']: .9})
        selected = self.app.ambient_for_turn(engine, generation_key='first', **args)
        self.assertEqual(card['card_id'], selected['ambient']['card_id'])
        self.assertIn('旧记忆候选', selected['rendered'])
        self.assertEqual(selected, self.app.ambient_for_turn(
            engine, generation_key='first', **args))
        quiet = self.app.ambient_for_turn(
            engine, generation_key='quiet', related_card_ids=[card['card_id']], **args)
        self.assertFalse(quiet['ambient'])
        self.assertEqual('', quiet['rendered'])
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.execute("UPDATE cards SET status='trashed' WHERE id=?", (card['card_id'],))
        self.assertEqual('', self.app.ambient_for_turn(
            engine, generation_key='after-delete', **args)['rendered'])
        self.assertEqual(self.providers.audit(), [])

    def test_vnext_host_receives_ambient_and_reflection_without_extra_model(self):
        from datetime import datetime, timezone
        from thalemora.lifecycle import AmbientEngine
        from thalemora.adapters.niannian_vnext import recall_vnext

        card = self.card()
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.execute("UPDATE cards SET importance=10,keywords='蓝天' WHERE id=?",
                       (card['card_id'],))
        self.indexed()
        engine = AmbientEngine(self.root / 'host_ambient.sqlite')
        host = dict(engine=engine, candidate_ids=[card['card_id']],
                    reflective_bias_by_card_id={card['card_id']: .07},
                    reflective_affinity_by_card_id={card['card_id']: .9},
                    now=datetime(2026, 11, 1, tzinfo=timezone.utc).isoformat(),
                    seed=20, enabled=True)
        request = dict(user_text='今天突然想起蓝天了', generation_key='ambient-1',
                       current_epoch_id='epoch-1')
        result = recall_vnext(self.app, 'chenlin', 'niannian', request, ambient_state=host)
        self.assertEqual(card['card_id'], result['trace']['ambient']['card_id'])
        self.assertEqual(card['current_revision_id'],
                         result['trace']['ambient']['revision_id'])
        self.assertIn('偶然想起的旧记忆候选', result['rendered'])
        self.assertFalse(result['trace']['related']['memory_refs'])
        self.assertEqual(result, recall_vnext(
            self.app, 'chenlin', 'niannian', request, ambient_state=host))
        quiet = recall_vnext(self.app, 'chenlin', 'niannian',
                            dict(request, generation_key='ambient-quiet',
                                 user_text='先检查数据库的错误'),
                            ambient_state=host)
        self.assertFalse(quiet['trace']['ambient'].get('card_id'))
        self.assertEqual('', quiet['rendered'])
        self.assertEqual([], self.providers.audit())

    def test_ambient_cannot_restore_visible_raw_or_previous_sources_on_replay(self):
        from thalemora.adapters.niannian_vnext import recall_vnext
        from thalemora.lifecycle import AmbientEngine

        for case in ('legacy_visible', 'verified_visible', 'legacy_previous'):
            with self.subTest(case=case):
                self.setUp()
                self.archive.import_history([dict(app='niannian', channel='main',
                    source_event_id='canonical:one', version=1, raw_event_id=72,
                    role='assistant' if case == 'legacy_previous' else 'user',
                    text='合成可见来源', conversation_id='conv', epoch_id='epoch')])
                ref = dict(agent_id='chenlin', app_id='niannian', channel='main',
                           source_event_id='canonical:one', version=1)
                card = (self.card('derived', json.dumps(dict(ref, app='niannian', kind='archive_quote')))
                        if case == 'verified_visible' else self.card())
                with closing(sqlite3.connect(self.memory)) as db, db:
                    db.execute("UPDATE cards SET importance=10,keywords='蓝天' WHERE id=?",
                               (card['card_id'],))
                self.indexed()
                host = dict(engine=AmbientEngine(self.root / 'visible_ambient.sqlite'),
                    candidate_ids=[card['card_id']], enabled=True, seed=20,
                    now='2026-11-01T00:00:00+00:00')
                request = dict(user_text='今天突然想起蓝天了', generation_key='first',
                               current_epoch_id='epoch')
                self.assertIn('偶然想起', recall_vnext(
                    self.app, 'chenlin', 'niannian', request, ambient_state=host)['rendered'])
                kwargs = dict(raw_to_archive={72: ref})
                if case == 'legacy_previous':
                    request['previous_assistant_event_id'] = 'previous'
                    kwargs.update(raw_to_archive={'previous': ref},
                                  previous_assistant_ref=dict(ref, text='合成可见来源'),
                                  conversation_id='conv')
                else:
                    request['visible_raw_event_ids'] = [72]
                for key in ('first', 'next'):
                    result = recall_vnext(self.app, 'chenlin', 'niannian',
                        dict(request, generation_key=key), ambient_state=host, **kwargs)
                    self.assertEqual('', result['rendered'])
                    self.assertFalse(result['trace']['ambient'].get('card_id'))
                    self.assertEqual([], result['trace']['resonance'])
                self.assertEqual([], self.providers.audit())

    def test_proactive_echo_trace_only_references_rendered_echo_and_rechecks_deletion(self):
        from thalemora.adapters.niannian_vnext import recall_vnext
        from thalemora.lifecycle import AmbientEngine

        cards = [self.card(text='合成第一段旧事'), self.card(text='合成第二段旧事')]
        with closing(sqlite3.connect(self.memory)) as db, db:
            for card, keyword in zip(cards, ('蓝天', '月光')):
                db.execute('UPDATE cards SET importance=10,keywords=? WHERE id=?',
                           (keyword, card['card_id']))
        self.indexed()
        engine = AmbientEngine(self.root / 'echo_ambient.sqlite')
        args = dict(candidate_ids=[c['card_id'] for c in cards], enabled=True, seed=20,
                    epoch='epoch', now='2026-11-01T00:00:00+00:00', mode='proactive')
        for key, keyword in (('first', '蓝天'), ('second', '月光')):
            self.assertIsNotNone(self.app.ambient_for_turn(
                engine, generation_key=key, context_text=keyword, **args)['ambient'])
        host = dict(engine=engine, **{k: v for k, v in args.items() if k not in ('epoch', 'mode')})
        request = dict(now_text='此刻很平静', generation_key='echo', current_epoch_id='epoch')
        result = recall_vnext(self.app, 'chenlin', 'niannian', request,
                              surface='contact', ambient_state=host)
        self.assertEqual(1, len(result['trace']['resonance']))
        selected = result['trace']['resonance'][0]
        texts = {c['card_id']: text for c, text in zip(cards, ('合成第一段旧事', '合成第二段旧事'))}
        self.assertIn(texts[selected['card_id']], result['rendered'])
        self.assertEqual(result, recall_vnext(self.app, 'chenlin', 'niannian', request,
                                             surface='contact', ambient_state=host))
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.execute("UPDATE cards SET status='trashed'")
        replay = recall_vnext(self.app, 'chenlin', 'niannian', request,
                              surface='contact', ambient_state=host)
        self.assertEqual('', replay['rendered'])
        self.assertEqual([], replay['trace']['resonance'])
        self.assertEqual([], self.providers.audit())

    def test_bounded_candidates_read_only_wal_and_wrong_source(self):
        for i in range(80):
            self.card(text=f'青岚工程第{i}项合成约定')
        writer = self.indexed()
        # 保持 WAL 未 checkpoint；查询必须读到后台刚刷新的记录。
        with closing(sqlite3.connect(writer.db_path)) as live:
            live.execute('SELECT count(*) FROM chunks').fetchone()
            card = self.card(text='独有锚点来自后台刚更新')
            with closing(sqlite3.connect(self.memory)) as db, db:
                db.execute("UPDATE cards SET status='sunk' WHERE id=?", (card['card_id'],))
            writer.refresh_memory_card(card['card_id'])
            self.archive.import_history([dict(app='niannian', channel='main', source_event_id='previous',
                version=1, role='assistant', text='可信上一条助手', conversation_id='conv', epoch_id='epoch',
                raw_event_id=72)])
            before = {p: p.read_bytes() for p in (self.memory, self.archive.path,
                      self.app.index_path, self.app.index_path.with_name(self.app.index_path.name + '-wal'))}
            statements = []

            @contextmanager
            def traced_ro(path):
                with _ro(path) as db:
                    db.set_trace_callback(statements.append)
                    yield db

            with patch('thalemora.retrieval.core._ro', traced_ro), patch.object(
                    self.app, '_rows', side_effect=AssertionError('不得整库读取')), patch.object(
                    RetrievalIndex, '_initialize', side_effect=AssertionError('不得初始化')), patch.object(
                    RetrievalIndex, '_replace_lane', side_effect=AssertionError('不得更新')), patch(
                    'thalemora.retrieval.recall.ensure_current_index', side_effect=AssertionError('不得重建检查')):
                result = self.recall()
                self.assertTrue(result['memories'])
                self.assertFalse(result['memories'][0]['verified'])
                evidence_queries = [s for s in statements if s.startswith('SELECT e.*,r.card_id')]
                self.assertEqual(len(evidence_queries), 2)  # 排序前/后各一次
                self.assertTrue(all('WHERE r.card_id IN (' in s for s in evidence_queries))
                self.assertTrue(all(len(s.split(' IN (')[1].split(')')[0].split(',')) <= 24
                                    for s in evidence_queries))
                found = self.app.recall_for_turn('chenlin', 'niannian', '独有锚点')
                self.assertEqual(found['memories'][0]['id'], card['card_id'])
                self.assertFalse(self.recall(visible_memory_ids=list(range(1, 100)))['memories'])
                self.assertFalse(self.recall(visible_archive_ids=['missing'])['memories'])
                self.assertFalse(self.recall(visible_archive_ids=['previous'])['memories'])
                self.assertFalse(self.recall(host_context=dict(conversation_id='conv', current_epoch_id='epoch',
                    previous_assistant_ref=dict(agent_id='chenlin', app_id='niannian', channel='main',
                        source_event_id='previous', version=1, text='可信上一条助手')))['memories'])
                reader = RetrievalIndex.open_read_only(writer.db_path, memory_db_path=str(self.memory),
                                                       embedding_model='unavailable', embedder=None)
                with reader._connect() as db, self.assertRaises(sqlite3.OperationalError):
                    db.execute('DELETE FROM chunks')
            for p, content in before.items():
                self.assertEqual(p.read_bytes(), content, p.name)
            with live:
                live.execute("UPDATE build_receipts SET source_path='/wrong/memory.db'")
            with self.assertRaises(RetrievalShadowError):
                self.recall()
        self.assertEqual(self.providers.audit(), [])

    def test_candidate_authority_and_revalidation_after_rerank(self):
        self.providers.configure('rerank', mode='local', model='synthetic')
        for change in ('revision', 'body', 'status', 'kind', 'retired', 'evidence_scope',
                       'source_version', 'source_deleted', 'visible_raw', 'previous'):
            with self.subTest(change=change):
                with closing(sqlite3.connect(self.memory)) as db, db:
                    db.execute("UPDATE cards SET status='trashed'")
                self.event('canonical:one', raw=900, version=1)
                card = self.card()
                if change in {'source_version', 'source_deleted'}:
                    with closing(sqlite3.connect(self.memory)) as db, db:
                        db.execute('UPDATE card_evidence SET source_kind=?,source_ref=? WHERE revision_id=?',
                            ('derived', json.dumps(dict(agent_id='chenlin', app='niannian', channel='main',
                                source_event_id='canonical:one', version=1, kind='archive_quote')),
                             card['current_revision_id']))
                if change == 'retired':
                    with closing(sqlite3.connect(self.memory)) as db, db:
                        db.execute('CREATE TABLE memory_card_lineage(from_card_id INTEGER,to_card_id INTEGER,relation TEXT,retired_at TEXT)')
                self.indexed()
                visible = {}
                if change == 'visible_raw':
                    visible['visible_archive_ids'] = ['canonical:one']
                if change == 'previous':
                    self.archive.import_history([dict(app='niannian', channel='main',
                        source_event_id='previous', version=1, role='assistant', text='可信上一条助手',
                        conversation_id='conv', epoch_id='epoch', raw_event_id=901)])
                    visible['host_context'] = dict(conversation_id='conv', current_epoch_id='epoch',
                        previous_assistant_ref=dict(agent_id='chenlin', app_id='niannian', channel='main',
                            source_event_id='previous', version=1, text='可信上一条助手'))

                def rerank(query, texts):
                    if change in {'source_version', 'source_deleted'}:
                        self.event('canonical:one', version=2, deleted=change == 'source_deleted')
                    elif change in {'visible_raw', 'previous'}:
                        with closing(sqlite3.connect(self.archive.path)) as db, db:
                            if change == 'visible_raw':
                                db.execute("UPDATE history_events SET raw_event_id=72 WHERE source_event_id='canonical:one'")
                            else:
                                db.execute("UPDATE history_events SET deleted=1,text=NULL WHERE source_event_id='previous'")
                    else:
                        with closing(sqlite3.connect(self.memory)) as db, db:
                            if change == 'evidence_scope':
                                db.execute('UPDATE card_evidence SET source_ref=? WHERE revision_id=?',
                                    (json.dumps(dict(channel='private')), card['current_revision_id']))
                            elif change == 'retired':
                                db.execute('INSERT INTO memory_card_lineage(from_card_id,to_card_id,relation) VALUES(?,?,?)',
                                           (card['card_id'], card['card_id'], 'merged_into'))
                            else:
                                column, value = {'revision': ('current_revision_id', 999),
                                    'body': ('content', '偷偷改正文'), 'status': ('status', 'trashed'),
                                    'kind': ('memory_kind', 'timeline')}[change]
                                db.execute(f'UPDATE cards SET {column}=? WHERE id=?', (value, card['card_id']))
                    return [1.0] * len(texts)

                self.providers.local_models['rerank'] = ('synthetic', rerank)
                result = self.recall(**visible)
                self.assertTrue(all(e.get('module') == 'embedding' for e in result['errors']))
                self.assertFalse(result['memories'])
                # 过期索引仍保持原修订，且下一轮不能悄悄修复/降级整库召回。
                if change != 'previous':
                    self.assertFalse(self.recall(**visible)['memories'])


if __name__ == '__main__':
    unittest.main()
