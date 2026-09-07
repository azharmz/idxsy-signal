from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC = (ROOT / 'backend' / 'zeta_sync.py').read_text(encoding='utf-8')
RECON = (ROOT / 'backend' / 'reconciliation.py').read_text(encoding='utf-8')
RESOLVER = (ROOT / 'backend' / 'outcome_resolver.py').read_text(encoding='utf-8')
MIGRATION = (ROOT / 'supabase' / 'migrations' / '20260907_signal_s4_zeta_rpc.sql').read_text(encoding='utf-8')


def test_member_api_is_only_zeta_source():
    assert 'member.zeta-ai.pro/api/signals' in SYNC
    assert 'idx-journal.zeta-ai.pro' not in SYNC
    assert 'PUBLIC_API_URL' not in SYNC
    assert 'public API fallback is disabled' in SYNC


def test_no_legacy_trades_data_or_recompute_path():
    assert 'trades_data' not in SYNC
    assert 'recompute_trades' not in SYNC


def test_zeta_native_identity_and_source_first_storage():
    assert '"source_system": "ZETA"' in SYNC
    assert '"native_source_id": native_id' in SYNC
    assert 'table("source_records")' in SYNC
    assert 'source, changed = preserve_source(raw, batch_id)' in SYNC


def test_reconstruction_reconciliation_semantics_retained():
    assert 'ZETA_MATCH_WINDOW_SECONDS = 300' in RECON
    assert 'PRICE_TOLERANCE_FRACTION = 0.001' in RECON
    assert 'result="ZETA_ONLY"' in RECON
    assert 'result="AMBIGUOUS"' in RECON
    assert 'result="MATCHED"' in RECON


def test_zeta_only_and_matched_link_semantics_are_explicit():
    assert 'NATIVE_ZETA_PUBLICATION' in MIGRATION
    assert 'TELEGRAM_ZETA_RECONCILIATION' in SYNC
    assert 'HIGH_CONFIDENCE' in SYNC


def test_outcome_vocabulary_and_version_match_reconstruction_baseline():
    assert '{"TP_HIT", "SL_HIT", "EXPIRED"}' in RESOLVER
    assert 'zeta-authority-v1' in RESOLVER
    assert 'zeta-authority-v1' in MIGRATION
    assert "'ZETA'" in MIGRATION


def test_assertion_and_canonical_outcome_are_separate():
    assert 'table("signal_outcome_assertions")' in SYNC
    assert 'resolve_zeta_outcome' in SYNC
    assert 'INSERT INTO public.signal_outcomes' in MIGRATION
    assert 'superseded_at = now()' in MIGRATION


def test_resolver_selects_latest_assertion_and_is_atomic_per_signal():
    assert 'ORDER BY COALESCE(a.source_resolved_at, sr.source_timestamp_utc, a.created_at) DESC' in MIGRATION
    assert 'pg_advisory_xact_lock' in MIGRATION
    assert 'superseded_at IS NULL' in MIGRATION


def test_zeta_publication_rpc_protects_telegram_snapshot():
    assert "sr.source_system = 'TELEGRAM'" in MIGRATION
    assert "sr.source_record_type = 'SIGNAL'" in MIGRATION
    assert 'IF v_has_telegram_provenance THEN' in MIGRATION
    assert 'RETURN v_signal_id;' in MIGRATION


def test_s4_rpcs_are_backend_only():
    for signature in (
        'ensure_zeta_publication(uuid, uuid, jsonb)',
        'resolve_zeta_outcome(uuid, uuid)',
    ):
        assert f'REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC;' in MIGRATION
        assert f'REVOKE ALL ON FUNCTION public.{signature} FROM anon;' in MIGRATION
        assert f'REVOKE ALL ON FUNCTION public.{signature} FROM authenticated;' in MIGRATION
        assert f'GRANT EXECUTE ON FUNCTION public.{signature} TO service_role;' in MIGRATION


def test_parser_failure_reuses_frozen_source_generic_vocabulary():
    assert 'item_type="UNPARSED_SOURCE"' in SYNC
    assert 'proposed_action="REPROCESS"' in SYNC
    assert 'conflict_type="PARSE_FAILED"' in SYNC


def test_open_does_not_create_outcome_assertion():
    assert 'if not is_final_zeta_status(parsed.get("status")):' in SYNC
    assert 'ZETA_STATUS_REGRESSION' in SYNC
