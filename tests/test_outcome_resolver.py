import pytest

from backend.outcome_resolver import (
    FINAL_ZETA_STATUSES,
    RESOLVER_VERSION,
    assertion_semantically_equal,
    build_assertion_payload,
    is_final_zeta_status,
)


def test_final_zeta_status_vocabulary():
    assert FINAL_ZETA_STATUSES == {'TP_HIT', 'SL_HIT', 'EXPIRED'}
    assert RESOLVER_VERSION == 'zeta-authority-v1'
    assert is_final_zeta_status('tp_hit')
    assert not is_final_zeta_status('OPEN')


def test_build_assertion_payload_preserves_source_fact():
    payload = build_assertion_payload(
        user_id='u1',
        signal_id='s1',
        source_record_id='src1',
        source={
            'zeta_id': '701',
            'status': 'TP_HIT',
            'profit_pct': 5.25,
            'resolved_at_utc': '2026-09-07T03:00:00+00:00',
            'resolved_at_raw': '2026-09-07 10:00:00',
            'source_timestamp_utc': '2026-09-07T02:00:00+00:00',
        },
    )
    assert payload['outcome_status'] == 'TP_HIT'
    assert payload['profit_pct'] == 5.25
    assert payload['assertion_data']['zeta_id'] == '701'
    assert payload['assertion_data']['source_interface'] == 'MEMBER_API'


def test_nonfinal_status_cannot_create_assertion():
    with pytest.raises(ValueError):
        build_assertion_payload(
            user_id='u1',
            signal_id='s1',
            source_record_id='src1',
            source={'zeta_id': '701', 'status': 'OPEN'},
        )


def test_assertion_semantic_equality_ignores_db_metadata():
    old = {
        'signal_id': 's1',
        'source_record_id': 'src1',
        'outcome_status': 'EXPIRED',
        'profit_pct': -1.2,
        'source_resolved_at': '2026-09-07T03:00:00+00:00',
        'assertion_data': {
            'zeta_id': '701',
            'resolved_at_raw': '2026-09-07 10:00:00',
            'source_interface': 'MEMBER_API',
        },
        'created_at': 'ignored',
    }
    new = {**old, 'created_at': 'different'}
    assert assertion_semantically_equal(old, new)
