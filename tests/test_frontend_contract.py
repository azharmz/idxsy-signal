from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = (ROOT / 'web' / 'idxsy-signal.html').read_text(encoding='utf-8')


def test_frontend_has_no_legacy_canonical_dependencies():
    for legacy in ('trades_data', 'lastXlsxRows', 'matchTrades(', 'mergeWithXlsxData(', 'recompute_trades'):
        assert legacy not in WEB


def test_frontend_has_no_manual_import_pipeline():
    assert 'type="file"' not in WEB
    assert 'FileReader' not in WEB
    assert 'XLSX' not in WEB
    assert 'parseTelegramExport' not in WEB


def test_frontend_is_read_only_against_canonical_tables():
    for mutation in ('.insert(', '.update(', '.upsert(', '.delete('):
        assert mutation not in WEB
    assert "from('signals').select" in WEB
    assert "from('signal_events').select" in WEB
    assert "from('signal_outcomes').select" in WEB
    assert "from('source_records').select" in WEB


def test_list_pagination_sort_filter_are_server_side():
    assert ".range(from,to)" in WEB
    assert ".order(state.sort" in WEB
    assert ".ilike('ticker'" in WEB
    assert ".eq('signal_type',state.type)" in WEB
    assert "{count:'exact'}" in WEB
    assert '.slice(' not in WEB


def test_sortable_fields_are_direct_database_fields_only():
    assert "directSorts=new Set(['signal_timestamp','ticker','signal_type','entry_price','tp1_price','sl_default_price','confidence_score'])" in WEB
    assert '<th>Outcome</th>' in WEB
    assert 'data-sort="outcome_status"' not in WEB
    assert 'data-sort="source"' not in WEB


def test_detail_is_lazy_loaded_and_includes_events_outcome_provenance():
    assert 'async function openDetail(id)' in WEB
    assert "from('signal_events')" in WEB
    assert "from('signal_source_links')" in WEB
    assert 'Event history' in WEB
    assert 'Provenance' in WEB


def test_no_backend_secret_in_frontend():
    assert 'SERVICE_ROLE' not in WEB
    assert 'SUPABASE_SERVICE_KEY' not in WEB
    assert 'ZETA_MEMBER_COOKIE' not in WEB
    assert 'TG_SESSION_STRING' not in WEB
