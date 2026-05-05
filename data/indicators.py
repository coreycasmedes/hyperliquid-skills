"""Pure indicator functions for technical analysis.

All functions except calc_funding_rate take list inputs and return list
outputs with no side effects. Index alignment is preserved — every output
list has the same length as its input. Positions without enough history
are filled with float('nan').

calc_funding_rate is the one exception: it makes a live API call.
"""

import requests

API_URL = "https://api.hyperliquid.xyz/info"


def calc_ema(closes: list[float], period: int) -> list[float]:
    """Calculate Exponential Moving Average.

    Seeded with a simple average over the first `period` values.
    Uses smoothing factor k = 2 / (period + 1).

    Args:
        closes: List of closing prices.
        period: Lookback period.

    Returns:
        List of EMA values, same length as closes.
        Indices 0 through period-2 are NaN (insufficient history).
    """
    result = [float("nan")] * len(closes)

    if len(closes) < period:
        return result

    k = 2.0 / (period + 1)

    # Seed: SMA over the first `period` closes
    result[period - 1] = sum(closes[:period]) / period

    for i in range(period, len(closes)):
        result[i] = closes[i] * k + result[i - 1] * (1.0 - k)

    return result


def calc_atr(candles: list[dict], period: int) -> list[float]:
    """Calculate Average True Range using EMA smoothing.

    True Range at index i = max(
        high[i] - low[i],
        abs(high[i] - close[i-1]),
        abs(low[i]  - close[i-1]),
    )

    The first candle has no previous close so ATR[0] is always NaN.

    Args:
        candles: List of candle dicts with keys h, l, c (floats).
        period: ATR lookback period.

    Returns:
        List of ATR values, same length as candles.
        Indices 0 through period are NaN (first candle + EMA warmup).
    """
    if len(candles) < 2:
        return [float("nan")] * len(candles)

    # Build true range series; first element is always NaN (no prev close)
    true_ranges: list[float] = [float("nan")]
    for i in range(1, len(candles)):
        high = candles[i]["h"]
        low = candles[i]["l"]
        prev_close = candles[i - 1]["c"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    # EMA of the valid TR values (everything after the first NaN)
    valid_trs = true_ranges[1:]  # length: len(candles) - 1
    ema_of_trs = calc_ema(valid_trs, period)  # same length

    # Re-attach the leading NaN to restore index alignment
    return [float("nan")] + ema_of_trs


def calc_rsi(closes: list[float], period: int = 14) -> list[float]:
    """Calculate Relative Strength Index using Wilder's smoothing.

    Args:
        closes: List of closing prices.
        period: RSI lookback period (default 14).

    Returns:
        List of RSI values (0–100), same length as closes.
        Indices 0 through period-1 are NaN (insufficient history).
    """
    result = [float("nan")] * len(closes)

    if len(closes) < period + 1:
        return result

    # Price changes: changes[i] = closes[i+1] - closes[i]
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0.0, delta) for delta in changes]
    losses = [max(0.0, -delta) for delta in changes]

    # Seed with the simple average of the first `period` gain/loss values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi_from_averages(ag: float, al: float) -> float:
        if al == 0.0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + ag / al))

    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    # Wilder smoothing for all subsequent closes
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        result[i] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def calc_adx(candles: list[dict], period: int = 14) -> list[float]:
    """Calculate Average Directional Index using Wilder's smoothing.

    ADX measures trend *strength* (not direction). Values above 20-25 indicate
    a trending market; below 20 suggests a ranging/choppy market. Warmup
    requires 2*period candles before the first valid value.

    Args:
        candles: List of candle dicts with keys h, l, c (floats).
        period: Smoothing period (default 14).

    Returns:
        List of ADX values, same length as candles. Indices without
        sufficient history are NaN.
    """
    n = len(candles)
    result = [float("nan")] * n

    if n < 2 * period + 1:
        return result

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr_vals = [0.0] * n

    for i in range(1, n):
        high, low = candles[i]["h"], candles[i]["l"]
        prev_high, prev_low, prev_close = (
            candles[i - 1]["h"],
            candles[i - 1]["l"],
            candles[i - 1]["c"],
        )
        up = high - prev_high
        down = prev_low - low
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr_vals[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))

    # Wilder smoothing seed: simple sum of first `period` values (indices 1..period)
    s_tr = sum(tr_vals[1 : period + 1])
    s_plus = sum(plus_dm[1 : period + 1])
    s_minus = sum(minus_dm[1 : period + 1])

    # Collect (candle_index, dx) pairs to seed the ADX smooth
    dx_pairs: list[tuple[int, float]] = []

    for i in range(period + 1, n):
        s_tr = s_tr - s_tr / period + tr_vals[i]
        s_plus = s_plus - s_plus / period + plus_dm[i]
        s_minus = s_minus - s_minus / period + minus_dm[i]

        plus_di = 100.0 * s_plus / s_tr if s_tr else 0.0
        minus_di = 100.0 * s_minus / s_tr if s_tr else 0.0
        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0
        dx_pairs.append((i, dx))

    if len(dx_pairs) < period:
        return result

    # Seed ADX with average of first `period` DX values, then Wilder smooth
    adx = sum(v for _, v in dx_pairs[:period]) / period
    result[dx_pairs[period - 1][0]] = adx

    for i in range(period, len(dx_pairs)):
        idx, dx = dx_pairs[i]
        adx = (adx * (period - 1) + dx) / period
        result[idx] = adx

    return result


def calc_bollinger_bands(
    closes: list[float], period: int, std_mult: float = 2.0
) -> tuple[list[float], list[float], list[float]]:
    """Calculate Bollinger Bands (upper, middle, lower).

    Middle band is a simple moving average. Upper and lower bands are
    middle ± std_mult standard deviations (population std dev over the window).

    Args:
        closes: List of closing prices.
        period: Lookback period for SMA and std dev.
        std_mult: Number of standard deviations for band width (default 2.0).

    Returns:
        Tuple of (upper, middle, lower) lists, each the same length as closes.
        Indices 0 through period-2 are NaN (insufficient history).
    """
    import math as _math

    n = len(closes)
    upper = [float("nan")] * n
    middle = [float("nan")] * n
    lower = [float("nan")] * n

    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = _math.sqrt(variance)
        middle[i] = sma
        upper[i] = sma + std_mult * std
        lower[i] = sma - std_mult * std

    return upper, middle, lower


def calc_funding_rate(coin: str) -> float:
    """Fetch the current (latest) funding rate for a coin.

    Makes a live HTTP call — not a pure function. Use sparingly; cache
    the result if calling in a tight loop.

    Args:
        coin: Asset symbol, e.g. "HYPE" or "BTC".

    Returns:
        Latest funding rate as a float (e.g. 0.0001 = 0.01% per 8 h).

    Raises:
        ValueError: If the coin is not found in the Hyperliquid universe.
        requests.HTTPError: On API failure.
    """
    payload = {"type": "metaAndAssetCtxs"}
    response = requests.post(API_URL, json=payload, timeout=10)
    response.raise_for_status()
    data = response.json()

    meta = data[0]
    asset_ctxs = data[1]
    universe = meta.get("universe", [])

    coin_index = None
    for i, asset in enumerate(universe):
        if asset.get("name") == coin:
            coin_index = i
            break

    if coin_index is None:
        raise ValueError(f"Coin '{coin}' not found in Hyperliquid universe")

    return float(asset_ctxs[coin_index].get("funding", "0"))
