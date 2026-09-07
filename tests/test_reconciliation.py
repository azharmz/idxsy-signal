from backend.reconciliation import match_zeta_to_telegram, price_close


def candidate(signal_id, ts, entry=100, tp=110, sl=95, msg='1'):
    return {
        'id': signal_id,
        'signal_timestamp': ts,
        'entry_price': entry,
        'tp1_price': tp,
        'sl_default_price': sl,
        'telegram_source_record_id': f'src-{signal_id}',
        'telegram_msg_id': msg,
    }


def zeta(ts='2026-09-07T02:00:00+00:00', entry=100, tp=110, sl=95):
    return {
        'source_timestamp_utc': ts,
        'entry_price': entry,
        'tp1_price': tp,
        'sl_default_price': sl,
    }


def test_price_close_matches_reconstruction_tolerance():
    assert price_close(1000, 1001)
    assert not price_close(1000, 1002.1)


def test_unique_exact_candidate_matches():
    result = match_zeta_to_telegram(
        zeta(),
        [candidate('s1', '2026-09-07T02:00:20+00:00')],
    )
    assert result.result == 'MATCHED'
    assert result.signal_id == 's1'


def test_unique_nearest_candidate_wins_when_distances_differ():
    result = match_zeta_to_telegram(
        zeta(),
        [
            candidate('near', '2026-09-07T02:00:10+00:00'),
            candidate('far', '2026-09-07T02:01:00+00:00'),
        ],
    )
    assert result.result == 'MATCHED'
    assert result.signal_id == 'near'
    assert result.evidence['candidate_count'] == 2


def test_equal_nearest_candidates_are_ambiguous():
    result = match_zeta_to_telegram(
        zeta(),
        [
            candidate('a', '2026-09-07T01:59:30+00:00'),
            candidate('b', '2026-09-07T02:00:30+00:00'),
        ],
    )
    assert result.result == 'AMBIGUOUS'
    assert result.signal_id is None


def test_no_candidate_becomes_zeta_only():
    result = match_zeta_to_telegram(zeta(), [])
    assert result.result == 'ZETA_ONLY'


def test_price_mismatch_disqualifies_candidate():
    result = match_zeta_to_telegram(
        zeta(entry=100),
        [candidate('s1', '2026-09-07T02:00:10+00:00', entry=120)],
    )
    assert result.result == 'ZETA_ONLY'
