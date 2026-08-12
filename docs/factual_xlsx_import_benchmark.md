# Factual XLSX streaming-import benchmark

Measured on 2026-08-12 with CPython 3.14.4 on the same managed runner. The
standalone command generates an outbound parser-v5 workbook before timing only
the importer. Timings are observations, not CI assertions.

## Root cause

A `cProfile` run of the base importer over 1,000 rows recorded 19,000
`strptime` calls. They consumed 0.865 s of the 1.899 s import: canonical ISO
dates were passed through the complete source-format scan again for both day
indexing and business evidence. On the optimized 100,000-row run, stage timing
was: XLSX iteration 38.6%, canonical conversion 9.3%, RAW serialization 8.0%,
business evidence 7.3%, all gzip writes 6.7%, canonical serialization 5.3%,
RAW preparation 3.7%, business serialization 3.5%, index maintenance 3.2%, and
RAW hashing 1.5%. Thus gzip was measurable but not the primary bottleneck.

The importer now recognizes its own unambiguous ISO date output directly,
profiles each persisted pipeline stage, precomputes header positions, and uses
gzip level 6. A separate 10,000-row compression comparison measured levels
1/3/6/9 at 3.784/4.163/3.961/4.287 seconds respectively. Level 6 kept artifact
sizes close to level 9 without paying level 9's write cost.

## Before / after

| Rows | Before | After | Speedup |
|---:|---:|---:|---:|
| 10,000 | 8.786 s | 4.377 s | 2.01x |
| 50,000 | 43.461 s | 20.571 s | 2.11x |
| 100,000 | 83.840 s | 39.796 s | 2.11x |

| Rows | Artifact | Before | After | Trade-off |
|---:|---|---:|---:|---:|
| 100,000 | RAW | 1,395,624 B | 1,568,022 B | +12.4% |
| 100,000 | canonical | 1,680,220 B | 1,700,322 B | +1.2% |
| 100,000 | business index | 5,059,010 B | 5,187,239 B | +2.5% |

Peak RSS was 197,423,104 bytes before and 196,775,936 bytes after at 100,000
rows. The path remains `openpyxl` read-only streaming; no complete RAW or
canonical row collection was introduced. Persisted JSONL schemas, parser
version, hashes, duplicate/business-key rules, and ordinary gzip compatibility
are unchanged.

Reproduce with:

```bash
python scripts/benchmark_factual_xlsx_import.py --rows 10000 50000 100000
```
