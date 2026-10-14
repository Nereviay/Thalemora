# -*- coding: utf-8 -*-
"""复用正式 Water 建议、审核、移动和撤销算法；调用者提供连接。"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any, Iterable
from contextlib import contextmanager


@contextmanager
def _transaction(connection: sqlite3.Connection):
    """用保存点保证多条写入原子化，保留调用者的外层事务。"""
    connection.execute("SAVEPOINT water_growth")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK TO water_growth")
        connection.execute("RELEASE water_growth")
        raise
    else:
        connection.execute("RELEASE water_growth")

from . import _keywords as keyword_policy


PROPOSAL_KINDS = {"new_branch", "new_stream"}
PROPOSERS = {"system", "agent", "user"}
GENERIC_ANCHORS = {
    "事情",
    "生活",
    "日常",
    "心情",
    "感受",
    "记忆",
    "记录",
    "聊天",
    "对话",
    "我们",
    "项目",
    "工作",
    "其他",
    "未分类",
}
GENERIC_ANCHOR_KEYS = {anchor.casefold() for anchor in GENERIC_ANCHORS}


def ensure(connection: sqlite3.Connection) -> None:
    """Explicitly install the two Water-growth tables."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS water_growth_proposals (
            proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposer TEXT NOT NULL CHECK(proposer IN ('system','agent','user')),
            kind TEXT NOT NULL CHECK(kind IN ('new_branch','new_stream')),
            parent_node_id TEXT NOT NULL REFERENCES memory_water_nodes(node_id),
            title TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','deferred','rejected','accepted')),
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_water_growth_proposal_theme
        ON water_growth_proposals(kind,parent_node_id,lower(title))
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS water_growth_actions (
            action_id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL CHECK(action_type IN ('accept_proposal','move_card')),
            actor TEXT NOT NULL,
            proposal_id INTEGER REFERENCES water_growth_proposals(proposal_id),
            card_id INTEGER REFERENCES cards(id),
            node_id TEXT REFERENCES memory_water_nodes(node_id),
            reason TEXT NOT NULL,
            previous_memberships_json TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            undone_at TEXT
        )
        """
    )


def _keywords(value: Any) -> list[str]:
    if value is None:
        return []
    parsed = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            parsed = json.loads(text)
    return keyword_policy.normalize_keywords(parsed)


def _evidence_rows(
    connection: sqlite3.Connection, card_ids: Iterable[int]
) -> list[dict[str, int]]:
    ids = list(dict.fromkeys(int(card_id) for card_id in card_ids))
    if len(ids) < 3:
        raise ValueError("at least three independent evidence cards are required")
    marks = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT c.id AS card_id,c.current_revision_id AS revision_id
        FROM cards c
        WHERE c.id IN ({marks}) AND c.memory_kind='episodic'
          AND c.status!='trashed' AND c.current_revision_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM memory_card_lineage l
              WHERE l.from_card_id=c.id AND l.retired_at IS NULL
          )
        """,
        ids,
    ).fetchall()
    by_id = {int(row["card_id"]): int(row["revision_id"]) for row in rows}
    if set(by_id) != set(ids):
        raise ValueError("evidence must reference current independent Memory cards")
    return [{"card_id": card_id, "revision_id": by_id[card_id]} for card_id in ids]


def _node(connection: sqlite3.Connection, node_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT node_id,node_type,parent_node_id,title
        FROM memory_water_nodes
        WHERE node_id=? AND retired_at IS NULL
        """,
        (node_id,),
    ).fetchone()
    if row is None:
        raise ValueError("parent or target Water node does not exist")
    return row


def _stream_for_card(connection: sqlite3.Connection, card_id: int) -> str | None:
    row = connection.execute(
        """
        SELECT CASE WHEN n.node_type='stream' THEN n.node_id ELSE n.parent_node_id END
        FROM memory_water_membership m
        JOIN memory_water_nodes n ON n.node_id=m.container_node_id
        WHERE m.card_id=? AND n.retired_at IS NULL
        """,
        (card_id,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _validate_scope(
    connection: sqlite3.Connection,
    kind: str,
    parent_node_id: str,
    evidence: list[dict[str, int]],
) -> None:
    parent = _node(connection, parent_node_id)
    expected_type = "stream" if kind == "new_branch" else "lake"
    if parent["node_type"] != expected_type:
        raise ValueError(f"{kind} requires an active {expected_type} parent")

    for item in evidence:
        stream_id = _stream_for_card(connection, item["card_id"])
        if kind == "new_branch":
            if stream_id != parent_node_id:
                raise ValueError("branch evidence must all belong to its parent Stream")
        elif stream_id is not None:
            stream = _node(connection, stream_id)
            if stream["parent_node_id"] != parent_node_id:
                raise ValueError("stream evidence must belong to its Lake or the Sea")


def create_proposal(
    connection: sqlite3.Connection,
    kind: str,
    parent_node_id: str,
    title: str,
    reason: str,
    evidence_card_ids: Iterable[int],
    proposer: str = "agent",
) -> int:
    """Create an unapproved structure proposal backed by current revisions."""
    if kind not in PROPOSAL_KINDS:
        raise ValueError("unsupported proposal kind")
    if proposer not in PROPOSERS:
        raise ValueError("unsupported proposer")
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("proposal title is required")
    evidence = _evidence_rows(connection, evidence_card_ids)
    _validate_scope(connection, kind, parent_node_id, evidence)
    if connection.execute(
        """
        SELECT 1 FROM memory_water_nodes
        WHERE node_type=? AND parent_node_id=? AND lower(title)=lower(?)
          AND retired_at IS NULL
        """,
        ("branch" if kind == "new_branch" else "stream", parent_node_id, clean_title),
    ).fetchone():
        raise ValueError("an active node with this title already exists")
    try:
        cursor = connection.execute(
            """
            INSERT INTO water_growth_proposals(
                proposer,kind,parent_node_id,title,reason,evidence_json
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                proposer,
                kind,
                parent_node_id,
                clean_title,
                reason.strip(),
                json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("a proposal for this theme already exists") from exc
    return int(cursor.lastrowid)


def _anchor_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Current independent memories and their existing Stream, without writes."""
    return connection.execute(
        """
        SELECT c.id AS card_id,c.current_revision_id,c.keywords,c.title,c.content,
               CASE WHEN n.node_type='stream' THEN n.node_id ELSE n.parent_node_id END
                   AS stream_id
        FROM cards c
        JOIN memory_water_membership m ON m.card_id=c.id
        JOIN memory_water_nodes n ON n.node_id=m.container_node_id
        WHERE c.memory_kind='episodic' AND c.status!='trashed'
          AND c.current_revision_id IS NOT NULL AND n.node_type IN ('stream','branch') AND n.retired_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM memory_card_lineage l
              WHERE l.from_card_id=c.id AND l.retired_at IS NULL
          )
        ORDER BY c.id
        """
    ).fetchall()

_EXPLICIT_ALIAS = re.compile(
    r"(?:名叫|叫)([^，,。；;！？!?\s]{1,18})[，,]\s*"
    r"(?:平时)?(?:也叫|又叫|又名|别名(?:是|为)?)([^。；;！？!?\n]+)"
)


def _alias_topics(rows: list[sqlite3.Row]) -> dict[tuple[str, str], str]:
    """Resolve only explicit 'called A, also called B' evidence in one Stream.

    Co-occurrence and partially overlapping evidence are not identity proof.
    This is a read projection of canonical memories, not a second alias store.
    """
    parents = {
        (str(row["stream_id"]), word.casefold()): word.casefold()
        for row in rows for word in _keywords(row["keywords"])
    }

    def root(stream: str, word: str) -> str:
        while parents[(stream, word)] != word:
            word = parents[(stream, word)]
        return word

    for row in rows:
        stream = str(row["stream_id"])
        text = str(row["title"] or "") + "\n" + str(row["content"] or "")
        for match in _EXPLICIT_ALIAS.finditer(text):
            name = match[1].strip('「」“”"\' ').casefold()
            if (stream, name) not in parents:
                continue
            for alias in _keywords(match[2]):
                alias = alias.strip('「」“”"\' ').casefold()
                if (stream, alias) in parents:
                    parents[(stream, root(stream, alias))] = root(stream, name)
    return {(stream, word): root(stream, word) for stream, word in parents}


def _group_proposals(
    connection: sqlite3.Connection, proposals: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    aliases = _alias_topics(_anchor_rows(connection))
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for proposal in proposals:
        key = ("single", proposal["proposal_id"])
        if proposal["kind"] == "new_branch" and proposal["status"] in {"pending", "deferred"}:
            stream = str(proposal["parent_node_id"])
            title = str(proposal["title"]).casefold()
            key = (proposal["proposer"], proposal["kind"], stream,
                   aliases.get((stream, title), title))
        groups.setdefault(key, []).append(proposal)
    result = []
    for key, items in groups.items():
        if len(items) == 1:
            result.append({**items[0], "proposal_ids": [items[0]["proposal_id"]], "aliases": []})
            continue
        primary = min(items, key=lambda p: (str(p["title"]).casefold() != key[-1], p["proposal_id"]))
        evidence = {int(e["card_id"]): e for p in items for e in json.loads(p["evidence_json"])}
        other_names = sorted({p["title"] for p in items if p["title"] != primary["title"]})
        result.append({
            **primary,
            "status": "pending" if any(p["status"] == "pending" for p in items) else "deferred",
            "proposal_ids": sorted(p["proposal_id"] for p in items),
            "aliases": other_names,
            "reason": f"记忆明确记录了同一对象的别名；合并展示，共 {len(evidence)} 张独立证据。",
            "evidence_json": json.dumps([evidence[i] for i in sorted(evidence)], ensure_ascii=False),
        })
    return result


def discover(connection: sqlite3.Connection, limit: int = 5) -> list[int]:
    """Propose Branches for concrete anchors repeated in one active Stream."""
    limit = min(max(int(limit), 0), 5)
    awaiting = connection.execute(
        "SELECT COUNT(*) FROM water_growth_proposals "
        "WHERE proposer='system' AND status IN ('pending','deferred')"
    ).fetchone()[0]
    limit = min(limit, max(0, 5-int(awaiting)))
    if not limit:
        return []
    rows = _anchor_rows(connection)
    aliases = _alias_topics(rows)
    spellings = {(str(row["stream_id"]), word.casefold()): word
                 for row in rows for word in _keywords(row["keywords"])}
    anchors: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        for anchor in _keywords(row["keywords"]):
            anchor = anchor.strip()
            if anchor and anchor.casefold() not in GENERIC_ANCHOR_KEYS:
                stream = str(row["stream_id"])
                canonical = aliases.get((stream, anchor.casefold()), anchor.casefold())
                key = (stream, canonical)
                topic = anchors.setdefault(key, {"title": spellings[(stream, canonical)], "card_ids": []})
                if int(row["card_id"]) not in topic["card_ids"]:
                    topic["card_ids"].append(int(row["card_id"]))

    existing_titles = {
        str(row["title"]).casefold()
        for row in connection.execute(
            """
            SELECT title FROM memory_water_nodes WHERE retired_at IS NULL
            """
        )
    }
    # An anchor already named by its Stream or Lake is not a new subtopic.
    parent_titles = {
        str(row["stream_id"]): (
            str(row["stream_title"]).casefold(),
            str(row["lake_title"]).casefold(),
        )
        for row in connection.execute(
            """
            SELECT s.node_id AS stream_id, s.title AS stream_title,
                   l.title AS lake_title
            FROM memory_water_nodes s
            JOIN memory_water_nodes l ON l.node_id=s.parent_node_id
            WHERE s.node_type='stream' AND s.retired_at IS NULL
              AND l.node_type='lake' AND l.retired_at IS NULL
            """
        )
    }
    proposed = {
        (str(row["parent_node_id"]), aliases.get(
            (str(row["parent_node_id"]), str(row["title"]).casefold()),
            str(row["title"]).casefold()))
        for row in connection.execute(
            "SELECT parent_node_id,title FROM water_growth_proposals WHERE kind='new_branch'"
        )
    }
    candidates = [
        (stream_id, topic["title"], topic["card_ids"])
        for (stream_id, anchor_key), topic in anchors.items()
        if len(topic["card_ids"]) >= 3
        and anchor_key not in existing_titles
        and not any(anchor_key in title for title in parent_titles.get(stream_id, ()))
        and (stream_id, anchor_key) not in proposed
    ]
    candidates.sort(key=lambda item: (-len(item[2]), item[1].casefold(), item[0]))

    # Multiple keywords backed by the same three memories describe one cluster,
    # not two independent new stories. Respect earlier rejected clusters too.
    seen_groups = {
        (str(row["parent_node_id"]),
         frozenset(int(item["card_id"]) for item in json.loads(row["evidence_json"])))
        for row in connection.execute(
            "SELECT parent_node_id,evidence_json FROM water_growth_proposals "
            "WHERE kind='new_branch'"
        )
    }
    created: list[int] = []
    for stream_id, anchor, card_ids in candidates:
        group = (stream_id, frozenset(card_ids))
        if group in seen_groups:
            continue
        created.append(
            create_proposal(
                connection,
                "new_branch",
                stream_id,
                anchor,
                f"同一 Stream 中有 {len(card_ids)} 张独立当前记忆共享具体锚点「{anchor}」。",
                card_ids,
                proposer="system",
            )
        )
        seen_groups.add(group)
        if len(created) >= limit:
            break
    return created


def _membership_snapshot(connection: sqlite3.Connection, card_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM memory_water_membership WHERE card_id=?", (card_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _replace_membership(
    connection: sqlite3.Connection,
    card_id: int,
    node_id: str | None,
    revision_id: int,
    origin: str,
    reason: str,
) -> None:
    connection.execute("DELETE FROM memory_water_membership WHERE card_id=?", (card_id,))
    if node_id is not None:
        connection.execute(
            """
            INSERT INTO memory_water_membership(
                card_id,container_node_id,based_on_revision_id,origin,
                assignment_reason,created_at,updated_at
            ) VALUES(?,?,?,?,?,datetime('now','localtime'),datetime('now','localtime'))
            """,
            (card_id, node_id, revision_id, origin, reason),
        )


def review_proposal(
    connection: sqlite3.Connection,
    proposal_id: int,
    action: str,
    edited_title: str | None = None,
    actor: str = "user",
    proposal_ids: Iterable[int] | None = None,
) -> int | None:
    """Accept, defer, or reject a proposal; only acceptance mutates Water."""
    if action not in {"accept", "defer", "reject"}:
        raise ValueError("unsupported review action")
    with _transaction(connection):
        proposal = connection.execute(
            "SELECT * FROM water_growth_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if proposal is None or proposal["status"] not in {"pending", "deferred"}:
            raise ValueError("proposal is not reviewable")
        ids = sorted(set(int(i) for i in proposal_ids)) if proposal_ids is not None else [proposal_id]
        if proposal_id not in ids:
            raise ValueError("review must include its primary proposal")
        marks = ",".join("?" for _ in ids)
        items = [dict(p) for p in connection.execute(
            f"SELECT * FROM water_growth_proposals WHERE proposal_id IN ({marks})", ids
        )]
        if len(items) != len(ids) or any(p["status"] not in {"pending", "deferred"} for p in items):
            raise ValueError("proposal is not reviewable")
        groups = _group_proposals(connection, items)
        if len(groups) != 1:
            raise ValueError("combined proposals need explicit same-object alias evidence")
        combined = groups[0]
        if action != "accept":
            connection.executemany(
                "UPDATE water_growth_proposals SET status=?,"
                "updated_at=datetime('now','localtime') WHERE proposal_id=?",
                [("deferred" if action == "defer" else "rejected", i) for i in ids],
            )
            return None

        for item in items:
            previous = json.loads(item["evidence_json"])
            current = _evidence_rows(connection, [e["card_id"] for e in previous])
            if current != previous:
                raise ValueError("proposal evidence revisions are no longer current")
        evidence = json.loads(combined["evidence_json"])
        _validate_scope(connection, proposal["kind"], proposal["parent_node_id"], evidence)
        title = (edited_title if edited_title is not None else proposal["title"]).strip()
        if not title:
            raise ValueError("accepted node title is required")
        node_type = "branch" if proposal["kind"] == "new_branch" else "stream"
        node_id = f"{node_type}:growth:{uuid.uuid4().hex}"
        sort_order = connection.execute(
            """
            SELECT COALESCE(MAX(sort_order),-1)+1 FROM memory_water_nodes
            WHERE node_type=? AND parent_node_id=? AND retired_at IS NULL
            """,
            (node_type, proposal["parent_node_id"]),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO memory_water_nodes(
                node_id,node_type,parent_node_id,title,sort_order,origin,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,datetime('now','localtime'),datetime('now','localtime'))
            """,
            (
                node_id,
                node_type,
                proposal["parent_node_id"],
                title,
                int(sort_order),
                f"water_growth:{proposal['proposer']}",
            ),
        )
        snapshots = []
        for item in evidence:
            snapshots.append(
                {"card_id": item["card_id"], "membership": _membership_snapshot(connection, item["card_id"])}
            )
            _replace_membership(
                connection,
                item["card_id"],
                node_id,
                item["revision_id"],
                f"water_growth:{actor}",
                proposal["reason"],
            )
        cursor = connection.execute(
            """
            INSERT INTO water_growth_actions(
                action_type,actor,proposal_id,node_id,reason,
                previous_memberships_json,details_json
            ) VALUES('accept_proposal',?,?,?,?,?,?)
            """,
            (
                actor,
                proposal_id,
                node_id,
                proposal["reason"],
                json.dumps(snapshots, ensure_ascii=False, separators=(",", ":")),
                json.dumps({"title": title, "kind": proposal["kind"], "proposal_ids": ids}, ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            UPDATE water_growth_proposals
            SET title=?,status='accepted',updated_at=datetime('now','localtime')
            WHERE proposal_id=?
            """,
            (title if len(ids) == 1 else proposal["title"], proposal_id),
        )
        connection.executemany(
            "UPDATE water_growth_proposals SET status='accepted',"
            "updated_at=datetime('now','localtime') WHERE proposal_id=?",
            [(i,) for i in ids if i != proposal_id],
        )
        return int(cursor.lastrowid)


def move_card(
    connection: sqlite3.Connection,
    card_id: int,
    to_node_id: str | None,
    reason: str,
    actor: str = "user",
) -> int:
    """Move one current card to a Stream/Branch or explicitly to the Sea."""
    with _transaction(connection):
        row = connection.execute(
            """
            SELECT id,current_revision_id FROM cards
            WHERE id=? AND memory_kind='episodic' AND current_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM memory_card_lineage l
                  WHERE l.from_card_id=cards.id AND l.retired_at IS NULL
              )
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            raise ValueError("card is not a current Memory")
        if to_node_id is not None:
            target = _node(connection, to_node_id)
            if target["node_type"] not in {"stream", "branch"}:
                raise ValueError("cards can only belong to a Stream or Branch")
        previous = _membership_snapshot(connection, card_id)
        _replace_membership(
            connection,
            int(card_id),
            to_node_id,
            int(row["current_revision_id"]),
            f"water_growth:{actor}",
            reason.strip(),
        )
        cursor = connection.execute(
            """
            INSERT INTO water_growth_actions(
                action_type,actor,card_id,node_id,reason,
                previous_memberships_json,details_json
            ) VALUES('move_card',?,?,?,?,?,?)
            """,
            (
                actor,
                card_id,
                to_node_id,
                reason.strip(),
                json.dumps(
                    [{"card_id": int(card_id), "membership": previous}],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps({"to_node_id": to_node_id}, ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid)


def _restore_snapshot(connection: sqlite3.Connection, item: dict[str, Any]) -> None:
    card_id = int(item["card_id"])
    old = item["membership"]
    connection.execute("DELETE FROM memory_water_membership WHERE card_id=?", (card_id,))
    if old is not None:
        columns = (
            "card_id,container_node_id,based_on_revision_id,origin,assignment_reason,"
            "created_at,updated_at"
        )
        connection.execute(
            f"INSERT INTO memory_water_membership({columns}) VALUES(?,?,?,?,?,?,?)",
            tuple(old[name] for name in columns.split(",")),
        )


def undo_action(connection: sqlite3.Connection, action_id: int, actor: str = "user") -> None:
    """Restore an action's exact previous memberships and retire an unused new node."""
    _ = actor
    with _transaction(connection):
        row = connection.execute(
            "SELECT * FROM water_growth_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None or row["undone_at"] is not None:
            raise ValueError("action is not undoable")
        snapshots = json.loads(row["previous_memberships_json"])
        touched = {int(item["card_id"]) for item in snapshots}
        # Undo only the latest effective change to a card; otherwise an older
        # Undo could silently overwrite a newer choice made in the Water UI.
        newer = connection.execute(
            "SELECT card_id,previous_memberships_json FROM water_growth_actions "
            "WHERE action_id>? AND undone_at IS NULL",
            (action_id,),
        )
        for action in newer:
            affected = (
                {int(action["card_id"])} if action["card_id"] is not None
                else {int(item["card_id"]) for item in json.loads(action["previous_memberships_json"])}
            )
            if touched & affected:
                raise ValueError("a newer Water action changed the same memory; undo that action first")
        for item in snapshots:
            _restore_snapshot(connection, item)
        if row["action_type"] == "accept_proposal" and row["node_id"] is not None:
            occupied = connection.execute(
                "SELECT 1 FROM memory_water_membership WHERE container_node_id=? LIMIT 1",
                (row["node_id"],),
            ).fetchone()
            if occupied is None:
                connection.execute(
                    """
                    UPDATE memory_water_nodes
                    SET retired_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                    WHERE node_id=? AND retired_at IS NULL
                    """,
                    (row["node_id"],),
                )
        connection.execute(
            """
            UPDATE water_growth_actions
            SET undone_at=datetime('now','localtime') WHERE action_id=?
            """,
            (action_id,),
        )
        if row["action_type"] == "accept_proposal" and row["proposal_id"] is not None:
            details = json.loads(row["details_json"])
            ids = details.get("proposal_ids") or [row["proposal_id"]]
            connection.executemany(
                "UPDATE water_growth_proposals SET status='deferred',"
                "updated_at=datetime('now','localtime') WHERE proposal_id=?",
                [(i,) for i in ids],
            )


def read_state(
    connection: sqlite3.Connection,
    limit: int = 20,
    proposal_offset: int = 0,
    action_offset: int = 0,
    sea_offset: int = 0,
) -> dict[str, list[dict[str, Any]]]:
    """Read bounded proposal/action/Sea history without initializing schema."""
    limit = min(max(int(limit), 1), 100)
    proposals = [
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM water_growth_proposals
            ORDER BY proposal_id DESC LIMIT ? OFFSET ?
            """,
            (limit, max(int(proposal_offset), 0)),
        )
    ]
    proposals = _group_proposals(connection, proposals)
    evidence_ids = {
        int(item["card_id"])
        for proposal in proposals
        for item in json.loads(proposal["evidence_json"])
    }
    titles: dict[int, str] = {}
    if evidence_ids:
        marks = ",".join("?" for _ in evidence_ids)
        titles = {
            int(row["id"]): str(row["title"] or "")
            for row in connection.execute(
                f"SELECT id,title FROM cards WHERE id IN ({marks})", tuple(evidence_ids)
            )
        }
    for proposal in proposals:
        evidence = json.loads(proposal.pop("evidence_json"))
        proposal["evidence"] = [
            {**item, "title": titles.get(int(item["card_id"]), "")[:80]}
            for item in evidence
        ]

    actions = [
        dict(row)
        for row in connection.execute(
            """
            SELECT action_id,action_type,actor,proposal_id,card_id,node_id,reason,
                   created_at,undone_at
            FROM water_growth_actions ORDER BY action_id DESC LIMIT ? OFFSET ?
            """,
            (limit, max(int(action_offset), 0)),
        )
    ]
    sea = [
        dict(row)
        for row in connection.execute(
            """
            SELECT c.id AS card_id,substr(COALESCE(c.title,''),1,80) AS title,
                   c.current_revision_id AS _revision_id,c.source AS _source,
                   a.reason,a.created_at,a.actor
            FROM cards c
            LEFT JOIN water_growth_actions a ON a.action_id=(
                SELECT a2.action_id FROM water_growth_actions a2
                WHERE a2.card_id=c.id AND a2.action_type='move_card'
                  AND json_extract(a2.details_json,'$.to_node_id') IS NULL
                  AND a2.undone_at IS NULL
                ORDER BY a2.action_id DESC LIMIT 1
            )
            WHERE c.memory_kind='episodic' AND c.status!='trashed'
              AND NOT EXISTS (
                  SELECT 1 FROM memory_water_membership m WHERE m.card_id=c.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM memory_card_lineage l
                  WHERE l.from_card_id=c.id AND l.retired_at IS NULL
              )
            ORDER BY COALESCE(a.action_id,0) DESC,c.id DESC LIMIT ? OFFSET ?
            """,
            (limit, max(int(sea_offset), 0)),
        )
    ]
    maintenance_tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('memory_maintenance_receipts','memory_maintenance_state')"
        )
    }
    state = (
        connection.execute(
            "SELECT last_revision_id FROM memory_maintenance_state WHERE id=1"
        ).fetchone()
        if "memory_maintenance_state" in maintenance_tables else None
    )
    for item in sea:
        revision_id = item.pop("_revision_id")
        source = item.pop("_source")
        if item["actor"] is not None:
            item["reason"] = item["reason"] or "已手动留在 Sea。"
            continue
        receipt = (
            connection.execute(
                "SELECT changes_json FROM memory_maintenance_receipts "
                "WHERE card_id=? AND json_extract(changes_json,'$.sea')=1 "
                "ORDER BY source_revision_id DESC,created_at DESC LIMIT 1",
                (item["card_id"],),
            ).fetchone()
            if "memory_maintenance_receipts" in maintenance_tables else None
        )
        if receipt:
            info = json.loads(receipt[0])
            item["reason"] = "上次整理后留在 Sea：" + (
                info.get("sea_reason") or "没有匹配到合适的现有溪流。"
            )
        elif state and source in {"auto", "manual", "agent"} and revision_id > state[0]:
            item["reason"] = "等待下一轮水系整理，尚未判断归属。"
        else:
            item["reason"] = "暂未归属，未找到水系整理记录。"
    return {"proposals": proposals, "actions": actions, "sea": sea}
