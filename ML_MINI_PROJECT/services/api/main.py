"""
FastAPI application entry-point for the Tariff Dispute Triage Service.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram, make_asgi_app
from prometheus_client import multiprocess, CollectorRegistry

from routes.disputes import router as disputes_router
from routes.feedback import router as feedback_router
from routes.health import router as health_router

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","message":"%(message)s","correlation_id":"%(correlation_id)s"}',
)

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
def setup_tracing():
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "tariff-api")})
    provider = TracerProvider(resource=resource)
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
    try:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    except Exception as e:
        logging.getLogger(__name__).warning("Could not configure OTLP exporter: %s", e)
    trace.set_tracer_provider(provider)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
REQUEST_COUNT = Counter(
    "api_requests_total", "Total API requests", ["method", "endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "api_request_latency_seconds", "API request latency", ["endpoint"]
)
DISPUTE_SUBMITTED = Counter("disputes_submitted_total", "Disputes submitted")
DISPUTE_QUARANTINED = Counter("disputes_quarantined_total", "Disputes quarantined")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_tracing()
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    app.state.redis = aioredis.from_url(redis_url, decode_responses=True)
    logging.getLogger(__name__).info("API service started")
    yield
    await app.state.redis.aclose()
    logging.getLogger(__name__).info("API service shutdown")


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Electricity Tariff & Billing Dispute Triage Service",
    description="Multi-agent triage service for electricity billing disputes (KERC tariff orders)",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instrument FastAPI for OTEL
FastAPIInstrumentor.instrument_app(app)

# Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

# ---------------------------------------------------------------------------
# Correlation ID middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    request.state.correlation_id = correlation_id

    start = time.perf_counter()
    response: Response = await call_next(request)
    latency = time.perf_counter() - start

    response.headers["X-Correlation-ID"] = correlation_id
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code,
    ).inc()
    REQUEST_LATENCY.labels(endpoint=request.url.path).observe(latency)
    return response


# ---------------------------------------------------------------------------
# Rate limiting middleware (Redis token bucket)
# ---------------------------------------------------------------------------
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in ("/health", "/metrics", "/docs", "/redoc", "/openapi.json"):
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    redis_client = getattr(request.app.state, "redis", None)

    if redis_client:
        key = f"rate_limit:{client_ip}"
        limit = int(os.getenv("RATE_LIMIT_PER_CLIENT_PER_MINUTE", "30"))
        try:
            pipe = redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 60)
            results = await pipe.execute()
            count = results[0]
            if count > limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again later."},
                    headers={"Retry-After": "60"},
                )
        except Exception:
            pass  # Fail open if Redis is down

    return await call_next(request)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(disputes_router, prefix="/disputes", tags=["Disputes"])
app.include_router(feedback_router, prefix="/feedback", tags=["Feedback"])
app.include_router(health_router, tags=["Health"])


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Tariff Dispute Triage Service", "version": "1.0.0", "docs": "/docs"}
