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

## OHLCV and Indicators

- Fetch candles via `data/fetcher.py` — `fetch_and_save()` writes to disk, `get_latest_candles()` fetches without saving
- Candle dict keys: `t` (open time ms), `o`, `h`, `l`, `c`, `v`, `n`
- All indicator functions are in `data/indicators.py` — pure functions returning NaN-padded lists, same length as input
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

- `--fetch` alone with no `--mode` defaults to `--mode paper` and starts the live loop after fetching — use `--mode backtest` explicitly when you only want fetch + backtest
- Increment `config.json["version"]` before each backtest run you intend to compare; the version is embedded in the output filename `trades/backtest_{coin}_{date}_v{version}.csv`
