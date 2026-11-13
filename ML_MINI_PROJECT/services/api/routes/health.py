"""Health check endpoint."""
from __future__ import annotations

import os
import logging
from typing import Any, Dict

import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from models import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Service health check")
async def health(request: Request):
    services: Dict[str, Any] = {}
    overall = "healthy"

    # Redis check
    try:
        redis: aioredis.Redis = request.app.state.redis
        await redis.ping()
        services["redis"] = "ok"
    except Exception as e:
        services["redis"] = f"error: {e}"
        overall = "degraded"

    # FAISS index check
    faiss_path = os.getenv("FAISS_INDEX_PATH", "/app/faiss_index")
    import os as _os
    services["faiss_index"] = "ok" if _os.path.isdir(faiss_path) else "missing"
    if services["faiss_index"] == "missing":
        overall = "degraded"

    resp = HealthResponse(status=overall, services=services)
    status_code = 200 if overall == "healthy" else 207
    return JSONResponse(content=resp.model_dump(), status_code=status_code)


@router.get("/ready", include_in_schema=False)
async def readiness():
    return {"status": "ready"}
