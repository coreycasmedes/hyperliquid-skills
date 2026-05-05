"""Trade journal — protocol-based persistence layer.

JournalBackend is a Protocol so any backend (CSV, Polars, DuckDB) can be
swapped in without changing the backtest engine or live loop.

CSVLogger is the stdlib v1 implementation. compute_stats() is extracted as
a pure function so compare.py can reuse it without duplicating the math.
"""

import csv
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from execution.order_manager import Position
    from signals.strategy import Signal

CSV_COLUMNS = [
    "timestamp",
    "coin",
    "side",
    "entry",
    "exit",
    "stop",
    "liq_price",
    "pnl_usd",
    "pnl_pct",
    "exit_reason",
    "ema_fast",
    "ema_mid",
    "ema_slow",
    "atr_mult",
    "funding_rate",
    "config_version",
    "leverage",
]


@runtime_checkable
class JournalBackend(Protocol):
    """Minimal interface any trade logger must satisfy.

    The backtest engine and live loop depend only on this protocol.
    Swap the concrete implementation without touching either caller.
    """

    def log_trade(
        self,
        position: "Position",
        signal: "Signal",
        exit_price: float,
        exit_reason: str,
        config_version: int,
        atr_mult: float,
    ) -> None:
        """Record a completed trade."""
        ...

    def summary(self) -> dict:
        """Return aggregated performance statistics for all logged trades."""
        ...


def compute_stats(rows: list[dict]) -> dict:
    """Compute performance statistics from a list of trade row dicts.

    Pure function — no I/O. Used by CSVLogger.summary() and compare.py
    so stats are computed identically in both places.

    Args:
        rows: List of dicts matching CSV_COLUMNS (values may be strings).

    Returns:
        Dict with keys: total_trades, win_rate, total_pnl, avg_winner,
        avg_loser, profit_factor, max_drawdown, sharpe_ratio.
    """
    if not rows:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": None,
        }

    pnls = [float(r["pnl_usd"]) for r in rows]
    pnl_pcts = [float(r["pnl_pct"]) for r in rows]

    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    win_rate = len(winners) / len(pnls) * 100
    avg_winner = sum(winners) / len(winners) if winners else 0.0
    avg_loser = sum(losers) / len(losers) if losers else 0.0

    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown from running equity curve (absolute USD)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    # Sharpe ratio — trade-level, unannualized (requires >= 2 trades)
    sharpe: float | None = None
    if len(pnl_pcts) >= 2:
        mean_r = statistics.mean(pnl_pcts)
        std_r = statistics.stdev(pnl_pcts)
        sharpe = mean_r / std_r if std_r != 0 else None

    return {
        "total_trades": len(rows),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_winner": avg_winner,
        "avg_loser": avg_loser,
        "profit_factor": profit_factor,
        "max_drawdown": -max_dd,  # negative convention: -47.3 means a $47.30 drawdown
        "sharpe_ratio": sharpe,
    }


class CSVLogger:
    """CSV trade journal. Satisfies JournalBackend.

    In overwrite mode (backtest): truncates the file on each instantiation
    so every run produces a clean, self-contained result.
    In append mode (live/paper): adds rows to an existing journal without
    disturbing prior trades.
    """

    def __init__(self, filepath: Path, overwrite: bool = False):
        """Initialise the logger.

        Args:
            filepath: Full path to the CSV file.
            overwrite: If True, truncate any existing file and write a fresh
                header. Use for backtests. If False (default), create the
                file only when it does not yet exist — safe for live journals.
        """
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

        if overwrite or not self.filepath.exists():
            with open(self.filepath, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_COLUMNS).writeheader()

    def log_trade(
        self,
        position: "Position",
        signal: "Signal",
        exit_price: float,
        exit_reason: str,
        config_version: int,
        atr_mult: float,
    ) -> None:
        """Append one completed trade to the CSV.

        Computes pnl_usd and pnl_pct from position fields.

        Args:
            position: The closed Position dataclass.
            signal: The Signal that opened the trade (carries EMA/ATR/funding state).
            exit_price: Actual close price.
            exit_reason: Why the trade was closed (e.g. "atr_stop_hit").
            config_version: config.json version field at trade time.
            atr_mult: ATR stop multiplier from config at trade time.
        """
        from execution.order_manager import calc_trade_pnl

        pnl_usd = calc_trade_pnl(position, exit_price)
        margin = (position.entry_price * position.size_coins) / position.leverage
        pnl_pct = (pnl_usd / margin * 100) if margin > 0 else 0.0

        ts = datetime.fromtimestamp(
            position.entry_time_ms / 1000, tz=UTC
        ).isoformat()

        row = {
            "timestamp": ts,
            "coin": position.coin,
            "side": position.side,
            "entry": round(position.entry_price, 6),
            "exit": round(exit_price, 6),
            "stop": round(position.stop_price, 6),
            "liq_price": round(position.liq_price, 6),
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 4),
            "exit_reason": exit_reason,
            "ema_fast": round(signal.ema_fast, 6),
            "ema_mid": round(signal.ema_mid, 6),
            "ema_slow": round(signal.ema_slow, 6),
            "atr_mult": atr_mult,
            "funding_rate": signal.funding_rate,
            "config_version": config_version,
            "leverage": position.leverage,
        }

        with open(self.filepath, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_COLUMNS).writerow(row)

    def summary(self) -> dict:
        """Read all rows and return aggregated performance statistics.

        Returns:
            Dict from compute_stats(). Empty stats if no trades logged yet.
        """
        rows = self._read_rows()
        return compute_stats(rows)

    def _read_rows(self) -> list[dict]:
        """Read all CSV rows as a list of dicts."""
        if not self.filepath.exists():
            return []
        with open(self.filepath, newline="") as f:
            return list(csv.DictReader(f))
