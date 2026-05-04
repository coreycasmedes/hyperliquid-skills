# hyperliquid-skills

Modular algorithmic trading system for Hyperliquid perpetuals.

## Setup

```bash
uv sync
cp .env.example .env   # add HL_PRIVATE_KEY and HL_ACCOUNT_ADDRESS
```

## Usage

```bash
# Fetch candle data
uv run --env-file .env python main.py --fetch --coin HYPE

# Backtest with stored candles
uv run --env-file .env python main.py --mode backtest --coin HYPE

# Paper trade (live loop, no real orders)
uv run --env-file .env python main.py --mode paper --coin HYPE

# Live trading (requires --confirm)
uv run --env-file .env python main.py --mode live --coin HYPE --confirm
```

## Architecture

```
data/           OHLCV fetcher + pure indicator functions
signals/        ThreeEMACross strategy — generates entry signals
risk/           RiskGate — approves/rejects signals on portfolio constraints
execution/      OrderManager — paper and live order placement
journal/        Trade logger + performance summary
backtest/       Historical simulation engine
```

## Safety

- Paper mode is the default. Live mode requires both `--mode live` and `--confirm`.
- Drop a file named `KILL` in the project root to halt the live loop immediately.
- Private keys are loaded from `.env` via `uv run --env-file .env` — never hardcoded.
- All live orders are limit-only with manual confirmation before placement.

## Configuration

All strategy and risk parameters live in `config.json`. Increment `version` and
update `notes` whenever you change a parameter so backtest runs are traceable.

## Disclaimer

Trading perpetual futures involves significant risk of loss. Use at your own risk.
Always run backtests and paper trade before going live. Start with small sizes.
