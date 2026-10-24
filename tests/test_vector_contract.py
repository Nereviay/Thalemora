"""F4 synthetic vectors/cache only; no model downloads or authoritative migrations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import numpy as np
from thalemora.memory import schema, revision_store
from thalemora.retrieval.core import RetrievalIndex, RetrievalShadowError, _sha
from thalemora.retrieval.recall import ensure_current_index
from thalemora.adapters.niannian_retrieval import NiannianRetrievalIndex
import test_retrieval_generation as generation_fixture
import test_p8_recall as recall_fixture


class Model:
    def __init__(self, value):
        self.value, self.calls = value, 0
    def encode_one(self, text):
        self.calls += 1
        return self.value
    def encode_many(self, texts, **kw):
        return [self.encode_one(t) for t in texts]


class VectorContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='f4-synthetic-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.memory, self.cache = self.root/'memory.sqlite', self.root/'index.sqlite'
        with closing(sqlite3.connect(self.memory)) as db, db:
            schema.initialize(db)
        self.card = revision_store.create_card(self.memory, content='synthetic valley',
            origin='manual_ui', source='manual', evidence_source_kind='manual', evidence_source_ref='synthetic')

    def index(self, value=(3., 4.), name='synthetic-v1'):
        self.model = Model(value)
        return RetrievalIndex(str(self.cache), memory_db_path=str(self.memory), embedding_model=name, embedder=self.model)

    def chunks(self):
        with closing(sqlite3.connect(self.cache)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM chunks ORDER BY source_key')]

    def test_documents_and_query_use_true_cosine(self):
        index = self.index((1., 100.))
        index.rebuild_memory()
        score = index._vector_hits([9., 0.], 'memory_card', 10)[0][1]
        self.assertAlmostEqual(score, 1 / np.sqrt(10001), places=6)
        np.testing.assert_allclose(np.linalg.norm(np.frombuffer(self.chunks()[0]['vector'], dtype=np.float32)), 1., rtol=1e-6)

    def test_same_direction_different_magnitudes_tie(self):
        index = self.index()
        rows = index._memory_rows(str(self.memory))
        rows.append(dict(rows[0], source_key='memory:2', source_id=2))
        class Varying(Model):
            def encode_many(self, texts, **kw): return [[3.,4.], [300.,400.]]
        index._replace_lane('memory_card', rows, source_path=str(self.memory), embedder=Varying(None))
        hits = index._vector_hits([30.,40.], 'memory_card', 10)
        self.assertEqual(len(hits), 2)
        for _, score in hits: self.assertAlmostEqual(score, 1., places=6)

    def test_actual_recall_threshold_rejects_large_orthogonal_document(self):
        f = recall_fixture.RecallTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        f.card('valley', '远方的山谷')
        class Different(Model):
            def encode_one(self, text): return [7.,0.] if text == '量子计算' else [1.,100.]
        f.app.providers.local_models['embedding'] = ('fake', Different(None))
        f.app.providers.configure('embedding', mode='local', model='fake')
        self.assertEqual(f.recall('量子计算')['rendered'], '')

    def test_true_cosine_threshold_both_sides(self):
        f = recall_fixture.RecallTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        f.card('valley', '远方的山谷')
        class Threshold(Model):
            def encode_one(self, text): return [10.,0.] if text == '量子计算' else self.value
        model = Threshold([60.,80.])
        f.app.providers.local_models['embedding'] = ('below', model)
        f.app.providers.configure('embedding', mode='local', model='below')
        self.assertEqual(f.recall('量子计算')['rendered'], '')
        model.value = [80.,60.]
        f.app.providers.local_models['embedding'] = ('above', model)
        f.app.providers.configure('embedding', mode='local', model='above')
        self.assertTrue(f.recall('量子计算')['memories'])

    def test_cosine_reason_rounding_cannot_cross_recall_threshold(self):
        f = recall_fixture.RecallTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        f.card('valley','远方的山谷')
        class Near(Model):
            def encode_one(self,text):
                return [1.,0.] if text=='量子计算' else [0.64999,np.sqrt(1-0.64999**2)]
        f.app.providers.local_models['embedding']=('near',Near(None))
        f.app.providers.configure('embedding',mode='local',model='near')
        self.assertEqual(f.recall('量子计算')['rendered'],'')

    def test_document_zero_nonfinite_and_malformed_fail_without_success_receipt(self):
        bad = ([0.,0.], [float('nan'),1.], [float('inf'),1.], [], [[1.,0.]], 3., ['1','2'], [True,False], [1+2j,0])
        for i, value in enumerate(bad):
            with self.subTest(value=value):
                self.cache = self.root/f'bad-{i}.sqlite'
                index = self.index(value)
                with self.assertRaises(RetrievalShadowError): index.rebuild_memory()
                self.assertIsNone(self.chunks()[0]['vector'])
                with closing(sqlite3.connect(self.cache)) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM build_receipts').fetchone()[0], 0)

    def test_query_zero_nonfinite_and_malformed_fail_even_without_rows(self):
        index = self.index()
        for value in ([0.,0.], [float('nan'),1.], [float('inf'),1.], [], [[1.,0.]], 3., ['1','2'], [True,False]):
            with self.subTest(value=value):
                with self.assertRaises(RetrievalShadowError): index._vector_hits(value, 'memory_card', 10)

    def test_extreme_finite_magnitudes_are_normalized_without_overflow(self):
        for i, value in enumerate(([1e300,1e300], [1e-300,1e-300])):
            self.cache = self.root/f'extreme-{i}.sqlite'
            index = self.index(value); index.rebuild_memory()
            self.assertAlmostEqual(index._vector_hits(value, 'memory_card', 10)[0][1], 1., places=6)

    def test_malformed_batch_return_is_explicit_contract_error(self):
        index=self.index()
        class Broken(Model):
            def encode_many(self,texts,**kw): return self.value
        for value in (None,1.,'bad',np.array(1.)):
            with self.subTest(value=value),self.assertRaises(RetrievalShadowError):
                index.embedder=Broken(value)
                index.rebuild_memory()

    def test_ragged_batch_is_rejected_atomically(self):
        index = self.index()
        rows = index._memory_rows(str(self.memory))
        rows.append(dict(rows[0], source_key='memory:2', source_id=2))
        class Ragged(Model):
            def encode_many(self, texts, **kw): return [[1.,0.],[1.,0.,0.]]
        with self.assertRaises(RetrievalShadowError):
            index._replace_lane('memory_card', rows, source_path=str(self.memory), embedder=Ragged(None))
        self.assertTrue(all(r['vector'] is None for r in self.chunks()))

    def test_same_model_dimension_drift_rejected_on_single_refresh_after_reopen(self):
        index = self.index(); index.rebuild_memory()
        self.model = Model([1.,0.,0.])
        index = RetrievalIndex(str(self.cache), memory_db_path=str(self.memory), embedding_model='synthetic-v1', embedder=self.model)
        revision_store.update_card(self.memory, card_id=self.card['card_id'], expected_current_revision_id=self.card['current_revision_id'], content='synthetic changed')
        with self.assertRaises(RetrievalShadowError): index.refresh_memory_card(self.card['card_id'])

    def test_query_dimension_and_model_mismatch_are_not_hidden(self):
        index = self.index(); index.rebuild_memory()
        with self.assertRaises(RetrievalShadowError): index._vector_hits([1.,0.,0.], 'memory_card', 10)
        other = self.index(name='synthetic-v2')
        with self.assertRaises(RetrievalShadowError): other._vector_hits([1.,0.], 'memory_card', 10)
        other.embedder = Model([1.,0.,0.]); other.rebuild_memory()
        self.assertEqual(self.chunks()[0]['vector_dim'], 3)

    def test_old_bound_cache_without_normalization_marker_is_not_reused(self):
        index = self.index(); index.rebuild_memory()
        before = self.memory.read_bytes()
        with closing(sqlite3.connect(self.cache)) as db, db:
            columns = {r[1] for r in db.execute('PRAGMA table_info(chunks)')}
            if 'vector_contract' in columns: db.execute('UPDATE chunks SET vector_contract=0')
            db.execute('UPDATE chunks SET vector=?', (np.array([30.,40.], dtype=np.float32).tobytes(),))
        reopened = self.index()
        self.assertEqual(reopened._vector_hits([1.,0.], 'memory_card', 10), [])
        ensure_current_index(reopened, lambda: reopened._memory_rows(str(self.memory)), self.model)
        self.assertEqual(self.model.calls, 1)
        self.assertAlmostEqual(np.linalg.norm(np.frombuffer(self.chunks()[0]['vector'], dtype=np.float32)), 1., places=6)
        self.assertEqual(self.memory.read_bytes(), before)

    def test_marked_corrupt_vectors_are_errors_not_silently_dropped_or_repaired(self):
        index = self.index(); index.rebuild_memory()
        for value in ([0.,0.], [float('nan'),1.], [float('inf'),1.], [3.,4.]):
            with self.subTest(value=value):
                with closing(sqlite3.connect(self.cache)) as db, db:
                    db.execute('UPDATE chunks SET vector=?', (np.array(value, dtype=np.float32).tobytes(),))
                with self.assertRaises(RetrievalShadowError): index._vector_hits([1.,0.], 'memory_card', 10)
                with self.assertRaises(RetrievalShadowError): index.rebuild_memory()

    def test_raw_all_write_entries_share_unit_vector_contract(self):
        f = generation_fixture.GenerationTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        raw, runtime = f.raw_fixture()
        model = Model([30.,40.])
        for operation in (lambda: f.b.rebuild_raw(raw,runtime,embedder=model),
                          lambda: f.b.sync_delivered_proactive(raw,runtime,embedder=model),
                          lambda: f.b.refresh_delivered_proactive(raw,runtime,1,embedder=model)):
            operation()
            for row in f.chunks():
                self.assertAlmostEqual(np.linalg.norm(np.frombuffer(row['vector'],dtype=np.float32)),1.,places=6)
            self.assertAlmostEqual(f.b._vector_hits([300.,400.],'raw_event',10)[0][1],1.,places=6)

    def test_f3_old_nonunit_completion_cannot_replace_normalized_new_model(self):
        f = generation_fixture.GenerationTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        f.old.value, f.new.value = [300.,0.], [0.,900.]
        f.b = f.index(f.new, 'synthetic-v2')
        stale, _ = f.race(f.a.rebuild_memory, lambda: (f.revise(),f.b.rebuild_memory()))
        self.assertTrue(stale['discarded'])
        row = f.chunks()[0]
        np.testing.assert_allclose(np.frombuffer(row['vector'],dtype=np.float32),[0.,1.])
        self.assertEqual(row['generation'], row['vector_generation'])
        self.assertEqual(row['content_hash'], row['vector_content_hash'])
        self.assertEqual(row['embedding_model'],'synthetic-v2')

    def test_legacy_cache_initialization_race_uses_real_threads_and_connections(self):
        from thalemora.retrieval.core import SCHEMA
        for iteration in range(10):
            self.cache=self.root/f'concurrent-{iteration}.sqlite'
            with closing(sqlite3.connect(self.cache)) as db,db:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO retrieval_shadow_schema VALUES(1,2,'synthetic old cache')")
            barrier=threading.Barrier(2)
            def initialize():
                barrier.wait(timeout=5)
                index=RetrievalIndex(str(self.cache),memory_db_path=str(self.memory),embedding_model='synthetic-v1',embedder=None)
                return index._generation('memory_card')
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(initialize) for _ in range(2)]
                self.assertEqual([f.result(timeout=5) for f in futures],[0,0])

    def test_real_schema2_upgrade_preserves_lexical_but_invalidates_bound_nonunit_bytes(self):
        from thalemora.retrieval.core import SCHEMA, SCHEMA_VERSION
        row = RetrievalIndex._memory_rows(str(self.memory))[0]
        vector = np.array([30.,40.],dtype=np.float32).tobytes()
        with closing(sqlite3.connect(self.cache)) as db,db:
            db.executescript(SCHEMA)
            for name,kind in (('generation','INTEGER'),('target_model','TEXT'),
                              ('vector_generation','INTEGER'),('vector_content_hash','TEXT')):
                db.execute(f'ALTER TABLE chunks ADD COLUMN {name} {kind}')
            db.execute("INSERT INTO retrieval_shadow_schema VALUES(1,2,'synthetic schema2')")
            db.execute('CREATE TABLE lane_generations(source_kind TEXT PRIMARY KEY,generation INTEGER)')
            db.executemany('INSERT INTO lane_generations VALUES(?,7)', [('memory_card',),('raw_event',)])
            db.execute('INSERT INTO chunks(source_kind,source_key,source_id,text,content_hash,provenance_json,'
                'vector,vector_dim,embedding_model,created_at,generation,target_model,vector_generation,vector_content_hash) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('memory_card',row['source_key'],row['source_id'],row['text'],row['content_hash'],
                json.dumps(row['provenance']),vector,2,'synthetic-v1','synthetic',7,'synthetic-v1',7,row['content_hash']))
            db.execute('INSERT INTO chunks_fts VALUES(?,?)',(row['source_key'],row['text']))
        before = self.memory.read_bytes()
        index = self.index()
        self.assertEqual(self.model.calls,0)
        self.assertEqual(index._generation('memory_card'),7)
        self.assertEqual(self.chunks()[0]['vector'],vector)
        self.assertEqual(index._vector_hits([1.,0.],'memory_card',10),[])
        self.assertEqual(len(index._lexical_hits('synthetic','memory_card',10)),1)
        ensure_current_index(index,lambda:index._memory_rows(str(self.memory)),self.model)
        self.assertEqual(self.model.calls,1)
        self.assertEqual(self.chunks()[0]['vector_contract'],1)
        self.assertEqual(self.memory.read_bytes(),before)
        with closing(sqlite3.connect(self.cache)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM retrieval_shadow_schema').fetchone()[0],SCHEMA_VERSION)

    def test_raw_zero_query_and_bad_single_refresh_fail_closed(self):
        f = generation_fixture.GenerationTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        raw,runtime = f.raw_fixture()
        with self.assertRaises(RetrievalShadowError):
            f.b.refresh_delivered_proactive(raw,runtime,1,embedder=Model([0.,0.]))
        with self.assertRaises(RetrievalShadowError):
            f.b.search_lane('synthetic','raw_event',embedder=Model([0.,0.]))

    def test_persistent_dimensions_are_shared_across_raw_and_memory_lanes(self):
        f = generation_fixture.GenerationTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        raw,runtime = f.raw_fixture()
        f.b.rebuild_memory()
        with self.assertRaises(RetrievalShadowError): f.b.rebuild_raw(raw,runtime,embedder=Model([1.,0.,0.]))
        self.assertTrue(all(r['vector'] is None for r in f.chunks() if r['source_kind']=='raw_event'))


if __name__ == '__main__': unittest.main()
