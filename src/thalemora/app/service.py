"""持久单用户应用接线；不运行 Agent、不将档案冒充 canonical 行动。"""

from contextlib import closing
from dataclasses import asdict, is_dataclass
import json
import sqlite3

from thalemora.identity import IdentityRegistry, OwnerPrincipal
from thalemora.history import Archive
from thalemora.inner_world.host import HostSession, page_size
from thalemora.inner_world.store import InnerStore
from thalemora.persona import Persona
from thalemora.self_model.host import HostedSelfModel
from thalemora.memory import schema as memory_schema
from thalemora.memory.revision_store import create_card, update_card
from thalemora.water import schema as water_schema
from thalemora.water.store import create_node


def plain(value):
    return json.loads(json.dumps(value, default=lambda x: asdict(x) if is_dataclass(x) else x.value,
                                 ensure_ascii=False))


class Application:
    def __init__(self, root, *, host_reader=None, embedder=None):
        self.registry = IdentityRegistry(root)
        self.owner = OwnerPrincipal("local-owner")
        self.host_reader = host_reader
        self.embedder = embedder
        from .providers import Providers
        self.providers = Providers(self.registry.root)

    def list_agents(self, *, limit=20, offset=0):
        self.page(limit, offset)
        with closing(sqlite3.connect(self.registry.registry_path)) as db:
            return [dict(agent_id=r[0], display_name=r[1]) for r in db.execute(
                "SELECT agent_id,display_name FROM agents WHERE principal_id=? "
                "ORDER BY agent_id LIMIT ? OFFSET ?", (self.owner.principal_id, limit, offset))]

    @staticmethod
    def page(limit, offset):
        page_size(limit)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset 必须为非负整数")

    def register_agent(self, display_name):
        return asdict(self.registry.register_agent(self.owner, display_name))

    def grant_source(self, agent_id, app_id, channel="main"):
        self.registry.grant_source(self.owner, agent_id, app_id, channel)
        return {"granted": True}

    def sources(self, agent_id, *, limit=20, offset=0):
        self.page(limit, offset)
        self.registry.archive_db_path(self.owner, agent_id)
        with closing(sqlite3.connect(self.registry.registry_path)) as db:
            return [dict(app_id=r[0], channel=r[1]) for r in db.execute(
                "SELECT app_id,channel FROM sources WHERE agent_id=? ORDER BY app_id,channel LIMIT ? OFFSET ?",
                (agent_id, limit, offset))]

    def session(self, agent_id, app_id, channel="main", *, role="maintenance"):
        return HostSession(self.registry, self.owner, agent_id, app_id, channel, role, self.host_reader)

    def archive(self, agent_id, app_id, channel):
        self.registry.assert_source(self.owner, agent_id, app_id, channel)
        archive = Archive(self.registry, self.owner, agent_id)
        self._lexical_index(archive.path)
        archive.ensure_short_index()
        return archive

    def history(self, agent_id, app_id, channel="main", *, limit=20, offset=0, include_deleted=False):
        self.page(limit, offset)
        return self.archive(agent_id, app_id, channel).export_history(
            app_id, channel, limit, offset, include_deleted)

    def import_history(self, agent_id, app_id, events, channel="main"):
        if not isinstance(events, list) or not 1 <= len(events) <= 100:
            raise ValueError("每批导入 1..100 个 envelope")
        if any(e.get("app") != app_id or e.get("channel", "main") != channel for e in events):
            raise ValueError("导入来源必须与请求域一致")
        return self.archive(agent_id, app_id, channel).import_history(events)

    def maintenance_tick(self, agent_id, app_id, channel="main", *, limit=4, provider=None):
        """显式后台单次工作；PRE6-4 仅接受带 synthetic=True 的合成 Provider。"""
        from .maintenance import maintenance_tick
        return maintenance_tick(self, agent_id, app_id, channel, limit=limit, provider=provider)

    def maintenance_status(self, agent_id, app_id, channel="main", *, limit=20):
        from .maintenance import maintenance_status
        return maintenance_status(self, agent_id, app_id, channel, limit=limit)

    def search(self, agent_id, app_id, query, channel="main", *, limit=20, offset=0):
        """1–2 字符走 Btree 短 gram；更长原话查询沿用 FTS5 trigram。"""
        self.page(limit, offset)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("词法查询须为 1..200 个字符")
        query = query.strip()
        archive = self.archive(agent_id, app_id, channel)
        with closing(sqlite3.connect(archive.path)) as db:
            db.row_factory = sqlite3.Row
            if len(query) <= 2:
                rows = db.execute(
                    "SELECT h.* FROM history_short_grams s CROSS JOIN history_events h "
                    "ON h.app=s.app AND h.channel=s.channel AND h.source_event_id=s.source_event_id "
                    "WHERE s.app=? AND s.channel=? AND s.gram=? AND h.deleted=0 "
                    "ORDER BY s.source_event_id LIMIT ? OFFSET ?",
                    (app_id, channel, query.lower(), limit, offset))
            else:
                rows = db.execute(
                    "SELECT h.* FROM history_fts f JOIN history_events h ON h.rowid=f.rowid "
                    "WHERE history_fts MATCH ? AND h.app=? AND h.channel=? AND h.deleted=0 "
                    "ORDER BY h.source_event_id LIMIT ? OFFSET ?",
                    ('"' + query.replace('"', '""') + '"', app_id, channel, limit, offset))
            return [dict(row) for row in rows]

    @staticmethod
    def _lexical_index(path):
        with closing(sqlite3.connect(path)) as db, db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_fts'").fetchone():
                return
            db.executescript("""
                CREATE VIRTUAL TABLE history_fts USING fts5(text,content='history_events',content_rowid='rowid',tokenize='trigram');
                CREATE TRIGGER history_fts_insert AFTER INSERT ON history_events BEGIN
                  INSERT INTO history_fts(rowid,text) VALUES(new.rowid,new.text); END;
                CREATE TRIGGER history_fts_delete AFTER DELETE ON history_events BEGIN
                  INSERT INTO history_fts(history_fts,rowid,text) VALUES('delete',old.rowid,old.text); END;
                CREATE TRIGGER history_fts_update AFTER UPDATE ON history_events BEGIN
                  INSERT INTO history_fts(history_fts,rowid,text) VALUES('delete',old.rowid,old.text);
                  INSERT INTO history_fts(rowid,text) VALUES(new.rowid,new.text); END;
                INSERT INTO history_fts(history_fts) VALUES('rebuild');
            """)

    def memory_path(self, agent_id, app_id, channel):
        session = self.session(agent_id, app_id, channel)
        path = self.registry.archive_db_path(self.owner, session.agent_id).with_name(f"memory_{channel}.sqlite")
        with closing(sqlite3.connect(path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            memory_schema.initialize(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_water_nodes'").fetchone():
                water_schema.initialize(db)
        return path

    def memory(self, agent_id, app_id, channel="main", *, limit=20, offset=0):
        self.page(limit, offset)
        with closing(sqlite3.connect(self.memory_path(agent_id, app_id, channel))) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                "SELECT id,title,content,happened_at,current_revision_id,memory_kind FROM cards ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset))]

    def remember(self, agent_id, app_id, source_event_id, channel="main"):
        """显式把档案原文存为人工记忆；证据保留完整来源 envelope，绝不宣称 verified。"""
        archive = self.archive(agent_id, app_id, channel)
        with closing(sqlite3.connect(archive.path)) as db:
            row = db.execute("SELECT text,occurred_at,version FROM history_events WHERE app=? AND channel=? "
                             "AND source_event_id=? AND deleted=0", (app_id, channel, source_event_id)).fetchone()
        if row is None or not row[0]:
            raise KeyError("找不到有效历史正文")
        ref = json.dumps(dict(agent_id=agent_id, app=app_id, channel=channel,
                              source_event_id=source_event_id, version=row[2]), ensure_ascii=False)
        return create_card(self.memory_path(agent_id, app_id, channel), content=row[0], happened_at=row[1],
                           origin="manual_ui", source="manual", evidence_source_kind="manual",
                           evidence_source_ref=ref)

    def revise_memory(self, agent_id, app_id, card_id, content, expected_current_revision_id, channel="main"):
        """人工正文修订，沿用正式 CAS；来源证据和其他字段语义保持原样。"""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("人工修订正文不能为空")
        if type(card_id) is not int or type(expected_current_revision_id) is not int:
            raise ValueError("card_id 和 expected_current_revision_id 须为整数")
        return update_card(self.memory_path(agent_id, app_id, channel), card_id=card_id,
                           content=content, expected_current_revision_id=expected_current_revision_id,
                           origin="manual_ui", change_type="manual_edit",
                           evidence_source_kind="manual",
                           evidence_source_ref=json.dumps({"kind": "mcp_explicit_manual_revision",
                                                           "app": app_id, "channel": channel}))

    def evidence(self, agent_id, app_id, card_id, channel="main", *, limit=20, offset=0):
        self.page(limit, offset)
        with closing(sqlite3.connect(self.memory_path(agent_id, app_id, channel))) as db:
            db.row_factory = sqlite3.Row
            evidence = [dict(r) for r in db.execute("SELECT e.* FROM card_evidence e JOIN card_revisions r "
                    "ON r.id=e.revision_id WHERE r.card_id=? ORDER BY e.id LIMIT ? OFFSET ?",
                    (card_id, limit, offset))]
        archive = self.archive(agent_id, app_id, channel)
        with closing(sqlite3.connect(archive.path)) as db:
            for item in evidence:
                if item["source_kind"] in {"manual", "derived"}:
                    if item["source_kind"] == "derived":
                        try:
                            ref = json.loads(item["source_ref"])
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(ref, dict) or ref.get("kind") != "archive_quote":
                            continue
                    else:
                        ref = json.loads(item["source_ref"])
                    if ref.get("kind") == "mcp_explicit_manual_revision":
                        item["resolution"] = "manual_revision"
                        item["current_source_text"] = None
                        item["verified"] = False
                        continue
                    self.registry.assert_source(self.owner, agent_id, ref["app"], ref["channel"])
                    row = db.execute("SELECT version,deleted,text FROM history_events WHERE app=? AND channel=? "
                                     "AND source_event_id=?", (ref["app"], ref["channel"], ref["source_event_id"])).fetchone()
                    item["resolution"] = ("missing" if row is None else "deleted" if row[1] else
                                          "current" if row[0] == ref["version"] else "source_changed")
                    item["current_source_text"] = row[2] if row and not row[1] else None
                    item["verified"] = False
        return evidence

    def water(self, agent_id, app_id, channel="main", *, limit=20, offset=0):
        self.page(limit, offset)
        with closing(sqlite3.connect(self.memory_path(agent_id, app_id, channel))) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM memory_water_nodes ORDER BY node_id LIMIT ? OFFSET ?",
                                               (limit, offset))]

    def water_node(self, agent_id, app_id, node_id, node_type, title, channel="main", parent_node_id=None):
        with closing(sqlite3.connect(self.memory_path(agent_id, app_id, channel))) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            return {"node_id": create_node(db, node_id=node_id, node_type=node_type, title=title,
                                            origin="manual_ui", parent_node_id=parent_node_id)}

    def water_move(self, agent_id, app_id, card_id, to_node_id, reason, channel="main"):
        from thalemora.water.growth import move_card
        with closing(sqlite3.connect(self.memory_path(agent_id, app_id, channel))) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return {"action_id": move_card(db, card_id, to_node_id, reason, actor=self.owner.principal_id)}

    def water_memberships(self, agent_id, app_id, channel="main", *, limit=20, offset=0):
        self.page(limit, offset)
        with closing(sqlite3.connect(self.memory_path(agent_id, app_id, channel))) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM memory_water_membership ORDER BY card_id LIMIT ? OFFSET ?",
                                               (limit, offset))]

    def local_index(self, agent_id, app_id, channel="main"):
        if self.embedder is None:
            raise ValueError("本地向量 unconfigured：Python 宿主须显式提供已就绪 embedder 和 model")
        from thalemora.retrieval.core import RetrievalIndex
        path = self.memory_path(agent_id, app_id, channel)
        model, embedder = self.embedder
        return RetrievalIndex(str(path.with_name(f"retrieval_{channel}.sqlite")),
                              memory_db_path=str(path), embedding_model=model, embedder=embedder)

    def build_local_index(self, agent_id, app_id, channel="main"):
        return self.local_index(agent_id, app_id, channel).rebuild_memory()

    def local_search(self, agent_id, app_id, query, channel="main", *, limit=20):
        page_size(limit)
        return self.local_index(agent_id, app_id, channel).search(query, top_k=limit, pool=100)

    def inner_state(self, agent_id, app_id, kind, channel="main", *, limit=20, offset=0):
        self.page(limit, offset)
        session = self.session(agent_id, app_id, channel)
        store = InnerStore(session)
        if kind == "persona":
            return Persona(store).history(after=offset, limit=limit)
        if kind == "self_model":
            model = HostedSelfModel(session)
            try:
                rows = model.connection.execute("SELECT claim_id FROM claim_lineages ORDER BY claim_id LIMIT ? OFFSET ?",
                                                (limit, offset)).fetchall()
                return [plain(model.claim(r[0])) for r in rows]
            finally:
                model.close()
        from thalemora.diary import Diary
        from thalemora.dream import Dream
        from thalemora.whisper import Whisper
        from thalemora.emotion import Emotion
        from thalemora.reflection import Reflection
        from thalemora.continuity import Continuity
        domains = {d.kind: d for d in (Diary, Dream, Whisper, Emotion, Reflection, Continuity)}
        if kind not in domains:
            raise ValueError("未知内在领域")
        return domains[kind](store).list(after=offset, limit=limit)

    def publish(self, agent_id, app_id, kind, event_ref, channel="main"):
        """仅可信 Python 宿主调用；角色与 reader 均由宿主接线。"""
        session = self.session(agent_id, app_id, channel, role="agent")
        if kind == "self_model":
            model = HostedSelfModel(session)
            try:
                return plain(model.apply(event_ref))
            finally:
                model.close()
        from thalemora.diary import Diary
        from thalemora.dream import Dream
        from thalemora.whisper import Whisper
        from thalemora.emotion import Emotion
        domains = {d.kind: d for d in (Diary, Dream, Whisper, Emotion)}
        if kind not in domains:
            raise ValueError("此领域不接受 publish")
        return domains[kind](InnerStore(session)).publish(event_ref)

    def configure_persona(self, agent_id, app_id, **configuration):
        return Persona(InnerStore(self.session(agent_id, app_id))).configure(**configuration)

    def reflect(self, agent_id, app_id, channel="main", **proposal):
        from thalemora.reflection import Reflection
        return Reflection(InnerStore(self.session(agent_id, app_id, channel))).propose(**proposal)

    def continuity(self, agent_id, app_id, epoch_id, sources, recorded_at, channel="main"):
        from thalemora.continuity import Continuity
        return Continuity(InnerStore(self.session(agent_id, app_id, channel))).build(
            epoch_id, sources, recorded_at=recorded_at)

    def capabilities(self):
        return dict(host_connected=self.host_reader is not None,
                    autonomous_write="需连接可信 Agent 宿主；HTTP 不接受自报角色或事件正文",
                    lexical="历史原文 1–2 字符 Btree 短 gram，3–200 字符 FTS5 trigram",
                    local_vector="ready" if self.embedder else "unconfigured",
                    models=[self.providers.status(m) for m in ("embedding", "writer", "gatekeeper", "reflection")],
                    backup="停止所有写入后使用 thalemora backup/restore；目标必须为新空目录")
