"""Incremental ingestion orchestrator.

Derives the last stored timestamp directly from the Parquet lake rather than
a separate metadata file. The lake is always the single source of truth.

On first run for a symbol/timeframe, performs a full backfill of lookback_days.
On subsequent runs, fetches only records newer than the last stored timestamp.
"""

import time
from datetime import UTC, datetime

from data.fetcher import INTERVAL_MINUTES, fetch_candles, fetch_funding_since
from data.lake import CandleLake

# Hyperliquid returns at most 500 funding records per API call.
_FUNDING_PAGE_SIZE = 500


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def ingest_candles(symbol: str, timeframe: str, lookback_days: int = 90) -> int:
    """Fetch and store new candles for symbol/timeframe.

    On first run performs a full backfill of lookback_days. On subsequent
    runs fetches only candles newer than the last stored timestamp.

    Returns the number of new candles fetched (0 if already up to date).
    """
    lake = CandleLake()
    last_ts = lake.last_candle_ts(symbol, timeframe)

    now_ms = _now_ms()
    interval_ms = INTERVAL_MINUTES[timeframe] * 60_000

    if last_ts is not None:
        start_ms = last_ts + interval_ms
        label = f"incremental from {_fmt_ts(last_ts)}"
    else:
        start_ms = now_ms - lookback_days * 86_400_000
        label = f"{lookback_days}d backfill"

    if start_ms >= now_ms - interval_ms:
        print(f"  {symbol} {timeframe}: already up to date")
        return 0

    print(f"Fetching {symbol} {timeframe} candles ({label})...")
    candles = fetch_candles(symbol, timeframe, start_ms, now_ms)

    if not candles:
        print("  No new candles returned")
        return 0

    lake.write_candles(symbol, timeframe, candles)

    new_last_ts = max(c["t"] for c in candles)
    print(f"  +{len(candles)} candles → lake  (last: {_fmt_ts(new_last_ts)})")
    return len(candles)


def ingest_funding(symbol: str, lookback_days: int = 90) -> int:
    """Fetch and store new funding rates for symbol.

    Hyperliquid emits one funding record per hour. The lake stores one row
    per hour_ms bucket. Returns the number of new hours stored.
    """
    lake = CandleLake()
    last_ts = lake.last_funding_ts(symbol)

    now_ms = _now_ms()
    hour_ms = 3_600_000

    if last_ts is not None:
        start_ms = last_ts + hour_ms
        label = f"incremental from {_fmt_ts(last_ts)}"
    else:
        start_ms = now_ms - lookback_days * 86_400_000
        label = f"{lookback_days}d backfill"

    if start_ms >= now_ms - hour_ms:
        print(f"  {symbol} funding: already up to date")
        return 0

    print(f"Fetching {symbol} funding history ({label})...")

    all_records: list[dict] = []
    page_start = start_ms
    while True:
        page = fetch_funding_since(symbol, page_start)
        if not page:
            break
        all_records.extend(page)
        if len(page) < _FUNDING_PAGE_SIZE:
            break
        page_start = int(page[-1]["time"]) + hour_ms
        if page_start >= now_ms:
            break

    if not all_records:
        print("  No new funding records returned")
        return 0

    funding_map: dict[int, float] = {
        (int(r["time"]) // hour_ms) * hour_ms: float(r["fundingRate"]) for r in all_records
    }

    lake.write_funding(symbol, funding_map)

    new_last_ts = max(funding_map.keys())
    print(f"  +{len(funding_map)} funding hours → lake  (last: {_fmt_ts(new_last_ts)})")
    return len(funding_map)
