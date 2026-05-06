"""Parameter grid search for any strategy config.

Runs the backtest engine across a parameter grid entirely in memory — no
CSV files written. Prints results ranked by profit factor.

Usage:
    uv run python scripts/optimize.py --coin HYPE --config config_donchian.json
    uv run python scripts/optimize.py --coin HYPE --config config_volatility.json
    uv run python scripts/optimize.py --coin HYPE --config config.json
"""

import argparse
import copy
import json
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING

from backtest.engine import BacktestEngine
from data.fetcher import load_candles
from journal.logger import JournalBackend, compute_stats

if TYPE_CHECKING:
    from execution.order_manager import Position
    from signals.strategy import Signal

PROJECT_ROOT = Path(__file__).parent


# ── In-memory logger ──────────────────────────────────────────────────────────


class _MemLogger:
    """Satisfies JournalBackend — stores trades in a list, never touches disk."""

    def __init__(self):
        self._rows: list[dict] = []

    def log_trade(
        self,
        position: "Position",
        signal: "Signal",
        exit_price: float,
        exit_reason: str,
        config_version: int,
        atr_mult: float,
    ) -> None:
        from execution.order_manager import calc_trade_pnl

        pnl_usd = calc_trade_pnl(position, exit_price)
        margin = (position.entry_price * position.size_coins) / position.leverage
        pnl_pct = (pnl_usd / margin * 100) if margin > 0 else 0.0
        self._rows.append({"pnl_usd": pnl_usd, "pnl_pct": pnl_pct})

    def summary(self) -> dict:
        return compute_stats(self._rows)


assert isinstance(_MemLogger(), JournalBackend)


# ── Per-strategy param grids ──────────────────────────────────────────────────

_GRIDS: dict[str, list[tuple[str, list]]] = {
    "three_ema_cross": [
        ("atr_stop_mult", [1.0, 1.5, 2.0, 2.5]),
        ("min_adx", [0, 15, 20, 25]),
        ("direction", ["long_only", "both"]),
    ],
    "donchian": [
        ("channel_period", [10, 20, 40]),
        ("atr_stop_mult", [1.0, 1.5, 2.0, 2.5]),
        ("min_adx", [0, 15, 20, 25]),
        ("direction", ["long_only", "both"]),
    ],
    "volatility_expansion": [
        ("squeeze_threshold", [0.04, 0.06, 0.08, 0.10]),
        ("atr_stop_mult", [1.0, 1.5, 2.0, 2.5]),
        ("min_adx", [0, 15, 20, 25]),
        ("direction", ["long_only", "both"]),
    ],
}


# ── Grid runner ───────────────────────────────────────────────────────────────


def run_grid(coin: str, base_config: dict, top_n: int = 15) -> None:
    strategy_type = base_config["strategy"].get("type", "three_ema_cross")
    grid_spec = _GRIDS.get(strategy_type)
    if grid_spec is None:
        print(f"No grid defined for strategy type '{strategy_type}'.")
        return

    lookback = base_config.get("lookback_days", 90)
    candles = load_candles(coin, base_config["interval"], lookback_days=lookback)
    print(f"Loaded {len(candles)} candles  ·  {coin} {base_config['interval']}")

    param_names = [name for name, _ in grid_spec]
    value_lists = [vals for _, vals in grid_spec]
    combos = list(product(*value_lists))

    print(f"Strategy: {strategy_type}  ·  {len(combos)} combinations\n")

    results: list[tuple[dict, dict]] = []

    for combo in combos:
        params = dict(zip(param_names, combo))
        cfg = copy.deepcopy(base_config)
        cfg["strategy"].update(params)

        logger = _MemLogger()
        engine = BacktestEngine(coin, cfg, logger=logger)

        import io
        import sys as _sys

        buf = io.StringIO()
        old_stdout = _sys.stdout
        _sys.stdout = buf
        try:
            engine.run(candles)
        finally:
            _sys.stdout = old_stdout

        stats = logger.summary()
        results.append((params, stats))

    results.sort(
        key=lambda x: (
            x[1]["profit_factor"] if x[1]["profit_factor"] != float("inf") else 999,
            x[1]["total_pnl"],
        ),
        reverse=True,
    )

    _print_results(results[:top_n], param_names, strategy_type, coin, base_config["interval"])


# ── Output ────────────────────────────────────────────────────────────────────


def _print_results(
    results: list[tuple[dict, dict]],
    param_names: list[str],
    strategy_type: str,
    coin: str,
    interval: str,
) -> None:
    param_cols = "  ".join(f"{n:<18}" for n in param_names)
    metrics = f"  {'trades':>6}  {'win%':>6}  {'P&L':>9}  {'PF':>6}  {'Sharpe':>7}"
    header = f"{'#':<4}  {param_cols}{metrics}"
    sep = "─" * len(header)

    print(f"TOP RESULTS  ·  {strategy_type}  {coin} {interval}")
    print(sep)
    print(header)
    print(sep)

    for rank, (params, stats) in enumerate(results, 1):
        param_str = "  ".join(f"{str(params[n]):<18}" for n in param_names)
        trades = stats["total_trades"]
        win = stats["win_rate"]
        pnl = stats["total_pnl"]
        pf = stats["profit_factor"]
        sr = stats["sharpe_ratio"]

        pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
        sr_str = f"{sr:.2f}" if sr is not None else "N/A"

        print(
            f"{rank:<4}  {param_str}  {trades:>6}  {win:>5.1f}%  "
            f"{pnl:>+9.2f}  {pf_str:>6}  {sr_str:>7}"
        )

    print(sep)
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Parameter grid search for strategy configs")
    parser.add_argument("--coin", default="HYPE", choices=["HYPE", "BTC", "ETH", "SOL"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--top", type=int, default=15, help="Number of top results to show")
    args = parser.parse_args()

    with open(PROJECT_ROOT / args.config) as f:
        config = json.load(f)

    run_grid(args.coin, config, top_n=args.top)


if __name__ == "__main__":
    main()
