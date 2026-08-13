# PR #189 refactor performance report

## Environment and method

Final measurements use CPython 3.14.4, Streamlit 1.61.1, Linux x86_64 and 3 logical CPUs exposed to the container. Results are diagnostics, never pytest wall-clock gates. The geometry/application dataset is deterministic (16,000 cells, 700 placements/500 occupied cells); routing/optimizer uses 16,000 cells and 500 active SKU. Short paths are repeated five times and report median and range/p95.

## Startup/import time

Seven fresh subprocess imports on base `819fa524` had median **2100.990 ms** (range 1965.320–2721.957); final HEAD had median **2075.408 ms** (1984.889–2259.777), 1.2% lower. `-X importtime` still identifies pandas (~982 ms cumulative) and Streamlit (~584 ms) as the dominant unavoidable entrypoint dependencies. `warehouse_outbound_experiment_ui` and its scenario/optimizer graph are now loaded only at the screen boundary. Reproduce with `python -X importtime -c "import virtual_warehouse_app"` and repeated subprocesses.

## Geometry and warm application no-op

Command: `python scripts/run_warehouse_performance_benchmark.py --mode synthetic --cells 16000 --occupied-cells 500 --placements 700 --warm-iterations 5 --import-iterations 5`.

| Scenario | Final measurement |
|---|---:|
| static cold | 9124.557 ms; peak 121,737,665 B; 22,413,197 B static HTML |
| static warm | median 186.015 ms; range 178.456–195.686; underlying builder total 1 |
| dynamic cold | 322.957 ms; peak 4,797,789 B |
| dynamic warm | median 2.411 ms; range 2.341–2.886; direct builder total 1 |
| composition | 244.465 ms; final HTML 23,093,944 B |
| render no-change | 577.919 ms; 0 measured file/artifact reads; 0 rebuilds |
| application warm no-op | 707.532 ms; peak 142,912,386 B; one state-loader call; 0 measured file/artifact reads; 0 static/dynamic/graph/readiness/effective-view calls |

The zero values above are produced by `capture_io_reads` and wrappers around actual production builders/readers, not literals. A regression test intentionally performs a JSON and artifact read and verifies the counters become positive. The application scenario executes a Streamlit-free production orchestration boundary; it resolves cached layers and composes the real payload without browser/UI mutation. Static DOM generation remains the dominant cost. No geometry algorithm was changed merely to improve a noisy number.

## Factual XLSX streaming 10k / 50k / 100k

Final HEAD was rerun, not copied from #187: **7.888 / 25.963 / 52.672 s**, throughput 1267.8 / 1925.8 / 1898.5 rows/s, peak RSS 168,419,328 / 180,609,024 / 197,419,008 B. A same-runner base `819fa524` 100k control was **52.557 s**, 1902.7 rows/s, 195,518,464 B: final elapsed differs by +0.2% and memory by +1.0%. The read-only openpyxl loop, parser `factual-july-v5`, authoritative quantity, staging/publication and schemas remain unchanged. Reproduce with `python scripts/benchmark_factual_xlsx_import.py --rows 10000 50000 100000`.

## Readiness and FACT/checkpoints

The targeted readiness suite exercises complete July inputs, compact daily positive-SKU indexes, SQLite conflict handling, exact historical cell resolution and VGH warning semantics. One-day/monthly suites exercise deterministic replay, lazy daily detail and monthly-fact-v2 resume/checkpoint compatibility. These were run on final HEAD; implementation still streams and no month/business index is materialized. The PR does not claim invented wall-clock figures where no standalone timer exists.

## Routing and optimizer

Command: `python scripts/benchmark_refactor_hotpaths.py --cells 16000 --occupied 500 --repeats 5`.

| Workload | Median | Range / p95 | Result |
|---|---:|---:|---|
| physical graph build | 591.055 ms | 496.070–617.193 / 617.193 | deterministic graph |
| reachable shortest path | 0.054 ms | 0.050–0.165 / 0.165 | reachable; bounded single query |
| optimizer, 16k/500 SKU | 925.710 ms | 870.474–988.531 / 988.531 | ready, valid, 500 placements |

Profiling did not justify changing routing policy or optimizer allocation. Graph construction remains operation-scoped; no all-pairs matrix or unbounded global cache was added. Exact equivalence remains covered by routing/optimizer tests.

## Compatibility conclusion

No business contract, factual parser/schema, monthly-fact-v2 checkpoint, placement/outbound/receipt JSON schema or query changed. New artifact readers remain streaming; atomic publication retains the last valid file on failure. Reported noisy timings are preserved honestly rather than attributed to unmeasured improvements.
