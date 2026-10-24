"""F3: actual threads/connections, deterministic events, synthetic models/data only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import numpy as np

from thalemora.memory import schema, revision_store
from thalemora.retrieval.core import RetrievalIndex, RetrievalShadowError, SCHEMA, SCHEMA_VERSION, _sha
from thalemora.adapters.niannian_retrieval import NiannianRetrievalIndex
from thalemora.retrieval.recall import ensure_current_index


class Model:
    def __init__(self, value=(0., 1.), *, blocked=False):
        self.value = value
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocked = blocked
        self.calls = 0

    def encode_one(self, text):
        self.calls += 1
        self.entered.set()
        if self.blocked and not self.release.wait(10):
            raise AssertionError('barrier timeout')
        return self.value

    def encode_many(self, texts, *, batch_size=16):
        return [self.encode_one(text) for text in texts]


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='f3-synthetic-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.memory = self.root / 'memory.sqlite'
        self.cache = self.root / 'index.sqlite'
        with closing(sqlite3.connect(self.memory)) as db, db:
            schema.initialize(db)
        self.card = revision_store.create_card(self.memory, content='synthetic old',
            origin='manual_ui', source='manual', evidence_source_kind='manual',
            evidence_source_ref='synthetic')
        self.new = Model()
        self.old = Model((1., 0.), blocked=True)
        self.a = self.index(self.old)
        self.b = self.index(self.new)

    def index(self, model=None, name='synthetic-v1'):
        return RetrievalIndex(str(self.cache), memory_db_path=str(self.memory),
                              embedding_model=name, embedder=model)

    def rows(self):
        return RetrievalIndex._memory_rows(str(self.memory))

    def revise(self, text='synthetic new'):
        self.card = revision_store.update_card(self.memory, card_id=self.card['card_id'],
            expected_current_revision_id=self.card['current_revision_id'], content=text)

    def chunks(self):
        with closing(sqlite3.connect(self.cache)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM chunks ORDER BY source_key')]

    def race(self, older, newer):
        # The competing operation must finish while the first model is blocked:
        # this also proves inference holds no SQLite write lock.
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(older)
            try:
                self.assertTrue(self.old.entered.wait(10), 'old model not reached')
                second = pool.submit(newer)
                result = second.result(timeout=5)
            finally:
                self.old.release.set()
            stale = first.result(timeout=10)
        return stale, result

    def assert_new_vector(self):
        rows = self.chunks()
        self.assertEqual(len(rows), 1)
        np.testing.assert_array_equal(np.frombuffer(rows[0]['vector'], dtype=np.float32), self.new.value)
        return rows[0]

    def test_lane_old_vectors_cannot_fill_new_text(self):
        self.race(self.a.rebuild_memory, lambda: (self.revise(), self.b.rebuild_memory()))
        row = self.assert_new_vector()
        self.assertEqual(row['text'], 'synthetic new')
        # Before F3 this did nothing despite the wrong old vector.
        count = self.new.calls
        ensure_current_index(self.b, self.rows(), self.new)
        self.assert_new_vector()
        self.assertEqual(self.new.calls, count)

    def test_same_text_new_revision_and_evidence_rejects_old_completion(self):
        def newer():
            self.revise('synthetic old')
            with closing(sqlite3.connect(self.memory)) as db, db:
                db.execute('UPDATE card_evidence SET source_ref=? WHERE revision_id=?',
                           ('synthetic new evidence', self.card['current_revision_id']))
            self.b.rebuild_memory()
        self.race(self.a.rebuild_memory, newer)
        row = self.assert_new_vector()
        self.assertEqual(json.loads(row['provenance_json'])['revision_id'], self.card['current_revision_id'])

    def test_model_switch_rejects_old_completion(self):
        self.b = self.index(self.new, 'synthetic-v2')
        self.race(self.a.rebuild_memory, self.b.rebuild_memory)
        self.assertEqual(self.assert_new_vector()['embedding_model'], 'synthetic-v2')

    def test_old_card_refresh_cannot_resurrect_deleted_card(self):
        def newer():
            with closing(sqlite3.connect(self.memory)) as db, db:
                db.execute("UPDATE cards SET status='trashed'")
            self.b.rebuild_memory()
        self.race(lambda: self.a.refresh_memory_card(self.card['card_id']), newer)
        self.assertEqual(self.chunks(), [])

    def test_old_card_refresh_cannot_overwrite_new_lane(self):
        self.race(lambda: self.a.refresh_memory_card(self.card['card_id']),
                  lambda: (self.revise(), self.b.rebuild_memory()))
        self.assertEqual(self.assert_new_vector()['text'], 'synthetic new')

    def test_old_lane_cannot_overwrite_new_card_refresh(self):
        self.race(self.a.rebuild_memory,
                  lambda: (self.revise(), self.b.refresh_memory_card(self.card['card_id'])))
        self.assertEqual(self.assert_new_vector()['text'], 'synthetic new')

    def test_extension_invalidation_while_lane_embeds(self):
        row = dict(self.rows()[0], source_key='memory:1:extension:1', text='synthetic delta',
                   content_hash=_sha('synthetic delta'), provenance={'extension_id': 1, 'base_revision_id': 1})
        self.race(lambda: self.a._replace_lane('memory_card', [row], source_path=str(self.memory), embedder=self.old),
                  lambda: self.b._replace_lane('memory_card', [], source_path=str(self.memory), embedder=self.new))
        self.assertEqual(self.chunks(), [])
        with closing(sqlite3.connect(self.cache)) as db:
            self.assertEqual(db.execute("SELECT source_count FROM build_receipts ORDER BY rowid DESC LIMIT 1").fetchone()[0], 0)

    def test_failed_inference_is_repaired_after_real_interpreter_restart(self):
        class Failure(Model):
            def encode_one(self, text):
                raise RuntimeError('synthetic crash')
        failed = self.index(Failure())
        with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
            failed.rebuild_memory()
        code = '''import sys
from thalemora.retrieval.core import RetrievalIndex
from thalemora.retrieval.recall import ensure_current_index
class Model:
 def encode_many(self,texts,**kw): return [[0.,1.] for _ in texts]
i=RetrievalIndex(sys.argv[1],memory_db_path=sys.argv[2],embedding_model='synthetic-v1',embedder=Model())
ensure_current_index(i,i._memory_rows(sys.argv[2]),i.embedder)
'''
        p = subprocess.run([sys.executable, '-c', code, str(self.cache), str(self.memory)], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assert_new_vector()

    def test_actual_extension_source_deleted_during_inference(self):
        from test_extension_recall import ExtensionRecallTests
        fixture = ExtensionRecallTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.base()
        fixture.extend()
        path = str(fixture.path)
        self.a = RetrievalIndex(str(self.cache), memory_db_path=path,
                                embedding_model='synthetic-v1', embedder=self.old)
        self.b = RetrievalIndex(str(self.cache), memory_db_path=path,
                                embedding_model='synthetic-v1', embedder=self.new)
        source = lambda: fixture.app._local_rows(fixture.agent, 'demo', 'main')
        self.assertEqual(len(source()), 2)
        def newer():
            fixture.event('delta', text=None, version=2, deleted=True)
            ensure_current_index(self.b, source, self.new)
        self.race(lambda: ensure_current_index(self.a, source, self.old), newer)
        self.assertEqual(len(self.chunks()), 1)
        self.assertNotIn('extension', self.chunks()[0]['source_key'])
        self.assertEqual(fixture.extensions()[0]['eligibility'], 'deleted')
        self.assertEqual(fixture.recall()['memories'], [])

    def test_stale_completion_reports_discard_not_successful_embeddings(self):
        stale, _ = self.race(self.a.rebuild_memory, lambda: (self.revise(), self.b.rebuild_memory()))
        self.assertEqual(stale['embedded_count'], 0)
        self.assertTrue(stale.get('discarded'))

    def pause_snapshot(self, reader):
        def paused(*args):
            rows = reader(*args)
            self.old.entered.set()
            if not self.old.release.wait(10):
                raise AssertionError('source snapshot barrier timeout')
            return rows
        return paused

    def test_stale_lane_snapshot_cannot_delete_new_card_unit(self):
        self.a._memory_rows = self.pause_snapshot(self.a._memory_rows)
        def newer():
            card = revision_store.create_card(self.memory, content='synthetic addition',
                origin='manual_ui', source='manual', evidence_source_kind='manual',
                evidence_source_ref='synthetic')
            self.b.refresh_memory_card(card['card_id'])
        stale, _ = self.race(self.a.rebuild_memory, newer)
        self.assertTrue(stale['discarded'])
        self.assertEqual([r['text'] for r in self.chunks()], ['synthetic addition'])
        self.assertEqual(self.old.calls, 0)

    def test_stale_card_snapshot_cannot_delete_new_extension_unit(self):
        self.a._memory_rows = self.pause_snapshot(self.a._memory_rows)
        def newer():
            rows = self.rows()
            rows.append(dict(rows[0], source_key='memory:1:extension:2', text='synthetic fresh delta',
                             content_hash=_sha('synthetic fresh delta'), provenance={'extension_id': 2}))
            self.b._replace_lane('memory_card', rows, source_path=str(self.memory), embedder=self.new)
        stale, _ = self.race(lambda: self.a.refresh_memory_card(self.card['card_id']), newer)
        self.assertTrue(stale['discarded'])
        self.assertEqual(len(self.chunks()), 2)
        self.assertEqual(self.old.calls, 0)

    def test_freshness_supplier_fenced_before_source_read(self):
        supplier = self.pause_snapshot(self.rows)
        stale, _ = self.race(lambda: ensure_current_index(self.a, supplier, self.old),
                            lambda: (self.revise(), self.b.rebuild_memory()))
        self.assertTrue(stale['discarded'])
        self.assertEqual(self.assert_new_vector()['text'], 'synthetic new')

    def test_v1_cache_migrates_lazily_and_never_trusts_old_vector(self):
        self.cache = self.root / 'legacy.sqlite'
        memory_bytes = self.memory.read_bytes()
        row = self.rows()[0]
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO retrieval_shadow_schema VALUES(1,1,'synthetic v1')")
            db.execute('INSERT INTO chunks(source_kind,source_key,source_id,text,content_hash,'
                       'provenance_json,vector,vector_dim,embedding_model,created_at) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?)', ('memory_card', row['source_key'], row['source_id'],
                        row['text'], row['content_hash'], json.dumps(row['provenance']),
                        np.array([1.,0.], dtype=np.float32).tobytes(), 2, 'synthetic-v1', 'synthetic'))
            db.execute('INSERT INTO chunks_fts VALUES(?,?)', (row['source_key'], row['text']))
        index = self.index(self.new)
        self.assertEqual(self.new.calls, 0)
        self.assertEqual(len(index._lexical_hits('synthetic', 'memory_card', 10)), 1)
        self.assertEqual(index._vector_hits(np.array([1.,0.]), 'memory_card', 10), [])
        ensure_current_index(index, self.rows, self.new)
        self.assert_new_vector()
        self.assertEqual(self.memory.read_bytes(), memory_bytes)
        with closing(sqlite3.connect(self.cache)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM retrieval_shadow_schema').fetchone()[0], SCHEMA_VERSION)

    def test_vector_binding_corruption_detected_after_restart(self):
        self.b.rebuild_memory()
        for field, value in [('vector_generation', -1), ('vector_content_hash', 'stale'),
                             ('target_model', 'synthetic-v0')]:
            with self.subTest(field=field):
                with closing(sqlite3.connect(self.cache)) as db, db:
                    db.execute(f'UPDATE chunks SET {field}=?', (value,))
                index = self.index(self.new)
                self.assertEqual(index._vector_hits(np.array([1.,0.]), 'memory_card', 10), [])
                calls = self.new.calls
                ensure_current_index(index, self.rows, self.new)
                self.assertEqual(self.new.calls, calls + 1)
                self.assert_new_vector()

    def test_completed_same_content_vector_reuses_valid_binding_for_new_revision(self):
        self.b.rebuild_memory()
        calls = self.new.calls
        self.revise('synthetic old')
        self.b.rebuild_memory()
        row = self.assert_new_vector()
        self.assertEqual(self.new.calls, calls)
        self.assertEqual(row['generation'], row['vector_generation'])
        self.assertEqual(json.loads(row['provenance_json'])['revision_id'], self.card['current_revision_id'])

    def test_vector_rowcount_loss_rolls_back_batch_and_is_repairable(self):
        # Simulate an unsupported destination deletion without bumping the clock:
        # even then rowcount must not be reported as a successful embedding.
        def delete_without_generation():
            with closing(sqlite3.connect(self.cache)) as db, db:
                db.execute('DELETE FROM chunks')
        with self.assertRaisesRegex(RetrievalShadowError, 'CAS lost'):
            self.race(self.a.rebuild_memory, delete_without_generation)
        with closing(sqlite3.connect(self.cache)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM build_receipts').fetchone()[0], 0)
        ensure_current_index(self.b, self.rows, self.new)
        self.assert_new_vector()

    def test_cached_single_card_refresh_preserves_embedded_result_contract(self):
        self.b.refresh_memory_card(self.card['card_id'])
        calls = self.new.calls
        result = self.b.refresh_memory_card(self.card['card_id'])
        self.assertTrue(result['embedded'])
        self.assertEqual(result['embedding_model'], 'synthetic-v1')
        self.assertEqual(self.new.calls, calls)

    def test_batch_rowcount_loss_rolls_back_prior_vector_updates(self):
        second = revision_store.create_card(self.memory, content='synthetic second',
            origin='manual_ui', source='manual', evidence_source_kind='manual',
            evidence_source_ref='synthetic')
        def delete_second():
            with closing(sqlite3.connect(self.cache)) as db, db:
                db.execute('DELETE FROM chunks WHERE source_id=?', (second['card_id'],))
        with self.assertRaisesRegex(RetrievalShadowError, 'CAS lost'):
            self.race(self.a.rebuild_memory, delete_second)
        self.assertIsNone(self.chunks()[0]['vector'])
        with closing(sqlite3.connect(self.cache)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM build_receipts').fetchone()[0], 0)
        ensure_current_index(self.b, self.rows, self.new)
        self.assertEqual(len(self.chunks()), 2)
        for row in self.chunks():
            np.testing.assert_array_equal(np.frombuffer(row['vector'], dtype=np.float32), self.new.value)

    def test_concurrent_v1_initialization_is_serialized(self):
        self.cache = self.root / 'legacy-concurrent.sqlite'
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO retrieval_shadow_schema VALUES(1,1,'synthetic v1')")
        barrier = threading.Barrier(2)
        def initialize():
            barrier.wait(timeout=5)
            return self.index(self.new)._generation('memory_card')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(initialize) for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [0, 0])
        self.index(self.new).rebuild_memory()
        self.assert_new_vector()

    def test_wrong_vector_count_remains_pending_and_recovers(self):
        class Wrong(Model):
            def encode_many(self, texts, **kw):
                return []
        with self.assertRaisesRegex(RetrievalShadowError, 'wrong vector count'):
            self.index(Wrong()).rebuild_memory()
        self.assertIsNone(self.chunks()[0]['vector_generation'])
        ensure_current_index(self.b, self.rows, self.new)
        self.assert_new_vector()

    def raw_fixture(self):
        raw, runtime = self.root / 'raw.sqlite', self.root / 'runtime.sqlite'
        with closing(sqlite3.connect(raw)) as db, db:
            db.execute('CREATE TABLE raw_events(id INTEGER PRIMARY KEY,source TEXT,source_event_id TEXT,'
                       'event_hash TEXT,role TEXT,text TEXT,created_at TEXT,metadata_json TEXT,deleted INTEGER)')
            db.execute("INSERT INTO raw_events VALUES(1,'heartbeat','synthetic','h1','assistant','synthetic old','2001','{}',0)")
            db.execute("INSERT INTO raw_events VALUES(2,'app','other','h2','user','synthetic other','2001','{}',0)")
        with closing(sqlite3.connect(runtime)) as db, db:
            db.execute('CREATE TABLE message_records(id INTEGER PRIMARY KEY,raw_event_id INTEGER,'
                       'epoch_id TEXT,status TEXT,kind TEXT,metadata_json TEXT)')
            db.execute('INSERT INTO message_records VALUES(1,1,\'s\',\'active\',\'proactive_message\',?)',
                       (json.dumps({'proactive_delivery': {'status': 'delivered'}}),))
        self.a = NiannianRetrievalIndex(str(self.cache), memory_db_path=str(self.memory), embedding_model='synthetic-v1', embedder=self.old)
        self.b = NiannianRetrievalIndex(str(self.cache), memory_db_path=str(self.memory), embedding_model='synthetic-v1', embedder=self.new)
        return str(raw), str(runtime)

    def test_raw_refresh_vs_sync_releases_write_lock_and_rejects_old_result(self):
        raw, runtime = self.raw_fixture()
        def newer():
            with closing(sqlite3.connect(raw)) as db, db:
                db.execute("UPDATE raw_events SET text='synthetic new',event_hash='new' WHERE id=1")
            return self.b.sync_delivered_proactive(raw, runtime, embedder=self.new)
        self.race(lambda: self.a.refresh_delivered_proactive(raw, runtime, 1, embedder=self.old), newer)
        self.assertEqual(self.assert_new_vector()['text'], 'synthetic new')

    def test_raw_sync_vs_rebuild_preserves_other_lane_and_new_content(self):
        raw, runtime = self.raw_fixture()
        self.b.rebuild_memory()
        def newer():
            with closing(sqlite3.connect(raw)) as db, db:
                db.execute("UPDATE raw_events SET text='synthetic new',event_hash='new' WHERE id=1")
            return self.b.rebuild_raw(raw, runtime, embedder=self.new)
        self.race(lambda: self.a.sync_delivered_proactive(raw, runtime, embedder=self.old), newer)
        chunks = self.chunks()
        self.assertEqual(len(chunks), 3)
        self.assertEqual({r['text'] for r in chunks}, {'synthetic old', 'synthetic new', 'synthetic other'})
        for row in chunks:
            np.testing.assert_array_equal(np.frombuffer(row['vector'], dtype=np.float32), self.new.value)

    def test_raw_rebuild_vs_delete_refresh_cannot_resurrect_heartbeat(self):
        raw, runtime = self.raw_fixture()
        def newer():
            with closing(sqlite3.connect(raw)) as db, db:
                db.execute('UPDATE raw_events SET deleted=1 WHERE id=1')
            return self.b.refresh_delivered_proactive(raw, runtime, 1, embedder=self.new)
        self.race(lambda: self.a.rebuild_raw(raw, runtime, embedder=self.old), newer)
        self.assertEqual([r['source_key'] for r in self.chunks()], ['raw:2'])
        # The conservative lane fence discarded the unrelated pending vector;
        # explicit rebuild recovers it without reviving the deleted event.
        self.assertIsNone(self.chunks()[0]['vector'])
        self.b.rebuild_raw(raw, runtime, embedder=self.new)
        self.assert_new_vector()


if __name__ == '__main__':
    unittest.main()
