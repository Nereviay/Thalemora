"""F2: synthetic Archive -> Writer/Gatekeeper -> independent extension consumption."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from thalemora.app import Application
from thalemora.memory.revision_store import create_card, create_extension, update_card
from thalemora.retrieval.core import RetrievalIndex
from test_pre6_maintenance import SyntheticProvider


class ExtensionRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="extension-recall-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.app = Application(self.root)
        self.agent = self.app.register_agent("synthetic-extension")['agent_id']
        self.app.grant_source(self.agent, "demo")
        self.net = patch("thalemora.app.providers.urlopen", side_effect=AssertionError("no network"))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.path = self.app.memory_path(self.agent, "demo", "main")

    def event(self, eid="base", text="合成长青项目采用紫色符号", version=1, deleted=False,
              app="demo", channel="main", agent=None):
        self.app.import_history(agent or self.agent, app, [dict(app=app, channel=channel,
            source_event_id=eid, version=version, text=text, deleted=deleted, role="user",
            occurred_at="2001-01-01T00:00:00Z", hash_verification="unknown")], channel)

    def evidence(self, eid="delta", version=1, **changes):
        ref = dict(kind="archive_quote", agent_id=self.agent, app="demo", channel="main",
                   source_event_id=eid, version=version)
        ref.update(changes)
        return dict(source_kind="derived", source_ref=json.dumps(ref, ensure_ascii=False))

    def base(self):
        self.event()
        self.card = create_card(self.path, content="合成长青项目采用紫色符号", source="auto",
                                origin="auto_writer", evidence_rows=[self.evidence("base")])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS archive_card_scopes(card_id INTEGER PRIMARY KEY, app TEXT, channel TEXT)")
            db.execute("INSERT INTO archive_card_scopes VALUES(?, 'demo', 'main')", (self.card['card_id'],))
        return self.card

    def extend(self, text="合成玄凤鹦鹉约定", eid="delta", rows=None):
        self.event(eid, text)
        return create_extension(self.path, card_id=self.card['card_id'],
            expected_base_revision_id=self.card['current_revision_id'], content=text,
            source="auto", origin="auto_writer", same_memory_confidence=.99,
            evidence_rows=rows if rows is not None else [self.evidence(eid)])

    def recall(self, query="玄凤鹦鹉", **kwargs):
        return self.app.recall_for_turn(self.agent, "demo", query, **kwargs)

    def extensions(self, *, scope='source-scoped'):
        return self.app.memory(self.agent, "demo", scope=scope)[0].get('extensions', [])

    def test_writer_gatekeeper_new_extend_read_recall_evidence_restart_update_delete(self):
        provider = SyntheticProvider()
        self.event()
        self.assertEqual(self.app.maintenance_tick(self.agent, "demo", provider=provider)['jobs'][0]['status'], 'done')
        before = self.app.memory(self.agent, "demo")[0]
        self.event(text="合成长期项目增加玄凤鹦鹉约定", version=2)
        provider.action = 'extend'
        result = self.app.maintenance_tick(self.agent, "demo", provider=provider)
        self.assertEqual(result['jobs'][0]['status'], 'done')
        with closing(sqlite3.connect(self.path)) as db:
            ext_id, base_id = db.execute('SELECT id,base_revision_id FROM memory_extensions').fetchone()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_extension_evidence').fetchone()[0], 1)
        after = self.app.memory(self.agent, "demo")[0]
        self.assertEqual((after['content'], after['current_revision_id']), (before['content'], before['current_revision_id']))
        self.assertEqual([e['extension_id'] for e in after.get('extensions', [])], [ext_id])
        hit = self.recall()['memories'][0]
        self.assertEqual((hit['id'], hit['extension_id'], hit['base_revision_id']), (before['id'], ext_id, base_id))
        self.assertEqual(hit['evidence_refs'][0]['resolution'], 'current')
        self.assertIn(f'扩展 {ext_id}', self.recall()['rendered'])
        self.assertNotIn('紫色符号', self.recall()['rendered'])
        evidence = self.app.evidence(self.agent, "demo", before['id'])
        self.assertEqual([(e.get('extension_id'), e['resolution']) for e in evidence], [(None, 'source_changed'), (ext_id, 'current')])
        # Real new interpreter, not just a new Python object; no provider configured.
        code = """import sys,json
from thalemora.app import Application
r=Application(sys.argv[1]).recall_for_turn(sys.argv[2], 'demo', '玄凤鹦鹉')
assert r['memories'][0]['extension_id']==int(sys.argv[3]), r
print(json.dumps(r,ensure_ascii=False))
"""
        proc = subprocess.run([sys.executable, '-c', code, str(self.root), self.agent, str(ext_id)], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.app = Application(self.root)
        self.event(text="合成新版琥珀约定", version=3)
        self.assertEqual(self.recall()['memories'], [])
        self.assertEqual(self.recall('琥珀')['archive'][0]['version'], 3)
        self.assertEqual(self.app.evidence(self.agent, 'demo', before['id'])[-1]['resolution'], 'source_changed')
        self.event(text=None, version=4, deleted=True)
        self.assertEqual(self.recall()['rendered'], '')
        self.assertEqual(self.app.evidence(self.agent, 'demo', before['id'])[-1]['resolution'], 'deleted')
        self.assertEqual(self.extensions()[0]['content'], '合成长期项目增加玄凤鹦鹉约定')

    def test_independent_units_multi_extension_evidence_and_pagination(self):
        self.base()
        self.event('support', '合成佐证')
        first = self.extend(rows=[self.evidence(), self.evidence('support')])
        second = self.extend('合成蓝鲸港湾约定', 'other')
        rows = RetrievalIndex._memory_rows(str(self.path))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r['source_key'] for r in rows}), 3)
        hit = self.recall('蓝鲸港湾')['memories'][0]
        self.assertEqual(hit['extension_id'], second['extension_id'])
        self.assertEqual(hit['evidence_refs'][0]['resolution'], 'current')
        all_evidence = self.app.evidence(self.agent, 'demo', self.card['card_id'])
        pages = sum((self.app.evidence(self.agent, 'demo', self.card['card_id'], limit=1, offset=i) for i in range(4)), [])
        self.assertEqual(pages, all_evidence)
        self.assertEqual([e.get('extension_id') for e in all_evidence], [None, first['extension_id'], first['extension_id'], second['extension_id']])
        self.event('support', None, version=2, deleted=True)
        self.assertEqual(self.recall()['memories'], [])
        self.assertTrue(self.recall('蓝鲸港湾')['memories'])

    def test_stale_base_correct_supersede_manual_edit_and_metadata_refresh(self):
        for change, origin in [('correct', 'auto_writer'), ('supersede', 'auto_writer'),
                               ('manual_edit', 'manual_ui'), ('derived_refresh', 'migration')]:
            with self.subTest(change=change):
                self.base()
                ext = self.extend()
                update_card(self.path, card_id=self.card['card_id'], expected_current_revision_id=self.card['current_revision_id'],
                    content='合成新的正式知识', origin=origin, change_type=change,
                    evidence_rows=[self.evidence('base')])
                self.assertEqual(self.recall()['memories'], [])
                extension = next(e for c in self.app.memory(self.agent, 'demo') for e in c.get('extensions', []) if e['extension_id'] == ext['extension_id'])
                self.assertEqual(extension['eligibility'], 'stale_base_revision')
                self.assertEqual(extension['status'], 'active')

    def test_trashed_retired_and_inactive_extension_are_not_consumed(self):
        self.base()
        ext = self.extend()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE memory_extensions SET status='retired' WHERE id=?", (ext['extension_id'],))
        self.assertEqual(self.recall()['rendered'], '')
        for status in ('trashed', 'sunk', 'archived'):
            with self.subTest(card_status=status):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("UPDATE memory_extensions SET status='active'")
                    db.execute('UPDATE cards SET status=?', (status,))
                self.assertEqual(self.recall()['rendered'], '')
        replacement = create_card(self.path, content='合成替代卡', source='auto', origin='auto_writer',
                                  evidence_rows=[self.evidence('base')])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE cards SET status='fresh'")
            db.execute('INSERT INTO memory_card_lineage(from_card_id,to_card_id,from_revision_id,to_revision_id,relation_type,origin,created_at) '
                       "VALUES(?,?,?,?, 'superseded_by', 'auto_writer', '2001-01-01')",
                       (self.card['card_id'], replacement['card_id'], self.card['current_revision_id'], replacement['current_revision_id']))
        self.assertEqual(self.recall()['rendered'], '')
        self.assertEqual(self.extensions()[0]['eligibility'], 'retired_card')

    def test_extension_cannot_claim_manual_exception_or_use_bad_missing_evidence(self):
        self.base()
        cases = [[], [dict(source_kind='manual', source_ref=json.dumps(dict(kind='mcp_explicit_manual_revision', app='demo', channel='main')))],
                 [dict(source_kind='derived', source_ref='bad json')], [self.evidence('missing')]]
        for i, rows in enumerate(cases):
            with self.subTest(case=i):
                ext = self.extend(eid=f'delta{i}', rows=rows)
                self.assertEqual(self.recall()['memories'], [])
                self.assertFalse(next(e for e in self.extensions(scope='owner-global') if e['extension_id']==ext['extension_id'])['eligible'])

    def test_extension_scope_filtering_agent_app_private_and_foreign_evidence(self):
        self.base()
        ext = self.extend()
        other = self.app.register_agent('synthetic-other')['agent_id']
        self.app.grant_source(other, 'demo')
        self.app.grant_source(self.agent, 'other')
        self.app.grant_source(self.agent, 'demo', 'private')
        for agent, app, channel in [(other, 'demo', 'main'), (self.agent, 'other', 'main'), (self.agent, 'demo', 'private')]:
            with self.subTest(scope=(agent, app, channel)):
                self.assertEqual(self.app.recall_for_turn(agent, app, '玄凤鹦鹉', channel)['rendered'], '')
                self.assertFalse(any(c.get('extensions') for c in self.app.memory(agent, app, channel)))
                with self.assertRaises(PermissionError):
                    self.app.evidence(agent, app, self.card['card_id'], channel)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE memory_extension_evidence SET source_ref=? WHERE extension_id=?',
                       (json.dumps(dict(kind='archive_quote', agent_id=other, app='demo', channel='main', source_event_id='delta', version=1)), ext['extension_id']))
        self.assertEqual(self.recall()['memories'], [])
        source = self.app.evidence(self.agent, 'demo', self.card['card_id'])
        self.assertFalse(any(e.get('extension_id') == ext['extension_id'] for e in source))
        evidence = self.app.evidence(self.agent, None, self.card['card_id'], scope='owner-global')[-1]
        self.assertEqual(evidence['resolution'], 'outside_scope')
        self.assertIsNone(evidence['current_source_text'])

    def test_final_injection_rechecks_extension_state_and_evidence(self):
        self.base()
        self.extend()
        def rerank(query, docs):
            self.event('delta', None, version=2, deleted=True)
            return [1] * len(docs)
        self.app.providers.local_models['rerank'] = ('synthetic', rerank)
        self.app.providers.configure('rerank', mode='local', model='synthetic')
        self.assertEqual(self.recall()['rendered'], '')

    def test_public_reference_contract_preserves_extension_identity_without_evidence_text(self):
        from thalemora.retrieval.recall import public_references
        self.base()
        extension = self.extend()
        result = self.recall()
        refs = public_references(result)
        self.assertEqual(refs['memories'], [dict(id=self.card['card_id'],
            revision_id=self.card['current_revision_id'], extension_id=extension['extension_id'],
            base_revision_id=self.card['current_revision_id'])])
        self.assertNotIn('evidence_refs', refs['memories'][0])
        self.assertNotIn('text', refs['memories'][0])
        self.assertNotIn('score', refs['memories'][0])

    def test_independent_extension_does_not_inherit_deleted_base_source_text(self):
        self.base()
        self.extend()
        self.event('base', None, version=2, deleted=True)
        result = self.recall()
        self.assertTrue(result['memories'])
        self.assertIn('扩展', result['rendered'])
        self.assertNotIn('紫色符号', result['rendered'])
        self.assertEqual(self.recall('紫色符号')['rendered'], '')

    def test_main_tombstone_cannot_reveal_private_extension_with_same_event_ids(self):
        self.base()
        self.extend()
        self.app.grant_source(self.agent, 'demo', 'private')
        self.event('base', '合成私密基础', channel='private')
        self.event('delta', '合成私密玄凤鹦鹉', channel='private')
        private_path = self.app.memory_path(self.agent, 'demo', 'private')
        private_ref = lambda eid: self.evidence(eid, channel='private')
        base = create_card(private_path, content='合成私密基础', source='auto', origin='auto_writer',
                           evidence_rows=[private_ref('base')])
        create_extension(private_path, card_id=base['card_id'], expected_base_revision_id=base['current_revision_id'],
            content='合成私密玄凤鹦鹉', source='auto', origin='auto_writer', same_memory_confidence=.99,
            evidence_rows=[private_ref('delta')])
        self.event('delta', None, version=2, deleted=True)
        self.app = Application(self.root)
        self.assertEqual(self.recall()['rendered'], '')
        private = self.app.recall_for_turn(self.agent, 'demo', '玄凤鹦鹉', 'private')
        self.assertIn('私密', private['rendered'])
        self.assertEqual(private['memories'][0]['evidence_refs'][0]['resolution'], 'current')

    def test_local_search_filters_extensions_before_embedding_and_after_source_changes(self):
        self.base()
        self.extend()
        class Embedder:
            def __init__(self):
                self.docs = []
            def encode_many(self, texts, batch_size=16):
                self.docs.extend(texts)
                return [[1., 0.] for _ in texts]
            def encode_one(self, text):
                return [1., 0.]
        embedder = Embedder()
        self.app.embedder = ('synthetic', embedder)
        self.app.grant_source(self.agent, 'other')
        self.app.build_local_index(self.agent, 'other')
        self.assertFalse(any('玄凤鹦鹉' in t for t in embedder.docs))
        self.assertFalse(any('extension_id' in h['provenance'] for h in self.app.local_search(self.agent, 'other', '玄凤鹦鹉')))
        hits = self.app.local_search(self.agent, 'demo', '玄凤鹦鹉')
        self.assertTrue(any(h['provenance'].get('extension_id') == 1 for h in hits))
        self.event('delta', None, version=2, deleted=True)
        self.assertFalse(any('extension_id' in h['provenance'] for h in self.app.local_search(self.agent, 'demo', '玄凤鹦鹉')))

    def test_visible_base_does_not_hide_extension_for_unrelated_visible_raw(self):
        self.base()
        self.extend()
        self.assertTrue(self.recall(visible_archive_ids=['base'])['memories'])
        self.assertEqual(self.recall(visible_archive_ids=['delta'])['memories'], [])
        # Existing visible_memory_ids denotes a whole card; keep compatibility.
        self.assertEqual(self.recall(visible_memory_ids=[self.card['card_id']])['memories'], [])

    def test_legacy_missing_extension_tables_need_no_migration(self):
        self.base()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE memory_extension_evidence')
            db.execute('DROP TABLE memory_extensions')
        before = self.path.read_bytes()
        rows = RetrievalIndex._memory_rows(str(self.path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(before, self.path.read_bytes())

    def test_fresh_extension_of_corrected_base_does_not_revive_old_extension(self):
        self.base()
        old = self.extend()
        self.card = update_card(self.path, card_id=self.card['card_id'],
            expected_current_revision_id=self.card['current_revision_id'], content='合成更正正文',
            origin='auto_writer', change_type='correct', evidence_rows=[self.evidence('base')])
        fresh = self.extend('合成蓝鲸港湾补充', 'fresh')
        self.assertNotEqual(old['extension_id'], fresh['extension_id'])
        self.assertEqual(self.recall()['memories'], [])
        self.assertEqual(self.recall('蓝鲸港湾')['memories'][0]['extension_id'], fresh['extension_id'])

    def test_refresh_single_card_includes_extensions_and_removes_stale_units(self):
        self.base()
        self.extend()
        index = RetrievalIndex(str(self.path.with_name('synthetic-index.sqlite')), memory_db_path=str(self.path), embedding_model='synthetic', embedder=None)
        index.refresh_memory_card(self.card['card_id'])
        hits = index.search('玄凤鹦鹉')
        self.assertEqual(hits[0]['provenance']['extension_id'], 1)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE cards SET status='trashed'")
        index.refresh_memory_card(self.card['card_id'])
        self.assertEqual(index.list_memory_chunks(), [])


if __name__ == '__main__':
    unittest.main()
