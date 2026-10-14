"""复用 Water 已通过的基础词法，仅补来源原有 CSV/合并职责。"""
from thalemora.water._keywords import normalize_keywords, MAX_KEYWORDS

def normalize_csv(value, *, title="", tags="", limit=MAX_KEYWORDS):
    return ",".join(normalize_keywords(value, title=title, tags=tags, limit=limit))

def merge_keywords(old_value, new_value, *, title="", tags="", limit=MAX_KEYWORDS):
    """Keep stable old aliases while reserving room for genuinely new aliases."""
    limit = max(0, int(limit))
    if limit == 0:
        return ""
    old = normalize_keywords(old_value, title=title, tags=tags, limit=limit)
    new = normalize_keywords(new_value, title=title, tags=tags, limit=limit)
    reserve_new = min(2, len(new), limit)
    keep_old = max(0, limit - reserve_new)
    merged = old[:keep_old]
    seen = {item.casefold() for item in merged}
    for item in new + old[keep_old:]:
        if item.casefold() in seen:
            continue
        merged.append(item)
        seen.add(item.casefold())
        if len(merged) >= limit:
            break
    return ",".join(merged)
