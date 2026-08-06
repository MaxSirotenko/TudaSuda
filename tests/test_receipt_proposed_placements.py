import copy
import json

import pytest

from warehouse_receipt_proposed_placements import LIMITATIONS, build_proposed_receipt_placements


def model(*cells, model_id="M"):
    return {"model_id": model_id, "cells": [{"cell_key": key, "weight_zone": zone, "row_order": row, "physical_index": index, "row_number": row, "cell_number": index, "tier": 1, "capacity_pallets": 99, "deep_lane_width": 9} for key, zone, row, index in cells]}


def day(*rows, dataset="D"):
    batches = [{"receipt_batch_key": key, "dataset_id": dataset, "normalized_warehouse": warehouse, "warehouse": warehouse.upper(), "sku_key": sku, "qty_units": qty, "unit_name": "короб"} for key, warehouse, sku, qty in rows]
    proposed = {"receipt_dataset_id": dataset, "receipt_batch_keys": [x[0] for x in rows], "total_boxes": sum(x[3] for x in rows)}
    return {"dataset_id": dataset, "operational_date": "2026-08-06", "receipt_sku_batches": batches, "scenario_inputs": {"proposed": proposed}}


def rules(*rows, dataset="D", target="w"):
    return {"slotting_rule_state_id": "R", "dataset_id": dataset, "target_normalized_warehouse": target, "zone_order": ["heavy", "medium", "light", "fragile"], "sku_rules": [{"rule_key": key, "normalized_warehouse": warehouse, "sku_key": sku, "weight_zone": zone, "priority_rank": rank, "source": "test_fixture"} for key, warehouse, sku, zone, rank in rows]}


def opening(*rows, model_id="M"):
    return {"model_id": model_id, "placements": [{"placement_id": str(i), "cell_key": cell, "sku_key": sku, "qty_units": qty, "unit_name": unit} for i, (cell, sku, qty, unit) in enumerate(rows)]}


M = (("C1", "heavy", 1, 1), ("C2", "heavy", 1, 2), ("C3", "light", 2, 1), ("C4", "light", 2, 2), ("C5", "light", 2, 3), ("C6", "fragile", 3, 1))


def build(d=None, r=None, o=None, m=None):
    return build_proposed_receipt_placements(m or model(*M), d or day(("B", "w", "SKU", 10)), o or opening(), r or rules(("RULE", "w", "SKU", "heavy", 1)))


def test_empty_valid_day():
    state, diagnostics = build(d=day(), r=rules())
    assert state["placements"] == state["unresolved_receipt_batches"] == []
    assert state["limitations"] == LIMITATIONS and diagnostics["receipt_boxes_total"] == 0


def test_first_free_exact_zone_and_opening_stock_even_same_sku_blocks():
    state, _ = build(o=opening(("C1", "SKU", 1, "короб")))
    assert [(x["cell_key"], x["qty_units"], x["weight_zone"]) for x in state["placements"]] == [("C2", 10, "heavy")]
    assert state["occupied_opening_stock_cell_keys"] == ["C1"]


def test_priority_then_descending_quantity_assign_distinct_cells():
    d = day(("B1", "w", "S1", 5), ("B2", "w", "S2", 20))
    r = rules(("R1", "w", "S1", "heavy", 1), ("R2", "w", "S2", "heavy", 1))
    state, _ = build(d=d, r=r)
    assert {(x["sku_key"], x["cell_key"]) for x in state["placements"]} == {("S2", "C1"), ("S1", "C2")}
    r["sku_rules"][0]["priority_rank"] = 0
    state, _ = build(d=d, r=r)
    assert {(x["sku_key"], x["cell_key"]) for x in state["placements"]} == {("S1", "C1"), ("S2", "C2")}


def test_permutations_repeat_immutability_json_and_capacity_ignored():
    d = day(("B1", "w", "S1", 500), ("B2", "w", "S2", 20)); r = rules(("R1", "w", "S1", "heavy", 1), ("R2", "w", "S2", "light", 1)); o = opening(("C6", "X", 2, "короб")); m = model(*M)
    before = copy.deepcopy((m, d, o, r)); first = build_proposed_receipt_placements(m, d, o, r)
    second = build_proposed_receipt_placements(m, {**d, "receipt_sku_batches": list(reversed(d["receipt_sku_batches"]))}, {**o, "placements": list(reversed(o["placements"]))}, {**r, "sku_rules": list(reversed(r["sku_rules"]))})
    assert first == second == build_proposed_receipt_placements(m, d, o, r); assert (m, d, o, r) == before
    json.dumps(first, ensure_ascii=False); assert first[0]["summary"]["placed_qty_units"] == 520


@pytest.mark.parametrize("kind,reason", [("missing", "slotting_rule_missing"), ("invalid", "slotting_rule_missing_or_invalid"), ("full", "no_free_cell_in_weight_zone")])
def test_unresolved_without_fallback(kind, reason):
    r = rules() if kind == "missing" else rules(("R", "w", "SKU", "bad" if kind == "invalid" else "heavy", 1))
    o = opening(("C1", "A", 1, "короб"), ("C2", "B", 1, "короб")) if kind == "full" else opening()
    state, _ = build(r=r, o=o)
    assert state["placements"] == [] and state["unresolved_receipt_batches"][0]["reason_code"] == reason


@pytest.mark.parametrize("mutation", ["key", "combo"])
def test_duplicate_rules_reject_all_conflicts(mutation):
    items = [("R", "w", "SKU", "heavy", 1), ("R" if mutation == "key" else "R2", "w", "SKU" if mutation == "combo" else "OTHER", "light", 2)]
    state, diagnostics = build(r=rules(*items))
    assert state["invalid_slotting_rules"] and state["placements"] == []
    assert diagnostics["duplicate_rule_keys" if mutation == "key" else "duplicate_rule_combinations"] == 1


def test_duplicate_batches_reject_all():
    d = day(("B", "w", "SKU", 1), ("B", "w", "SKU", 1)); state, _ = build(d=d)
    assert len(state["invalid_receipt_batches"]) == 2 and state["summary"]["valid_receipt_batches"] == 0


def test_unassigned_unknown_and_invalid_opening_do_not_block():
    m = model(("U", "unassigned", 0, 0), *M)
    o = opening(("BAD", "X", 1, "короб"), ("C1", "X", 0, "короб"), ("C2", "X", 1, "шт"))
    state, diagnostics = build(m=m, o=o)
    assert state["placements"][0]["cell_key"] == "C1"
    assert diagnostics["unsupported_zone_model_cells"] == diagnostics["unknown_opening_stock_cells"] == diagnostics["non_positive_opening_stock_placements"] == 1


@pytest.mark.parametrize("change,error", [
    (lambda d, r, o, m: r.update(dataset_id="X"), "slotting_rule_dataset_id_mismatch"),
    (lambda d, r, o, m: d["scenario_inputs"]["proposed"].update(total_boxes=999), "proposed_total_boxes_mismatch"),
    (lambda d, r, o, m: d["receipt_sku_batches"].append({**d["receipt_sku_batches"][0], "receipt_batch_key": "B2", "normalized_warehouse": "x"}), "multiple_receipt_normalized_warehouses"),
    (lambda d, r, o, m: r.update(target_normalized_warehouse="x"), "target_normalized_warehouse_mismatch"),
    (lambda d, r, o, m: o.update(model_id="X"), "opening_stock_model_id_mismatch"),
])
def test_configuration_errors_block_all(change, error):
    d = day(("B", "w", "SKU", 10)); r = rules(("R", "w", "SKU", "heavy", 1)); o = opening(); m = model(*M)
    change(d, r, o, m)
    if error == "multiple_receipt_normalized_warehouses": d["scenario_inputs"]["proposed"].update(receipt_batch_keys=["B", "B2"], total_boxes=20)
    state, diagnostics = build_proposed_receipt_placements(m, d, o, r)
    assert state["placements"] == [] and error in diagnostics["configuration_errors"]
    assert state["summary"]["placed_qty_units"] + state["summary"]["unresolved_qty_units"] == state["summary"]["valid_receipt_qty_units"]


def test_integration_c1_c6():
    d = day(("B-B", "w", "SKU B", 100), ("B-C", "w", "SKU C", 50), ("B-D", "w", "SKU D", 20))
    r = rules(("RB", "w", "SKU B", "heavy", 1), ("RC", "w", "SKU C", "light", 1), ("RD", "w", "SKU D", "light", 2))
    o = opening(("C1", "SKU A", 30, "короб"), ("C3", "SKU C", 10, "короб")); before = copy.deepcopy(o)
    state, _ = build(d=d, r=r, o=o)
    assert {(x["sku_key"], x["cell_key"], x["qty_units"]) for x in state["placements"]} == {("SKU B", "C2", 100), ("SKU C", "C4", 50), ("SKU D", "C5", 20)}
    assert state["summary"]["placed_qty_units"] == 170 and state["summary"]["unresolved_qty_units"] == 0 and state["summary"]["quantity_conservation_ok"]
    assert all(not x["is_virtual"] and not x["physical_capacity_recalculated"] for x in state["placements"]); assert o == before
