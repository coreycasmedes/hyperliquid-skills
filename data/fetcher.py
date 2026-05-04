"""Hyperliquid public API client and data access layer.

Two responsibilities live here:
  1. Raw API calls — fetch_candles, fetch_funding_since, fetch_funding_history,
     get_latest_candles. These are pure HTTP functions with no side effects
     beyond the returned data.
  2. High-level accessors — fetch_and_save, fetch_and_save_funding, load_candles,
     load_funding_map. These delegate to data.ingest (writes) and data.lake
     (reads) so callers don't need to import those modules directly.

No authentication is required; all endpoints are public.
"""

import time
from pathlib import Path

import requests

API_URL = "https://api.hyperliquid.xyz/info"
DATA_DIR = Path(__file__).parent

SUPPORTED_COINS = ["HYPE", "BTC", "ETH", "SOL"]

INTERVAL_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
    "3d": 4320,
    "1w": 10080,
}


# ── Raw API calls ─────────────────────────────────────────────────────────────


def _post(payload: dict) -> dict | list:
    response = requests.post(API_URL, json=payload, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch a range of OHLCV candles from Hyperliquid.

    Returns a list of dicts with keys: t (open time ms), o, h, l, c, v, n.
    All price/volume fields are floats; timestamps are ints.
    """
    raw = _post(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        }
    )
    return [
        {
            "t": int(row["t"]),
            "o": float(row["o"]),
            "h": float(row["h"]),
            "l": float(row["l"]),
            "c": float(row["c"]),
            "v": float(row["v"]),
            "n": int(row["n"]),
        }
        for row in raw
    ]


def fetch_funding_since(coin: str, start_ms: int) -> list[dict]:
    """Fetch funding history records starting from a specific UTC millisecond timestamp.

    Each record has keys: coin, fundingRate (str), premium (str), time (int ms).
    Used by the incremental ingestor; prefer this over fetch_funding_history
    when you already know the last stored timestamp.
    """
    return _post({"type": "fundingHistory", "coin": coin, "startTime": start_ms})


def fetch_funding_history(coin: str, lookback_days: int = 7) -> list[dict]:
    """Fetch funding history for the last N days.

    Convenience wrapper around fetch_funding_since for callers that think in
    days rather than timestamps.
    """
    start_ms = int(time.time() * 1000) - (lookback_days * 24 * 60 * 60 * 1000)
    return fetch_funding_since(coin, start_ms)


def get_latest_candles(coin: str, interval: str, count: int = 200) -> list[dict]:
    """Fetch the most recent N closed candles without writing to disk.

    Used by the live loop for real-time signal evaluation.
    """
    minutes_per_candle = INTERVAL_MINUTES.get(interval, 15)
    lookback_minutes = count * minutes_per_candle * 2
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (lookback_minutes * 60 * 1000)
    return fetch_candles(coin, interval, start_ms, end_ms)[-count:]


# ── High-level accessors ──────────────────────────────────────────────────────


def fetch_and_save(coin: str, interval: str, lookback_days: int = 90) -> list[dict]:
    """Incrementally fetch candles and persist them to the Parquet lake.

    On first call performs a full backfill. Subsequent calls fetch only
    new candles since the last stored timestamp. Refreshes lake.duckdb
    so VS Code / DBeaver clients see the new data immediately.

    Returns the stored candles for the requested lookback window.
    """
    from data.ingest import ingest_candles
    from data.lake import CandleLake

    ingest_candles(coin, interval, lookback_days)
    db_path = CandleLake().to_duckdb()
    print(f"  DuckDB views refreshed → {db_path.name}")
    return load_candles(coin, interval, lookback_days=lookback_days)


def fetch_and_save_funding(coin: str, lookback_days: int = 90) -> dict[int, float]:
    """Incrementally fetch funding rates and persist them to the Parquet lake.

    Returns the full stored funding map for this coin.
    """
    from data.ingest import ingest_funding
    from data.lake import CandleLake

    ingest_funding(coin, lookback_days)
    CandleLake().to_duckdb()
    return load_funding_map(coin)


def load_candles(
    coin: str,
    interval: str,
    lookback_days: int | None = None,
) -> list[dict]:
    """Load stored candles from the Parquet lake.

    Returns candle dicts with the legacy keys (t, o, h, l, c, v, n) so the
    backtest engine and strategy code require no changes.

    Args:
        coin: Asset symbol.
        interval: Candle interval string.
        lookback_days: If set, return only candles from the last N days.
            Useful when the lake holds months of history but the backtest
            only needs a recent window.

    Raises:
        FileNotFoundError: If no data is stored for coin/interval yet.
    """
    from data.lake import CandleLake

    lake = CandleLake()

    start_ms: int | None = None
    if lookback_days is not None:
        start_ms = int(time.time() * 1000) - lookback_days * 86_400_000

    rows = lake.read_candles(coin, interval, start_ms=start_ms)
    if not rows:
        raise FileNotFoundError(
            f"No stored data for {coin}/{interval}. Run: python main.py --fetch"
        )

    return [
        {
            "t": r["timestamp"],
            "o": r["open"],
            "h": r["high"],
            "l": r["low"],
            "c": r["close"],
            "v": r["volume"],
            "n": r["num_trades"],
        }
        for r in rows
    ]


def load_funding_map(coin: str) -> dict[int, float]:
    """Load all stored funding rates for coin from the Parquet lake.

    Returns {hour_ms: rate} — the same format used by the backtest engine
    for its per-candle funding lookup.
    """
    from data.lake import CandleLake

    return CandleLake().read_funding_map(coin)
