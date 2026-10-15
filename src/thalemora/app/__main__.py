"""独立入口；模型默认不调用。"""
import argparse
import json
import os
from pathlib import Path

from .service import Application


def main():
    parser = argparse.ArgumentParser(prog="thalemora")
    parser.add_argument("--data", default=os.environ.get("THALEMORA_DATA", "./state"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--container", action="store_true", help="容器内部绑定所有接口；Compose 宿主仍限 loopback")
    commands.add_parser("mcp")
    for name in ("worker", "maintenance-status"):
        command = commands.add_parser(name)
        command.add_argument("agent_id")
        command.add_argument("app_id")
        command.add_argument("--channel", choices=("main", "private"), default="main")
        command.add_argument("--limit", type=int, default=1 if name == "worker" else 20)
        if name == "worker":
            command.add_argument("--interval", type=float, default=60)
            command.add_argument("--once", action="store_true", help="只运行一轮有界维护")
    for name in ("export-history", "import-history"):
        command = commands.add_parser(name)
        command.add_argument("agent_id")
        command.add_argument("app_id")
        command.add_argument("file", type=Path)
        command.add_argument("--channel", choices=("main", "private"), default="main")
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("source")
        command.add_argument("destination")
    args = parser.parse_args()
    if args.command in ("backup", "restore"):
        from . import backup
        operation = backup.snapshot if args.command == "backup" else backup.restore
        print(operation(args.source, args.destination))
        return
    service = Application(args.data)
    if args.command == "maintenance-status":
        print(json.dumps(service.maintenance_status(args.agent_id, args.app_id, args.channel,
                                                   limit=args.limit), ensure_ascii=False))
        return
    if args.command == "worker":
        from threading import Event
        import signal
        from .worker import run_worker
        stop = Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        raise SystemExit(run_worker(service, args.agent_id, args.app_id, args.channel,
                                    limit=args.limit, interval=args.interval, once=args.once, stop=stop))
    if args.command == "export-history":
        offset = 0
        with args.file.open("w", encoding="utf-8") as target:
            while True:
                rows = service.history(args.agent_id, args.app_id, args.channel,
                                       limit=100, offset=offset, include_deleted=True)
                for row in rows:
                    target.write(json.dumps(row, ensure_ascii=False) + "\n")
                if len(rows) < 100:
                    break
                offset += 100
        print(f"已导出 {offset + len(rows)} 条当前来源 envelope（含删除标记）")
        return
    if args.command == "import-history":
        count = 0
        with args.file.open(encoding="utf-8") as source:
            for line in source:
                service.import_history(args.agent_id, args.app_id, [json.loads(line)], args.channel)
                count += 1
        print(f"已导入 {count} 条来源 envelope")
        return
    if args.command == "mcp":
        from .mcp import create_mcp
        create_mcp(service).run(transport="stdio")
        return
    from .http import create_http
    import uvicorn
    app = create_http(service, os.environ.get("THALEMORA_TOKEN"))
    uvicorn.run(app, host="0.0.0.0" if args.container else "127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
