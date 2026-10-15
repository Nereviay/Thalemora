"""念念离线档案初始化/追赶；源 feed 必须已由宿主显式安装。"""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import time

from thalemora.adapters.niannian_history import (
    CanonicalHistoryReader, NiannianHistoryBinding, sync_once,
)
from thalemora.adapters.niannian_raw_feed import RawHistoryReader
from thalemora.history import HistorySync


def bootstrap(*, runtime_db, raw_db, archive_path, owner, agent_id):
    started=time.monotonic()
    binding=NiannianHistoryBinding(
        archive_path=archive_path,owner=owner,agent_id=agent_id,
        apps=["niannian","niannian-legacy"])
    sync=HistorySync(binding,owner,agent_id)
    pages={}
    for reader in (CanonicalHistoryReader(runtime_db,"niannian"),
                   RawHistoryReader(raw_db,"niannian-legacy")):
        count=0
        while sync_once(sync,reader,limit=1000)["status"]!="idle":
            count+=1
        pages[reader.app_id]=count
    with closing(sqlite3.connect(binding.path)) as db:
        counts={app:{"events":count,"deleted":deleted}
                for app,count,deleted in db.execute(
                    "SELECT app,COUNT(*),SUM(deleted) FROM history_events GROUP BY app")}
    return {"status":"PASS","new_pages":pages,"sources":counts,
            "cursors":{app:sync.get_cursor(app) for app in pages},
            "elapsed_seconds":time.monotonic()-started,"identity_unchanged":True,
            "models_called":0,
            "count_semantics":"canonical 与 RawEvent 是两个来源投影，不能相加当作唯一聊天数",
            "coverage":"both durable append/edit/delete/purge feeds; raw private channel excluded"}
