from __future__ import annotations

import copy
import inspect

from warehouse_placement_rules import build_placement_rule_set
from warehouse_proposed_placement_optimizer import (
    build_proposed_placement_plan,
    compute_proposed_placement_plan_id,
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


def test_conflicting_mapping_is_blocking_and_adjacency_is_supported():
    model, state = fixture([("P1", "heavy", "A")])
    conflict, diagnostics = build_proposed_placement_plan(model, state, rules(True), zones(("A", "heavy"), ("A", "light")))
    assert conflict["status"] == "blocked"
    assert diagnostics["errors"][0]["code"] == "conflicting_sku_zone_assignment"
    supported, supported_diagnostics = build_proposed_placement_plan(model, state, rules(True, adjacency=True), zones(("A", "heavy")))
    assert supported_diagnostics["valid"] and supported["status"] == "ready"


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


def test_base_capacity_reserves_empty_positions_without_collapsing_or_creating_stock():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "heavy", "A")],
                           free=[("P3", "heavy"), ("P4", "heavy")])
    before = copy.deepcopy((model, state))
    configured = rules(base_sku_capacity={"enabled": True,
        "parameters": {"minimum_positions_per_sku": 3}})
    plan, diagnostics = build_proposed_placement_plan(model, state, configured, [])
    allocation = plan["sku_capacity_allocations"][0]
    assert diagnostics["valid"] and plan["status"] == "ready"
    assert allocation == {"sku_key": "A", "target_zone": "heavy", "minimum_positions": 3,
        "stock_positions_required": 2, "positions_required": 3,
        "occupied_target_position_ids": ["P1", "P2"], "reserved_position_ids": ["P3"],
        "positions_allocated": 3, "shortage_positions": 0, "status": "satisfied", "reason": None}
    assert all(not row["moved"] for row in plan["placements"])
    assert (model, state) == before


def test_capacity_shortage_is_partial_and_reservations_are_exclusive_and_known():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "heavy", "B")],
                           free=[("P3", "heavy")], unknown=[("P4", "heavy")])
    configured = rules(base_sku_capacity={"enabled": True,
        "parameters": {"minimum_positions_per_sku": 3}})
    plan, diagnostics = build_proposed_placement_plan(model, state, configured, [])
    reserved = [position for row in plan["sku_capacity_allocations"] for position in row["reserved_position_ids"]]
    assert diagnostics["valid"] and plan["status"] == "partial"
    assert reserved == ["P3"] and len(reserved) == len(set(reserved)) and "P4" not in reserved
    assert plan["summary"]["capacity_positions_required"] == 6
    assert plan["summary"]["capacity_shortage_positions"] == 3


def test_weight_capacity_uses_explicit_zone_and_deep_lane_is_never_reserved():
    model, state = fixture([("P1", "light", "A")], free=[("H1", "heavy")],
                           deep=[("D1", "heavy", None)])
    configured = rules(True, base_sku_capacity={"enabled": True,
        "parameters": {"minimum_positions_per_sku": 3}})
    plan, _ = build_proposed_placement_plan(model, state, configured, zones(("A", "heavy")))
    allocation = plan["sku_capacity_allocations"][0]
    assert allocation["target_zone"] == "heavy"
    assert allocation["reserved_position_ids"] == []
    assert allocation["shortage_positions"] == 2 and plan["status"] == "partial"


def test_capacity_permutation_identity_and_actual_placement_priority():
    model, state = fixture([("L1", "light", "A"), ("H1", "heavy", "B")],
                           free=[("H2", "heavy"), ("H3", "heavy")])
    configured = rules(True, adjacency=True, base_sku_capacity={"enabled": True,
        "parameters": {"minimum_positions_per_sku": 2}})
    mapping = zones(("A", "heavy"), ("B", "heavy"))
    first, _ = build_proposed_placement_plan(model, state, configured, mapping)
    shuffled_model, shuffled_state = copy.deepcopy(model), copy.deepcopy(state)
    shuffled_model["cells"].reverse()
    for key in ("physical_positions", "cell_occupancy", "stock_lots"):
        shuffled_state[key].reverse()
    second, _ = build_proposed_placement_plan(shuffled_model, shuffled_state, configured, list(reversed(mapping)))
    assert first == second
    actual_targets = {row["target_position_id"] for row in first["placements"]}
    reserved = {position for row in first["sku_capacity_allocations"] for position in row["reserved_position_ids"]}
    assert len(actual_targets) == 2 and not actual_targets & reserved


def test_fixed_origin_is_counted_and_cannot_be_reserved_or_validated_as_reserve():
    model, state = fixture([("P1", "heavy", "A")], free=[("P2", "heavy")])
    state["cell_occupancy"][0]["occupancy_conflict"] = True
    configured = rules(True, base_sku_capacity={"enabled": True,
        "parameters": {"minimum_positions_per_sku": 2}})
    mapping = zones(("A", "heavy"))
    plan, diagnostics = build_proposed_placement_plan(model, state, configured, mapping)
    allocation = plan["sku_capacity_allocations"][0]
    assert diagnostics["valid"] and allocation["occupied_target_position_ids"] == ["P1"]
    assert allocation["reserved_position_ids"] == ["P2"]

    invalid = copy.deepcopy(plan)
    invalid["sku_capacity_allocations"][0]["reserved_position_ids"] = ["P1"]
    invalid["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(invalid)
    validation = validate_proposed_placement_plan(invalid, model, state, configured, mapping)
    assert "reserved_capacity_overlaps_fixed_stock" in {
        error["code"] for error in validation["errors"]}


def test_deep_lane_position_counts_with_normal_position_and_is_never_reserved():
    model, state = fixture([("P1", "heavy", "A")], free=[("P2", "heavy")],
                           deep=[("D1", "heavy", "A")])
    configured = rules(base_sku_capacity={"enabled": True,
        "parameters": {"minimum_positions_per_sku": 2}})
    plan, diagnostics = build_proposed_placement_plan(model, state, configured, [])
    allocation = plan["sku_capacity_allocations"][0]
    assert diagnostics["valid"]
    assert allocation["occupied_target_position_ids"] == ["D1", "P1"]
    assert allocation["stock_positions_required"] == 2
    assert allocation["reserved_position_ids"] == []


def test_unknown_represented_origin_is_counted_but_never_released_for_reservation():
    model, state = fixture([], free=[("P2", "heavy")], unknown=[("P1", "heavy")])
    state["physical_positions"][1].update(pallet_unit_id="pallet-1")
    state["stock_lots"] = [{"stock_lot_id": "lot-1", "sku_key": "A", "qty_boxes": 10,
                            "location_status": "located", "cell_key": "cell:P1",
                            "position_id": "P1", "pallet_unit_id": "pallet-1"}]
    state["pallet_units"] = [{"pallet_unit_id": "pallet-1", "sku_key": "A", "remaining_boxes": 10,
                              "physical_status": "active", "location_status": "located",
                              "position_id": "P1", "cell_key": "cell:P1"}]
    configured = rules(base_sku_capacity={"enabled": True,
        "parameters": {"minimum_positions_per_sku": 2}})
    plan, diagnostics = build_proposed_placement_plan(model, state, configured, [])
    allocation = plan["sku_capacity_allocations"][0]
    assert diagnostics["valid"] and allocation["stock_positions_required"] == 1
    assert allocation["reserved_position_ids"] == ["P2"]


def velocity_fixture():
    model = {
        "model_id": "m", "source_file_hash": "source",
        "roads": [{"road_id": "bottom", "road_type": "bottom", "x_min": 0, "x_max": 4, "y_min": 0, "y_max": 2}],
        "aisles": [{"aisle_id": "a", "x_min": 0, "x_max": 2, "y_min": 2, "y_max": 12}],
        "cross_aisles": [], "rows": [{"row_number": "1"}],
        "cells": [
            {"cell_key": "near", "row_number": "1", "cell_number": 1, "tier": 1, "physical_index": 1,
             "x_min": 2, "x_max": 3, "y_min": 3, "y_max": 5, "side": "left", "weight_zone": "heavy",
             "storage_type": "normal", "capacity_pallets": 1},
            {"cell_key": "far", "row_number": "1", "cell_number": 2, "tier": 1, "physical_index": 2,
             "x_min": 2, "x_max": 3, "y_min": 7, "y_max": 9, "side": "left", "weight_zone": "heavy",
             "storage_type": "normal", "capacity_pallets": 1},
        ],
    }
    state = {"simulation_state_id": "s", "model_id": "m", "target_normalized_warehouse": "вешки",
             "physical_positions": [
                 {"position_id": "P_NEAR", "cell_key": "near", "status": "occupied", "slot_index": 1},
                 {"position_id": "P_FAR", "cell_key": "far", "status": "occupied", "slot_index": 1}],
             "cell_occupancy": [{"cell_key": key, "occupancy_conflict": False} for key in ("near", "far")],
             "stock_lots": [
                 {"stock_lot_id": "cold", "sku_key": "COLD", "qty_boxes": 10, "location_status": "located", "cell_key": "near"},
                 {"stock_lot_id": "hot", "sku_key": "HOT", "qty_boxes": 10, "location_status": "located", "cell_key": "far"}],
             "pallet_units": []}
    gate = {"model_id": "m", "gates": [{"gate_key": "g", "road_type": "bottom", "x": 1, "y": .5}]}
    velocity = [{"sku_key": "HOT", "velocity_rank": 1, "velocity_class": "confirmed_core"},
                {"sku_key": "COLD", "velocity_rank": 6, "velocity_class": "tail"}]
    return model, state, gate, velocity


def test_velocity_and_full_rule_set_remain_supported_with_base_capacity():
    model, state, gate, velocity = velocity_fixture()
    base = {"enabled": True, "parameters": {"minimum_positions_per_sku": 1}}
    velocity_plan, velocity_diagnostics = build_proposed_placement_plan(
        model, state, rules(velocity=True, base_sku_capacity=base), [],
        sku_velocity_rows=velocity, gate_state=gate)
    full_plan, full_diagnostics = build_proposed_placement_plan(
        model, state, rules(True, velocity=True, adjacency=True, base_sku_capacity=base),
        zones(("HOT", "heavy"), ("COLD", "heavy")), sku_velocity_rows=velocity,
        adjacency_profile={"adjacency_profile_id": "profile", "rows": [
            {"sku_key": "HOT", "adjacency_group": "group"},
            {"sku_key": "COLD", "adjacency_group": "group"}]}, gate_state=gate)
    for plan, diagnostics in ((velocity_plan, velocity_diagnostics), (full_plan, full_diagnostics)):
        assert diagnostics["valid"] and plan["status"] == "ready"
        assert plan["summary"]["capacity_skus_satisfied"] == 2
        assert next(row for row in plan["placements"] if row["sku_key"] == "HOT")["target_position_id"] == "P_NEAR"


def test_velocity_only_uses_graph_distance_and_stays_inside_zone_deterministically():
    model, state, gate, velocity = velocity_fixture()
    plan, diagnostics = build_proposed_placement_plan(
        model, state, rules(velocity=True), [], sku_velocity_rows=velocity, gate_state=gate)
    targets = {row["sku_key"]: row["target_position_id"] for row in plan["placements"]}
    assert diagnostics["valid"] and targets == {"HOT": "P_NEAR", "COLD": "P_FAR"}
    assert plan["summary"]["velocity_units_moved"] == 2
    assert plan["summary"]["rank_1_average_gate_distance_after_m"] < plan["summary"]["rank_1_average_gate_distance_before_m"]
    repeated, _ = build_proposed_placement_plan(
        model, state, rules(velocity=True), [], sku_velocity_rows=list(reversed(velocity)), gate_state=gate)
    assert repeated == plan


def test_velocity_requires_gate_and_profile_and_does_not_invent_missing_rank():
    model, state, gate, velocity = velocity_fixture()
    missing_gate, _ = build_proposed_placement_plan(
        model, state, rules(velocity=True), [], sku_velocity_rows=velocity)
    assert missing_gate["blocked_reasons"] == [{"code": "velocity_gate_required"}]
    empty, _ = build_proposed_placement_plan(model, state, rules(velocity=True), [], gate_state=gate)
    assert empty["blocked_reasons"] == [{"code": "velocity_profile_empty"}]
    partial, _ = build_proposed_placement_plan(
        model, state, rules(velocity=True), [], sku_velocity_rows=velocity[:1], gate_state=gate)
    cold = next(row for row in partial["placements"] if row["sku_key"] == "COLD")
    assert cold["velocity_rank"] is None


def test_picking_storage_assigns_exactly_one_nearest_picking_position_per_sku():
    model, state, gate, _ = velocity_fixture()
    for lot in state["stock_lots"]:
        lot["sku_key"] = "A"
    plan, diagnostics = build_proposed_placement_plan(
        model, state, rules(picking_storage=True), [], gate_state=gate)
    roles = [row["stock_role"] for row in plan["placements"]]
    picking = next(row for row in plan["placements"] if row["stock_role"] == "picking")
    assert diagnostics["valid"] and plan["status"] == "ready"
    assert roles.count("picking") == 1 and roles.count("storage") == 1
    assert picking["target_position_id"] == "P_NEAR"
    assert plan["summary"]["picking_positions"] == 1
    assert plan["summary"]["storage_positions"] == 1


def test_picking_storage_requires_mapped_gate_and_is_permutation_stable():
    model, state, gate, _ = velocity_fixture()
    blocked, diagnostics = build_proposed_placement_plan(
        model, state, rules(picking_storage=True), [])
    assert not diagnostics["valid"]
    assert blocked["blocked_reasons"] == [{"code": "picking_storage_gate_required"}]

    first, _ = build_proposed_placement_plan(
        model, state, rules(picking_storage=True), [], gate_state=gate)
    shuffled_model, shuffled_state = copy.deepcopy(model), copy.deepcopy(state)
    shuffled_model["cells"].reverse()
    for key in ("physical_positions", "stock_lots", "pallet_units"):
        shuffled_state[key].reverse()
    second, _ = build_proposed_placement_plan(
        shuffled_model, shuffled_state, rules(picking_storage=True), [], gate_state=gate)
    assert second == first


def test_adjacency_compacts_same_sku_without_explicit_profile():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "heavy", "B"), ("P3", "heavy", "A"), ("P4", "heavy", "C"), ("P5", "heavy", "B"), ("P6", "heavy", "A")])
    plan, diagnostics = build_proposed_placement_plan(model, state, rules(adjacency=True), [])
    targets = sorted(int(row["target_position_id"][1:]) for row in plan["placements"] if row["sku_key"] == "A")
    assert diagnostics["valid"] and targets[-1] - targets[0] == 2
    assert plan["summary"]["same_sku_fragments_after"] < plan["summary"]["same_sku_fragments_before"]


def test_explicit_group_blocks_are_neighbours_and_conflict_blocks_plan():
    model, state = fixture([("P1", "heavy", "A"), ("P2", "heavy", "X"), ("P3", "heavy", "B"), ("P4", "heavy", "Y")])
    profile = {"adjacency_profile_id": "sha256:test", "rows": [{"sku_key": "A", "adjacency_group": "dairy"}, {"sku_key": "B", "adjacency_group": "dairy"}], "validation_errors": []}
    plan, diagnostics = build_proposed_placement_plan(model, state, rules(adjacency=True), [], adjacency_profile=profile)
    targets = sorted(int(row["target_position_id"][1:]) for row in plan["placements"] if row["sku_key"] in {"A", "B"})
    assert diagnostics["valid"] and targets[1] == targets[0] + 1
    bad = dict(profile, validation_errors=[{"code": "conflicting_adjacency_group_assignment", "sku_key": "A"}])
    blocked, blocked_diagnostics = build_proposed_placement_plan(model, state, rules(adjacency=True), [], adjacency_profile=bad)
    assert blocked["status"] == "blocked" and not blocked_diagnostics["valid"]
