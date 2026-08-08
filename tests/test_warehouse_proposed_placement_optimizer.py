from __future__ import annotations

import copy
import inspect

from warehouse_placement_rules import build_placement_rule_set
from warehouse_proposed_placement_optimizer import (
    build_proposed_placement_plan,
    validate_proposed_placement_plan,
)


def rules(weight=False, **extra):
    config = {"weight_zones": weight} | extra
    return build_placement_rule_set(config)[0]


def fixture(occupants, *, free=(), unknown=(), deep=()):
    """Build a compact but contract-shaped baseline from (position, zone, sku)."""
    records = list(occupants) + [(position, zone, None) for position, zone in free]
    records += [(position, zone, None) for position, zone in unknown]
    records += [(position, zone, sku) for position, zone, sku in deep]
    cells, positions, lots, occupancy = [], [], [], []
    for index, (position_id, zone, sku) in enumerate(records, 1):
        cell_key = "cell:" + position_id
        is_deep = any(position_id == row[0] for row in deep)
        status = "unknown" if any(position_id == row[0] for row in unknown) else ("occupied" if sku else "free")
        cells.append({"cell_key": cell_key, "row_number": index, "row_order": index,
                      "cell_number": 1, "tier": 1, "physical_index": index,
                      "weight_zone": zone, "storage_type": "deep_lane" if is_deep else "normal",
                      "capacity_pallets": 2 if is_deep else 1})
        lot_id = "lot:" + position_id
        if sku:
            lots.append({"stock_lot_id": lot_id, "sku_key": sku, "qty_boxes": 10,
                         "location_status": "located", "cell_key": cell_key,
                         "position_id": None, "pallet_unit_id": None})
        positions.append({"position_id": position_id, "cell_key": cell_key, "slot_index": 1,
                          "row_number": index, "cell_number": 1, "tier": 1, "status": status,
                          "occupied_stock_lot_ids": [lot_id] if sku and not is_deep else [],
                          "pallet_unit_id": None})
        occupancy.append({"cell_key": cell_key, "storage_type": "deep_lane" if is_deep else "normal",
                          "capacity_pallet_positions": 2 if is_deep else 1,
                          "occupancy_conflict": False})
    model = {"model_id": "model-1", "cells": cells}
    state = {"simulation_state_id": "state-1", "model_id": "model-1",
             "target_normalized_warehouse": "вешки", "physical_positions": positions,
             "cell_occupancy": occupancy, "stock_lots": lots, "pallet_units": []}
    return model, state


def zones(*rows):
    return [{"sku_key": sku, "target_zone": zone, "source": "test"} for sku, zone in rows]


def test_all_off_is_baseline_identity_and_roundtrip_is_pure():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "light", "B")])
    inputs = copy.deepcopy((model, state, rules(), zones(("A", "light"), ("B", "heavy"))))
    first, diagnostics = build_proposed_placement_plan(*inputs)
    build_proposed_placement_plan(model, state, rules(True), inputs[3])
    third, _ = build_proposed_placement_plan(*inputs)
    assert diagnostics["valid"] and first == third
    assert first["status"] == "ready" and first["summary"]["units_moved"] == 0
    assert all(row["origin_position_id"] == row["target_position_id"] for row in first["placements"])
    assert (model, state, inputs[2], inputs[3]) == inputs


def test_weight_zone_full_layout_swap_without_free_buffer():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "light", "B")])
    plan, diagnostics = build_proposed_placement_plan(model, state, rules(True), zones(("A", "light"), ("B", "heavy")))
    targets = {row["sku_key"]: row["target_position_id"] for row in plan["placements"]}
    assert diagnostics["valid"] and plan["status"] == "ready"
    assert targets == {"A": "P2", "B": "P1"}
    assert plan["summary"]["units_moved"] == 2
    assert plan["summary"]["weight_zone_compliance_before_percent"] == 0
    assert plan["summary"]["weight_zone_compliance_after_percent"] == 100


def test_correct_unit_is_kept_and_unassigned_is_not_candidate():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "light", "B")],
                           free=[("P3", "heavy"), ("P4", "unassigned")])
    plan, _ = build_proposed_placement_plan(model, state, rules(True), zones(("A", "heavy"), ("B", "heavy")))
    targets = {row["sku_key"]: row["target_position_id"] for row in plan["placements"]}
    assert targets == {"A": "P1", "B": "P3"}


def test_missing_mapping_is_fixed_partial_and_reserves_origin():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "light", "B")], free=[("P3", "heavy")])
    plan, diagnostics = build_proposed_placement_plan(model, state, rules(True), zones(("B", "heavy")))
    assert diagnostics["valid"] and plan["status"] == "partial"
    assert plan["unresolved_units"][0]["reason"] == "missing_sku_zone_assignment"
    assert next(row for row in plan["placements"] if row["sku_key"] == "A")["target_position_id"] == "P1"
    assert plan["summary"]["weight_zone_compliance_complete"] is False


def test_insufficient_capacity_blocks_whole_relocation():
    model, state = fixture([("P1", "light", "A"), ("P2", "light", "B"), ("P3", "light", "C")],
                           free=[("H1", "heavy"), ("H2", "heavy")])
    plan, diagnostics = build_proposed_placement_plan(
        model, state, rules(True), zones(("A", "heavy"), ("B", "heavy"), ("C", "heavy")))
    assert diagnostics["valid"] and plan["status"] == "blocked" and plan["placements"] == []
    assert plan["blocked_reasons"] == [{"code": "insufficient_capacity_in_target_zone", "zone": "heavy",
                                        "required_units": 3, "available_positions": 2, "shortage_positions": 1}]


def test_deep_lane_and_unknown_positions_are_never_moved_or_candidates():
    model, state = fixture([("P1", "light", "A")], unknown=[("H1", "heavy")],
                           deep=[("D1", "heavy", "D")])
    plan, _ = build_proposed_placement_plan(model, state, rules(True), zones(("A", "heavy"), ("D", "light")))
    assert plan["status"] == "blocked"
    assert all(row.get("target_position_id") != "H1" for row in plan["placements"])
    assert any(unit["reason"] == "unsupported_physical_footprint" for unit in plan["unresolved_units"])


def test_exact_pallet_is_one_unit_not_duplicated_by_linked_lot():
    model, state = fixture([], free=[("P1", "heavy")])
    state["physical_positions"][0].update(status="occupied", pallet_unit_id="pallet-1",
                                           occupied_stock_lot_ids=["lot-1"])
    state["stock_lots"] = [{"stock_lot_id": "lot-1", "sku_key": "A", "qty_boxes": 10,
                            "location_status": "located", "cell_key": "cell:P1", "position_id": "P1",
                            "pallet_unit_id": "pallet-1"}]
    state["pallet_units"] = [{"pallet_unit_id": "pallet-1", "sku_key": "A", "remaining_boxes": 10,
                              "physical_status": "active", "location_status": "located",
                              "position_id": "P1", "cell_key": "cell:P1"}]
    plan, _ = build_proposed_placement_plan(model, state, rules(), [])
    assert len(plan["placements"]) == 1
    assert plan["placements"][0]["placement_unit_id"] == "pallet-1"
    assert plan["placements"][0]["unit_type"] == "pallet"


def test_conflicting_mapping_and_unsupported_rule_are_blocking():
    model, state = fixture([("P1", "heavy", "A")])
    conflict, diagnostics = build_proposed_placement_plan(model, state, rules(True), zones(("A", "heavy"), ("A", "light")))
    assert conflict["status"] == "blocked"
    assert diagnostics["errors"][0]["code"] == "conflicting_sku_zone_assignment"
    unsupported, _ = build_proposed_placement_plan(model, state, rules(True, adjacency=True), zones(("A", "heavy")))
    assert {row["code"] for row in unsupported["blocked_reasons"]} == {"unsupported_enabled_rule"}


def test_permutations_have_same_plan_and_identity_and_api_has_no_previous_plan():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "light", "B")], free=[("P3", "heavy")])
    mapping = zones(("A", "light"), ("B", "heavy"), ("B", "heavy"))
    first, _ = build_proposed_placement_plan(model, state, rules(True), mapping)
    shuffled_model, shuffled_state = copy.deepcopy(model), copy.deepcopy(state)
    shuffled_model["cells"].reverse()
    for key in ("physical_positions", "stock_lots", "pallet_units"):
        shuffled_state[key].reverse()
    second, diagnostics = build_proposed_placement_plan(shuffled_model, shuffled_state, rules(True), list(reversed(mapping)))
    assert diagnostics["valid"] and first == second
    assert "previous" not in " ".join(inspect.signature(build_proposed_placement_plan).parameters)
    assert validate_proposed_placement_plan(first, model, state, rules(True), mapping)["valid"]
