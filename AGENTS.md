# AGENTS.md

Read `README.md` for project context and architecture before proceeding.

## Credentials and Config

- Credentials live in `.env`, loaded via `uv run --env-file .env` — never in `config.json`
- Required env vars: `HL_PRIVATE_KEY` (API wallet private key) and `HL_ACCOUNT_ADDRESS` (main wallet public address)
- `config.json` is version-controlled strategy/risk params only — increment `version` and update `notes` on every parameter change
- Never print, log, or surface `HL_PRIVATE_KEY` anywhere

## SDK Footguns (live mode)

- `triggerPx` in stop-loss/take-profit orders must be a **string**: `"triggerPx": "1600"`, not `1600`
- `limit_px` on trigger orders is required — set it ~5% beyond `triggerPx` to avoid silent rejection
- Coin names are exact uppercase: `"ETH"`, `"BTC"`, `"HYPE"` — never lowercase
- Size is in coin units, never USD notional
- Call `exchange.update_leverage()` before `market_open()`, not after

## Safety Constraints

- Always use `constants.TESTNET_API_URL` unless the user explicitly requests mainnet
- Live mode requires both `--mode live` AND `--confirm` — do not generate live execution code without confirming the user intends real trades

## Data Layer

**Fetching and storage**
- `data/fetcher.py` — HTTP client only. `fetch_candles()` and `get_latest_candles()` are the raw API calls; `fetch_and_save()` and `fetch_and_save_funding()` are thin wrappers that delegate to `ingest.py` and return from the lake
- `data/ingest.py` — incremental orchestrator. Reads `data/metadata.json` for last stored timestamp, fetches only the delta from the API, writes to the Parquet lake, then updates metadata atomically
- `data/lake.py` — `CandleLake` class. Writes via PyArrow (monthly Parquet files, idempotent dedup on timestamp). Reads via DuckDB. Provides `query(sql)` for ad-hoc analysis and `last_n_days(symbol, timeframe, n)` for convenience

**Parquet layout** — Hive-partitioned, DuckDB reads with `hive_partitioning=true`:
```
data/lake/candles/symbol={COIN}/timeframe={TF}/year={YYYY}/month={MM}/data.parquet
data/lake/funding/symbol={COIN}/year={YYYY}/month={MM}/data.parquet
```

**Candle dict keys** (used throughout backtest/strategy/live loop): `t` (open time ms), `o`, `h`, `l`, `c`, `v`, `n`
- `load_candles(coin, interval, lookback_days=None)` returns this format from the lake
- The lake stores the canonical column names (`timestamp`, `open`, `high`, `low`, `close`, `volume`, `num_trades`); the fetcher translates on read

**Funding rates**
- Hyperliquid emits one funding record per hour. `ingest_funding()` stores `hour_ms → rate` in the lake
- The backtest engine loads the full funding map once via `load_funding_map(coin)` and does a per-candle dict lookup — no per-candle API calls
- Missing hours fall back to `0.0` in `_lookup_funding()`

**Indicators**
- All in `data/indicators.py` — pure functions returning NaN-padded lists, same length as input
- Do not use `pandas-ta`; the project uses its own `calc_ema`, `calc_atr`, `calc_rsi`

## Architecture Non-Obviities

- `calc_trade_pnl(position, close_price)` is a **module-level function** in `execution/order_manager.py` — import it directly without instantiating `OrderManager`
- `JournalBackend` is a `typing.Protocol` in `journal/logger.py`; both `BacktestEngine` and the live loop depend on the protocol, not `CSVLogger` directly — pass any conforming logger to swap backends
- `compute_stats(rows)` in `journal/logger.py` is a pure function shared by `CSVLogger.summary()` and `compare.py` — do not duplicate this logic
- Signal fields needed for journaling (ema_fast/mid/slow, atr, funding_rate) are attached to `Position` at open time as a runtime attribute: `position._signal = signal` — retrieve with `getattr(position, "_signal", None)` at close time

## Backtest Behaviour

- Entry fills at the **next candle's open/low**, not at the signal candle's close — this mirrors placing a limit order after the signal candle closes
- Hard-stop checks use `candle["l"]` (long) or `candle["h"]` (short) — intra-candle detection; fills at `open` if there's a gap through the stop
- `BacktestEngine` tracks `entry_candle_idx` as a local variable and passes it directly to `strategy.check_exit()` — no timestamp lookup needed

## CLI Behaviour

- `--fetch` alone incrementally updates the Parquet lake for the given `--coin` (candles + funding); combine with `--mode backtest` to fetch then immediately run
- `--stats` prints what's in the lake (row counts, date ranges per symbol/timeframe) and exits — safe to run at any time
- Increment `config.json["version"]` before each backtest run you intend to compare; the version is embedded in the output filename `trades/backtest_{coin}_{date}_v{version}.csv`
