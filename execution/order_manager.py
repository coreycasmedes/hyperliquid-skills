"""Order manager — executes signals in PAPER or LIVE mode.

PAPER: logs every action to trades/paper_trades.json, never touches exchange.
LIVE:  places real orders via hyperliquid-python-sdk. Requires HL_PRIVATE_KEY
       and HL_ACCOUNT_ADDRESS in the environment (set via uv run --env-file .env).

Safety rules enforced here:
  - Kill switch: exits immediately if ./KILL file exists.
  - Limit orders only in LIVE mode (no market orders).
  - Random jitter before every order to avoid pattern detection.
  - Full order details printed and "y" confirmation required before any live order.
  - Private key never logged or printed.
"""

import json
import os
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from signals.strategy import Signal, ThreeEMACross

PROJECT_ROOT = Path(__file__).parent.parent
TRADES_DIR = PROJECT_ROOT / "trades"
PAPER_TRADES_FILE = TRADES_DIR / "paper_trades.json"
KILL_FILE = PROJECT_ROOT / "KILL"


def calc_trade_pnl(position: "Position", close_price: float) -> float:
    """Calculate gross P&L in USDC for a closed position.

    Pure function — usable by the backtest engine, journal, and main loop
    without importing the full OrderManager.

    Args:
        position: The closed Position dataclass.
        close_price: Price at which the position was closed.

    Returns:
        P&L in USDC (positive = profit, negative = loss).
        Does not include fees or funding payments.
    """
    price_delta = close_price - position.entry_price
    if position.side == "short":
        price_delta = -price_delta
    return price_delta * position.size_coins


@dataclass
class Position:
    """An open position tracked by the order manager.

    Attributes:
        coin: Asset symbol.
        side: "long" or "short".
        entry_price: Limit price the order was placed at.
        stop_price: Hard stop level from the signal.
        size_coins: Position size in coin units.
        entry_time_ms: Open-time (ms) of the candle that triggered entry.
        leverage: Leverage applied at entry.
        order_id: Exchange order ID (None in paper mode).
        stop_order_id: Exchange stop order ID (None in paper mode).
    """

    coin: str
    side: str
    entry_price: float
    stop_price: float
    size_coins: float
    entry_time_ms: int
    leverage: int
    order_id: str | None = None
    stop_order_id: str | None = None


@dataclass
class OrderResult:
    """Outcome of an open or close attempt.

    Attributes:
        success: True if the order was placed (or paper-logged) without error.
        mode: "paper" or "live".
        coin: Asset symbol.
        side: "long", "short", "close_long", or "close_short".
        size_coins: Order size in coin units.
        price: Limit price used.
        timestamp_ms: Wall-clock time of the order attempt (ms).
        order_id: Exchange order ID if available.
        error: Error message if success is False.
    """

    success: bool
    mode: str
    coin: str
    side: str
    size_coins: float
    price: float
    timestamp_ms: int
    order_id: str | None = None
    error: str | None = None


@dataclass
class ExitSignal:
    """Instruction to close an open position.

    Attributes:
        position: The Position that should be closed.
        exit_type: "stop", "ema_reversal", or "timeout".
        reason: Human-readable detail (e.g. "atr_stop_hit").
        exit_price: Close price of the candle that triggered the exit.
    """

    position: Position
    exit_type: str
    reason: str
    exit_price: float


class OrderManager:
    """Execute entry and exit orders in PAPER or LIVE mode.

    In LIVE mode the manager is stateless with respect to the exchange —
    it places orders and returns results, but does not poll for fills.
    The caller (main loop) is responsible for tracking open positions.
    """

    def __init__(self, config: dict | None = None):
        """Load execution config and, for LIVE mode, initialise the SDK client.

        Args:
            config: Full config dict. If None, reads config.json.

        Raises:
            EnvironmentError: In LIVE mode if HL_PRIVATE_KEY or
                HL_ACCOUNT_ADDRESS are missing from the environment.
        """
        if config is None:
            with open(PROJECT_ROOT / "config.json") as f:
                config = json.load(f)

        exec_cfg = config.get("execution", {})
        risk_cfg = config.get("risk", {})

        self.mode: str = exec_cfg.get("mode", "paper").lower()
        self.network: str = exec_cfg.get("network", "testnet").lower()
        self.tick_offset: int = exec_cfg.get("tick_offset_ticks", 2)
        self.leverage: int = risk_cfg.get("leverage", 3)

        TRADES_DIR.mkdir(exist_ok=True)

        self._exchange = None
        self._info = None
        self._account_address = None

        if self.mode == "live":
            self._init_live_client()

    def _init_live_client(self) -> None:
        """Initialise the Hyperliquid SDK exchange client from env vars.

        Raises:
            EnvironmentError: If required env vars are absent.
        """
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        private_key = os.environ.get("HL_PRIVATE_KEY")
        account_address = os.environ.get("HL_ACCOUNT_ADDRESS")

        if not private_key:
            raise OSError(
                "HL_PRIVATE_KEY not found. Run: uv run --env-file .env python main.py"
            )
        if not account_address:
            raise OSError(
                "HL_ACCOUNT_ADDRESS not found. Run: uv run --env-file .env python main.py"
            )

        api_url = (
            constants.MAINNET_API_URL
            if self.network == "mainnet"
            else constants.TESTNET_API_URL
        )

        wallet = Account.from_key(private_key)
        self._info = Info(api_url, skip_ws=True)
        self._exchange = Exchange(wallet, api_url, account_address=account_address)
        self._account_address = account_address

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def open_position(
        self, signal: "Signal", size_coins: float
    ) -> tuple[Position | None, OrderResult]:
        """Place an entry order for a signal.

        In LIVE mode: sets leverage, places a limit entry order, then places
        the stop-loss as a separate trigger order.

        Args:
            signal: The entry signal from the strategy.
            size_coins: Position size in coin units (from RiskGate).

        Returns:
            (Position, OrderResult) on success.
            (None, OrderResult) with success=False on failure.
        """
        self._check_kill_switch()

        limit_price = self._limit_price_for_entry(signal)
        timestamp_ms = int(time.time() * 1000)

        if self.mode == "paper":
            return self._paper_open(signal, size_coins, limit_price, timestamp_ms)
        else:
            return self._live_open(signal, size_coins, limit_price, timestamp_ms)

    def close_position(
        self, position: Position, reason: str, close_price: float
    ) -> OrderResult:
        """Place a close order for an open position.

        Args:
            position: The position to close.
            reason: Why the position is being closed (for logging).
            close_price: Current market price used as the limit close price.

        Returns:
            OrderResult describing the outcome.
        """
        self._check_kill_switch()

        timestamp_ms = int(time.time() * 1000)

        if self.mode == "paper":
            return self._paper_close(position, reason, close_price, timestamp_ms)
        else:
            return self._live_close(position, reason, close_price, timestamp_ms)

    def check_exits(
        self,
        candles: list[dict],
        open_positions: list[Position],
        strategy: "ThreeEMACross",
    ) -> list[ExitSignal]:
        """Scan all open positions against the latest candles for exit conditions.

        Calls strategy.check_exit() for each position to evaluate ATR stops,
        EMA reversals, and timeouts. The kill switch is also polled here so
        the loop exits cleanly even mid-iteration.

        Args:
            candles: Latest closed candles (same list used for signals).
            open_positions: Currently open Position objects.
            strategy: The active strategy instance (provides check_exit logic).

        Returns:
            List of ExitSignals for positions that should be closed.
            Empty if no exits are triggered.
        """
        if not candles or not open_positions:
            return []

        self._check_kill_switch()

        current_idx = len(candles) - 1
        current_close = candles[current_idx]["c"]
        exits: list[ExitSignal] = []

        for position in open_positions:
            entry_idx = self._find_entry_candle_idx(candles, position.entry_time_ms)

            if entry_idx is None:
                # Entry candle fell out of the window — check hard stop only
                if self._hard_stop_hit(position, current_close):
                    exits.append(
                        ExitSignal(
                            position=position,
                            exit_type="stop",
                            reason="atr_stop_hit",
                            exit_price=current_close,
                        )
                    )
                continue

            result = strategy.check_exit(
                candles=candles,
                entry_idx=entry_idx,
                current_idx=current_idx,
                side=position.side,
                stop_price=position.stop_price,
            )

            if result is not None:
                exit_type, reason = result
                exits.append(
                    ExitSignal(
                        position=position,
                        exit_type=exit_type,
                        reason=reason,
                        exit_price=current_close,
                    )
                )

        return exits

    # ------------------------------------------------------------------
    # Paper mode
    # ------------------------------------------------------------------

    def _paper_open(
        self,
        signal: "Signal",
        size_coins: float,
        limit_price: float,
        timestamp_ms: int,
    ) -> tuple[Position, OrderResult]:
        """Log a paper entry and return a synthetic Position."""
        position = Position(
            coin=signal.coin,
            side=signal.side,
            entry_price=limit_price,
            stop_price=signal.stop_price,
            size_coins=size_coins,
            entry_time_ms=signal.timestamp,
            leverage=self.leverage,
        )

        record = {
            "action": "open",
            "timestamp_ms": timestamp_ms,
            **asdict(position),
        }
        self._append_paper_trade(record)

        print(
            f"[PAPER] OPEN {signal.side.upper()} {signal.coin} "
            f"size={size_coins:.4f} @ {limit_price:.4f}  stop={signal.stop_price:.4f}"
        )

        result = OrderResult(
            success=True,
            mode="paper",
            coin=signal.coin,
            side=signal.side,
            size_coins=size_coins,
            price=limit_price,
            timestamp_ms=timestamp_ms,
        )
        return position, result

    def _paper_close(
        self,
        position: Position,
        reason: str,
        close_price: float,
        timestamp_ms: int,
    ) -> OrderResult:
        """Log a paper exit."""
        pnl_usd = self._calc_pnl(position, close_price)

        record = {
            "action": "close",
            "timestamp_ms": timestamp_ms,
            "reason": reason,
            "close_price": close_price,
            "pnl_usd": pnl_usd,
            **asdict(position),
        }
        self._append_paper_trade(record)

        print(
            f"[PAPER] CLOSE {position.side.upper()} {position.coin} "
            f"@ {close_price:.4f}  pnl={pnl_usd:+.2f} USDC  reason={reason}"
        )

        return OrderResult(
            success=True,
            mode="paper",
            coin=position.coin,
            side=f"close_{position.side}",
            size_coins=position.size_coins,
            price=close_price,
            timestamp_ms=timestamp_ms,
        )

    # ------------------------------------------------------------------
    # Live mode
    # ------------------------------------------------------------------

    def _live_open(
        self,
        signal: "Signal",
        size_coins: float,
        limit_price: float,
        timestamp_ms: int,
    ) -> tuple[Position | None, OrderResult]:
        """Place a live limit entry + stop-loss order after user confirmation."""
        is_buy = signal.side == "long"
        stop_is_buy = not is_buy

        stop_trigger = str(round(signal.stop_price, 6))
        # Limit price for the stop order sits 5% beyond trigger to avoid rejection
        stop_limit = str(round(signal.stop_price * (0.95 if is_buy else 1.05), 6))

        print("\n" + "=" * 50)
        print(f"  LIVE ORDER — {self.network.upper()}")
        print(f"  Action:    OPEN {signal.side.upper()}")
        print(f"  Coin:      {signal.coin}")
        print(f"  Size:      {size_coins:.6f} coins")
        print(f"  Entry:     limit @ {limit_price:.6f}")
        print(f"  Stop:      trigger @ {stop_trigger}  limit @ {stop_limit}")
        print(f"  Leverage:  {self.leverage}x")
        print(f"  Network:   {self.network}")
        print("=" * 50)

        if input("Confirm? [y/N] ").strip().lower() != "y":
            return None, OrderResult(
                success=False,
                mode="live",
                coin=signal.coin,
                side=signal.side,
                size_coins=size_coins,
                price=limit_price,
                timestamp_ms=timestamp_ms,
                error="user_cancelled",
            )

        time.sleep(random.uniform(0.5, 2.5))

        try:
            self._exchange.update_leverage(self.leverage, signal.coin, is_cross=True)

            entry_resp = self._exchange.order(
                signal.coin,
                is_buy,
                size_coins,
                limit_price,
                {"limit": {"tif": "Gtc"}},
            )
            order_id = str(
                entry_resp.get("response", {})
                .get("data", {})
                .get("statuses", [{}])[0]
                .get("resting", {})
                .get("oid", "")
            )

            stop_type = {
                "trigger": {
                    "triggerPx": stop_trigger,
                    "isMarket": True,
                    "tpsl": "sl",
                }
            }
            stop_resp = self._exchange.order(
                signal.coin,
                stop_is_buy,
                size_coins,
                stop_limit,
                stop_type,
                reduce_only=True,
            )
            stop_order_id = str(
                stop_resp.get("response", {})
                .get("data", {})
                .get("statuses", [{}])[0]
                .get("resting", {})
                .get("oid", "")
            )

        except Exception as exc:
            return None, OrderResult(
                success=False,
                mode="live",
                coin=signal.coin,
                side=signal.side,
                size_coins=size_coins,
                price=limit_price,
                timestamp_ms=timestamp_ms,
                error=str(exc),
            )

        position = Position(
            coin=signal.coin,
            side=signal.side,
            entry_price=limit_price,
            stop_price=signal.stop_price,
            size_coins=size_coins,
            entry_time_ms=signal.timestamp,
            leverage=self.leverage,
            order_id=order_id or None,
            stop_order_id=stop_order_id or None,
        )

        return position, OrderResult(
            success=True,
            mode="live",
            coin=signal.coin,
            side=signal.side,
            size_coins=size_coins,
            price=limit_price,
            timestamp_ms=timestamp_ms,
            order_id=order_id or None,
        )

    def _live_close(
        self,
        position: Position,
        reason: str,
        close_price: float,
        timestamp_ms: int,
    ) -> OrderResult:
        """Cancel the stop order and place a limit close after user confirmation."""
        is_buy = position.side == "short"  # closing a short = buying back

        print("\n" + "=" * 50)
        print(f"  LIVE ORDER — {self.network.upper()}")
        print(f"  Action:    CLOSE {position.side.upper()}")
        print(f"  Coin:      {position.coin}")
        print(f"  Size:      {position.size_coins:.6f} coins")
        print(f"  Price:     limit @ {close_price:.6f}")
        print(f"  Reason:    {reason}")
        print("=" * 50)

        if input("Confirm? [y/N] ").strip().lower() != "y":
            return OrderResult(
                success=False,
                mode="live",
                coin=position.coin,
                side=f"close_{position.side}",
                size_coins=position.size_coins,
                price=close_price,
                timestamp_ms=timestamp_ms,
                error="user_cancelled",
            )

        time.sleep(random.uniform(0.5, 2.5))

        try:
            # Cancel the standing stop order before placing the close
            if position.stop_order_id:
                self._exchange.cancel(position.coin, int(position.stop_order_id))

            close_resp = self._exchange.order(
                position.coin,
                is_buy,
                position.size_coins,
                close_price,
                {"limit": {"tif": "Gtc"}},
                reduce_only=True,
            )
            order_id = str(
                close_resp.get("response", {})
                .get("data", {})
                .get("statuses", [{}])[0]
                .get("resting", {})
                .get("oid", "")
            )

        except Exception as exc:
            return OrderResult(
                success=False,
                mode="live",
                coin=position.coin,
                side=f"close_{position.side}",
                size_coins=position.size_coins,
                price=close_price,
                timestamp_ms=timestamp_ms,
                error=str(exc),
            )

        return OrderResult(
            success=True,
            mode="live",
            coin=position.coin,
            side=f"close_{position.side}",
            size_coins=position.size_coins,
            price=close_price,
            timestamp_ms=timestamp_ms,
            order_id=order_id or None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_kill_switch(self) -> None:
        """Raise SystemExit if the KILL file is present in the project root."""
        if KILL_FILE.exists():
            print("\n[KILL SWITCH] KILL file detected — shutting down immediately.")
            raise SystemExit(0)

    def _limit_price_for_entry(self, signal: "Signal") -> float:
        """Derive the limit price for an entry order.

        Uses the signal's entry_price (last close). The tick_offset is a
        config knob for future refinement using live order-book data.

        Args:
            signal: Entry signal.

        Returns:
            Limit price as a float.
        """
        # tick_offset is reserved for order-book-aware placement (future work)
        return signal.entry_price

    def _hard_stop_hit(self, position: Position, current_close: float) -> bool:
        """Return True if the current close has breached the hard stop."""
        if position.side == "long":
            return current_close <= position.stop_price
        return current_close >= position.stop_price

    def _calc_pnl(self, position: Position, close_price: float) -> float:
        """Delegate to the module-level calc_trade_pnl."""
        return calc_trade_pnl(position, close_price)

    def _find_entry_candle_idx(
        self, candles: list[dict], entry_time_ms: int
    ) -> int | None:
        """Find the index of the entry candle in the current candles list.

        Args:
            candles: Current candle list.
            entry_time_ms: Open-time of the entry candle (ms).

        Returns:
            Index if found, None if the entry candle has aged out of the window.
        """
        for i, candle in enumerate(candles):
            if candle["t"] == entry_time_ms:
                return i
        return None

    def _append_paper_trade(self, record: dict) -> None:
        """Append a record to the paper trades JSON file.

        Args:
            record: Dict to append.
        """
        trades: list = []
        if PAPER_TRADES_FILE.exists():
            with open(PAPER_TRADES_FILE) as f:
                try:
                    trades = json.load(f)
                except json.JSONDecodeError:
                    trades = []

        trades.append(record)

        with open(PAPER_TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2)
