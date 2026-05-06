"""Tests for signal generation logic.

The critical property tested here is no-lookahead bias: the channel is computed
from the N bars *before* the signal bar, never including it. A regression that
adds the signal bar to the window would cause the test_no_lookahead_long test
to fail (upper would become 11, close=11 would not be > 11 → no signal).
"""

import pytest

from signals.donchian import DonchianBreakout


def _c(o, h, l, c, t=0):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 0.0, "n": 0}


def _baseline(n, h=10.0, l=9.5, c=9.8):
    """N stable candles used to fill warmup history."""
    return [_c(o=10, h=h, l=l, c=c, t=i * 900_000) for i in range(n)]


def _donchian(**overrides):
    cfg = {
        "type": "donchian",
        "channel_period": 5,
        "atr_period": 3,
        "atr_stop_mult": 1.5,
        "funding_rate_max": 1.0,  # disable funding gate for unit tests
        "direction": "both",
        "max_hold_candles": 48,
        "min_adx": 0.0,
    }
    cfg.update(overrides)
    return DonchianBreakout("TEST", cfg)


class TestDonchianBreakout:
    # channel_period=5, atr_period=3 → warmup = max(5+3+2, 0) = 10
    # Need ≥ 11 candles to get a signal on index 10.

    def test_long_signal_on_breakout(self):
        # Prior 5 bars: high=10.  Signal bar close=11 > upper=10 → LONG
        candles = _baseline(10, h=10.0) + [_c(o=10, h=11, l=10, c=11)]
        sig = _donchian().generate_signal(candles, funding_rate=0.0)
        assert sig is not None
        assert sig.side == "long"

    def test_short_signal_on_breakout(self):
        # Prior 5 bars: low=9.  Signal bar close=8 < lower=9 → SHORT
        candles = _baseline(10, l=9.0, c=9.5) + [_c(o=9, h=9, l=8, c=8)]
        sig = _donchian().generate_signal(candles, funding_rate=0.0)
        assert sig is not None
        assert sig.side == "short"

    def test_no_signal_at_channel_boundary(self):
        # Close exactly at upper (not strictly above) → no signal
        candles = _baseline(10, h=10.0) + [_c(o=10, h=10, l=9.5, c=10.0)]
        sig = _donchian().generate_signal(candles, funding_rate=0.0)
        assert sig is None

    def test_no_signal_before_warmup(self):
        # Only 9 candles — warmup requires 10
        candles = _baseline(9)
        sig = _donchian().generate_signal(candles, funding_rate=0.0)
        assert sig is None

    def test_no_lookahead_long(self):
        """
        The channel is computed from candles[last-5 : last] — the signal bar
        (index 10) is excluded. Prior 5 bars have high=10, so upper=10.
        close=11 > 10 → signal fires.

        If the signal bar were included in the window:
          upper = max(10, 10, 10, 10, 10, 11) = 11
          close=11 is NOT > 11 → no signal (regression)

        This test would fail if someone changed `candles[last-n:last]` to
        `candles[last-n:last+1]`.
        """
        candles = _baseline(10, h=10.0) + [_c(o=10, h=11, l=10, c=11)]
        sig = _donchian().generate_signal(candles, funding_rate=0.0)
        assert sig is not None, "Lookahead regression: signal bar was included in the channel"

    def test_stop_price_set_below_entry_for_long(self):
        candles = _baseline(10, h=10.0) + [_c(o=10, h=11, l=10, c=11)]
        sig = _donchian().generate_signal(candles, funding_rate=0.0)
        assert sig.stop_price < sig.entry_price

    def test_stop_price_set_above_entry_for_short(self):
        candles = _baseline(10, l=9.0, c=9.5) + [_c(o=9, h=9, l=8, c=8)]
        sig = _donchian().generate_signal(candles, funding_rate=0.0)
        assert sig.stop_price > sig.entry_price

    def test_direction_long_only_blocks_short(self):
        strategy = _donchian(direction="long_only")
        candles = _baseline(10, l=9.0, c=9.5) + [_c(o=9, h=9, l=8, c=8)]
        sig = strategy.generate_signal(candles, funding_rate=0.0)
        assert sig is None

    def test_direction_short_only_blocks_long(self):
        strategy = _donchian(direction="short_only")
        candles = _baseline(10, h=10.0) + [_c(o=10, h=11, l=10, c=11)]
        sig = strategy.generate_signal(candles, funding_rate=0.0)
        assert sig is None

    def test_funding_gate_blocks_high_rate(self):
        strategy = _donchian(funding_rate_max=0.0001)
        candles = _baseline(10, h=10.0) + [_c(o=10, h=11, l=10, c=11)]
        sig = strategy.generate_signal(candles, funding_rate=0.001)
        assert sig is None

    def test_funding_gate_allows_low_rate(self):
        strategy = _donchian(funding_rate_max=0.0005)
        candles = _baseline(10, h=10.0) + [_c(o=10, h=11, l=10, c=11)]
        sig = strategy.generate_signal(candles, funding_rate=0.0003)
        assert sig is not None

    def test_signal_coin_matches_instance(self):
        candles = _baseline(10, h=10.0) + [_c(o=10, h=11, l=10, c=11)]
        sig = DonchianBreakout("HYPE", {
            "type": "donchian", "channel_period": 5, "atr_period": 3,
            "atr_stop_mult": 1.5, "funding_rate_max": 1.0,
            "direction": "both", "max_hold_candles": 48, "min_adx": 0.0,
        }).generate_signal(candles, funding_rate=0.0)
        assert sig.coin == "HYPE"
