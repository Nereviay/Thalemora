"""Evidence-key de-duplication at the final Memory VNext assembly boundary.

The old explicit-history lane and the VNext lane search independently.
Exclusions pass canonical IDs to VNext; they do not alter evidence, memory
records, or any provider-stable prompt prefix.
"""

from __future__ import annotations


def _positive_id(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def vnext_exclusions(
    *,
    dependency_candidates=(),
    ephemeral_card_refs=(),
    canonical_raw_event_ids=(),
    history_raw_event_ids=(),
    max_raw_ids=200,
):
    """Prevent VNext from replaying exact already-visible canonical evidence.

    History IDs have priority over the oldest visible canonical events if the
    bounded exclusion list reaches the downstream 200-ID transport limit.
    """
    cards = set()
    for candidate in dependency_candidates or ():
        if not isinstance(candidate, dict):
            continue
        for ref in candidate.get("refs") or ():
            if not isinstance(ref, dict):
                continue
            value = _positive_id(ref.get("card_id"))
            if value is not None:
                cards.add(value)
    for ref in ephemeral_card_refs or ():
        if isinstance(ref, dict):
            value = _positive_id(ref.get("card_id"))
            if value is not None:
                cards.add(value)

    raw = []
    for item in list(canonical_raw_event_ids or ()) + list(history_raw_event_ids or ()):
        value = _positive_id(item)
        if value is not None:
            if value in raw:
                raw.remove(value)
            raw.append(value)
    max_ids = max(1, int(max_raw_ids))
    return {
        "exclude_memory_card_ids": sorted(cards),
        "visible_raw_event_ids": raw[-max_ids:],
    }
