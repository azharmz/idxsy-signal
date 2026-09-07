from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INGEST = (ROOT / "backend" / "telegram_ingest.py").read_text(encoding="utf-8")
PARSER = (ROOT / "backend" / "telegram_parser.py").read_text(encoding="utf-8")
MIGRATION = (ROOT / "supabase" / "migrations" / "20260907_signal_s3_live_ingestion.sql").read_text(encoding="utf-8")


def test_no_new_trades_data_dependency():
    assert 'table("trades_data")' not in INGEST
    assert "payload.trades" not in INGEST


def test_native_source_identity_and_source_first_contract_present():
    assert '"source_system": "TELEGRAM"' in INGEST
    assert '"native_source_id": native_id' in INGEST
    assert 'table("source_records")' in INGEST
    assert "source, changed = preserve_source(message, batch_id)" in INGEST


def test_cursor_advances_only_after_source_preservation_in_loop():
    preserve_at = INGEST.index("source, changed = preserve_source(message, batch_id)")
    cursor_at = INGEST.index("set_cursor(safe_frontier)", preserve_at)
    assert preserve_at < cursor_at


def test_parser_failure_vocabulary_is_frozen():
    assert 'item_type="UNPARSED_SOURCE"' in INGEST
    assert 'proposed_action="REPROCESS"' in INGEST
    assert 'conflict_type="PARSE_FAILED"' in INGEST


def test_event_language_is_not_legacy_result_classification():
    assert 'return "event"' in PARSER
    assert 'return "result"' not in PARSER


def test_telegram_events_do_not_write_outcomes():
    assert 'table("signal_outcomes")' not in INGEST
    assert 'table("signal_outcome_assertions")' not in INGEST
    assert 'table("signal_events")' in INGEST


def test_gap_001_unique_event_source_index_present():
    assert "CREATE UNIQUE INDEX IF NOT EXISTS signal_events_source_unique_idx" in MIGRATION
    assert "ON public.signal_events (source_record_id)" in MIGRATION
    assert "WHERE source_record_id IS NOT NULL" in MIGRATION


def test_gap_002_atomic_publication_rpc_present():
    assert "CREATE OR REPLACE FUNCTION public.ensure_telegram_publication" in MIGRATION
    assert "INSERT INTO public.signals" in MIGRATION
    assert "INSERT INTO public.signal_source_links" in MIGRATION
    assert "NATIVE_TELEGRAM_PUBLICATION" in MIGRATION
