"""F5：仅临时合成数据；默认来源隔离、显式 owner 管理与真实 SQLite 锁。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from thalemora.app import Application
from thalemora.identity import OwnerPrincipal
from thalemora.memory.revision_store import create_card, RevisionConflict


class SyntheticEmbedder:
    def __init__(self, callback):
        self.callback = callback

    def encode_many(self, texts, *, batch_size=16):
        return self.callback(texts)

    def encode_one(self, text):
        return [1., 0.]


class ApplicationScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="f5-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(Path(self.tmp.name) / "state")
        self.agent = self.app.register_agent("合成同一Agent")["agent_id"]
        self.other = self.app.register_agent("合成第二Agent")["agent_id"]
        for agent in (self.agent, self.other):
            for app, channel in (("A", "main"), ("B", "main"), ("A", "private")):
                self.app.grant_source(agent, app, channel)
        self.path = self.app.memory_path(self.agent, "A", "main")
        self.net = patch("thalemora.app.providers.urlopen", side_effect=AssertionError("禁止联网"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def event(self, app="A", eid="home", text=None, version=1, deleted=False, channel="main", agent=None):
        self.app.import_history(agent or self.agent, app, [dict(app=app, channel=channel,
            source_event_id=eid, text=text if text is not None else (None if deleted else "合成" + app + "青鹭约定"),
            version=version, deleted=deleted, role="user", occurred_at="2001-01-01T00:00:00Z")], channel)

    def remember(self, app="A", eid="home", channel="main", agent=None):
        self.event(app, eid, channel=channel, agent=agent)
        return self.app.remember(agent or self.agent, app, eid, channel)

    def ref(self, app="A", **changes):
        return dict(dict(kind="archive_quote", agent_id=self.agent, app=app, channel="main",
                         source_event_id="home", version=1), **changes)

    def legacy(self, refs, content="合成旧卡青鹭约定"):
        return create_card(self.path, content=content, origin="auto_writer", source="auto",
                           evidence_rows=[dict(source_kind="derived", source_ref=json.dumps(r)) for r in refs])

    def test_default_filters_before_memory_pagination_owner_without_arbitrary_grant(self):
        b = self.remember("B")
        a = [self.remember("A", "a" + str(i)) for i in range(3)]
        self.remember(channel="private")
        self.remember(agent=self.other)
        self.assertEqual([r["id"] for r in self.app.memory(self.agent, "A", limit=1)], [a[0]["card_id"]])
        pages = sum((self.app.memory(self.agent, "A", limit=1, offset=i) for i in range(4)), [])
        self.assertEqual([r["id"] for r in pages], [r["card_id"] for r in a])
        self.assertEqual(len(self.app.memory(self.agent, scope="owner-global")), 4)
        self.assertEqual(self.app.memory(self.agent, "B")[0]["id"], b["card_id"])
        for method in (self.app.memory, self.app.water):
            with self.assertRaises(ValueError):
                method(self.agent, "A", scope="invalid")

    def test_remember_scope_revision_and_evidence_are_one_transaction(self):
        card = self.remember()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT app,channel FROM archive_card_scopes WHERE card_id=?",
                                        (card["card_id"],)).fetchone(), ("A", "main"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM card_evidence WHERE revision_id=?",
                                        (card["current_revision_id"],)).fetchone()[0], 1)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER reject_scope BEFORE INSERT ON archive_card_scopes BEGIN SELECT RAISE(ABORT,'synthetic scope failure'); END")
        self.event(eid="rollback")
        with self.assertRaises(sqlite3.IntegrityError):
            self.app.remember(self.agent, "A", "rollback")
        self.assertEqual(len(self.app.memory(self.agent, "A")), 1)
        with closing(sqlite3.connect(self.path)) as db:
            for table in ('cards', 'card_revisions', 'card_evidence', 'archive_card_scopes'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 1)

    def test_foreign_evidence_and_revision_never_expose_body_ref_or_current_revision(self):
        b = self.remember("B")
        for call in (lambda: self.app.evidence(self.agent, "A", b["card_id"]),
                     lambda: self.app.revise_memory(self.agent, "A", b["card_id"], "外域写入", -1)):
            with self.assertRaises(PermissionError) as denied:
                call()
            self.assertNotIn(str(b["current_revision_id"]), str(denied.exception))
            self.assertNotIn("current revision", str(denied.exception))
        self.assertEqual(self.app.memory(self.agent, "B")[0]["content"], "合成B青鹭约定")

    def test_old_unique_ownership_survives_deleted_source_unknown_conflict_owner_only(self):
        self.event()
        known = self.legacy([self.ref()])
        unknown = [self.legacy(refs) for refs in ([], ["free text"], [None],
            [dict(self.ref(), channel=None)], [dict(self.ref(), agent_id=self.other)],
            [self.ref(), self.ref("B")])]
        self.event(version=2, deleted=True)
        self.assertEqual([r["id"] for r in self.app.memory(self.agent, "A")], [known["card_id"]])
        all_cards = self.app.memory(self.agent, scope="owner-global")
        self.assertEqual(len(all_cards), 7)
        self.assertEqual(all_cards[0]["ownership"]["state"], "known")
        self.assertTrue(all(c["ownership"]["state"] in {"unknown", "conflict"} for c in all_cards[1:]))
        for card in unknown:
            with self.assertRaises(PermissionError):
                self.app.evidence(self.agent, "A", card["card_id"])
        changed = self.app.revise_memory(self.agent, "A", known["card_id"], "人工独立青鹭确认", known["current_revision_id"])
        self.assertTrue(changed["changed"])
        self.assertIn("人工独立", self.app.recall_for_turn(self.agent, "A", "青鹭")["rendered"])
        card = unknown[0]
        self.app.revise_memory(self.agent, None, card["card_id"], "未知人工青鹭", card["current_revision_id"], scope="owner-global")
        self.assertNotIn(card["card_id"], [r["id"] for r in self.app.memory(self.agent, "A")])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT * FROM archive_card_scopes WHERE card_id=?", (card["card_id"],)).fetchone())

    def test_persistent_conflict_and_later_foreign_manual_marker_are_not_claimed(self):
        card = self.remember()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE archive_card_scopes SET app='B' WHERE card_id=?", (card["card_id"],))
        self.assertEqual(self.app.memory(self.agent, "A"), [])
        self.assertEqual(self.app.memory(self.agent, "B"), [])
        self.assertEqual(self.app.memory(self.agent, scope="owner-global")[0]["ownership"]["state"], "conflict")
        self.assertEqual(self.app.recall_for_turn(self.agent, "A", "青鹭")["memories"], [])

    def test_owner_edit_retains_card_app_and_cas_noop(self):
        card = self.remember("B")
        result = self.app.revise_memory(self.agent, "A", card["card_id"], "人工B青鹭", card["current_revision_id"], scope="owner-global")
        ref = json.loads(self.app.evidence(self.agent, "B", card["card_id"])[-1]["source_ref"])
        self.assertEqual((ref["app"], ref["channel"]), ("B", "main"))
        self.assertFalse(self.app.revise_memory(self.agent, "B", card["card_id"], "人工B青鹭", result["current_revision_id"])["changed"])
        with self.assertRaises(RevisionConflict):
            self.app.revise_memory(self.agent, "B", card["card_id"], "CAS拒绝", card["current_revision_id"])
        self.assertEqual(self.app.memory(self.agent, "A"), [])

    def test_project_evidence_before_pagination_not_just_outside_scope_label(self):
        card = self.remember()
        with closing(sqlite3.connect(self.path)) as db, db:
            for ref in (self.ref("B", quote="不应泄漏外域正文"), self.ref()):
                db.execute("INSERT INTO card_evidence(revision_id,source_kind,source_ref) VALUES(?, 'derived', ?)",
                           (card["current_revision_id"], json.dumps(ref)))
        # 首修订含两域证据，整体归属冲突，source连current revision都不能看到。
        with self.assertRaises(PermissionError):
            self.app.evidence(self.agent, "A", card["card_id"], limit=1, offset=1)
        owner = self.app.evidence(self.agent, None, card["card_id"], scope="owner-global")
        self.assertEqual(len(owner), 3)
        self.assertEqual(json.loads(owner[1]["source_ref"])["quote"], "不应泄漏外域正文")

    def test_water_requires_explicit_owner_and_no_source_tree(self):
        card = self.remember()
        calls = [lambda: self.app.water(self.agent, "A"),
                 lambda: self.app.water_memberships(self.agent, "A"),
                 lambda: self.app.water_node(self.agent, "A", "l", "lake", "湖"),
                 lambda: self.app.water_move(self.agent, "A", card["card_id"], None, "调整")]
        for call in calls:
            with self.assertRaisesRegex(ValueError, "scope_not_supported"):
                call()
        self.app.water_node(self.agent, None, "l", "lake", "湖", scope="owner-global")
        self.app.water_node(self.agent, None, "s", "stream", "溪", parent_node_id="l", scope="owner-global")
        self.app.water_move(self.agent, None, card["card_id"], "s", "调整", scope="owner-global")
        self.assertEqual(len(self.app.water(self.agent, scope="owner-global")), 2)
        self.assertEqual(self.app.water_memberships(self.agent, scope="owner-global")[0]["card_id"], card["card_id"])

    def test_local_indexes_filter_before_embedding_topk_and_have_distinct_namespaces(self):
        a, b = self.remember(), self.remember("B")
        seen = []
        def embed(texts):
            seen.extend(texts)
            return [[1., 0.] for _ in texts]
        self.app.embedder = ("synthetic", SyntheticEmbedder(embed))
        self.app.build_local_index(self.agent, "A")
        self.assertEqual(seen, ["合成A青鹭约定"])
        self.assertEqual([r["source_id"] for r in self.app.local_search(self.agent, "A", "青鹭", limit=1)], [a["card_id"]])
        seen.clear()
        self.app.build_local_index(self.agent, "B")
        self.assertEqual(seen, ["合成B青鹭约定"])
        seen.clear()
        self.app.build_local_index(self.agent, scope="owner-global")
        self.assertEqual(set(seen), {"合成A青鹭约定", "合成B青鹭约定"})
        paths = [str(self.app.local_index(self.agent, app).db_path) for app in ("A", "B")]
        paths.append(str(self.app.local_index(self.agent, scope="owner-global").db_path))
        self.assertEqual(len(set(paths)), 3)
        self.assertNotIn(str(self.path.with_name("retrieval_main.sqlite")), paths)

    def test_model_scope_change_is_rechecked_before_local_and_recall_output(self):
        card = self.remember()
        def embed(texts):
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE archive_card_scopes SET app='B' WHERE card_id=?", (card["card_id"],))
            return [[1., 0.] for _ in texts]
        self.app.embedder = ("synthetic", SyntheticEmbedder(embed))
        self.assertEqual(self.app.local_search(self.agent, "A", "青鹭"), [])
        self.assertEqual(self.app.recall_for_turn(self.agent, "A", "青鹭")["memories"], [])

    def test_recall_models_receive_only_source_and_final_scope_change_is_filtered(self):
        a = self.remember()
        self.remember('B')
        embedded, ranked = [], []
        self.app.providers.local_models['embedding'] = ('synthetic', SyntheticEmbedder(
            lambda texts: embedded.extend(texts) or [[1., 0.] for _ in texts]))
        self.app.providers.configure('embedding', mode='local', model='synthetic')
        def rerank(query, docs):
            ranked.extend(docs)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE archive_card_scopes SET app='B' WHERE card_id=?", (a['card_id'],))
            return [1] * len(docs)
        self.app.providers.local_models['rerank'] = ('synthetic', rerank)
        self.app.providers.configure('rerank', mode='local', model='synthetic')
        self.assertEqual(self.app.recall_for_turn(self.agent, 'A', '青鹭')['memories'], [])
        self.assertEqual(embedded, ['合成A青鹭约定'])
        self.assertEqual(ranked, ['合成A青鹭约定'])
        with self.assertRaises(PermissionError):
            self.app.recall_for_turn(self.agent, 'not-granted', '青鹭')
        self.assertEqual(embedded, ['合成A青鹭约定'])

    def test_revise_holds_memory_and_registry_write_locks_through_cas(self):
        card = self.remember()
        from thalemora.memory.revision_store import update_card_in_transaction
        outcomes = []
        def contender(path, sql, args=()):
            with closing(sqlite3.connect(path, timeout=.05)) as db, db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(sql, args)
                except sqlite3.OperationalError as exc:
                    self.assertIn("locked", str(exc))
                    return "locked"
                return "wrote"
        def checked(db, **kwargs):
            self.assertTrue(db.in_transaction)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(contender, self.path, "UPDATE archive_card_scopes SET app='B'"),
                           pool.submit(contender, self.app.registry.registry_path, "DELETE FROM sources WHERE agent_id=?", (self.agent,))]
                outcomes.extend(f.result(timeout=2) for f in futures)
            return update_card_in_transaction(db, **kwargs)
        with patch("thalemora.app.service.update_card_in_transaction", checked):
            self.app.revise_memory(self.agent, "A", card["card_id"], "人工锁内青鹭", card["current_revision_id"])
        self.assertEqual(outcomes, ["locked", "locked"])
        # 锁释放后实际改归属；下一次必须拒绝且不能泄漏CAS当前值。
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE archive_card_scopes SET app='B'")
        with self.assertRaises(PermissionError):
            self.app.revise_memory(self.agent, "A", card["card_id"], "不可写", -1)

    def test_real_concurrent_scope_writer_serializes_after_revision_commit(self):
        from threading import Event
        from thalemora.memory.revision_store import update_card_in_transaction
        card = self.remember()
        attempted = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None
            def writer():
                with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
                    attempted.set()
                    db.execute('BEGIN IMMEDIATE')
                    # 能看到新修订，证明写入顺序是CAS提交后，归属才改变。
                    revision = db.execute('SELECT current_revision_id FROM cards WHERE id=?', (card['card_id'],)).fetchone()[0]
                    db.execute("UPDATE archive_card_scopes SET app='B' WHERE card_id=?", (card['card_id'],))
                    return revision
            def checked(db, **kwargs):
                nonlocal future
                future = pool.submit(writer)
                self.assertTrue(attempted.wait(2))
                return update_card_in_transaction(db, **kwargs)
            with patch('thalemora.app.service.update_card_in_transaction', checked):
                changed = self.app.revise_memory(self.agent, 'A', card['card_id'], '合成并发人工青鹭', card['current_revision_id'])
            self.assertEqual(future.result(timeout=3), changed['current_revision_id'])
        self.assertEqual(self.app.memory(self.agent, 'A'), [])
        self.assertEqual(self.app.memory(self.agent, 'B'), [])  # 表/创建来源冲突，不改投B
        with self.assertRaises(PermissionError):
            self.app.revise_memory(self.agent, 'A', card['card_id'], '不可写', -1)

    def test_later_manual_marker_conflict_and_legacy_missing_agent(self):
        from thalemora.memory.revision_store import update_card
        self.event()
        legacy_ref = self.ref()
        del legacy_ref['agent_id']  # 已知旧envelope；可信per-Agent文件上下文
        card = self.legacy([legacy_ref])
        self.assertEqual(self.app.memory(self.agent, 'A')[0]['id'], card['card_id'])
        update_card(self.path, card_id=card['card_id'], expected_current_revision_id=card['current_revision_id'],
            content='合成错误归属的手改', origin='manual_ui', change_type='manual_edit',
            evidence_source_kind='manual', evidence_source_ref=json.dumps(dict(kind='mcp_explicit_manual_revision', app='B', channel='main')))
        self.assertEqual(self.app.memory(self.agent, 'A'), [])
        self.assertEqual(self.app.memory(self.agent, 'B'), [])
        self.assertEqual(self.app.memory(self.agent, scope='owner-global')[0]['ownership']['state'], 'conflict')
        self.assertEqual(self.app.recall_for_turn(self.agent, 'A', '手改')['memories'], [])

    def test_later_external_evidence_filtered_before_page_not_echoed(self):
        from thalemora.memory.revision_store import update_card
        self.event()
        card = self.legacy([self.ref()])
        update_card(self.path, card_id=card['card_id'], expected_current_revision_id=card['current_revision_id'],
            content='合成A青鹭新版', origin='auto_writer', change_type='correct', evidence_rows=[
                dict(source_kind='derived', source_ref=json.dumps(self.ref('B', quote='外域正文不返回'))),
                dict(source_kind='derived', source_ref=json.dumps(self.ref()))])
        source = self.app.evidence(self.agent, 'A', card['card_id'])
        self.assertEqual(len(source), 2)
        pages = sum((self.app.evidence(self.agent, 'A', card['card_id'], limit=1, offset=i) for i in range(3)), [])
        self.assertEqual(pages, source)
        self.assertTrue(all(json.loads(e['source_ref'])['app'] == 'A' for e in pages))
        self.assertEqual(len(self.app.evidence(self.agent, None, card['card_id'], scope='owner-global')), 3)

    def test_owner_extensions_across_apps_and_source_model_inputs(self):
        from thalemora.memory.revision_store import create_extension
        for app in ('A', 'B'):
            self.event(app)
            self.event(app, 'delta', '合成' + app + '独立扩展')
            card = self.legacy([self.ref(app)], content='合成' + app + '青鹭基卡')
            create_extension(self.path, card_id=card['card_id'], expected_base_revision_id=card['current_revision_id'],
                content='合成' + app + '独立扩展', source='auto', origin='auto_writer', same_memory_confidence=.99,
                evidence_rows=[dict(source_kind='derived', source_ref=json.dumps(self.ref(app, source_event_id='delta')))])
        seen = []
        self.app.embedder = ('synthetic', SyntheticEmbedder(lambda texts: seen.extend(texts) or [[1., 0.] for _ in texts]))
        self.app.build_local_index(self.agent, 'A')
        self.assertEqual(set(seen), {'合成A青鹭基卡', '合成A独立扩展'})
        owner = self.app.memory(self.agent, scope='owner-global')
        self.assertEqual([c['extensions'][0]['content'] for c in owner], ['合成A独立扩展', '合成B独立扩展'])
        seen.clear()
        self.app.build_local_index(self.agent, scope='owner-global')
        self.assertEqual(set(seen), {'合成A青鹭基卡', '合成B青鹭基卡', '合成A独立扩展', '合成B独立扩展'})
        self.assertTrue(any('extension_id' in h['provenance'] for h in self.app.local_search(self.agent, None, '独立扩展', scope='owner-global')))

    def test_private_second_agent_local_search_and_maintenance_share_ownership(self):
        a, b = self.remember(), self.remember('B')
        self.event('A', 'private-only', '合成私有紫荆', channel='private')
        self.app.remember(self.agent, 'A', 'private-only', 'private')
        self.event('A', 'agent-only', '合成第二Agent红杉', agent=self.other)
        self.app.remember(self.other, 'A', 'agent-only')
        self.legacy(['bad JSON ownership'])
        for agent, app, channel, text in [(self.agent, 'A', 'main', '合成A青鹭约定'),
                (self.agent, 'B', 'main', '合成B青鹭约定'),
                (self.agent, 'A', 'private', '合成私有紫荆'),
                (self.other, 'A', 'main', '合成第二Agent红杉')]:
            with self.subTest(agent=agent, app=app, channel=channel):
                seen = []
                self.app.embedder = ('synthetic', SyntheticEmbedder(lambda texts: seen.extend(texts) or [[1., 0.] for _ in texts]))
                self.app.build_local_index(agent, app, channel)
                self.assertEqual(seen, [text])
                self.assertEqual([h['text'] for h in self.app.local_search(agent, app, '合成', channel)], [text])
        from thalemora.app.maintenance import freeze, setup, reflect
        from test_pre6_maintenance import SyntheticProvider
        self.assertEqual([r['card_id'] for r in freeze(self.path, self.agent, 'A', 'main')], [a['card_id']])
        _, ops, _ = setup(self.app, self.agent, 'A', 'main')
        with closing(sqlite3.connect(ops)) as db, db:
            db.execute("INSERT INTO maintenance_reflections VALUES('synthetic-cross-app',?,?,NULL,'pending',NULL,NULL)",
                       (b['card_id'], b['current_revision_id']))
        provider = SyntheticProvider()
        errors = reflect(self.app, self.agent, 'A', 'main', ops, provider, 20)
        self.assertTrue(any('card_outside_scope' in e for e in errors))
        self.assertEqual(provider.calls, [])

    def test_owner_evidence_does_not_resolve_current_private_or_other_agent_text(self):
        self.event()
        self.event('A', 'private', '合成private当前正文', channel='private')
        card = self.legacy([self.ref(channel='private', source_event_id='private', quote='历史引用')])
        owner = self.app.evidence(self.agent, None, card['card_id'], scope='owner-global')
        self.assertEqual(owner[0]['resolution'], 'outside_scope')
        self.assertIsNone(owner[0]['current_source_text'])
        with self.assertRaises(PermissionError):
            self.app.evidence(self.agent, 'A', card['card_id'])

    def test_owner_global_still_checks_agent_owner_and_channel(self):
        self.remember()
        self.app.owner = OwnerPrincipal("foreign-owner")
        with self.assertRaises(PermissionError):
            self.app.memory(self.agent, scope="owner-global")
        self.app.owner = OwnerPrincipal("local-owner")
        with self.assertRaises(ValueError):
            self.app.memory(self.agent, channel="arbitrary", scope="owner-global")
        with self.assertRaises(PermissionError):
            self.app.memory(self.agent, "not-granted")


if __name__ == "__main__":
    unittest.main()
