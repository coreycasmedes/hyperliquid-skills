"""Fetch OHLCV candles and funding rates from the Hyperliquid public API.

No authentication required. All data is saved to data/ as JSON so the
backtest engine can run offline after an initial --fetch.
"""

import json
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


def _post(payload: dict) -> dict | list:
    """Send a POST request to the Hyperliquid info endpoint.

    Args:
        payload: JSON body to send.

    Returns:
        Parsed JSON response.

    Raises:
        requests.HTTPError: If the server returns a non-2xx status.
    """
    response = requests.post(API_URL, json=payload, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch a range of OHLCV candles from Hyperliquid.

    Args:
        coin: Asset symbol, e.g. "HYPE" or "BTC".
        interval: Candle interval string, e.g. "15m" or "1h".
        start_ms: Range start in Unix milliseconds.
        end_ms: Range end in Unix milliseconds.

    Returns:
        List of candle dicts with keys: t (open time ms), o, h, l, c, v, n.
        All price/volume fields are floats. Timestamps are ints.
    """
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    raw = _post(payload)

    candles = []
    for row in raw:
        candles.append(
            {
                "t": int(row["t"]),
                "o": float(row["o"]),
                "h": float(row["h"]),
                "l": float(row["l"]),
                "c": float(row["c"]),
                "v": float(row["v"]),
                "n": int(row["n"]),
            }
        )
    return candles


def fetch_and_save(coin: str, interval: str, lookback_days: int = 90) -> list[dict]:
    """Fetch candles for a lookback window and persist them to disk.

    Overwrites any existing file for this coin/interval pair.

    Args:
        coin: Asset symbol.
        interval: Candle interval.
        lookback_days: How many days of history to fetch (default 90).

    Returns:
        The fetched list of candle dicts.
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (lookback_days * 24 * 60 * 60 * 1000)

    print(f"Fetching {coin} {interval} candles ({lookback_days}d)...")
    candles = fetch_candles(coin, interval, start_ms, end_ms)

    out_path = DATA_DIR / f"candles_{coin}_{interval}.json"
    with open(out_path, "w") as f:
        json.dump(candles, f)

    print(f"  {len(candles)} candles → {out_path.name}")
    return candles


def load_candles(coin: str, interval: str) -> list[dict]:
    """Load previously saved candles from disk.

    Args:
        coin: Asset symbol.
        interval: Candle interval.

    Returns:
        List of candle dicts.

    Raises:
        FileNotFoundError: If no saved file exists — run with --fetch first.
    """
    path = DATA_DIR / f"candles_{coin}_{interval}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No saved data for {coin}/{interval}. Run: python main.py --fetch"
        )
    with open(path) as f:
        return json.load(f)


def get_latest_candles(coin: str, interval: str, count: int = 200) -> list[dict]:
    """Fetch the most recent N closed candles without writing to disk.

    Used by the live loop for real-time signal evaluation.

    Args:
        coin: Asset symbol.
        interval: Candle interval.
        count: Number of candles to return (fetches 2× buffer to ensure count).

    Returns:
        List of the most recent candle dicts, up to count length.
    """
    minutes_per_candle = INTERVAL_MINUTES.get(interval, 15)
    lookback_minutes = count * minutes_per_candle * 2

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (lookback_minutes * 60 * 1000)

    candles = fetch_candles(coin, interval, start_ms, end_ms)
    return candles[-count:]


def fetch_funding_history(coin: str, lookback_days: int = 7) -> list[dict]:
    """Fetch historical funding rate records for a coin.

    Args:
        coin: Asset symbol.
        lookback_days: How many days of history to fetch (default 7).

    Returns:
        List of funding records. Each record has keys:
        coin, fundingRate (str), premium (str), time (int ms).
    """
    start_ms = int(time.time() * 1000) - (lookback_days * 24 * 60 * 60 * 1000)

    payload = {
        "type": "fundingHistory",
        "coin": coin,
        "startTime": start_ms,
    }
    return _post(payload)
