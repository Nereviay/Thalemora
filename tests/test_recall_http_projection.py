"""Run the exact recall route AST in isolation, NOT full HTTP/MCP startup.

The host blocks pydantic_core at import. No stubbing/bypassing that dependency:
extract only the nested route, using real Request/Response, AnyIO, tokens and Application.
Full mounted HTTP tests remain separately required and are still attempted.
"""
import ast
from contextlib import closing
from functools import partial
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from thalemora.app.providers import ProviderUnavailable
from thalemora.app.recall_tokens import InvalidRecallToken
import test_p8_recall as fixtures


def isolated_route(service):
    path = Path(__file__).resolve().parents[1]/'src/thalemora/app/http.py'
    tree = ast.parse(path.read_text(), filename=str(path))
    create = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='create_http')
    route = next(n for n in create.body if isinstance(n, ast.AsyncFunctionDef) and n.name=='recall')
    code = compile(ast.fix_missing_locations(ast.Module(body=[route],type_ignores=[])),str(path),'exec')
    namespace = dict(service=service,partial=partial,json=json,anyio=anyio,JSONResponse=JSONResponse,
        InvalidRecallToken=InvalidRecallToken,ProviderUnavailable=ProviderUnavailable,RECALL_TIMEOUT_SECONDS=5)
    exec(code,namespace)
    return namespace['recall']


class RecallHTTPProjectionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RecallTests('runTest'); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.card('tea','我喜欢乌龙茶'); self.f.insert('raw','乌龙茶在合成店铺有售')
        self.service = self.f.app
        self.token = self.service.issue_recall_token(self.f.agent,'demo')['token']
        self.route = isolated_route(self.service)

    def request(self, message='乌龙茶', token=None):
        async def invoke():
            async def receive():
                return {'type':'http.request','body':json.dumps({'message':message}).encode(),'more_body':False}
            request = Request({'type':'http','method':'POST','path':'/api/recall',
                'headers':[(b'authorization',('Bearer '+(token or self.token)).encode())]},receive)
            response = await self.route(request)
            return response.status_code,json.loads(response.body)
        return anyio.run(invoke)

    def test_missing_embedding_warning_not_mislabeled_rerank_and_minimal_refs(self):
        status,data = self.request()
        self.assertEqual(status,200)
        self.assertEqual(data['errors'],['embedding_unavailable'])
        self.assertEqual(set(data),{'rendered','memories','archive','empty','errors'})
        self.assertEqual(set(data['memories'][0]),{'id','revision_id'})
        self.assertEqual(data['archive'],[{'id':'raw','version':1}])

    def test_empty_lexical_result_still_has_explicit_semantic_warning(self):
        status,data = self.request('量子计算')
        self.assertEqual(status,200)
        self.assertEqual(data['errors'],['embedding_unavailable'])
        self.assertEqual(data['rendered'],'')
        self.assertTrue(data['empty'])

    def test_combined_optional_warnings_deduplicate_and_redact(self):
        result=dict(rendered='synthetic',memories=[],archive=[],errors=[
            {'module':'embedding','error':'private-detail'}, {'module':'rerank','error':'private-detail'},
            {'module':'embedding','error':'private-detail'}])
        with patch.object(self.service,'recall_for_turn',return_value=result):
            status,data = self.request()
        self.assertEqual(status,200)
        self.assertEqual(data['errors'],['embedding_unavailable','rerank_unavailable'])
        self.assertNotIn('private-detail',json.dumps(data))

    def test_unknown_internal_errors_are_not_projected_as_optional_success(self):
        for errors in (['private-detail'],[{'module':'database','error':'private-detail'}]):
            with patch.object(self.service,'recall_for_turn',return_value=dict(
                    rendered='not trustworthy',memories=[],archive=[],errors=errors)):
                self.assertEqual(self.request(),(503,{'error':'recall_unavailable'}))

    def test_bad_credentials_and_revoked_scope_still_fail(self):
        self.assertEqual(self.request(token='invalid'),(401,{'error':'invalid_recall_token'}))
        with closing(sqlite3.connect(self.service.registry.registry_path)) as db,db:
            db.execute('DELETE FROM sources WHERE agent_id=? AND app_id=?',(self.f.agent,'demo'))
        self.assertEqual(self.request(),(403,{'error':'source_not_authorized'}))

    def test_internal_error_and_contract_error_do_not_leak_or_become_warning(self):
        from thalemora.retrieval.core import VectorContractError
        for exc in (sqlite3.OperationalError('private-detail'),VectorContractError('private-detail'),RuntimeError('private-detail')):
            with patch.object(self.service,'recall_for_turn',side_effect=exc):
                self.assertEqual(self.request(),(500,{'error':'recall_failed'}))


if __name__ == '__main__': unittest.main()
