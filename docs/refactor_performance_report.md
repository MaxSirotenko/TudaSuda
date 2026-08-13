# PR #189 final stabilization performance report

## Environment and method

All final geometry comparisons below were run consecutively on the same Linux x86_64 runner (CPython 3.14.4, Streamlit 1.61.1, 3 exposed logical CPUs) using base `819fa524` in a detached worktree and final PR code. Dataset: deterministic 16,000 cells, 700 placements in 500 cells. Commands use `--warm-iterations 5 --import-iterations 5`; timings are diagnostics, not CI gates.

## Geometry: base vs final

Both sides used `python scripts/run_warehouse_performance_benchmark.py --mode synthetic --cells 16000 --occupied-cells 500 --placements 700 --warm-iterations 5 --import-iterations 5`. The base production warm no-op was additionally timed five times using its actual sequence (cached state → revision tokens → cached static/dynamic layers → dynamic size → compose → diagnostics), because the old benchmark did not expose its elapsed time.

| Scenario | Base `819fa524` | Final | Notes |
|---|---:|---:|---|
| static cold | 5903.497 ms | 6377.015 ms | single cold observation; unchanged renderer |
| static warm | median 157.259 ms (152.441–196.935) | 176.513 ms (154.122–222.988) | builder count remains one |
| dynamic cold | 255.735 ms | 215.392 ms | unchanged dynamic algorithm |
| dynamic warm | median 1.821 ms (1.778–2.171) | 1.832 ms (1.817–2.076) | effectively unchanged |
| compose | 193.185 ms | 244.392 ms | single observation; identical 23,093,944-byte output |
| production warm no-op | median 658.747 ms (620.623–705.342) | median 700.616 ms (653.155–770.163) | final includes scoped I/O accounting; payload unchanged |

The warm no-op difference is +6.4% median on a large 22 MiB payload; there are zero static/dynamic rebuilds. No UI/renderer business behavior was changed to chase noisy serialization timings. Static DOM generation remains the dominant cost.

## Real persisted-read instrumentation

The final application scenario invokes the same production boundary as the app: `load_placement_state_cached` followed by `render_geometry_layers`. Synthetic state is written to a temporary persisted file and its real production cache is primed before measurement—there is no benchmark lambda pretending to be a loader. Five warm iterations recorded **10 file reads total (2 per iteration)**, both revision-state reads; placement JSON remained a genuine warm cache hit. Artifact reads were zero. Production JSON readers for geometry/manual overrides, placement, receipts, outbound, render settings and revisions are instrumented through `read_json`; factual registry/artifact boundaries report their own reads. A regression test invokes the real placement loader and observes a positive probe count.

## Startup/import time

Seven fresh subprocess imports on base had median 2100.990 ms (1965.320–2721.957); the prior final profiling pass measured 2075.408 ms (1984.889–2259.777). `-X importtime` identified pandas and Streamlit as dominant. The outbound experiment/scenario/optimizer UI remains a single lazy screen boundary.

## Factual XLSX 10k / 50k / 100k

Final HEAD measurements from the same PR environment: **7.888 / 25.963 / 52.672 s**, 1267.8 / 1925.8 / 1898.5 rows/s, peak RSS 168,419,328 / 180,609,024 / 197,419,008 B. A same-runner base 100k control was 52.557 s and 195,518,464 B (+0.2% elapsed, +1.0% RSS final). The openpyxl read-only importer, parser `factual-july-v5`, authoritative quantity, atomic staging and schemas are unchanged.

## Routing and optimizer

`python scripts/benchmark_refactor_hotpaths.py --cells 16000 --occupied 500 --repeats 5` measured physical graph build median 591.055 ms (496.070–617.193), reachable shortest path median 0.054 ms (0.050–0.165), and optimizer median 925.710 ms (870.474–988.531), producing a valid ready 500-placement plan. No routing policy, all-pairs cache, or optimizer semantics changed.

## Readiness, FACT and compatibility

Final targeted suites cover complete-July readiness, streaming/SQLite conflicts, VGH warnings, exact historical cells, deterministic one-day/monthly replay and monthly-fact-v2 checkpoint resume. No business contract, persisted business schema, SKU identity, `РасчетноеОтгруженоКоробок`, CURRENT/FACT/PROPOSED rule or `queries_1c` file changed.
