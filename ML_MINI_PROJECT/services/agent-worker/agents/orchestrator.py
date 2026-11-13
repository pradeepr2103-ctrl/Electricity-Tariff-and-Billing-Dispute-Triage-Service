"""
Orchestrator Agent: LangGraph StateGraph orchestrating the multi-agent pipeline.

Graph topology:
  START → classifier → retriever → verifier → [re-retrieve | router | escalate] → END

Conditional edges:
  - verifier → re-retrieve: if verification partial and iterations < max
  - verifier → escalate: if verification failed
  - verifier → router: if verified
  - router → END
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, END

from agents.classifier import classify_dispute
from agents.retriever import retrieve_tariff_info
from agents.verifier import verify_citations
from agents.router import route_case
from cost_tracker import CostTracker

logger = logging.getLogger(__name__)

MAX_ITERATIONS = int(os.getenv("MAX_AGENT_ITERATIONS", "10"))
MAX_RETRIEVAL_RETRIES = 2


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    run_id: str
    dispute_id: str
    correlation_id: str
    dispute: Dict[str, Any]
    classification: Optional[Dict[str, Any]]
    retrieval_result: Optional[Dict[str, Any]]
    verification_result: Optional[Dict[str, Any]]
    routing_result: Optional[Dict[str, Any]]
    iteration: int
    retrieval_retries: int
    error: Optional[str]
    status: str  # "running" | "completed" | "failed" | "escalated"


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------

def node_classify(state: AgentState) -> AgentState:
    logger.info("→ Classifier node", extra={"run_id": state["run_id"]})
    try:
        cost_tracker: CostTracker = state.get("_cost_tracker")  # type: ignore
        classification = classify_dispute(state["dispute"], cost_tracker)
        return {**state, "classification": classification, "iteration": state["iteration"] + 1}
    except Exception as e:
        logger.error("Classifier node error: %s", e)
        return {**state, "error": str(e), "status": "failed"}


def node_retrieve(state: AgentState) -> AgentState:
    logger.info("→ Retriever node (retry=%d)", state["retrieval_retries"], extra={"run_id": state["run_id"]})
    try:
        cost_tracker: CostTracker = state.get("_cost_tracker")  # type: ignore
        retrieval_result = retrieve_tariff_info(
            state["dispute"],
            state["classification"],
            cost_tracker,
        )
        return {
            **state,
            "retrieval_result": retrieval_result,
            "iteration": state["iteration"] + 1,
        }
    except Exception as e:
        logger.error("Retriever node error: %s", e)
        return {**state, "error": str(e), "status": "failed"}


def node_verify(state: AgentState) -> AgentState:
    logger.info("→ Verifier node", extra={"run_id": state["run_id"]})
    try:
        cost_tracker: CostTracker = state.get("_cost_tracker")  # type: ignore
        verification_result = verify_citations(
            state["retrieval_result"],
            state["dispute"],
            cost_tracker,
        )
        return {
            **state,
            "verification_result": verification_result,
            "iteration": state["iteration"] + 1,
        }
    except Exception as e:
        logger.error("Verifier node error: %s", e)
        return {**state, "error": str(e), "status": "failed"}


def node_route(state: AgentState) -> AgentState:
    logger.info("→ Router node", extra={"run_id": state["run_id"]})
    try:
        cost_tracker: CostTracker = state.get("_cost_tracker")  # type: ignore
        routing_result = route_case(
            state["dispute"],
            state["classification"],
            state["retrieval_result"],
            state["verification_result"],
            state["run_id"],
            cost_tracker,
        )
        status = "escalated" if routing_result.get("routing_decision") == "escalated-to-human" else "completed"
        return {
            **state,
            "routing_result": routing_result,
            "status": status,
            "iteration": state["iteration"] + 1,
        }
    except Exception as e:
        logger.error("Router node error: %s", e)
        return {**state, "error": str(e), "status": "failed"}


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------

def should_re_retrieve(state: AgentState) -> str:
    """After verification, decide: re-retrieve, route, or escalate."""
    if state.get("status") == "failed":
        return "end"

    verification = state.get("verification_result", {})
    v_status = verification.get("verification_status", "failed")
    needs_escalation = verification.get("needs_escalation", False)

    if needs_escalation or v_status == "failed":
        if state["retrieval_retries"] < MAX_RETRIEVAL_RETRIES:
            logger.info("Re-retrieve: verification failed, retry %d", state["retrieval_retries"] + 1)
            return "re_retrieve"
        else:
            return "route"  # Route will handle escalation

    return "route"


def is_running(state: AgentState) -> str:
    if state.get("status") == "failed" or state["iteration"] >= MAX_ITERATIONS:
        return "end"
    return "continue"


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    graph = StateGraph(AgentState)

    graph.add_node("classifier", node_classify)
    graph.add_node("retriever", node_retrieve)
    graph.add_node("verifier", node_verify)
    graph.add_node("router", node_route)

    graph.set_entry_point("classifier")
    graph.add_edge("classifier", "retriever")
    graph.add_edge("retriever", "verifier")

    graph.add_conditional_edges(
        "verifier",
        should_re_retrieve,
        {
            "re_retrieve": "retriever",
            "route": "router",
            "end": END,
        },
    )

    graph.add_edge("router", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Run function
# ---------------------------------------------------------------------------

def run_orchestrator(
    run_id: str,
    dispute_id: str,
    correlation_id: str,
    dispute: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Execute the full multi-agent pipeline.

    Returns:
        RunResult dict ready to be stored.
    """
    t_start = time.time()
    cost_tracker = CostTracker(run_id=run_id)

    initial_state: AgentState = {
        "run_id": run_id,
        "dispute_id": dispute_id,
        "correlation_id": correlation_id,
        "dispute": dispute,
        "classification": None,
        "retrieval_result": None,
        "verification_result": None,
        "routing_result": None,
        "iteration": 0,
        "retrieval_retries": 0,
        "error": None,
        "status": "running",
        "_cost_tracker": cost_tracker,  # type: ignore
    }

    try:
        app_graph = build_graph()
        final_state = app_graph.invoke(initial_state)
    except Exception as e:
        logger.error("Orchestrator error: %s", e)
        final_state = {**initial_state, "error": str(e), "status": "failed"}

    latency_ms = int((time.time() - t_start) * 1000)
    routing = final_state.get("routing_result") or {}
    verification = final_state.get("verification_result") or {}

    citations = routing.get("citations", [])
    for c in citations:
        if "relevance_score" not in c:
            c["relevance_score"] = 0.5

    return {
        "run_id": run_id,
        "dispute_id": dispute_id,
        "routing_decision": routing.get("routing_decision", "escalated-to-human"),
        "tariff_position": routing.get("tariff_position", "Unable to determine"),
        "citations": citations,
        "verification_status": (
            "verified" if verification.get("verification_status") == "verified"
            else "escalated"
        ),
        "reasoning": routing.get("reasoning", final_state.get("error", "No reasoning produced")),
        "prompt_version": os.getenv("PROMPT_VERSION", "v2"),
        "token_usage": cost_tracker.to_token_usage(),
        "cost_usd": round(cost_tracker.total_cost_usd, 6),
        "latency_ms": latency_ms,
        "correlation_id": correlation_id,
        "escalation_reason": routing.get("escalation_reason"),
    }
