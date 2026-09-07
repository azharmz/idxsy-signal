from backend.telegram_parser import classify, parse_result, parse_signal


def test_classify_uses_event_not_legacy_result():
    assert classify("SIGNAL CONFIRMED\nSymbol: BBCA") == "event"
    assert classify("PROFIT TERKUNCI\nSymbol: BBCA") == "event"
    assert classify("PROFIT TERUS NAIK\nSymbol: BBCA") == "event"


def test_parse_signal_minimal_publication():
    text = """ZETA IDX STOCK SIGNAL
Saham: BBCA
Signal: BUY
Entry Price: Rp9.500
TP1: Rp9.900 (+4.2%)
Default (ATR): Rp9.300 (-2.1%)
Confidence Score: 8/10 (HIGH)
Powered by Zeta AI v2.1
"""
    row = parse_signal(text, 7001, "2026-09-07T02:00:00+00:00")
    assert row["symbol"] == "BBCA"
    assert row["signal_type"] == "BUY"
    assert row["entry_price"] == 9500
    assert row["take_profit"] == 9900
    assert row["take_profit_pct"] == "+4.2%"
    assert row["stop_loss"] == 9300
    assert row["confidence_score"] == 8


def test_parse_signal_preserves_broker_detail():
    text = """ZETA IDX STOCK SIGNAL
Saham: BBRI
Signal: WATCHLIST
Entry Price: Rp4.500
Top Buyer:
YP 10.000 lot
AK 8.000 lot
CC 5.000 lot

Top Seller:
PD 9.000 lot
XL 7.000 lot
NI 4.000 lot

Powered by Zeta AI v2.1
"""
    row = parse_signal(text, 7005, "2026-09-07T02:00:00+00:00")
    assert row["top_buyer_1"] == "YP 10.000 lot"
    assert row["top_buyer_3"] == "CC 5.000 lot"
    assert row["top_seller_1"] == "PD 9.000 lot"
    assert row["top_seller_3"] == "NI 4.000 lot"


def test_parse_tp_hit_event():
    text = """SIGNAL CONFIRMED
Symbol: BBCA
Entry: Rp9.500 → TP1: Rp9.900
Profit: +4.2%
"""
    row = parse_result(text, 7002, "2026-09-07T04:00:00+00:00")
    assert row["type"] == "TP_HIT"
    assert row["symbol"] == "BBCA"
    assert row["entry"] == 9500
    assert row["exit_price"] == 9900
    assert row["profit_pct"] == "+4.2%"


def test_parse_profit_locked_event():
    text = """PROFIT TERKUNCI
Symbol: TLKM
Entry: Rp3.000 → Exit: Rp3.150
Profit Terkunci: +5.0%
"""
    row = parse_result(text, 7003, "2026-09-07T04:00:00+00:00")
    assert row["type"] == "PROFIT_LOCKED"
    assert row["symbol"] == "TLKM"
    assert row["profit_pct"] == "+5.0%"


def test_parse_profit_running_event():
    text = """PROFIT TERUS NAIK
Symbol: ASII
Entry: Rp5.000
Day High: Rp5.200
Profit Sekarang: +4.0%
"""
    row = parse_result(text, 7004, "2026-09-07T04:00:00+00:00")
    assert row["type"] == "PROFIT_RUNNING"
    assert row["symbol"] == "ASII"
    assert row["day_high"] == 5200
