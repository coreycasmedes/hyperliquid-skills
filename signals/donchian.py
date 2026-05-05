"""Donchian Channel Breakout strategy.

Generates a long signal when price closes above the N-period high of the prior
N candles (excluding the signal bar). Generates a short signal when price
closes below the N-period low. Uses ATR for stop placement.

Entry rules (all must be true):
  1. Close breaks above (long) or below (short) the Donchian Channel computed
     from the prior channel_period bars — no lookahead.
  2. |funding_rate| <= funding_rate_max.

Exit rules (first triggered wins):
  a. Hard stop: close hits stop_price (intra-candle low/high checked by engine).
  b. Midline reversal: close crosses back through (upper + lower) / 2.
  c. Timeout: position held for max_hold_candles without an exit.
"""

import json
import math
from pathlib import Path

from data.indicators import calc_adx, calc_atr, calc_funding_rate
from signals.strategy import Signal

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


class DonchianBreakout:
    """Donchian Channel Breakout strategy with ATR stops."""

    def __init__(self, coin: str, config: dict | None = None):
        self.coin = coin

        if config is None:
            with open(CONFIG_PATH) as f:
                config = json.load(f).get("strategy", {})

        self.channel_period: int = config.get("channel_period", 20)
        self.atr_period: int = config.get("atr_period", 14)
        self.atr_stop_mult: float = config.get("atr_stop_mult", 1.5)
        self.funding_rate_max: float = config.get("funding_rate_max", 0.0005)
        self.direction: str = config.get("direction", "both")
        self.max_hold_candles: int = config.get("max_hold_candles", 48)
        self.min_adx: float = config.get("min_adx", 0.0)

    @property
    def warmup_candles(self) -> int:
        base = self.channel_period + self.atr_period + 2
        adx_warmup = 2 * self.atr_period + 2 if self.min_adx > 0 else 0
        return max(base, adx_warmup)

    def generate_signal(
        self,
        candles: list[dict],
        funding_rate: float | None = None,
    ) -> Signal | None:
        if len(candles) < self.warmup_candles:
            return None

        last = len(candles) - 1

        # Channel computed from prior bars only — exclude the signal bar
        prior = candles[last - self.channel_period : last]
        upper = max(c["h"] for c in prior)
        lower = min(c["l"] for c in prior)

        atr_series = calc_atr(candles, self.atr_period)
        atr = atr_series[last]

        if math.isnan(atr):
            return None

        if self.min_adx > 0:
            adx_series = calc_adx(candles, self.atr_period)
            adx = adx_series[last]
            if math.isnan(adx) or adx < self.min_adx:
                return None

        if funding_rate is None:
            try:
                funding_rate = calc_funding_rate(self.coin)
            except Exception:
                funding_rate = 0.0

        if abs(funding_rate) > self.funding_rate_max:
            return None

        entry_price = candles[last]["c"]

        if self.direction in ("long_only", "both") and entry_price > upper:
            return Signal(
                side="long",
                entry_price=entry_price,
                stop_price=entry_price - atr * self.atr_stop_mult,
                reason="donchian_breakout_long",
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
                reason="donchian_breakout_short",
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
        current_close = candles[current_idx]["c"]

        if side == "long" and current_close <= stop_price:
            return ("stop", "atr_stop_hit")
        if side == "short" and current_close >= stop_price:
            return ("stop", "atr_stop_hit")

        hold_candles = current_idx - entry_idx
        if hold_candles >= self.max_hold_candles:
            return ("timeout", f"held_{hold_candles}_candles")

        # Midline reversal — requires a full channel window before current bar
        channel_start = current_idx - self.channel_period
        if channel_start < 0:
            return None

        prior = candles[channel_start:current_idx]
        upper = max(c["h"] for c in prior)
        lower = min(c["l"] for c in prior)
        midline = (upper + lower) / 2

        if side == "long" and current_close < midline:
            return ("midline_reversal", "close_below_midline")
        if side == "short" and current_close > midline:
            return ("midline_reversal", "close_above_midline")

        return None
