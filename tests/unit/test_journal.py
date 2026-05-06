"""Tests for journal/logger.py.

compute_stats is a pure function — every stat is verified against hand-computed
values so a formula change is immediately visible. CSVLogger tests confirm the
overwrite semantics that guarantee backtest determinism.
"""

import statistics

import pytest

from execution.order_manager import Position
from journal.logger import CSVLogger, compute_stats
from signals.strategy import Signal

# Hand-computed fixture
# pnls      = [10, -5, 20, -3]
# winners   = [10, 20]  losers = [-5, -3]
# equity    = 10 → 5 → 25 → 22
# peak      = 10 → 10 → 25 → 25
# max_dd    = 5  (at trade 2)  → returned as -5.0
ROWS = [
    {"pnl_usd": "10", "pnl_pct": "5.0"},
    {"pnl_usd": "-5", "pnl_pct": "-2.5"},
    {"pnl_usd": "20", "pnl_pct": "10.0"},
    {"pnl_usd": "-3", "pnl_pct": "-1.5"},
]


class TestComputeStats:
    def test_empty(self):
        s = compute_stats([])
        assert s["total_trades"] == 0
        assert s["win_rate"] == 0.0
        assert s["total_pnl"] == 0.0
        assert s["sharpe_ratio"] is None

    def test_total_trades(self):
        assert compute_stats(ROWS)["total_trades"] == 4

    def test_win_rate(self):
        assert compute_stats(ROWS)["win_rate"] == pytest.approx(50.0)

    def test_total_pnl(self):
        assert compute_stats(ROWS)["total_pnl"] == pytest.approx(22.0)

    def test_avg_winner(self):
        assert compute_stats(ROWS)["avg_winner"] == pytest.approx(15.0)

    def test_avg_loser(self):
        assert compute_stats(ROWS)["avg_loser"] == pytest.approx(-4.0)

    def test_profit_factor(self):
        # gross_profit=30, gross_loss=8 → 30/8 = 3.75
        assert compute_stats(ROWS)["profit_factor"] == pytest.approx(3.75)

    def test_max_drawdown(self):
        # equity: 10→5→25→22, max peak-to-trough = 5 → returned negative
        assert compute_stats(ROWS)["max_drawdown"] == pytest.approx(-5.0)

    def test_sharpe_matches_formula(self):
        pnl_pcts = [5.0, -2.5, 10.0, -1.5]
        expected = statistics.mean(pnl_pcts) / statistics.stdev(pnl_pcts)
        assert compute_stats(ROWS)["sharpe_ratio"] == pytest.approx(expected, rel=1e-6)

    def test_all_winners(self):
        rows = [{"pnl_usd": "5", "pnl_pct": "2"}, {"pnl_usd": "10", "pnl_pct": "5"}]
        s = compute_stats(rows)
        assert s["win_rate"] == 100.0
        assert s["profit_factor"] == float("inf")
        assert s["avg_loser"] == 0.0
        assert s["max_drawdown"] == 0.0

    def test_all_losers(self):
        rows = [{"pnl_usd": "-5", "pnl_pct": "-2"}, {"pnl_usd": "-3", "pnl_pct": "-1"}]
        s = compute_stats(rows)
        assert s["win_rate"] == 0.0
        assert s["profit_factor"] == 0.0
        assert s["avg_winner"] == 0.0

    def test_single_trade_sharpe_is_none(self):
        rows = [{"pnl_usd": "10", "pnl_pct": "5"}]
        assert compute_stats(rows)["sharpe_ratio"] is None

    def test_drawdown_is_zero_with_monotone_gains(self):
        rows = [
            {"pnl_usd": "5", "pnl_pct": "2"},
            {"pnl_usd": "10", "pnl_pct": "5"},
            {"pnl_usd": "3", "pnl_pct": "1"},
        ]
        assert compute_stats(rows)["max_drawdown"] == 0.0


def _make_pos():
    return Position(
        coin="TEST",
        side="long",
        entry_price=100.0,
        stop_price=90.0,
        liq_price=70.0,
        size_coins=1.0,
        entry_time_ms=0,
        leverage=3,
    )


def _make_sig():
    return Signal(
        side="long",
        entry_price=100.0,
        stop_price=90.0,
        reason="test",
        timestamp=0,
        coin="TEST",
        atr=1.0,
        funding_rate=0.0,
    )


class TestCSVLogger:
    def test_overwrite_clears_prior_trades(self, tmp_path):
        filepath = tmp_path / "trades.csv"
        pos, sig = _make_pos(), _make_sig()

        # First run: 2 trades
        logger1 = CSVLogger(filepath, overwrite=True)
        logger1.log_trade(pos, sig, 105.0, "timeout", 1, 1.5)
        logger1.log_trade(pos, sig, 95.0, "atr_stop_hit", 1, 1.5)

        # Second run with overwrite=True: must start fresh
        logger2 = CSVLogger(filepath, overwrite=True)
        logger2.log_trade(pos, sig, 108.0, "timeout", 1, 1.5)

        assert logger2.summary()["total_trades"] == 1

    def test_append_accumulates(self, tmp_path):
        filepath = tmp_path / "trades.csv"
        pos, sig = _make_pos(), _make_sig()

        logger1 = CSVLogger(filepath, overwrite=True)
        logger1.log_trade(pos, sig, 105.0, "timeout", 1, 1.5)

        logger2 = CSVLogger(filepath, overwrite=False)
        logger2.log_trade(pos, sig, 108.0, "timeout", 1, 1.5)

        assert logger2.summary()["total_trades"] == 2

    def test_pnl_computed_correctly(self, tmp_path):
        filepath = tmp_path / "trades.csv"
        pos, sig = _make_pos(), _make_sig()

        logger = CSVLogger(filepath, overwrite=True)
        logger.log_trade(pos, sig, 110.0, "timeout", 1, 1.5)  # +$10 on 1 coin

        s = logger.summary()
        assert s["total_pnl"] == pytest.approx(10.0)
        assert s["win_rate"] == 100.0
