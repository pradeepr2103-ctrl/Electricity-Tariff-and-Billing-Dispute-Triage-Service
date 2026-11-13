"""
Dispute submission and status polling routes.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder

from models import BillingDisputeRequest, DisputeResponse, StatusResponse
from validation import validate_dispute

logger = logging.getLogger(__name__)
router = APIRouter()

CELERY_BROKER = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/1")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/2")


async def get_redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis


# ---------------------------------------------------------------------------
# Submit dispute
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=DisputeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a billing dispute for triage",
)
async def submit_dispute(
    request: Request,
    payload: dict,
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    Accept a billing dispute, validate it, enqueue it for async agent processing,
    and return a run_id for status polling.
    """
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))

    validated, errors = validate_dispute(payload)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"validation_errors": errors, "message": "Request quarantined"},
        )

    run_id = str(uuid.uuid4())

    # Store initial status in Redis
    run_meta = {
        "run_id": run_id,
        "dispute_id": validated.dispute_id,
        "status": "queued",
        "correlation_id": correlation_id,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    await redis.setex(f"run:{run_id}", 86400, json.dumps(run_meta))

    # Enqueue Celery task via Redis list (simple approach)
    task_payload = {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "dispute": jsonable_encoder(validated),
    }
    await redis.lpush("tariff:tasks", json.dumps(task_payload))

    logger.info(
        "Dispute queued",
        extra={
            "run_id": run_id,
            "dispute_id": validated.dispute_id,
            "correlation_id": correlation_id,
        },
    )

    return DisputeResponse(
        run_id=run_id,
        dispute_id=validated.dispute_id,
        status="queued",
    )


@router.post(
    "/submit",
    response_model=DisputeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a billing dispute (typed body)",
)
async def submit_dispute_typed(
    request: Request,
    body: BillingDisputeRequest,
    redis: aioredis.Redis = Depends(get_redis),
):
    """Typed variant of the dispute submission endpoint."""
    correlation_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    run_id = str(uuid.uuid4())

    run_meta = {
        "run_id": run_id,
        "dispute_id": body.dispute_id,
        "status": "queued",
        "correlation_id": correlation_id,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    await redis.setex(f"run:{run_id}", 86400, json.dumps(run_meta))

    task_payload = {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "dispute": jsonable_encoder(body),
    }
    await redis.lpush("tariff:tasks", json.dumps(task_payload))

    logger.info("Typed dispute queued", extra={"run_id": run_id, "correlation_id": correlation_id})
    return DisputeResponse(run_id=run_id, dispute_id=body.dispute_id, status="queued")


# ---------------------------------------------------------------------------
# Poll status
# ---------------------------------------------------------------------------
@router.get(
    "/{run_id}/status",
    response_model=StatusResponse,
    summary="Poll the status of a queued dispute",
)
async def get_status(
    run_id: str,
    redis: aioredis.Redis = Depends(get_redis),
):
    """Return current status and (if complete) full result for a run."""
    raw = await redis.get(f"run:{run_id}")
    if not raw:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    data = json.loads(raw)
    result_raw = await redis.get(f"result:{run_id}")
    result = json.loads(result_raw) if result_raw else None

    return StatusResponse(
        run_id=run_id,
        status=data.get("status", "queued"),
        result=result,
        error=data.get("error"),
    )


@router.get("", summary="List recent runs (admin)")
async def list_runs(
    limit: int = 20,
    redis: aioredis.Redis = Depends(get_redis),
):
    """List recent run IDs (for admin / debugging)."""
    keys = await redis.keys("run:*")
    runs = []
    for key in keys[:limit]:
        raw = await redis.get(key)
        if raw:
            runs.append(json.loads(raw))
    return {"runs": runs, "count": len(runs)}
