# AGENTS.md

Read `README.md` and `SKILL.md` for project context, SDK initialization patterns, and order type reference before proceeding.

## SDK Footguns

- `triggerPx` in stop-loss and take-profit orders must be a **string**, not a float: `"triggerPx": "1600"`, not `1600`
- `limit_px` on trigger orders is required by the SDK but largely ignored by the exchange — set it ~5% beyond `triggerPx` to avoid silent rejection
- Coin names are exact uppercase strings: `"ETH"`, `"BTC"`, `"SOL"` — never lowercase, never full names like `"ethereum"`
- Size is always in coin units (e.g., `0.1` = 0.1 ETH), never USD notional
- Call `exchange.update_leverage()` before `market_open()` — setting it after has no effect on the opening order

## Config

- `account_address` is the main wallet's **public** address; `secret_key` is the API wallet's **private** key — they are different accounts on Hyperliquid
- Never print, log, or include `secret_key` in generated output, error messages, or example code

## Safety Constraints

- Always use `constants.TESTNET_API_URL` unless the user explicitly requests mainnet
- Require explicit user confirmation before generating any code that writes to mainnet — do not infer intent from context

## OHLCV Data

- Fetch candles via `info.candles_snapshot(coin, interval, startTime, endTime)` — `startTime` and `endTime` are Unix milliseconds
- Response fields: `t` (open time ms), `o`, `h`, `l`, `c`, `v` (volume in coin units), `n` (num trades)
- Valid intervals: `"1m"`, `"5m"`, `"15m"`, `"30m"`, `"1h"`, `"4h"`, `"8h"`, `"12h"`, `"1d"`, `"3d"`, `"1w"`
- Use `pandas-ta` for all indicator calculations — it attaches to DataFrames via `df.ta.<indicator>()`; build the DataFrame from candle response before computing any indicators

## Project Files

- `crypto-ta.skill` and `hyperliquid-trading.skill` are ZIP archives — do not attempt to read them as source; `SKILL.md` contains the equivalent content
- New agent scripts go in `skills/<skill-name>/scripts/`; add `sys.path.insert(0, repo_root)` to import from `shared/`
- `shared/setup_client.py` handles all SDK initialization — prefer it over re-implementing client setup inline
