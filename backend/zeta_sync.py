#!/usr/bin/env python3
"""IDXSY Signal S4 — canonical Zeta Member API synchronization.

Authoritative flow:
    Zeta Member API -> source_records -> reconciliation/source link
                    -> outcome assertion -> canonical outcome resolver

The incomplete public Zeta API is intentionally NOT a fallback. Member API
failure makes the run fail/retry so a partial source cannot masquerade as a
successful canonical synchronization.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from supabase import Client, create_client
from urllib3.util.retry import Retry

try:
    from backend.outcome_resolver import (
        RESOLVER_VERSION,
        assertion_semantically_equal,
        build_assertion_payload,
        is_final_zeta_status,
    )
    from backend.reconciliation import match_zeta_to_telegram
except ModuleNotFoundError:  # direct `python backend/zeta_sync.py`
    from outcome_resolver import (  # type: ignore
        RESOLVER_VERSION,
        assertion_semantically_equal,
        build_assertion_payload,
        is_final_zeta_status,
    )
    from reconciliation import match_zeta_to_telegram  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("zeta-sync")

MEMBER_API_URL = "https://member.zeta-ai.pro/api/signals"
PARSER_VERSION = "zeta-live-v1"
WIB = ZoneInfo("Asia/Jakarta")
UTC = timezone.utc

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_USER_ID = os.environ["SUPABASE_USER_ID"]
ZETA_MEMBER_COOKIE = os.environ.get("ZETA_MEMBER_COOKIE", "").strip()

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def first_row(response: Any) -> dict[str, Any] | None:
    rows = response.data or []
    return rows[0] if rows else None


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def payload_equal(left: Any, right: Any) -> bool:
    """Compare source payloads independently of the fingerprint algorithm.

    Reconstruction and live ingestion intentionally may use different fingerprint
    encodings. Raw payload equality is therefore the cross-version compatibility
    boundary that prevents the first live run from rewriting 1,000 historical
    Zeta sources merely because the parser/fingerprint implementation changed.
    """
    try:
        return canonical_json(left) == canonical_json(right)
    except (TypeError, ValueError):
        return left == right


def fingerprint_payload(raw: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


def member_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_member_signals() -> list[dict[str, Any]]:
    if not ZETA_MEMBER_COOKIE:
        raise RuntimeError("ZETA_MEMBER_COOKIE is required; public API fallback is disabled")

    cookie = ZETA_MEMBER_COOKIE if "=" in ZETA_MEMBER_COOKIE else f"zeta_member={ZETA_MEMBER_COOKIE}"
    response = member_session().get(
        MEMBER_API_URL,
        headers={"Cookie": cookie, "Accept": "application/json"},
        timeout=(15, 45),
    )
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"Zeta member API auth failed ({response.status_code}); refresh ZETA_MEMBER_COOKIE"
        )
    response.raise_for_status()

    payload = response.json()
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("signals"), list):
        rows = payload["signals"]
    else:
        raise RuntimeError("Unexpected Zeta member API response shape")

    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        raise RuntimeError("Zeta member API returned zero signal rows; refusing empty canonical sync")
    return rows


def parse_wib_timestamp(value: Any) -> tuple[str | None, str | None]:
    if value in (None, ""):
        return None, None
    raw = str(value).strip()
    try:
        naive = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, None
    local = naive.replace(tzinfo=WIB)
    return local.astimezone(UTC).isoformat(), local.date().isoformat()


def normalized_zeta(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("id") is None:
        raise ValueError("Zeta row has no native id")

    source_utc, market_date = parse_wib_timestamp(raw.get("timestamp"))
    resolved_utc, _ = parse_wib_timestamp(raw.get("resolved_at"))
    if not source_utc:
        raise ValueError("Zeta row has invalid/missing timestamp")

    ticker = str(raw.get("symbol") or "").strip().upper()
    if not ticker:
        raise ValueError("Zeta row has no symbol")

    return {
        "zeta_id": str(raw["id"]),
        "ticker": ticker,
        "signal_type": raw.get("decision"),
        "source_timestamp_raw": str(raw.get("timestamp") or ""),
        "source_timestamp_utc": source_utc,
        "market_date": market_date,
        "entry_price": raw.get("close_price"),
        "tp1_price": raw.get("take_profit"),
        "sl_default_price": raw.get("stop_loss"),
        "status": str(raw.get("status") or "").strip().upper(),
        "profit_pct": raw.get("profit_pct"),
        "resolved_at_raw": raw.get("resolved_at"),
        "resolved_at_utc": resolved_utc,
        "raw": raw,
    }


def create_batch() -> str:
    response = (
        sb.table("source_batches")
        .insert(
            {
                "user_id": SUPABASE_USER_ID,
                "batch_type": "LIVE_ZETA",
                "status": "COMMITTING",
                "source_label": "Zeta Member API live synchronization",
                "parser_version": PARSER_VERSION,
                "summary": {"operation": "LIVE_ZETA_SYNC", "source_interface": "MEMBER_API"},
            }
        )
        .execute()
    )
    row = first_row(response)
    if not row:
        raise RuntimeError("Failed to create LIVE_ZETA source batch")
    return str(row["id"])


def finish_batch(batch_id: str, status: str, summary: dict[str, Any]) -> None:
    sb.table("source_batches").update(
        {
            "status": status,
            "completed_at": utc_now_iso(),
            "summary": {"operation": "LIVE_ZETA_SYNC", **summary},
            "verification_result": summary,
        }
    ).eq("id", batch_id).eq("user_id", SUPABASE_USER_ID).execute()


def source_by_native_id(native_id: str) -> dict[str, Any] | None:
    return first_row(
        sb.table("source_records")
        .select("*")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_system", "ZETA")
        .eq("native_source_id", native_id)
        .limit(1)
        .execute()
    )


def preserve_source(raw: dict[str, Any], batch_id: str) -> tuple[dict[str, Any], bool]:
    """Persist source evidence first and preserve historical batch provenance.

    Same Zeta id is the same source record. Existing reconstruction rows are not
    rewritten just because their fingerprint/parser version was produced by a
    different importer. Only an actual raw-payload change constitutes source
    evolution for the live path.
    """
    if raw.get("id") is None:
        raise ValueError("Cannot preserve Zeta source without native id")

    native_id = str(raw["id"])
    existing = source_by_native_id(native_id)
    source_utc, market_date = parse_wib_timestamp(raw.get("timestamp"))
    payload = {
        "source_record_type": "SIGNAL",
        "source_timestamp_raw": str(raw.get("timestamp") or "") or None,
        "source_timezone_semantics": "KNOWN_WIB" if source_utc else "UNKNOWN",
        "source_timestamp_utc": source_utc,
        "market_date": market_date,
        "raw_payload": raw,
        "fingerprint": fingerprint_payload(raw),
        "parser_version": PARSER_VERSION,
    }

    if existing:
        changed = not payload_equal(existing.get("raw_payload"), raw)
        if not changed:
            return existing, False

        response = (
            sb.table("source_records")
            .update(payload)
            .eq("id", existing["id"])
            .eq("user_id", SUPABASE_USER_ID)
            .execute()
        )
        row = first_row(response)
        return (row or source_by_native_id(native_id) or existing), True

    response = sb.table("source_records").insert(
        {
            "user_id": SUPABASE_USER_ID,
            "batch_id": batch_id,
            "source_system": "ZETA",
            "native_source_id": native_id,
            **payload,
        }
    ).execute()
    row = first_row(response) or source_by_native_id(native_id)
    if not row:
        raise RuntimeError(f"Failed to persist Zeta source {native_id}")
    return row, True


def link_for_source(source_record_id: str) -> dict[str, Any] | None:
    return first_row(
        sb.table("signal_source_links")
        .select("signal_id,source_record_id,match_method,match_confidence,evidence")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_record_id", source_record_id)
        .limit(1)
        .execute()
    )


def assertion_for_source(source_record_id: str) -> dict[str, Any] | None:
    return first_row(
        sb.table("signal_outcome_assertions")
        .select("*")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_record_id", source_record_id)
        .limit(1)
        .execute()
    )


def active_outcome(signal_id: str) -> dict[str, Any] | None:
    return first_row(
        sb.table("signal_outcomes")
        .select("id,outcome_status,resolver_version,authority,supporting_evidence")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("signal_id", signal_id)
        .is_("superseded_at", "null")
        .limit(1)
        .execute()
    )


def reconciliation_for_source(source_record_id: str, item_type: str) -> dict[str, Any] | None:
    return first_row(
        sb.table("reconciliation_items")
        .select("*")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_record_id", source_record_id)
        .eq("item_type", item_type)
        .limit(1)
        .execute()
    )


def record_reconciliation(
    *,
    batch_id: str,
    source_record_id: str,
    item_type: str,
    status: str,
    proposed_action: str,
    conflict_type: str,
    evidence: dict[str, Any],
    match_confidence: str | None = None,
) -> None:
    existing = reconciliation_for_source(source_record_id, item_type)
    values = {
        "status": status,
        "match_confidence": match_confidence,
        "proposed_action": proposed_action,
        "conflict_type": conflict_type,
        "evidence": evidence,
        "review_note": None,
        "reviewed_at": None,
    }
    if existing:
        sb.table("reconciliation_items").update(values).eq("id", existing["id"]).execute()
        return

    sb.table("reconciliation_items").insert(
        {
            "user_id": SUPABASE_USER_ID,
            "batch_id": batch_id,
            "source_record_id": source_record_id,
            "item_type": item_type,
            **values,
        }
    ).execute()


def resolve_reconciliation(source_record_id: str, item_types: list[str], note: str) -> None:
    if not item_types:
        return
    response = (
        sb.table("reconciliation_items")
        .select("id,status")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_record_id", source_record_id)
        .in_("item_type", item_types)
        .execute()
    )
    for row in response.data or []:
        if row.get("status") == "ACCEPTED":
            continue
        sb.table("reconciliation_items").update(
            {"status": "ACCEPTED", "review_note": note, "reviewed_at": utc_now_iso()}
        ).eq("id", row["id"]).execute()


def derivation_complete(source: dict[str, Any], parsed: dict[str, Any]) -> bool:
    source_id = str(source["id"])
    link = link_for_source(source_id)
    if not link:
        return reconciliation_for_source(source_id, "AMBIGUOUS_ZETA_SIGNAL") is not None
    if not is_final_zeta_status(parsed.get("status")):
        return True
    assertion = assertion_for_source(source_id)
    if not assertion:
        return False
    return active_outcome(str(link["signal_id"])) is not None


def telegram_candidates(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only canonical candidates backed by Telegram SIGNAL provenance."""
    ts = datetime.fromisoformat(parsed["source_timestamp_utc"])
    start = (ts - timedelta(seconds=300)).isoformat()
    end = (ts + timedelta(seconds=300)).isoformat()

    response = (
        sb.table("signals")
        .select("id,ticker,signal_timestamp,entry_price,tp1_price,sl_default_price")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("ticker", parsed["ticker"])
        .gte("signal_timestamp", start)
        .lte("signal_timestamp", end)
        .execute()
    )
    signals = response.data or []
    if not signals:
        return []

    signal_ids = [row["id"] for row in signals]
    response = (
        sb.table("signal_source_links")
        .select("signal_id,source_record_id")
        .eq("user_id", SUPABASE_USER_ID)
        .in_("signal_id", signal_ids)
        .execute()
    )
    links = response.data or []
    if not links:
        return []

    source_ids = [row["source_record_id"] for row in links]
    response = (
        sb.table("source_records")
        .select("id,native_source_id,source_timestamp_utc")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_system", "TELEGRAM")
        .eq("source_record_type", "SIGNAL")
        .in_("id", source_ids)
        .execute()
    )
    tg_by_id = {row["id"]: row for row in (response.data or [])}

    tg_by_signal: dict[str, dict[str, Any]] = {}
    for link in links:
        tg = tg_by_id.get(link["source_record_id"])
        if tg:
            tg_by_signal[str(link["signal_id"])] = tg

    candidates: list[dict[str, Any]] = []
    for signal in signals:
        tg = tg_by_signal.get(str(signal["id"]))
        if tg:
            candidates.append(
                {
                    **signal,
                    "telegram_source_record_id": tg["id"],
                    "telegram_msg_id": tg["native_source_id"],
                }
            )
    return candidates


def zeta_signal_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": parsed["ticker"],
        "signal_type": parsed.get("signal_type"),
        "signal_timestamp": parsed["source_timestamp_utc"],
        "market_date": parsed["market_date"],
        "entry_price": parsed.get("entry_price"),
        "tp1_price": parsed.get("tp1_price"),
        "tp1_pct": None,
        "tp2_price": None,
        "tp2_pct": None,
        "sl_default_price": parsed.get("sl_default_price"),
        "sl_default_pct": None,
        "sl_moderat_price": None,
        "sl_moderat_pct": None,
        "sl_konservatif_price": None,
        "sl_konservatif_pct": None,
        "confidence_score": None,
        "confidence_label": None,
        "detail": {},
    }


def ensure_zeta_signal(source: dict[str, Any], parsed: dict[str, Any], batch_id: str) -> str | None:
    source_id = str(source["id"])
    existing = link_for_source(source_id)
    if existing:
        response = sb.rpc(
            "ensure_zeta_publication",
            {
                "p_user_id": SUPABASE_USER_ID,
                "p_source_record_id": source["id"],
                "p_signal": zeta_signal_payload(parsed),
            },
        ).execute()
        resolve_reconciliation(
            source_id,
            ["AMBIGUOUS_ZETA_SIGNAL", "UNPARSED_SOURCE"],
            "Zeta source successfully linked",
        )
        return str(response.data)

    match = match_zeta_to_telegram(
        {
            "source_timestamp_utc": parsed["source_timestamp_utc"],
            "entry_price": parsed.get("entry_price"),
            "tp1_price": parsed.get("tp1_price"),
            "sl_default_price": parsed.get("sl_default_price"),
        },
        telegram_candidates(parsed),
    )

    if match.result == "AMBIGUOUS":
        record_reconciliation(
            batch_id=batch_id,
            source_record_id=source_id,
            item_type="AMBIGUOUS_ZETA_SIGNAL",
            status="REVIEW_REQUIRED",
            match_confidence="AMBIGUOUS",
            proposed_action="REVIEW",
            conflict_type="MULTIPLE_TELEGRAM_CANDIDATES",
            evidence={
                "source_system": "ZETA",
                "native_source_id": parsed["zeta_id"],
                "parser_version": PARSER_VERSION,
                **(match.evidence or {}),
            },
        )
        return None

    if match.result == "MATCHED" and match.signal_id:
        sb.table("signal_source_links").insert(
            {
                "user_id": SUPABASE_USER_ID,
                "signal_id": match.signal_id,
                "source_record_id": source["id"],
                "match_method": "TELEGRAM_ZETA_RECONCILIATION",
                "match_confidence": "HIGH_CONFIDENCE",
                "review_status": "AUTO_ACCEPTED",
                "evidence": {
                    "zeta_id": parsed["zeta_id"],
                    "telegram_msg_id": match.telegram_msg_id,
                    **(match.evidence or {}),
                },
            }
        ).execute()
        resolve_reconciliation(
            source_id,
            ["AMBIGUOUS_ZETA_SIGNAL", "UNPARSED_SOURCE"],
            "Zeta source matched Telegram publication",
        )
        return match.signal_id

    response = sb.rpc(
        "ensure_zeta_publication",
        {
            "p_user_id": SUPABASE_USER_ID,
            "p_source_record_id": source["id"],
            "p_signal": zeta_signal_payload(parsed),
        },
    ).execute()
    resolve_reconciliation(
        source_id,
        ["AMBIGUOUS_ZETA_SIGNAL", "UNPARSED_SOURCE"],
        "ZETA_ONLY publication created",
    )
    return str(response.data)


def upsert_assertion(source: dict[str, Any], signal_id: str, parsed: dict[str, Any], batch_id: str) -> bool:
    source_id = str(source["id"])
    existing = assertion_for_source(source_id)

    if not is_final_zeta_status(parsed.get("status")):
        if existing:
            record_reconciliation(
                batch_id=batch_id,
                source_record_id=source_id,
                item_type="ZETA_STATUS_REGRESSION",
                status="QUARANTINED",
                proposed_action="REVIEW",
                conflict_type="FINAL_TO_NONFINAL",
                evidence={
                    "source_system": "ZETA",
                    "native_source_id": parsed["zeta_id"],
                    "current_status": parsed.get("status"),
                    "existing_assertion_status": existing.get("outcome_status"),
                },
            )
        return False

    assertion = build_assertion_payload(
        user_id=SUPABASE_USER_ID,
        signal_id=signal_id,
        source_record_id=source_id,
        source=parsed,
    )

    changed = True
    if existing:
        changed = not assertion_semantically_equal(existing, assertion)
        if changed:
            sb.table("signal_outcome_assertions").update(
                {
                    "signal_id": assertion["signal_id"],
                    "outcome_status": assertion["outcome_status"],
                    "profit_pct": assertion["profit_pct"],
                    "source_resolved_at": assertion["source_resolved_at"],
                    "assertion_data": assertion["assertion_data"],
                }
            ).eq("id", existing["id"]).execute()
    else:
        sb.table("signal_outcome_assertions").insert(assertion).execute()

    sb.rpc(
        "resolve_zeta_outcome",
        {"p_user_id": SUPABASE_USER_ID, "p_signal_id": signal_id},
    ).execute()
    resolve_reconciliation(source_id, ["ZETA_STATUS_REGRESSION"], "Final Zeta assertion restored")
    return changed


def process_source(raw: dict[str, Any], batch_id: str) -> dict[str, Any]:
    source, changed = preserve_source(raw, batch_id)

    try:
        parsed = normalized_zeta(raw)
    except Exception as exc:
        record_reconciliation(
            batch_id=batch_id,
            source_record_id=str(source["id"]),
            item_type="UNPARSED_SOURCE",
            status="QUARANTINED",
            proposed_action="REPROCESS",
            conflict_type="PARSE_FAILED",
            evidence={
                "source_system": "ZETA",
                "native_source_id": str(raw.get("id")),
                "parser_version": PARSER_VERSION,
                "error_code": type(exc).__name__,
                "error_message": str(exc),
                "source_record_type_candidate": "SIGNAL",
            },
        )
        return {"result": "UNPARSED", "changed": changed, "final": False}

    if not changed and derivation_complete(source, parsed):
        return {
            "result": "UNCHANGED",
            "changed": False,
            "final": is_final_zeta_status(parsed.get("status")),
        }

    signal_id = ensure_zeta_signal(source, parsed, batch_id)
    if not signal_id:
        return {"result": "AMBIGUOUS", "changed": changed, "final": False}

    assertion_changed = upsert_assertion(source, signal_id, parsed, batch_id)
    return {
        "result": "SYNCED",
        "changed": changed,
        "signal_id": signal_id,
        "final": is_final_zeta_status(parsed.get("status")),
        "assertion_changed": assertion_changed,
    }


def main() -> None:
    batch_id = create_batch()
    stats = {
        "api_rows": 0,
        "synced": 0,
        "unchanged": 0,
        "unparsed": 0,
        "ambiguous": 0,
        "final_rows": 0,
        "assertions_changed": 0,
        "member_api_only": True,
        "resolver_version": RESOLVER_VERSION,
    }

    try:
        rows = fetch_member_signals()
        stats["api_rows"] = len(rows)

        for raw in rows:
            result = process_source(raw, batch_id)
            if result.get("final"):
                stats["final_rows"] += 1
            if result["result"] == "SYNCED":
                stats["synced"] += 1
            elif result["result"] == "UNCHANGED":
                stats["unchanged"] += 1
            elif result["result"] == "UNPARSED":
                stats["unparsed"] += 1
            elif result["result"] == "AMBIGUOUS":
                stats["ambiguous"] += 1
            if result.get("assertion_changed"):
                stats["assertions_changed"] += 1

        final_status = (
            "PARTIAL_REVIEW_REQUIRED"
            if stats["unparsed"] or stats["ambiguous"]
            else "VERIFIED"
        )
        finish_batch(batch_id, final_status, stats)
        log.info("Zeta S4 complete: %s", stats)
    except Exception as exc:
        stats["error"] = str(exc)
        try:
            finish_batch(batch_id, "FAILED", stats)
        finally:
            log.exception("Zeta S4 failed")
        raise


if __name__ == "__main__":
    main()
