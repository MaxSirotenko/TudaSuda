from __future__ import annotations

import copy
import inspect
import json

import pytest

from warehouse_business_identity import canonical_sku_key
from warehouse_opening_stock_reconciliation import reconcile_opening_stock
from warehouse_simulation_state import (
    build_initial_simulation_state,
    compute_simulation_state_id,
    summarize_simulation_state,
    validate_simulation_state,
)


def normal(key="1|1|1"):
    row, cell, tier = key.split("|")
    return {"cell_key": key, "row_number": row, "cell_number": cell, "tier": tier,
            "storage_type": "normal", "capacity_pallets": 1, "physical_slots": []}


def deep(key="2|1|1", capacity=5, slots=None):
    row, cell, tier = key.split("|")
    if slots is None:
        slots = [{"slot_index": index, "capacity_pallets": 1} for index in range(1, capacity + 1)]
    return {"cell_key": key, "row_number": row, "cell_number": cell, "tier": tier,
            "storage_type": "deep_lane", "row_storage_type": "deep_lane",
            "deep_lane_width": capacity, "capacity_pallets": capacity, "physical_slots": slots}


def model(cells=None):
    return {"model_id": "model-1", "source_file_hash": "source-1", "cells": cells if cells is not None else [normal(), deep()]}


def item(name="A", qty=10, **changes):
    value = {"sku_key": canonical_sku_key({"nomenclature": name, "characteristic": "red"}),
             "nomenclature": name, "characteristic": "red", "qty_units": qty, "unit_name": "короб",
             "production_dates": ["2026-01-02", "2026-01-01"]}
    value.update(changes)
    return value


def opening(placements=None, unknown=None, **metadata):
    return {"placements": placements or [], "unknown_location_inventory": unknown or [], **metadata}


def build(cells=None, placements=None, unknown=None, opening_changes=None):
    return build_initial_simulation_state(
        model(cells), opening(placements, unknown, **(opening_changes or {})),
        target_normalized_warehouse=" ВЁШКИ ", simulation_time="2026-07-15T00:00:00",
    )


def test_empty_state_and_ordinary_and_deep_position_registries():
    state, diagnostics = build()
    assert state["simulation_state_version"] == 3
    assert state["target_normalized_warehouse"] == "вешки"
    assert state["summary"]["total_boxes"] == 0
    assert [p["position_id"] for p in state["physical_positions"]] == [
        "position:1|1|1:1", *[f"position:2|1|1:{i}" for i in range(1, 6)]]
    assert {p["status"] for p in state["physical_positions"]} == {"free"}
    assert diagnostics["configuration_errors"] == []
    assert validate_simulation_state(state, model())["valid"]


def test_located_and_unknown_boxes_are_conserved_and_unknown_pallet_is_not_zero():
    placed = item(qty=100, cell_key="1|1|1", placement_id="p1", qty_pallets=0,
                  occupancy_not_authoritative=True, allocation_method="exact_single_cell")
    state, diagnostics = build([normal()], placements=[placed], unknown=[item("B", 50)])
    assert state["stock_conservation"] == {
        "opening_boxes_input": 150, "cumulative_receipt_boxes": 0,
        "cumulative_picked_boxes": 0, "expected_stock_boxes": 150,
        "stock_boxes_state": 150, "stock_conservation_ok": True,
    }
    assert state["summary"]["located_boxes"] == 100
    assert state["summary"]["unknown_location_boxes"] == 50
    assert all(lot["pallet_count"] is None and lot["pallet_count_status"] == "unknown" for lot in state["stock_lots"])
    assert all(lot["unit_name"] == "короб" and lot["sku_key"].startswith("sku:v2:") for lot in state["stock_lots"])
    assert state["readiness"]["stock_ready"] is True
    assert state["readiness"]["physical_occupancy_ready"] is False
    assert state["readiness"]["capacity_sensitive_placement_ready"] is False
    assert diagnostics["unknown_location_stock"] == 1


def test_normal_cell_with_stock_is_exact_occupied_and_empty_is_exact_free():
    cells = [normal(), normal("1|2|1")]
    state, _ = build(cells, [item(cell_key="1|1|1")])
    by_key = {cell["cell_key"]: cell for cell in state["cell_occupancy"]}
    assert state["physical_positions"][0]["status"] == "occupied"
    assert (by_key["1|1|1"]["min_occupied_positions"], by_key["1|1|1"]["max_occupied_positions"],
            by_key["1|1|1"]["exact_occupied_positions"]) == (1, 1, 1)
    assert by_key["1|1|1"]["capacity_available_for_new_receipt"] is False
    assert by_key["1|2|1"]["exact_occupied_positions"] == 0
    assert by_key["1|2|1"]["capacity_available_for_new_receipt"] is True
    assert state["readiness"]["physical_occupancy_ready"] is True


def test_unknown_location_prevents_global_free_assumption():
    state, _ = build(unknown=[item(qty=100)])
    assert {position["status"] for position in state["physical_positions"]} == {"unknown"}
    assert all(cell["exact_occupied_positions"] is None for cell in state["cell_occupancy"])


def test_deep_lane_stock_has_bounded_unknown_occupancy_without_depth_assignment():
    state, diagnostics = build([deep()], [item(cell_key="2|1|1", qty=100)])
    occupancy = state["cell_occupancy"][0]
    assert (occupancy["min_occupied_positions"], occupancy["max_occupied_positions"],
            occupancy["exact_occupied_positions"]) == (1, 5, None)
    assert {position["status"] for position in state["physical_positions"]} == {"unknown"}
    assert all(position["occupied_stock_lot_ids"] == [] for position in state["physical_positions"])
    assert occupancy["capacity_available_for_new_receipt"] is False
    assert diagnostics["occupancy_not_authoritative"] == 1


def test_empty_deep_lane_positions_are_free():
    state, _ = build([deep()])
    assert len(state["physical_positions"]) == 5
    assert all(position["status"] == "free" for position in state["physical_positions"])
    assert state["cell_occupancy"][0]["exact_occupied_positions"] == 0


def test_two_lots_in_normal_cell_preserve_stock_but_disable_physical_readiness():
    state, diagnostics = build([normal()], [item("A", 10, cell_key="1|1|1"), item("B", 20, cell_key="1|1|1")])
    assert len(state["stock_lots"]) == 2 and state["summary"]["total_boxes"] == 30
    assert diagnostics["multiple_stock_lots_single_position"] == 1
    assert state["readiness"]["stock_ready"] is True
    assert state["readiness"]["physical_occupancy_ready"] is False


def test_multiple_sku_deep_lane_is_warning_not_stock_loss():
    state, diagnostics = build([deep()], [item("A", 10, cell_key="2|1|1"), item("B", 20, cell_key="2|1|1")])
    assert state["summary"]["total_boxes"] == 30
    assert diagnostics["multiple_sku_deep_lane"] == 1


@pytest.mark.parametrize("slots", [[], [{"slot_index": i} for i in range(1, 5)]])
def test_bad_deep_lane_geometry_is_not_filled_with_guessed_positions(slots):
    state, diagnostics = build([deep(slots=slots)])
    assert state["physical_positions"] == []
    assert diagnostics["physical_slot_contract_invalid"] == 1
    assert state["readiness"]["physical_occupancy_ready"] is False


def test_duplicate_slot_indexes_are_diagnosed_without_duplicate_registry_entries():
    state, diagnostics = build([deep(capacity=2, slots=[{"slot_index": 1}, {"slot_index": 1}])])
    assert state["physical_positions"] == []
    assert diagnostics["physical_slot_contract_invalid"] == 1


def test_unknown_cell_reference_keeps_boxes_and_fails_only_physical_readiness():
    state, diagnostics = build([normal()], [item(cell_key="9|9|1")])
    assert state["summary"]["total_boxes"] == 10
    assert diagnostics["unknown_cell_reference"] == 1
    assert state["readiness"]["stock_ready"] is True
    assert state["readiness"]["physical_occupancy_ready"] is False


@pytest.mark.parametrize("changes,reason", [
    ({"qty_units": -1}, "invalid_box_quantity"),
    ({"qty_units": 1.5}, "invalid_box_quantity"),
    ({"unit_name": "шт"}, "unsupported_unit"),
    ({"sku_key": "legacy"}, "invalid_sku_identity"),
])
def test_invalid_opening_business_values_are_rejected(changes, reason):
    state, diagnostics = build([normal()], [{**item(cell_key="1|1|1"), **changes}])
    assert state["stock_lots"] == []
    assert diagnostics[reason] == 1
    assert state["readiness"]["stock_ready"] is False


def test_zero_placement_is_ignored_and_does_not_occupy():
    state, diagnostics = build([normal()], [item(qty=0, cell_key="1|1|1")])
    assert state["stock_lots"] == []
    assert state["physical_positions"][0]["status"] == "free"
    assert diagnostics["zero_quantity_opening_placement"] == 1


def test_duplicate_lot_identity_collision_is_configuration_error():
    placement = item(cell_key="1|1|1", placement_id="same")
    state, diagnostics = build([normal()], [placement, copy.deepcopy(placement)])
    assert len(state["stock_lots"]) == 2
    assert diagnostics["duplicate_stock_lot_id"] == 1
    assert state["readiness"]["stock_ready"] is False


def test_permutation_stability_no_daily_identity_and_input_immutability_json():
    cells = [normal(), deep()]
    placements = [item("A", 10, cell_key="1|1|1", placement_id="a"), item("B", 20, cell_key="2|1|1", placement_id="b")]
    unknown = [item("C", 30), item("D", 40)]
    first_inputs = (model(cells), opening(placements, unknown, receipt_dataset_id="day-one"))
    originals = copy.deepcopy(first_inputs)
    first, _ = build_initial_simulation_state(*first_inputs, target_normalized_warehouse="вешки", simulation_time="2026-07-15T00:00:00")
    second, _ = build(list(reversed(cells)), list(reversed(placements)), list(reversed(unknown)), {"receipt_dataset_id": "day-two"})
    assert first_inputs == originals
    assert first == second
    assert first["simulation_state_id"] == second["simulation_state_id"]
    json.dumps(first, ensure_ascii=False)


def test_state_id_depends_on_explicit_simulation_time_and_validator_detects_tampering():
    state, _ = build([normal()], [item(cell_key="1|1|1")])
    assert state["simulation_state_id"].startswith("sha256:") and len(state["simulation_state_id"]) == 71
    assert state["simulation_state_id"] == compute_simulation_state_id(state)
    assert summarize_simulation_state(state) == state["summary"]
    assert validate_simulation_state(state, model([normal()])) == {"valid": True, "errors": []}
    tampered = copy.deepcopy(state)
    tampered["stock_lots"][0]["qty_boxes"] = 11
    reasons = {error["reason"] for error in validate_simulation_state(tampered, model([normal()]))["errors"]}
    assert "simulation_state_id_mismatch" in reasons and "stock_conservation_metadata_mismatch" in reasons
    later = copy.deepcopy(state)
    later["simulation_time"] = "2026-07-16T00:00:00"
    later["simulation_state_id"] = compute_simulation_state_id(later)
    assert later["simulation_state_id"] != state["simulation_state_id"]


def test_validator_detects_bad_quantity_unit_and_event_duplicates():
    state, _ = build([normal()], [item(cell_key="1|1|1")])
    state["stock_lots"][0]["qty_boxes"] = -1
    state["stock_lots"][0]["unit_name"] = "шт"
    state["applied_event_ids"] = ["event", "event"]
    state["simulation_state_id"] = compute_simulation_state_id(state)
    reasons = {error["reason"] for error in validate_simulation_state(state)["errors"]}
    assert {"invalid_box_quantity", "unsupported_unit", "duplicate_applied_event_id"} <= reasons


def test_reconciliation_integration_conserves_authoritative_inventory_boxes():
    warehouse = model([normal()])
    inventory = [item("A", 100), item("B", 50)]
    actual = {"placements": [{"sku_key": inventory[0]["sku_key"], "cell_key": "1|1|1", "qty_units": 1}]}
    reconciled, reconciliation_diagnostics = reconcile_opening_stock(warehouse, inventory, actual)
    state, _ = build_initial_simulation_state(
        warehouse, reconciled, target_normalized_warehouse="вешки", simulation_time="2026-07-15T00:00:00")
    assert reconciliation_diagnostics["inventory_boxes_total"] == 150
    assert state["summary"]["total_boxes"] == 150
    assert state["stock_conservation"]["stock_conservation_ok"] is True


def test_module_remains_pure_stdlib_and_has_no_execution_or_persistence_surface():
    import warehouse_simulation_state as module
    source = inspect.getsource(module)
    for forbidden in ("pandas", "streamlit", "openpyxl", "datetime.now", "uuid", "pathlib", "open("):
        assert forbidden not in source.lower()
    for forbidden_api in ("apply_event", "apply_receipt", "apply_outbound", "save_simulation_state"):
        assert not hasattr(module, forbidden_api)
    state, _ = build()
    assert state["applied_event_ids"] == []
