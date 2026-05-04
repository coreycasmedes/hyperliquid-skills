"""Hyperliquid algorithmic trading system — CLI entry point.

Modes:
  backtest  Run the walk-forward engine on stored candles.
  paper     Live candle loop; orders logged to trades/journal.csv, never sent.
  live      Live candle loop; real orders via Hyperliquid SDK (requires --confirm).

Usage:
  uv run --env-file .env python main.py --mode backtest --coin HYPE
  uv run --env-file .env python main.py --mode paper   --coin HYPE
  uv run --env-file .env python main.py --mode live    --coin HYPE --confirm
  uv run --env-file .env python main.py --fetch --coin HYPE        (data only)
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

from backtest.engine import BacktestEngine
from data.fetcher import (
    INTERVAL_MINUTES,
    fetch_and_save,
    fetch_and_save_funding,
    get_latest_candles,
    load_candles,
)
from data.lake import CandleLake
from execution.order_manager import OrderManager, calc_trade_pnl
from journal.logger import CSVLogger
from risk.gate import PortfolioState, RiskGate
from signals.strategy import ThreeEMACross

PROJECT_ROOT = Path(__file__).parent
TRADES_DIR = PROJECT_ROOT / "trades"
KILL_FILE = PROJECT_ROOT / "KILL"


# ── Safety helpers ─────────────────────────────────────────────────────────────


def _check_kill_switch() -> None:
    """Exit immediately if a file named KILL exists in the project root."""
    if KILL_FILE.exists():
        print("\n[KILL SWITCH] KILL file detected — stopping immediately.")
        sys.exit(0)


def _candle_is_closed(candle: dict, interval: str) -> bool:
    """Return True if the candle's close time is in the past.

    Args:
        candle: Candle dict with key 't' (open time in ms).
        interval: Candle interval string (e.g. "15m").
    """
    interval_ms = INTERVAL_MINUTES[interval] * 60 * 1000
    return candle["t"] + interval_ms <= int(time.time() * 1000)


# ── Modes ──────────────────────────────────────────────────────────────────────


def run_backtest(coin: str, config: dict) -> None:
    """Load candles from disk and run the walk-forward backtest engine.

    Args:
        coin: Asset symbol to backtest.
        config: Full config dict.
    """
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

    Args:
        coin: Asset symbol to trade.
        config: Full config dict (mode already set by CLI arg).
    """
    mode = config["execution"]["mode"]
    interval = config["interval"]

    strategy = ThreeEMACross(coin, config["strategy"])
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

        # Wait for the current candle to close before acting on it
        if not _candle_is_closed(candles[-1], interval):
            time.sleep(random.uniform(25, 35))
            continue

        # Skip candles we've already processed
        if candles[-1]["t"] == last_candle_t:
            time.sleep(random.uniform(25, 35))
            continue

        last_candle_t = candles[-1]["t"]

        # ── Check exits ───────────────────────────────────────────────────
        exits = manager.check_exits(candles, open_positions, strategy)
        for ex in exits:
            result = manager.close_position(ex.position, ex.reason, ex.exit_price)
            if result.success:
                pnl = calc_trade_pnl(ex.position, ex.exit_price)
                portfolio.record_closed_trade(pnl)
                open_positions = [p for p in open_positions if p is not ex.position]
                portfolio.open_positions = open_positions

                # Signal was attached to position at open time for deferred logging
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

        # ── Generate and act on signal ────────────────────────────────────
        signal = strategy.generate_signal(candles)
        if signal is not None:
            approved, reason = gate.approve(signal, portfolio)
            if approved:
                size = gate.position_size_coins(signal.entry_price)
                position, result = manager.open_position(signal, size)
                if result.success and position is not None:
                    # Attach signal for deferred journal logging at close time
                    position._signal = signal
                    open_positions.append(position)
                    portfolio.open_positions = open_positions
            else:
                print(f"  [{mode.upper()}] Signal rejected: {reason}")

        # Vary the sleep slightly each iteration — no predictable timing
        time.sleep(random.uniform(55, 65))


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Parse CLI args and dispatch to the appropriate mode."""
    parser = argparse.ArgumentParser(
        description="Hyperliquid perpetuals trading system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["backtest", "paper", "live"],
        default="paper",
        help="Execution mode (default: paper)",
    )
    parser.add_argument(
        "--coin",
        default="HYPE",
        choices=["HYPE", "BTC", "ETH", "SOL"],
        help="Asset to trade (default: HYPE)",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Refresh candle data from the Hyperliquid API before running",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required alongside --mode live. Acknowledges real money is at risk.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show what's stored in the Parquet lake and exit.",
    )

    args = parser.parse_args()

    if args.stats:
        CandleLake().print_stats()
        sys.exit(0)

    # ── Live mode safety gate ─────────────────────────────────────────────
    if args.mode == "live" and not args.confirm:
        print("ERROR: Live trading requires the --confirm flag.\n")
        print(
            "  uv run --env-file .env python main.py --mode live --coin HYPE --confirm"
        )
        sys.exit(1)

    with open(PROJECT_ROOT / "config.json") as f:
        config = json.load(f)

    # CLI mode overrides config so you don't have to edit the file for paper runs
    if args.mode in ("paper", "live"):
        config["execution"]["mode"] = args.mode

    TRADES_DIR.mkdir(exist_ok=True)

    if args.fetch:
        lookback = config.get("lookback_days", 90)
        fetch_and_save(args.coin, config["interval"], lookback)
        fetch_and_save_funding(args.coin, lookback)

    if args.mode == "backtest":
        run_backtest(args.coin, config)
    else:
        run_live_loop(args.coin, config)


if __name__ == "__main__":
    main()
