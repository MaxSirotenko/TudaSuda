#!/usr/bin/env python3
"""Reproducible, non-CI benchmark for the factual streaming XLSX importer."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warehouse_factual_data import import_excel_dataset  # noqa: E402

try:
    import resource
except ImportError:  # Windows: keep the benchmark usable, just omit RSS.
    resource = None

HEADERS = ("СсылкаРО", "НомерРО", "ДатаРО", "Склад", "НомерСтроки", "Номенклатура",
           "Характеристика", "Количество", "РасчетноеОтгруженоКоробок")


def create_workbook(path: Path, rows: int) -> None:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Данные")
    sheet.append(HEADERS)
    for number in range(1, rows + 1):
        document = (number - 1) // 20
        sheet.append((f"ref-{document}", f"РО-{document}", "2026-07-15", "Склад", number,
                      f"Товар-{number % 2000}", f"Характеристика-{number % 5}", 24, 3))
    workbook.save(path)


def benchmark(rows: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="factual-benchmark-") as directory:
        root = Path(directory)
        workbook_path = root / f"outbound-{rows}.xlsx"
        create_workbook(workbook_path, rows)
        data = workbook_path.read_bytes()
        started = time.perf_counter()
        result = import_excel_dataset(data, workbook_path.name, root=root / "artifacts")
        elapsed = time.perf_counter() - started
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 if resource else None
        artifact = Path(result["artifact"])
        canonical_size = sum(path.stat().st_size for path in (artifact / "canonical").glob("*.gz"))
        return {"rows": rows, "xlsx_bytes": len(data), "elapsed_seconds": round(elapsed, 3),
                "rows_per_second": round(rows / elapsed, 1), "peak_rss_bytes": peak,
                "artifact_bytes": {"raw": (artifact / "raw.jsonl.gz").stat().st_size,
                    "canonical": canonical_size,
                    "business_index": (artifact / "business_index.jsonl.gz").stat().st_size},
                "stage_seconds": result.get("diagnostics", {}).get("import_performance", {}).get("stage_seconds", {})}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", nargs="+", type=int, default=[10_000, 50_000, 100_000])
    args = parser.parse_args()
    for rows in args.rows:
        print(json.dumps(benchmark(rows), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
