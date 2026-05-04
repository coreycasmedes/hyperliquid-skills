"""Incremental ingestion orchestrator.

Reads data/metadata.json to determine the last stored timestamp for each
symbol/timeframe, then fetches only new records from the Hyperliquid API
and writes them to the Parquet lake.

metadata.json structure
-----------------------
{
  "candles": {
    "HYPE/15m": 1746000000000,   <- open-time ms of last stored candle
    "BTC/1h":   1746000000000
  },
  "funding": {
    "HYPE": 1746000000000        <- hour_ms of last stored funding record
  }
}

This file is the source of truth for incremental updates. It is written
atomically (temp-file rename) so a crash during ingestion leaves the
previous state intact.
"""

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from data.fetcher import INTERVAL_MINUTES, fetch_candles, fetch_funding_since
from data.lake import CandleLake

# Hyperliquid returns at most 500 funding records per API call.
# Pagination is required for windows longer than ~20 days.
_FUNDING_PAGE_SIZE = 500

METADATA_PATH = Path(__file__).parent / "metadata.json"


# ── Metadata helpers ──────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _load_metadata() -> dict:
    if not METADATA_PATH.exists():
        return {"candles": {}, "funding": {}}
    with open(METADATA_PATH) as f:
        return json.load(f)


def _save_metadata(meta: dict) -> None:
    """Write metadata atomically via temp-file rename."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=METADATA_PATH.parent,
        delete=False,
        suffix=".tmp",
    ) as f:
        json.dump(meta, f, indent=2)
        tmp = f.name
    os.replace(tmp, METADATA_PATH)


# ── Public API ────────────────────────────────────────────────────────────────


def ingest_candles(symbol: str, timeframe: str, lookback_days: int = 90) -> int:
    """Fetch and store new candles for symbol/timeframe.

    On first run performs a full backfill of lookback_days. On subsequent
    runs fetches only candles newer than the last stored timestamp.

    Returns the number of new candles fetched (0 if already up to date).
    """
    meta = _load_metadata()
    key = f"{symbol}/{timeframe}"
    last_ts: int | None = meta["candles"].get(key)

    now_ms = _now_ms()
    interval_ms = INTERVAL_MINUTES[timeframe] * 60_000

    if last_ts is not None:
        start_ms = last_ts + interval_ms
        label = f"incremental from {_fmt_ts(last_ts)}"
    else:
        start_ms = now_ms - lookback_days * 86_400_000
        label = f"{lookback_days}d backfill"

    # Less than one interval of headroom means nothing new to fetch
    if start_ms >= now_ms - interval_ms:
        print(f"  {symbol} {timeframe}: already up to date")
        return 0

    print(f"Fetching {symbol} {timeframe} candles ({label})...")
    candles = fetch_candles(symbol, timeframe, start_ms, now_ms)

    if not candles:
        print("  No new candles returned")
        return 0

    CandleLake().write_candles(symbol, timeframe, candles)

    new_last_ts = max(c["t"] for c in candles)
    meta["candles"][key] = new_last_ts
    _save_metadata(meta)

    print(f"  +{len(candles)} candles → lake  (last: {_fmt_ts(new_last_ts)})")
    return len(candles)


def ingest_funding(symbol: str, lookback_days: int = 90) -> int:
    """Fetch and store new funding rates for symbol.

    Hyperliquid emits one funding record per hour. The lake stores one row
    per hour_ms bucket. Returns the number of new hours stored.
    """
    meta = _load_metadata()
    last_ts: int | None = meta["funding"].get(symbol)

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

    # Paginate: Hyperliquid caps each response at _FUNDING_PAGE_SIZE records.
    all_records: list[dict] = []
    page_start = start_ms
    while True:
        page = fetch_funding_since(symbol, page_start)
        if not page:
            break
        all_records.extend(page)
        if len(page) < _FUNDING_PAGE_SIZE:
            break  # last page — no more data
        # Advance to the hour after the last returned record
        page_start = int(page[-1]["time"]) + hour_ms
        if page_start >= now_ms:
            break

    if not all_records:
        print("  No new funding records returned")
        return 0

    funding_map: dict[int, float] = {
        (int(r["time"]) // hour_ms) * hour_ms: float(r["fundingRate"])
        for r in all_records
    }

    CandleLake().write_funding(symbol, funding_map)

    new_last_ts = max(funding_map.keys())
    meta["funding"][symbol] = new_last_ts
    _save_metadata(meta)

    print(f"  +{len(funding_map)} funding hours → lake  (last: {_fmt_ts(new_last_ts)})")
    return len(funding_map)
