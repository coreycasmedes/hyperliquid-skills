"""Parquet data lake with DuckDB query layer for Hyperliquid market data.

Directory layout (Hive partitioning):
    data/lake/candles/symbol={COIN}/timeframe={TF}/year={YYYY}/{MM:02d}.parquet
    data/lake/funding/symbol={COIN}/year={YYYY}/{MM:02d}.parquet

Each file covers one calendar month. Historical months are immutable once
complete; the current month is rewritten on each update with duplicates
removed. All timestamps are UTC milliseconds.

Write path: PyArrow (fast columnar I/O, no runtime overhead).
Read path:  DuckDB (SQL over Parquet, hive partition pruning in query()).
"""

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

LAKE_DIR = Path(__file__).parent / "lake"

CANDLE_SCHEMA = pa.schema(
    [
        pa.field("timestamp", pa.int64()),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("num_trades", pa.int64()),
    ]
)

FUNDING_SCHEMA = pa.schema(
    [
        pa.field("timestamp", pa.int64()),
        pa.field("rate", pa.float64()),
    ]
)


# ── Path helpers ──────────────────────────────────────────────────────────────


def _ms_to_ym(ms: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.year, dt.month


def _ms_to_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _candle_path(lake_dir: Path, symbol: str, timeframe: str, year: int, month: int) -> Path:
    part = (
        f"candles/symbol={symbol}/timeframe={timeframe}/year={year}/month={month:02d}/data.parquet"
    )
    return lake_dir / part


def _funding_path(lake_dir: Path, symbol: str, year: int, month: int) -> Path:
    return lake_dir / f"funding/symbol={symbol}/year={year}/month={month:02d}/data.parquet"


# ── Core write primitive ───────────────────────────────────────────────────────


def _upsert_parquet(path: Path, new_table: pa.Table) -> int:
    """Merge new_table into a monthly Parquet file, deduped on timestamp.

    If the file exists, existing rows are loaded, concatenated with new_table,
    sorted, and deduplicated (new data wins). The file is then rewritten.
    Returns the total number of rows in the written file.
    """
    if path.exists():
        existing = pq.read_table(path, schema=new_table.schema)
        combined = pa.concat_tables([existing, new_table])
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        combined = new_table

    combined = combined.sort_by("timestamp")

    # Keep the last occurrence of each timestamp so new data wins on overlap
    timestamps = combined.column("timestamp").to_pylist()
    seen: set = set()
    keep = [False] * len(timestamps)
    for i in range(len(timestamps) - 1, -1, -1):
        ts = timestamps[i]
        if ts not in seen:
            seen.add(ts)
            keep[i] = True

    deduped = combined.filter(pa.array(keep, type=pa.bool_()))
    pq.write_table(deduped, path, compression="snappy")
    return len(deduped)


# ── DuckDB read helper ────────────────────────────────────────────────────────


def _duckdb_read(
    files: list[Path],
    select: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    hive: bool = False,
) -> list[dict]:
    """Query a list of Parquet files via DuckDB. Returns list of row dicts."""
    if not files:
        return []

    files_repr = repr([str(f) for f in files])
    hive_opt = ", hive_partitioning=true" if hive else ""

    where_parts = []
    if start_ms is not None:
        where_parts.append(f"timestamp >= {start_ms}")
    if end_ms is not None:
        where_parts.append(f"timestamp <= {end_ms}")
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    sql = f"SELECT {select} FROM read_parquet({files_repr}{hive_opt}){where} ORDER BY timestamp"

    conn = duckdb.connect()
    try:
        cursor = conn.execute(sql)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


# ── Main class ────────────────────────────────────────────────────────────────


class CandleLake:
    """Parquet data lake for Hyperliquid historical market data."""

    def __init__(self, lake_dir: Path = LAKE_DIR):
        self.lake_dir = lake_dir
        self._candles_dir = lake_dir / "candles"
        self._funding_dir = lake_dir / "funding"

    # ── Write ─────────────────────────────────────────────────────────────────

    def write_candles(self, symbol: str, timeframe: str, candles: list[dict]) -> int:
        """Write candles to monthly Parquet files. Returns total rows stored across all months."""
        if not candles:
            return 0

        groups: dict[tuple[int, int], list[dict]] = {}
        for c in candles:
            groups.setdefault(_ms_to_ym(c["t"]), []).append(c)

        total = 0
        for (year, month), group in sorted(groups.items()):
            table = pa.table(
                {
                    "timestamp": [c["t"] for c in group],
                    "open": [c["o"] for c in group],
                    "high": [c["h"] for c in group],
                    "low": [c["l"] for c in group],
                    "close": [c["c"] for c in group],
                    "volume": [c["v"] for c in group],
                    "num_trades": [c["n"] for c in group],
                },
                schema=CANDLE_SCHEMA,
            )
            path = _candle_path(self.lake_dir, symbol, timeframe, year, month)
            total += _upsert_parquet(path, table)

        return total

    def write_funding(self, symbol: str, funding_map: dict[int, float]) -> int:
        """Write funding rates to monthly Parquet files. Returns total rows stored."""
        if not funding_map:
            return 0

        groups: dict[tuple[int, int], list] = {}
        for hour_ms, rate in funding_map.items():
            groups.setdefault(_ms_to_ym(hour_ms), []).append((hour_ms, rate))

        total = 0
        for (year, month), group in sorted(groups.items()):
            table = pa.table(
                {
                    "timestamp": [g[0] for g in group],
                    "rate": [g[1] for g in group],
                },
                schema=FUNDING_SCHEMA,
            )
            path = _funding_path(self.lake_dir, symbol, year, month)
            total += _upsert_parquet(path, table)

        return total

    # ── Read ──────────────────────────────────────────────────────────────────

    def read_candles(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict]:
        """Return candles ordered by timestamp, filtered by optional ms range."""
        base = self._candles_dir / f"symbol={symbol}" / f"timeframe={timeframe}"
        if not base.exists():
            return []
        files = sorted(base.rglob("*.parquet"))
        return _duckdb_read(
            files,
            "timestamp, open, high, low, close, volume, num_trades",
            start_ms,
            end_ms,
        )

    def read_funding_map(
        self,
        symbol: str,
        start_ms: int | None = None,
    ) -> dict[int, float]:
        """Return {hour_ms: rate} for all stored funding rates."""
        base = self._funding_dir / f"symbol={symbol}"
        if not base.exists():
            return {}
        files = sorted(base.rglob("*.parquet"))
        rows = _duckdb_read(files, "timestamp, rate", start_ms)
        return {r["timestamp"]: r["rate"] for r in rows}

    def last_candle_ts(self, symbol: str, timeframe: str) -> int | None:
        """Return the open-time ms of the most recent stored candle, or None."""
        base = self._candles_dir / f"symbol={symbol}" / f"timeframe={timeframe}"
        if not base.exists():
            return None
        files = sorted(base.rglob("*.parquet"))
        if not files:
            return None
        conn = duckdb.connect()
        try:
            files_repr = repr([str(f) for f in files])
            row = conn.execute(
                f"SELECT MAX(timestamp) AS ts FROM read_parquet({files_repr})"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()

    def last_funding_ts(self, symbol: str) -> int | None:
        """Return the hour_ms of the most recent stored funding record, or None."""
        base = self._funding_dir / f"symbol={symbol}"
        if not base.exists():
            return None
        files = sorted(base.rglob("*.parquet"))
        if not files:
            return None
        conn = duckdb.connect()
        try:
            files_repr = repr([str(f) for f in files])
            row = conn.execute(
                f"SELECT MAX(timestamp) AS ts FROM read_parquet({files_repr})"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()

    # ── DuckDB query interface ─────────────────────────────────────────────────

    def query(self, sql: str) -> list[dict]:
        """Run arbitrary SQL against the full lake via DuckDB.

        Two views are available with hive partition columns:
          candles — columns: timestamp, open, high, low, close, volume, num_trades,
                             symbol (str), timeframe (str), year (int)
          funding — columns: timestamp, rate, symbol (str), year (int)

        Examples
        --------
        # Last 7 days of BTC 1h candles
        lake.query(
            \"\"\"SELECT * FROM candles
               WHERE symbol='BTC' AND timeframe='1h'
                 AND timestamp >= epoch_ms(now()) - interval '7 days'
               ORDER BY timestamp\"\"\"
        )

        # Average hourly funding rate per symbol
        lake.query(
            \"\"\"SELECT symbol, AVG(rate) AS avg_rate FROM funding GROUP BY symbol\"\"\"
        )
        """
        candle_files = (
            sorted(self._candles_dir.rglob("*.parquet")) if self._candles_dir.exists() else []
        )
        funding_files = (
            sorted(self._funding_dir.rglob("*.parquet")) if self._funding_dir.exists() else []
        )

        conn = duckdb.connect()
        try:
            if candle_files:
                conn.execute(
                    f"CREATE VIEW candles AS SELECT * FROM read_parquet("
                    f"{repr([str(f) for f in candle_files])}, hive_partitioning=true)"
                )
            if funding_files:
                conn.execute(
                    f"CREATE VIEW funding AS SELECT * FROM read_parquet("
                    f"{repr([str(f) for f in funding_files])}, hive_partitioning=true)"
                )

            cursor = conn.execute(sql)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def last_n_days(self, symbol: str, timeframe: str, days: int) -> list[dict]:
        """Convenience wrapper: return candles for the last N calendar days."""
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        cutoff_ms = now_ms - days * 86_400_000
        return self.read_candles(symbol, timeframe, start_ms=cutoff_ms)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def to_duckdb(self, path: Path | None = None) -> Path:
        """Create (or refresh) a persistent DuckDB file with views over the lake.

        The file can be opened by any DuckDB-compatible client — VS Code with
        the SQLTools DuckDB driver, DBeaver, or the duckdb CLI — without
        needing to know the Parquet layout.

        Views use glob patterns so new monthly files are picked up automatically
        on each query without needing to call this method again.

        Returns the path to the written .duckdb file.
        """
        if path is None:
            path = self.lake_dir.parent / "lake.duckdb"

        candles_glob = str(self._candles_dir / "**" / "*.parquet")
        funding_glob = str(self._funding_dir / "**" / "*.parquet")

        try:
            conn = duckdb.connect(str(path))
        except duckdb.IOException:
            print(f"  Warning: {path.name} is locked by another process (VS Code?). Parquet data unaffected.")
            return path

        try:
            conn.execute("DROP VIEW IF EXISTS candles")
            conn.execute("DROP VIEW IF EXISTS funding")
            if self._candles_dir.exists():
                conn.execute(
                    f"CREATE VIEW candles AS SELECT * FROM read_parquet("
                    f"'{candles_glob}', hive_partitioning=true)"
                )
            if self._funding_dir.exists():
                conn.execute(
                    f"CREATE VIEW funding AS SELECT * FROM read_parquet("
                    f"'{funding_glob}', hive_partitioning=true)"
                )
        finally:
            conn.close()

        return path

    def print_stats(self) -> None:
        """Print a formatted summary of what's in the lake."""
        width = 72
        print("\n" + "═" * width)
        print(f"  LAKE  {self.lake_dir}")
        print("═" * width)

        candle_files = (
            sorted(self._candles_dir.rglob("*.parquet")) if self._candles_dir.exists() else []
        )
        funding_files = (
            sorted(self._funding_dir.rglob("*.parquet")) if self._funding_dir.exists() else []
        )

        conn = duckdb.connect()
        try:
            if candle_files:
                conn.execute(
                    f"CREATE VIEW candles AS SELECT * FROM read_parquet("
                    f"{repr([str(f) for f in candle_files])}, hive_partitioning=true)"
                )
                rows = conn.execute("""
                    SELECT symbol, timeframe, COUNT(*) AS rows,
                           MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts
                    FROM candles
                    GROUP BY symbol, timeframe
                    ORDER BY symbol, timeframe
                """).fetchall()
                print("  Candles")
                for r in rows:
                    first = _ms_to_str(r[3])
                    last = _ms_to_str(r[4])
                    print(f"    {r[0]:6} {r[1]:4}  {r[2]:>8,} rows  {first} → {last}")
            else:
                print("  Candles  (empty)")

            print()

            if funding_files:
                conn.execute(
                    f"CREATE VIEW funding AS SELECT * FROM read_parquet("
                    f"{repr([str(f) for f in funding_files])}, hive_partitioning=true)"
                )
                rows = conn.execute("""
                    SELECT symbol, COUNT(*) AS rows,
                           MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts
                    FROM funding
                    GROUP BY symbol
                    ORDER BY symbol
                """).fetchall()
                print("  Funding")
                for r in rows:
                    first = _ms_to_str(r[2])
                    last = _ms_to_str(r[3])
                    print(f"    {r[0]:6}       {r[1]:>8,} hours  {first} → {last}")
            else:
                print("  Funding  (empty)")
        finally:
            conn.close()

        print("═" * width + "\n")
