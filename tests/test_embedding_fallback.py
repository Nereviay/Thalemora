"""F7 optional semantic failures only; explicit model/network substitutes, no retries."""
from contextlib import closing
import io
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from thalemora.app.providers import ProviderUnavailable
from thalemora.identity import AccessDenied
from thalemora.retrieval.core import RetrievalIndex, RetrievalShadowError
import test_p8_recall as fixtures


class FailingModel(fixtures.Embedder):
    def __init__(self, *, document=False, error=None):
        super().__init__()
        self.document = document
        self.error = error or ProviderUnavailable('synthetic embedding unavailable')
        self.attempts = []
    def encode_many(self, texts, **kw):
        self.attempts.append('document')
        if self.document: raise self.error
        return [[3.,4.] for t in texts]
    def encode_one(self, text):
        self.attempts.append('query')
        raise self.error


class EmbeddingFallbackTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RecallTests('runTest'); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.card('tea','我喜欢乌龙茶')
        self.f.insert('raw','乌龙茶在合成茶店有售')
        self.app = self.f.app

    def configure(self, model, name='synthetic'):
        self.app.providers.local_models['embedding'] = (name,model)
        self.app.providers.configure('embedding',mode='local',model=name)

    def assert_fallback(self, result):
        self.assertTrue(result['memories'])
        self.assertEqual(result['archive'][0]['id'],'raw')
        self.assertTrue(result['rendered'])
        embedding = [e for e in result['errors'] if e['module']=='embedding']
        self.assertEqual(len(embedding),1)
        self.assertEqual(embedding[0]['code'],'embedding_unavailable')
        self.assertFalse(any('vector' in str(m) for m in result['memories']))

    def test_document_embedding_failure_keeps_lexical_and_raw_without_query_retry(self):
        model = FailingModel(document=True); self.configure(model)
        with patch('thalemora.app.providers.urlopen',side_effect=AssertionError('no payment')):
            self.assert_fallback(self.f.recall('乌龙茶'))
        self.assertEqual(model.attempts,['document'])

    def test_query_embedding_failure_keeps_lexical_and_raw_without_retry(self):
        model = FailingModel(); self.configure(model)
        self.assert_fallback(self.f.recall('乌龙茶'))
        self.assertEqual(model.attempts,['document','query'])

    def test_missing_and_disabled_embedding_are_explicit_lexical_no_loader(self):
        for config in ({'mode':'disabled'}, {'mode':'local','model':'absent-synthetic-model'}, {}):
            with self.subTest(config=config):
                if config: self.app.providers.configure('embedding',**config)
                with patch('thalemora.app.providers.urlopen',side_effect=AssertionError('no network')):
                    self.assert_fallback(self.f.recall('乌龙茶'))
        self.assertEqual(self.app.providers.audit(),[])

    def test_timeout_and_connection_failures_are_optional_but_no_retry(self):
        for exc in (TimeoutError('synthetic'), ConnectionError('synthetic')):
            with self.subTest(exc=exc):
                model = FailingModel(document=True,error=exc); self.configure(model, type(exc).__name__)
                self.assert_fallback(self.f.recall('乌龙茶'))
                self.assertEqual(model.attempts,['document'])

    def api(self, **kw):
        self.app.providers.configure('embedding',mode='api',model='fake',base_url='https://synthetic.invalid/v1',
            key_env='F7_FAKE_KEY',call_budget=kw.get('calls',10),token_budget=kw.get('tokens',100))

    def test_api_timeout_attempt_is_one_with_real_provider_audit(self):
        with patch.dict(os.environ,{'F7_FAKE_KEY':'synthetic'}):
            self.api()
            with patch('thalemora.app.providers.urlopen',side_effect=TimeoutError('synthetic')) as net:
                self.assert_fallback(self.f.recall('乌龙茶'))
            self.assertEqual(net.call_count,1)
        self.assertEqual([a['status'] for a in self.app.providers.audit()],['error'])

    def test_call_and_token_budget_exhaustion_do_not_attempt_network(self):
        with patch.dict(os.environ,{'F7_FAKE_KEY':'synthetic'}):
            for kwargs in ({'calls':0},{'tokens':0}):
                with self.subTest(kwargs=kwargs):
                    self.api(**kwargs)
                    with patch('thalemora.app.providers.urlopen',side_effect=AssertionError('no payment')) as net:
                        self.assert_fallback(self.f.recall('乌龙茶'))
                    net.assert_not_called()

    def test_budget_exhausted_between_document_and_query_falls_back(self):
        payload = {'model':'fake','data':[{'embedding':[30.,40.]}],'usage':{'total_tokens':8}}
        with patch.dict(os.environ,{'F7_FAKE_KEY':'synthetic'}):
            self.api(calls=1)
            with patch('thalemora.app.providers.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())) as net:
                self.assert_fallback(self.f.recall('乌龙茶'))
            self.assertEqual(net.call_count,1)
        self.assertEqual(sorted(a['status'] for a in self.app.providers.audit()),['blocked','success'])

    def test_prior_model_vectors_are_not_used_if_new_model_build_fails(self):
        old = fixtures.Embedder(); self.configure(old,'old')
        self.assertTrue(self.f.recall('量子计算')['memories'])
        model = FailingModel(document=True); self.configure(model,'new')
        result = self.f.recall('量子计算')
        self.assertEqual(result['rendered'],'')
        self.assertEqual(result['errors'][0]['code'],'embedding_unavailable')
        self.assertEqual(model.attempts,['document'])
        self.assertEqual(len(old.inputs),2)

    def test_disabled_mode_does_not_load_or_call_previously_broken_model(self):
        model = FailingModel(document=True); self.configure(model)
        self.app.providers.configure('embedding',mode='disabled',model='synthetic')
        self.assert_fallback(self.f.recall('乌龙茶'))
        self.assertEqual(model.attempts,[])

    def test_explicit_lexical_archive_search_never_touches_configured_broken_model(self):
        model=FailingModel(document=True); self.configure(model)
        with patch('thalemora.retrieval.models.configured_model',side_effect=AssertionError('must not load model')):
            result=self.app.search(self.f.agent,'demo','乌龙茶')
        self.assertEqual({r['source_event_id'] for r in result},{'tea','raw'})
        self.assertEqual(model.attempts,[])

    def test_authorization_and_source_scope_fail_before_any_model(self):
        model = FailingModel(document=True); self.configure(model)
        for query in ('乌龙茶',''):
            with self.assertRaises(AccessDenied): self.app.recall_for_turn(self.f.agent,'ungranted',query)
        self.assertEqual(model.attempts,[])

    def test_db_permission_and_programming_errors_are_not_embedding_fallback(self):
        self.configure(fixtures.Embedder())
        for exc in (sqlite3.OperationalError('synthetic db'),PermissionError('synthetic'),
                    TypeError('synthetic code'),ValueError('synthetic code'),RuntimeError('synthetic code')):
            with self.subTest(exc=exc):
                with patch('thalemora.retrieval.recall.ensure_current_index',side_effect=exc):
                    with self.assertRaises(type(exc)): self.f.recall('乌龙茶')
        for exc in (TypeError('host bug'),ValueError('host bug'),RuntimeError('host bug'),PermissionError('host access')):
            model = FailingModel(document=True,error=exc); self.configure(model,type(exc).__name__)
            with self.assertRaises(type(exc)): self.f.recall('乌龙茶')

    def test_invalid_vector_contract_still_fails_closed_in_application(self):
        class Zero(fixtures.Embedder):
            def encode_one(self,text): return [0.,0.]
        self.configure(Zero())
        with self.assertRaises(RetrievalShadowError): self.f.recall('乌龙茶')

    def test_local_search_remains_fail_closed(self):
        model = FailingModel(document=True)
        self.app.embedder = ('synthetic',model)
        with self.assertRaises(ProviderUnavailable): self.app.local_search(self.f.agent,'demo','乌龙茶')

    def test_rerank_existing_fallback_survives_embedding_failure(self):
        self.configure(FailingModel(document=True))
        def unavailable(query,docs): raise ProviderUnavailable('synthetic rerank')
        self.app.providers.local_models['rerank'] = ('r',unavailable)
        self.app.providers.configure('rerank',mode='local',model='r')
        result = self.f.recall('乌龙茶'); self.assert_fallback(result)
        self.assertEqual([e['module'] for e in result['errors']],['embedding','rerank'])

    def test_api_transport_programming_and_permission_errors_are_not_outages(self):
        with patch.dict(os.environ,{'F7_FAKE_KEY':'synthetic'}):
            self.api()
            for exc in (TypeError('transport code'),ValueError('transport code'),
                        RuntimeError('transport code'),PermissionError('host access')):
                with self.subTest(exc=exc),patch('thalemora.app.providers.urlopen',side_effect=exc):
                    with self.assertRaises(type(exc)): self.f.recall('乌龙茶')

    def test_api_declared_wrong_model_and_bad_vector_are_not_outages(self):
        for model,vector in (('wrong-model',[1.,0.]),('fake',[0.,0.]),('fake',[float('nan'),1.]),('fake',[[1.,0.]])):
            with self.subTest(model=model,vector=vector),patch.dict(os.environ,{'F7_FAKE_KEY':'synthetic'}):
                self.api()
                response={'model':model,'data':[{'embedding':vector}],'usage':{'total_tokens':2}}
                with patch('thalemora.app.providers.urlopen',side_effect=lambda *a,**kw:io.BytesIO(json.dumps(response).encode())) as net:
                    with self.assertRaises(RetrievalShadowError): self.f.recall('乌龙茶')
                self.assertEqual(net.call_count,1)
                self.assertEqual(self.app.providers.audit()[0]['status'],'error')

    def test_api_identity_switch_during_call_fails_instead_of_labeling_new_model_as_old(self):
        from thalemora.retrieval.models import configured_model
        with patch.dict(os.environ,{'F7_FAKE_KEY':'synthetic'}):
            self.api()
            identity,model = configured_model(self.app.providers,'embedding')
            self.app.providers.configure('embedding',mode='api',model='new-model',base_url='https://synthetic.invalid/v1',
                key_env='F7_FAKE_KEY',call_budget=10,token_budget=100)
            with patch('thalemora.app.providers.urlopen',side_effect=AssertionError('must not call new model')) as net:
                with self.assertRaises(RetrievalShadowError): model.encode_one('synthetic')
            net.assert_not_called()
        self.assertEqual(self.app.providers.audit(),[])

    def test_local_library_runtime_failure_is_translated_only_at_library_boundary(self):
        from thalemora.retrieval.models import LocalEmbedder
        class Native:
            def encode(self,*args,**kwargs): raise RuntimeError('synthetic native inference')
        self.configure(LocalEmbedder(Native()))
        self.assert_fallback(self.f.recall('乌龙茶'))
        class Programming:
            def encode(self,*args,**kwargs): raise TypeError('synthetic programming error')
        self.configure(LocalEmbedder(Programming()),'bug')
        with self.assertRaises(TypeError): self.f.recall('乌龙茶')
        class Contract:
            def encode(self,*args,**kwargs):
                from thalemora.retrieval.core import VectorContractError
                raise VectorContractError('synthetic known numerical contract')
        self.configure(LocalEmbedder(Contract()),'contract')
        with self.assertRaises(RetrievalShadowError): self.f.recall('乌龙茶')

    def test_failed_partial_batch_does_not_publish_partial_vectors_or_retry(self):
        self.f.card('other','乌龙茶还有另一份合成记忆')
        calls=[]
        def response(*args,**kw):
            calls.append(1)
            if len(calls)>1: raise TimeoutError('second document fails')
            return io.BytesIO(json.dumps({'data':[{'embedding':[30.,40.]}],'usage':{'total_tokens':2}}).encode())
        with patch.dict(os.environ,{'F7_FAKE_KEY':'synthetic'}):
            self.api()
            with patch('thalemora.app.providers.urlopen',side_effect=response): self.assert_fallback(self.f.recall('乌龙茶'))
        self.assertEqual(len(calls),2)
        path=self.app.memory_path(self.f.agent,'demo','main').with_name('recall_1.sqlite')
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM build_receipts').fetchone()[0],0)

    def test_source_deleted_during_failed_embedding_never_reappears_in_fallback(self):
        model=FailingModel(document=True)
        original=model.encode_many
        def remove(texts,**kw):
            self.f.insert('tea',None,version=2,deleted=True)
            return original(texts,**kw)
        model.encode_many=remove
        self.configure(model)
        result=self.f.recall('乌龙茶')
        self.assertEqual(result['memories'],[])
        self.assertEqual(result['archive'][0]['id'],'raw')
        self.assertEqual(result['errors'][0]['code'],'embedding_unavailable')
        self.assertEqual(model.attempts,['document'])


if __name__ == '__main__': unittest.main()
