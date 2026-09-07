"""Zeta outcome assertion helpers for IDXSY Signal S4."""

from __future__ import annotations

from datetime import datetime
from typing import Any

FINAL_ZETA_STATUSES = frozenset({"TP_HIT", "SL_HIT", "EXPIRED"})
RESOLVER_VERSION = "zeta-authority-v1"


def is_final_zeta_status(status: Any) -> bool:
    return str(status or "").strip().upper() in FINAL_ZETA_STATUSES


def build_assertion_payload(
    *,
    user_id: str,
    signal_id: str,
    source_record_id: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    status = str(source.get("status") or "").strip().upper()
    if status not in FINAL_ZETA_STATUSES:
        raise ValueError(f"Not a final Zeta status: {status or '<empty>'}")
    return {
        "user_id": user_id,
        "signal_id": signal_id,
        "source_record_id": source_record_id,
        "outcome_status": status,
        "profit_pct": source.get("profit_pct"),
        "source_resolved_at": source.get("resolved_at_utc"),
        "assertion_data": {
            "zeta_id": str(source["zeta_id"]),
            "source_timestamp_utc": source.get("source_timestamp_utc"),
            "resolved_at_raw": source.get("resolved_at_raw"),
            "source_interface": "MEMBER_API",
        },
    }


def assertion_semantically_equal(existing: dict[str, Any], new: dict[str, Any]) -> bool:
    """Ignore database-owned metadata when checking whether an assertion changed."""
    keys = ("signal_id", "source_record_id", "outcome_status", "profit_pct", "source_resolved_at")
    if any(existing.get(key) != new.get(key) for key in keys):
        return False
    old_data = existing.get("assertion_data") or {}
    new_data = new.get("assertion_data") or {}
    return (
        old_data.get("zeta_id") == new_data.get("zeta_id")
        and old_data.get("resolved_at_raw") == new_data.get("resolved_at_raw")
        and old_data.get("source_interface") == new_data.get("source_interface")
    )
