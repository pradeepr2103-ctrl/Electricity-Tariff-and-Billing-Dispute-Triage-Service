"""
Validation middleware + quarantine storage for rejected requests.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError

from models import BillingDisputeRequest, QuarantineRecord

logger = logging.getLogger(__name__)

QUARANTINE_DIR = Path(os.getenv("QUARANTINE_DIR", "/app/quarantine"))
QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)


def validate_dispute(payload: Dict[str, Any]) -> Tuple[BillingDisputeRequest | None, List[str]]:
    """
    Validate an incoming dispute payload.

    Returns:
        (BillingDisputeRequest, []) on success
        (None, [error_messages]) on failure — side-effect: writes quarantine record
    """
    try:
        request = BillingDisputeRequest(**payload)
        return request, []
    except ValidationError as exc:
        errors = [f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors()]
        _quarantine(payload, errors)
        return None, errors
    except Exception as exc:
        errors = [f"Unexpected validation error: {str(exc)}"]
        _quarantine(payload, errors)
        return None, errors


def _quarantine(payload: Dict[str, Any], errors: List[str]) -> str:
    """Persist a rejected payload to the quarantine directory."""
    record = QuarantineRecord(
        raw_payload=payload,
        validation_errors=errors,
        received_at=datetime.now(timezone.utc).isoformat(),
    )
    filename = f"rejected_{uuid.uuid4().hex}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    path = QUARANTINE_DIR / filename
    try:
        path.write_text(json.dumps(record.model_dump(), indent=2, default=str))
        logger.warning("Quarantined invalid request", extra={"quarantine_file": str(path), "errors": errors})
    except Exception as write_exc:
        logger.error("Failed to write quarantine record: %s", write_exc)
    return str(path)


def list_quarantine(limit: int = 50) -> List[Dict]:
    """Return the most recent quarantine records (for admin/ops use)."""
    files = sorted(QUARANTINE_DIR.glob("rejected_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    records = []
    for f in files[:limit]:
        try:
            records.append(json.loads(f.read_text()))
        except Exception:
            pass
    return records
