"""Integration tests — require real candle data in the lake."""

import contextlib
import json
from io import StringIO
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent


class TestBacktestDeterminism:
    def test_same_candles_same_result(self, tmp_path):
        from backtest.engine import BacktestEngine
        from data.fetcher import load_candles
        from journal.logger import CSVLogger

        try:
            candles = load_candles("HYPE", "15m", lookback_days=90)
        except FileNotFoundError:
            pytest.skip("No HYPE 15m candle data in lake")

        with open(PROJECT_ROOT / "config.json") as f:
            config = json.load(f)

        def _run(path):
            logger = CSVLogger(path, overwrite=True)
            engine = BacktestEngine("HYPE", config, logger)
            with contextlib.redirect_stdout(StringIO()):
                return engine.run(candles)

        r1 = _run(tmp_path / "r1.csv")
        r2 = _run(tmp_path / "r2.csv")

        assert r1["total_trades"] == r2["total_trades"]
        assert r1["total_pnl"] == pytest.approx(r2["total_pnl"])
        assert r1["win_rate"] == pytest.approx(r2["win_rate"])
