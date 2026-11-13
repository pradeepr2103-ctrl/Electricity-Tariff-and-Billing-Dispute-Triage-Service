"""
Escalation tool: flag a case for human review.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import redis as redis_lib

logger = logging.getLogger(__name__)


def escalate_to_human(
    run_id: str,
    dispute_id: str,
    reason: str,
    partial_analysis: str = "",
) -> dict:
    """
    Escalate a case to human review queue.

    Args:
        run_id: Current agent run ID
        dispute_id: The dispute being processed
        reason: Why escalation is needed
        partial_analysis: Any partial analysis done so far

    Returns:
        Dict with escalation_id and status
    """
    escalation_record = {
        "escalation_id": f"ESC-{int(time.time())}-{dispute_id[:8]}",
        "run_id": run_id,
        "dispute_id": dispute_id,
        "reason": reason,
        "partial_analysis": partial_analysis,
        "escalated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_human_review",
    }

    logger.warning(
        "Case escalated to human",
        extra={
            "run_id": run_id,
            "dispute_id": dispute_id,
            "reason": reason,
            "escalation_id": escalation_record["escalation_id"],
        },
    )

    # Push to escalation queue in Redis
    try:
        r = redis_lib.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True,
        )
        r.lpush("tariff:escalations", json.dumps(escalation_record))
        r.setex(
            f"escalation:{escalation_record['escalation_id']}",
            86400 * 7,
            json.dumps(escalation_record),
        )
    except Exception as e:
        logger.error("Failed to push escalation to Redis: %s", e)

    return escalation_record
