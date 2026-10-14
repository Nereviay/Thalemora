"""P3 纯合成宿主及隔离数据库验收；不导入生产模块，不访问生产数据。"""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thalemora.identity.registry import AccessDenied, IdentityRegistry, OwnerPrincipal
from thalemora.inner_world.host import AgentRequired, HostEvent, HostSession
from thalemora.inner_world.store import InnerStore
from thalemora.diary import Diary
from thalemora.dream import Dream
from thalemora.whisper import Whisper
from thalemora.emotion import Emotion, DIM_KEYS, DIM_RANGES
from thalemora.reflection import Reflection
from thalemora.continuity import Continuity
from thalemora.persona import Persona
from thalemora.self_model import (
    AuthoritativeRecord, ContractValidationError, EvidenceResolutionError,
    GenerationState, InvocationContext,
)
from thalemora.self_model.host import HostedSelfModel


NOW = "2026-10-14T08:00:00+08:00"
PROJECT = Path(__file__).resolve().parents[1]


class SyntheticHost:
    """独立已落库宿主夹具。接口只按稳定 Agent principal 返回事实。

    测试写入此宿主库模拟已认证 Runtime，调用方只持引用；不能靠请求体 actor。
    """
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE events(agent TEXT, ref TEXT, record TEXT, envelope TEXT,
                                    generation TEXT, PRIMARY KEY(agent,ref));
                CREATE TABLE epochs(agent TEXT, channel TEXT, epoch TEXT,
                                    PRIMARY KEY(agent,channel,epoch));
            """)
        self.serial = 0

    def confirm(self, agent, channel="main", epoch="epoch-1"):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR IGNORE INTO epochs VALUES(?,?,?)", (agent, channel, epoch))

    def confirmed_epoch(self, epoch_id, principal_id, channel):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT 1 FROM epochs WHERE agent=? AND channel=? AND epoch=?",
                              (principal_id, channel, epoch_id)).fetchone() is not None

    def add(self, agent, kind, payload, *, app="app-a", channel="main", epoch="epoch-1",
            status="completed", actor="assistant", outcome="succeeded", active=True,
            committed=True, source_type="canonical_reply", tool_name=None,
            branch="branch-1", duplicate=False):
        self.serial += 1
        generation_id = f"g-{self.serial}"
        call = f"call-{self.serial}"
        ref = f"synthetic:event-{self.serial}"
        context = InvocationContext(agent, generation_id, call, branch, epoch, "normal", NOW)
        record = AuthoritativeRecord(
            durable_ref=ref, source_type=source_type, actor=actor, root_event_id=call,
            generation_id=generation_id, canonical_state="active" if active else "superseded",
            outcome=outcome, committed=committed, occurred_at=NOW, surface="normal",
            content=json.dumps(payload, ensure_ascii=False), tool_name=tool_name,
            duplicate_suppressed=duplicate,
        )
        event = HostEvent(kind, app, channel, context)
        generation = GenerationState(generation_id, status, True, branch, (call,))
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (
                agent, ref, json.dumps(record.__dict__),
                json.dumps({**event.__dict__, "context": context.__dict__}),
                json.dumps(generation.__dict__),
            ))
        return ref

    def _row(self, ref, agent):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT record,envelope,generation FROM events WHERE agent=? AND ref=?",
                              (agent, ref)).fetchone()

    def record(self, durable_ref, principal_id):
        row = self._row(durable_ref, principal_id)
        return AuthoritativeRecord(**json.loads(row[0])) if row else None

    def event(self, durable_ref, principal_id):
        row = self._row(durable_ref, principal_id)
        if row is None:
            return None
        event = json.loads(row[1])
        event["context"] = InvocationContext(**event["context"])
        return HostEvent(**event)

    def generation_state(self, generation_id, principal_id):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT generation FROM events WHERE agent=? AND json_extract(generation,'$.generation_id')=? LIMIT 1",
                             (principal_id, generation_id)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        data["canonical_tool_call_event_ids"] = tuple(data["canonical_tool_call_event_ids"])
        return GenerationState(**data)

    def refs_for_generation(self, generation_id, principal_id):
        with sqlite3.connect(self.path) as db:
            return tuple(row[0] for row in db.execute(
                "SELECT ref FROM events WHERE agent=? AND json_extract(generation,'$.generation_id')=?",
                (principal_id, generation_id)))

    def replace_record(self, ref, agent, **updates):
        record = replace(self.record(ref, agent), source_hash=None, **updates)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE events SET record=? WHERE agent=? AND ref=?",
                       (json.dumps(record.__dict__), agent, ref))


class P3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p3-test-", dir=PROJECT)
        root = Path(self.tmp.name)
        self.registry = IdentityRegistry(root / "state")
        self.owner = OwnerPrincipal("synthetic-owner")
        self.other_owner = OwnerPrincipal("other-owner")
        self.a = self.registry.register_agent(self.owner, "同名 Agent").agent_id
        self.b = self.registry.register_agent(self.owner, "同名 Agent").agent_id
        for agent in (self.a, self.b):
            self.registry.grant_source(self.owner, agent, "app-a")
        self.registry.grant_source(self.owner, self.a, "app-a", "private")
        self.registry.grant_source(self.owner, self.a, "app-b")
        self.host = SyntheticHost(root / "host.sqlite")
        for agent, channel in ((self.a, "main"), (self.b, "main"), (self.a, "private")):
            for epoch in ("epoch-1", "epoch-2", "epoch-3"):
                self.host.confirm(agent, channel, epoch)
        self.session = self.session_for()
        self.store = InnerStore(self.session)
        self.addCleanup(self.tmp.cleanup)

    def session_for(self, *, agent=None, channel="main", app="app-a", role="agent", reader=True, owner=None):
        return HostSession(self.registry, owner or self.owner, agent or self.a, app,
                           channel, role, self.host if reader else None)

    def emit(self, kind, payload, **kwargs):
        return self.host.add(kwargs.pop("agent", self.a), kind, payload, **kwargs)

    def diary(self, text="合成日记", **kwargs):
        return Diary(self.store).publish(self.emit("diary", {"text": text}, **kwargs))

    def model(self, session=None):
        model = HostedSelfModel(session or self.session)
        self.addCleanup(model.close)
        return model

    def model_event(self, tool="self_model_observe", args=None, **kwargs):
        if args is None:
            args = {"statement": "我会确认合成来源", "domain": "habit", "topic_key": "check_source",
                    "scope": "global", "requested_maturity": "established"}
        return self.emit("self_model", {"tool_name": tool, "args": args},
                         source_type="canonical_tool_call", tool_name=tool, **kwargs)

    def mood(self):
        return {"dims": {k: (-3 if k == "valence" else 4) for k in DIM_KEYS},
                "label": "平静", "emoji": "😌", "note": "合成情绪", "source": "合成对话",
                "source_type": "conversation"}

    def test_each_content_domain_real_roundtrip_provenance_and_reopen(self):
        for cls, channel in ((Diary, "main"), (Dream, "main"), (Whisper, "private")):
            with self.subTest(domain=cls.kind):
                session = self.session_for(channel=channel)
                domain = cls(InnerStore(session))
                ref = self.emit(cls.kind, {"text": "本人合成创作", "entry_id": "entry", "segment": 0}, channel=channel)
                saved = domain.publish(ref)
                reopened = cls(InnerStore(session)).get(saved["item_id"])
                self.assertEqual(reopened, saved)
                self.assertEqual(saved["provenance"]["event_time"], NOW)
                self.assertEqual(saved["provenance"]["source_ref"], ref)
                self.assertEqual(saved["provenance"]["agent_id"], self.a)
                self.assertEqual(saved["provenance"]["author"], "assistant")
                self.assertEqual(saved["payload"]["nature"], cls.nature)
                self.assertEqual(domain.publish(ref), saved)

    def test_content_revision_revocation_and_paginated_replay(self):
        domain = Diary(self.store)
        first = self.diary()
        ref = self.emit("diary", {"text": "第二版", "target_id": first["item_id"], "expected_version": 1})
        second = domain.publish(ref)
        revoked = domain.revoke(first["item_id"], expected_version=2, reason="合成撤销", recorded_at=NOW)
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["provenance"]["author"], "assistant")
        self.assertEqual(revoked["provenance"]["management"]["action"], "revoke")
        self.assertEqual(domain.get(first["item_id"], version=1), first)
        self.assertEqual(domain.get(first["item_id"], version=2), second)
        page = domain.history(first["item_id"], limit=2)
        self.assertEqual([x["version"] for x in page], [1, 2])
        self.assertEqual([x["version"] for x in domain.history(first["item_id"], after=2)], [3])
        with self.assertRaisesRegex(ValueError, "版本冲突"):
            domain.revoke(first["item_id"], expected_version=1, reason="旧版本", recorded_at=NOW)

    def test_dream_and_whisper_versions_replay(self):
        for cls, channel in ((Dream, "main"), (Whisper, "private")):
            with self.subTest(kind=cls.kind):
                domain = cls(InnerStore(self.session_for(channel=channel)))
                first = domain.publish(self.emit(cls.kind, {"text": "第一版"}, channel=channel))
                second = domain.publish(self.emit(cls.kind, {"text": "新版", "target_id": first["item_id"],
                                                           "expected_version": 1}, channel=channel))
                domain.revoke(first["item_id"], expected_version=2, reason="撤销", recorded_at=NOW)
                self.assertEqual(domain.history(first["item_id"])[1], second)
                self.assertEqual(domain.get(first["item_id"], version=1), first)

    def test_unconnected_agent_all_autonomous_domains_reject_and_management_works(self):
        self.diary()
        session = self.session_for(reader=False)
        for cls in (Diary, Dream, Emotion):
            with self.subTest(kind=cls.kind), self.assertRaises(AgentRequired):
                cls(InnerStore(session)).publish("request:made-up")
        with self.assertRaises(AgentRequired):
            Whisper(InnerStore(self.session_for(channel="private", reader=False))).publish("request:made-up")
        with self.assertRaises(AgentRequired):
            self.model(session).apply("request:made-up")
        self.assertEqual(len(Diary(InnerStore(session)).list()), 1)
        persona = Persona(InnerStore(session))
        self.assertEqual(persona.configure(authority="managed", expected_version=0, content="离线配置",
                                         reason="基础管理", recorded_at=NOW)["version"], 1)
        suggestion = Reflection(InnerStore(session)).propose(text="待本人审核", target_kind="persona",
                           target_id="persona", target_version=1, recorded_at=NOW)
        self.assertEqual(suggestion["payload"]["effect"], "suggestion_only")

    def test_maintenance_can_import_but_cannot_create_as_agent(self):
        domain = Diary(InnerStore(self.session_for(role="maintenance")))
        ref = self.emit("diary", {"text": "本人已生成历史"})
        with self.assertRaises(AccessDenied):
            domain.publish(ref)
        saved = domain.import_event(ref)
        self.assertEqual(saved["provenance"]["author"], "assistant")
        with self.assertRaises(AccessDenied):
            domain.import_event("request:self-reported")

    def test_unproven_failed_wrong_author_and_noncanonical_events_rejected(self):
        invalid = [dict(status="running"), dict(status="failed"), dict(status="cancelled"),
                   dict(actor="maintenance"), dict(actor="curator"), dict(actor="user"),
                   dict(outcome="failed"), dict(committed=False), dict(active=False),
                   dict(duplicate=True), dict(source_type="maintenance_tool_call")]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(AccessDenied):
                Diary(self.store).publish(self.emit("diary", {"text": "不应写入"}, **options))
        self.assertEqual(Diary(self.store).list(), [])

    def test_request_actor_and_generation_fields_give_no_authority(self):
        for field in ("actor", "generation_id", "principal_id", "source_ref"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                Diary(self.store).publish(self.emit("diary", {"text": "合成", field: "agent"}))
        with self.assertRaises(TypeError):
            Diary(self.store).publish("self:fake", actor="assistant")

    def test_wrong_kind_generation_and_source_app_rejected(self):
        with self.assertRaises(ValueError):
            Diary(self.store).publish(self.emit("dream", {"text": "梦"}))
        with self.assertRaises(AccessDenied):
            Diary(self.store).publish(self.emit("diary", {"text": "无授权"}, app="unknown-app"))
        ref = self.emit("diary", {"text": "错位"})
        self.host.replace_record(ref, self.a, generation_id="other-generation")
        with self.assertRaises(AccessDenied):
            Diary(self.store).publish(ref)

    def test_same_name_agents_owner_and_app_grants_isolate(self):
        first = self.diary()
        other = Diary(InnerStore(self.session_for(agent=self.b)))
        self.assertEqual(other.list(), [])
        with self.assertRaises(KeyError):
            other.get(first["item_id"])
        with self.assertRaises(AccessDenied):
            other.publish(self.emit("diary", {"text": "A 的事件"}))
        with self.assertRaises(AccessDenied):
            self.session_for(owner=self.other_owner)
        with self.assertRaises(AccessDenied):
            self.session_for(agent=self.b, app="app-b")
        self.assertEqual(Diary(InnerStore(self.session_for(app="app-b"))).get(first["item_id"]), first)

    def test_private_channel_never_leaks_to_main_or_other_app(self):
        private = Whisper(InnerStore(self.session_for(channel="private")))
        saved = private.publish(self.emit("whisper", {"text": "私有合成"}, channel="private"))
        with self.assertRaises(AccessDenied):
            Whisper(self.store)
        with self.assertRaises(AccessDenied):
            self.session_for(channel="private", app="app-b")
        with self.assertRaises(AccessDenied):
            self.session_for(agent=self.b, channel="private")
        with self.assertRaises(KeyError):
            self.store.get("whisper", saved["item_id"])
        with self.assertRaises(AccessDenied):
            Diary(self.store).publish(self.emit("diary", {"text": "私有日记"}, channel="private"))

    def test_authorization_rechecked_on_existing_read_and_write_handles(self):
        saved = self.diary()
        domain = Diary(self.store)
        model = self.model()
        persona = Persona(self.store)
        with sqlite3.connect(self.registry.registry_path) as db:
            db.execute("DELETE FROM sources WHERE agent_id=? AND app_id=? AND channel='main'", (self.a, "app-a"))
        operations = [lambda: domain.get(saved["item_id"]), domain.list,
                      lambda: domain.history(saved["item_id"]),
                      lambda: domain.publish("fake:ref"),
                      lambda: domain.revoke(saved["item_id"], expected_version=1, reason="拒绝", recorded_at=NOW),
                      persona.get, lambda: model.claim("fake"), lambda: model.history("fake"),
                      lambda: Continuity(self.store).build("epoch-1", [], recorded_at=NOW)]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(AccessDenied):
                operation()

    def test_emotion_eleven_dimensions_semantics_and_reopen(self):
        domain = Emotion(self.store)
        self.assertIsNone(domain.latest())
        saved = domain.publish(self.emit("emotion", self.mood()))
        self.assertEqual(Emotion(InnerStore(self.session)).latest(), saved)
        self.assertEqual(saved["payload"]["dims"]["valence"], -3)
        self.assertEqual(set(saved["payload"]["dims"]), set(DIM_KEYS))
        self.assertEqual(DIM_RANGES["valence"], (-10, 10))
        self.assertEqual(saved["provenance"]["event_time"], NOW)
        revoked = domain.revoke(saved["item_id"], expected_version=1, reason="合成撤销", recorded_at=NOW)
        self.assertIsNone(domain.latest())
        self.assertEqual(domain.get(saved["item_id"], version=1), saved)
        self.assertEqual(domain.history(saved["item_id"])[1], revoked)

    def test_emotion_invalid_dims_and_ranges_rejected(self):
        for field, value in (("valence", -11), ("guilt", -1), ("desire", 11), ("arousal", True)):
            mood = self.mood()
            mood["dims"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                Emotion(self.store).publish(self.emit("emotion", mood))
        mood = self.mood()
        del mood["dims"]["guilt"]
        with self.assertRaises(ValueError):
            Emotion(self.store).publish(self.emit("emotion", mood))

    def test_reflection_only_suggests_and_audits_state(self):
        diary = self.diary()
        persona = Persona(self.store)
        config = persona.configure(authority="managed", expected_version=0, content="合成人格", reason="配置", recorded_at=NOW)
        domain = Reflection(InnerStore(self.session_for(role="curator", reader=False)))
        proposal = domain.propose(text="建议本人检查日记", target_kind="diary", target_id=diary["item_id"], target_version=1, recorded_at=NOW)
        self.assertFalse(proposal["payload"]["target_verified"])
        reviewed = domain.transition(proposal["item_id"], expected_version=1, state="reviewed", reason="已查看", recorded_at=NOW)
        domain.transition(proposal["item_id"], expected_version=2, state="dismissed", reason="不采纳", recorded_at=NOW)
        self.assertEqual(domain.history(proposal["item_id"])[1], reviewed)
        self.assertEqual(Diary(self.store).get(diary["item_id"]), diary)
        self.assertEqual(persona.get(), config)
        with self.assertRaises(ValueError):
            domain.transition(proposal["item_id"], expected_version=3, state="applied", reason="不可自动执行", recorded_at=NOW)

    def test_continuity_explicit_sources_confirmed_epoch_and_no_runtime_delivery(self):
        diary = self.diary()
        domain = Continuity(self.store)
        candidate = domain.build("epoch-1", [("diary", diary["item_id"], 1)], recorded_at=NOW)
        self.assertEqual(candidate["payload"]["facts"][0]["payload"], diary["payload"])
        self.assertFalse(candidate["payload"]["provider_visible"])
        self.assertFalse(candidate["payload"]["adopted"])
        self.assertTrue(candidate["provenance"]["epoch_confirmed"])
        self.assertEqual(Continuity(InnerStore(self.session)).get(candidate["item_id"]), candidate)
        with self.assertRaises(AccessDenied):
            domain.build("made-up-epoch", [("diary", diary["item_id"], 1)], recorded_at=NOW)
        with self.assertRaises(ValueError):
            domain.build("epoch-1", [("diary", diary["item_id"], 9)], recorded_at=NOW)
        Diary(self.store).revoke(diary["item_id"], expected_version=1, reason="撤销来源", recorded_at=NOW)
        with self.assertRaises(AccessDenied):
            domain.get(candidate["item_id"])
        with self.assertRaises(AccessDenied):
            domain.list()

    def test_continuity_excludes_dream_and_private_cross_channel(self):
        dream = Dream(self.store).publish(self.emit("dream", {"text": "梦不冒充现实"}))
        with self.assertRaises(AccessDenied):
            Continuity(self.store).build("epoch-1", [("dream", dream["item_id"], 1)], recorded_at=NOW)
        private_store = InnerStore(self.session_for(channel="private"))
        whisper = Whisper(private_store).publish(self.emit("whisper", {"text": "私有"}, channel="private"))
        candidate = Continuity(private_store).build("epoch-1", [("whisper", whisper["item_id"], 1)], recorded_at=NOW)
        with self.assertRaises(KeyError):
            Continuity(self.store).get(candidate["item_id"])
        with self.assertRaises(AccessDenied):
            Continuity(self.store).build("epoch-1", [("whisper", whisper["item_id"], 1)], recorded_at=NOW)
        with self.assertRaises(ValueError):
            Continuity(private_store).build("epoch-1", [("whisper", whisper["item_id"], 1)] * 21, recorded_at=NOW)

    def test_persona_managed_external_single_authority_and_epoch_freeze(self):
        persona = Persona(self.store)
        v1 = persona.configure(authority="managed", expected_version=0, content="合成人格 v1", reason="配置", recorded_at=NOW)
        self.assertEqual(persona.bind_epoch("epoch-1", reason="宿主绑定", recorded_at=NOW), v1)
        v2 = persona.configure(authority="managed", expected_version=1, content="合成人格 v2", reason="更新", recorded_at=NOW)
        self.assertEqual(persona.bind_epoch("epoch-1", reason="再次进入", recorded_at=NOW), v1)
        self.assertEqual(persona.bind_epoch("epoch-2", reason="新窗口", recorded_at=NOW), v2)
        v3 = persona.configure(authority="external", expected_version=2, external_ref="synthetic:niannian-prompt",
                               external_revision="v3", reason="外部权威", recorded_at=NOW)
        self.assertIsNone(v3["content"])
        self.assertEqual(persona.for_epoch("epoch-2"), v2)
        self.assertEqual(Persona(InnerStore(self.session)).get(), v3)
        self.assertEqual([x["version"] for x in persona.history(after=1)], [2, 3])
        self.assertEqual(persona.history(limit=1), [v1])
        self.assertEqual(Persona(InnerStore(self.session_for(agent=self.b))).history(), [])

    def test_persona_rejects_dual_authority_stale_versions_and_unconfirmed_epoch(self):
        persona = Persona(self.store)
        with self.assertRaises(ValueError):
            persona.configure(authority="external", expected_version=0, content="双权威", external_ref="synthetic:prompt",
                              external_revision="v1", reason="拒绝", recorded_at=NOW)
        persona.configure(authority="external", expected_version=0, external_ref="synthetic:prompt",
                          external_revision="v1", reason="念念式 external", recorded_at=NOW)
        with self.assertRaises(ValueError):
            persona.configure(authority="managed", expected_version=0, content="冲突", reason="旧版本", recorded_at=NOW)
        with self.assertRaises(AccessDenied):
            persona.bind_epoch("not-confirmed", reason="拒绝", recorded_at=NOW)
        with self.assertRaises(AccessDenied):
            Persona(InnerStore(self.session_for(channel="private")))

    def test_self_model_gate_real_agent_observe_revise_retire_and_replay(self):
        model = self.model()
        ref = self.model_event()
        first = model.apply(ref)
        self.assertEqual(first.phase, "finalized")
        self.assertEqual(first.maturity, "observing")
        self.assertEqual(model.apply(ref), first)
        second = model.apply(self.model_event("self_model_revise", {
            "claim_id": first.claim_id, "expected_version": 1,
            "intent": "add_support", "requested_maturity": "established"}))
        self.assertEqual(second.maturity, "established")
        frozen = model.freeze_epoch("epoch-2", now=NOW)
        self.assertEqual(len(frozen.items), 1)
        retired = model.apply(self.model_event("self_model_retire", {
            "claim_id": first.claim_id, "expected_version": 2, "reason": "changed"}, epoch="epoch-2"))
        self.assertEqual(retired.maturity, "retired")
        self.assertEqual(model.freeze_epoch("epoch-2", now=NOW), frozen)
        self.assertEqual(model.freeze_epoch("epoch-3", now=NOW).items, ())
        self.assertEqual([x.version for x in model.history(first.claim_id)], [1, 2, 3])
        self.assertEqual([x.version for x in model.history(first.claim_id, after=1, limit=1)], [2])
        self.assertEqual(self.model().claim(first.claim_id).maturity, "retired")
        self.assertIsNone(self.model(self.session_for(agent=self.b)).claim(first.claim_id))

    def test_self_model_curator_maintenance_only_suggestions_and_no_forged_actor(self):
        model = self.model()
        first = model.apply(self.model_event())
        for role in ("curator", "maintenance"):
            other = self.model(self.session_for(role=role))
            with self.assertRaises(AccessDenied):
                other.apply(self.model_event())
            self.assertTrue(other.suggest(suggestion_id=f"suggestion-{role}", claim_id=first.claim_id,
                                          summary="仅建议本人复核", recorded_at=NOW))
            self.assertEqual(other.claim(first.claim_id).version, 1)
        with self.assertRaises(ContractValidationError) as raised:
            model.apply(self.model_event(args={"statement": "伪造", "domain": "habit", "topic_key": "fake",
                "scope": "global", "actor": "assistant"}))
        self.assertIn("actor", str(raised.exception))

    def test_self_model_private_and_noncanonical_tool_call_rejection(self):
        private = self.model(self.session_for(channel="private"))
        ref = self.model_event(channel="private")
        claim = private.apply(ref)
        self.assertIsNone(self.model().claim(claim.claim_id))
        with self.assertRaises(AccessDenied):
            self.model().apply(ref)
        with self.assertRaises(AccessDenied):
            private.apply(self.model_event(channel="private", status="running"))
        ref = self.model_event()
        self.host.replace_record(ref, self.a, root_event_id="wrong-call")
        with self.assertRaises(ValueError):
            self.model().apply(ref)

    def test_paginated_reads_bounded_and_do_not_call_expensive_host(self):
        for i in range(4):
            self.diary(text=f"第 {i} 段")
        domain = Diary(self.store)
        page = domain.list(limit=2)
        self.assertEqual(len(page), 2)
        self.assertEqual(len(domain.list(after=page[-1]["seq"], limit=2)), 2)
        for limit in (0, 101, True):
            with self.assertRaises(ValueError):
                domain.list(limit=limit)
        with patch.object(self.host, "event", side_effect=AssertionError("读热路径禁止宿主慢操作")), patch.object(
                self.host, "confirmed_epoch", side_effect=AssertionError("不热查 Runtime")):
            self.assertEqual(len(domain.list()), 4)
            self.assertEqual(domain.get(page[0]["item_id"]), page[0])
            self.assertEqual(domain.history(page[0]["item_id"]), [page[0]])
        with self.store.connect() as db:
            plan = db.execute("EXPLAIN QUERY PLAN SELECT v.* FROM inner_heads h JOIN inner_versions v ON v.seq=h.seq "
                              "WHERE h.channel=? AND h.kind=? AND h.seq>? ORDER BY h.seq LIMIT ?",
                              ("main", "diary", 0, 2)).fetchall()
        self.assertTrue(any("inner_page" in row[3] for row in plan), plan)
        self.assertFalse(any("SCAN" in row[3] for row in plan), plan)

    def test_bad_time_oversized_content_conflicts_leave_no_partial_state(self):
        with self.assertRaises(ValueError):
            Diary(self.store).publish(self.emit("diary", {"text": "x" * 8001}))
        saved = self.diary()
        with self.assertRaises(ValueError):
            Diary(self.store).revoke(saved["item_id"], expected_version=1, reason="时间不明", recorded_at="2026-10-14T00:00:00")
        with self.assertRaises(ValueError):
            Diary(self.store).publish(self.emit("diary", {"text": "冲突", "target_id": saved["item_id"], "expected_version": 9}))
        self.assertEqual(Diary(self.store).history(saved["item_id"]), [saved])

    def test_revoking_older_emotion_does_not_replace_current_mood(self):
        domain = Emotion(self.store)
        first = domain.publish(self.emit("emotion", self.mood()))
        newer = self.mood()
        newer["dims"]["valence"] = 2
        second = domain.publish(self.emit("emotion", newer))
        domain.revoke(first["item_id"], expected_version=1, reason="撤销旧快照", recorded_at=NOW)
        self.assertEqual(domain.latest(), second)

    def test_tampered_source_hash_and_noncanonical_branch_cannot_be_imported(self):
        ref = self.emit("diary", {"text": "来源遭篡改"})
        record = self.host.record(ref, self.a)
        changed = {**record.__dict__, "content": json.dumps({"text": "未经核证修改"})}
        with sqlite3.connect(self.host.path) as db:
            db.execute("UPDATE events SET record=? WHERE agent=? AND ref=?", (json.dumps(changed), self.a, ref))
        with self.assertRaises(EvidenceResolutionError):
            Diary(self.store).publish(ref)
        ref = self.emit("diary", {"text": "错分支"})
        with sqlite3.connect(self.host.path) as db:
            row = db.execute("SELECT generation FROM events WHERE agent=? AND ref=?", (self.a, ref)).fetchone()
            changed = {**json.loads(row[0]), "canonical_branch_id": "another-branch"}
            db.execute("UPDATE events SET generation=? WHERE agent=? AND ref=?", (json.dumps(changed), self.a, ref))
        with self.assertRaises(AccessDenied):
            Diary(self.store).publish(ref)
        self.assertEqual(Diary(self.store).list(), [])


if __name__ == "__main__":
    unittest.main()
