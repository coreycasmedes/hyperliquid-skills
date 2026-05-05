"""Volatility Expansion Breakout (Bollinger Band Squeeze) strategy.

Detects periods of low volatility ("squeeze") where the Bollinger Band width
has been compressed below a threshold. When price subsequently breaks out
above the upper band (long) or below the lower band (short), it signals a
volatility expansion move.

BB width = (upper - lower) / middle

Entry rules (all must be true):
  1. BB width < squeeze_threshold for the prior squeeze_lookback candles
     (the market was in a squeeze before the signal bar).
  2. Close breaks above upper BB (long) or below lower BB (short) on the
     signal bar.
  3. |funding_rate| <= funding_rate_max.

Exit rules (first triggered wins):
  a. Hard stop: close hits stop_price (intra-candle low/high checked by engine).
  b. Middle-band reversal: close crosses back through the SMA middle band.
  c. Timeout: position held for max_hold_candles without an exit.
"""

import json
import math
from pathlib import Path

from data.indicators import calc_adx, calc_atr, calc_bollinger_bands, calc_funding_rate
from signals.strategy import Signal

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


class VolatilityExpansion:
    """Bollinger Band squeeze breakout strategy with ATR stops."""

    def __init__(self, coin: str, config: dict | None = None):
        self.coin = coin

        if config is None:
            with open(CONFIG_PATH) as f:
                config = json.load(f).get("strategy", {})

        self.bb_period: int = config.get("bb_period", 20)
        self.bb_std_mult: float = config.get("bb_std_mult", 2.0)
        self.squeeze_lookback: int = config.get("squeeze_lookback", 5)
        self.squeeze_threshold: float = config.get("squeeze_threshold", 0.06)
        self.atr_period: int = config.get("atr_period", 14)
        self.atr_stop_mult: float = config.get("atr_stop_mult", 1.5)
        self.funding_rate_max: float = config.get("funding_rate_max", 0.0005)
        self.direction: str = config.get("direction", "both")
        self.max_hold_candles: int = config.get("max_hold_candles", 48)
        self.min_adx: float = config.get("min_adx", 0.0)

    @property
    def warmup_candles(self) -> int:
        base = max(self.bb_period, self.atr_period) + self.squeeze_lookback + 2
        adx_warmup = 2 * self.atr_period + 2 if self.min_adx > 0 else 0
        return max(base, adx_warmup)

    def generate_signal(
        self,
        candles: list[dict],
        funding_rate: float | None = None,
    ) -> Signal | None:
        if len(candles) < self.warmup_candles:
            return None

        closes = [c["c"] for c in candles]
        last = len(closes) - 1

        upper_bb, middle_bb, lower_bb = calc_bollinger_bands(
            closes, self.bb_period, self.bb_std_mult
        )
        atr_series = calc_atr(candles, self.atr_period)

        atr = atr_series[last]
        upper = upper_bb[last]
        mid = middle_bb[last]
        lower = lower_bb[last]

        if any(math.isnan(v) for v in [atr, upper, mid, lower]) or mid == 0:
            return None

        if self.min_adx > 0:
            adx_series = calc_adx(candles, self.atr_period)
            adx = adx_series[last]
            if math.isnan(adx) or adx < self.min_adx:
                return None

        # Squeeze condition: prior squeeze_lookback bars (not including signal bar)
        # must all have BB width below the threshold
        squeeze_end = last - 1
        squeeze_start = squeeze_end - self.squeeze_lookback + 1
        if squeeze_start < 0:
            return None

        for j in range(squeeze_start, squeeze_end + 1):
            u, m, lo = upper_bb[j], middle_bb[j], lower_bb[j]
            if math.isnan(u) or math.isnan(m) or math.isnan(lo) or m == 0:
                return None
            if (u - lo) / m >= self.squeeze_threshold:
                return None  # not in squeeze

        if funding_rate is None:
            try:
                funding_rate = calc_funding_rate(self.coin)
            except Exception:
                funding_rate = 0.0

        if abs(funding_rate) > self.funding_rate_max:
            return None

        entry_price = closes[last]

        if self.direction in ("long_only", "both") and entry_price > upper:
            return Signal(
                side="long",
                entry_price=entry_price,
                stop_price=entry_price - atr * self.atr_stop_mult,
                reason="vol_expansion_long",
                timestamp=candles[last]["t"],
                coin=self.coin,
                atr=atr,
                funding_rate=funding_rate,
            )

        if self.direction in ("short_only", "both") and entry_price < lower:
            return Signal(
                side="short",
                entry_price=entry_price,
                stop_price=entry_price + atr * self.atr_stop_mult,
                reason="vol_expansion_short",
                timestamp=candles[last]["t"],
                coin=self.coin,
                atr=atr,
                funding_rate=funding_rate,
            )

        return None

    def check_exit(
        self,
        candles: list[dict],
        entry_idx: int,
        current_idx: int,
        side: str,
        stop_price: float,
    ) -> tuple[str, str] | None:
        closes = [c["c"] for c in candles]
        current_close = closes[current_idx]

        if side == "long" and current_close <= stop_price:
            return ("stop", "atr_stop_hit")
        if side == "short" and current_close >= stop_price:
            return ("stop", "atr_stop_hit")

        hold_candles = current_idx - entry_idx
        if hold_candles >= self.max_hold_candles:
            return ("timeout", f"held_{hold_candles}_candles")

        # Middle-band reversal
        upper_bb, middle_bb, lower_bb = calc_bollinger_bands(
            closes[: current_idx + 1], self.bb_period, self.bb_std_mult
        )
        mid = middle_bb[-1]

        if not math.isnan(mid):
            if side == "long" and current_close < mid:
                return ("midband_reversal", "close_below_midband")
            if side == "short" and current_close > mid:
                return ("midband_reversal", "close_above_midband")

        return None
