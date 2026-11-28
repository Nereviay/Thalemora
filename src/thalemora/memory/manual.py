"""人工记忆的字段来源、修订和冲突回执。"""
import datetime
import re
from . import revision_store
from .management import freshness_of

def _normalize_csv(value):
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return ",".join(part.strip() for part in str(value or "").split(",") if part.strip())

def _positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None

def create(body, content, title, keywords, *, db_path, embed, embedding_model, evidence_ref):
    try:
        importance = (float(body["importance"]) if body.get("importance") is not None else None)
    except (TypeError, ValueError):
        return {"error": "invalid importance"}, 400
    embedding = embed(
        content,
        caller="manual_create",
        purpose="manual_create_embedding",
    )
    tags = _normalize_csv(body.get("tags")) or "未分类"
    created = revision_store.create_card(
        db_path,
        content=content,
        title=title,
        tags=tags,
        keywords=keywords,
        importance=importance,
        source="manual",
        origin="manual_ui",
        evidence_source_kind="manual",
        evidence_source_ref=evidence_ref,
        embedding=embedding,
        field_sources={
            "title": "user_explicit" if title else "pending",
            "keywords": "user_explicit" if keywords else "pending",
            "importance": "user_explicit" if importance is not None else "pending",
            "tags": "user_explicit" if _normalize_csv(body.get("tags")) else "pending",
        },
        embedding_model=embedding_model,
    )
    return {
        "ok": True,
        "action": "new",
        "card_id": created["card_id"],
        "current_revision_id": created["current_revision_id"],
    }, 200

def update(body, *, db_path, read_card, embed, embedding_model):
    card_id = _positive_int(body.get("card_id"))
    if card_id is None:
        return {"error": "need card_id"}, 400
    if "expected_current_revision_id" not in body:
        return {"error": "expected_current_revision_id required"}, 400
    expected_revision_id = _positive_int(body.get("expected_current_revision_id"))
    if expected_revision_id is None:
        return {"error": "invalid expected_current_revision_id"}, 400

    old = read_card(card_id)
    if old is None:
        return {"error": "card not found"}, 404
    if old.get("current_revision_id") != expected_revision_id:
        return {
            "error": "stale revision",
            "current_revision_id": old.get("current_revision_id"),
        }, 409
    new_content = (
        old["content"]
        if body.get("content") is None
        else str(body["content"]).strip()
    )
    new_title = (
        old["title"] if body.get("title") is None else str(body["title"]).strip()
    )
    new_tags = (
        old["tags"] if body.get("tags") is None else _normalize_csv(body["tags"])
    )
    if not new_content:
        return {"error": "empty content"}, 400

    new_keywords = (
        old["keywords"] if body.get("keywords") is None
        else _normalize_csv(body["keywords"])
    )
    # M12: event time is an explicitly user-editable, versioned fact, not a
    # derivative of when the card was written. Blank/null clears an erroneous
    # date; never retroactively edits original raw evidence.
    happened_at = old.get("happened_at")
    if "happened_at" in body:
        proposed = str(body["happened_at"] or "").strip()
        if proposed:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", proposed):
                return {"error": "happened_at must be YYYY-MM-DD"}, 400
            try:
                datetime.date.fromisoformat(proposed)
            except ValueError:
                return {"error": "invalid happened_at calendar date"}, 400
        happened_at = proposed or None
    try:
        new_importance = (
            old["importance"] if "importance" not in body
            else (None if body["importance"] is None
                  else float(body["importance"]))
        )
    except (TypeError, ValueError):
        return {"error": "invalid importance"}, 400
    content_changed = new_content != old["content"]
    embedding = revision_store.UNCHANGED
    if content_changed:
        embedding = embed(
            new_content,
            caller="manual_update",
            purpose="manual_update_embedding",
        )
    try:
        revision_store.update_card(
            db_path,
            card_id=card_id,
            expected_current_revision_id=expected_revision_id,
            content=new_content,
            title=new_title,
            tags=new_tags,
            keywords=new_keywords,
            importance=new_importance,
            happened_at=happened_at,
            embedding=embedding,
            field_sources={
                field: (
                    "pending" if field == "importance" and new_importance is None
                    else "user_explicit"
                )
                for field in ("title","tags","keywords","importance")
                if field in body and (body[field] is not None or field == "importance")
            },
            embedding_model=embedding_model,
        )
    except revision_store.CardNotFound:
        return {"error": "card not found"}, 404
    except revision_store.RevisionConflict as exc:
        return {
            "error": "stale revision",
            "current_revision_id": exc.current_revision_id,
        }, 409

    card = read_card(card_id)
    card.pop("embedding", None)
    card["freshness"] = freshness_of(card)
    return card, 200
