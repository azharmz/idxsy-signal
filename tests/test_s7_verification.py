from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY = (ROOT / 'supabase' / 'verification' / 's7_pre_cutover.sql').read_text(encoding='utf-8')


def test_verification_is_read_only():
    sql = VERIFY.upper()
    for statement in ('INSERT INTO', 'UPDATE PUBLIC.', 'DELETE FROM', 'DROP TABLE', 'TRUNCATE'):
        assert statement not in sql


def test_authoritative_reconstruction_baselines_are_present():
    for value in ('1138::bigint', '3023::bigint', '2025::bigint', '480::bigint', '460::bigint', '1000::bigint'):
        assert value in VERIFY


def test_duplicate_and_orphan_guards_are_present():
    assert 'GROUP BY user_id, source_system, native_source_id' in VERIFY
    assert 'GROUP BY source_record_id' in VERIFY
    assert 'superseded_at IS NULL' in VERIFY
    assert 'orphan source link -> signal' in VERIFY
    assert 'orphan assertion -> source' in VERIFY


def test_event_outcome_boundary_is_verified():
    assert "WHERE sr.source_system <> 'ZETA'" in VERIFY
    assert "outcome_status NOT IN ('TP_HIT', 'SL_HIT', 'EXPIRED')" in VERIFY


def test_cutover_objects_are_verified():
    assert 'signal_events_source_unique_idx' in VERIFY
    assert 'ensure_telegram_publication(uuid,uuid,jsonb)' in VERIFY
    assert 'ensure_zeta_publication(uuid,uuid,jsonb)' in VERIFY
    assert 'resolve_zeta_outcome(uuid,uuid)' in VERIFY
