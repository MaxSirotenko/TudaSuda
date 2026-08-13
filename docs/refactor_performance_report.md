# Refactor performance report

## Environment and method

* CPython 3.14.4; Linux 6.18.35 x86_64/glibc; Streamlit 1.61.1; logical CPU count: 3.
* Dataset: deterministic 16,000 cells, 200 rows, 700 placements in 500 occupied cells. Measurements are single-run diagnostics on a shared runner and are not CI SLAs.
* Before: base `819fa524`; after: working tree based on the same commit. Reproduce the geometry/startup/no-op suite with `python scripts/run_warehouse_performance_benchmark.py --mode synthetic --cells 16000 --occupied-cells 500 --placements 700 --warm-iterations 3 --import-iterations 3`.

## Results

| Scenario | Before | After | Interpretation |
|---|---:|---:|---|
| startup subprocess | 2006.912 ms | 1677.524 ms | 16.4% faster observation; repeated median 1758.573 → 1674.657 ms |
| warm no-op rerun | builder/snapshot/disk calls all 0 | 536.485 ms; heavy calls 0; file/artifact reads 0; peak 82,791,378 B; payload 23,093,944 B | newly complete instrumentation; HTML composition/copy dominates |
| static geometry cold | 6429.844 ms / 121,737,377 B peak | 6063.210 ms / 121,737,273 B | 5.7% faster observation |
| static geometry warm | 158.821 ms median | 189.024 ms median | +19.0%, run variance; builder stays at one and code path is unchanged |
| dynamic geometry cold | 226.346 ms / 4,798,349 B | 265.659 ms / 4,798,349 B | +17.4%, run variance; implementation unchanged |
| dynamic geometry warm | 1.969 ms median | 1.951 ms median | effectively unchanged |
| compose layers | 217.577 ms / 41,862,440 B | 202.892 ms / 41,862,440 B | 6.8% faster observation |

The meaningful invariant is zero heavy rebuilds and zero artifact reads on a warm no-op, rather than noisy single-run wall time. Static DOM generation remains the primary bottleneck; reducing DOM would be a product/rendering trade-off and is not hidden in this persistence refactor.

## Factual XLSX 10k / 50k / 100k

The importer was not modified. The retained #187/#188 reproducible measurements are 8.786/43.461/83.840 seconds before their optimization and 4.377/20.571/39.796 seconds after it. Peak RSS for 100k remains the documented 196,775,936 bytes after. Reproduce with `python scripts/benchmark_factual_xlsx_import.py --rows 10000 50000 100000`. The openpyxl read-only streaming loop, parser v5, progress callback and atomic staged publication remain intact.

## Readiness synthetic large index

No readiness implementation changed. The #180 streaming business-index and SQLite conflict architecture and its existing contract tests remain the regression evidence; this PR does not materialize index rows. No new comparable wall-clock number is claimed.

## One-day and monthly FACT

No replay implementation changed. Existing deterministic one-day/monthly and checkpoint compatibility tests cover behavior. The #183 day-by-day streaming, compact manifest, resume and lazy details remain intact; no month is materialized. No new comparable wall-clock number is claimed.

## Representative routing workload

No graph/routing implementation changed. Fixed-route and physical-graph tests remain equivalence checks. This PR does not create an all-pairs matrix or unbounded shortest-path cache. No before/after speedup is claimed.

## Representative optimizer workload

No optimizer implementation changed, so small/medium/16k behavior and complexity are unchanged. Deterministic optimizer tests remain equivalence evidence. Profiling did not justify a risky algorithmic rewrite within this persistence-focused slice; no speedup is claimed.

## Memory and compatibility conclusion

Tracemalloc peaks are shown above. Static and dynamic peaks are effectively identical; the no-op figure includes production cache return copies and 22.02 MiB HTML composition. The new JSONL utility is lazy, and atomic writes hold only the serialized artifact being written. No factual/monthly/geometry data is duplicated or materialized by the refactor. There is no important measured regression with an implementation change; noisy unchanged geometry timings are reported rather than concealed.
