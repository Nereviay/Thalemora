"""念念 P6 离线存储预演；只读来源，所有核心操作限定在新副本。

不启动服务、不加载 Provider、不导入 Prompt/私有通道。逐库 SQLite backup
只保证各库自己的快照；不能把在线多库快照当作正式切权的一致性备份。
"""
from contextlib import closing
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path
import argparse
import json
import os
import sqlite3
import time

DATABASES = (
    "memory.db", "self_model.sqlite", "self_model_maintenance.sqlite",
    "memory_ops.sqlite", "vnext_scene.sqlite", "vnext_retrieval.sqlite",
    "vnext_runtime.sqlite", "raw_events.sqlite", "runtime.sqlite",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def reader(path):
    db = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    return db


def table_counts(db):
    return {
        name: db.execute(f"SELECT COUNT(*) FROM {quote(name)}").fetchone()[0]
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    }


def snapshot(source, destination):
    """目标必须全新；失败保留局部副本供诊断，绝不覆盖旧快照。"""
    source = Path(source).resolve(strict=True)
    destination = Path(destination)
    # 全部来源先查存在，避免把拼错路径初始化成空生产库。
    paths = [source.joinpath(name).resolve(strict=True) for name in DATABASES]
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {"kind": "per_database_online_snapshot", "cross_database_atomic": False,
              "started_at": now(), "databases": {}}
    for name, path in zip(DATABASES, paths):
        target = destination / name
        start = time.monotonic()
        with closing(reader(path)) as src, closing(sqlite3.connect(target)) as dst:
            def progress(status, remaining, total):
                if time.monotonic() - start > 60:
                    raise TimeoutError(f"SQLite backup 超时: {name}")
            src.backup(dst, pages=256, progress=progress, sleep=0.02)
        os.chmod(target, 0o600)
        with closing(reader(target)) as db:
            checks = db.execute("PRAGMA quick_check").fetchall()
            if checks != [("ok",)]:
                raise ValueError(f"副本 quick_check 失败: {name}")
            result["databases"][name] = {
                "copied_at": now(), "bytes": target.stat().st_size,
                "tables": table_counts(db),
            }
    result["completed_at"] = now()
    (destination / "snapshot.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def compare(original, candidate):
    """逐表、逐行比较；包括 BLOB、legacy 表及 SQLite/FTS 内部表，不输出正文。"""
    with closing(reader(original)) as left, closing(reader(candidate)) as right:
        schema = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        if left.execute(schema).fetchall() != right.execute(schema).fetchall():
            raise ValueError(f"schema 改变: {Path(original).name}")
        counts = table_counts(left)
        for name in counts:
            columns = left.execute(f"PRAGMA table_info({quote(name)})").fetchall()
            keys = [c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5]]
            order = ",".join(map(quote, keys)) if keys else "rowid"
            sql = f"SELECT * FROM {quote(name)} ORDER BY {order}"
            missing = object()
            for a, b in zip_longest(left.execute(sql), right.execute(sql), fillvalue=missing):
                if a != b:
                    raise ValueError(f"记录改变: {Path(original).name}/{name}")
        return {"tables": len(counts), "rows": sum(counts.values()), "unchanged": True}


def probe_core(directory):
    """直接复用既有库；不调用只适用于空库的 Memory/Water 初始化器。"""
    from thalemora.gatekeeper.core import freeze_candidate_map_from_card_ids
    from thalemora.self_model.contract import namespace_for_principal
    from thalemora.self_model.store import SQLiteSelfModelStore
    from thalemora.water.store import read_water_map

    directory = Path(directory)
    memory = directory / "memory.db"
    with closing(reader(memory)) as db:
        ids = [r[0] for r in db.execute("SELECT id FROM cards ORDER BY id")]
        expected = db.execute(
            "SELECT COUNT(*),COALESCE(SUM(c.source='manual' OR r.origin='manual_ui'),0) "
            "FROM cards c JOIN card_revisions r ON r.id=c.current_revision_id "
            "WHERE c.memory_kind='episodic'"
        ).fetchone()
        # 只记录现存关联缺口数量；旧资料不借此重写或反推。
        references = {
            "card_revision_missing": db.execute(
                "SELECT COUNT(*) FROM cards c LEFT JOIN card_revisions r "
                "ON r.id=c.current_revision_id AND r.card_id=c.id "
                "WHERE c.current_revision_id IS NOT NULL AND r.id IS NULL"
            ).fetchone()[0],
            "revision_evidence_orphan": db.execute(
                "SELECT COUNT(*) FROM card_evidence e LEFT JOIN card_revisions r "
                "ON r.id=e.revision_id WHERE r.id IS NULL"
            ).fetchone()[0],
        }
    frozen = freeze_candidate_map_from_card_ids(
        ids, memory_db_path=memory, include_read_only=True,
    )
    observed = (len(frozen), sum(bool(c["read_only"]) for c in frozen))
    if observed != expected:
        raise ValueError("Memory/人工只读投影数量不一致")
    water = read_water_map(memory)
    with closing(sqlite3.connect(directory / "self_model.sqlite", isolation_level=None)) as db:
        db.execute("PRAGMA query_only=ON")
        store = SQLiteSelfModelStore(db)
        # 不初始化另一身份、namespace 或数据库。
        bindings = db.execute(
            "SELECT namespace,principal_id FROM self_model_namespaces"
        ).fetchall()
        identity_ok = all(namespace == namespace_for_principal(principal)
                          for namespace, principal in bindings)
        claims = store.p3c_latest_claims()
        histories = sum(len(store.p3c_history(c.claim_id)) for c in claims)
        if not identity_ok:
            raise ValueError("既有 Self Model principal/namespace 不匹配")
        expected_claims = db.execute("SELECT COUNT(*) FROM claim_lineages").fetchone()[0]
        expected_versions = db.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        if (len(claims), histories) != (expected_claims, expected_versions):
            raise ValueError("Self Model 核心读取未覆盖全部声明/版本")
    return {
        "memory": {"projected": observed[0], "manual_read_only": observed[1],
                   "existing_reference_gaps": references},
        "water": water["counts"],
        "self_model": {"namespaces": len(bindings), "identity_preserved": identity_ok,
                       "claims": len(claims), "versions": histories},
        "models_called": 0,
    }


def rehearse(source, destination):
    """source 是快照，destination 是全新工作副本；失败不产生 PASS 报告。"""
    destination = Path(destination)
    snapshot(source, destination)
    result = {"started_at": now(), "scope": "existing_storage_core_compatibility",
              "migration": "preserve_existing_tables_and_identity",
              "production_ready": False, "cross_database_atomic": False}
    result["core"] = probe_core(destination)
    if probe_core(destination) != result["core"]:
        raise ValueError("重复核心读取结果不一致")
    result["preservation"] = {
        name: compare(Path(source) / name, destination / name) for name in DATABASES
    }
    result["status"] = "PASS"
    result["completed_at"] = now()
    (destination / "compatibility.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "rehearse"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = (snapshot if args.action == "snapshot" else rehearse)(args.source, args.destination)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
