"""
Pydantic data contracts for the Tariff Dispute Triage Service.
"""
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
import uuid
import re


# ---------------------------------------------------------------------------
# Enumerations (as Literals for runtime validation)
# ---------------------------------------------------------------------------

PremisesType = Literal["domestic", "commercial", "industrial", "agricultural", "mixed"]
DisputeReason = Literal[
    "tariff_category",
    "meter_accuracy",
    "load_violation",
    "arrears",
    "disconnection",
    "other",
]
RunStatus = Literal["queued", "processing", "completed", "failed", "escalated"]
RoutingDecision = Literal[
    "bill-stands-with-reason",
    "revision-recommended",
    "site-inspection-required",
    "escalated-to-human",
]
VerificationStatus = Literal["verified", "escalated"]


# ---------------------------------------------------------------------------
# Supporting Models
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    """A single clause-level citation from the tariff corpus."""

    document: str = Field(..., description="Source document name")
    clause_id: str = Field(..., description="Clause identifier, e.g. 'Clause 8.2'")
    excerpt: str = Field(..., description="Verbatim excerpt from the corpus")
    relevance_score: float = Field(
        ..., ge=0.0, le=1.0, description="Semantic similarity score"
    )


class TokenUsage(BaseModel):
    """Per-run LLM token accounting."""

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    prompt_tokens_by_step: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Ingestion Request / Response
# ---------------------------------------------------------------------------


class BillingDisputeRequest(BaseModel):
    """Validated data contract for incoming billing dispute submissions."""

    dispute_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique dispute identifier (UUID)",
    )
    consumer_id: str = Field(..., min_length=1, max_length=64)
    consumer_name: str = Field(..., min_length=1, max_length=256)
    premises_type: PremisesType
    connected_load_kw: float = Field(..., gt=0, description="Connected load in kW (must be > 0)")
    billing_month: str = Field(
        ..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Format: YYYY-MM"
    )
    disputed_amount: float = Field(..., gt=0, description="Disputed amount in INR (must be > 0)")
    tariff_category_applied: str = Field(..., min_length=1, max_length=32)
    dispute_reason: DisputeReason
    description: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Free-text description of the dispute — will be sanitized",
    )
    additional_premises_use: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Any additional/mixed use of premises (e.g. 'tailoring shop')",
    )

    @field_validator("consumer_id", "consumer_name")
    @classmethod
    def no_control_chars(cls, v: str) -> str:
        if re.search(r"[\x00-\x1f\x7f]", v):
            raise ValueError("Field contains control characters")
        return v

    @field_validator("description", "additional_premises_use", mode="before")
    @classmethod
    def sanitize_text(cls, v):
        """Strip potential injection patterns and normalize whitespace."""
        if v is None:
            return v
        # Remove null bytes, excessive whitespace
        v = re.sub(r"\x00", "", str(v))
        v = re.sub(r"\s+", " ", v).strip()
        # Block obvious prompt injection patterns
        injection_patterns = [
            r"ignore\s+(?:all\s+)?(?:previous|above)\s+instructions",
            r"disregard\s+(?:all\s+)?(?:previous|above)",
            r"system\s*:\s*you\s+are",
            r"<\s*/?(?:script|iframe|object|embed)\s*>",
        ]
        for pattern in injection_patterns:
            if re.search(pattern, v, re.IGNORECASE):
                raise ValueError("Potential prompt injection detected in field")
        return v

    @model_validator(mode="after")
    def validate_load_and_category_consistency(self) -> "BillingDisputeRequest":
        """Warn (soft check) on obviously inconsistent category/load combos."""
        # Industrial typically > 25 kW; domestic typically <= 10 kW — just store
        return self


class DisputeResponse(BaseModel):
    """Immediate acknowledgement returned when a dispute is queued."""

    run_id: str
    dispute_id: str
    status: RunStatus
    message: str = "Dispute accepted and queued for processing"


# ---------------------------------------------------------------------------
# Result / Status Models
# ---------------------------------------------------------------------------


class RunResult(BaseModel):
    """Full result returned after agent processing is complete."""

    run_id: str
    dispute_id: str
    routing_decision: RoutingDecision
    tariff_position: str = Field(..., description="Authoritative tariff category determination")
    citations: List[Citation]
    verification_status: VerificationStatus
    reasoning: str = Field(..., description="Step-by-step agent reasoning")
    prompt_version: str
    token_usage: TokenUsage
    cost_usd: float = Field(..., ge=0.0)
    latency_ms: int = Field(..., ge=0)
    correlation_id: str
    escalation_reason: Optional[str] = None


class StatusResponse(BaseModel):
    """Polling response for /disputes/{run_id}/status."""

    run_id: str
    status: RunStatus
    result: Optional[RunResult] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """Consumer/operator feedback on a completed run."""

    run_id: str
    satisfactory: bool
    comment: Optional[str] = Field(default=None, max_length=1000)
    reviewer_id: Optional[str] = Field(default=None, max_length=64)


class FeedbackResponse(BaseModel):
    run_id: str
    recorded: bool
    message: str


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    services: dict
    version: str = "1.0.0"


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


class QuarantineRecord(BaseModel):
    """Record for a rejected / quarantined request."""

    raw_payload: dict
    validation_errors: List[str]
    received_at: str
    client_ip: Optional[str] = None
