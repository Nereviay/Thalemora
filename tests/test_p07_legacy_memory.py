"""P07 旧单 Agent Memory 只读接线；所有库和原话均为合成数据。"""
import json
import sqlite3
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from thalemora.adapters.niannian_history import NiannianHistoryBinding
from thalemora.adapters.niannian_vnext import NiannianReadOnlyRecall, recall_vnext
from thalemora.app import Application
from thalemora.app.providers import Providers
from thalemora.history import Archive
from thalemora.identity import AccessDenied, OwnerPrincipal
from thalemora.memory import schema
from thalemora.memory.revision_store import create_card_in_transaction, update_card_in_transaction
from thalemora.memory.scopes import ownership
from thalemora.retrieval.recall import scoped_rows


class LegacyMemoryTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory(prefix='p07-legacy-synthetic-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.memory = self.root / 'memory.db'
        with closing(sqlite3.connect(self.memory)) as db:
            schema.initialize(db)
            db.execute('DROP TABLE archive_card_scopes')
            db.commit()
        self.owner = OwnerPrincipal('niannian-host')
        self.binding = NiannianHistoryBinding(archive_path=self.root / 'thalemora_history.sqlite',
            owner=self.owner, agent_id='chenlin', apps=['niannian', 'other'])
        self.archive = Archive(self.binding, self.owner, 'chenlin')
        Application._lexical_index(self.archive.path)
        self.archive.ensure_short_index()
        self.providers = Providers(self.root)
        self.app = NiannianReadOnlyRecall(binding=self.binding, owner=self.owner,
            agent_id='chenlin', app_id='niannian', memory_path=self.memory,
            index_path=self.root / 'recall_legacy.sqlite', providers=self.providers)
        net = patch('socket.socket.connect', side_effect=AssertionError('禁止联网/付费'))
        net.start()
        self.addCleanup(net.stop)

    def card(self, kind='legacy_unverified', ref=None, text='青岚工程只能在预览分支修改'):
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.row_factory = sqlite3.Row
            result = create_card_in_transaction(db, content=text, origin='migration', source='auto',
                evidence_source_kind=kind, evidence_source_ref=ref)
            db.execute('UPDATE card_evidence SET raw_event_start_id=72,raw_event_end_id=72 '
                       'WHERE revision_id=?', (result['current_revision_id'],))
            return result

    def event(self, event_id='canonical:one', raw=72, version=1, deleted=False):
        self.archive.import_history([dict(app='niannian', channel='main', source_event_id=event_id,
            version=version, text=None if deleted else '合成来源正文', role='user',
            deleted=deleted, raw_event_id=raw, hash_verification='unknown')])

    def recall(self, **visible):
        return self.app.recall_for_turn('chenlin', 'niannian', '青岚工程', **visible)

    def test_previous_assistant_visible_raw_excludes_legacy_card(self):
        card = self.card(text='青岚工程只能在预览分支修改')
        self.archive.import_history([dict(
            app='niannian', channel='main', source_event_id='canonical:previous',
            version=1, role='assistant', text='前一轮助手正在解释选项',
            occurred_at='2026-10-31T04:00:00Z', conversation_id='synthetic-conv',
            epoch_id='synthetic-epoch', raw_event_id=72, hash_verification='unknown')])
        previous = dict(agent_id='chenlin', app_id='niannian', channel='main',
                        source_event_id='canonical:previous', version=1,
                        text='前一轮助手正在解释选项')
        context = dict(previous_assistant_ref=previous, current_epoch_id='synthetic-epoch',
                       conversation_id='synthetic-conv')
        self.assertIn(card['card_id'], [m['id'] for m in self.recall()['memories']])
        self.assertNotIn(card['card_id'], [m['id'] for m in self.recall(host_context=context)['memories']])

    def test_all_legacy_kinds_are_unverified_and_sources_unchanged(self):
        for kind in ('user_text_exact', 'assistant_text_exact', 'raw_range',
                     'legacy_unverified', 'manual', 'derived', 'agent_mcp', 'bottle_answer'):
            with self.subTest(kind=kind):
                card = self.card(kind)
                memory_before, archive_before = self.memory.read_bytes(), self.archive.path.read_bytes()
                result = self.recall(visible_memory_ids=list(range(1, card['card_id'])))
                item = result['memories'][0]
                self.assertEqual(item['id'], card['card_id'])
                self.assertEqual(item['revision_id'], card['current_revision_id'])
                self.assertEqual(item['ownership'], 'unknown')
                self.assertEqual(item['evidence_resolution'], 'legacy_unverified')
                self.assertFalse(item['verified'])
                self.assertEqual(item['source_refs'], [])
                self.assertIn('证据未核实', result['rendered'])
                evidence = self.app.evidence('chenlin', 'niannian', card['card_id'])
                self.assertEqual(evidence[0]['resolution'], 'legacy_unverified')
                self.assertIsNone(evidence[0]['current_source_text'])
                self.assertEqual(self.memory.read_bytes(), memory_before)
                self.assertEqual(self.archive.path.read_bytes(), archive_before)
        with closing(sqlite3.connect(self.memory)) as db:
            db.row_factory = sqlite3.Row
            self.assertTrue(all(s['state'] == 'unknown' for s in ownership(db, 'chenlin', 'main').values()))
        self.assertEqual(scoped_rows(self.memory, 'niannian', 'main', self.archive.path, 'chenlin')[0], [])
        self.assertEqual(self.providers.audit(), [])

    def test_current_revision_seen_ids_texts_and_raw_range(self):
        card = self.card(text='青岚工程旧约定是正式分支')
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.row_factory = sqlite3.Row
            current = update_card_in_transaction(db, card_id=card['card_id'],
                content='青岚工程新约定只能在预览分支',
                expected_current_revision_id=card['current_revision_id'], origin='manual_ui',
                change_type='manual_edit', evidence_source_kind='manual', evidence_source_ref=None)
            db.execute('UPDATE card_evidence SET raw_event_start_id=73,raw_event_end_id=73 '
                       'WHERE revision_id=?', (current['current_revision_id'],))
        result = self.recall()
        self.assertEqual(result['memories'][0]['revision_id'], current['current_revision_id'])
        self.assertNotIn('正式分支', result['rendered'])
        for visible in (dict(visible_memory_ids=[card['card_id']]),
                        dict(visible_texts=['青岚工程新约定只能在预览分支'])):
            self.assertFalse(self.recall(**visible)['memories'])
        self.event(raw=72)  # 历史修订来源已见也排除整卡
        self.assertFalse(self.recall(visible_archive_ids=['canonical:one'])['memories'])
        self.event('canonical:unrelated', raw=900)
        self.assertTrue(self.recall(visible_archive_ids=['canonical:unrelated'])['memories'])
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.execute('UPDATE card_evidence SET raw_event_start_id=NULL')
        self.assertFalse(self.recall(visible_archive_ids=['canonical:unrelated'])['memories'])

    def test_stale_deleted_cross_scope_and_inactive_rejected(self):
        self.event()
        ref = dict(agent_id='chenlin', app='niannian', channel='main',
                   source_event_id='canonical:one', version=1, kind='archive_quote')
        card = self.card('derived', json.dumps(ref))
        self.assertTrue(self.recall()['memories'])
        self.event(version=2)
        self.assertFalse(self.recall()['memories'])
        self.event(version=3, deleted=True)
        self.assertFalse(self.recall()['memories'])
        for scope in ({'channel': 'private'}, {'app': 'other'}, {'agent_id': 'someone'}):
            self.card('user_text_exact', json.dumps(scope))
        self.assertFalse(self.recall()['memories'])
        sunk = self.card(text='青岚工程沉底但仍有效')
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.execute('UPDATE cards SET status=? WHERE id=?', ('sunk', sunk['card_id']))
        self.assertIn(sunk['card_id'], [m['id'] for m in self.recall()['memories']])
        for status in ('trashed', 'archived', 'retired'):
            other = self.card()
            with closing(sqlite3.connect(self.memory)) as db, db:
                db.execute('UPDATE cards SET status=? WHERE id=?', (status, other['card_id']))
            self.assertNotIn(other['card_id'], [m['id'] for m in self.recall()['memories']])
        self.assertEqual(self.app.evidence('chenlin', 'niannian', card['card_id'])[0]['resolution'], 'deleted')

    def test_rechecks_revision_after_ranking_and_bridge_uses_real_core(self):
        card = self.card()
        self.providers.configure('rerank', mode='local', model='synthetic')
        def rerank(query, texts):
            with closing(sqlite3.connect(self.memory)) as db, db:
                db.execute('UPDATE cards SET status=? WHERE id=?', ('trashed', card['card_id']))
            return [1.0] * len(texts)
        # configured_model 支持宿主已加载的 callable，无 API。
        self.providers.local_models['rerank'] = ('synthetic', rerank)
        self.assertFalse(self.recall()['memories'])
        self.providers.configure('rerank', mode='disabled')
        active = self.card()
        result = recall_vnext(self.app, 'chenlin', 'niannian', dict(user_text='青岚工程'))
        self.assertEqual(result['trace']['related']['memory_refs'], [dict(
            card_id=active['card_id'], revision_id=active['current_revision_id'])])
        self.assertIn('证据未核实', result['rendered'])

    def test_stable_turn_batches_evidence_and_scans_once_without_persistent_cache(self):
        from thalemora.retrieval.core import _ro
        for i in range(24):
            self.card(text=f'青岚工程第{i}项合成约定')
        statements = []

        @contextmanager
        def traced_ro(path):
            with _ro(path) as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch('thalemora.retrieval.core._ro', traced_ro), patch.object(
                self.app, '_rows', wraps=self.app._rows) as rows, patch.object(
                self.app, '_evidence', side_effect=AssertionError('不得逐卡查询')):
            self.assertTrue(self.recall()['memories'])
            self.assertEqual(rows.call_count, 1)
            queries = [s for s in statements if s.startswith('SELECT e.*,r.card_id')]
            self.assertEqual(len(queries), 1)
            self.assertNotIn('WHERE r.card_id=', queries[0])
            self.assertTrue(self.recall()['memories'])
            self.assertEqual(rows.call_count, 2)  # 下一轮重新读取，不留常驻缓存。

    def test_changes_during_rerank_invalidate_memory_and_archive_snapshots(self):
        self.providers.configure('rerank', mode='local', model='synthetic')
        for journal in ('DELETE', 'WAL'):
            for path in (self.memory, self.archive.path):
                with closing(sqlite3.connect(path)) as db:
                    db.execute('PRAGMA journal_mode=' + journal)
            for change in ('revision', 'evidence_scope', 'source_version', 'source_deleted', 'visible_raw'):
                with self.subTest(journal=journal, change=change):
                    with closing(sqlite3.connect(self.memory)) as db, db:
                        db.execute("UPDATE cards SET status='trashed'")
                    event_id = f'canonical:{journal}:{change}'
                    self.event(event_id, raw=900)
                    card = self.card()
                    ref = dict(agent_id='chenlin', app='niannian', channel='main',
                               source_event_id=event_id, version=1, kind='archive_quote')
                    if change in {'source_version', 'source_deleted'}:
                        with closing(sqlite3.connect(self.memory)) as db, db:
                            db.execute('INSERT INTO card_evidence(revision_id,source_kind,source_ref) '
                                       "VALUES(?,'derived',?)",
                                       (card['current_revision_id'], json.dumps(ref)))

                    def rerank(query, texts):
                        if change == 'revision':
                            with closing(sqlite3.connect(self.memory)) as db, db:
                                db.row_factory = sqlite3.Row
                                update_card_in_transaction(db, card_id=card['card_id'],
                                    content='青岚工程修订后的约定',
                                    expected_current_revision_id=card['current_revision_id'],
                                    origin='manual_ui', change_type='manual_edit',
                                    evidence_source_kind='manual', evidence_source_ref=None)
                        elif change == 'evidence_scope':
                            with closing(sqlite3.connect(self.memory)) as db, db:
                                db.execute('UPDATE card_evidence SET source_ref=? WHERE revision_id=?',
                                           (json.dumps(dict(channel='private')), card['current_revision_id']))
                        elif change == 'visible_raw':
                            with closing(sqlite3.connect(self.archive.path)) as db, db:
                                db.execute('UPDATE history_events SET raw_event_id=72 WHERE source_event_id=?',
                                           (event_id,))
                        else:
                            self.event(event_id, version=2, deleted=change == 'source_deleted')
                        return [1.0] * len(texts)

                    self.providers.local_models['rerank'] = ('synthetic', rerank)
                    visible = dict(visible_archive_ids=[event_id]) if change == 'visible_raw' else {}
                    with patch.object(self.app, '_rows', wraps=self.app._rows) as rows:
                        self.assertFalse(self.recall(**visible)['memories'])
                        self.assertEqual(rows.call_count, 2)

    def test_unverified_fact_is_not_promoted_and_bad_revision_is_rejected(self):
        card = self.card(text='合成人物的生日是8月')
        result = self.app.recall_for_turn('chenlin', 'niannian', '合成人物的生日是9月')
        self.assertIn('证据未核实', result['rendered'])
        self.assertNotIn('fact_hints', result)
        self.assertNotIn('原事实', result['rendered'])
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.execute('UPDATE cards SET current_revision_id=999 WHERE id=?', (card['card_id'],))
        self.assertFalse(self.app.recall_for_turn('chenlin', 'niannian', '合成人物')['memories'])

    def test_foreign_record_and_unmapped_visible_source_fail_closed(self):
        card = self.card()
        self.assertFalse(self.recall(visible_archive_ids=['missing-source'])['memories'])
        with closing(sqlite3.connect(self.memory)) as db, db:
            db.execute('CREATE TABLE archive_card_scopes(card_id INTEGER,app TEXT,channel TEXT)')
            db.execute('INSERT INTO archive_card_scopes VALUES(?,?,?)', (card['card_id'], 'other', 'main'))
        self.assertEqual(self.app.evidence('chenlin', 'niannian', card['card_id']), [])
        self.assertFalse(self.recall()['memories'])

    def test_binding_rejects_other_principal_agent_source_and_private(self):
        for agent, app, channel in (('other', 'niannian', 'main'), ('chenlin', 'other', 'main'),
                                    ('chenlin', 'niannian', 'private')):
            with self.subTest(scope=(agent, app, channel)), self.assertRaises(AccessDenied):
                self.app.recall_for_turn(agent, app, '青岚工程', channel)
        with self.assertRaises(AccessDenied):
            NiannianReadOnlyRecall(binding=self.binding, owner=OwnerPrincipal('other'),
                agent_id='chenlin', app_id='niannian', memory_path=self.memory,
                index_path=self.root / 'other.sqlite', providers=self.providers)


if __name__ == '__main__':
    unittest.main()
