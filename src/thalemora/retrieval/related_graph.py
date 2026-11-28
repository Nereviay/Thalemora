from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from itertools import combinations

import numpy as np

from thalemora.gatekeeper.keywords import normalize_keywords


POLICY_VERSION = "memory_related_graph:v1"
PROVIDER_GRAPH_USE_DEFAULT = False

MIN_SHARED_KEYWORDS = 2
MIN_COSINE = 0.65
SINGLE_KEYWORD_COSINE = 0.83
MAX_EDGES_PER_CARD = 3
MAX_TOTAL_EDGES = 500
MAX_KEYWORD_CARD_FANOUT = 50

# These pass the general keyword policy but are still too broad to establish a
# graph edge.  They may accompany concrete terms; they are never evidence here.
GRAPH_GENERIC_KEYWORDS = {
    "python",
    "工作",
    "编程",
    "开发",
    "代码",
    "项目",
    "软件",
    "技术",
}


GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_related_edges (
    card_a_id INTEGER NOT NULL,
    revision_a_id INTEGER NOT NULL CHECK(revision_a_id > 0),
    card_b_id INTEGER NOT NULL,
    revision_b_id INTEGER NOT NULL CHECK(revision_b_id > 0),
    relation TEXT NOT NULL CHECK(relation = 'related'),
    weight REAL NOT NULL,
    cosine REAL NOT NULL,
    shared_keywords_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    built_at TEXT NOT NULL,
    PRIMARY KEY(card_a_id, card_b_id, relation),
    CHECK(card_a_id < card_b_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_related_edges_b
ON memory_related_edges(card_b_id, relation);
CREATE TABLE IF NOT EXISTS memory_related_graph_build (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    policy_version TEXT NOT NULL,
    source_max_revision_id INTEGER NOT NULL,
    active_card_count INTEGER NOT NULL,
    indexed_card_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    built_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class _Card:
    card_id: int
    revision_id: int
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class _Edge:
    card_a_id: int
    revision_a_id: int
    card_b_id: int
    revision_b_id: int
    cosine: float
    shared_keywords: tuple[str, ...]
    reason: str

    @property
    def weight(self) -> float:
        return self.cosine


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _read_only(path: str) -> sqlite3.Connection:
    absolute = os.path.abspath(os.path.expanduser(path))
    connection = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _concrete_keywords(value: object, *, title: str) -> tuple[str, ...]:
    normalized = normalize_keywords(value, title=title)
    return tuple(
        item for item in normalized if item.casefold() not in GRAPH_GENERIC_KEYWORDS
    )


def _load_active_cards(memory_db: str) -> dict[int, _Card]:
    with _read_only(memory_db) as connection:
        has_lineage = _table_exists(connection, "memory_card_lineage")
        lineage_filter = (
            "AND NOT EXISTS ("
            "SELECT 1 FROM memory_card_lineage l "
            "WHERE l.from_card_id=cards.id AND l.retired_at IS NULL"
            ") "
            if has_lineage
            else ""
        )
        rows = connection.execute(
            "SELECT id,title,keywords,current_revision_id FROM cards "
            "WHERE status!='trashed' AND memory_kind='episodic' "
            "AND current_revision_id>0 "
            + lineage_filter
            + "ORDER BY id"
        ).fetchall()

    return {
        int(row["id"]): _Card(
            card_id=int(row["id"]),
            revision_id=int(row["current_revision_id"]),
            keywords=_concrete_keywords(row["keywords"], title=str(row["title"] or "")),
        )
        for row in rows
    }


def _decode_vector(row: sqlite3.Row) -> np.ndarray | None:
    blob = row["vector"]
    dimension = row["vector_dim"]
    if blob is None or dimension is None or int(dimension) <= 0:
        return None
    vector = np.frombuffer(blob, dtype=np.float32)
    if vector.size != int(dimension) or not np.isfinite(vector).all():
        return None
    norm = float(np.linalg.norm(vector))
    if norm <= 0.0 or not math.isfinite(norm):
        return None
    return vector.copy()


def _load_current_vectors(index_db: str, cards: dict[int, _Card]) -> dict[int, np.ndarray]:
    vectors: dict[int, np.ndarray] = {}
    with _read_only(index_db) as connection:
        if not _table_exists(connection, "chunks"):
            raise RuntimeError("retrieval index has no chunks table")
        rows = connection.execute(
            "SELECT source_id,provenance_json,vector,vector_dim FROM chunks "
            "WHERE source_kind='memory_card' AND vector IS NOT NULL ORDER BY source_id"
        ).fetchall()

    for row in rows:
        card_id = int(row["source_id"])
        card = cards.get(card_id)
        if card is None:
            continue
        try:
            provenance = json.loads(str(row["provenance_json"] or "{}"))
            indexed_card_id = int(provenance.get("card_id", card_id))
            indexed_revision = int(provenance["revision_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if indexed_card_id != card_id or indexed_revision != card.revision_id:
            continue
        vector = _decode_vector(row)
        if vector is not None:
            vectors[card_id] = vector
    return vectors


def _cosine(left: np.ndarray, right: np.ndarray) -> float | None:
    if left.shape != right.shape:
        return None
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 0.0 or not math.isfinite(denominator):
        return None
    value = float(np.dot(left, right) / denominator)
    if not math.isfinite(value):
        return None
    return max(-1.0, min(1.0, value))


def _distinctive_single_keyword(keyword: str) -> bool:
    compact = "".join(character for character in keyword if character.isalnum())
    if not compact or compact.isdigit():
        return False
    has_cjk = any("\u4e00" <= character <= "\u9fff" for character in compact)
    return len(compact) >= (3 if has_cjk else 7)


def _candidate_pairs(cards: dict[int, _Card], indexed_ids: set[int]) -> dict[tuple[int, int], tuple[str, ...]]:
    postings: dict[str, list[tuple[int, str]]] = {}
    for card_id in sorted(indexed_ids):
        for keyword in cards[card_id].keywords:
            postings.setdefault(keyword.casefold(), []).append((card_id, keyword))

    shared: dict[tuple[int, int], dict[str, str]] = {}
    for normalized, entries in postings.items():
        if len(entries) > MAX_KEYWORD_CARD_FANOUT:
            continue
        for left, right in combinations(entries, 2):
            pair = (left[0], right[0])
            shared.setdefault(pair, {})[normalized] = left[1]
    return {
        pair: tuple(values[key] for key in sorted(values))
        for pair, values in shared.items()
    }


def _select_edges(
    cards: dict[int, _Card],
    vectors: dict[int, np.ndarray],
    *,
    max_edges_per_card: int,
    max_total_edges: int,
) -> tuple[list[_Edge], int]:
    pairs = _candidate_pairs(cards, set(vectors))
    eligible: list[_Edge] = []
    for (card_a_id, card_b_id), shared_keywords in pairs.items():
        cosine = _cosine(vectors[card_a_id], vectors[card_b_id])
        if cosine is None:
            continue
        if len(shared_keywords) >= MIN_SHARED_KEYWORDS and cosine >= MIN_COSINE:
            reason = "two_or_more_keywords_and_high_cosine"
        elif (
            len(shared_keywords) == 1
            and _distinctive_single_keyword(shared_keywords[0])
            and cosine >= SINGLE_KEYWORD_COSINE
        ):
            reason = "distinctive_keyword_and_very_high_cosine"
        else:
            continue
        eligible.append(
            _Edge(
                card_a_id=card_a_id,
                revision_a_id=cards[card_a_id].revision_id,
                card_b_id=card_b_id,
                revision_b_id=cards[card_b_id].revision_id,
                cosine=cosine,
                shared_keywords=shared_keywords,
                reason=reason,
            )
        )

    eligible.sort(
        key=lambda edge: (
            -edge.weight,
            -len(edge.shared_keywords),
            edge.card_a_id,
            edge.card_b_id,
        )
    )
    degree: dict[int, int] = {}
    selected: list[_Edge] = []
    for edge in eligible:
        if len(selected) >= max_total_edges:
            break
        if degree.get(edge.card_a_id, 0) >= max_edges_per_card:
            continue
        if degree.get(edge.card_b_id, 0) >= max_edges_per_card:
            continue
        selected.append(edge)
        degree[edge.card_a_id] = degree.get(edge.card_a_id, 0) + 1
        degree[edge.card_b_id] = degree.get(edge.card_b_id, 0) + 1
    selected.sort(key=lambda edge: (edge.card_a_id, edge.card_b_id))
    return selected, len(eligible)


def _safe_samples(edges: list[_Edge], limit: int = 5) -> list[dict]:
    return [
        {
            "card_a_id": edge.card_a_id,
            "revision_a_id": edge.revision_a_id,
            "card_b_id": edge.card_b_id,
            "revision_b_id": edge.revision_b_id,
            "score": round(edge.weight, 6),
            "reason": edge.reason,
            "shared_keyword_count": len(edge.shared_keywords),
        }
        for edge in edges[:limit]
    ]


def rebuild(
    memory_db: str,
    index_db: str,
    *,
    dry_run: bool = True,
    max_edges_per_card: int = MAX_EDGES_PER_CARD,
    max_total_edges: int = MAX_TOTAL_EDGES,
) -> dict:
    """Compute the graph and, unless ``dry_run``, replace derived edges.

    Both source reads are read-only.  Write mode creates/replaces only the two
    ``memory_related_*`` tables in the retrieval index.
    """
    if not 0 < int(max_edges_per_card) <= MAX_EDGES_PER_CARD:
        raise ValueError(f"max_edges_per_card must be within 1..{MAX_EDGES_PER_CARD}")
    if not 0 < int(max_total_edges) <= MAX_TOTAL_EDGES:
        raise ValueError(f"max_total_edges must be within 1..{MAX_TOTAL_EDGES}")
    cards = _load_active_cards(memory_db)
    vectors = _load_current_vectors(index_db, cards)
    edges, candidate_count = _select_edges(
        cards,
        vectors,
        max_edges_per_card=int(max_edges_per_card),
        max_total_edges=int(max_total_edges),
    )
    built_at = _now()
    report = {
        "mode": "dry_run" if dry_run else "run",
        "policy_version": POLICY_VERSION,
        "provider_graph_use_default": PROVIDER_GRAPH_USE_DEFAULT,
        "active_card_count": len(cards),
        "indexed_card_count": len(vectors),
        "candidate_count": candidate_count,
        "edge_count": len(edges),
        "source_max_revision_id": max(
            (card.revision_id for card in cards.values()), default=0
        ),
        "samples": _safe_samples(edges),
    }
    if dry_run:
        return report

    absolute_index = os.path.abspath(os.path.expanduser(index_db))
    if not os.path.isfile(absolute_index):
        raise RuntimeError("retrieval index does not exist")
    with sqlite3.connect(absolute_index, timeout=10) as connection:
        connection.execute("PRAGMA busy_timeout=10000")
        if not _table_exists(connection, "chunks"):
            raise RuntimeError("retrieval index has no chunks table")
        connection.executescript(GRAPH_SCHEMA)
        connection.execute("DELETE FROM memory_related_edges")
        connection.executemany(
            "INSERT INTO memory_related_edges("
            "card_a_id,revision_a_id,card_b_id,revision_b_id,relation,weight,"
            "cosine,shared_keywords_json,reason,policy_version,built_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    edge.card_a_id,
                    edge.revision_a_id,
                    edge.card_b_id,
                    edge.revision_b_id,
                    "related",
                    edge.weight,
                    edge.cosine,
                    json.dumps(edge.shared_keywords, ensure_ascii=False),
                    edge.reason,
                    POLICY_VERSION,
                    built_at,
                )
                for edge in edges
            ],
        )
        connection.execute(
            "INSERT OR REPLACE INTO memory_related_graph_build("
            "singleton,policy_version,source_max_revision_id,active_card_count,"
            "indexed_card_count,candidate_count,edge_count,built_at"
            ") VALUES(1,?,?,?,?,?,?,?)",
            (
                POLICY_VERSION,
                report["source_max_revision_id"],
                report["active_card_count"],
                report["indexed_card_count"],
                report["candidate_count"],
                report["edge_count"],
                built_at,
            ),
        )
    return report


def read_neighbors(
    memory_db: str,
    index_db: str,
    card_id: int,
    *,
    limit: int = 3,
    expected_revision_id: int | None = None,
) -> list[dict]:
    """Read current valid neighbors without creating or changing any schema."""
    if int(card_id) <= 0 or int(limit) <= 0:
        return []
    if not os.path.isfile(os.path.abspath(os.path.expanduser(index_db))):
        return []
    cards = _load_active_cards(memory_db)
    source = cards.get(int(card_id))
    if source is None or (
        expected_revision_id is not None
        and source.revision_id != int(expected_revision_id)
    ):
        return []

    with _read_only(index_db) as connection:
        if not _table_exists(connection, "memory_related_edges"):
            return []
        rows = connection.execute(
            "SELECT card_a_id,revision_a_id,card_b_id,revision_b_id,weight,"
            "shared_keywords_json,reason FROM memory_related_edges "
            "WHERE relation='related' AND policy_version=? "
            "AND (card_a_id=? OR card_b_id=?) "
            "ORDER BY weight DESC,card_a_id,card_b_id",
            (POLICY_VERSION, int(card_id), int(card_id)),
        ).fetchall()

    result = []
    for row in rows:
        if int(row["card_a_id"]) == int(card_id):
            source_revision = int(row["revision_a_id"])
            neighbor_id = int(row["card_b_id"])
            neighbor_revision = int(row["revision_b_id"])
        else:
            source_revision = int(row["revision_b_id"])
            neighbor_id = int(row["card_a_id"])
            neighbor_revision = int(row["revision_a_id"])
        neighbor = cards.get(neighbor_id)
        if source_revision != source.revision_id:
            continue
        if neighbor is None or neighbor_revision != neighbor.revision_id:
            continue
        try:
            shared_keywords = list(json.loads(row["shared_keywords_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        result.append(
            {
                "card_id": neighbor_id,
                "revision_id": neighbor_revision,
                "score": float(row["weight"]),
                "reason": str(row["reason"]),
                "shared_keywords": shared_keywords,
            }
        )
        if len(result) >= int(limit):
            break
    return result
