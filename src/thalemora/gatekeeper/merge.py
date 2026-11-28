"""按已有记忆和当前修订整理重复卡，复用正式合并事务与回执。"""
import datetime
import hashlib
import json
import re
import sqlite3
from thalemora.memory import revision_store
from thalemora.memory.operations import consolidate_card, StaleConsolidation
from thalemora.gatekeeper import ops

MERGE_JUDGE_TEMPERATURE = 0.1
MERGE_JUDGE_POLICY_VERSION = "p1a-v2-same-event"


DATE_TITLE_RE = re.compile(r"^\d{4}[-年/.]\d{1,2}[-月/.]\d{1,2}(?:日|号)?")


MERGE_JUDGE_SYSTEM_PROMPT = (
    "你是记忆库的保守存量整理员。判断两张卡是否属于同一个具体记忆身份。\n"
    "规则：\n"
    "1. 只有两张卡描述同一个可识别的事实、事件、约定或偏好，且一张是另一张的补充、进展、纠正或重复表述时，才允许合并。\n"
    "2. 仅主题相同、人物相同、情绪相似、tags 相似，或都属于同一段关系经历，必须判为不合并。\n"
    "3. 日期不同、场景不同、关键动作不同，或两张卡各自已经语义闭合，必须判为不合并。\n"
    "4. 有疑问时一律不合并。宁可保留两张独立记忆，不可错合。\n"
    "5. 真正同一记忆才选信息更完整的一张为主卡；合并正文只保留这一记忆身份的有效信息，不做跨主题人物小传。\n"
    "6. 年轮只说明这次对同一记忆补了什么；importance 0-10。\n"
    "输出严格 JSON，无其他文字：\n"
    "合并：{\"merge\":true,\"main_id\":主卡id,\"content\":\"合并后完整正文\",\"ring\":\"年轮一句话\",\"importance\":5.0,\"reason\":\"一句话理由\"}\n"
    "不合并：{\"merge\":false,\"main_id\":null,\"content\":\"\",\"ring\":\"\",\"importance\":0.0,\"reason\":\"不合并的理由\"}"
)


def is_daily_title(title):
    """日卡判定：标题匹配日期格式的跳过，不参与合并"""
    return bool(DATE_TITLE_RE.match((title or "").strip()))


def _judge_card_input(card):
    """Fields rendered into the provider prompt, normalized exactly once."""
    return {
        "id": card["id"],
        "title": card.get("title") or "（无）",
        "content": card["content"],
    }
class MergeSweep:
    def __init__(self, *, memory_db, ops_db, stats, recall, embed, embedding_model, judge, model, logger):
        self.memory_db = memory_db
        self.ops_db = ops_db
        self.stats = stats
        self.recall = recall
        self.embed = embed
        self.embedding_model = embedding_model
        self.judge = judge
        self.model = model
        self.logger = logger

    def load_cards(self):
        """所有 fresh 卡，排除 timeline；日期标题只兜底识别 legacy 日卡。"""
        conn = sqlite3.connect(self.memory_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT c.*, r.origin AS current_revision_origin
            FROM cards c
            LEFT JOIN card_revisions r ON r.id=c.current_revision_id
            WHERE c.status='fresh' AND c.memory_kind='episodic'
            """
        ).fetchall()
        conn.close()
        cards = [dict(r) for r in rows]
        total = self.stats()["total"]
        excluded = [
            c for c in cards
            if c.get("memory_kind") == "timeline" or is_daily_title(c.get("title"))
        ]
        scan = [
            c for c in cards
            if c.get("memory_kind") != "timeline"
            and c.get("source") != "manual"
            and c.get("current_revision_origin") != "manual_ui"
            and not is_daily_title(c.get("title"))
        ]
        return total, excluded, scan

    def find_pairs(self, scan_cards, threshold):
        """每张卡 recall(content, top_n=8) 捞候选，去重 A↔B，只留分数>=threshold 的对"""
        eligible_cards = [
            card for card in scan_cards
            if card.get("memory_kind") != "timeline"
            and card.get("source") != "manual"
            and card.get("current_revision_origin") != "manual_ui"
            and not is_daily_title(card.get("title"))
        ]
        by_id = {c["id"]: c for c in eligible_cards}
        pairs = {}  # (min_id, max_id) -> (score, card_a, card_b)
        failed = 0
        for card in eligible_cards:
            try:
                hits = self.recall(card["content"], top_n=8, use_rerank=False, caller="merge_sweep")
            except Exception as e:
                self.logger.warning(f"卡片#{card['id']} 召回失败，跳过: {e}")
                failed += 1
                continue
            # v0.2.151：防御性解包。recall 历史版本/异常路径可能返回多余字段
            # （如 (id, content, score, created_at)），只取前三个，不再整条抛异常。
            for hit in hits:
                if not (isinstance(hit, (list, tuple)) and len(hit) >= 3):
                    continue
                cid, content, score = hit[0], hit[1], hit[2]
                if cid == card["id"] or cid not in by_id:
                    continue  # 自己、日卡、已沉底的都不算候选
                if score < threshold:
                    continue
                key = tuple(sorted((card["id"], cid)))
                other = by_id[cid]
                if key not in pairs or score > pairs[key][0]:
                    pairs[key] = (score, card, other)
        if failed:
            self.logger.warning(f"召回失败跳过 {failed} 张卡")
        return [p for p in sorted(pairs.values(), key=lambda x: -x[0])]

    def merge_input_hash(self, a, b, model=None, policy_version=None):
        """Hash the ordered, exact A/B judge input and provider policy identity."""
        payload = {
            "cards": [_judge_card_input(a), _judge_card_input(b)],
            "model": model or self.model,
            "policy_version": policy_version or MERGE_JUDGE_POLICY_VERSION,
        }
        stable_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(stable_json.encode("utf-8")).hexdigest()

    def _receipt_decision(self, a, b):
        """Read a valid receipt; any ops-store failure is a cache miss."""
        try:
            return ops.get_merge_decision_receipt(self.merge_input_hash(a, b), db_path=self.ops_db)
        except Exception as e:
            self.logger.warning(f"merge receipt 读取失败，按 miss 处理: {e}")
            return None

    def _put_receipt(self, a, b, decision):
        """Best-effort receipt persistence; never block this sweep's decision."""
        try:
            ops.put_merge_decision_receipt(
                self.merge_input_hash(a, b),
                a["id"],
                b["id"],
                self.model,
                MERGE_JUDGE_POLICY_VERSION,
                decision, db_path=self.ops_db,
            )
        except Exception as e:
            self.logger.warning(f"merge receipt 写入失败，本轮继续: {e}")

    def _cards_still_fresh(self, a, b, scatter_id):
        """Re-read judge fields and return latest revision-bound projections."""
        conn = sqlite3.connect(self.memory_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT c.*, r.origin AS current_revision_origin
                  FROM cards c
                  LEFT JOIN card_revisions r ON r.id=c.current_revision_id
                 WHERE c.id IN (?, ?)
                """,
                (a["id"], b["id"]),
            ).fetchall()
        finally:
            conn.close()
        current = {row["id"]: row for row in rows}
        for judged in (a, b):
            row = current.get(judged["id"])
            if row is None or row["status"] != "fresh":
                return None
            if row["source"] == "manual" or row["current_revision_origin"] == "manual_ui":
                return None
            if (row["title"] or "") != (judged.get("title") or ""):
                return None
            if row["content"] != judged["content"]:
                return None
            if row["current_revision_id"] is None:
                return None
        if bool(current[scatter_id]["pinned"]):
            return None
        return {card_id: dict(row) for card_id, row in current.items()}

    def process_pairs(self, pairs, dry_run):
        """按分数降序逐个送 DeepSeek；批准则执行合并（dry_run 只记录不落库）。
        返回 (送 DeepSeek 对数, 合并对数)"""
        state = {
            "sunk": set(),    # 已被并入而沉底的卡
            "mains": set(),   # 本趟当过主卡的卡，不再被并入别处，避免链条混乱
            "content": {},    # 主卡最新正文（一趟并入多张时用最新正文去问）
        }
        sent, merged = 0, 0
        for score, a, b in pairs:
            if a["id"] in state["sunk"] or b["id"] in state["sunk"]:
                continue
            # 用最新正文（主卡可能已被并入过散卡）去问 DeepSeek
            ca = dict(a)
            cb = dict(b)
            ca["content"] = state["content"].get(a["id"], a["content"])
            cb["content"] = state["content"].get(b["id"], b["content"])
            # load_cards() always supplies title. Minimal unit fixtures remain
            # receipt-free so dry-run tests do not create operational cache rows.
            receipt_enabled = "title" in a and "title" in b
            d = self._receipt_decision(ca, cb) if receipt_enabled else None
            if d is None:
                sent += 1
                try:
                    d = self.judge(ca, cb)
                except Exception as e:
                    self.logger.warning(f"候选对 #{a['id']}↔#{b['id']} DeepSeek 判断失败，按不合并处理: {e}")
                    continue
                if isinstance(d, dict) and receipt_enabled:
                    self._put_receipt(ca, cb, d)
            if not isinstance(d, dict) or not d.get("merge"):
                reason = (d or {}).get("reason", "") or "DeepSeek 判定不合并"
                self.logger.info(f"[不合并] #{a['id']} ↔ #{b['id']}（相似度{score:.3f}）: {reason}")
                continue
            try:
                main_id = int(d.get("main_id") or 0)
            except (TypeError, ValueError):
                main_id = 0
            if main_id == a["id"]:
                main_card, scatter_card = a, b
            elif main_id == b["id"]:
                main_card, scatter_card = b, a
            else:
                self.logger.warning(f"候选对 #{a['id']}↔#{b['id']} main_id 非法（{main_id}），按不合并处理")
                continue
            scatter_id = scatter_card["id"]
            if scatter_card.get("pinned"):
                self.logger.warning(f"候选对 #{a['id']}↔#{b['id']}：散卡#{scatter_id} 是置顶卡，跳过不沉底")
                continue
            if scatter_id in state["mains"]:
                self.logger.warning(f"候选对 #{a['id']}↔#{b['id']}：散卡#{scatter_id} 本趟已是主卡，跳过避免链条混乱")
                continue
            content = (d.get("content") or "").strip()
            if not content:
                self.logger.warning(f"候选对 #{a['id']}↔#{b['id']} 合并正文为空，按不合并处理")
                continue
            importance = float(d.get("importance") or 0)
            final_importance = max(importance, main_card["importance"], scatter_card["importance"])
            ring_text = f"原卡#{scatter_id}: {scatter_card['content']}"
            reason = d.get("reason", "") or "同主题散卡合并"
            ring_note = d.get("ring", "")
            if dry_run:
                self.logger.info(f"[计划合并] 主卡#{main_card['id']} ← 散卡#{scatter_id}（相似度{score:.3f}）")
                self.logger.info(f"   新正文: {content}")
                self.logger.info(f"   年轮: {ring_text}")
                self.logger.info(f"   importance: {final_importance} | 理由: {reason}（{ring_note}）")
            else:
                try:
                    current = self._cards_still_fresh(ca, cb, scatter_id)
                except Exception as e:
                    current = None
                    self.logger.warning(
                        f"候选对 #{a['id']}↔#{b['id']} stale guard 读取失败: {e}"
                    )
                if not current:
                    self.logger.warning(
                        f"[stale_skip] #{a['id']} ↔ #{b['id']}：judge 输入已变化或卡片不可合并"
                    )
                    continue
                current_main = current[main_card["id"]]
                current_scatter = current[scatter_id]
                final_importance = max(
                    importance,
                    float(current_main["importance"]),
                    float(current_scatter["importance"]),
                )
                try:
                    embedding = self.embed(
                        content,
                        caller="merge_sweep",
                        purpose="merge_sweep_rebuild_embedding",
                    )
                except Exception as e:
                    embedding = None
                    self.logger.warning(f"主卡#{main_card['id']} 合并向量失败，提交时清空 freshness: {e}")
                try:
                    consolidate_card(
                        self.memory_db, embedding_model=self.embedding_model,
                        card_id=main_card["id"],
                        expected_current_revision_id=current_main["current_revision_id"],
                        scatter_card_id=scatter_id,
                        expected_scatter_current_revision_id=current_scatter["current_revision_id"],
                        merged_from_revision_id=current_scatter["current_revision_id"],
                        content=content,
                        importance=final_importance,
                        embedding=embedding,
                        origin="merge_sweep",
                        evidence_source_kind="derived",
                        evidence_source_ref=(
                            f"card:{scatter_id}@revision:"
                            f"{current_scatter['current_revision_id']}"
                        ),
                        ring_by="merge",
                        ring_content=ring_text,
                    )
                except (
                    revision_store.CardNotFound,
                    revision_store.InvalidRevisionState,
                    revision_store.RevisionConflict,
                    StaleConsolidation,
                ) as e:
                    self.logger.warning(
                        f"[stale_skip] #{a['id']} ↔ #{b['id']}：transaction guard 拒绝合并: {e}"
                    )
                    continue
                self.logger.info(f"[已合并] 主卡#{main_card['id']} ← 散卡#{scatter_id}（相似度{score:.3f}）理由: {reason}")
            merged += 1
            state["sunk"].add(scatter_id)
            state["mains"].add(main_card["id"])
            state["content"][main_card["id"]] = content
        return sent, merged
