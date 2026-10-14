"""停写后的本地 SQLite 封存/恢复；不访问生产，不接受 HTTP 文件路径。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not (source / "registry.sqlite").is_file():
        raise ValueError("来源不是独立应用数据目录")
    if source == destination or source in destination.parents:
        raise ValueError("备份目标须在数据目录之外")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("目标必须为新空目录")
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(source.rglob("*.sqlite")):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("备份数据库 quick_check 失败")
        files.append(str(relative))
    (destination / "snapshot.json").write_text(json.dumps({"format": 1, "files": files}, ensure_ascii=False), encoding="utf-8")
    return {"databases": len(files), "status": "snapshot_created", "requires_quiesced_source": True}


def restore(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    manifest = json.loads((source / "snapshot.json").read_text(encoding="utf-8"))
    if manifest["format"] != 1:
        raise ValueError("未知快照格式")
    result = snapshot(source, destination)
    with closing(sqlite3.connect(destination / "registry.sqlite")) as db, db:
        for agent_id, in db.execute("SELECT agent_id FROM agents").fetchall():
            db.execute("UPDATE agents SET archive_path=? WHERE agent_id=?",
                       (str(destination / "agents" / agent_id / "archive.sqlite"), agent_id))
    return {**result, "status": "restored", "new_management_token_required": True}
