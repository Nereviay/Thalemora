"""Read-only extension projection; stored deltas/evidence remain the only authority.

Extensions do not inherit revision evidence or the manual confirmation exception.
A changed base revision is deliberately not rebased (including metadata refreshes).
"""
from contextlib import closing, nullcontext
import sqlite3

from .source_validity import resolve_archive_evidence
from .scopes import SOURCE, OWNER, ownership, belongs, reference_scope, validate_scope


def load_extensions(db):
    """Read V2 if present, without installing/migrating tables; include inactive history."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'memory_extensions', 'memory_extension_evidence'} <= tables:
        return []
    retired = ({r[0] for r in db.execute(
        'SELECT from_card_id FROM memory_card_lineage WHERE retired_at IS NULL')}
        if 'memory_card_lineage' in tables else set())
    evidence = {}
    for row in db.execute('SELECT * FROM memory_extension_evidence ORDER BY id'):
        evidence.setdefault(row['extension_id'], []).append(dict(row))
    out = []
    for row in db.execute(
            'SELECT e.*, c.current_revision_id, c.status AS card_status, c.memory_kind, '
            'c.source AS card_source, r.card_id AS base_card_id, r.origin AS base_origin, '
            'r.change_type AS base_change_type FROM memory_extensions e '
            'JOIN cards c ON c.id=e.card_id LEFT JOIN card_revisions r ON r.id=e.base_revision_id '
            'ORDER BY e.id'):
        item = dict(row)
        if item['status'] != 'active':
            reason = 'inactive_extension'
        elif item['card_status'] != 'fresh' or item['memory_kind'] != 'episodic':
            reason = 'inactive_card'
        elif item['card_id'] in retired:
            reason = 'retired_card'
        elif item['base_card_id'] != item['card_id']:
            reason = 'invalid_base_revision'
        elif item['base_revision_id'] != item['current_revision_id']:
            reason = 'stale_base_revision'
        elif (item['card_source'] == 'manual' or not (
                item['base_origin'] in {'auto_writer', 'agent_mcp', 'bottle_answer'} or
                (item['base_origin'], item['base_change_type']) == ('migration', 'derived_refresh'))):
            reason = 'unsupported_base_revision'
        else:
            reason = 'current'
        item['extension_id'] = item.pop('id')
        item['eligibility'] = reason
        item['evidence_refs'] = evidence.get(item['extension_id'], [])
        out.append(item)
    return out


def scoped_extensions(path, archive_path, agent_id, app_id, channel, *, scope=SOURCE, connection=None):
    """Scope comes from canonical ownership; validity comes only from delta evidence.

    Base text is never injected with a delta. Thus its old Archive evidence need
    not still be current: same-event v2 deltas remain usable while base v1 is
    suppressed by F1. The base *identity* must still be current and active.
    """
    validate_scope(scope)
    with (nullcontext(connection) if connection is not None else closing(sqlite3.connect(path))) as db:
        db.row_factory = sqlite3.Row
        if not db.in_transaction:
            db.execute('BEGIN')
        scopes = ownership(db, agent_id, channel)
        items = load_extensions(db)
    out = []
    with closing(sqlite3.connect(archive_path)) as db:
        for item in items:
            card_scope = scopes.get(item['card_id'])
            if scope == SOURCE and (not belongs(card_scope, app_id, channel) or any(
                    reference_scope(e, agent_id) != (app_id, channel) for e in item['evidence_refs'])):
                continue
            for evidence in item['evidence_refs']:
                own_scope = (card_scope['app'], channel) if card_scope and card_scope['state'] == 'known' else (None, channel)
                state = resolve_archive_evidence(db, evidence, agent_id, *own_scope)
                evidence.update({k: state[k] for k in ('resolution', 'current_source_text', 'verified')})
                # JSON-stable provenance; also used for exact-version raw suppression.
                evidence['source_key'] = list(state['source_key']) if state['source_key'] else None
            if item['eligibility'] == 'current':
                states = [e['resolution'] for e in item['evidence_refs']]
                item['eligibility'] = ('no_evidence' if not states else
                                       next((s for s in states if s != 'current'), 'current'))
                if item['eligibility'] == 'manual_revision':
                    item['eligibility'] = 'unsupported'
            item['eligible'] = item['eligibility'] == 'current'
            out.append(item)
    return out
