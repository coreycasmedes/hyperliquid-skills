# hyperliquid-skills

Modular algorithmic trading system for Hyperliquid perpetuals.

## Setup

```bash
uv sync
cp .env.example .env   # add HL_PRIVATE_KEY and HL_ACCOUNT_ADDRESS
```

## Usage

```bash
# Fetch and store candle + funding data (incremental — only new candles on re-run)
uv run --env-file .env python main.py --fetch --coin HYPE

# Show what's stored in the lake
uv run python main.py --stats

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

## Historical Data Lake

Candles and funding rates are stored as partitioned Parquet files under `data/lake/`.

```
data/lake/
  candles/
    symbol=HYPE/
      timeframe=15m/
        year=2026/
          month=03/data.parquet
          month=04/data.parquet
          month=05/data.parquet
  funding/
    symbol=HYPE/
      year=2026/
        month=04/data.parquet
        month=05/data.parquet
```

`data/metadata.json` tracks the last ingested timestamp per symbol/timeframe and is the
source of truth for incremental updates. Each `--fetch` only pulls candles newer than
the last stored timestamp — re-running is safe and fast.

### DuckDB queries

```python
from data.lake import CandleLake

lake = CandleLake()

# Last 7 days of HYPE 15m candles
rows = lake.last_n_days("HYPE", "15m", days=7)

# Arbitrary SQL — hive partition columns (symbol, timeframe, year, month) available
rows = lake.query("""
    SELECT symbol, timeframe, year, month,
           MIN(close) AS low_close, MAX(close) AS high_close
    FROM candles
    WHERE symbol = 'HYPE' AND timeframe = '15m'
    GROUP BY symbol, timeframe, year, month
    ORDER BY year, month
""")

# Funding map for the backtest engine
funding_map = lake.read_funding_map("HYPE")   # {hour_ms: rate}
```

## Architecture

```
data/
  fetcher.py    Raw Hyperliquid API client (fetch_candles, get_latest_candles)
  ingest.py     Incremental ingestor — reads metadata.json, fetches delta, writes lake
  lake.py       CandleLake — Parquet I/O (PyArrow) + DuckDB query interface
  indicators.py calc_ema(), calc_atr(), calc_rsi() — pure functions, no side effects

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
main.py         CLI: --mode backtest | paper | live | --fetch | --stats
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
