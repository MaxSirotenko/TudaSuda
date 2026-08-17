#!/usr/bin/env python3
"""Static guard for direct legacy reader calls in production functions."""
from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = {"load_outbound_orders", "load_outbound_orders_cached", "read_outbound_table",
           "load_receipts_state", "load_receipts_state_cached", "read_receipt_table",
           "load_placement_state", "load_placement_state_cached", "read_inventory_table"}
# Function-level compatibility boundaries, never whole-module exemptions.
ALLOW = {
    ("virtual_warehouse_app.py", "render_receipts_section"): "explicit receipt manual fallback",
    ("virtual_warehouse_app.py", "render_outbound_picking"): "explicit outbound manual fallback / mutable execution",
    ("virtual_warehouse_app.py", "render_inventory_section"): "explicit inventory reconciliation",
    ("virtual_warehouse_app.py", "render_inventory_placement"): "explicit inventory reconciliation",
    ("virtual_warehouse_app.py", "render_placement_diagnostics_section"): "derived-state diagnostics",
    ("virtual_warehouse_app.py", "_current_warehouse_state"): "derived mutable warehouse state",
    ("virtual_warehouse_app.py", "render_diagnostics_section"): "derived-state diagnostics",
    ("virtual_warehouse_app.py", "render_placement_section"): "derived mutable placement",
    ("virtual_warehouse_app.py", "render_warehouse_map"): "derived mutable placement map",
    ("virtual_warehouse_app.py", "execute"): "mutable outbound execution state",
    ("virtual_warehouse_app.py", "render_operation_history"): "derived operation history",
    ("virtual_warehouse_app.py", "render_analytics_tab"): "derived placement analytics",
    ("virtual_warehouse_app.py", "render_service_tab"): "explicit service/compatibility tools",
    ("virtual_warehouse_app.py", "render_rules_workspace"): "derived classification overlay",
    ("virtual_warehouse_app.py", "render_zone_boundaries_editor"): "user configuration over mutable state",
    ("virtual_warehouse_app.py", "render_geometry_map_view"): "derived mutable placement map",
    ("warehouse_outbound_experiment_ui.py", "render_outbound_experiment"): "explicit scenario manual fallback",
    ("warehouse_inventory_placement.py", "reconcile_inventory"): "mutable placement boundary",
}
COMPATIBILITY_MODULES = {"warehouse_state_cache.py", "warehouse_outbound_orders.py", "warehouse_receipts.py",
                         "warehouse_inventory_placement.py", "warehouse_import_cache.py",
                         "warehouse_performance_benchmark.py"}

class Calls(ast.NodeVisitor):
    def __init__(self): self.scope = "<module>"; self.rows = []
    def visit_FunctionDef(self, node):
        previous = self.scope; self.scope = node.name; self.generic_visit(node); self.scope = previous
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_Call(self, node):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name in SYMBOLS: self.rows.append((self.scope, name, node.lineno))
        self.generic_visit(node)

def main() -> int:
    findings = []
    for path in sorted(ROOT.glob("*.py")):
        visitor = Calls(); visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for function, symbol, line in visitor.rows:
            key = (path.name, function); allowed = key in ALLOW or path.name in COMPATIBILITY_MODULES
            classification = ALLOW.get(key, "compatibility implementation" if path.name in COMPATIBILITY_MODULES else "modern production call")
            findings.append((path.name, function, line, symbol, classification, allowed))
    print("module\tfunction\tline\tlegacy symbol\tclassification\tallowed")
    for row in findings: print("\t".join(map(str, [*row[:-1], "yes" if row[-1] else "NO"])))
    return 1 if any(not row[-1] for row in findings) else 0

if __name__ == "__main__": raise SystemExit(main())
