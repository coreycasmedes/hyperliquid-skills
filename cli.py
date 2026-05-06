"""CLI entry point — argument parsing and mode dispatch.

The run logic lives in main.py; this module only handles the command surface.

Entry points registered in pyproject.toml:
    hl          → cli:main   (trading system)
    hl-compare  → scripts.compare:main
    hl-optimize → scripts.optimize:main
"""

import argparse
import json
import sys
from pathlib import Path

from data.fetcher import fetch_and_save, fetch_and_save_funding
from data.lake import CandleLake
from main import run_backtest, run_live_loop

PROJECT_ROOT = Path(__file__).parent
TRADES_DIR = PROJECT_ROOT / "trades"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hyperliquid perpetuals trading system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  backtest  Run the walk-forward engine on stored candles.
  paper     Live candle loop; orders logged to trades/journal.csv, never sent.
  live      Live candle loop; real orders via Hyperliquid SDK (requires --confirm).

Usage:
  uv run --env-file .env python main.py --mode backtest --coin HYPE
  uv run --env-file .env python main.py --mode paper   --coin HYPE
  uv run --env-file .env python main.py --mode live    --coin HYPE --confirm
  uv run --env-file .env python main.py --fetch --coin HYPE
""",
    )
    parser.add_argument(
        "--mode",
        choices=["backtest", "paper", "live"],
        default=None,
        help="Execution mode. Required unless using --fetch or --stats alone.",
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
    parser.add_argument(
        "--config",
        default="config.json",
        help="Config file to load (default: config.json).",
    )
    return parser


def main() -> None:
    """Parse CLI args and dispatch to the appropriate mode."""
    args = build_parser().parse_args()

    if args.stats:
        CandleLake().print_stats()
        sys.exit(0)

    config_path = PROJECT_ROOT / args.config
    with open(config_path) as f:
        config = json.load(f)

    TRADES_DIR.mkdir(exist_ok=True)

    if args.fetch:
        lookback = config.get("lookback_days", 90)
        fetch_and_save(args.coin, config["interval"], lookback)
        fetch_and_save_funding(args.coin, lookback)
        if args.mode is None:
            sys.exit(0)

    if args.mode is None:
        print("ERROR: --mode is required (backtest, paper, live). Use --fetch to update data only.")
        sys.exit(1)

    if args.mode == "live" and not args.confirm:
        print("ERROR: Live trading requires the --confirm flag.\n")
        print(
            "  uv run --env-file .env python main.py --mode live --coin HYPE --confirm"
        )
        sys.exit(1)

    if args.mode in ("paper", "live"):
        config["execution"]["mode"] = args.mode

    if args.mode == "backtest":
        run_backtest(args.coin, config)
    else:
        run_live_loop(args.coin, config)
