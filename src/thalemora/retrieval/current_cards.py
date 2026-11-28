"""召回输出引用当前可见的卡片正文和修订。"""
from thalemora.memory.management import _canonical_filter

def current_items(items, connect):
    """Enrich one recall result batch from the local card projection."""
    card_ids = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            try:
                card_ids.append(int(item[0]))
            except (TypeError, ValueError):
                pass
    if not card_ids:
        return []

    unique_ids = list(dict.fromkeys(card_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    conn = connect()
    try:
        canonical = _canonical_filter(conn)
        rows = conn.execute(
            "SELECT id, content, current_revision_id, memory_kind, status, "
            "title, tags, happened_at, valence, arousal FROM cards "
            f"WHERE status IN ('fresh','sunk') AND id IN ({placeholders}){canonical}",
            unique_ids,
        ).fetchall()
    finally:
        conn.close()
    cards = {int(row["id"]): row for row in rows}

    enriched = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        card_id, _, score = item[:3]
        try:
            lookup_id = int(card_id)
        except (TypeError, ValueError):
            lookup_id = card_id
        card = cards.get(lookup_id)
        if card is None:
            continue
        enriched.append({
            "id": card_id,
            "content": card["content"],
            "score": score,
            "current_revision_id": card["current_revision_id"],
            "memory_kind": card["memory_kind"],
            "status": card["status"],
            "title": card["title"],
            "tags": card["tags"],
            "happened_at": card["happened_at"],
            "valence": card["valence"],
            "arousal": card["arousal"],
        })
    return enriched
