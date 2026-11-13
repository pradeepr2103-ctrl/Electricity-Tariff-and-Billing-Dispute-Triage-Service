"""
Token counting and cost tracking per step and per run.
Supports Google Gemini pricing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Gemini 1.5 Flash pricing (USD per 1M tokens, as of 2024)
# Input: $0.075 / 1M tokens, Output: $0.30 / 1M tokens
PRICING = {
    "gemini-1.5-flash": {"input_per_1m": 0.075, "output_per_1m": 0.30},
    "gemini-1.5-pro": {"input_per_1m": 3.50, "output_per_1m": 10.50},
    "gemini-1.0-pro": {"input_per_1m": 0.50, "output_per_1m": 1.50},
    "stub": {"input_per_1m": 0.0, "output_per_1m": 0.0},
}


@dataclass
class StepUsage:
    step_name: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = "gemini-1.5-flash"
    latency_ms: int = 0
    timestamp: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float:
        pricing = PRICING.get(self.model, PRICING["gemini-1.5-flash"])
        input_cost = (self.prompt_tokens / 1_000_000) * pricing["input_per_1m"]
        output_cost = (self.completion_tokens / 1_000_000) * pricing["output_per_1m"]
        return input_cost + output_cost


class CostTracker:
    """Accumulates token usage and costs for a single agent run."""

    def __init__(self, run_id: str, model: str = "gemini-1.5-flash"):
        self.run_id = run_id
        self.model = model
        self.steps: List[StepUsage] = []
        self._run_start = time.time()

    def record_step(
        self,
        step_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int = 0,
    ) -> StepUsage:
        usage = StepUsage(
            step_name=step_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=self.model,
            latency_ms=latency_ms,
        )
        self.steps.append(usage)
        logger.debug(
            "Step usage recorded",
            extra={
                "run_id": self.run_id,
                "step": step_name,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": usage.cost_usd,
            },
        )
        return usage

    @property
    def total_prompt_tokens(self) -> int:
        return sum(s.prompt_tokens for s in self.steps)

    @property
    def total_completion_tokens(self) -> int:
        return sum(s.completion_tokens for s in self.steps)

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    @property
    def total_cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.steps)

    @property
    def total_latency_ms(self) -> int:
        return int((time.time() - self._run_start) * 1000)

    def to_dict(self) -> Dict:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.total_cost_usd, 6),
            "latency_ms": self.total_latency_ms,
            "steps": [
                {
                    "step": s.step_name,
                    "prompt_tokens": s.prompt_tokens,
                    "completion_tokens": s.completion_tokens,
                    "cost_usd": round(s.cost_usd, 6),
                    "latency_ms": s.latency_ms,
                }
                for s in self.steps
            ],
        }

    def to_token_usage(self) -> dict:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "prompt_tokens_by_step": {s.step_name: s.prompt_tokens for s in self.steps},
        }
