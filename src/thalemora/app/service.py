"""持久单用户应用接线；不运行 Agent、不将档案冒充 canonical 行动。"""

from contextlib import closing, contextmanager
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
from thalemora.memory.revision_store import create_card_in_transaction, update_card_in_transaction
from thalemora.memory.source_validity import resolve_archive_evidence
from thalemora.memory.scopes import SOURCE, OWNER, validate_scope, ownership, belongs, require_card, reference_scope
from thalemora.water import schema as water_schema
from thalemora.water.store import create_node


def plain(value):
    return json.loads(json.dumps(value, default=lambda x: asdict(x) if is_dataclass(x) else x.value,
                                 ensure_ascii=False))


class Application:
    def __init__(self, root, *, host_reader=None, embedder=None, reranker=None):
        self.registry = IdentityRegistry(root)
        self.owner = OwnerPrincipal("local-owner")
        from .recall_tokens import RecallTokens
        self.recall_tokens = RecallTokens(self.registry, self.owner)
        self.host_reader = host_reader
        self.embedder = embedder
        from .providers import Providers
        self.providers = Providers(self.registry.root)
        if embedder is not None:
            self.providers.local_models["embedding"] = embedder
        if reranker is not None:
            self.providers.local_models["rerank"] = reranker

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
        """显式后台单次工作；真实模型由宿主/worker 显式注入，前台不隐式触发。"""
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

    def _management_authorize(self, db, agent_id, app_id, channel, scope):
        validate_scope(scope)
        if channel not in ("main", "private"):
            raise ValueError("channel 须为 main 或 private")
        path = self.registry._agent_path(db, self.owner, agent_id)
        if scope == SOURCE:
            self.registry._source(app_id, channel)
            if not db.execute("SELECT 1 FROM sources WHERE agent_id=? AND app_id=? AND channel=?",
                              (agent_id, app_id, channel)).fetchone():
                raise PermissionError("来源未获该 Agent 的指定域授权")
        return path

    @contextmanager
    def _memory_transaction(self, agent_id, app_id, channel, scope, path):
        # 真锁序：Archive（需要时）→ registry → Memory；只写 Memory，不需多库写原子性。
        with closing(sqlite3.connect(self.registry.registry_path)) as registry, registry:
            registry.execute("BEGIN IMMEDIATE")
            archive_path = self._management_authorize(registry, agent_id, app_id, channel, scope)
            if archive_path.with_name(f"memory_{channel}.sqlite") != path:
                raise PermissionError("Agent 存储路径已变化")
            with closing(sqlite3.connect(path)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                yield db

    def memory_path(self, agent_id, app_id=None, channel="main", *, scope=SOURCE):
        with closing(sqlite3.connect(self.registry.registry_path)) as registry:
            path = self._management_authorize(registry, agent_id, app_id, channel, scope).with_name(f"memory_{channel}.sqlite")
        with closing(sqlite3.connect(path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            memory_schema.initialize(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_water_nodes'").fetchone():
                water_schema.initialize(db)
        return path

    def memory(self, agent_id, app_id=None, channel="main", *, limit=20, offset=0, scope=SOURCE):
        self.page(limit, offset)
        path = self.memory_path(agent_id, app_id, channel, scope=scope)
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            scopes = ownership(db, agent_id, channel)
            db.create_function("in_source_scope", 1, lambda cid: belongs(scopes.get(cid), app_id, channel))
            cards = [dict(r) for r in db.execute(
                "SELECT id,title,content,happened_at,current_revision_id,memory_kind FROM cards "
                "WHERE ? OR in_source_scope(id) ORDER BY id LIMIT ? OFFSET ?", (scope == OWNER, limit, offset))]
        from thalemora.memory.extensions import scoped_extensions
        archive_path = self.registry.archive_db_path(self.owner, agent_id)
        extensions = scoped_extensions(path, archive_path, agent_id, app_id, channel, scope=scope)
        for card in cards:
            if scope == OWNER:
                card['ownership'] = scopes[card['id']]
            card['extensions'] = [e for e in extensions if e['card_id'] == card['id']]
        return cards

    def remember(self, agent_id, app_id, source_event_id, channel="main"):
        """显式把档案原文存为人工记忆；证据保留完整来源 envelope，绝不宣称 verified。"""
        archive = self.archive(agent_id, app_id, channel)
        path = self.memory_path(agent_id, app_id, channel)
        # 与后台同序：Archive → registry → Memory；来源读取至卡/修订/证据/归属提交均持锁，无模型。
        with closing(sqlite3.connect(archive.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            archive._authorize(app_id, channel)
            row = db.execute("SELECT text,occurred_at,version FROM history_events WHERE app=? AND channel=? "
                             "AND source_event_id=? AND deleted=0", (app_id, channel, source_event_id)).fetchone()
            if row is None or not row[0]:
                raise KeyError("找不到有效历史正文")
            ref = json.dumps(dict(agent_id=agent_id, app=app_id, channel=channel,
                                  source_event_id=source_event_id, version=row[2]), ensure_ascii=False)
            with self._memory_transaction(agent_id, app_id, channel, SOURCE, path) as memory:
                result = create_card_in_transaction(memory, content=row[0], happened_at=row[1],
                    origin="manual_ui", source="manual", evidence_source_kind="manual", evidence_source_ref=ref)
                memory.execute("INSERT INTO archive_card_scopes VALUES(?,?,?)", (result['card_id'], app_id, channel))
                return result

    def revise_memory(self, agent_id, app_id, card_id, content, expected_current_revision_id, channel="main", *, scope=SOURCE):
        """同一写事务核归属/CAS；owner 编辑不转移归属，也不认领未知旧卡。"""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("人工修订正文不能为空")
        if type(card_id) is not int or type(expected_current_revision_id) is not int:
            raise ValueError("card_id 和 expected_current_revision_id 须为整数")
        path = self.memory_path(agent_id, app_id, channel, scope=scope)
        with self._memory_transaction(agent_id, app_id, channel, scope, path) as db:
            card_scope = require_card(db, card_id, agent_id, app_id, channel, scope)
            ref = {"kind": "mcp_explicit_manual_revision"}
            if card_scope['state'] == 'known':
                ref.update(agent_id=agent_id, app=card_scope['app'], channel=channel)
            return update_card_in_transaction(db, card_id=card_id,
                content=content, expected_current_revision_id=expected_current_revision_id,
                origin="manual_ui", change_type="manual_edit", evidence_source_kind="manual",
                evidence_source_ref=json.dumps(ref))

    def evidence(self, agent_id, app_id, card_id, channel="main", *, limit=20, offset=0, scope=SOURCE):
        self.page(limit, offset)
        path = self.memory_path(agent_id, app_id, channel, scope=scope)
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            require_card(db, card_id, agent_id, app_id, channel, scope)
            evidence = [dict(r, evidence_kind='revision', extension_id=None, base_revision_id=None)
                        for r in db.execute("SELECT e.* FROM card_evidence e JOIN card_revisions r "
                    "ON r.id=e.revision_id WHERE r.card_id=? ORDER BY e.id", (card_id,))]
        # 先投影再分页；不返回外域/不明 envelope，连 source_ref 也不泄漏。
        evidence = [e for e in evidence if scope == OWNER or reference_scope(e, agent_id) == (app_id, channel)]
        archive_path = self.registry.archive_db_path(self.owner, agent_id)
        with closing(sqlite3.connect(archive_path)) as db:
            for item in evidence:
                ref_scope = (app_id, channel)
                if scope == OWNER:
                    try:
                        ref = json.loads(item['source_ref'])
                    except (ValueError, TypeError):
                        ref = None
                    # owner 跨 app，不跨当前 channel；外通道仅保留历史诊断，不解引用当前正文。
                    ref_scope = (ref.get('app'), channel) if isinstance(ref, dict) else (None, channel)
                state = resolve_archive_evidence(db, item, agent_id, *ref_scope)
                item.update({key: state[key] for key in ("resolution", "current_source_text", "verified")})
        from thalemora.memory.extensions import scoped_extensions
        for extension in scoped_extensions(path, archive_path, agent_id, app_id, channel, scope=scope):
            if extension['card_id'] != card_id:
                continue
            for item in extension['evidence_refs']:
                evidence.append(dict(item, evidence_kind='extension', revision_id=None,
                                     extension_id=extension['extension_id'],
                                     base_revision_id=extension['base_revision_id'],
                                     extension_status=extension['status'],
                                     eligibility=extension['eligibility']))
        # Stable order: canonical history first, then extension ID/evidence ID.
        # IDs belong to separate tables; evidence_kind + id is the identity.
        return evidence[offset:offset + limit]

    @staticmethod
    def _water_scope(scope):
        validate_scope(scope)
        if scope != OWNER:
            raise ValueError("scope_not_supported: Water 仅支持 owner-global")

    def water(self, agent_id, app_id=None, channel="main", *, limit=20, offset=0, scope=SOURCE):
        self._water_scope(scope)
        self.page(limit, offset)
        with closing(sqlite3.connect(self.memory_path(agent_id, app_id, channel, scope=scope))) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM memory_water_nodes ORDER BY node_id LIMIT ? OFFSET ?",
                                               (limit, offset))]

    def water_node(self, agent_id, app_id, node_id, node_type, title, channel="main", parent_node_id=None, *, scope=SOURCE):
        self._water_scope(scope)
        path = self.memory_path(agent_id, app_id, channel, scope=scope)
        with self._memory_transaction(agent_id, app_id, channel, scope, path) as db:
            return {"node_id": create_node(db, node_id=node_id, node_type=node_type, title=title,
                                            origin="manual_ui", parent_node_id=parent_node_id)}

    def water_move(self, agent_id, app_id, card_id, to_node_id, reason, channel="main", *, scope=SOURCE):
        self._water_scope(scope)
        from thalemora.water.growth import move_card
        path = self.memory_path(agent_id, app_id, channel, scope=scope)
        with self._memory_transaction(agent_id, app_id, channel, scope, path) as db:
            require_card(db, card_id, agent_id, app_id, channel, scope)
            return {"action_id": move_card(db, card_id, to_node_id, reason, actor=self.owner.principal_id)}

    def water_memberships(self, agent_id, app_id=None, channel="main", *, limit=20, offset=0, scope=SOURCE):
        self._water_scope(scope)
        self.page(limit, offset)
        with closing(sqlite3.connect(self.memory_path(agent_id, app_id, channel, scope=scope))) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM memory_water_membership ORDER BY card_id LIMIT ? OFFSET ?",
                                               (limit, offset))]

    def local_index(self, agent_id, app_id=None, channel="main", *, scope=SOURCE):
        path = self.memory_path(agent_id, app_id, channel, scope=scope)
        if self.embedder is None:
            raise ValueError("本地向量 unconfigured：Python 宿主须显式提供已就绪 embedder 和 model")
        from thalemora.retrieval.core import RetrievalIndex
        namespace = f"local_owner_{channel}"
        if scope == SOURCE:
            with closing(sqlite3.connect(self.registry.registry_path)) as db:
                source_id = db.execute("SELECT rowid FROM sources WHERE agent_id=? AND app_id=? AND channel=?",
                                       (agent_id, app_id, channel)).fetchone()[0]
            namespace = f"local_source_{source_id}"
        model, embedder = self.embedder
        return RetrievalIndex(str(path.with_name(namespace + ".sqlite")),
                              memory_db_path=str(path), embedding_model=model, embedder=embedder)

    def recall_for_turn(self, agent_id, app_id, query, channel="main", **visible):
        """共享的本轮召回核心，HTTP 只做凭据与协议接线。"""
        from thalemora.retrieval.recall import recall_for_turn
        return recall_for_turn(self, agent_id, app_id, query, channel, **visible)

    def issue_recall_token(self, agent_id, app_id, channel="main"):
        return self.recall_tokens.issue(agent_id, app_id, channel)

    def revoke_recall_token(self, token_id):
        return self.recall_tokens.revoke(token_id)

    def _local_rows(self, agent_id, app_id, channel, scope=SOURCE):
        from thalemora.memory.extensions import scoped_extensions
        from thalemora.retrieval.core import RetrievalIndex
        from thalemora.retrieval.recall import scoped_rows
        path = self.memory_path(agent_id, app_id, channel, scope=scope)
        archive_path = self.registry.archive_db_path(self.owner, agent_id)
        if scope == SOURCE:
            return scoped_rows(path, app_id, channel, archive_path, agent_id)[0]
        # owner 索引只供显式管理，不供自动召回；扩展仍是独立、有自身有效证据的 delta。
        extensions = {e['extension_id']: e for e in
                      scoped_extensions(path, archive_path, agent_id, app_id, channel, scope=scope) if e['eligible']}
        rows = []
        for row in RetrievalIndex._memory_rows(str(path)):
            extension_id = row['provenance'].get('extension_id')
            if extension_id is not None:
                if extension_id not in extensions:
                    continue
                row['provenance']['evidence_refs'] = extensions[extension_id]['evidence_refs']
            rows.append(row)
        return rows

    def build_local_index(self, agent_id, app_id=None, channel="main", *, scope=SOURCE):
        index = self.local_index(agent_id, app_id, channel, scope=scope)
        return index._replace_lane('memory_card', lambda: self._local_rows(agent_id, app_id, channel, scope),
                                   source_path=index.memory_db_path, embedder=index.embedder)

    def local_search(self, agent_id, app_id, query, channel="main", *, limit=20, scope=SOURCE):
        page_size(limit)
        from thalemora.retrieval.recall import ensure_current_index
        index = self.local_index(agent_id, app_id, channel, scope=scope)
        ensure_current_index(index, lambda: self._local_rows(agent_id, app_id, channel, scope), index.embedder)
        hits = index.search(query, top_k=100, pool=100)
        latest = {r['source_key']: r for r in self._local_rows(agent_id, app_id, channel, scope)}
        return [h for h in hits if h['source_key'] in latest
                and h['provenance'] == latest[h['source_key']]['provenance']
                and h['text'] == latest[h['source_key']]['text']][:limit]

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
                    models=[self.providers.status(m) for m in ("embedding", "rerank", "writer", "gatekeeper", "reflection")],
                    backup="停止所有写入后使用 thalemora backup/restore；目标必须为新空目录")
