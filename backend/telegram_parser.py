"""Pure Telegram parsing helpers for IDXSY Signal S3.

This module deliberately contains no database writes. Raw Telegram evidence is
preserved by telegram_ingest.py before these functions are used.
"""

from __future__ import annotations

import re
from typing import Any

PARSER_VERSION = "telegram-s3-v1"


class ParseError(ValueError):
    """Raised when a message classified as SIGNAL/EVENT cannot be interpreted."""


def match1(pattern: str, text: str, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else None


def parse_num(value: Any) -> float | None:
    if value is None:
        return None
    text = re.sub(r"Rp", "", str(value), flags=re.IGNORECASE).strip()
    text = re.sub(r"[^\d.,\-+]", "", text)
    if text in ("", "-", "+"):
        return None

    has_comma = "," in text
    has_dot = "." in text
    if has_comma and has_dot:
        last_comma, last_dot = text.rfind(","), text.rfind(".")
        if last_comma > last_dot:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        parts = text.split(",")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            text = "".join(parts)
        else:
            text = text.replace(",", ".", 1)
    elif has_dot:
        parts = text.split(".")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            text = "".join(parts)

    try:
        return float(text)
    except ValueError:
        return None


def parse_pct(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def classify(text: str) -> str:
    """Return signal, event, regime, or other.

    `event` replaces the legacy frontend/backend term `result`; TP/profit
    messages are historical signal events, not canonical outcomes.
    """
    if "ZETA IDX STOCK SIGNAL" in text:
        return "signal"
    if (
        "SIGNAL CONFIRMED" in text
        or "PROFIT TERKUNCI" in text
        or "PROFIT TERUS NAIK" in text
    ):
        return "event"
    if "Regime Prediction" in text:
        return "regime"
    return "other"


def parse_signal(text: str, msg_id: int, timestamp_iso: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "msg_id": msg_id,
        "date": timestamp_iso,
        "type": "SIGNAL",
    }
    lines = text.split("\n")

    row["market_warning"] = match1(r"^⚠️\s*(.+)$", text, flags=re.MULTILINE)
    row["symbol"] = match1(r"Saham:\s*(\S+)", text)
    if not row["symbol"]:
        raise ParseError("SIGNAL message has no ticker/Saham field")
    row["symbol"] = row["symbol"].upper()

    sig_match = re.search(r"Signal:\s*.*?\b(BUY|WATCHLIST|SELL)\b", text)
    row["signal_type"] = sig_match.group(1) if sig_match else None

    conf_match = re.search(r"Confidence Score:\s*[^\d]*(\d+)/10\s*\(([^)]+)\)", text)
    if conf_match:
        row["confidence_score"] = int(conf_match.group(1))
        row["confidence_label"] = conf_match.group(2).strip()

    reasons = [line.strip() for line in lines if re.match(r"^\s*[+\-]\d+\s+\S", line)]
    row["confidence_reasons"] = "; ".join(reasons) if reasons else None

    row["entry_price"] = parse_num(match1(r"Entry Price:\s*(Rp?[\d.,]+)", text))

    tp_match = re.search(r"Take Profit:\s*(Rp?[\d.,]+)", text)
    if tp_match:
        row["take_profit"] = parse_num(tp_match.group(1))
    else:
        tp_match = re.search(r"TP1:\s*(Rp?[\d.,]+)\s*\(([+\-\d.]+%)\)", text)
        if tp_match:
            row["take_profit"] = parse_num(tp_match.group(1))
            row["take_profit_pct"] = tp_match.group(2)

    tp2_match = re.search(
        r"(?:Target 2|TP2)[^:]*:\s*(Rp?[\d.,]+)(?:\s*\(([+\-\d.]+%)\))?",
        text,
    )
    if tp2_match:
        row["target2_price"] = parse_num(tp2_match.group(1))
        if tp2_match.group(2):
            row["target2_pct"] = tp2_match.group(2)

    sl_single = re.search(r"Stop Loss:\s*(Rp?[\d.,]+)", text)
    if sl_single:
        row["stop_loss"] = parse_num(sl_single.group(1))

    sl_default = re.search(r"Default \(ATR\):\s*(Rp?[\d.,]+)\s*\(([+\-\d.]+%)\)", text)
    if sl_default:
        row["stop_loss"] = parse_num(sl_default.group(1))
        row["stop_loss_pct"] = sl_default.group(2)

    sl_mod = re.search(r"Moderat \((-?[\d.]+)%\):\s*(Rp?[\d.,]+)", text)
    if sl_mod:
        row["sl_moderat"] = parse_num(sl_mod.group(2))
        row["sl_moderat_pct"] = float(sl_mod.group(1))

    sl_cons = re.search(r"Konservatif \((-?[\d.]+)%\):\s*(Rp?[\d.,]+)", text)
    if sl_cons:
        row["sl_konservatif"] = parse_num(sl_cons.group(2))
        row["sl_konservatif_pct"] = float(sl_cons.group(1))

    macd_match = re.search(
        r"MACD:\s*([\-\d.]+)\s*\(Sig:\s*([\-\d.]+)\)\s*\S*\s*(Bullish|Bearish)?",
        text,
    )
    if macd_match:
        row["macd"] = float(macd_match.group(1))
        row["macd_signal_val"] = float(macd_match.group(2))
        row["macd_trend"] = macd_match.group(3)

    rsi_match = re.search(r"RSI \(14\):\s*([\d.]+)\s*\S*\s*\(?(Overbought|Oversold)?\)?", text)
    if rsi_match:
        row["rsi"] = float(rsi_match.group(1))
        row["rsi_label"] = rsi_match.group(2)

    ema_match = re.search(
        r"EMA 20/50:\s*(Rp?[\d.,]+)\s*/\s*(Rp?[\d.,]+)\s*\S*\s*(Bullish|Bearish)?",
        text,
    )
    if ema_match:
        row["ema20"] = parse_num(ema_match.group(1))
        row["ema50"] = parse_num(ema_match.group(2))
        row["ema_trend"] = ema_match.group(3)

    vwap_match = re.search(r"VWAP:\s*(Rp?[\d.,]+)\s*\S*\s*(Above|Below)?", text)
    if vwap_match:
        row["vwap"] = parse_num(vwap_match.group(1))
        row["vwap_position"] = vwap_match.group(2)

    bb_match = re.search(r"(?:Bollinger Bands|BB):\s*\[(Rp?[\d.,]+)\s*-\s*(Rp?[\d.,]+)\]", text)
    if bb_match:
        row["bb_lower"] = parse_num(bb_match.group(1))
        row["bb_upper"] = parse_num(bb_match.group(2))

    adx_match = re.search(r"ADX:\s*([\d.]+)\s*(?:\(([^)]+)\))?\s*\S*\s*(Strong|Weak)?", text)
    if adx_match:
        row["adx"] = float(adx_match.group(1))
        row["adx_label"] = adx_match.group(3) or adx_match.group(2)

    atr_match = re.search(r"ATR(?:\s*\(Volatilitas\))?:\s*(Rp?[\d.,]+)", text)
    if atr_match:
        row["atr"] = parse_num(atr_match.group(1))

    row["chart_pattern"] = match1(r"Chart:\s*(.+)", text)
    row["candle_pattern"] = match1(r"Candle:\s*(.+)", text)
    row["bandar_signal"] = match1(r"Sinyal Bandar:\s*\S*\s*(\S+)", text)
    row["smart_money_net"] = match1(r"Smart Money Net:\s*([+\-][\w.,]+\s*\w*)", text)

    buyer_block = re.search(r"Top Buyer:\s*\n([\s\S]*?)(?:\n\s*\n|🔴|Top Seller|📈|💡|$)", text)
    if buyer_block:
        buyers = [line.strip() for line in buyer_block.group(1).split("\n") if line.strip()]
        for index, line in enumerate(buyers[:3], start=1):
            row[f"top_buyer_{index}"] = line

    seller_block = re.search(r"Top Seller:\s*\n([\s\S]*?)(?:\n\s*\n|📈|💡|$)", text)
    if seller_block:
        sellers = [line.strip() for line in seller_block.group(1).split("\n") if line.strip()]
        for index, line in enumerate(sellers[:3], start=1):
            row[f"top_seller_{index}"] = line

    beta_match = re.search(r"Beta:\s*([\d.]+)\s*\(([^)]+)\)\s*\|\s*Volatilitas:\s*(\d+)%", text)
    if beta_match:
        row["beta"] = float(beta_match.group(1))
        row["beta_label"] = beta_match.group(2)
        row["volatilitas_pct"] = float(beta_match.group(3))

    foreign_status = match1(r"Status:\s*\S*\s*(NET BUY ASING|NET SELL ASING|NEUTRAL)", text)
    if foreign_status:
        row["foreign_status"] = foreign_status

    net_foreign = re.search(r"Net Asing:\s*([+\-][\w.]+)\s*\(([+\-\d,]+)\s*lot\)", text)
    if net_foreign:
        row["net_asing"] = net_foreign.group(1)
        row["net_asing_lot"] = parse_num(net_foreign.group(2))

    buy_sell = re.search(r"Buy:\s*([\d,]+)\s*lot\s*\|\s*Sell:\s*([\d,]+)\s*lot", text)
    if buy_sell:
        row["foreign_buy_lot"] = parse_num(buy_sell.group(1))
        row["foreign_sell_lot"] = parse_num(buy_sell.group(2))

    participation = re.search(r"Partisipasi Asing:\s*(\d+)%", text)
    if participation:
        row["partisipasi_asing_pct"] = float(participation.group(1))

    opinion = re.search(r"Analyst Opinion:\s*\n([\s\S]*?)(?:\n\s*\n|📰|🤖|$)", text)
    if opinion:
        row["analyst_opinion"] = opinion.group(1).strip()

    news = re.search(r"Berita Terkait:\s*\n([\s\S]*?)(?:🤖|$)", text)
    if news:
        cleaned = [re.sub(r"^•\s*", "", line).strip() for line in news.group(1).split("\n")]
        for index, line in enumerate([line for line in cleaned if line][:5], start=1):
            row[f"news_{index}"] = line

    version = re.search(r"Powered by Zeta AI\s*(v[\d.]+)?", text)
    row["bot_version"] = version.group(1) if version and version.group(1) else "v1"
    return row


def parse_result(text: str, msg_id: int, timestamp_iso: str) -> dict[str, Any]:
    """Parse a legacy Telegram result/profit message into a canonical EVENT payload."""
    row: dict[str, Any] = {"msg_id": msg_id, "date": timestamp_iso}

    if "SIGNAL CONFIRMED" in text:
        row["type"] = "TP_HIT"
        row["symbol"] = match1(r"Symbol:\s*(\S+)", text)
        row["status_text"] = match1(r"Status:\s*\S*\s*(.+)", text)
        entry_exit = re.search(r"Entry:\s*(Rp?[\d.,]+)\s*→\s*TP1?:\s*(Rp?[\d.,]+)", text)
        if entry_exit:
            row["entry"] = parse_num(entry_exit.group(1))
            row["exit_price"] = parse_num(entry_exit.group(2))
        row["day_high"] = parse_num(match1(r"Day High:\s*(Rp?[\d.,]+)", text))
        peak = re.search(r"Peak Tertinggi:\s*(Rp?[\d.,]+)\s*\(([+\-\d.]+%)\)", text)
        if peak:
            row["peak_price"] = parse_num(peak.group(1))
            row["peak_pct"] = peak.group(2)
        row["profit_pct"] = (
            match1(r"Profit:\s*([+\-\d.]+%)", text)
            or match1(r"Profit Terkunci:\s*([+\-\d.]+%)", text)
        )

    elif "PROFIT TERKUNCI" in text:
        row["type"] = "PROFIT_LOCKED"
        row["symbol"] = match1(r"Symbol:\s*(\S+)", text)
        row["status_text"] = match1(r"Status:\s*\S*\s*(.+)", text)
        entry_exit = re.search(r"Entry:\s*(Rp?[\d.,]+)\s*→\s*Exit:\s*(Rp?[\d.,]+)", text)
        if entry_exit:
            row["entry"] = parse_num(entry_exit.group(1))
            row["exit_price"] = parse_num(entry_exit.group(2))
        row["profit_pct"] = match1(r"Profit Terkunci:\s*([+\-\d.]+%)", text)
        peak = re.search(r"Peak Tertinggi:\s*(Rp?[\d.,]+)\s*\(([+\-\d.]+%)\)", text)
        if peak:
            row["peak_price"] = parse_num(peak.group(1))
            row["peak_pct"] = peak.group(2)

    elif "PROFIT TERUS NAIK" in text:
        row["type"] = "PROFIT_RUNNING"
        row["symbol"] = match1(r"Symbol:\s*(\S+)", text)
        row["entry"] = parse_num(match1(r"Entry:\s*(Rp?[\d.,]+)", text))
        row["day_high"] = parse_num(match1(r"Day High:\s*(Rp?[\d.,]+)", text))
        row["profit_pct"] = match1(r"Profit Sekarang:\s*([+\-\d.]+%)", text)
    else:
        raise ParseError("EVENT classifier matched but no supported event type parsed")

    if not row.get("symbol"):
        raise ParseError("EVENT message has no Symbol field")
    row["symbol"] = str(row["symbol"]).upper()

    duration = re.search(r"Durasi(?:\s*Sinyal)?:\s*([\d.]+)\s*(hari|jam)", text, re.IGNORECASE)
    if duration:
        value = float(duration.group(1))
        row["duration_days_confirm"] = value / 24 if duration.group(2).lower() == "jam" else value

    return row


def parse_regime(text: str, msg_id: int, timestamp_iso: str) -> dict[str, Any]:
    row: dict[str, Any] = {"msg_id": msg_id, "date": timestamp_iso, "type": "REGIME"}
    date_match = re.search(r"Regime Prediction\s*—\s*(.+)", text)
    row["regime_date"] = date_match.group(1).strip() if date_match else None
    prediction = re.search(r"Prediksi:\s*(BULLISH|BEARISH|NEUTRAL)\s*\(Score:\s*([+\-\d.]+)\)", text)
    if prediction:
        row["prediction"] = prediction.group(1)
        row["score"] = float(prediction.group(2))
    return row
