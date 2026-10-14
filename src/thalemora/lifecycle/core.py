"""生命周期领域写入；唯一 cards authority 与派生层在同一事务中更新。"""
from __future__ import annotations

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

from .activation import ActivationLedger, connect_existing, parse_time
from .scoring import _authority_blocked, dormancy_score, summarize


def _cards(conn):
    return [dict(row) for row in conn.execute(
        "SELECT c.id,c.title,c.importance,c.arousal,c.created_at,c.status,c.current_revision_id,"
        "c.pinned,c.source,c.memory_kind,COALESCE(r.origin,'') AS revision_origin "
        'FROM cards c LEFT JOIN card_revisions r ON r.id=c.current_revision_id'
    )]


def _lineage_blocked(conn, cid):
    return conn.execute(
        'SELECT 1 FROM memory_card_lineage WHERE from_card_id=? AND retired_at IS NULL',
        (cid,),
    ).fetchone() is not None


def _revision_times(conn):
    # 按修订 id 读取最新有语义修订；迁入/合并技术记录也不成为互动活跃。
    return {row['id']: row['created_at'] for row in conn.execute(
        'SELECT c.id,(SELECT r.created_at FROM card_revisions r WHERE r.card_id=c.id '
        "AND r.change_type NOT IN ('derived_refresh','legacy_snapshot','consolidate') AND r.origin!='migration' "
        'ORDER BY r.id DESC LIMIT 1) AS created_at FROM cards c'
    )}


def _dormancy(conn, activations, *, now, archive_days, min_dormancy_days, score_threshold):
    now = parse_time(now.isoformat())
    revisions = _revision_times(conn)
    candidates = []
    for card in _cards(conn):
        if (card['status'] != 'fresh' or _authority_blocked(card)
                or _lineage_blocked(conn, card['id'])):
            continue
        activation = activations.get(card['id'], {})
        meta = dormancy_score(
            card, now=now, archive_days=archive_days,
            revision_created_at=revisions.get(card['id']) or '',
            activation_weight=activation.get('weight_sum', 0.0),
            activation_last_at=activation.get('last_at', ''),
        )
        if meta['days'] >= max(1.0, float(min_dormancy_days)) and meta['score'] < score_threshold:
            candidates.append(dict(card, **meta, proposed_status='sunk',
                                   semantic_status='dormant', would_archive=False))
    return sorted(candidates, key=lambda item: (item['score'], -item['days'], item['id']))


def dormancy_candidates(*, memory_db, activation: ActivationLedger, now,
                        archive_days=30.0, min_dormancy_days=30.0, score_threshold=1.8):
    """只读建议；执行时在锁内重核同一评分、版本及人工权威。"""
    with closing(connect_existing(memory_db, readonly=True)) as conn:
        return _dormancy(conn, activation.summary(), now=now, archive_days=archive_days,
                         min_dormancy_days=min_dormancy_days, score_threshold=score_threshold)


def _reactivation(conn, activation, *, now, lookback_days, locked=False):
    now = parse_time(now.isoformat())
    users, sinks = activation.epochs(conn if locked else None)
    pending = []
    for card in _cards(conn):
        cid = card['id']
        if (card['status'] != 'sunk' or _authority_blocked(card)
                or card['source'] != 'auto' or cid not in users or cid not in sinks
                or _lineage_blocked(conn, cid)):
            continue
        user_at, user_stamp = users[cid]
        sunk_at, sunk_stamp, revision = sinks[cid]
        created = parse_time(card['created_at'])
        if (created is None or revision != card['current_revision_id']
                or user_at <= sunk_at or user_at < created):
            continue
        age = (now - user_at).total_seconds() / 86400
        if 0 <= age <= lookback_days:
            pending.append(dict(id=cid, title=card['title'], current_revision_id=revision,
                                activation_at=user_stamp, last_sunk_at=sunk_stamp,
                                days_since_user_reactivation=round(age, 2),
                                from_status='sunk', proposed_status='fresh'))
    return sorted(pending, key=lambda item: (item['activation_at'], item['id']), reverse=True)


def reactivation_candidates(*, memory_db, activation, now, lookback_days=14.0):
    """只有显式来源用户事件晚于当前修订的沉底回执，才能恢复。"""
    with closing(connect_existing(memory_db, readonly=True)) as conn:
        return _reactivation(conn, activation, now=now, lookback_days=lookback_days)


@contextmanager
def _transaction(memory_db, activation, retrieval_db=None):
    paths = [Path(memory_db).resolve(), Path(activation.db_path).resolve()]
    if retrieval_db is not None:
        paths.append(Path(retrieval_db).resolve())
    for i, path in enumerate(paths):
        if not path.is_file():
            raise FileNotFoundError(path)
        if any(path.samefile(other) for other in paths[:i]):
            raise ValueError('Memory, activation and retrieval databases must be distinct')
    with closing(connect_existing(memory_db)) as conn:
        conn.execute('ATTACH DATABASE ? AS act', (paths[1].as_uri() + '?mode=rw',))
        if retrieval_db is not None:
            conn.execute('ATTACH DATABASE ? AS idx', (paths[2].as_uri() + '?mode=rw',))
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn


def _sink_key(revision, epoch):
    if not str(epoch or '').strip():
        raise ValueError('explicit lifecycle epoch required')
    return f'lifecycle:rev:{int(revision)}:{epoch}'


def _record_sunk(conn, candidates, ids, *, activation, now, epoch):
    source = {int(item['id']): item for item in candidates}
    count = duplicates = 0
    for cid in dict.fromkeys(int(cid) for cid in ids):
        revision = source[cid]['current_revision_id']
        row = conn.execute('SELECT status,current_revision_id FROM cards WHERE id=?', (cid,)).fetchone()
        if row is None or row['status'] != 'sunk' or row['current_revision_id'] != revision:
            raise ValueError(f'sink receipt does not match canonical state:{cid}')
        result = activation.record(cid, 'lifecycle_sunk', _sink_key(revision, epoch),
                                   now=now, connection=conn)
        count += int(result['recorded'])
        duplicates += int(not result['recorded'])
    return dict(sunk_epoch_receipts=count, duplicate_receipts=duplicates,
                receipt_failed_card_ids=[], auto_reactivation_allowed_only_if_receipt_exists=True)


def record_lifecycle_sunk_epochs(candidates, sunk_ids, *, memory_db, activation, now, epoch):
    """显式补记已沉底卡片的真实 epoch；不得由历史缺口推断调用此函数。

    正常写入使用 apply_dormancy，其状态与回执同时提交。此入口仅供调用方持有
    实际沉底操作证据时使用；本层校验现态/修订，不能验证外部历史作者。
    """
    with _transaction(memory_db, activation) as conn:
        return _record_sunk(conn, candidates, sunk_ids, activation=activation, now=now, epoch=epoch)


def _index_status(conn, cid, status):
    cur = conn.execute(
        'UPDATE idx.chunks SET status=? WHERE source_kind=\'memory_card\' AND source_id=?',
        (status, cid),
    )
    if cur.rowcount != 1:
        raise sqlite3.IntegrityError(f'index card count mismatch:{cid}:{cur.rowcount}')


def apply_dormancy(candidates, *, memory_db, activation, retrieval_db, now, epoch,
                   archive_days=30.0, min_dormancy_days=30.0, score_threshold=1.8):
    """新鲜→沉底；批内任一步失败则 canonical、索引、回执全部回滚。

    同一 epoch 重放不会再次沉底恢复过的卡片。并发冲突保留原态并返回 id。
    """
    candidates = list(candidates)
    changed, conflicts, replayed = [], [], []
    with _transaction(memory_db, activation, retrieval_db) as conn:
        eligible = {item['id']: item for item in _dormancy(
            conn, activation.summary(conn), now=now, archive_days=archive_days,
            min_dormancy_days=min_dormancy_days, score_threshold=score_threshold,
        )}
        for item in candidates:
            cid, revision = int(item['id']), item['current_revision_id']
            key = _sink_key(revision, epoch)
            if conn.execute(
                "SELECT 1 FROM act.memory_activation_events WHERE card_id=? "
                "AND event_type='lifecycle_sunk' AND source_key=?", (cid, key),
            ).fetchone():
                replayed.append(cid)
                continue
            current = eligible.pop(cid, None)
            if current is None or current['current_revision_id'] != revision:
                conflicts.append(cid)
                continue
            conn.execute("UPDATE cards SET status='sunk' WHERE id=?", (cid,))
            _index_status(conn, cid, 'sunk')
            changed.append(cid)
        receipts = _record_sunk(conn, candidates, changed, activation=activation, now=now, epoch=epoch)
    return dict(sunk_count=len(changed), sunk_card_ids=changed,
                indexed_status_updates=len(changed), index_sync_ok=True, index_sync_error=None,
                conflict_card_ids=conflicts, replayed_card_ids=replayed, **receipts)


def apply_reactivation(candidates, *, memory_db, activation, retrieval_db, now, lookback_days=14.0):
    """沉底→恢复；锁内重读显式用户来源、当前沉底 epoch 与人工权威。"""
    changed, conflicts = [], []
    with _transaction(memory_db, activation, retrieval_db) as conn:
        eligible = {item['id']: item for item in _reactivation(
            conn, activation, now=now, lookback_days=lookback_days, locked=True,
        )}
        for item in candidates:
            cid = int(item['id'])
            current = eligible.pop(cid, None)
            if current is None or any(current[key] != item.get(key) for key in (
                'current_revision_id', 'activation_at', 'last_sunk_at',
            )):
                conflicts.append(cid)
                continue
            conn.execute("UPDATE cards SET status='fresh' WHERE id=?", (cid,))
            _index_status(conn, cid, 'fresh')
            changed.append(cid)
    return dict(reactivated_count=len(changed), reactivated_card_ids=changed,
                indexed_status_updates=len(changed), index_sync_ok=True, index_sync_error=None,
                conflict_card_ids=conflicts)
