"""从宿主提供的已核验事件、现有 Claim 和定时回执选择维护任务。"""
from datetime import timedelta
from .maintenance_plan import TopicSignal, CONTEMPLATION_DEBOUNCE, REPEAT_THRESHOLD_14D, INDEPENDENCE_THRESHOLD, plan_maintenance

def conversation_signal(branch, epoch_id, processed, activation_floor_seq, last_completed):
    fresh = []
    for event in branch:
        if not isinstance(event, dict):
            continue
        if int(event.get("seq") or 0) <= activation_floor_seq:
            continue
        if str(event.get("status") or "active") != "active":
            continue
        if str(event.get("role") or "") not in {"assistant", "activity"}:
            continue
        if str(event.get("kind") or "message") == "no_response":
            continue
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            continue
        durable_ref = f"canonical-message:{epoch_id}:{event_id}"
        if durable_ref in processed:
            continue
        fresh.append(event)
    if not fresh:
        return None
    last_event_id = str(fresh[-1].get("event_id") or "").strip()
    return {
        "signal": TopicSignal(
            topic_key="conversation.recent",
            new_agent_evidence_count=len(fresh),
            last_maintenance_at=(
                last_completed[2] if last_completed is not None else None
            ),
        ),
        "trigger_kind": "conversation_batch",
        "trigger_key": f"conversation:{epoch_id}:{last_event_id}",
        "new_agent_evidence_count": len(fresh),
    }


def contemplation_signal(rows, now_dt, completed_keys, latest_completed, parse_time):
    for row in rows:
        contemplation_id = int(row[0])
        created_at = parse_time(
            row[1], assume_local=True
        )
        if created_at is None:
            continue
        if now_dt < created_at + CONTEMPLATION_DEBOUNCE:
            continue
        trigger_key = f"contemplation:{contemplation_id}"
        if ("contemplation", trigger_key) in completed_keys:
            continue
        return {
            "signal": TopicSignal(
                topic_key=f"contemplation.{contemplation_id}",
                contemplation_at=created_at,
                last_maintenance_at=(
                    latest_completed[2]
                    if latest_completed is not None else None
                ),
            ),
            "trigger_kind": "contemplation",
            "trigger_key": trigger_key,
            "contemplation_id": contemplation_id,
        }
    return None


def claim_signals(runtime, now_dt, completed_keys, *, include_repetition, latest_completed, digest, parse_time):
    results = []
    cutoff = now_dt - timedelta(days=14)
    last_at = latest_completed[2] if latest_completed is not None else None
    for claim in runtime.self_model_store.p3c_latest_claims():
        if claim.namespace != runtime.namespace or claim.maturity == "retired":
            continue
        contradiction_ids = tuple(sorted(claim.contradiction_evidence_ids))
        if contradiction_ids:
            fingerprint = digest(
                [claim.claim_id, claim.version, *contradiction_ids]
            ).split(":", 1)[-1][:24]
            trigger_key = f"contradiction:{fingerprint}"
            if ("contradiction", trigger_key) not in completed_keys:
                results.append({
                    "signal": TopicSignal(
                        topic_key=f"claim.{fingerprint}.contradiction",
                        has_contradiction=True,
                        last_maintenance_at=last_at,
                    ),
                    "trigger_kind": "contradiction",
                    "trigger_key": trigger_key,
                    "claim_id": claim.claim_id,
                })
        if not include_repetition:
            continue
        evidence = []
        for evidence_id in claim.support_evidence_ids:
            item = runtime.self_model_store.evidence(evidence_id)
            if (
                item is None
                or not item.committed
                or not item.eligible_for_trait_support
            ):
                continue
            occurred_at = parse_time(item.occurred_at)
            if occurred_at is None or occurred_at < cutoff:
                continue
            evidence.append(item)
        independence_keys = {
            item.independence_key for item in evidence
            if str(item.independence_key or "")
        }
        if len(evidence) < REPEAT_THRESHOLD_14D or len(independence_keys) < INDEPENDENCE_THRESHOLD:
            continue
        if last_at is not None and not any(
            (
                parse_time(item.occurred_at) is not None
                and parse_time(item.occurred_at) > last_at
            )
            for item in evidence
        ):
            continue
        fingerprint = digest(
            [
                claim.claim_id,
                claim.version,
                *sorted(item.evidence_id for item in evidence),
            ]
        ).split(":", 1)[-1][:24]
        trigger_key = f"topic-repeat:{fingerprint}"
        if ("topic_repeat", trigger_key) in completed_keys:
            continue
        results.append({
            "signal": TopicSignal(
                topic_key=f"claim.{fingerprint}.repeat",
                repeat_count_14d=len(evidence),
                independence_key_count=len(independence_keys),
                last_maintenance_at=last_at,
            ),
            "trigger_kind": "topic_repeat",
            "trigger_key": trigger_key,
            "claim_id": claim.claim_id,
        })
    return results


def choose_maintenance(candidates, now_dt, completed, failed, event_only, *, retry_seconds=3600):
    if not candidates:
        return {
            "should_run": False,
            "reason": "no_eligible_signal",
            "event_only": bool(event_only),
        }
    plan = plan_maintenance(
        [item["signal"] for item in candidates],
        now=now_dt,
        last_scan_at=now_dt,
        last_real_run_at=(completed[2] if completed is not None else None),
    )
    runnable_by_topic = {
        item.topic_key: item for item in plan.eligible_topics
    }
    chosen = next(
        (
            item for item in candidates
            if item["signal"].topic_key in runnable_by_topic
        ),
        None,
    )
    if chosen is None:
        return {
            "should_run": False,
            "reason": (
                "daily_limit"
                if plan.daily_limit_blocked
                else "topic_cooldown_or_threshold"
            ),
            "event_only": bool(event_only),
        }
    eligibility = runnable_by_topic[chosen["signal"].topic_key]
    if (
        failed is not None
        and now_dt < failed[2] + timedelta(
            seconds=retry_seconds
        )
        and (
            completed is None
            or failed[2] > completed[2]
        )
    ):
        return {
            "should_run": False,
            "reason": "failure_backoff",
            "retry_after": (
                failed[2]
                + timedelta(
                    seconds=retry_seconds
                )
            ).isoformat(),
            "event_only": bool(event_only),
        }
    return {
        "should_run": True,
        "urgent": bool(eligibility.urgent),
        "trigger_kind": chosen["trigger_kind"],
        "trigger_key": chosen["trigger_key"],
        "reasons": list(eligibility.reasons),
        "event_only": bool(event_only),
    }
