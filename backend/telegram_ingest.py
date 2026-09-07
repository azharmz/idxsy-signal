#!/usr/bin/env python3
"""IDXSY Signal S3 — canonical Telegram live ingestion.

Architecture:
    Telegram -> source_records -> SIGNAL/EVENT interpretation -> canonical DB

Critical invariants:
* raw source evidence is durably preserved before parsing/canonicalization;
* Telegram message.id is native source identity, never canonical signal identity;
* SIGNAL publication creates signals + signal_source_links atomically via RPC;
* Telegram result/profit messages become signal_events, never signal_outcomes;
* unmatched/ambiguous/parser-failed sources go to reconciliation_items;
* cursor means "last Telegram message durably preserved", not "last parsed";
* no trades_data dependency.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from supabase import Client, create_client
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from telegram_parser import PARSER_VERSION, ParseError, classify, parse_pct, parse_result, parse_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("telegram-s3")

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION_STRING = os.environ["TG_SESSION_STRING"]
TG_GROUP_ID_RAW = os.environ["TG_GROUP_ID"]
TG_GROUP_ID: str | int = int(TG_GROUP_ID_RAW) if TG_GROUP_ID_RAW.lstrip("-").isdigit() else TG_GROUP_ID_RAW
TG_TOPIC_ID = int(os.environ["TG_TOPIC_ID"]) if os.environ.get("TG_TOPIC_ID") else None

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_USER_ID = os.environ["SUPABASE_USER_ID"]

BACKFILL_SINCE_DAYS = int(os.environ["BACKFILL_SINCE_DAYS"]) if os.environ.get("BACKFILL_SINCE_DAYS") else None
EDIT_RESCAN_DAYS = int(os.environ.get("EDIT_RESCAN_DAYS", "7"))
CURSOR_SOURCE = os.environ.get("INGEST_CURSOR_SOURCE", "telegram_group")
ENTRY_MATCH_TOLERANCE_PCT = float(os.environ.get("ENTRY_MATCH_TOLERANCE_PCT", "0.05"))

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def idx_market_date(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("Asia/Jakarta")).date().isoformat()


def json_safe(value: Any) -> Any:
    """Best-effort JSON-safe conversion for Telethon source payloads."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def fingerprint(raw_text: str, raw_payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"raw_text": raw_text, "raw_payload": raw_payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_batch() -> str:
    row = {
        "user_id": SUPABASE_USER_ID,
        "batch_type": "LIVE_TELEGRAM",
        "status": "CREATED",
        "source_label": "Telegram live ingestion",
        "parser_version": PARSER_VERSION,
        "summary": {},
    }
    data = sb.table("source_batches").insert(row).execute().data or []
    if not data:
        raise RuntimeError("source_batches insert returned no row")
    batch_id = data[0]["id"]
    sb.table("source_batches").update({"status": "COMMITTING"}).eq("id", batch_id).execute()
    return batch_id


def finish_batch(batch_id: str, status: str, summary: dict[str, Any]) -> None:
    sb.table("source_batches").update(
        {
            "status": status,
            "summary": summary,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", batch_id).execute()


def get_cursor() -> int:
    data = (
        sb.table("ingest_cursor")
        .select("last_processed_msg_id")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source", CURSOR_SOURCE)
        .limit(1)
        .execute()
        .data
        or []
    )
    return int(data[0]["last_processed_msg_id"] or 0) if data else 0


def set_cursor(message_id: int) -> None:
    sb.table("ingest_cursor").upsert(
        {
            "user_id": SUPABASE_USER_ID,
            "source": CURSOR_SOURCE,
            "last_processed_msg_id": message_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_id,source",
    ).execute()


def source_lookup(native_source_id: str) -> dict[str, Any] | None:
    data = (
        sb.table("source_records")
        .select("*")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_system", "TELEGRAM")
        .eq("native_source_id", native_source_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return data[0] if data else None


def preserve_source(message: Any, batch_id: str) -> tuple[dict[str, Any], bool]:
    """Persist Telegram evidence before interpretation.

    Returns (source_record, content_changed). Existing historical source rows keep
    their original batch_id; an edit updates the same source identity.
    """
    native_id = str(message.id)
    raw_text = message.raw_text or ""
    payload = json_safe(message.to_dict())
    fp = fingerprint(raw_text, payload)
    existing = source_lookup(native_id)

    common = {
        "source_timestamp_raw": message.date.isoformat(),
        "source_timezone_semantics": "KNOWN_UTC",
        "source_timestamp_utc": utc_iso(message.date),
        "market_date": idx_market_date(message.date),
        "raw_payload": payload,
        "raw_text": raw_text,
        "fingerprint": fp,
        "parser_version": PARSER_VERSION,
    }

    if existing:
        changed = existing.get("fingerprint") != fp
        if changed or existing.get("parser_version") != PARSER_VERSION:
            sb.table("source_records").update(common).eq("id", existing["id"]).execute()
            existing.update(common)
        return existing, changed

    row = {
        "user_id": SUPABASE_USER_ID,
        "batch_id": batch_id,
        "source_system": "TELEGRAM",
        "native_source_id": native_id,
        "source_record_type": None,
        **common,
    }
    data = sb.table("source_records").insert(row).execute().data or []
    if not data:
        # A concurrent retry may have won the native-identity race.
        existing = source_lookup(native_id)
        if existing:
            return existing, existing.get("fingerprint") != fp
        raise RuntimeError(f"source_records insert returned no row for Telegram {native_id}")
    return data[0], True


def update_source_type(source_record_id: str, record_type: str | None) -> None:
    sb.table("source_records").update(
        {"source_record_type": record_type, "parser_version": PARSER_VERSION}
    ).eq("id", source_record_id).execute()


def reconciliation_lookup(source_record_id: str, item_type: str) -> dict[str, Any] | None:
    data = (
        sb.table("reconciliation_items")
        .select("*")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_record_id", source_record_id)
        .eq("item_type", item_type)
        .in_("status", ["QUARANTINED", "REVIEW_REQUIRED", "CONFLICTED"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return data[0] if data else None


def upsert_reconciliation(
    *,
    batch_id: str,
    source_record_id: str,
    item_type: str,
    status: str,
    match_confidence: str | None,
    proposed_action: str,
    conflict_type: str,
    evidence: dict[str, Any],
    candidate_signal_id: str | None = None,
) -> None:
    row = {
        "user_id": SUPABASE_USER_ID,
        "batch_id": batch_id,
        "source_record_id": source_record_id,
        "candidate_signal_id": candidate_signal_id,
        "item_type": item_type,
        "status": status,
        "match_confidence": match_confidence,
        "proposed_action": proposed_action,
        "conflict_type": conflict_type,
        "evidence": evidence,
    }
    existing = reconciliation_lookup(source_record_id, item_type)
    if existing:
        sb.table("reconciliation_items").update(row).eq("id", existing["id"]).execute()
    else:
        sb.table("reconciliation_items").insert(row).execute()


def record_parse_failure(
    batch_id: str,
    source: dict[str, Any],
    error: Exception,
    candidate_type: str | None,
) -> None:
    upsert_reconciliation(
        batch_id=batch_id,
        source_record_id=source["id"],
        item_type="UNPARSED_SOURCE",
        status="QUARANTINED",
        match_confidence=None,
        proposed_action="REPROCESS",
        conflict_type="PARSE_FAILED",
        evidence={
            "source_system": "TELEGRAM",
            "native_source_id": source["native_source_id"],
            "parser_version": PARSER_VERSION,
            "error_code": error.__class__.__name__,
            "error_message": str(error),
            "source_record_type_candidate": candidate_type.upper() if candidate_type else None,
        },
    )


def signal_detail(parsed: dict[str, Any]) -> dict[str, Any]:
    relational = {
        "msg_id",
        "date",
        "type",
        "symbol",
        "signal_type",
        "entry_price",
        "take_profit",
        "take_profit_pct",
        "target2_price",
        "target2_pct",
        "stop_loss",
        "stop_loss_pct",
        "sl_moderat",
        "sl_moderat_pct",
        "sl_konservatif",
        "sl_konservatif_pct",
        "confidence_score",
        "confidence_label",
    }
    return {key: value for key, value in parsed.items() if key not in relational and value is not None}


def publication_payload(parsed: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": parsed["symbol"],
        "signal_type": parsed.get("signal_type"),
        "signal_timestamp": source["source_timestamp_utc"],
        "market_date": source["market_date"],
        "entry_price": parsed.get("entry_price"),
        "tp1_price": parsed.get("take_profit"),
        "tp1_pct": parse_pct(parsed.get("take_profit_pct")),
        "tp2_price": parsed.get("target2_price"),
        "tp2_pct": parse_pct(parsed.get("target2_pct")),
        "sl_default_price": parsed.get("stop_loss"),
        "sl_default_pct": parse_pct(parsed.get("stop_loss_pct")),
        "sl_moderat_price": parsed.get("sl_moderat"),
        "sl_moderat_pct": parsed.get("sl_moderat_pct"),
        "sl_konservatif_price": parsed.get("sl_konservatif"),
        "sl_konservatif_pct": parsed.get("sl_konservatif_pct"),
        "confidence_score": parsed.get("confidence_score"),
        "confidence_label": parsed.get("confidence_label"),
        "detail": signal_detail(parsed),
    }


def ensure_publication(source: dict[str, Any], parsed: dict[str, Any]) -> str:
    result = sb.rpc(
        "ensure_telegram_publication",
        {
            "p_user_id": SUPABASE_USER_ID,
            "p_source_record_id": source["id"],
            "p_signal": publication_payload(parsed, source),
        },
    ).execute().data
    if isinstance(result, str):
        return result
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("signal_id") or first.get("ensure_telegram_publication")
    if isinstance(result, dict):
        return result.get("signal_id") or result.get("ensure_telegram_publication")
    raise RuntimeError(f"ensure_telegram_publication returned unexpected result: {result!r}")


def event_candidates(parsed: dict[str, Any], event_timestamp: str) -> list[dict[str, Any]]:
    ticker = parsed["symbol"]
    data = (
        sb.table("signals")
        .select("id,ticker,signal_timestamp,entry_price")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("ticker", ticker)
        .lte("signal_timestamp", event_timestamp)
        .order("signal_timestamp", desc=True)
        .limit(25)
        .execute()
        .data
        or []
    )
    if not data:
        return []

    # Legacy-compatible event window: the event belongs to the most recent
    # publication interval for this ticker. If several publications share that
    # effective interval/timestamp, preserve ambiguity rather than choosing one.
    latest_ts = data[0]["signal_timestamp"]
    same_window = [row for row in data if row["signal_timestamp"] == latest_ts]

    event_entry = parsed.get("entry")
    if event_entry is None:
        return same_window

    candidates: list[dict[str, Any]] = []
    for row in same_window:
        signal_entry = row.get("entry_price")
        if signal_entry in (None, 0):
            candidates.append(row)
            continue
        diff_pct = abs(float(event_entry) - float(signal_entry)) / float(signal_entry) * 100
        if diff_pct <= ENTRY_MATCH_TOLERANCE_PCT:
            candidates.append(row)
    return candidates


def existing_event(source_record_id: str) -> dict[str, Any] | None:
    data = (
        sb.table("signal_events")
        .select("*")
        .eq("source_record_id", source_record_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return data[0] if data else None


def event_data(parsed: dict[str, Any], match_evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "status_text": parsed.get("status_text"),
            "entry": parsed.get("entry"),
            "exit_price": parsed.get("exit_price"),
            "day_high": parsed.get("day_high"),
            "peak_price": parsed.get("peak_price"),
            "peak_pct": parsed.get("peak_pct"),
            "profit_pct": parsed.get("profit_pct"),
            "duration_days_confirm": parsed.get("duration_days_confirm"),
            "reconciliation": match_evidence,
        }.items()
        if value is not None
    }


def process_event(batch_id: str, source: dict[str, Any], parsed: dict[str, Any]) -> str:
    candidates = event_candidates(parsed, source["source_timestamp_utc"])
    evidence = {
        "method": "TELEGRAM_TICKER_LATEST_INTERVAL_ENTRY_TOLERANCE",
        "ticker": parsed["symbol"],
        "event_timestamp": source["source_timestamp_utc"],
        "entry_tolerance_pct": ENTRY_MATCH_TOLERANCE_PCT,
        "candidate_signal_ids": [row["id"] for row in candidates],
    }

    if not candidates:
        upsert_reconciliation(
            batch_id=batch_id,
            source_record_id=source["id"],
            item_type="UNMATCHED_EVENT",
            status="QUARANTINED",
            match_confidence=None,
            proposed_action="QUARANTINE",
            conflict_type="NO_DEFENSIBLE_SIGNAL_CANDIDATE",
            evidence=evidence,
        )
        return "UNMATCHED"

    if len(candidates) > 1:
        upsert_reconciliation(
            batch_id=batch_id,
            source_record_id=source["id"],
            item_type="AMBIGUOUS_EVENT",
            status="REVIEW_REQUIRED",
            match_confidence="AMBIGUOUS",
            proposed_action="REVIEW",
            conflict_type="MULTIPLE_SIGNAL_CANDIDATES",
            evidence=evidence,
        )
        return "AMBIGUOUS"

    signal_id = candidates[0]["id"]
    payload = {
        "user_id": SUPABASE_USER_ID,
        "signal_id": signal_id,
        "source_record_id": source["id"],
        "event_type": parsed["type"],
        "event_timestamp": source["source_timestamp_utc"],
        "event_data": event_data(parsed, {**evidence, "match_confidence": "HIGH_CONFIDENCE"}),
    }
    event = existing_event(source["id"])
    if event:
        sb.table("signal_events").update(payload).eq("id", event["id"]).execute()
    else:
        sb.table("signal_events").insert(payload).execute()
    return "MATCHED"


def process_preserved_source(batch_id: str, source: dict[str, Any]) -> str:
    text = source.get("raw_text") or ""
    category = classify(text)
    candidate_type = "EVENT" if category == "event" else category.upper() if category in ("signal", "regime") else None

    try:
        if category == "signal":
            parsed = parse_signal(text, int(source["native_source_id"]), source["source_timestamp_utc"])
            update_source_type(source["id"], "SIGNAL")
            ensure_publication(source, parsed)
            return "SIGNAL"

        if category == "event":
            parsed = parse_result(text, int(source["native_source_id"]), source["source_timestamp_utc"])
            update_source_type(source["id"], "EVENT")
            return process_event(batch_id, source, parsed)

        # Regime/other are preserved source evidence but are not canonical Signal
        # publication/event rows in S3.
        update_source_type(source["id"], None)
        return category.upper()

    except Exception as exc:
        # Source evidence is already durable; parser/canonical interpretation remains retryable.
        if isinstance(exc, ParseError):
            update_source_type(source["id"], None)
        record_parse_failure(batch_id, source, exc, candidate_type)
        raise


def fetch_messages(client: TelegramClient, cursor: int) -> list[Any]:
    kwargs: dict[str, Any] = {"reverse": True}
    if TG_TOPIC_ID is not None:
        kwargs["reply_to"] = TG_TOPIC_ID

    if BACKFILL_SINCE_DAYS is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=BACKFILL_SINCE_DAYS)
        kwargs["offset_date"] = cutoff
        messages = list(client.iter_messages(TG_GROUP_ID, **kwargs))
    else:
        kwargs["min_id"] = cursor
        messages = list(client.iter_messages(TG_GROUP_ID, **kwargs))

        # Cursor cannot discover edits to old IDs. Re-scan a short recent window and
        # let fingerprint/native identity decide whether any existing source evolved.
        if EDIT_RESCAN_DAYS > 0:
            recent_kwargs: dict[str, Any] = {
                "reverse": True,
                "offset_date": datetime.now(timezone.utc) - timedelta(days=EDIT_RESCAN_DAYS),
            }
            if TG_TOPIC_ID is not None:
                recent_kwargs["reply_to"] = TG_TOPIC_ID
            recent = list(client.iter_messages(TG_GROUP_ID, **recent_kwargs))
            by_id = {message.id: message for message in recent}
            by_id.update({message.id: message for message in messages})
            messages = sorted(by_id.values(), key=lambda message: message.id)

    return messages


def main() -> int:
    batch_id = create_batch()
    cursor = get_cursor()
    safe_frontier = cursor
    summary = {
        "fetched": 0,
        "source_new_or_changed": 0,
        "signals": 0,
        "events_matched": 0,
        "events_unmatched": 0,
        "events_ambiguous": 0,
        "unparsed_source": 0,
        "other": 0,
        "regime": 0,
    }
    review_required = False

    try:
        with TelegramClient(StringSession(TG_SESSION_STRING), TG_API_ID, TG_API_HASH) as client:
            client.get_dialogs()
            messages = fetch_messages(client, cursor)

        summary["fetched"] = len(messages)
        log.info("Fetched %s Telegram messages (cursor=%s)", len(messages), cursor)

        for message in messages:
            # Empty/service messages still need a durable source record before the cursor
            # can move beyond their native ID.
            try:
                source, changed = preserve_source(message, batch_id)
            except Exception:
                log.exception("Source persistence failed for Telegram msg %s", message.id)
                raise

            # Cursor may move after durable source preservation, never before.
            if message.id > safe_frontier:
                safe_frontier = message.id
                set_cursor(safe_frontier)

            if changed:
                summary["source_new_or_changed"] += 1

            # Unchanged re-scan rows can still be retried if canonical derivation is incomplete;
            # processing is intentionally idempotent.
            try:
                result = process_preserved_source(batch_id, source)
            except Exception as exc:
                summary["unparsed_source"] += 1
                review_required = True
                log.error("Canonical processing failed for Telegram msg %s: %s", message.id, exc)
                continue

            if result == "SIGNAL":
                summary["signals"] += 1
            elif result == "MATCHED":
                summary["events_matched"] += 1
            elif result == "UNMATCHED":
                summary["events_unmatched"] += 1
                review_required = True
            elif result == "AMBIGUOUS":
                summary["events_ambiguous"] += 1
                review_required = True
            elif result == "REGIME":
                summary["regime"] += 1
            else:
                summary["other"] += 1

        final_status = "PARTIAL_REVIEW_REQUIRED" if review_required else "VERIFIED"
        finish_batch(batch_id, final_status, summary)
        log.info("S3 Telegram ingestion complete: %s", summary)
        return 0 if not review_required else 1

    except Exception as exc:
        summary["fatal_error"] = f"{exc.__class__.__name__}: {exc}"
        try:
            finish_batch(batch_id, "FAILED", summary)
        except Exception:
            log.exception("Failed to mark source batch %s FAILED", batch_id)
        return 1


if __name__ == "__main__":
    sys.exit(main())
