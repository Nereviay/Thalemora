"""念念历史后台单次增量；不调用模型，不安装 feed，不启动常驻服务。"""
import argparse
import json

from thalemora.adapters.niannian_history import (
    CanonicalHistoryReader, NiannianHistoryBinding, sync_once,
)
from thalemora.adapters.niannian_raw_feed import RawHistoryReader
from thalemora.history import HistorySync
from thalemora.identity import OwnerPrincipal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-db", required=True)
    parser.add_argument("--raw-db", required=True)
    parser.add_argument("--archive-db", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    owner = OwnerPrincipal(args.owner)
    binding = NiannianHistoryBinding(
        archive_path=args.archive_db, owner=owner, agent_id=args.agent_id,
        apps=["niannian", "niannian-legacy"],
    )
    sync = HistorySync(binding, owner, args.agent_id)
    results = {}
    for reader in (CanonicalHistoryReader(args.runtime_db, "niannian"),
                   RawHistoryReader(args.raw_db, "niannian-legacy")):
        result = sync_once(sync, reader, limit=args.limit)
        results[reader.app_id] = {"status":result["status"],"cursor":result["cursor"],
                                 "events":len(result["events"])}
    # 回执不包含原文；来源故障直接报错，已成功来源的游标保留。
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
