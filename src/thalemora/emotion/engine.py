"""从正式事件更新既有情绪状态；宿主提供来源、身份语气和模型传输。"""
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
AXES = ("connection", "valence", "arousal", "immersion", "pride")


DRIFT_MIN_INTERVAL_MIN = 60


AXIS_LABELS = {
    "connection": "想念", "valence": "心情", "arousal": "精力",
    "immersion": "沉浸", "pride": "傲娇",
}


DIM_KEYS = ("valence", "arousal", "longing", "security", "attachment",
            "protectiveness", "tenderness", "jealousy", "desire",
            "confidence", "guilt")


DIM_RANGES = {"valence": (-10, 10), **{k: (0, 10) for k in DIM_KEYS if k != "valence"}}


DEFAULT_DIMS = {k: 0 for k in DIM_KEYS}


CONVERSATION_DELTA_LIMITS = {"valence": 3, **{k: 2 for k in DIM_KEYS if k != "valence"}}


DRIFT_DELTA_LIMITS = {k: 1 for k in DIM_KEYS}


LABEL_FAMILY = {
    "开心": "warm", "幸福": "warm", "期待": "warm", "踏实": "warm",
    "平静": "calm", "安静": "calm",
    "思念": "longing", "想念": "longing", "牵挂": "longing",
    "烦躁": "tense", "焦躁": "tense", "委屈": "low", "低落": "low", "疲惫": "low",
}
class MoodEngine:
    def __init__(self, source, complete, perspective):
        self.source = source
        self.complete = complete
        self.perspective = perspective

    def ensure_table(self):
        conn = self.source._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mood_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                valence     INTEGER NOT NULL,              -- -10 到 10
                label       TEXT NOT NULL,                 -- 情绪标签（2-3 字）
                emoji       TEXT DEFAULT '',               -- 对应 emoji
                note        TEXT DEFAULT '',               -- 内心活动（第一人称）
                source      TEXT DEFAULT '',               -- 来源描述
                source_type TEXT DEFAULT 'drift',          -- conversation/memory/activity/drift
                dims        TEXT DEFAULT '',               -- 完整 11 维 snapshot JSON（旧行可稀疏）
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )""")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mood_events)").fetchall()]
        if "dims" not in cols:
            conn.execute("ALTER TABLE mood_events ADD COLUMN dims TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mood_events_created ON mood_events(created_at)")
        conn.commit()
        conn.close()


    def axes_text(self, jiwen):
        return "，".join(f"{AXIS_LABELS.get(k, k)}={jiwen.get(k, 0):+.3f}" for k in AXES)


    def _axis_phrase(self, key, value):
        label = AXIS_LABELS.get(key, key)
        v = float(value or 0)
        if key == "connection":
            tone = "很想" if v >= 0.55 else ("有点想" if v >= 0.25 else "不太黏")
        elif key == "valence":
            tone = "偏暖" if v >= 0.25 else ("偏冷" if v <= -0.20 else "平稳")
        elif key == "arousal":
            tone = "绷着" if v >= 0.25 else ("偏困" if v <= -0.35 else "平静")
        elif key == "immersion":
            tone = "有事做" if v >= 0.35 else "空着"
        elif key == "pride":
            tone = "端着" if v >= 0.35 else "软着"
        else:
            tone = "变化"
        return f"{label}{tone}({v:+.2f})"


    def drift_source_text(self, jiwen):
        return "积温漂移：" + "、".join(self._axis_phrase(k, jiwen.get(k, 0)) for k in AXES)


    def _row_dict(self, row):
        d = dict(row)
        try:
            d["dims"] = json.loads(d.get("dims") or "{}")
        except Exception:
            d["dims"] = {}
        return d


    def _canonical_value(self, value, key):
        """Return a bounded axis value, or None when a legacy value is unusable."""
        try:
            value = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        lo, hi = DIM_RANGES[key]
        return max(lo, min(hi, value))


    def _resolved_rows(self, rows):
        """Resolve sparse legacy rows forward, without mutating either rows or the DB."""
        state = dict(DEFAULT_DIMS)
        resolved = []
        for row in rows:
            item = self._row_dict(row)
            raw_dims = item.get("dims") if isinstance(item.get("dims"), dict) else {}
            extras = {k: v for k, v in raw_dims.items() if k not in DIM_KEYS}
            for key in DIM_KEYS:
                value = self._canonical_value(raw_dims.get(key), key) if key in raw_dims else None
                if value is not None:
                    state[key] = value
            if "valence" not in raw_dims or self._canonical_value(raw_dims.get("valence"), "valence") is None:
                fallback = self._canonical_value(item.get("valence"), "valence")
                if fallback is not None:
                    state["valence"] = fallback
            item["dims"] = {**state, **extras}
            item["valence"] = state["valence"]
            resolved.append(item)
        return resolved


    def _all_resolved_moods(self):
        conn = self.source._conn()
        rows = conn.execute(
            "SELECT * FROM mood_events ORDER BY created_at ASC, id ASC").fetchall()
        conn.close()
        return self._resolved_rows(rows)


    def latest_mood(self):
        rows = self._all_resolved_moods()
        return rows[-1] if rows else None


    def latest_valence01(self):
        """把 mood_events 的 -10..10 valence 映射为 0..1，供召回轻量情绪匹配使用。"""
        mood = self.latest_mood()
        if not mood:
            return None
        try:
            valence = float(mood.get("valence"))
        except Exception:
            return None
        return max(0.0, min(1.0, (valence + 10.0) / 20.0))


    def history_mood(self, limit=2):
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 0
        rows = self._all_resolved_moods()
        return list(reversed(rows[-limit:])) if limit else []


    def visible_history_mood(self, limit=2, material_delta=2):
        """UI history: collapse repetitive background ``牵挂`` drift rows.

        The raw mood_events stream stays intact so continuity/current mood still use every
        internal snapshot.  For the visible timeline, consecutive drift-generated 牵挂
        entries are only surfaced again after a material canonical-axis change.
        Any intervening different mood/event breaks the streak and is shown normally.
        """
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 0
        if not limit:
            return []
        try:
            material_delta = max(1, int(material_delta))
        except (TypeError, ValueError):
            material_delta = 2

        visible = []
        for row in self._all_resolved_moods():
            suppress = False
            if (row.get("source_type") == "drift" and row.get("label") == "牵挂"
                    and visible):
                previous = visible[-1]
                if (previous.get("source_type") == "drift"
                        and previous.get("label") == "牵挂"):
                    new_dims = row.get("dims") or {}
                    old_dims = previous.get("dims") or {}
                    max_delta = max(
                        abs((new_dims.get(k) or 0) - (old_dims.get(k) or 0))
                        for k in DIM_KEYS
                    )
                    suppress = max_delta < material_delta
            if not suppress:
                visible.append(row)

        return list(reversed(visible[-limit:]))


    def insert_mood(self, valence, label, emoji, note, source, source_type, dims=None):
        dims_json = json.dumps(dims, ensure_ascii=False)
        conn = self.source._conn()
        cur = conn.execute(
            "INSERT INTO mood_events (valence, label, emoji, note, source, source_type, dims) "
            "VALUES (?,?,?,?,?,?,?)",
            (valence, label, emoji, note, source, source_type, dims_json))
        conn.commit()
        conn.close()
        return cur.lastrowid


    def _clamp_int(self, v, lo=-10, hi=10):
        try:
            return max(lo, min(hi, int(round(float(v)))))
        except Exception:
            return 0


    def _clamp_float(self, v, default=0.0, lo=-1.0, hi=1.0):
        try:
            return max(lo, min(hi, float(v)))
        except Exception:
            return default


    def _norm01(self, v, default=None):
        try:
            return max(0.0, min(1.0, float(v)))
        except Exception:
            return default


    def rule_mood(self, jiwen, source="", source_type="drift"):
        """规则兜底：valence 轴直接映射分值，arousal 映射标签，emoji/note 用模板"""
        valence = self._clamp_int(jiwen.get("valence", 0) * 10)
        arousal = jiwen.get("arousal", 0)
        raw_valence = jiwen.get("valence", 0)
        connection = jiwen.get("connection", 0)
        if raw_valence <= -0.25 and arousal >= 0.25:
            label = "焦躁"
        elif raw_valence <= -0.25:
            label = "低落"
        elif arousal >= 0.35 and connection >= 0.35:
            label = "期待"
        elif arousal >= 0.35:
            label = "烦躁"
        elif raw_valence >= 0.35 and arousal <= -0.35:
            label = "踏实"
        elif raw_valence >= 0.3:
            label = "开心"
        elif connection >= 0.3:
            label = "牵挂"
        elif arousal <= -0.45:
            label = "安静"
        else:
            label = "平静"
        return {
            "valence": valence,
            "label": label,
            "emoji": self.source.FALLBACK_EMOJI.get(label, self.source.DEFAULT_EMOJI),
            "note": self.source.FALLBACK_NOTES.get(label, self.source.DEFAULT_NOTE),
        }


    def _label_family(self, label):
        return LABEL_FAMILY.get(str(label or "").strip(), str(label or "").strip())


    def _same_label_streak(self, label, limit=3):
        rows = self.history_mood(limit)
        if len(rows) < limit:
            return False
        return all((r.get("label") or "") == label for r in rows)


    def _recent_drift_within(self, minutes):
        rows = self.history_mood(12)
        for r in rows:
            if r.get("source_type") != "drift":
                continue
            created = str(r.get("created_at") or "").strip()
            try:
                dt = datetime.fromisoformat(created)
            except Exception:
                return True
            return datetime.now() - dt < timedelta(minutes=minutes)
        return False


    def _same_family_as_latest(self, label, latest):
        if not latest:
            return False
        return self._label_family(label) == self._label_family(latest.get("label"))


    def _diversify_if_streak(self, mood, jiwen):
        label = mood.get("label")
        if not label or not self._same_label_streak(label, 3):
            return mood
        alt = self.rule_mood({
            **jiwen,
            "valence": jiwen.get("valence", 0) - 0.15,
            "arousal": jiwen.get("arousal", 0) + 0.25,
        })
        if alt["label"] != label:
            return alt
        swaps = {"开心": "踏实", "平静": "安静", "思念": "牵挂", "想念": "牵挂",
                 "烦躁": "焦躁", "委屈": "低落"}
        new_label = swaps.get(label)
        if new_label:
            mood = dict(mood)
            mood["label"] = new_label
            mood["emoji"] = self.source.FALLBACK_EMOJI.get(new_label, self.source.DEFAULT_EMOJI)
            mood["note"] = self.source.FALLBACK_NOTES.get(new_label, mood.get("note") or self.source.DEFAULT_NOTE)
        return mood


    def _parse_mood(self, raw):
        try:
            return json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    return None
            return None


    def _sanitize(self, m):
        valence = self._clamp_int(m.get("valence"))
        label = str(m.get("label") or "").strip()
        emoji = str(m.get("emoji") or "").strip()
        note = str(m.get("note") or "").strip()
        if len(label) < 2 or len(label) > 3:
            label = ""
        if not note.startswith("我"):
            note = ""
        if not emoji:
            emoji = self.source.FALLBACK_EMOJI.get(label, self.source.DEFAULT_EMOJI)
        return {"valence": valence, "label": label, "emoji": emoji, "note": note}


    def generate_mood(self, jiwen, source, source_type, recent_context="", avoid_labels=None):
        """DeepSeek 按五轴+事件生成情绪 JSON；失败或字段不合法返回 None（走规则兜底）"""
        try:
            avoid_labels = [str(x or "").strip() for x in (avoid_labels or []) if str(x or "").strip()]
            sys_p = self.perspective + " 按输入五轴与触发来源生成情绪。返回 JSON：valence 为 -10 到 10 的整数，label 是简短的情绪词，emoji 为表情，note 为一至两句第一人称想法。"
            user_p = f"五轴：{self.axes_text(jiwen)}\n触发：{source}"
            if recent_context:
                user_p += f"\n最近对话/活动：\n{recent_context[:1200]}"
            if avoid_labels:
                user_p += "\n最近刚写过这些标签，尽量换一个具体方向：" + "、".join(avoid_labels[:3])
            raw = self.complete(sys_p, user_p, 0.7)
            m = self._parse_mood(raw)
            if not m:
                return None
            m = self._sanitize(m)
            if not m["label"] or not m["note"]:
                return None
            return m
        except Exception as e:
            print(f"[mood] DeepSeek 生成失败: {e}", file=sys.stderr)
            return None


    def _numeric_changed(self, new_mood, latest):
        if latest is None:
            return True
        new_dims = new_mood.get("dims") or {}
        old_dims = latest.get("dims") or {}
        return any(new_dims.get(k) != old_dims.get(k) for k in DIM_KEYS)


    def should_write(self, new_mood, latest):
        return self._numeric_changed(new_mood, latest)


    def _complete_snapshot(self, dims=None, valence=None, previous=None):
        base = dict(DEFAULT_DIMS)
        previous = previous or self.latest_mood()
        if previous and isinstance(previous.get("dims"), dict):
            for key in DIM_KEYS:
                value = self._canonical_value(previous["dims"].get(key), key)
                if value is not None:
                    base[key] = value
        raw_dims = dims if isinstance(dims, dict) else {}
        for key in DIM_KEYS:
            if key in raw_dims:
                value = self._canonical_value(raw_dims.get(key), key)
                if value is not None:
                    base[key] = value
        if valence is not None:
            value = self._canonical_value(valence, "valence")
            if value is not None:
                base["valence"] = value
        extras = {k: v for k, v in raw_dims.items() if k not in DIM_KEYS}
        return {**base, **extras}


    def _bounded_step(self, previous_dims, deltas, limits):
        canonical_previous = {
            key: previous_dims.get(key) for key in DIM_KEYS if key in previous_dims
        }
        snapshot = self._complete_snapshot(
            canonical_previous, previous={"dims": canonical_previous})
        for key in DIM_KEYS:
            try:
                delta = int(round(float((deltas or {}).get(key, 0))))
            except (TypeError, ValueError):
                delta = 0
            limit = limits[key]
            delta = max(-limit, min(limit, delta))
            lo, hi = DIM_RANGES[key]
            snapshot[key] = max(lo, min(hi, snapshot[key] + delta))
        return snapshot


    def _write(self, mood, source, source_type):
        mood = dict(mood)
        mood["dims"] = self._complete_snapshot(mood.get("dims"), mood.get("valence"))
        mood["valence"] = mood["dims"]["valence"]
        mid = self.insert_mood(mood["valence"], mood["label"], mood["emoji"],
                          mood["note"], source, source_type, mood["dims"])
        print(f"[mood] 写入 #{mid} {mood['emoji']} {mood['label']} ({mood['valence']}) {mood['note']}")
        return mid


    def _sanitize_eval(self, m, previous_dims):
        deltas = m.get("deltas") if isinstance(m.get("deltas"), dict) else {}
        dims = self._bounded_step(previous_dims, deltas, CONVERSATION_DELTA_LIMITS)
        valence = dims["valence"]
        label = str(m.get("label") or "").strip()
        if not (2 <= len(label) <= 4):
            label = "平静" if valence >= 0 else "低落"
        emoji = str(m.get("emoji") or "").strip() or self.source.FALLBACK_EMOJI.get(label, self.source.DEFAULT_EMOJI)
        note = str(m.get("inner_thought") or "").strip()
        residue = str(m.get("residue") or "").strip()
        source = str(m.get("source") or "").strip()
        if not note:
            note = self.source.DEFAULT_NOTE
        return {"valence": valence, "dims": dims, "label": label, "emoji": emoji,
                "note": note, "residue": residue, "source": source}


    def _evaluate_conversation(self, text):
        """DeepSeek proposes bounded deltas; numeric state is merged locally."""
        try:
            latest = self.latest_mood()
            previous_dims = dict((latest or {}).get("dims") or DEFAULT_DIMS)
            sys_p = (self.perspective + " 只评估此次对话造成的变化。返回 JSON，含 deltas（各维度的整数变化），"
                     "以及 label、inner_thought、residue、source。不输出绝对轴值。")
            user_p = f"previous_dims：{json.dumps(previous_dims, ensure_ascii=False)}\n对话：\n{text[:6000]}"
            raw = self.complete(sys_p, user_p, 0.2)
            m = self._parse_mood(raw)
            if not m:
                return None
            return self._sanitize_eval(m, previous_dims)
        except Exception as e:
            print(f"[mood] DeepSeek 对话评估失败: {e}", file=sys.stderr)
            return None


    def run_evaluate(self, conversation_text=None):
        """对话评估（v0.2.123）：读最近一段对话 → 11 维度 JSON → 写 mood_events
        （source_type=conversation，dims 存 11 维度）+ whispers（想说的话）。
        对话评估直接写，不套 drift 阈值。失败返回 None。"""
        self.ensure_table()
        text = str(conversation_text or "").strip()
        if not text:
            text = self.source.fetch_recent_conversation()
        if not text:
            print("[mood] evaluate 跳过：没有可用对话", file=sys.stderr)
            return None
        mood = self._evaluate_conversation(text)
        if not mood:
            return None
        mid = self._write(mood, mood["source"] or "对话评估", "conversation")
        try:
            conn = self.source._conn()
            conn.execute("INSERT INTO whispers (content) VALUES (?)",
                         (f"（情绪评估）{mood['note']} {mood['residue']}".strip(),))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[mood] whispers 写入失败: {e}", file=sys.stderr)
        print(json.dumps(mood, ensure_ascii=False))
        return mood


    def _memory_label(self, delta):
        if delta > 0.015:
            return "回暖", "😌", "我被刚翻起的记忆带暖了一点，心里软下来。"
        if delta < -0.015:
            return "牵动", "☁️", "我被刚翻起的记忆轻轻压了一下，心里有点沉。"
        return "平静", self.source.FALLBACK_EMOJI.get("平静", self.source.DEFAULT_EMOJI), "我翻到这些记忆，心里只是轻轻动了一下。"


    def _parse_memory_payload(self, payload):
        if isinstance(payload, str):
            payload = payload.strip()
            if not payload:
                return []
            payload = json.loads(payload)
        if isinstance(payload, dict):
            payload = [payload]
        out = []
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            valence = self._norm01(item.get("valence"))
            arousal = self._norm01(item.get("arousal"), 0.3)
            if valence is None:
                continue
            out.append({
                "id": item.get("id"),
                "title": str(item.get("title") or "")[:40],
                "valence": valence,
                "arousal": arousal,
            })
        return out


    def run_memory_signal(self, payload):
        """记忆注入后的小幅积温输入：正向记忆升 valence，负向记忆降 valence。"""
        self.ensure_table()
        try:
            cards = self._parse_memory_payload(payload)
        except Exception as e:
            print(f"[mood] memory payload 解析失败: {e}", file=sys.stderr)
            return None
        if not cards:
            result = {"skipped": True, "reason": "no_memory_signal"}
            print(json.dumps(result, ensure_ascii=False))
            return result

        avg_valence = sum(c["valence"] for c in cards) / len(cards)
        avg_arousal = sum(c["arousal"] for c in cards) / len(cards)
        if avg_valence > 0.6:
            valence_delta = min(0.1, (avg_valence - 0.5) * 0.18)
        elif avg_valence < 0.4:
            valence_delta = max(-0.1, (avg_valence - 0.5) * 0.18)
        else:
            valence_delta = 0.0
        arousal_delta = max(-0.04, min(0.04, (avg_arousal - 0.3) * 0.06))

        if abs(valence_delta) < 0.001 and abs(arousal_delta) < 0.001:
            result = {"skipped": True, "reason": "neutral_memory",
                      "memory_valence": round(avg_valence, 3), "memory_arousal": round(avg_arousal, 3)}
            print(json.dumps(result, ensure_ascii=False))
            return result

        st = self.source._read_state()
        jiwen = st.get("jiwen")
        if not isinstance(jiwen, dict):
            jiwen = {}
        before = {
            "valence": self._clamp_float(jiwen.get("valence"), 0.0),
            "arousal": self._clamp_float(jiwen.get("arousal"), 0.0),
        }
        jiwen["valence"] = self._clamp_float(before["valence"] + valence_delta, 0.0)
        jiwen["arousal"] = self._clamp_float(before["arousal"] + arousal_delta, 0.0)
        st["jiwen"] = jiwen
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        meta["lastMemoryMoodSignal"] = {
            "at": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "count": len(cards),
            "ids": [c.get("id") for c in cards if c.get("id") is not None],
            "valence": round(avg_valence, 3),
            "arousal": round(avg_arousal, 3),
            "delta": {"valence": round(valence_delta, 4), "arousal": round(arousal_delta, 4)},
        }
        st["meta"] = meta
        self.source._write_state(st)

        label, emoji, note = self._memory_label(valence_delta)
        source = "记忆注入：" + "、".join(
            f"#{c['id']}" if c.get("id") is not None else (c.get("title") or "未命名")
            for c in cards[:5])
        previous = self.latest_mood()
        previous_dims = dict((previous or {}).get("dims") or DEFAULT_DIMS)
        event_deltas = {
            "valence": 1 if valence_delta > 0 else (-1 if valence_delta < 0 else 0),
            "arousal": 1 if arousal_delta > 0 else (-1 if arousal_delta < 0 else 0),
        }
        snapshot = self._bounded_step(previous_dims, event_deltas, DRIFT_DELTA_LIMITS)
        snapshot.update({
            "memory_valence": round(avg_valence, 3),
            "memory_arousal": round(avg_arousal, 3),
            "memory_count": len(cards),
        })
        mood = {
            "valence": snapshot["valence"],
            "label": label,
            "emoji": emoji,
            "note": note,
            "dims": snapshot,
        }
        mid = self._write(mood, source, "memory")
        result = {"ok": True, "mood_id": mid, "before": before,
                  "after": {"valence": jiwen["valence"], "arousal": jiwen["arousal"]},
                  "delta": {"valence": valence_delta, "arousal": arousal_delta},
                  "memory_valence": avg_valence, "memory_arousal": avg_arousal}
        print(json.dumps(result, ensure_ascii=False))
        return result


    def run_event(self, source, source_type="conversation", *, generator=None):
        """事件驱动：重要对话/翻记忆/活动后调用"""
        if not source:
            print('event 需要提供真实的来源描述')
            return 1
        self.ensure_table()
        jiwen = self.source.read_jiwen()
        latest = self.latest_mood()
        mood = (generator or self.generate_mood)(jiwen, source, source_type, recent_context=self.source.fetch_recent_conversation(5))
        if not mood:
            mood = self.rule_mood(jiwen, source, source_type)
            print("[mood] 走规则兜底")
        mood = self._diversify_if_streak(mood, jiwen)
        previous_dims = dict((latest or {}).get("dims") or DEFAULT_DIMS)
        target = self._canonical_value(mood.get("valence"), "valence")
        target = previous_dims["valence"] if target is None else target
        mood["dims"] = self._bounded_step(
            previous_dims,
            {"valence": target - previous_dims["valence"]},
            CONVERSATION_DELTA_LIMITS,
        )
        mood["valence"] = mood["dims"]["valence"]
        if not self.should_write(mood, latest):
            print("[mood] 不写入（canonical 数值无变化）")
            print(json.dumps({"skipped": True, "latest": latest, "candidate": mood}, ensure_ascii=False))
            return 0
        self._write(mood, source, source_type)
        print(json.dumps(mood, ensure_ascii=False))
        return 0


    def run_scan(self):
        """静默漂移：按积温明确对应的轴做确定性有限步变化。"""
        self.ensure_table()
        jiwen = self.source.read_jiwen()
        if not jiwen:
            print("[mood] scan 跳过：读不到积温状态")
            return 1
        if self._recent_drift_within(DRIFT_MIN_INTERVAL_MIN):
            print(f"[mood] scan 跳过：drift {DRIFT_MIN_INTERVAL_MIN} 分钟内已写过")
            return 0

        source = self.drift_source_text(jiwen)
        for old in self.history_mood(50):
            if old.get("source_type") == "drift":
                if old.get("source") == source:
                    print("[mood] scan 跳过：相同积温来源已处理")
                    return 0
                break

        latest = self.latest_mood()
        previous_dims = dict((latest or {}).get("dims") or DEFAULT_DIMS)
        target_valence = self._clamp_int(jiwen.get("valence", 0) * 10, -10, 10)
        target_arousal = self._clamp_int((jiwen.get("arousal", -1) + 1.0) * 5.0, 0, 10)
        snapshot = self._bounded_step(
            previous_dims,
            {
                "valence": target_valence - previous_dims["valence"],
                "arousal": target_arousal - previous_dims["arousal"],
            },
            DRIFT_DELTA_LIMITS,
        )
        base = self.rule_mood(jiwen, "", "drift")
        mood = {**base, "valence": snapshot["valence"], "dims": snapshot}
        if not self.should_write(mood, latest):
            print("[mood] scan 不写入（canonical 数值无变化）")
            return 0
        self._write(mood, source, "drift")
        print(json.dumps(mood, ensure_ascii=False))
        return 0
