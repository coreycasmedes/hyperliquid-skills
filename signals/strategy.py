"""ThreeEMACross strategy.

Generates entry signals when three EMAs align and a recent crossover confirms
the new trend direction. Filters stale signals, extreme funding rates, and
thin data. Provides an exit check method used by the order manager.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from data.indicators import calc_atr, calc_ema, calc_funding_rate

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


@dataclass
class Signal:
    """A confirmed entry signal ready to be evaluated by the RiskGate.

    Attributes:
        side: "long" or "short".
        entry_price: Close price of the signal candle.
        stop_price: Hard stop level derived from ATR.
        reason: Short string identifying the trigger, e.g. "3ema_cross_long".
        timestamp: Open-time (ms) of the candle that generated the signal.
        coin: Asset symbol.
        ema_fast: EMA-fast value at signal time (logged for journaling).
        ema_mid: EMA-mid value at signal time.
        ema_slow: EMA-slow value at signal time.
        atr: ATR value at signal time.
        funding_rate: Live funding rate at signal time.
    """

    side: str
    entry_price: float
    stop_price: float
    reason: str
    timestamp: int
    coin: str
    ema_fast: float
    ema_mid: float
    ema_slow: float
    atr: float
    funding_rate: float


class ThreeEMACross:
    """Three-EMA crossover strategy with ATR stops and funding-rate filter.

    Entry rules (all must be true):
      1. All three EMAs aligned: fast > mid > slow (long) or reverse (short).
      2. Fast/mid crossover occurred within the last `cross_lookback` closed candles.
      3. |funding_rate| <= funding_rate_max.

    Exit rules (first triggered wins):
      a. Hard stop: close price hits stop_price.
      b. EMA reversal: fast EMA crosses back through mid EMA.
      c. Timeout: position held for max_hold_candles without an exit.
    """

    def __init__(self, coin: str, config: Optional[dict] = None):
        """Load strategy parameters from config.json or a supplied dict.

        Args:
            coin: Asset symbol to trade (e.g. "HYPE").
            config: Strategy sub-dict. If None, reads from config.json["strategy"].
        """
        self.coin = coin

        if config is None:
            with open(CONFIG_PATH) as f:
                config = json.load(f).get("strategy", {})

        self.ema_fast: int = config.get("ema_fast", 8)
        self.ema_mid: int = config.get("ema_mid", 21)
        self.ema_slow: int = config.get("ema_slow", 50)
        self.atr_period: int = config.get("atr_period", 14)
        self.atr_stop_mult: float = config.get("atr_stop_mult", 1.5)
        self.funding_rate_max: float = config.get("funding_rate_max", 0.0005)
        self.direction: str = config.get("direction", "long_only")
        self.max_hold_candles: int = config.get("max_hold_candles", 48)
        self.cross_lookback: int = config.get("cross_lookback", 3)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_signal(self, candles: list[dict]) -> Optional[Signal]:
        """Evaluate the most recent closed candle and return a Signal if triggered.

        IMPORTANT: the last candle in `candles` must be fully closed before
        calling this method. Never pass a candle whose close time is in the
        future — the signal has not confirmed yet.

        Args:
            candles: List of closed OHLCV candle dicts (keys: t, o, h, l, c, v, n).

        Returns:
            Signal if all entry conditions are met, otherwise None.
        """
        min_required = self.ema_slow + self.cross_lookback + 2
        if len(candles) < min_required:
            return None

        closes = [c["c"] for c in candles]
        last = len(closes) - 1

        fast_series = calc_ema(closes, self.ema_fast)
        mid_series = calc_ema(closes, self.ema_mid)
        slow_series = calc_ema(closes, self.ema_slow)
        atr_series = calc_atr(candles, self.atr_period)

        fast = fast_series[last]
        mid = mid_series[last]
        slow = slow_series[last]
        atr = atr_series[last]

        if any(math.isnan(v) for v in [fast, mid, slow, atr]):
            return None

        # Funding rate filter — if the fetch fails, skip filtering rather than crash
        try:
            funding_rate = calc_funding_rate(self.coin)
        except Exception:
            funding_rate = 0.0

        if abs(funding_rate) > self.funding_rate_max:
            return None

        entry_price = closes[last]

        if self.direction in ("long_only", "both"):
            if self._aligned_long(fast, mid, slow):
                if self._cross_recent(fast_series, mid_series, last, "long"):
                    return Signal(
                        side="long",
                        entry_price=entry_price,
                        stop_price=entry_price - atr * self.atr_stop_mult,
                        reason="3ema_cross_long",
                        timestamp=candles[last]["t"],
                        coin=self.coin,
                        ema_fast=fast,
                        ema_mid=mid,
                        ema_slow=slow,
                        atr=atr,
                        funding_rate=funding_rate,
                    )

        if self.direction in ("short_only", "both"):
            if self._aligned_short(fast, mid, slow):
                if self._cross_recent(fast_series, mid_series, last, "short"):
                    return Signal(
                        side="short",
                        entry_price=entry_price,
                        stop_price=entry_price + atr * self.atr_stop_mult,
                        reason="3ema_cross_short",
                        timestamp=candles[last]["t"],
                        coin=self.coin,
                        ema_fast=fast,
                        ema_mid=mid,
                        ema_slow=slow,
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
    ) -> Optional[tuple[str, str]]:
        """Determine whether an open position should be exited.

        Called once per closed candle while a position is open.

        Args:
            candles: Full candle list used during the trade.
            entry_idx: Index of the candle where the trade was opened.
            current_idx: Index of the most recent closed candle.
            side: "long" or "short".
            stop_price: Hard stop level set at entry.

        Returns:
            (exit_type, reason) if an exit is triggered, else None.
            exit_type is one of: "stop", "ema_reversal", "timeout".
        """
        closes = [c["c"] for c in candles]
        current_close = closes[current_idx]

        # --- Hard stop ---
        if side == "long" and current_close <= stop_price:
            return ("stop", "atr_stop_hit")
        if side == "short" and current_close >= stop_price:
            return ("stop", "atr_stop_hit")

        # --- Timeout ---
        hold_candles = current_idx - entry_idx
        if hold_candles >= self.max_hold_candles:
            return ("timeout", f"held_{hold_candles}_candles")

        # --- EMA reversal (graceful exit) ---
        fast_series = calc_ema(closes, self.ema_fast)
        mid_series = calc_ema(closes, self.ema_mid)

        fast_now = fast_series[current_idx]
        mid_now = mid_series[current_idx]

        if not (math.isnan(fast_now) or math.isnan(mid_now)):
            if current_idx >= 1:
                fast_prev = fast_series[current_idx - 1]
                mid_prev = mid_series[current_idx - 1]
                if not (math.isnan(fast_prev) or math.isnan(mid_prev)):
                    if side == "long" and fast_prev >= mid_prev and fast_now < mid_now:
                        return ("ema_reversal", "fast_crossed_below_mid")
                    if side == "short" and fast_prev <= mid_prev and fast_now > mid_now:
                        return ("ema_reversal", "fast_crossed_above_mid")

        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _aligned_long(self, fast: float, mid: float, slow: float) -> bool:
        """Return True when EMAs are in bullish stack: fast > mid > slow."""
        return fast > mid > slow

    def _aligned_short(self, fast: float, mid: float, slow: float) -> bool:
        """Return True when EMAs are in bearish stack: fast < mid < slow."""
        return fast < mid < slow

    def _cross_recent(
        self,
        fast_series: list[float],
        mid_series: list[float],
        last_idx: int,
        side: str,
    ) -> bool:
        """Return True if a fast/mid crossover occurred in the last N candles.

        For "long": fast was <= mid on candle i-1, then > mid on candle i.
        For "short": fast was >= mid on candle i-1, then < mid on candle i.

        Prevents entering on an alignment that has been in place for many
        candles — those setups have already moved without us.

        Args:
            fast_series: Full EMA-fast series.
            mid_series: Full EMA-mid series.
            last_idx: Index of the last closed candle.
            side: "long" or "short".

        Returns:
            True if a qualifying cross is found within the lookback window.
        """
        start = max(1, last_idx - self.cross_lookback + 1)

        for i in range(start, last_idx + 1):
            prev_fast = fast_series[i - 1]
            prev_mid = mid_series[i - 1]
            curr_fast = fast_series[i]
            curr_mid = mid_series[i]

            if any(math.isnan(v) for v in [prev_fast, prev_mid, curr_fast, curr_mid]):
                continue

            if side == "long" and prev_fast <= prev_mid and curr_fast > curr_mid:
                return True

            if side == "short" and prev_fast >= prev_mid and curr_fast < curr_mid:
                return True

        return False
