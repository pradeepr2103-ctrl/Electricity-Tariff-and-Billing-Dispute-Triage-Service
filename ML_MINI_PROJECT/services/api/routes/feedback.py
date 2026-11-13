"""Feedback endpoint for marking results satisfactory/not."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request

from models import FeedbackRequest, FeedbackResponse

logger = logging.getLogger(__name__)
router = APIRouter()


async def get_redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis


@router.post("/{run_id}", response_model=FeedbackResponse, summary="Submit feedback on a result")
async def submit_feedback(
    run_id: str,
    body: FeedbackRequest,
    redis: aioredis.Redis = Depends(get_redis),
):
    """Record operator/consumer feedback for a completed run."""
    if body.run_id != run_id:
        raise HTTPException(status_code=400, detail="run_id mismatch")

    raw = await redis.get(f"run:{run_id}")
    if not raw:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    feedback = {
        "run_id": run_id,
        "satisfactory": body.satisfactory,
        "comment": body.comment,
        "reviewer_id": body.reviewer_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    await redis.setex(f"feedback:{run_id}", 86400 * 30, json.dumps(feedback))
    await redis.lpush("tariff:feedback", json.dumps(feedback))

    logger.info("Feedback recorded", extra={"run_id": run_id, "satisfactory": body.satisfactory})
    return FeedbackResponse(run_id=run_id, recorded=True, message="Feedback recorded successfully")


@router.get("", summary="List recent feedback (admin)")
async def list_feedback(limit: int = 20, redis: aioredis.Redis = Depends(get_redis)):
    entries = await redis.lrange("tariff:feedback", 0, limit - 1)
    return {"feedback": [json.loads(e) for e in entries]}
