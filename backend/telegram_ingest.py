#!/usr/bin/env python3
"""IDXSY Signal S3 — canonical Telegram live ingestion.

Telegram -> source_records -> parse/classify -> signals / signal_events /
reconciliation_items -> ingest_cursor.

Critical invariants:
* preserve source evidence before interpretation;
* Telegram message.id is native source identity, never signals.id;
* canonical publication + provenance link is atomic via RPC;
* Telegram result/profit messages are events, not outcomes;
* UNPARSED_SOURCE is only for parser failures, never DB/RPC failures;
* cursor advances only after durable source preservation;
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
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def content_fingerprint(raw_text: str, raw_payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"raw_text": raw_text, "raw_payload": raw_payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_batch() -> str:
    rows = sb.table("source_batches").insert(
        {
            "user_id": SUPABASE_USER_ID,
            "batch_type": "LIVE_TELEGRAM",
            "status": "CREATED",
            "source_label": "Telegram live ingestion",
            "parser_version": PARSER_VERSION,
            "summary": {},
        }
    ).execute().data or []
    if not rows:
        raise RuntimeError("source_batches insert returned no row")
    batch_id = rows[0]["id"]
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
    rows = (
        sb.table("ingest_cursor")
        .select("last_processed_msg_id")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source", CURSOR_SOURCE)
        .limit(1)
        .execute().data or []
    )
    return int(rows[0]["last_processed_msg_id"] or 0) if rows else 0


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
    rows = (
        sb.table("source_records").select("*")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_system", "TELEGRAM")
        .eq("native_source_id", native_source_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


def preserve_source(message: Any, batch_id: str) -> tuple[dict[str, Any], bool]:
    """Persist evidence before parsing. Existing sources retain original batch_id."""
    native_id = str(message.id)
    raw_text = message.raw_text or ""
    raw_payload = json_safe(message.to_dict())
    fingerprint = content_fingerprint(raw_text, raw_payload)
    existing = source_lookup(native_id)
    mutable = {
        "source_timestamp_raw": message.date.isoformat(),
        "source_timezone_semantics": "KNOWN_UTC",
        "source_timestamp_utc": utc_iso(message.date),
        "market_date": idx_market_date(message.date),
        "raw_payload": raw_payload,
        "raw_text": raw_text,
        "fingerprint": fingerprint,
        "parser_version": PARSER_VERSION,
    }

    if existing:
        changed = existing.get("fingerprint") != fingerprint
        if changed or existing.get("parser_version") != PARSER_VERSION:
            sb.table("source_records").update(mutable).eq("id", existing["id"]).execute()
            existing.update(mutable)
        return existing, changed

    row = {
        "user_id": SUPABASE_USER_ID,
        "batch_id": batch_id,
        "source_system": "TELEGRAM",
        "native_source_id": native_id,
        "source_record_type": None,
        **mutable,
    }
    rows = sb.table("source_records").insert(row).execute().data or []
    if rows:
        return rows[0], True

    # Defensive native-identity race recovery.
    raced = source_lookup(native_id)
    if raced:
        return raced, raced.get("fingerprint") != fingerprint
    raise RuntimeError(f"source_records insert returned no row for Telegram {native_id}")


def update_source_type(source_record_id: str, record_type: str | None) -> None:
    sb.table("source_records").update(
        {"source_record_type": record_type, "parser_version": PARSER_VERSION}
    ).eq("id", source_record_id).execute()


def reconciliation_lookup(source_record_id: str, item_type: str) -> dict[str, Any] | None:
    rows = (
        sb.table("reconciliation_items").select("*")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("source_record_id", source_record_id)
        .eq("item_type", item_type)
        .in_("status", ["QUARANTINED", "REVIEW_REQUIRED", "CONFLICTED"])
        .order("created_at", desc=True).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def upsert_reconciliation(
    *, batch_id: str, source_record_id: str, item_type: str, status: str,
    match_confidence: str | None, proposed_action: str, conflict_type: str,
    evidence: dict[str, Any], candidate_signal_id: str | None = None,
) -> None:
    payload = {
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
        sb.table("reconciliation_items").update(payload).eq("id", existing["id"]).execute()
    else:
        sb.table("reconciliation_items").insert(payload).execute()


def record_parse_failure(batch_id: str, source: dict[str, Any], error: Exception, candidate_type: str | None) -> None:
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
            "source_record_type_candidate": candidate_type,
        },
    )


def signal_detail(parsed: dict[str, Any]) -> dict[str, Any]:
    relational = {
        "msg_id", "date", "type", "symbol", "signal_type", "entry_price",
        "take_profit", "take_profit_pct", "target2_price", "target2_pct",
        "stop_loss", "stop_loss_pct", "sl_moderat", "sl_moderat_pct",
        "sl_konservatif", "sl_konservatif_pct", "confidence_score", "confidence_label",
    }
    return {k: v for k, v in parsed.items() if k not in relational and v is not None}


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
            value = first.get("signal_id") or first.get("ensure_telegram_publication")
            if value:
                return value
    if isinstance(result, dict):
        value = result.get("signal_id") or result.get("ensure_telegram_publication")
        if value:
            return value
    raise RuntimeError(f"ensure_telegram_publication returned unexpected result: {result!r}")


def event_candidates(parsed: dict[str, Any], event_timestamp: str) -> list[dict[str, Any]]:
    rows = (
        sb.table("signals")
        .select("id,ticker,signal_timestamp,entry_price")
        .eq("user_id", SUPABASE_USER_ID)
        .eq("ticker", parsed["symbol"])
        .lte("signal_timestamp", event_timestamp)
        .order("signal_timestamp", desc=True)
        .limit(25).execute().data or []
    )
    if not rows:
        return []

    latest_timestamp = rows[0]["signal_timestamp"]
    candidates = [row for row in rows if row["signal_timestamp"] == latest_timestamp]
    event_entry = parsed.get("entry")
    if event_entry is None:
        return candidates

    filtered: list[dict[str, Any]] = []
    for row in candidates:
        signal_entry = row.get("entry_price")
        if signal_entry in (None, 0):
            filtered.append(row)
            continue
        difference = abs(float(event_entry) - float(signal_entry)) / float(signal_entry) * 100
        if difference <= ENTRY_MATCH_TOLERANCE_PCT:
            filtered.append(row)
    return filtered


def existing_event(source_record_id: str) -> dict[str, Any] | None:
    rows = (
        sb.table("signal_events").select("*")
        .eq("source_record_id", source_record_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


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
            batch_id=batch_id, source_record_id=source["id"],
            item_type="UNMATCHED_EVENT", status="QUARANTINED", match_confidence=None,
            proposed_action="QUARANTINE", conflict_type="NO_DEFENSIBLE_SIGNAL_CANDIDATE",
            evidence=evidence,
        )
        return "UNMATCHED"

    if len(candidates) > 1:
        upsert_reconciliation(
            batch_id=batch_id, source_record_id=source["id"],
            item_type="AMBIGUOUS_EVENT", status="REVIEW_REQUIRED", match_confidence="AMBIGUOUS",
            proposed_action="REVIEW", conflict_type="MULTIPLE_SIGNAL_CANDIDATES",
            evidence=evidence,
        )
        return "AMBIGUOUS"

    signal_id = candidates[0]["id"]
    event_specific = {
        key: value for key, value in {
            "status_text": parsed.get("status_text"),
            "entry": parsed.get("entry"),
            "exit_price": parsed.get("exit_price"),
            "day_high": parsed.get("day_high"),
            "peak_price": parsed.get("peak_price"),
            "peak_pct": parsed.get("peak_pct"),
            "profit_pct": parsed.get("profit_pct"),
            "duration_days_confirm": parsed.get("duration_days_confirm"),
            "reconciliation": {**evidence, "match_confidence": "HIGH_CONFIDENCE"},
        }.items() if value is not None
    }
    payload = {
        "user_id": SUPABASE_USER_ID,
        "signal_id": signal_id,
        "source_record_id": source["id"],
        "event_type": parsed["type"],
        "event_timestamp": source["source_timestamp_utc"],
        "event_data": event_specific,
    }
    existing = existing_event(source["id"])
    if existing:
        sb.table("signal_events").update(payload).eq("id", existing["id"]).execute()
    else:
        sb.table("signal_events").insert(payload).execute()
    return "MATCHED"


def process_preserved_source(batch_id: str, source: dict[str, Any]) -> str:
    text = source.get("raw_text") or ""
    category = classify(text)

    if category == "signal":
        try:
            parsed = parse_signal(text, int(source["native_source_id"]), source["source_timestamp_utc"])
        except ParseError as exc:
            update_source_type(source["id"], None)
            record_parse_failure(batch_id, source, exc, "SIGNAL")
            return "UNPARSED"
        update_source_type(source["id"], "SIGNAL")
        ensure_publication(source, parsed)  # technical/RPC failures must propagate
        return "SIGNAL"

    if category == "event":
        try:
            parsed = parse_result(text, int(source["native_source_id"]), source["source_timestamp_utc"])
        except ParseError as exc:
            update_source_type(source["id"], None)
            record_parse_failure(batch_id, source, exc, "EVENT")
            return "UNPARSED"
        update_source_type(source["id"], "EVENT")
        return process_event(batch_id, source, parsed)  # DB/matching failures must propagate

    # S3 preserves regime/other evidence but does not create canonical signal/event rows.
    update_source_type(source["id"], None)
    return category.upper()


def fetch_messages(client: TelegramClient, cursor: int) -> list[Any]:
    common: dict[str, Any] = {"reverse": True}
    if TG_TOPIC_ID is not None:
        common["reply_to"] = TG_TOPIC_ID

    if BACKFILL_SINCE_DAYS is not None:
        kwargs = dict(common)
        kwargs["offset_date"] = datetime.now(timezone.utc) - timedelta(days=BACKFILL_SINCE_DAYS)
        return list(client.iter_messages(TG_GROUP_ID, **kwargs))

    kwargs = dict(common)
    kwargs["min_id"] = cursor
    messages = list(client.iter_messages(TG_GROUP_ID, **kwargs))

    # Cursor cannot discover edited old IDs. A small recent re-scan lets fingerprint
    # detect source evolution while native_source_id remains unchanged.
    if EDIT_RESCAN_DAYS > 0:
        recent_kwargs = dict(common)
        recent_kwargs["offset_date"] = datetime.now(timezone.utc) - timedelta(days=EDIT_RESCAN_DAYS)
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
            # Any failure here is fatal for this run: cursor must not cross an
            # unpreserved Telegram source.
            source, changed = preserve_source(message, batch_id)

            if message.id > safe_frontier:
                safe_frontier = message.id
                set_cursor(safe_frontier)

            if changed:
                summary["source_new_or_changed"] += 1

            # Parse failures return UNPARSED. Technical DB/RPC failures raise and
            # fail the run so that the preserved source is retried on the next scan.
            result = process_preserved_source(batch_id, source)

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
            elif result == "UNPARSED":
                summary["unparsed_source"] += 1
                review_required = True
            elif result == "REGIME":
                summary["regime"] += 1
            else:
                summary["other"] += 1

        finish_batch(batch_id, "PARTIAL_REVIEW_REQUIRED" if review_required else "VERIFIED", summary)
        log.info("S3 Telegram ingestion complete: %s", summary)
        # Unmatched/ambiguous/unparsed are durable review states, not infrastructure failure.
        return 0

    except Exception as exc:
        summary["fatal_error"] = f"{exc.__class__.__name__}: {exc}"
        log.exception("S3 Telegram ingestion failed")
        try:
            finish_batch(batch_id, "FAILED", summary)
        except Exception:
            log.exception("Failed to mark source batch %s FAILED", batch_id)
        return 1


if __name__ == "__main__":
    sys.exit(main())
