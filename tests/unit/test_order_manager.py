"""Tests for the financial math primitives in execution/order_manager.py.

These functions are the foundation of every P&L number in the system —
wrong here means wrong everywhere.
"""

import pytest
from execution.order_manager import Position, calc_liq_price, calc_trade_pnl


def _pos(side="long", entry=100.0, stop=None, liq=70.0, size=1.0):
    if stop is None:
        stop = 95.0 if side == "long" else 105.0
    return Position(
        coin="TEST",
        side=side,
        entry_price=entry,
        stop_price=stop,
        liq_price=liq,
        size_coins=size,
        entry_time_ms=0,
        leverage=3,
    )


class TestCalcLiqPrice:
    def test_long_known_value(self):
        # entry=100, size=1, lev=3, mmf=0.05
        # margin_available = 100/3 - 100*0.05 = 28.333...
        # liq = 100 - 28.333.../0.95 = 70.175...
        liq = calc_liq_price(100.0, 1.0, "long", 3, 0.05)
        assert abs(liq - 70.175) < 0.001

    def test_short_known_value(self):
        # liq = 100 + 28.333.../1.05 = 126.984...
        liq = calc_liq_price(100.0, 1.0, "short", 3, 0.05)
        assert abs(liq - 126.984) < 0.001

    def test_long_liq_is_below_entry(self):
        liq = calc_liq_price(100.0, 1.0, "long", 3, 0.05)
        assert liq < 100.0

    def test_short_liq_is_above_entry(self):
        liq = calc_liq_price(100.0, 1.0, "short", 3, 0.05)
        assert liq > 100.0

    def test_liq_independent_of_size(self):
        # Percentage distance from entry doesn't change with position size
        liq1 = calc_liq_price(100.0, 1.0, "long", 3, 0.05)
        liq5 = calc_liq_price(100.0, 5.0, "long", 3, 0.05)
        assert abs(liq1 - liq5) < 0.001

    def test_higher_leverage_brings_liq_closer(self):
        liq_3x = calc_liq_price(100.0, 1.0, "long", 3, 0.05)
        liq_10x = calc_liq_price(100.0, 1.0, "long", 10, 0.05)
        dist_3x = abs(liq_3x - 100.0)
        dist_10x = abs(liq_10x - 100.0)
        assert dist_3x > dist_10x

    def test_liq_far_from_entry_at_3x(self):
        # ATR stops (1–5% from entry) always fire before liq at 3x
        liq = calc_liq_price(100.0, 1.0, "long", 3, 0.05)
        pct_away = (100.0 - liq) / 100.0
        assert pct_away > 0.25


class TestCalcTradePnl:
    def test_long_win(self):
        assert calc_trade_pnl(_pos("long", entry=100), 110.0) == pytest.approx(10.0)

    def test_long_loss(self):
        assert calc_trade_pnl(_pos("long", entry=100), 90.0) == pytest.approx(-10.0)

    def test_long_breakeven(self):
        assert calc_trade_pnl(_pos("long", entry=100), 100.0) == pytest.approx(0.0)

    def test_short_win(self):
        assert calc_trade_pnl(_pos("short", entry=100), 90.0) == pytest.approx(10.0)

    def test_short_loss(self):
        assert calc_trade_pnl(_pos("short", entry=100), 110.0) == pytest.approx(-10.0)

    def test_size_scales_pnl(self):
        assert calc_trade_pnl(_pos("long", entry=100, size=3.0), 105.0) == pytest.approx(15.0)

    def test_short_breakeven(self):
        assert calc_trade_pnl(_pos("short", entry=100), 100.0) == pytest.approx(0.0)
