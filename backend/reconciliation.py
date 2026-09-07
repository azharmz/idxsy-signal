"""Pure reconciliation helpers shared by live Signal backends.

The Zeta matcher deliberately mirrors the reconstruction rule established in
002_reconcile_dry_run.py: same ticker, publication timestamps within five
minutes, compatible entry/TP/SL, and only a unique nearest candidate may be
auto-linked. Ambiguous candidates are never silently merged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

ZETA_MATCH_WINDOW_SECONDS = 300
PRICE_TOLERANCE_FRACTION = 0.001  # 0.1%; reconstruction compatibility


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Expected timezone-aware canonical timestamp")
    return dt.astimezone(timezone.utc)


def price_close(a: Any, b: Any, tol: float = PRICE_TOLERANCE_FRACTION) -> bool:
    """Compatibility rule used by the canonical reconstruction.

    Missing source fields do not disqualify a candidate. When both values are
    present the accepted absolute difference is max(Rp1, 0.1% of source value).
    """
    if a is None or b is None:
        return True
    av, bv = float(a), float(b)
    return abs(av - bv) <= max(1.0, abs(av) * tol)


def diff_pct(a: Any, b: Any) -> float | None:
    if a is None or b in (None, 0):
        return None
    return abs(float(a) - float(b)) / abs(float(b)) * 100.0


@dataclass(frozen=True)
class ZetaMatchResult:
    result: str  # MATCHED | ZETA_ONLY | AMBIGUOUS
    signal_id: str | None = None
    telegram_source_record_id: str | None = None
    telegram_msg_id: str | None = None
    evidence: dict[str, Any] | None = None


def match_zeta_to_telegram(
    zeta: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
) -> ZetaMatchResult:
    """Match a new Zeta publication to Telegram-provenance canonical signals.

    Each candidate must already have Telegram publication provenance and expose:
    id, signal_timestamp, entry_price, tp1_price, sl_default_price,
    telegram_source_record_id, telegram_msg_id.
    """
    zeta_ts = parse_utc(zeta["source_timestamp_utc"])
    viable: list[tuple[float, dict[str, Any]]] = []

    for candidate in candidates:
        signal_ts = parse_utc(candidate["signal_timestamp"])
        seconds = abs((zeta_ts - signal_ts).total_seconds())
        if seconds > ZETA_MATCH_WINDOW_SECONDS:
            continue
        if not price_close(zeta.get("entry_price"), candidate.get("entry_price")):
            continue
        if not price_close(zeta.get("tp1_price"), candidate.get("tp1_price")):
            continue
        if not price_close(zeta.get("sl_default_price"), candidate.get("sl_default_price")):
            continue
        viable.append((seconds, candidate))

    viable.sort(key=lambda item: item[0])
    if not viable:
        return ZetaMatchResult(
            result="ZETA_ONLY",
            evidence={"candidate_count": 0, "window_seconds": ZETA_MATCH_WINDOW_SECONDS},
        )

    if len(viable) > 1 and viable[0][0] == viable[1][0]:
        return ZetaMatchResult(
            result="AMBIGUOUS",
            evidence={
                "candidate_count": len(viable),
                "window_seconds": ZETA_MATCH_WINDOW_SECONDS,
                "candidate_signal_ids": [row[1]["id"] for row in viable],
                "nearest_age_seconds": viable[0][0],
            },
        )

    seconds, chosen = viable[0]
    return ZetaMatchResult(
        result="MATCHED",
        signal_id=str(chosen["id"]),
        telegram_source_record_id=str(chosen["telegram_source_record_id"]),
        telegram_msg_id=str(chosen["telegram_msg_id"]),
        evidence={
            "candidate_count": len(viable),
            "age_seconds": seconds,
            "entry_diff_pct": diff_pct(zeta.get("entry_price"), chosen.get("entry_price")),
            "tp1_diff_pct": diff_pct(zeta.get("tp1_price"), chosen.get("tp1_price")),
            "sl_diff_pct": diff_pct(zeta.get("sl_default_price"), chosen.get("sl_default_price")),
            "window_seconds": ZETA_MATCH_WINDOW_SECONDS,
        },
    )
