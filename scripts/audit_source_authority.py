#!/usr/bin/env python3
"""Small static guard against direct legacy readers in modern scenario modules."""
from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = {"load_outbound_orders", "load_outbound_orders_cached", "read_outbound_table",
           "load_receipts_state", "load_receipts_state_cached", "read_receipt_table",
           "load_placement_state", "load_placement_state_cached", "read_inventory_table"}
ALLOW = {
    "virtual_warehouse_app.py": "explicit legacy/manual workspace",
    "warehouse_outbound_experiment_ui.py": "explicit manual fallback adapter",
    "warehouse_state_cache.py": "compatibility cache boundary",
    "warehouse_outbound_orders.py": "legacy persistence boundary",
    "warehouse_receipts.py": "legacy/derived receipts boundary",
    "warehouse_inventory_placement.py": "mutable derived placement boundary",
    "warehouse_import_cache.py": "manual import cache boundary",
    "warehouse_performance_benchmark.py": "benchmark harness",
}

def main() -> int:
    findings = []
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom): used.update(alias.name for alias in node.names if alias.name in SYMBOLS)
            elif isinstance(node, ast.Name) and node.id in SYMBOLS: used.add(node.id)
        for symbol in sorted(used):
            allowed = path.name in ALLOW
            findings.append((path.name, symbol, ALLOW.get(path.name, "modern production module"), allowed))
    print("module\tlegacy symbol\tclassification\tallowed")
    for row in findings: print("\t".join([row[0], row[1], row[2], "yes" if row[3] else "NO"]))
    return 1 if any(not row[3] for row in findings) else 0

if __name__ == "__main__": raise SystemExit(main())
