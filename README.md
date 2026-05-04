# hyperliquid-skills

Modular algorithmic trading system for Hyperliquid perpetuals.

## Setup

```bash
uv sync
cp .env.example .env   # add HL_PRIVATE_KEY and HL_ACCOUNT_ADDRESS
```

## Usage

```bash
# Fetch candle data (stores to data/)
uv run --env-file .env python main.py --mode backtest --fetch --coin HYPE

# Backtest on stored candles
uv run --env-file .env python main.py --mode backtest --coin HYPE

# Compare two backtest runs
uv run python compare.py trades/backtest_HYPE_2026-05-04_v1.csv \
                         trades/backtest_HYPE_2026-05-04_v2.csv

# Paper trade (live loop, no real orders)
uv run --env-file .env python main.py --mode paper --coin HYPE

# Live trading (requires --confirm)
uv run --env-file .env python main.py --mode live --coin HYPE --confirm
```

## Iteration Workflow

```
1. Edit config.json — change a param, increment version, add a note
2. python main.py --mode backtest --coin HYPE
3. python compare.py trades/backtest_HYPE_*_v1.csv trades/backtest_HYPE_*_v2.csv
4. Keep if better, revert if worse
```

## Architecture

```
data/           fetch_and_save(), get_latest_candles()
                calc_ema(), calc_atr(), calc_rsi(), calc_funding_rate()

signals/        ThreeEMACross — 3-EMA crossover strategy with ATR stops

risk/           RiskGate — position limits, daily loss, drawdown ceiling
                PortfolioState — running equity tracker

execution/      OrderManager — paper and live order placement
                calc_trade_pnl() — shared pure P&L function

journal/        JournalBackend protocol — swap CSV/Polars/DuckDB freely
                CSVLogger — stdlib append-only CSV implementation
                compute_stats() — shared pure stats function

backtest/       BacktestEngine — walk-forward simulation, no lookahead
                Accepts any JournalBackend for persistence

compare.py      Side-by-side backtest metric comparison
main.py         CLI: --mode backtest | paper | live
```

## Safety

- Paper mode is the default. Live mode requires both `--mode live` and `--confirm`.
- Drop a file named `KILL` in the project root to halt the live loop immediately.
- Private keys are loaded from `.env` via `uv run --env-file .env` — never hardcoded.
- All live orders are limit-only with manual confirmation before placement.

## Configuration

All strategy and risk parameters live in `config.json`. It is version-controlled —
credentials belong in `.env`, never here.

| Field | Purpose |
|---|---|
| `version` | Increment before each backtest run you intend to compare |
| `notes` | Why you changed the params — becomes the experiment log |
| `strategy.*` | EMA periods, ATR mult, funding filter, direction, hold limit |
| `risk.*` | Position limit, daily loss cap, drawdown cap, capital, leverage |
| `execution.mode` | `"paper"` default — overridden by `--mode` CLI arg |
| `execution.network` | `"testnet"` default |
