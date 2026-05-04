"""Walk-forward backtest engine.

Simulates the strategy and risk gate over stored candles with no lookahead.
Uses the same ThreeEMACross and RiskGate classes as the live loop — the only
difference is that order execution is simulated internally rather than sent
to the exchange.

Accepts any JournalBackend so the persistence layer can be swapped without
touching the engine.
"""

import json
from datetime import date
from pathlib import Path

from data.fetcher import load_funding_map
from execution.order_manager import Position, calc_trade_pnl
from journal.logger import CSVLogger, JournalBackend
from risk.gate import PortfolioState, RiskGate
from signals.strategy import Signal, ThreeEMACross

PROJECT_ROOT = Path(__file__).parent.parent
TRADES_DIR = PROJECT_ROOT / "trades"


class BacktestEngine:
    """Walk-forward simulation over historical candles.

    Entry signals fire at the close of candle N. The limit order is simulated
    as filling at candle N+1 (next open/low). This mirrors live behaviour where
    you place the order after the signal candle closes.

    Stop-loss checks use the candle's low/high (intra-candle) rather than just
    the close, so stops that were touched mid-candle are not missed.
    """

    def __init__(
        self,
        coin: str,
        config: dict | None = None,
        logger: JournalBackend | None = None,
    ):
        """Initialise the engine.

        Args:
            coin: Asset symbol to simulate (e.g. "HYPE").
            config: Full config dict. If None, reads config.json.
            logger: Any JournalBackend. Defaults to CSVLogger writing to
                trades/backtest_{coin}_{date}_v{version}.csv.
        """
        if config is None:
            with open(PROJECT_ROOT / "config.json") as f:
                config = json.load(f)

        self.coin = coin
        self.config = config
        self.interval = config["interval"]

        self.strategy = ThreeEMACross(coin, config["strategy"])
        self.gate = RiskGate(config["risk"])

        self.atr_mult: float = config["strategy"].get("atr_stop_mult", 1.5)
        self.leverage: int = config["risk"].get("leverage", 3)
        self.capital: float = config["risk"].get("capital_per_trade", 100.0)

        if logger is None:
            today = date.today().isoformat()
            version = config.get("version", 1)
            TRADES_DIR.mkdir(exist_ok=True)
            filepath = TRADES_DIR / f"backtest_{coin}_{today}_v{version}.csv"
            logger = CSVLogger(filepath)

        self.logger: JournalBackend = logger

    def run(self, candles: list[dict]) -> dict:
        """Simulate the strategy over the full candle history.

        Args:
            candles: List of closed OHLCV candle dicts loaded from disk.

        Returns:
            Summary stats dict from logger.summary().
        """
        min_required = self.strategy.ema_slow + self.strategy.cross_lookback + 2

        portfolio = PortfolioState(
            starting_equity=self.capital * 10,
            current_equity=self.capital * 10,
            peak_equity=self.capital * 10,
        )

        open_position: Position | None = None
        open_position_entry_idx: int | None = None  # candle index of fill
        pending_signal: Signal | None = None  # signal queued for next open
        entry_signal: Signal | None = None  # signal held until trade closes

        funding_map = load_funding_map(self.coin)
        if not funding_map:
            print(
                f"  Warning: no funding data for {self.coin} — using 0.0 for all candles. "
                "Run with --fetch to download funding history."
            )

        print(
            f"Running backtest: {self.coin} {self.interval}  "
            f"({len(candles)} candles, warmup={min_required})"
        )

        for i in range(min_required, len(candles)):
            # ── 1. Fill pending entry at this candle ──────────────────────
            if pending_signal is not None:
                fill = _fill_price(pending_signal, candles[i])
                if fill is not None:
                    open_position = Position(
                        coin=self.coin,
                        side=pending_signal.side,
                        entry_price=fill,
                        stop_price=pending_signal.stop_price,
                        size_coins=self.gate.position_size_coins(fill),
                        entry_time_ms=candles[i]["t"],
                        leverage=self.leverage,
                    )
                    open_position_entry_idx = i
                    portfolio.open_positions = [open_position]
                    entry_signal = pending_signal
                pending_signal = None

            # ── 2. Check exits on the open position ───────────────────────
            if open_position is not None:
                result = _check_exit(
                    candles=candles,
                    i=i,
                    position=open_position,
                    entry_idx=open_position_entry_idx,
                    strategy=self.strategy,
                )
                if result is not None:
                    exit_type, reason, exit_price = result
                    pnl = calc_trade_pnl(open_position, exit_price)
                    portfolio.record_closed_trade(pnl)
                    self.logger.log_trade(
                        position=open_position,
                        signal=entry_signal,
                        exit_price=exit_price,
                        exit_reason=reason,
                        config_version=self.config.get("version", 1),
                        atr_mult=self.atr_mult,
                    )
                    open_position = None
                    open_position_entry_idx = None
                    entry_signal = None
                    portfolio.open_positions = []

            # ── 3. Generate signal if flat ─────────────────────────────────
            if open_position is None and pending_signal is None:
                funding_rate = _lookup_funding(funding_map, candles[i]["t"])
                signal = self.strategy.generate_signal(
                    candles[: i + 1], funding_rate=funding_rate
                )
                if signal is not None:
                    approved, _ = self.gate.approve(signal, portfolio)
                    if approved:
                        pending_signal = signal

        # ── Close any position still open at end of history ───────────────
        if open_position is not None and entry_signal is not None:
            exit_price = candles[-1]["c"]
            pnl = calc_trade_pnl(open_position, exit_price)
            portfolio.record_closed_trade(pnl)
            self.logger.log_trade(
                position=open_position,
                signal=entry_signal,
                exit_price=exit_price,
                exit_reason="end_of_data",
                config_version=self.config.get("version", 1),
                atr_mult=self.atr_mult,
            )

        summary = self.logger.summary()
        _print_summary(summary, self.coin, self.interval, self.config.get("version", 1))
        return summary


# ── Module-level helpers ───────────────────────────────────────────────────────


def _lookup_funding(funding_map: dict, candle_t: int) -> float:
    """Return the funding rate for the hour containing candle_t.

    Floors candle_t to the nearest hour boundary (same key format used when
    the funding map was built). Falls back to 0.0 when the hour is absent,
    which happens if funding history doesn't cover that period.
    """
    hour_ms = (candle_t // 3_600_000) * 3_600_000
    return funding_map.get(hour_ms, 0.0)


def _fill_price(signal: Signal, candle: dict) -> float | None:
    """Simulate a limit entry order filling at the given candle.

    Long limit buy:
      - Candle opens at or below limit → fill at open (favourable gap)
      - Candle trades down to limit (low <= limit) → fill at limit
      - Candle never reaches limit → no fill (signal expires)

    Short limit sell: mirror logic using open/high.

    Args:
        signal: The entry signal with entry_price as the limit price.
        candle: The candle immediately after the signal candle.

    Returns:
        Fill price as a float, or None if the limit was not touched.
    """
    limit = signal.entry_price

    if signal.side == "long":
        if candle["o"] <= limit:
            return candle["o"]  # gap fill at open
        if candle["l"] <= limit:
            return limit  # traded to limit
        return None

    else:  # short
        if candle["o"] >= limit:
            return candle["o"]  # gap fill at open
        if candle["h"] >= limit:
            return limit  # traded to limit
        return None


def _check_exit(
    candles: list[dict],
    i: int,
    position: Position,
    entry_idx: int | None,
    strategy: ThreeEMACross,
) -> tuple[str, str, float] | None:
    """Two-stage exit check for a single candle.

    Stage 1 — Intra-candle hard stop:
      Uses candle low/high so stops touched mid-candle are not missed.
      If the candle gaps through the stop, the fill is at the open (gap slippage).

    Stage 2 — EMA reversal + timeout:
      Delegates to strategy.check_exit() which uses close prices.
      Only reached if the hard stop did not trigger.

    Args:
        candles: Full candle list.
        i: Index of the current candle being evaluated.
        position: The open position.
        entry_idx: Candle index at which the position was filled.
        strategy: Strategy instance for EMA/timeout checks.

    Returns:
        (exit_type, reason, exit_price) if an exit is triggered, else None.
    """
    candle = candles[i]
    stop = position.stop_price

    # Stage 1: intra-candle stop using low / high
    if position.side == "long" and candle["l"] <= stop:
        # Gap below stop → fill at open, otherwise fill at stop
        exit_price = candle["o"] if candle["o"] < stop else stop
        return ("stop", "atr_stop_hit", exit_price)

    if position.side == "short" and candle["h"] >= stop:
        exit_price = candle["o"] if candle["o"] > stop else stop
        return ("stop", "atr_stop_hit", exit_price)

    # Stage 2: EMA reversal / timeout via strategy (close-price based)
    if entry_idx is None:
        return None

    result = strategy.check_exit(
        candles=candles,
        entry_idx=entry_idx,
        current_idx=i,
        side=position.side,
        stop_price=stop,
    )

    if result is not None:
        exit_type, reason = result
        return (exit_type, reason, candle["c"])

    return None


def _print_summary(summary: dict, coin: str, interval: str, version: int) -> None:
    """Print a formatted performance summary table to stdout."""
    width = 42

    def _fmt_usd(v: float | None) -> str:
        if v is None:
            return "N/A"
        return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

    def _fmt_pct(v: float | None) -> str:
        return f"{v:.1f}%" if v is not None else "N/A"

    def _fmt_factor(v: float | None) -> str:
        if v is None:
            return "N/A"
        return "∞" if v == float("inf") else f"{v:.2f}"

    sr = summary.get("sharpe_ratio")
    sharpe_str = f"{sr:.2f}" if sr is not None else "N/A"

    print("\n" + "═" * width)
    print(f"  BACKTEST  {coin}  {interval}  config v{version}")
    print("═" * width)
    print(f"  Total trades      {summary['total_trades']}")
    print(f"  Win rate          {_fmt_pct(summary['win_rate'])}")
    print(f"  Total P&L         {_fmt_usd(summary['total_pnl'])}")
    print(f"  Avg winner        {_fmt_usd(summary['avg_winner'])}")
    print(f"  Avg loser         {_fmt_usd(summary['avg_loser'])}")
    print(f"  Profit factor     {_fmt_factor(summary['profit_factor'])}")
    print(f"  Max drawdown      {_fmt_usd(summary['max_drawdown'])}")
    print(f"  Sharpe (trade)    {sharpe_str}")
    print("═" * width + "\n")
