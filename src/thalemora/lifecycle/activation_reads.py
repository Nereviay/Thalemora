import datetime as dt
import math
from contextlib import closing
from .activation import timestamp, parse_time, connect_existing

class ActivationReads:
    def latest_bias_by_memory(
        self,
        *,
        now: dt.datetime | None = None,
        half_life_days: float = 14.0,
        lookback_days: float = 90.0,
        max_bias: float = 0.20,
    ) -> dict[int, float]:
        now = parse_time((now or dt.datetime.now(dt.timezone.utc)).isoformat())
        half_life_days = max(1.0, float(half_life_days))
        lookback_days = max(1.0, float(lookback_days))
        cutoff = timestamp(now - dt.timedelta(days=lookback_days))
        decay = math.log(2.0) / half_life_days
        with closing(connect_existing(self.db_path, readonly=True)) as conn:
            rows = conn.execute(
                "SELECT card_id,weight,created_at FROM memory_activation_events "
                "WHERE created_at>=?",
                (cutoff,),
            ).fetchall()

        totals: dict[int, float] = {}
        for row in rows:
            when = parse_time(row["created_at"])
            if when is None:
                continue
            age_days = max(
                0.0, (now - when).total_seconds() / 86400.0
            )
            contribution = float(row["weight"] or 0.0) * math.exp(
                -decay * age_days
            )
            cid = int(row["card_id"])
            totals[cid] = totals.get(cid, 0.0) + contribution

        ceiling = max(0.0, float(max_bias))
        # Activation is advisory only. Scale normalized activity into a small
        # ambient score bias; lifecycle/status gates remain authoritative.
        return {
            cid: min(ceiling, max(0.0, total * 0.08))
            for cid, total in totals.items()
            if total > 0.0
        }


    def recent_events(self, card_id: int | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(200, int(limit)))
        with closing(connect_existing(self.db_path, readonly=True)) as conn:
            if card_id is None:
                rows = conn.execute(
                    "SELECT * FROM memory_activation_events "
                    "ORDER BY created_at DESC,event_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_activation_events WHERE card_id=? "
                    "ORDER BY created_at DESC,event_id DESC LIMIT ?",
                    (int(card_id), limit),
                ).fetchall()
        return [dict(row) for row in rows]
