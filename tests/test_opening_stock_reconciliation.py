from __future__ import annotations

import copy
import inspect
import json

import pytest

import warehouse_opening_stock_reconciliation as reconciliation
from warehouse_outbound_orders import make_outbound_sku_key
from warehouse_pick_inventory import build_pickable_inventory_index


def model():
    return {
        "model_id": "m1", "source_file_hash": "h1",
        "cells": [
            {"row_number": "10", "cell_number": "10", "tier": "1", "row_order": 2, "weight_zone": "light"},
            {"row_number": "10", "cell_number": "2", "tier": "1", "row_order": 1, "weight_zone": "heavy"},
            {"row_number": "10", "cell_number": "3", "tier": "1", "row_order": 1, "weight_zone": "heavy"},
        ],
    }


def inventory(qty=40, **changes):
    row = {"nomenclature": "A", "characteristic": "red", "qty_units": qty, "unit_name": "короб", "source_index": 1}
    row.update(changes)
    return row


def location(cell="10|2|1", qty=1, sku_key=None, **changes):
    row = {"sku_key": sku_key or make_outbound_sku_key("A", "red"), "cell_key": cell, "qty_units": qty, "confidence": "exact"}
    row.update(changes)
    return row


def run(rows=None, locations=None, warehouse=None):
    return reconciliation.reconcile_opening_stock(warehouse or model(), rows or [], {"placements": locations or []})


def test_empty_inputs_and_json_serializable_outputs():
    state, diagnostics = run()
    assert state["placements"] == [] and state["unplaced_inventory"] == []
    json.dumps(state); json.dumps(diagnostics)


def test_inputs_are_not_modified_and_runs_are_deterministic():
    inputs = (model(), [inventory()], {"placements": [location()]})
    originals = copy.deepcopy(inputs)
    first = reconciliation.reconcile_opening_stock(*inputs)
    second = reconciliation.reconcile_opening_stock(*inputs)
    assert inputs == originals
    assert first == second


def test_sku_key_is_built_with_outbound_helper_and_explicit_key_is_preserved():
    state, _ = run([inventory()], [location()])
    assert state["placements"][0]["sku_key"] == make_outbound_sku_key("A", "red")
    state, _ = run([inventory(sku_key="explicit")], [location(sku_key="explicit")])
    assert state["placements"][0]["sku_key"] == "explicit"


def test_duplicate_inventory_rows_are_merged_with_sorted_indexes():
    rows = [inventory(10, source_index=9), inventory(30, source_index=2)]
    state, diagnostics = run(rows, [location()])
    assert state["placements"][0]["qty_units"] == 40
    assert state["placements"][0]["source_indexes"] == [2, 9]
    assert diagnostics["merged_inventory_rows"] == 1


@pytest.mark.parametrize("unit", ["короб", "  КОРОБА ", "коробов"])
def test_supported_box_units(unit):
    state, _ = run([inventory(unit_name=unit)], [location()])
    assert state["placements"][0]["unit_name"] == "короб"


def test_unsupported_unit_is_excluded():
    state, diagnostics = run([inventory(unit_name="шт")])
    assert state["excluded_inventory"][0]["reason"] == "unsupported_inventory_unit"
    assert diagnostics["unsupported_inventory_unit"] == 1


@pytest.mark.parametrize("qty,reason", [(None, "inventory_quantity_missing"), ("", "inventory_quantity_missing"), (1.5, "inventory_quantity_invalid"), (True, "inventory_quantity_invalid"), (float("nan"), "inventory_quantity_invalid"), (-1, "inventory_quantity_non_positive"), (0, "inventory_quantity_non_positive")])
def test_invalid_inventory_quantities_are_excluded(qty, reason):
    state, diagnostics = run([inventory(qty)])
    assert state["excluded_inventory"][0]["reason"] == reason
    assert diagnostics[reason] == 1


@pytest.mark.parametrize("qty", [40, 40.0, "40", "40,0", "40.0"])
def test_valid_integer_quantity_forms(qty):
    state, _ = run([inventory(qty)], [location()])
    assert state["placements"][0]["qty_units"] == 40


def test_single_cell_uses_complete_inventory_not_reported_quantity():
    state, diagnostics = run([inventory(40)], [location(qty=1)])
    placement = state["placements"][0]
    assert placement["qty_units"] == placement["qty_boxes"] == 40
    assert placement["reported_location_qty_units"] == 1
    assert placement["allocation_method"] == "exact_single_cell"
    assert diagnostics["exact_single_cell_boxes"] == 40


def test_exact_multi_cell_reported_distribution():
    state, _ = run([inventory(40)], [location(qty=25), location("10|3|1", 15)])
    assert [p["qty_units"] for p in state["placements"]] == [25, 15]
    assert {p["allocation_method"] for p in state["placements"]} == {"exact_reported_distribution"}


def test_mismatch_with_informative_weights_uses_integral_largest_remainders():
    state, diagnostics = run([inventory(41)], [location(qty=30), location("10|3|1", 10)])
    quantities = [p["qty_units"] for p in state["placements"]]
    assert quantities == [31, 10]
    assert all(isinstance(q, int) for q in quantities) and sum(quantities) == 41
    assert diagnostics["estimated_proportional_boxes"] == 41


@pytest.mark.parametrize("weights", [(1, 1), (4, 4), (0, 0)])
def test_uninformative_weights_are_even_and_remainder_follows_physical_order(weights):
    state, diagnostics = run([inventory(41)], [location("10|10|1", weights[0]), location("10|2|1", weights[1])])
    assert [(p["cell_key"], p["qty_units"]) for p in state["placements"]] == [("10|2|1", 21), ("10|10|1", 20)]
    assert {p["allocation_method"] for p in state["placements"]} == {"estimated_even"}
    if weights == (1, 1):
        assert diagnostics["technical_one_box_location_sku_count"] == 1


def test_same_cell_production_dates_merge_but_different_cells_do_not():
    locations = [location(qty=1, production_date="2026-02-01"), location(qty=2, production_date="2026-01-01")]
    state, _ = run([inventory(40)], locations)
    assert len(state["placements"]) == 1
    assert state["placements"][0]["reported_location_qty_units"] == 3
    assert state["placements"][0]["production_dates"] == ["2026-01-01", "2026-02-01"]
    state, _ = run([inventory(40)], [location(), location("10|3|1")])
    assert len(state["placements"]) == 2


def test_unknown_inventory_stale_hints_and_unknown_cells_are_separated():
    stale = location(sku_key="stale", cell="10|3|1", qty=99)
    state, diagnostics = run([inventory(7)], [location(cell="999|1|1"), stale])
    assert state["placements"] == [] and state["unplaced_inventory"] == []
    assert state["unknown_location_inventory"][0]["qty_units"] == 7
    assert state["unknown_location_inventory"][0]["reason"] == "no_location_hint"
    assert state["stale_location_hints"][0]["reason"] == "location_without_inventory"
    assert diagnostics["location_records_unknown_cell"] == 1


def test_placement_metadata_does_not_copy_source_confidence():
    state, _ = run([inventory()], [location(confidence="exact")])
    placement = state["placements"][0]
    assert placement["confidence"] == "exact_location_estimated_quantity_distribution"
    assert placement["quantity_source"] == "inventory"
    assert placement["location_source"] == "actual_placement_snapshot"
    assert placement["occupied_capacity_pallets"] == 0.0


def test_pickable_index_receives_only_reconciled_located_boxes():
    rows = [inventory(40), inventory(9, sku_key="unknown", nomenclature="B")]
    locations = [location(qty=1), location(sku_key="stale", cell="10|3|1", qty=50)]
    state, _ = run(rows, locations)
    index = build_pickable_inventory_index(model(), state)
    records = [record for records in index["by_sku"].values() for record in records]
    assert len(records) == 1
    assert records[0]["sku_key"] == make_outbound_sku_key("A", "red")
    assert records[0]["qty_units"] == 40


def test_module_has_no_streamlit_or_file_writes():
    source = inspect.getsource(reconciliation)
    assert "streamlit" not in source
    assert "open(" not in source and ".write(" not in source
