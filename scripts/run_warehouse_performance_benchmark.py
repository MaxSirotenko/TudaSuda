#!/usr/bin/env python3
"""CLI entry point for the warehouse performance baseline."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warehouse_performance_benchmark import run_benchmark, write_reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("current", "synthetic", "current-or-synthetic"), default="current-or-synthetic")
    parser.add_argument("--cells", type=int, default=16_000)
    parser.add_argument("--occupied-cells", type=int, default=500)
    parser.add_argument("--placements", type=int, default=700)
    parser.add_argument("--warm-iterations", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=Path("data/performance_benchmarks"))
    args = parser.parse_args(argv)
    try:
        result = run_benchmark(args.mode, args.cells, args.occupied_cells, args.placements, args.warm_iterations)
        paths = write_reports(result, args.output_dir)
    except Exception as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1
    s, d, a = result["scenarios"], result["dataset"], result["bottleneck_analysis"]
    print(f"Source mode: {result['environment']['source_mode']}")
    print(f"Cells: {d['cells_count']}; placements: {d['placements_count']}")
    print(f"Static cold: {s['static_cold']['wall_time_ms']:.3f} ms; warm median: {s['static_warm']['median_ms']:.3f} ms")
    print(f"Dynamic cold: {s['dynamic_cold']['wall_time_ms']:.3f} ms; warm median: {s['dynamic_warm']['median_ms']:.3f} ms")
    print(f"Compose: {s['compose_layers']['wall_time_ms']:.3f} ms; final HTML: {result['payload_sizes']['final_html_bytes']/1024/1024:.2f} MiB")
    print(f"Primary bottleneck: {a['primary_bottleneck']}")
    print(f"Recommended next step: {a['recommended_next_step']}")
    print(f"Markdown report: {paths['latest_markdown']}")
    if not result["persisted_state_unchanged"]:
        print("Persisted state changed: " + ", ".join(result["changed_persisted_files"]), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
