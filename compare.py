"""Compare two backtest CSV files side-by-side.

Reuses compute_stats() from journal/logger.py so metrics are computed
identically to what CSVLogger.summary() reports.

Usage:
    uv run python scripts/compare.py trades/backtest_HYPE_2026-05-04_v1.csv \\
                                     trades/backtest_HYPE_2026-05-04_v2.csv
"""

import csv
import sys
from pathlib import Path

from journal.logger import compute_stats


def _load_csv(filepath: str) -> list[dict]:
    path = Path(filepath)
    if not path.exists():
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    """Load two CSVs, compute stats for each, and print a comparison table."""
    if len(sys.argv) != 3:
        print("Usage: uv run python scripts/compare.py <file1.csv> <file2.csv>")
        sys.exit(1)

    f1, f2 = sys.argv[1], sys.argv[2]
    s1 = compute_stats(_load_csv(f1))
    s2 = compute_stats(_load_csv(f2))

    label1 = Path(f1).stem
    label2 = Path(f2).stem

    # (display_name, stats_key, format_string, higher_is_better)
    metrics = [
        ("Total trades", "total_trades", "{:.0f}", None),
        ("Win rate", "win_rate", "{:.1f}%", True),
        ("Total P&L", "total_pnl", "${:+.2f}", True),
        ("Avg winner", "avg_winner", "${:+.2f}", True),
        ("Avg loser", "avg_loser", "${:+.2f}", True),
        ("Profit factor", "profit_factor", "{:.2f}", True),
        ("Max drawdown", "max_drawdown", "${:+.2f}", True),
        ("Sharpe (trade)", "sharpe_ratio", "{:.2f}", True),
    ]

    col = max(len(label1), len(label2), 28)

    def _fmt(v, fmt: str) -> str:
        if v is None:
            return "N/A"
        if v == float("inf"):
            return "∞"
        return fmt.format(v)

    def _better(v1, v2, higher: bool | None) -> str:
        if higher is None or v1 is None or v2 is None or v1 == v2:
            return "—"
        if v1 == float("inf") and v2 != float("inf"):
            return "▲ v1" if higher else "▲ v2"
        if v2 == float("inf") and v1 != float("inf"):
            return "▲ v2" if higher else "▲ v1"
        if not isinstance(v1, (int, float)) or not isinstance(v2, (int, float)):
            return "—"
        if higher:
            return "▲ v1" if v1 > v2 else "▲ v2"
        return "▲ v1" if v1 < v2 else "▲ v2"

    sep = "─" * (18 + col * 2 + 14)
    print()
    print(f"{'Metric':<18}  {label1:<{col}}  {label2:<{col}}  Better")
    print(sep)

    for name, key, fmt, higher in metrics:
        v1 = s1.get(key)
        v2 = s2.get(key)
        print(
            f"{name:<18}  {_fmt(v1, fmt):<{col}}  {_fmt(v2, fmt):<{col}}  "
            f"{_better(v1, v2, higher)}"
        )

    print(sep)
    print()


if __name__ == "__main__":
    main()
