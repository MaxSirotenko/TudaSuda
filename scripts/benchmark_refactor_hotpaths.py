"""Repeatable routing and optimizer benchmarks for the refactor report."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warehouse_performance_benchmark import generate_synthetic_dataset
from warehouse_physical_graph import build_physical_warehouse_graph, find_shortest_path
from warehouse_placement_rules import build_placement_rule_set
from warehouse_proposed_placement_optimizer import build_proposed_placement_plan


def timed(callback, repeats: int) -> dict:
    values, last = [], None
    for _ in range(repeats):
        started = time.perf_counter_ns(); last = callback()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(values)
    return {"median_ms": statistics.median(values), "min_ms": min(values),
            "max_ms": max(values), "p95_ms": ordered[min(len(ordered) - 1, int(.95 * len(ordered)))],
            "iterations": repeats, "result": last}


def optimizer_input(cells: int, occupied: int):
    model, _ = generate_synthetic_dataset(cells, 0, 0)
    model["model_id"] = "optimizer-benchmark"
    model_cells, positions, lots, occupancy, zones = [], [], [], [], []
    for index, source in enumerate(model["cells"]):
        key, position = f"cell:{index}", f"P{index}"
        zone = ("heavy", "medium", "light")[index % 3]
        cell = dict(source, cell_key=key, weight_zone=zone, storage_type="normal", capacity_pallets=1)
        model_cells.append(cell)
        sku = f"SKU{index}" if index < occupied else None
        positions.append({"position_id": position, "cell_key": key, "slot_index": 1,
                          "status": "occupied" if sku else "free",
                          "occupied_stock_lot_ids": [f"L{index}"] if sku else []})
        occupancy.append({"cell_key": key, "storage_type": "normal", "capacity_pallet_positions": 1,
                          "occupancy_conflict": False})
        if sku:
            lots.append({"stock_lot_id": f"L{index}", "sku_key": sku, "qty_boxes": 10,
                         "location_status": "located", "cell_key": key, "position_id": position})
            zones.append({"sku_key": sku, "target_zone": zone, "source": "benchmark"})
    model["cells"] = model_cells
    state = {"simulation_state_id": "optimizer-state", "model_id": model["model_id"],
             "target_normalized_warehouse": "benchmark", "physical_positions": positions,
             "cell_occupancy": occupancy, "stock_lots": lots, "pallet_units": []}
    rules = build_placement_rule_set({"weight_zones": True})[0]
    return model, state, rules, zones


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--cells", type=int, default=16_000)
    parser.add_argument("--occupied", type=int, default=500); parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    model, _ = generate_synthetic_dataset(args.cells, 0, 0)
    gates = {"model_id": model["model_id"], "gates": [{"gate_key": "g", "road_type": "bottom", "x": 1, "y": 1}]}
    route = timed(lambda: build_physical_warehouse_graph(model, gates), args.repeats)
    graph = route.pop("result")[0]
    components = sorted(graph.get("connected_components", []), key=lambda row: row.get("node_count", 0), reverse=True)
    node_ids = components[0].get("node_ids", []) if components else []
    endpoints = (node_ids[0], node_ids[-1]) if node_ids else (None, None)
    shortest = timed(lambda: find_shortest_path(graph, *endpoints), args.repeats) if all(endpoints) else {"status": "skipped"}
    if "result" in shortest:
        shortest["reachable"] = shortest.pop("result").get("reachable")
    inputs = optimizer_input(args.cells, min(args.occupied, args.cells))
    optimizer = timed(lambda: build_proposed_placement_plan(*inputs), args.repeats)
    plan, diagnostics = optimizer.pop("result")
    optimizer.update(status=plan.get("status"), valid=diagnostics.get("valid"), placements=len(plan.get("placements", [])))
    print(json.dumps({"dataset": {"cells": args.cells, "occupied_skus": args.occupied},
                      "graph_build": route, "shortest_path": shortest, "optimizer": optimizer}, indent=2))


if __name__ == "__main__":
    main()
