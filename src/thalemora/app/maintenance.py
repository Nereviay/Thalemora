"""显式、可恢复的 Archive → Scene → Writer → Gatekeeper → Memory 单次后台任务。

只在显式 tick / 独立 worker 中调用配置的 Provider，不在导入或检索中调用模型。
Scene/ops 是派生进度；Archive、revision_store、Reflection 各自保留正式权威。
"""
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
import sqlite3

from thalemora.gatekeeper import core as gate, ops
from thalemora.memory import revision_store
from thalemora.scene.archive import ArchiveSceneRuntime


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return closing(db)


def setup(application, agent_id, app_id, channel):
    archive = application.archive(agent_id, app_id, channel)
    with connect(archive.path) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS maintenance_scopes("
                   "id INTEGER PRIMARY KEY,app TEXT NOT NULL,channel TEXT NOT NULL,UNIQUE(app,channel))")
        db.execute("INSERT OR IGNORE INTO maintenance_scopes(app,channel) VALUES(?,?)", (app_id, channel))
        scope = db.execute("SELECT id FROM maintenance_scopes WHERE app=? AND channel=?",
                           (app_id, channel)).fetchone()[0]
    path = archive.path.with_name(f"maintenance_{scope}.sqlite")
    with connect(path) as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS maintenance_jobs(
                job_id TEXT PRIMARY KEY,scene_id TEXT NOT NULL,scene_revision INTEGER NOT NULL,
                sources TEXT NOT NULL,status TEXT NOT NULL,error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS maintenance_calls(
                id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,module TEXT NOT NULL,
                status TEXT NOT NULL,synthetic INTEGER NOT NULL CHECK(synthetic IN (0,1)),
                usage TEXT,output TEXT,actual_tokens INTEGER,error TEXT,provider_call_id INTEGER,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS maintenance_reflections(
                candidate_id TEXT PRIMARY KEY,card_id INTEGER NOT NULL,revision_id INTEGER NOT NULL,
                text TEXT,status TEXT NOT NULL,error TEXT,item_id TEXT);
        """)
        db.execute("BEGIN IMMEDIATE")
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='maintenance_calls'").fetchone()[0]
        if "CHECK(synthetic=1)" in schema:
            # v0.5.0 的审计表只允许合成调用；保留全部旧回执再一次性升级。
            db.execute("CREATE TABLE maintenance_calls_v2("
                       "id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,module TEXT NOT NULL,"
                       "status TEXT NOT NULL,synthetic INTEGER NOT NULL CHECK(synthetic IN (0,1)),"
                       "usage TEXT,output TEXT,actual_tokens INTEGER,error TEXT,provider_call_id INTEGER,"
                       "created_at TEXT NOT NULL)")
            db.execute("INSERT INTO maintenance_calls_v2 "
                       "SELECT id,job_id,module,status,synthetic,usage,output,actual_tokens,error,NULL,created_at "
                       "FROM maintenance_calls")
            db.execute("DROP TABLE maintenance_calls")
            db.execute("ALTER TABLE maintenance_calls_v2 RENAME TO maintenance_calls")
    with closing(ops.connect_writer_ops(path)):
        pass
    return archive, path, scope


def maintenance_status(application, agent_id, app_id, channel="main", *, limit=20):
    application.page(limit, 0)
    _, path, _ = setup(application, agent_id, app_id, channel)
    with connect(path) as db:
        state = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='scene_runtime_state'").fetchone():
            row = db.execute("SELECT * FROM scene_runtime_state WHERE singleton=1").fetchone()
            state = dict(row) if row else None
        result = {"jobs": [dict(r) for r in db.execute(
            "SELECT * FROM maintenance_jobs ORDER BY updated_at DESC,job_id LIMIT ?", (limit,))],
            "candidates": [dict(r) for r in db.execute(
                "SELECT * FROM memory_candidates ORDER BY updated_at DESC,candidate_id LIMIT ?", (limit,))],
            "calls": [dict(r) for r in db.execute(
                "SELECT * FROM maintenance_calls ORDER BY id DESC LIMIT ?", (limit,))],
            "reflections": [dict(r) for r in db.execute(
                "SELECT * FROM maintenance_reflections ORDER BY candidate_id LIMIT ?", (limit,))]}
    return {"agent_id": agent_id, "app": app_id, "channel": channel,
            "provider_mode": "explicit_provider", "scene_progress": state, **result}


def call(provider, path, job_id, module, payload):
    with connect(path) as db, db:
        call_id = db.execute("INSERT INTO maintenance_calls(job_id,module,status,synthetic,created_at) "
                             "VALUES(?,?,'pending',?,?)", (job_id, module, int(provider.synthetic), now())).lastrowid
    try:
        response = provider.call(module, payload)
        # 合成用量不冒充实际 token；真实调用关联同一 Providers 审计回执。
        output = response["output"]
        usage = encoded(response.get("usage"))
        with connect(path) as db, db:
            db.execute("UPDATE maintenance_calls SET status='success',usage=?,output=?,actual_tokens=?,"
                       "provider_call_id=? WHERE id=?",
                       (usage, encoded(output), None if provider.synthetic else response["usage"]["total_tokens"],
                        response.get("call_id"), call_id))
        return output
    except Exception as exc:
        with connect(path) as db, db:
            db.execute("UPDATE maintenance_calls SET status='error',error=? WHERE id=?", (str(exc), call_id))
        raise


def job_state(path, job_id, status, error=None):
    with connect(path) as db, db:
        db.execute("UPDATE maintenance_jobs SET status=?,error=?,updated_at=? WHERE job_id=?",
                   (status, error, now(), job_id))


def materialize(routes, sources, agent_id, app_id, channel, job_id):
    """只适配 Archive 引用；不伪造 raw exact/verified 证据。"""
    if not isinstance(routes, list) or len(routes) > 8:
        raise ValueError("Writer candidates must be a list of at most 8")
    by_id = {r["source_event_id"]: r for r in sources if not r["deleted"]}
    candidates = []
    for index, route in enumerate(routes):
        if not isinstance(route.get("content"), str) or not route["content"].strip():
            raise ValueError("Writer content required")
        evidence = []
        if not isinstance(route.get("evidence"), list) or not route["evidence"]:
            raise ValueError("Writer exact Archive quotes required")
        roles = []
        for quote in route["evidence"]:
            row = by_id.get(quote.get("source_event_id"))
            text = quote.get("quote")
            if (row is None or type(quote.get("version")) is not int or row["version"] != quote["version"]
                    or not isinstance(text, str) or not text or text not in (row["text"] or "")):
                raise ValueError("Writer evidence is not a current exact Archive quote")
            roles.append(row["role"])
            evidence.append({"source_kind": "derived", "source_ref": encoded({
                "kind": "archive_quote", "agent_id": agent_id, "app": app_id, "channel": channel,
                "source_event_id": row["source_event_id"], "version": row["version"],
                "quote": text, "hash_verification": row["hash_verification"],
                "content_hash": row["content_hash"]})})
        if route.get("claim_scope") == "user_fact" and "user" not in roles:
            raise ValueError("user_fact requires a user Archive quote")
        # Gatekeeper receives provenance roles without claiming raw verification.
        payload = {key: route[key] for key in ("content", "title", "keywords", "importance",
                   "memory_type", "claim_scope", "identity", "reason") if key in route}
        payload["memory_type"] = route.get("memory_type", "event")
        payload["source_roles"] = [f"{role}_text_exact" for role in sorted(set(roles))]
        payload["evidence_rows"] = evidence
        payload["happened_at"] = next((r["occurred_at"] for r in sources if not r["deleted"]), None)
        candidates.append({"candidate_id": f"{job_id}:{index}", "payload": payload})
    return candidates


def freeze(memory_path, agent_id, app_id, channel):
    from thalemora.memory.scopes import ownership, belongs
    with connect(memory_path) as db:
        db.execute('BEGIN')
        ids = [cid for cid, scope in ownership(db, agent_id, channel).items() if belongs(scope, app_id, channel)]
        return gate.freeze_candidate_map_from_card_ids(sorted(ids), memory_db_path=memory_path,
                                                       include_read_only=True, connection=db)


def applied_receipt(path, candidate_id, result):
    # Queue before the ops receipt: recovery can reconstruct either from canonical Memory.
    with connect(path) as db, db:
        db.execute("INSERT OR IGNORE INTO maintenance_reflections VALUES(?,?,?,NULL,'pending',NULL,NULL)",
                   (candidate_id, result["card_id"], result["current_revision_id"]))
    ops.mark_candidate_applied(candidate_id, result["card_id"], result["current_revision_id"], path)


def commit(application, archive, memory_path, sources, app_id, channel, candidate_id, payload, decision, frozen):
    # Source write lock covers the canonical Memory transaction. No model runs under either lock.
    with connect(archive.path) as source_db, source_db:
        source_db.execute("BEGIN IMMEDIATE")
        archive._authorize(app_id, channel)
        for source in sources:
            row = source_db.execute("SELECT version,deleted FROM history_events WHERE app=? AND channel=? "
                                    "AND source_event_id=?", (app_id, channel, source["source_event_id"])).fetchone()
            if row is None or row["version"] != source["version"] or row["deleted"] != source["deleted"]:
                raise ValueError("stale_archive_source")
        from thalemora.memory.scopes import SOURCE, ownership, belongs
        with application._memory_transaction(archive.agent_id, app_id, channel, SOURCE, memory_path) as db:
            target = decision.get("target_card_id")
            if target is not None and not belongs(ownership(db, archive.agent_id, channel).get(target), app_id, channel):
                raise ValueError("target_outside_archive_scope")
            result = gate._commit(decision, payload, frozen, memory_db_path=memory_path,
                                  candidate_id=candidate_id, source="auto", origin="auto_writer",
                                  evidence_source_kind=None, evidence_source_ref=None,
                                  evidence_rows=payload["evidence_rows"], connection=db)
            if decision['action'] == 'new':
                db.execute("INSERT OR IGNORE INTO archive_card_scopes VALUES(?,?,?)",
                           (result["card_id"], app_id, channel))
        return result


def reflect(application, agent_id, app_id, channel, path, provider, limit):
    errors = []
    with connect(path) as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM maintenance_reflections WHERE status!='done' ORDER BY candidate_id LIMIT ?", (limit,))]
    for row in rows:
        try:
            from thalemora.memory.scopes import SOURCE, require_card
            memory_path = application.memory_path(agent_id, app_id, channel)
            with connect(memory_path) as db:
                db.execute('BEGIN')
                require_card(db, row['card_id'], agent_id, app_id, channel, SOURCE)
                revision = db.execute("SELECT content,title FROM card_revisions WHERE id=? AND card_id=?",
                                      (row["revision_id"], row["card_id"])).fetchone()
            text = row["text"]
            if text is None:
                if revision is None:
                    raise ValueError("reflection_target_revision_missing")
                output = call(provider, path, row["candidate_id"], "reflection", {
                    "instruction": "Only suggest a review of this exact Memory revision. Never publish domain state.",
                    "target_kind": "memory", "target_id": str(row["card_id"]),
                    "target_version": row["revision_id"], "revision": dict(revision)})
                text = output["text"]
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
                    raise ValueError("invalid reflection suggestion")
                with connect(path) as db, db:
                    db.execute("UPDATE maintenance_reflections SET text=? WHERE candidate_id=?", (text, row["candidate_id"]))
            with connect(memory_path) as db:
                require_card(db, row['card_id'], agent_id, app_id, channel, SOURCE)
            result = application.reflect(agent_id, app_id, channel, text=text, target_kind="memory",
                                         target_id=str(row["card_id"]), target_version=row["revision_id"],
                                         recorded_at=now(), source_ref=row["candidate_id"])
            with connect(path) as db, db:
                db.execute("UPDATE maintenance_reflections SET status='done',error=NULL,item_id=? WHERE candidate_id=?",
                           (result["item_id"], row["candidate_id"]))
        except Exception as exc:
            errors.append(f"reflection:{type(exc).__name__}:{exc}")
            with connect(path) as db, db:
                db.execute("UPDATE maintenance_reflections SET status='error',error=? WHERE candidate_id=?",
                           (str(exc), row["candidate_id"]))
    return errors


def maintenance_tick(application, agent_id, app_id, channel="main", *, limit=4, provider=None):
    application.page(limit, 0)
    from .maintenance_provider import MaintenanceProvider
    if provider is not None and not isinstance(provider, MaintenanceProvider) and getattr(provider, "synthetic", False) is not True:
        raise ValueError("provider 须为 MaintenanceProvider 或显式 synthetic=True 的合成实现")
    archive, path, scope = setup(application, agent_id, app_id, channel)
    # Process death releases this lock. Across-process ticks cannot duplicate model/commit work.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with connect(path) as db, db:
            db.execute("UPDATE maintenance_calls SET status='interrupted',error='process_interrupted_usage_unknown' "
                       "WHERE status='pending'")
        engine = ArchiveSceneRuntime(archive, app_id, channel, path)
        engine.refresh_sources()
        progress = engine.sync(limit=1000)
        backlog = any(r["archive_rowid"] > progress["last_processed_raw_event_id"] for r in engine.rows())
        memory_path = application.memory_path(agent_id, app_id, channel)
        # Completed receipts remain durable; changed Scene versions get a new job identity.
        for summary in reversed(engine.store.list_scenes(states=["finalized"])):
            scene = engine.store.get_scene(summary["scene_id"])
            job_id = f"archive:{agent_id}:{scope}:{scene['scene_id']}:{scene['current_revision_no']}"
            with connect(path) as db, db:
                db.execute("INSERT OR IGNORE INTO maintenance_jobs VALUES(?,?,?,?,?,NULL,0,?)",
                           (job_id, scene["scene_id"], scene["current_revision_no"],
                            encoded(engine.source_snapshot(scene)), "pending", now()))
        with connect(path) as db:
            jobs = [dict(r) for r in db.execute(
                "SELECT * FROM maintenance_jobs WHERE status NOT IN ('done','stale') ORDER BY updated_at,job_id LIMIT ?", (limit,))]
        errors = []
        for job in jobs:
            candidate_id = None
            sources = json.loads(job["sources"])
            scene = engine.store.get_scene(job["scene_id"])
            source_refs = [engine._event_ref(engine.event(source)) for source in sources]
            if (scene["current_revision_no"] != job["scene_revision"] or scene["projection_status"] != "current"
                    or encoded(engine.source_snapshot(scene)) != job["sources"]
                    or source_refs != scene["events"]):
                for candidate in ops.get_pending_candidates(job["job_id"], path):
                    prior = revision_store.find_candidate_mutation(memory_path, candidate["candidate_id"])
                    if prior:
                        applied_receipt(path, candidate["candidate_id"], prior)
                    else:
                        ops.mark_candidate_failed(candidate["candidate_id"], "stale_archive_or_scene", path)
                        ops.mark_candidate_dropped(candidate["candidate_id"], path)
                job_state(path, job["job_id"], "stale", "archive_or_scene_changed")
                continue
            if provider is None or backlog:
                job_state(path, job["job_id"], "blocked", "scene_backlog" if backlog else "provider_unconfigured")
                continue
            try:
                with connect(path) as db, db:
                    db.execute("UPDATE maintenance_jobs SET attempts=attempts+1 WHERE job_id=?", (job["job_id"],))
                if ops.get_writer_batch(job["job_id"], path) is None:
                    live = [r for r in sources if not r["deleted"] and r["text"]]
                    if not live:
                        job_state(path, job["job_id"], "done", "no_live_source")
                        continue
                    output = call(provider, path, job["job_id"], "writer", {
                        "instruction": "Extract durable Scene memories only; return candidates with content, memory_type, "
                                       "claim_scope and exact evidence [{source_event_id,version,quote}]. Empty list means discard.",
                        "scene_id": job["scene_id"], "scene_revision": job["scene_revision"], "sources": live})
                    candidates = materialize(output["candidates"], sources, agent_id, app_id, channel, job["job_id"])
                    ids = [r["archive_rowid"] for r in sources]
                    ops.put_writer_batch(dict(batch_key=job["job_id"], raw_start_id=min(ids), raw_end_id=max(ids),
                                              input_hash=job["sources"], extractor_model=("synthetic-injected" if provider.synthetic
                                                                                         else provider.model("writer")),
                                              policy_version="archive-scene-v1"), candidates, path)
                job_state(path, job["job_id"], "extracted")
                for candidate in ops.get_pending_candidates(job["job_id"], path):
                    candidate_id = candidate["candidate_id"]
                    # Crash after canonical commit, before ops receipt: never call Gatekeeper or write again.
                    prior = revision_store.find_candidate_mutation(memory_path, candidate_id)
                    if prior:
                        result = {"card_id": prior["card_id"], "current_revision_id": prior["current_revision_id"]}
                    else:
                        payload = json.loads(candidate["payload_json"])
                        frozen = freeze(memory_path, agent_id, app_id, channel)
                        decision, frozen = gate.decide(payload, frozen, provider=lambda messages: call(
                            provider, path, job["job_id"], "gatekeeper", {"messages": messages}))
                        ops.record_gate_result(candidate_id, gate.gate_input_hash(payload, frozen, {
                            "model": "synthetic-injected" if provider.synthetic else provider.model("gatekeeper"),
                            "thinking_enabled": False, "reasoning_effort": None}),
                            decision["reason"], increment=True, db_path=path)
                        if decision["action"] == "drop":
                            ops.mark_candidate_dropped(candidate_id, path, reason=decision["reason"])
                            continue
                        if decision["action"] == "uncertain":
                            ops.update_candidate_payload(candidate_id, payload, "uncertain", path)
                            continue
                        result = commit(application, archive, memory_path, sources, app_id, channel,
                                        candidate_id, payload, decision, frozen)
                    applied_receipt(path, candidate_id, result)
                pending = ops.get_pending_candidates(job["job_id"], path)
                job_state(path, job["job_id"], "pending" if pending else "done")
                if not pending:
                    engine.mark_considered(job["scene_id"], job["scene_revision"], "processed", "archive_tick")
            except Exception as exc:
                errors.append(f"{type(exc).__name__}:{exc}")
                if candidate_id is not None:
                    ops.mark_candidate_failed(candidate_id, f"{type(exc).__name__}:{exc}", path)
                job_state(path, job["job_id"], "error", f"{type(exc).__name__}:{exc}")
        if provider is not None:
            errors.extend(reflect(application, agent_id, app_id, channel, path, provider, limit))
    return {"progress": progress, "errors": errors,
            **maintenance_status(application, agent_id, app_id, channel, limit=limit)}
