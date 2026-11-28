"""现有单用户卡库的管理与查询，连接由宿主显式提供。"""
from contextlib import closing
from .revision_store import _embedding_values
import datetime
import sqlite3
import json
def _canonical_filter(conn, alias="cards"):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_card_lineage'"
    ).fetchone()
    if not exists:
        return ""
    return (
        " AND NOT EXISTS (SELECT 1 FROM memory_card_lineage l "
        f"WHERE l.from_card_id={alias}.id AND l.retired_at IS NULL)"
    )


def day_source_available(card, read_day):
    day = str(card.get("happened_at") or card.get("created_at") or "")[:10]
    return len(day) == 10 and bool(read_day(day))


def freshness_of(card):
    """新鲜度：按最后翻卡时间算，没翻过按写入时间。
    近7天=fresh热 / 近30天=warm / 更早=cold。"""
    base = str(card.get("last_hit_at") or card.get("created_at") or "")[:19]
    if not base:
        return "cold"
    try:
        d = datetime.datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "cold"
    days = (datetime.datetime.now() - d).days
    if days <= 7:
        return "fresh"
    if days <= 30:
        return "warm"
    return "cold"
class CardManagement:
    def __init__(self, connect):
        self.connect = connect

    def get_card(self, card_id, ring_limit=None):
        """读一张卡；ring_limit 仅控制返回的 legacy 年轮数量。"""
        conn = self.connect()
        card = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not card:
            conn.close(); return None
        ring_total = conn.execute(
            "SELECT COUNT(*) FROM rings WHERE card_id=?", (card_id,)
        ).fetchone()[0]
        if ring_limit is None:
            rings = conn.execute(
                "SELECT content, created_at, by FROM rings WHERE card_id=? ORDER BY id",
                (card_id,),
            ).fetchall()
        elif ring_limit <= 0:
            rings = []
        else:
            rings = conn.execute(
                "SELECT content, created_at, by FROM rings WHERE card_id=? ORDER BY id DESC LIMIT ?",
                (card_id, int(ring_limit)),
            ).fetchall()[::-1]
        conn.close()
        d = dict(card)
        d["embedding"] = json.loads(d["embedding"].decode()) if d["embedding"] else None
        d["rings"] = [dict(r) for r in rings]
        d["rings_total"] = int(ring_total)
        return d

    def search_keyword(self, keyword, limit=10, include_sunk=False, include_archived=False, episodic_only=False):
        """关键词检索（默认只捞浮着的，归档卡不参与自动检索）
        include_sunk=True 连沉底一起；include_archived=True 连归档一起（手动搜归档区用）。"""
        conn = self.connect()
        if include_sunk and include_archived:
            cond = "status != 'trashed'"
        elif include_sunk:
            cond = "status IN ('fresh','sunk')"
        elif include_archived:
            cond = "status IN ('fresh','archived')"
        else:
            cond = "status IN ('fresh')"
        card_columns = {row[1] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
        kind_cond = " AND memory_kind='episodic'" if episodic_only and "memory_kind" in card_columns else ""
        canonical = _canonical_filter(conn)
        rows = conn.execute(
            f"SELECT * FROM cards WHERE {cond}{kind_cond}{canonical} AND "
            "(content LIKE ? OR title LIKE ? OR tags LIKE ? OR keywords LIKE ?) "
            "ORDER BY pinned DESC, importance DESC, hits DESC LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def list_fresh(self, limit=200, include_sunk=False, include_archived=False):
        """列出卡片（面板用）。默认只浮着的，include_sunk=True 连沉底一起列出；
        include_archived=True 时归档卡也列出（归档区用）。"""
        conn = self.connect()
        if include_sunk and include_archived:
            cond = "status != 'trashed'"
        elif include_sunk:
            cond = "status IN ('fresh','sunk')"
        elif include_archived:
            cond = "status IN ('fresh','archived')"
        else:
            cond = "status IN ('fresh')"
        rows = conn.execute(
            f"SELECT * FROM cards WHERE {cond}{_canonical_filter(conn)} ORDER BY pinned DESC, "
            "CASE status WHEN 'fresh' THEN 0 ELSE 1 END, last_hit_at DESC, created_at DESC LIMIT ?",
            (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def latest(self):
        """最近发生变化的一条记忆，供面板顶部“最新记忆”卡使用。

        新建卡、legacy ring、P3 canonical revision 都可成为活动；迁移生成的
        legacy_snapshot 不算新活动，也不把 recall hit 当成更新。
        """
        conn = self.connect()
        canonical = _canonical_filter(conn, "c")
        row = conn.execute(
            f"""
            WITH activities AS (
                SELECT id AS card_id, created_at AS activity_at,
                       content AS activity_content, 'created' AS activity_kind,
                       2 AS activity_priority, id AS activity_id
                FROM cards
                UNION ALL
                SELECT card_id, created_at, content, 'ring', 1, id
                FROM rings
                UNION ALL
                SELECT card_id, created_at, content, 'revision', 3, id
                FROM card_revisions
                WHERE change_type NOT IN ('legacy_snapshot','derived_refresh')
                  AND NOT EXISTS (
                      SELECT 1 FROM card_evidence e
                      WHERE e.revision_id=card_revisions.id
                        AND e.source_ref LIKE 'keyword_maintenance:%'
                  )
            )
            SELECT c.*, a.activity_at AS latest_activity_at,
                   a.activity_content AS latest_activity_content,
                   a.activity_kind AS latest_activity_kind
            FROM activities a
            JOIN cards c ON c.id=a.card_id
            WHERE c.status != 'trashed'
              {canonical}
            ORDER BY a.activity_at DESC, a.activity_priority DESC,
                     a.activity_id DESC, c.id DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def list_archived(self, limit=200):
        """列出归档卡（status='archived'），归档区用。"""
        conn = self.connect()
        rows = conn.execute(
            "SELECT * FROM cards WHERE status='archived' " + _canonical_filter(conn) + " "
            "ORDER BY pinned DESC, last_hit_at DESC, created_at DESC LIMIT ?",
            (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def list_trashed(self, limit=200):
        """列出回收站卡（status='trashed'），回收站用。"""
        conn = self.connect()
        rows = conn.execute(
            "SELECT * FROM cards WHERE status='trashed' "
            "ORDER BY pinned DESC, last_hit_at DESC, created_at DESC LIMIT ?",
            (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def list_by_day(self):
        """按日期分组列出有 happened_at 的卡片（每张含 id/title/importance/content 前100字），无日期的排除"""
        conn = self.connect()
        rows = conn.execute(
            "SELECT id, title, importance, content, happened_at FROM cards "
            "WHERE happened_at IS NOT NULL AND happened_at != '' "
            "AND memory_kind='timeline' AND tags='日子' "
            "AND status != 'trashed' "
            "ORDER BY happened_at DESC").fetchall()
        conn.close()
        days = {}
        for r in rows:
            day = str(r["happened_at"])[:10]
            days.setdefault(day, []).append({
                "id": r["id"],
                "title": r["title"],
                "importance": r["importance"],
                "content": (r["content"] or "")[:100],
            })
        return days

    def stale_fresh(self, days=30, min_importance=0.0):
        """自动遗忘候选：fresh 且非置顶，超过 days 天没被翻的卡（last_hit_at 为 NULL 按 created_at）。
        min_importance>0 时只捞重要性低于该值的（低重要性优先遗忘）。
        只读不改库，返回 [{id,title,importance,created_at,last_hit_at}]；幂等由 status='fresh' 保证。"""
        conn = self.connect()
        sql = ("SELECT id, title, importance, created_at, last_hit_at FROM cards "
               "WHERE status='fresh' AND pinned=0 AND ("
               "(last_hit_at IS NOT NULL AND last_hit_at < datetime('now','localtime', ?)) OR "
               "(last_hit_at IS NULL AND created_at < datetime('now','localtime', ?)))")
        params = [f"-{days} days", f"-{days} days"]
        if min_importance > 0:
            sql += " AND importance < ?"
            params.append(min_importance)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def sink_card(self, card_id):
        """单卡沉底：status 改成 sunk（不删）；普通自动召回不捞，显式历史召回可 include_sunk 找回。"""
        conn = self.connect()
        cur = conn.execute("UPDATE cards SET status='sunk' WHERE id=?", (card_id,))
        n = cur.rowcount
        conn.commit(); conn.close()
        return n

    def archive_card(self, card_id):
        """归档：status 改成 archived（不删），不再参与自动召回/注入"""
        conn = self.connect()
        cur = conn.execute("UPDATE cards SET status='archived' WHERE id=?", (card_id,))
        n = cur.rowcount
        conn.commit(); conn.close()
        return n

    def unarchive_card(self, card_id):
        """取消归档：status 改回 fresh（回到活跃区）"""
        conn = self.connect()
        cur = conn.execute("UPDATE cards SET status='fresh' WHERE id=?", (card_id,))
        n = cur.rowcount
        conn.commit(); conn.close()
        return n

    def trash_card(self, card_id):
        """软删：status 改成 trashed，保留卡片 id 和年轮。"""
        conn = self.connect()
        cur = conn.execute("UPDATE cards SET status='trashed' WHERE id=?", (card_id,))
        n = cur.rowcount
        conn.commit(); conn.close()
        return n

    def restore_card(self, card_id):
        """从回收站恢复：status 改回 fresh。"""
        conn = self.connect()
        cur = conn.execute("UPDATE cards SET status='fresh' WHERE id=?", (card_id,))
        n = cur.rowcount
        conn.commit(); conn.close()
        return n

    def purge_card(self, card_id):
        """彻底删除：物理删除 cards 行和该卡的 rings 行。"""
        conn = self.connect()
        conn.execute("DELETE FROM rings WHERE card_id=?", (card_id,))
        cur = conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
        n = cur.rowcount
        conn.commit(); conn.close()
        return n

    def stats(self):
        """统计（面板用）"""
        conn = self.connect()
        canonical = _canonical_filter(conn)
        total = conn.execute("SELECT COUNT(*) FROM cards WHERE status != 'trashed'" + canonical).fetchone()[0]
        fresh = conn.execute("SELECT COUNT(*) FROM cards WHERE status='fresh'" + canonical).fetchone()[0]
        sunk = conn.execute("SELECT COUNT(*) FROM cards WHERE status='sunk'" + canonical).fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM cards WHERE status='archived'" + canonical).fetchone()[0]
        rings = conn.execute(
            "SELECT COUNT(*) FROM rings WHERE card_id IN ("
            "SELECT id FROM cards WHERE status != 'trashed'" + canonical + ")"
        ).fetchone()[0]
        conn.close()
        return {"total": total, "fresh": fresh, "sunk": sunk,
                "archived": archived, "rings": rings}

    def ensure_injection_hit_receipts(self):
        """最终上下文中可见的卡与修订，一个 trace 记录一次。"""
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_injection_receipts (
                    trace_id TEXT NOT NULL, card_id INTEGER NOT NULL,
                    revision_id INTEGER NOT NULL DEFAULT 0, injected_at TEXT NOT NULL,
                    PRIMARY KEY (trace_id, card_id, revision_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_injection_receipts_card
                  ON memory_injection_receipts(card_id, injected_at DESC);
            """)

    def record_provider_injection_receipt(self, trace_id, card_id, revision_id=0):
        trace = str(trace_id or "").strip()
        if not trace or len(trace) > 160:
            raise ValueError("trace_id required")
        try:
            cid, rid = int(card_id), int(revision_id or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid card_id/revision_id") from exc
        if cid <= 0 or rid < 0:
            raise ValueError("invalid card_id/revision_id")
        self.ensure_injection_hit_receipts()
        with closing(self.connect()) as db, db:
            return db.execute(
                "INSERT OR IGNORE INTO memory_injection_receipts "
                "(trace_id,card_id,revision_id,injected_at) "
                "SELECT ?, id, ?, ? FROM cards WHERE id=?",
                (trace, rid, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cid),
            ).rowcount == 1


    def ensure_embedding_freshness(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(cards)")}
            for column in ("embedding_content_hash", "embedding_model", "embedded_at"):
                if column not in columns:
                    db.execute(f"ALTER TABLE cards ADD COLUMN {column} TEXT")

    def backfill_embeddings(self, embed, model, *, batch, caller, purpose):
        self.ensure_embedding_freshness()
        with closing(self.connect()) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(cards)")}
            kind = " AND memory_kind='episodic'" if "memory_kind" in columns else ""
            rows = db.execute("SELECT id, content FROM cards WHERE embedding IS NULL" + kind).fetchall()
            updated = 0
            for start in range(0, len(rows), batch):
                group = rows[start:start + batch]
                vectors = list(embed([row[1] for row in group], caller=caller, purpose=purpose))
                if len(vectors) != len(group) or any(not vector for vector in vectors):
                    raise ValueError("embedding provider returned incomplete batch")
                with db:
                    for (cid, content), vector in zip(group, vectors):
                        updated += db.execute(
                            "UPDATE cards SET embedding=?, embedding_content_hash=?, embedding_model=?, "
                            "embedded_at=? WHERE id=? AND content=? AND embedding IS NULL",
                            (*_embedding_values(content, vector, model), cid, content),
                        ).rowcount
            return updated
