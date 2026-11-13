"""
Field lookup tool: extract specific fields from the dispute request.
"""
from __future__ import annotations

from typing import Any, Optional


def field_lookup(dispute: dict, field_name: str) -> Any:
    """
    Retrieve a specific field from the dispute request.

    Args:
        dispute: The dispute request dictionary
        field_name: Field to retrieve (e.g., 'premises_type', 'connected_load_kw')

    Returns:
        Field value or None if not found
    """
    if not dispute or not field_name:
        return None

    # Direct key lookup
    if field_name in dispute:
        return dispute[field_name]

    # Case-insensitive lookup
    field_lower = field_name.lower()
    for key, val in dispute.items():
        if key.lower() == field_lower:
            return val

    # Common aliases
    aliases = {
        "load": "connected_load_kw",
        "kw": "connected_load_kw",
        "connected_load": "connected_load_kw",
        "type": "premises_type",
        "premises": "premises_type",
        "reason": "dispute_reason",
        "category": "tariff_category_applied",
        "tariff": "tariff_category_applied",
        "amount": "disputed_amount",
        "month": "billing_month",
        "additional_use": "additional_premises_use",
        "home_use": "additional_premises_use",
        "mixed_use": "additional_premises_use",
    }

    alias_key = aliases.get(field_lower)
    if alias_key and alias_key in dispute:
        return dispute[alias_key]

    return None


def get_dispute_summary(dispute: dict) -> str:
    """Return a human-readable summary of key dispute fields."""
    fields = [
        ("Consumer ID", "consumer_id"),
        ("Premises Type", "premises_type"),
        ("Connected Load (kW)", "connected_load_kw"),
        ("Billing Month", "billing_month"),
        ("Disputed Amount (INR)", "disputed_amount"),
        ("Tariff Category Applied", "tariff_category_applied"),
        ("Dispute Reason", "dispute_reason"),
        ("Additional Premises Use", "additional_premises_use"),
    ]
    lines = []
    for label, key in fields:
        val = dispute.get(key)
        if val is not None:
            lines.append(f"- {label}: {val}")
    return "\n".join(lines)
