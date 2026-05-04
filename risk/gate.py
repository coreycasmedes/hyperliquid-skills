"""Risk gate — evaluates whether a signal should proceed to execution.

All checks are fail-safe: when in doubt, reject. Every rejection includes
a reason string so the journal can record why a signal was skipped.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from signals.strategy import Signal

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


@dataclass
class PortfolioState:
    """Snapshot of portfolio health passed to RiskGate.approve() each cycle.

    Attributes:
        open_positions: List of currently open Position objects.
        starting_equity: Account equity at session start (USDC). Used to
            compute daily loss percentage.
        current_equity: Current account equity (USDC).
        peak_equity: Highest equity seen this session. Used to compute
            rolling drawdown.
        daily_realized_pnl: Sum of closed-trade P&L since session start (USDC).
            Negative means a loss.
    """

    open_positions: list = field(default_factory=list)
    starting_equity: float = 1000.0
    current_equity: float = 1000.0
    peak_equity: float = 1000.0
    daily_realized_pnl: float = 0.0

    def record_closed_trade(self, pnl_usd: float) -> None:
        """Update equity and P&L after a trade closes.

        Args:
            pnl_usd: Realized P&L of the closed trade (negative = loss).
        """
        self.daily_realized_pnl += pnl_usd
        self.current_equity += pnl_usd
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from peak equity as a percentage."""
        if self.peak_equity <= 0:
            return 0.0
        return ((self.peak_equity - self.current_equity) / self.peak_equity) * 100

    @property
    def daily_loss_pct(self) -> float:
        """Session loss as a percentage of starting equity (positive = loss)."""
        if self.starting_equity <= 0:
            return 0.0
        return -(self.daily_realized_pnl / self.starting_equity) * 100


class RiskGate:
    """Approve or reject entry signals based on portfolio risk constraints.

    Checks run in a fixed priority order. The first failure short-circuits
    the rest so the reason is always specific.
    """

    def __init__(self, config: Optional[dict] = None):
        """Load risk parameters from config.json or a supplied dict.

        Args:
            config: Risk sub-dict. If None, reads config.json["risk"].
        """
        if config is None:
            with open(CONFIG_PATH) as f:
                config = json.load(f).get("risk", {})

        self.max_open_positions: int = config.get("max_open_positions", 1)
        self.max_daily_loss_pct: float = config.get("max_daily_loss_pct", 5.0)
        self.max_drawdown_pct: float = config.get("max_drawdown_pct", 15.0)
        self.capital_per_trade: float = config.get("capital_per_trade", 100.0)
        self.leverage: int = config.get("leverage", 3)

    def approve(self, signal: "Signal", state: PortfolioState) -> tuple[bool, str]:
        """Check whether a signal should proceed to execution.

        Args:
            signal: Entry signal from the strategy.
            state: Current portfolio snapshot.

        Returns:
            (True, "approved") if all checks pass.
            (False, reason) on the first failed check.
        """
        # 1. Hard position cap
        if len(state.open_positions) >= self.max_open_positions:
            return (
                False,
                f"position_limit: {len(state.open_positions)}/{self.max_open_positions} open",
            )

        # 2. No doubling into the same coin
        open_coins = {p.coin for p in state.open_positions}
        if signal.coin in open_coins:
            return False, f"duplicate_coin: already in {signal.coin}"

        # 3. Daily loss ceiling
        if state.daily_loss_pct >= self.max_daily_loss_pct:
            return (
                False,
                f"daily_loss_limit: {state.daily_loss_pct:.1f}% >= {self.max_daily_loss_pct}%",
            )

        # 4. Drawdown ceiling
        if state.drawdown_pct >= self.max_drawdown_pct:
            return (
                False,
                f"drawdown_limit: {state.drawdown_pct:.1f}% >= {self.max_drawdown_pct}%",
            )

        return True, "approved"

    def position_size_coins(self, entry_price: float) -> float:
        """Calculate position size in coin units.

        Notional = capital_per_trade * leverage
        Size     = notional / entry_price

        Args:
            entry_price: Intended entry price (USDC per coin).

        Returns:
            Position size in coin units (e.g. 7.16 for HYPE).
        """
        notional_usd = self.capital_per_trade * self.leverage
        return notional_usd / entry_price
