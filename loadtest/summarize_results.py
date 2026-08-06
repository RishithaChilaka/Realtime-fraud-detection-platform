#!/usr/bin/env python3
"""Turn a Locust `--csv` run's `<prefix>_stats.csv` into a short markdown
summary against the Phase 2 success criteria (500 RPS, p95 < 100ms).

Usage:
    python loadtest/summarize_results.py loadtest/results/run1_stats.csv
"""
from __future__ import annotations

import csv
import sys


def summarize(stats_csv_path: str) -> str:
    with open(stats_csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    lines = ["| Endpoint | Requests | Failures | RPS | p50 (ms) | p95 (ms) | p99 (ms) |", "|---|---|---|---|---|---|---|"]
    aggregated = None
    for row in rows:
        name = row.get("Name", "")
        if name == "Aggregated":
            aggregated = row
            continue
        lines.append(
            f"| {name} | {row.get('Request Count', '?')} | {row.get('Failure Count', '?')} | "
            f"{row.get('Requests/s', '?')} | {row.get('50%', '?')} | {row.get('95%', '?')} | {row.get('99%', '?')} |"
        )

    if aggregated:
        lines.append(
            f"| **Aggregated** | {aggregated.get('Request Count', '?')} | "
            f"{aggregated.get('Failure Count', '?')} | {aggregated.get('Requests/s', '?')} | "
            f"{aggregated.get('50%', '?')} | **{aggregated.get('95%', '?')}** | {aggregated.get('99%', '?')} |"
        )
        p95 = aggregated.get("95%")
        rps = aggregated.get("Requests/s")
        try:
            p95_ok = float(p95) < 100
            rps_ok = float(rps) >= 500
            verdict = (
                f"\n**Result**: p95={p95}ms ({'PASS' if p95_ok else 'FAIL'} vs <100ms target), "
                f"throughput={rps} req/s ({'PASS' if rps_ok else 'FAIL'} vs 500 req/s target).\n"
            )
            lines.append(verdict)
        except (TypeError, ValueError):
            pass

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python loadtest/summarize_results.py <path_to_stats.csv>")
        sys.exit(1)
    print(summarize(sys.argv[1]))
