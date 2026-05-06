"""Hyperliquid algorithmic trading system — run logic.

Contains the two execution modes (backtest and live/paper loop) and their
supporting helpers. CLI argument parsing and dispatch live in cli.py.

Direct invocation still works:
    uv run --env-file .env python main.py --mode backtest --coin HYPE
"""

import random
import sys
import time
from pathlib import Path

from backtest.engine import BacktestEngine
from data.fetcher import INTERVAL_MINUTES, get_latest_candles, load_candles
from data.lake import CandleLake
from execution.order_manager import OrderManager, calc_trade_pnl
from journal.logger import CSVLogger
from risk.gate import PortfolioState, RiskGate
from signals import get_strategy

PROJECT_ROOT = Path(__file__).parent
TRADES_DIR = PROJECT_ROOT / "trades"
KILL_FILE = PROJECT_ROOT / "KILL"


def _check_kill_switch() -> None:
    """Exit immediately if a file named KILL exists in the project root."""
    if KILL_FILE.exists():
        print("\n[KILL SWITCH] KILL file detected — stopping immediately.")
        sys.exit(0)


def _candle_is_closed(candle: dict, interval: str) -> bool:
    """Return True if the candle's close time is in the past."""
    interval_ms = INTERVAL_MINUTES[interval] * 60 * 1000
    return candle["t"] + interval_ms <= int(time.time() * 1000)


def run_backtest(coin: str, config: dict) -> None:
    """Load candles from the lake and run the walk-forward backtest engine."""
    lookback = config.get("lookback_days", 90)
    candles = load_candles(coin, config["interval"], lookback_days=lookback)
    print(f"Loaded {len(candles)} candles for {coin} {config['interval']}")
    engine = BacktestEngine(coin, config)
    engine.run(candles)


def run_live_loop(coin: str, config: dict) -> None:
    """Fetch fresh candles in a loop, generate signals, manage positions.

    Operates in whichever mode is set in config["execution"]["mode"].
    The loop never acts on a candle that has not fully closed, and never
    processes the same candle twice.
    """
    mode = config["execution"]["mode"]
    interval = config["interval"]

    strategy = get_strategy(coin, config["strategy"])
    gate = RiskGate(config["risk"])
    manager = OrderManager(config)
    logger = CSVLogger(TRADES_DIR / "journal.csv")

    capital = config["risk"]["capital_per_trade"]
    portfolio = PortfolioState(
        starting_equity=capital * 10,
        current_equity=capital * 10,
        peak_equity=capital * 10,
    )

    open_positions: list = []
    last_candle_t: int = 0

    print(f"[{mode.upper()}] {coin} {interval} — live loop started.")
    print("  Drop a file named KILL in the project root to stop cleanly.\n")

    while True:
        _check_kill_switch()

        candles = get_latest_candles(coin, interval, count=200)

        if not _candle_is_closed(candles[-1], interval):
            time.sleep(random.uniform(25, 35))
            continue

        if candles[-1]["t"] == last_candle_t:
            time.sleep(random.uniform(25, 35))
            continue

        last_candle_t = candles[-1]["t"]

        exits = manager.check_exits(candles, open_positions, strategy)
        for ex in exits:
            result = manager.close_position(ex.position, ex.reason, ex.exit_price)
            if result.success:
                pnl = calc_trade_pnl(ex.position, ex.exit_price)
                portfolio.record_closed_trade(pnl)
                open_positions = [p for p in open_positions if p is not ex.position]
                portfolio.open_positions = open_positions

                signal = getattr(ex.position, "_signal", None)
                if signal is not None:
                    logger.log_trade(
                        position=ex.position,
                        signal=signal,
                        exit_price=ex.exit_price,
                        exit_reason=ex.reason,
                        config_version=config.get("version", 1),
                        atr_mult=config["strategy"].get("atr_stop_mult", 1.5),
                    )

        signal = strategy.generate_signal(candles)
        if signal is not None:
            approved, reason = gate.approve(signal, portfolio)
            if approved:
                size = gate.position_size_coins(signal.entry_price)
                position, result = manager.open_position(signal, size)
                if result.success and position is not None:
                    position._signal = signal
                    open_positions.append(position)
                    portfolio.open_positions = open_positions
            else:
                print(f"  [{mode.upper()}] Signal rejected: {reason}")

        time.sleep(random.uniform(55, 65))


if __name__ == "__main__":
    from cli import main
    main()
