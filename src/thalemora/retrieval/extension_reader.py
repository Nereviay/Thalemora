# -*- coding: utf-8 -*-
"""Read-only Memory Extension V2 detail and lineage eligibility helpers."""
import os
import re
import sqlite3
from urllib.parse import quote

from . import bm25
from thalemora.memory import evidence_resolver


DEFAULT_RECALL_LIMIT = 2
DEFAULT_RECALL_CONTENT_BUDGET = 360
_KEYWORD_SPLIT_RE = re.compile(r"[,，、;；\n]+")


def _readonly_connection(path):
    absolute_path = os.path.abspath(os.fspath(path))
    connection = sqlite3.connect(
        f"file:{quote(absolute_path)}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection, table_name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _eligibility(extension, current_revision_id, revisions):
    if extension["status"] != "active":
        return False, "status_inactive"

    base_revision_id = int(extension["base_revision_id"])
    if current_revision_id is None:
        return False, "lineage_break"
    current_revision_id = int(current_revision_id)
    base = revisions.get(base_revision_id)
    current = revisions.get(current_revision_id)
    card_id = int(extension["card_id"])
    if (
        base is None
        or current is None
        or int(base["card_id"]) != card_id
        or int(current["card_id"]) != card_id
    ):
        return False, "lineage_break"
    if base_revision_id == current_revision_id:
        return True, "current"

    cursor = current
    seen = set()
    while int(cursor["id"]) != base_revision_id:
        revision_id = int(cursor["id"])
        previous_revision_id = cursor["previous_revision_id"]
        if revision_id in seen or previous_revision_id is None:
            return False, "lineage_break"
        seen.add(revision_id)
        previous = revisions.get(int(previous_revision_id))
        if previous is None or int(previous["card_id"]) != card_id:
            return False, "lineage_break"
        if cursor["content_hash"] != previous["content_hash"]:
            return False, "content_rewrite"
        cursor = previous
    return True, "compatible_lineage"


def list_card_extensions(card_id, *, memory_db_path, raw_db_path):
    """Return a card's extensions and computed recall eligibility, read-only."""
    connection = _readonly_connection(memory_db_path)
    try:
        if not _table_exists(connection, "memory_extensions"):
            return []
        extensions = connection.execute(
            """
            SELECT id, card_id, base_revision_id, content, keywords,
                   importance, happened_at, origin, source, status,
                   same_memory_confidence, created_at
            FROM memory_extensions
            WHERE card_id=?
            ORDER BY id
            """,
            (card_id,),
        ).fetchall()
        if not extensions:
            return []
        card = connection.execute(
            "SELECT current_revision_id FROM cards WHERE id=?", (card_id,)
        ).fetchone()
        revision_rows = connection.execute(
            """
            SELECT id, card_id, previous_revision_id, content_hash
            FROM card_revisions
            WHERE card_id=?
            """,
            (card_id,),
        ).fetchall()
    finally:
        connection.close()

    current_revision_id = card["current_revision_id"] if card else None
    revisions = {int(row["id"]): row for row in revision_rows}
    results = []
    for extension in extensions:
        eligible, reason = _eligibility(
            extension, current_revision_id, revisions
        )
        results.append({
            "id": int(extension["id"]),
            "card_id": int(extension["card_id"]),
            "base_revision_id": int(extension["base_revision_id"]),
            "content": extension["content"],
            "keywords": extension["keywords"],
            "importance": extension["importance"],
            "happened_at": extension["happened_at"],
            "origin": extension["origin"],
            "source": extension["source"],
            "status": extension["status"],
            "confidence": extension["same_memory_confidence"],
            "created_at": extension["created_at"],
            "evidence": evidence_resolver.resolve_extension_evidence(
                int(extension["id"]),
                memory_db_path=memory_db_path,
                raw_db_path=raw_db_path,
            ),
            "auto_recall_eligible": eligible,
            "auto_recall_reason": reason,
        })
    return results


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _keyword_phrases(value):
    return {
        phrase.strip().lower()
        for phrase in _KEYWORD_SPLIT_RE.split(str(value or ""))
        if phrase.strip()
    }


def _lexical_relevance(query, extension):
    """Return a deterministic local relevance score and its matched terms."""
    normalized_query = str(query or "").strip().lower()
    query_tokens = set(bm25.tokenize(normalized_query))
    if not query_tokens:
        return 0.0, []

    content_tokens = set(bm25.tokenize(str(extension.get("content") or "")))
    phrases = _keyword_phrases(extension.get("keywords"))
    keyword_tokens = {
        token for phrase in phrases for token in bm25.tokenize(phrase)
    }
    content_overlap = query_tokens & content_tokens
    keyword_overlap = query_tokens & keyword_tokens
    phrase_overlap = {
        phrase for phrase in phrases if phrase and phrase in normalized_query
    }
    matched = content_overlap | keyword_overlap | phrase_overlap
    if not matched:
        return 0.0, []

    score = (
        len(content_overlap)
        + 2 * len(keyword_overlap)
        + 3 * len(phrase_overlap)
    )
    return float(score), sorted(matched)


def select_card_extensions(
        query, card_id, *, limit=DEFAULT_RECALL_LIMIT,
        content_budget=DEFAULT_RECALL_CONTENT_BUDGET,
        memory_db_path, raw_db_path):
    """Select relevant eligible extensions after the parent card was recalled."""
    extensions = list_card_extensions(
        card_id,
        memory_db_path=memory_db_path,
        raw_db_path=raw_db_path,
    )
    eligible = [item for item in extensions if item.get("auto_recall_eligible") is True]
    ranked = []
    for item in eligible:
        score, matched_terms = _lexical_relevance(query, item)
        if score <= 0:
            continue
        ranked.append((item, score, matched_terms))
    ranked.sort(key=lambda entry: (
        entry[1],
        _safe_float(entry[0].get("importance")),
        str(entry[0].get("happened_at") or ""),
        int(entry[0].get("id") or 0),
    ), reverse=True)

    selected = []
    budget_used = 0
    for item, score, matched_terms in ranked:
        content = str(item.get("content") or "")
        content_chars = len(content)
        if content_chars > content_budget - budget_used:
            continue
        selected.append({
            "id": item["id"],
            "content": content,
            "happened_at": item.get("happened_at"),
            "keywords": item.get("keywords"),
            "evidence": item.get("evidence", []),
            "score": score,
            "reason": "lexical_overlap:" + ",".join(matched_terms),
        })
        budget_used += content_chars
        if len(selected) >= limit:
            break

    trace = {
        "card_id": int(card_id),
        "extension_ids": [item["id"] for item in selected],
        "budget_used": budget_used,
        "candidate_count": len(extensions),
        "eligible_count": len(eligible),
    }
    return selected, trace
