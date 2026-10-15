"""独立、串行、有界的后台 worker；进度沿用 maintenance 的持久数据。"""
from collections import Counter
import json
import math
from threading import Event

from .maintenance_provider import MaintenanceProvider


def run_worker(application, agent_id, app_id, channel="main", *, interval=60, limit=1,
               once=False, stop=None):
    if not math.isfinite(interval) or interval < 1:
        raise ValueError("worker interval 须为至少 1 秒的有限值")
    application.page(limit, 0)
    stop = stop if stop is not None else Event()
    provider = MaintenanceProvider(application.providers)
    while not stop.is_set():
        result = application.maintenance_tick(agent_id, app_id, channel, limit=limit, provider=provider)
        print(json.dumps({"agent_id": agent_id, "app": app_id, "channel": channel,
                          "jobs": dict(Counter(row["status"] for row in result["jobs"])),
                          "reflections": dict(Counter(row["status"] for row in result["reflections"])),
                          "errors": result["errors"]}, ensure_ascii=False), flush=True)
        # 出错后保留进度并退出，交由使用者修配置/预算；不反复付费重试。
        if result["errors"]:
            return 1
        if once:
            return 0
        stop.wait(interval)
    return 0
