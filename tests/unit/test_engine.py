"""Tests for the backtest engine helpers.

_fill_price and _check_exit are the core simulation primitives — bugs here
introduce lookahead bias or miss stops entirely.
"""

import pytest

from backtest.engine import _check_exit, _fill_price
from execution.order_manager import Position


def _c(o, h, l, c, t=0):
    return {"o": o, "h": h, "l": l, "c": c, "t": t, "v": 0.0, "n": 0}


def _long_pos(entry=100.0, stop=90.0, liq=70.0):
    return Position(
        coin="TEST",
        side="long",
        entry_price=entry,
        stop_price=stop,
        liq_price=liq,
        size_coins=1.0,
        entry_time_ms=0,
        leverage=3,
    )


def _short_pos(entry=100.0, stop=110.0, liq=130.0):
    return Position(
        coin="TEST",
        side="short",
        entry_price=entry,
        stop_price=stop,
        liq_price=liq,
        size_coins=1.0,
        entry_time_ms=0,
        leverage=3,
    )


class _NoExit:
    def check_exit(self, **_):
        return None


class _FakeLongSignal:
    side = "long"
    entry_price = 100.0


class _FakeShortSignal:
    side = "short"
    entry_price = 100.0


class TestFillPrice:
    def test_long_gap_fill(self):
        # Candle opens below limit → fill at open (not at limit)
        assert _fill_price(_FakeLongSignal(), _c(o=98, h=103, l=97, c=101)) == 98

    def test_long_limit_fill(self):
        # Open above limit, low dips to it → fill at limit exactly
        assert _fill_price(_FakeLongSignal(), _c(o=102, h=104, l=99, c=103)) == 100.0

    def test_long_no_fill(self):
        # Low never reaches the limit → order expires
        assert _fill_price(_FakeLongSignal(), _c(o=105, h=107, l=101, c=106)) is None

    def test_short_gap_fill(self):
        # Candle opens above limit → fill at open
        assert _fill_price(_FakeShortSignal(), _c(o=102, h=104, l=99, c=101)) == 102

    def test_short_limit_fill(self):
        # Open below limit, high reaches it → fill at limit
        assert _fill_price(_FakeShortSignal(), _c(o=98, h=101, l=96, c=99)) == 100.0

    def test_short_no_fill(self):
        # High never reaches the limit
        assert _fill_price(_FakeShortSignal(), _c(o=96, h=99, l=94, c=97)) is None

    def test_long_fill_at_limit_not_below_when_open_above(self):
        # Open=101 (above limit=100), low=99 (below limit) → fill at limit, not at low
        fill = _fill_price(_FakeLongSignal(), _c(o=101, h=104, l=99, c=102))
        assert fill == pytest.approx(100.0)


class TestCheckExit:
    # ── Liquidation (Stage 1a) ──────────────────────────────────────────────

    def test_liq_fires_long_gap_below_liq(self):
        # Candle gaps entirely below liq → exit at max(open, liq)
        pos = _long_pos(entry=100, stop=90, liq=70)
        candle = _c(o=60, h=100, l=55, c=62)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        assert result is not None
        exit_type, reason, price = result
        assert exit_type == "liquidation"
        assert reason == "margin_call"
        assert price == pytest.approx(70.0)  # max(60, 70) = 70

    def test_liq_long_open_above_liq(self):
        # Candle opens above liq but low dips below → exit at open (max wins)
        pos = _long_pos(entry=100, stop=90, liq=70)
        candle = _c(o=75, h=100, l=65, c=72)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        assert result[0] == "liquidation"
        assert result[2] == pytest.approx(75.0)  # max(75, 70)

    def test_liq_fires_short_gap_above_liq(self):
        pos = _short_pos(entry=100, stop=110, liq=130)
        candle = _c(o=140, h=145, l=100, c=142)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        exit_type, reason, price = result
        assert exit_type == "liquidation"
        assert reason == "margin_call"
        assert price == pytest.approx(130.0)  # min(140, 130)

    def test_liq_fires_before_atr_stop_when_both_triggered(self):
        # Candle gaps below both liq (70) and stop (90) — liquidation must win
        pos = _long_pos(entry=100, stop=90, liq=70)
        candle = _c(o=60, h=100, l=55, c=62)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        assert result[0] == "liquidation"

    # ── ATR stop (Stage 1b) ─────────────────────────────────────────────────

    def test_atr_stop_long_clean(self):
        # Open above stop, low touches stop → fill at stop price
        pos = _long_pos(entry=100, stop=90, liq=70)
        candle = _c(o=95, h=100, l=88, c=92)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        exit_type, reason, price = result
        assert exit_type == "stop"
        assert reason == "atr_stop_hit"
        assert price == pytest.approx(90.0)

    def test_atr_stop_long_gap(self):
        # Open below stop → gap fill at open (slippage)
        pos = _long_pos(entry=100, stop=90, liq=70)
        candle = _c(o=85, h=95, l=83, c=87)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        assert result[0] == "stop"
        assert result[2] == pytest.approx(85.0)  # open

    def test_atr_stop_short_clean(self):
        pos = _short_pos(entry=100, stop=110, liq=130)
        candle = _c(o=105, h=112, l=103, c=107)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        assert result[0] == "stop"
        assert result[2] == pytest.approx(110.0)

    def test_atr_stop_short_gap(self):
        # Open above stop → gap fill at open
        pos = _short_pos(entry=100, stop=110, liq=130)
        candle = _c(o=115, h=120, l=110, c=118)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        assert result[0] == "stop"
        assert result[2] == pytest.approx(115.0)

    # ── No exit ─────────────────────────────────────────────────────────────

    def test_no_exit_within_bounds(self):
        pos = _long_pos(entry=100, stop=90, liq=70)
        candle = _c(o=99, h=102, l=97, c=101)
        assert _check_exit([candle], 0, pos, None, _NoExit()) is None

    def test_no_exit_at_stop_exact_long(self):
        # Low exactly at stop — the condition is <=, so this DOES trigger
        pos = _long_pos(entry=100, stop=90, liq=70)
        candle = _c(o=95, h=100, l=90, c=93)
        result = _check_exit([candle], 0, pos, None, _NoExit())
        assert result is not None
        assert result[0] == "stop"
