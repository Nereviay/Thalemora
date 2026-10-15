"""现役 Memory 关键词、合并与 evidence anchor 操作；调用方显式提供 DB。

从念念 revision_store 原样提取，不加载 Provider、不持有生产路径。
"""

import datetime

import hashlib

import json

import sqlite3

from . import revision_store as _revision_core
from . import field_sources as memory_field_sources

class StaleConsolidation(Exception):
    pass

def _split_keywords(raw):
    result = []
    for value in str(raw or '').split(','):
        keyword = value.strip()
        if keyword and keyword not in result:
            result.append(keyword)
    return result

def apply_keyword_intent(db_path, *, card_id, action, keyword):
    """Apply add/remove to the latest projection without caller-side CAS state."""
    if action not in {'add', 'remove'}:
        raise ValueError('invalid keyword action')
    normalized = str(keyword or '').strip()
    if not normalized:
        raise ValueError('empty keyword')
    conn = _revision_core._connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT current_revision_id, keywords FROM cards WHERE id=?', (card_id,)).fetchone()
        if current is None:
            raise _revision_core.CardNotFound(card_id)
        keywords = _split_keywords(current['keywords'])
        if action == 'add':
            if normalized in keywords:
                changed = False
            else:
                keywords.append(normalized)
                changed = True
        else:
            changed = normalized in keywords
            keywords = [item for item in keywords if item != normalized]
        if changed:
            result = _revision_core.update_card_in_transaction(conn, card_id=card_id, expected_current_revision_id=current['current_revision_id'], keywords=','.join(keywords), change_type='manual_edit', origin='manual_ui', evidence_source_kind='manual', evidence_source_ref='app_keywords', ring_by='manual', field_sources={'keywords': 'user_explicit'})
        else:
            result = {'card_id': card_id, 'current_revision_id': current['current_revision_id'], 'changed': False}
        memory_field_sources.mark(conn, card_id, {'keywords': 'user_explicit'})
        result['keywords'] = ','.join(keywords)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def consolidate_card_in_transaction(conn, *, scatter_card_id, expected_scatter_current_revision_id, merged_from_revision_id, origin, **kwargs):
    """Create a consolidate revision and sink its scatter atomically."""
    main_card_id = kwargs.get('card_id')
    expected_main_revision_id = kwargs.get('expected_current_revision_id')
    if main_card_id == scatter_card_id:
        raise _revision_core.InvalidRevisionState('main and scatter cards must differ')
    main = conn.execute('\n        SELECT c.current_revision_id, c.status, c.source,\n               r.origin AS current_revision_origin\n        FROM cards c\n        LEFT JOIN card_revisions r ON r.id=c.current_revision_id\n        WHERE c.id=?\n        ', (main_card_id,)).fetchone()
    if main is None:
        raise _revision_core.CardNotFound(main_card_id)
    if main['current_revision_id'] != expected_main_revision_id:
        raise _revision_core.RevisionConflict(expected_main_revision_id, main['current_revision_id'])
    if main['status'] != 'fresh':
        raise StaleConsolidation('main is no longer fresh')
    if main['source'] == 'manual' and origin != 'manual_ui':
        raise StaleConsolidation('manual-source main cannot be auto-consolidated')
    if main['current_revision_origin'] == 'manual_ui' and origin != 'manual_ui':
        raise StaleConsolidation('manual-ui current main cannot be auto-consolidated')
    scatter = conn.execute('\n        SELECT c.current_revision_id, c.status, c.pinned, c.source,\n               r.origin AS current_revision_origin\n        FROM cards c\n        LEFT JOIN card_revisions r ON r.id=c.current_revision_id\n        WHERE c.id=?\n        ', (scatter_card_id,)).fetchone()
    if scatter is None:
        raise _revision_core.CardNotFound(scatter_card_id)
    if scatter['current_revision_id'] != expected_scatter_current_revision_id:
        raise _revision_core.RevisionConflict(expected_scatter_current_revision_id, scatter['current_revision_id'])
    if merged_from_revision_id != expected_scatter_current_revision_id:
        raise _revision_core.InvalidRevisionState('merged revision must be the scatter current revision')
    merged_revision = conn.execute('SELECT card_id FROM card_revisions WHERE id=?', (merged_from_revision_id,)).fetchone()
    if merged_revision is None or merged_revision['card_id'] != scatter_card_id:
        raise _revision_core.InvalidRevisionState('merged revision does not belong to scatter card')
    if scatter['status'] != 'fresh' or bool(scatter['pinned']):
        raise StaleConsolidation('scatter is no longer fresh and unpinned')
    if scatter['source'] == 'manual' and origin != 'manual_ui':
        raise StaleConsolidation('manual-source scatter cannot be auto-consolidated')
    if scatter['current_revision_origin'] == 'manual_ui' and origin != 'manual_ui':
        raise StaleConsolidation('manual-ui current scatter cannot be auto-consolidated')
    result = _revision_core.update_card_in_transaction(conn, merged_from_revision_id=merged_from_revision_id, change_type='consolidate', origin=origin, **kwargs)
    sunk = conn.execute("\n        UPDATE cards SET status='sunk'\n        WHERE id=? AND current_revision_id=? AND status='fresh' AND pinned=0\n        ", (scatter_card_id, expected_scatter_current_revision_id))
    if sunk.rowcount != 1:
        raise StaleConsolidation('scatter changed before sink')
    return result

def consolidate_card(db_path, **kwargs):
    """Atomically create a consolidate revision and sink the scatter card."""
    conn = _revision_core._connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = consolidate_card_in_transaction(conn, **kwargs)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def bootstrap_legacy_snapshot(db_path, *, card_id, origin='migration', source_ref=None):
    """Attach one legacy_snapshot revision to a projection that still has no revision."""
    conn = _revision_core._connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        card = conn.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()
        if card is None:
            raise _revision_core.CardNotFound(card_id)
        if card['current_revision_id'] is not None:
            conn.commit()
            return {'card_id': card_id, 'current_revision_id': int(card['current_revision_id']), 'changed': False}
        if conn.execute('SELECT 1 FROM card_revisions WHERE card_id=?', (card_id,)).fetchone():
            raise _revision_core.InvalidRevisionState(f'card {card_id} has revisions but no current revision')
        snapshot = {field: card[field] for field in _revision_core._SNAPSHOT_FIELDS}
        revision_id = _revision_core._insert_revision(conn, card_id=card_id, revision_no=1, previous_revision_id=None, merged_from_revision_id=None, snapshot=snapshot, change_type='legacy_snapshot', origin=origin)
        _revision_core._insert_evidence(conn, revision_id, 'legacy_unverified', source_ref or f'card:{card_id}:p3_interim_daily_no_manifest')
        cursor = conn.execute('UPDATE cards SET current_revision_id=? WHERE id=? AND current_revision_id IS NULL', (revision_id, card_id))
        if cursor.rowcount != 1:
            raise _revision_core.RevisionConflict(None, None)
        conn.commit()
        return {'card_id': card_id, 'current_revision_id': int(revision_id), 'changed': True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def revision_content_changed_since(db_path, *, card_id, ancestor_revision_id, current_revision_id):
    """Return (ancestor_reachable, content_changed) for one card revision chain."""
    conn = _revision_core._connect(db_path)
    try:
        ancestor = conn.execute('SELECT id, card_id, content_hash FROM card_revisions WHERE id=?', (ancestor_revision_id,)).fetchone()
        current = conn.execute('SELECT id, card_id, previous_revision_id, content_hash FROM card_revisions WHERE id=?', (current_revision_id,)).fetchone()
        if not ancestor or not current or ancestor['card_id'] != card_id or (current['card_id'] != card_id):
            return (False, True)
        cursor = current
        seen = set()
        while cursor and int(cursor['id']) != int(ancestor_revision_id):
            rid = int(cursor['id'])
            if rid in seen or cursor['previous_revision_id'] is None:
                return (False, True)
            seen.add(rid)
            cursor = conn.execute('SELECT id, card_id, previous_revision_id, content_hash FROM card_revisions WHERE id=?', (cursor['previous_revision_id'],)).fetchone()
            if cursor is None or cursor['card_id'] != card_id:
                return (False, True)
        return (True, ancestor['content_hash'] != current['content_hash'])
    finally:
        conn.close()

def revision_manual_content_changed_since(db_path, *, card_id, ancestor_revision_id, current_revision_id):
    """Return whether reachable manual_edit/correct revisions changed content."""
    conn = _revision_core._connect(db_path)
    try:
        ancestor = conn.execute('SELECT id, card_id FROM card_revisions WHERE id=?', (ancestor_revision_id,)).fetchone()
        cursor = conn.execute('SELECT id, card_id, previous_revision_id, content_hash, change_type FROM card_revisions WHERE id=?', (current_revision_id,)).fetchone()
        if not ancestor or not cursor or ancestor['card_id'] != card_id or (cursor['card_id'] != card_id):
            return (False, True)
        seen = set()
        manual_content_changed = False
        while int(cursor['id']) != int(ancestor_revision_id):
            revision_id = int(cursor['id'])
            if revision_id in seen or cursor['previous_revision_id'] is None:
                return (False, True)
            seen.add(revision_id)
            previous = conn.execute('SELECT id, card_id, previous_revision_id, content_hash, change_type FROM card_revisions WHERE id=?', (cursor['previous_revision_id'],)).fetchone()
            if previous is None or previous['card_id'] != card_id:
                return (False, True)
            if cursor['change_type'] in ('manual_edit', 'correct') and cursor['content_hash'] != previous['content_hash']:
                manual_content_changed = True
            cursor = previous
        return (True, manual_content_changed)
    finally:
        conn.close()

def validate_episodic_anchors(db_path, items):
    """Validate replayable context revisions against current card lineages."""
    if not items:
        return []
    card_ids = sorted({item['card_id'] for item in items})
    marks = ','.join(('?' for _ in card_ids))
    conn = _revision_core._connect(db_path)
    try:
        cards = {int(row['id']): row for row in conn.execute(f'SELECT id, status, current_revision_id, memory_kind FROM cards WHERE id IN ({marks})', card_ids).fetchall()}
        revisions = {int(row['id']): row for row in conn.execute(f'SELECT id, card_id, previous_revision_id, content_hash, change_type FROM card_revisions WHERE card_id IN ({marks})', card_ids).fetchall()}
    finally:
        conn.close()
    results = []
    for item in items:
        card_id = item['card_id']
        anchored_revision_id = item['anchored_revision_id']
        result = {'card_id': card_id, 'anchored_revision_id': anchored_revision_id, 'valid': False, 'reason': 'not_found'}
        card = cards.get(card_id)
        anchored = revisions.get(anchored_revision_id)
        if card is None or anchored is None or int(anchored['card_id']) != card_id:
            results.append(result)
            continue
        expected_kind = 'timeline' if item.get('kind') == 'boot_context' else 'episodic'
        if card['status'] != 'fresh' or card['memory_kind'] != expected_kind:
            result['reason'] = 'status_invalid'
            results.append(result)
            continue
        current_revision_id = card['current_revision_id']
        current = revisions.get(current_revision_id)
        if current is None or int(current['card_id']) != card_id:
            result['reason'] = 'lineage_break'
            results.append(result)
            continue
        if int(current_revision_id) == anchored_revision_id:
            result.update(valid=True, reason='current')
            results.append(result)
            continue
        cursor = current
        seen = set()
        while int(cursor['id']) != anchored_revision_id:
            revision_id = int(cursor['id'])
            previous_revision_id = cursor['previous_revision_id']
            if revision_id in seen or previous_revision_id is None:
                result['reason'] = 'lineage_break'
                break
            seen.add(revision_id)
            previous = revisions.get(previous_revision_id)
            if previous is None or int(previous['card_id']) != card_id:
                result['reason'] = 'lineage_break'
                break
            change_type = cursor['change_type']
            if change_type in ('correct', 'supersede'):
                result['reason'] = 'content_rewrite'
                break
            if change_type == 'manual_edit':
                if cursor['content_hash'] != previous['content_hash']:
                    result['reason'] = 'content_rewrite'
                    break
            elif change_type not in (('derived_refresh',) if item.get('kind') == 'boot_context' else ('extend', 'consolidate')):
                result['reason'] = 'unknown_change_type'
                break
            cursor = previous
        else:
            result.update(valid=True, reason='compatible_lineage')
        results.append(result)
    return results
