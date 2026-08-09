from __future__ import annotations

import copy

from warehouse_business_identity import canonical_sku_key
from warehouse_placement_rules import build_placement_rule_set
from warehouse_proposed_placement_optimizer import (
    build_proposed_placement_plan,
    compute_proposed_placement_plan_id,
)
from warehouse_proposed_state import apply_proposed_placement_plan
from warehouse_simulation_state import (
    build_initial_simulation_state,
    refresh_simulation_state,
    validate_simulation_state,
)


def _sku(name):
    return canonical_sku_key({"nomenclature": name, "characteristic": "test"})


def _cell(key, zone, *, deep=False):
    capacity = 2 if deep else 1
    return {
        "cell_key": key, "row_number": key, "cell_number": "1", "tier": "1",
        "row_order": int(key), "physical_index": int(key), "weight_zone": zone,
        "storage_type": "deep_lane" if deep else "normal", "capacity_pallets": capacity,
        "physical_slots": ([{"slot_index": 1}, {"slot_index": 2}] if deep else []),
    }


def _opening(name, cell, qty=10):
    return {
        "sku_key": _sku(name), "nomenclature": name, "characteristic": "test",
        "qty_units": qty, "unit_name": "короб", "cell_key": cell,
        "production_dates": [], "placement_id": "opening:" + name,
        "allocation_method": "exact_single_cell",
    }


def _baseline(*, pallet=False, unknown=False):
    cells = [_cell("1", "heavy"), _cell("2", "light"), _cell("3", "heavy")]
    model = {"model_id": "model-1", "source_file_hash": "source", "cells": cells}
    opening = {
        "placements": [_opening("A", "1", 50), _opening("B", "2", 30)],
        "unknown_location_inventory": [_opening("U", None, 7)] if unknown else [],
    }
    state, _ = build_initial_simulation_state(
        model, opening, target_normalized_warehouse="вешки", simulation_time="2026-08-08T10:00:00"
    )
    state["applied_event_ids"] = ["A", "B", "C"]
    if pallet:
        lot = next(lot for lot in state["stock_lots"] if lot["sku_key"] == _sku("A"))
        position = "position:1:1"
        lot.update(pallet_unit_id="pallet-A", position_id=position,
                   pallet_count=1, pallet_count_status="exact")
        state["pallet_units"] = [{
            "pallet_unit_id": "pallet-A", "sku_key": _sku("A"), "capacity_boxes": 50,
            "initial_boxes": 50, "remaining_boxes": 50, "is_partial": False,
            "physical_status": "active", "location_status": "located",
            "position_id": position, "cell_key": "1", "location_role": "unassigned",
        }]
    state = refresh_simulation_state(model, state)
    assert validate_simulation_state(state, model)["valid"]
    return model, state


def _rules(enabled):
    return build_placement_rule_set({"weight_zones": enabled})[0]


def _plan(model, state, enabled=True, mappings=None):
    mappings = mappings or [("A", "light"), ("B", "heavy")]
    rows = [{"sku_key": _sku(name), "target_zone": zone} for name, zone in mappings]
    plan, validation = build_proposed_placement_plan(model, state, _rules(enabled), rows)
    assert validation["valid"]
    return plan


def test_all_off_keeps_state_identity_and_inputs_are_deeply_immutable():
    model, baseline = _baseline()
    plan = _plan(model, baseline, enabled=False)
    before = copy.deepcopy((model, baseline, plan))
    proposed, report = apply_proposed_placement_plan(model, baseline, plan)
    assert report["status"] == "applied" and report["units_moved"] == 0
    assert proposed["simulation_state_id"] == baseline["simulation_state_id"]
    assert (model, baseline, plan) == before
    proposed["stock_lots"][0]["qty_boxes"] = 999
    assert baseline == before[1]


def test_opaque_opening_swap_is_atomic_stable_and_deterministic():
    model, baseline = _baseline()
    plan = _plan(model, baseline)
    before = copy.deepcopy(baseline)
    first, report = apply_proposed_placement_plan(model, baseline, plan)
    second, second_report = apply_proposed_placement_plan(model, baseline, plan)
    locations = {lot["sku_key"]: lot["cell_key"] for lot in first["stock_lots"]}
    assert locations[_sku("A")] == "2" and locations[_sku("B")] == "1"
    assert {p["position_id"]: p["status"] for p in first["physical_positions"]} == {
        "position:1:1": "occupied", "position:2:1": "occupied", "position:3:1": "free"
    }
    assert first["pallet_units"] == [] and all(lot["position_id"] is None for lot in first["stock_lots"])
    assert [lot["stock_lot_id"] for lot in first["stock_lots"]] == [lot["stock_lot_id"] for lot in baseline["stock_lots"]]
    assert report["units_moved"] == report["opaque_opening_units_moved"] == 2
    assert report["boxes_before"] == report["boxes_after"] == 80
    assert first == second and report == second_report and baseline == before
    assert first["simulation_state_id"] != baseline["simulation_state_id"]
    assert validate_simulation_state(first, model)["valid"]


def test_pallet_move_preserves_unit_lot_boxes_history_and_time():
    model, baseline = _baseline(pallet=True)
    plan = _plan(model, baseline, mappings=[("A", "light"), ("B", "heavy")])
    proposed, report = apply_proposed_placement_plan(model, baseline, plan)
    pallet = proposed["pallet_units"][0]
    lot = next(lot for lot in proposed["stock_lots"] if lot["pallet_unit_id"] == "pallet-A")
    assert pallet["pallet_unit_id"] == "pallet-A" and pallet["position_id"] == "position:2:1"
    assert lot["stock_lot_id"] == next(l["stock_lot_id"] for l in baseline["stock_lots"] if l["pallet_unit_id"])
    assert lot["qty_boxes"] == pallet["remaining_boxes"] == 50
    assert proposed["applied_event_ids"] == ["A", "B", "C"]
    assert proposed["simulation_time"] == baseline["simulation_time"]
    assert proposed["positions_released_total"] == baseline["positions_released_total"]
    assert report["pallet_units_moved"] == 1 and report["box_conservation_ok"]


def test_partial_moves_mapped_unit_and_keeps_missing_assignment_unit():
    model, baseline = _baseline()
    plan = _plan(model, baseline, mappings=[("B", "heavy")])
    assert plan["status"] == "partial"
    proposed, report = apply_proposed_placement_plan(model, baseline, plan)
    assert report["status"] == "applied_partial"
    assert next(l for l in proposed["stock_lots"] if l["sku_key"] == _sku("B"))["cell_key"] == "3"
    assert next(l for l in proposed["stock_lots"] if l["sku_key"] == _sku("A"))["cell_key"] == "1"
    assert report["boxes_before"] == report["boxes_after"]


def test_blocked_identity_tampering_unknown_duplicate_and_deep_lane_fail_closed():
    model, baseline = _baseline()
    plan = _plan(model, baseline)
    cases = []
    blocked = copy.deepcopy(plan)
    blocked["status"] = "blocked"
    blocked["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(blocked)
    cases.append((model, baseline, blocked, "proposed_placement_plan_not_applicable"))
    wrong_baseline = copy.deepcopy(plan)
    wrong_baseline["baseline_state_id"] = "wrong"
    wrong_baseline["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(wrong_baseline)
    cases.append((model, baseline, wrong_baseline, "baseline_state_id_mismatch"))
    wrong_model = copy.deepcopy(model)
    wrong_model["model_id"] = "wrong"
    cases.append((wrong_model, baseline, plan, "model_id_mismatch"))
    tampered = copy.deepcopy(plan)
    tampered["placements"][0]["target_position_id"] = "position:3:1"
    cases.append((model, baseline, tampered, "proposed_placement_plan_id_mismatch"))
    unknown_baseline = copy.deepcopy(baseline)
    unknown_baseline["physical_positions"][2]["status"] = "unknown"
    # Keep this deliberately authoritative baseline valid by recomputing its identity.
    from warehouse_simulation_state import compute_simulation_state_id
    unknown_baseline["simulation_state_id"] = compute_simulation_state_id(unknown_baseline)
    unknown_plan = copy.deepcopy(plan)
    unknown_plan["baseline_state_id"] = unknown_baseline["simulation_state_id"]
    unknown_plan["placements"][0].update(target_position_id="position:3:1", target_cell_key="3", target_zone="heavy", moved=True)
    unknown_plan["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(unknown_plan)
    cases.append((model, unknown_baseline, unknown_plan, "unknown_target_position"))
    for use_model, use_baseline, use_plan, code in cases:
        snapshot = copy.deepcopy((use_model, use_baseline, use_plan))
        proposed, report = apply_proposed_placement_plan(use_model, use_baseline, use_plan)
        assert proposed is None and report["status"] == "blocked"
        assert report["blocked_reasons"][0]["code"] == code
        assert (use_model, use_baseline, use_plan) == snapshot


def test_duplicate_target_and_manual_deep_lane_move_are_blocked_even_with_valid_id():
    model, baseline = _baseline()
    duplicate = _plan(model, baseline)
    duplicate["placements"][1]["target_position_id"] = duplicate["placements"][0]["target_position_id"]
    duplicate["placements"][1]["target_cell_key"] = duplicate["placements"][0]["target_cell_key"]
    duplicate["placements"][1]["target_zone"] = duplicate["placements"][0]["target_zone"]
    duplicate["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(duplicate)
    assert apply_proposed_placement_plan(model, baseline, duplicate)[0] is None

    deep_model = {"model_id": "model-1", "source_file_hash": "source",
                  "cells": [_cell("1", "heavy"), _cell("2", "light", deep=True), _cell("3", "light")]}
    deep_baseline, _ = build_initial_simulation_state(
        deep_model, {"placements": [_opening("A", "1"), _opening("B", "3")],
                     "unknown_location_inventory": []},
        target_normalized_warehouse="вешки", simulation_time="2026-08-08T10:00:00")
    deep = _plan(deep_model, deep_baseline, enabled=False)
    row = next(row for row in deep["placements"] if row["sku_key"] == _sku("A"))
    row.update(target_position_id="position:2:1", target_cell_key="2", target_zone="light", moved=True)
    deep["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(deep)
    proposed, report = apply_proposed_placement_plan(deep_model, deep_baseline, deep)
    assert proposed is None and report["blocked_reasons"][0]["code"] == "deep_lane_unit_moved"


def test_unknown_location_stock_is_untouched():
    model, baseline = _baseline(unknown=True)
    # Unknown stock makes capacity non-authoritative, so an all-off plan is the safe applicable layout.
    plan = _plan(model, baseline, enabled=False)
    proposed, report = apply_proposed_placement_plan(model, baseline, plan)
    assert report["status"] == "applied_partial"
    unknown_before = next(lot for lot in baseline["stock_lots"] if lot["location_status"] == "unknown")
    unknown_after = next(lot for lot in proposed["stock_lots"] if lot["location_status"] == "unknown")
    assert unknown_after == unknown_before
