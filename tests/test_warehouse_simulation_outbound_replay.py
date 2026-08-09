from __future__ import annotations

import copy

from warehouse_simulation_distance_comparison import compare_simulation_outbound_replay
from warehouse_simulation_outbound_replay import replay_outbound_on_simulation_states
from warehouse_placement_rules import build_placement_rule_set


def fixture():
    model = {
        "model_id": "m", "source_file_hash": "source",
        "roads": [{"road_id": "bottom", "road_type": "bottom", "x_min": 0, "x_max": 4, "y_min": 0, "y_max": 2}],
        "aisles": [{"aisle_id": "a", "x_min": 0, "x_max": 2, "y_min": 2, "y_max": 12}],
        "cross_aisles": [], "rows": [{"row_number": "1"}],
        "cells": [
            {"cell_key": "near", "row_number": "1", "cell_number": 1, "tier": 1, "physical_index": 1,
             "x_min": 2, "x_max": 3, "y_min": 3, "y_max": 5, "side": "left", "weight_zone": "heavy"},
            {"cell_key": "far", "row_number": "1", "cell_number": 2, "tier": 1, "physical_index": 2,
             "x_min": 2, "x_max": 3, "y_min": 7, "y_max": 9, "side": "left", "weight_zone": "heavy"},
            {"cell_key": "unmapped", "row_number": "missing", "cell_number": 3, "tier": 1, "physical_index": 3},
        ],
    }
    gate = {"model_id": "m", "gates": [{"gate_key": "g", "road_type": "bottom", "x": 1, "y": .5}]}
    demand = {"outbound_demand_state_id": "d", "orders": [{"order_key": "o1", "outbound_order_number": "RO-1",
        "created_at": "2026-08-09T10:00:00", "demands": [{"demand_key": "d1", "sku_key": "sku", "requested_units": 4}]}]}
    return model, gate, demand


def state(cell="far", qty=10, *, unknown=0, unmapped=0):
    lots = [{"stock_lot_id": "main", "sku_key": "sku", "cell_key": cell, "location_status": "located", "qty_boxes": qty}]
    if unknown:
        lots.append({"stock_lot_id": "unknown", "sku_key": "sku", "cell_key": None, "location_status": "unknown", "qty_boxes": unknown})
    if unmapped:
        lots.append({"stock_lot_id": "unmapped", "sku_key": "sku", "cell_key": "unmapped", "location_status": "located", "qty_boxes": unmapped})
    return {"simulation_state_id": f"{cell}-{qty}-{unknown}-{unmapped}", "stock_lots": lots}


def test_identical_states_are_deterministic_include_return_and_do_not_mutate_inputs():
    model, gate, demand = fixture(); current = state()
    originals = copy.deepcopy((model, gate, demand, current))
    first, diagnostics = replay_outbound_on_simulation_states(model, current, current, demand, gate)
    second, _ = replay_outbound_on_simulation_states(model, current, current, demand, gate)
    comparison, _ = compare_simulation_outbound_replay(first)
    assert first == second
    assert first["simulation_outbound_replay_id"].startswith("sha256:")
    assert first["current"]["orders"][0]["route_legs"][-1]["to_kind"] == "gate"
    assert first["current"]["orders"][0]["route_distance_m"] == 15.0
    assert comparison["summary"]["distance_saved_m"] == comparison["summary"]["distance_saved_percent"] == 0
    assert diagnostics["current"]["unknown_location_stock"] == []
    assert (model, gate, demand, current) == originals


def test_nearer_proposed_improves_and_farther_proposed_worsens():
    model, gate, demand = fixture()
    improved, _ = replay_outbound_on_simulation_states(model, state("far"), state("near"), demand, gate)
    improved_comparison, _ = compare_simulation_outbound_replay(improved)
    assert improved_comparison["summary"]["current_total_distance_m"] == 15
    assert improved_comparison["summary"]["proposed_total_distance_m"] == 7
    assert improved_comparison["summary"]["distance_saved_m"] == 8
    assert improved_comparison["orders"][0]["classification"] == "improved"
    worse, _ = replay_outbound_on_simulation_states(model, state("near"), state("far"), demand, gate)
    worse_comparison, _ = compare_simulation_outbound_replay(worse)
    assert worse_comparison["summary"]["distance_saved_m"] == -8
    assert worse_comparison["orders"][0]["classification"] == "worsened"


def test_split_stock_conserves_quantity_and_unknown_unmapped_stock_is_not_pickable():
    model, gate, demand = fixture(); demand["orders"][0]["demands"][0]["requested_units"] = 7
    split = state("near", 3, unknown=9, unmapped=8)
    split["stock_lots"].append({"stock_lot_id": "second", "sku_key": "sku", "cell_key": "far", "location_status": "located", "qty_boxes": 4})
    replay, diagnostics = replay_outbound_on_simulation_states(model, split, split, demand, gate)
    order = replay["current"]["orders"][0]
    assert order["demands"][0]["split"] is True and order["picked_boxes"] == 7
    assert replay["current"]["summary"]["conservation_valid"] is True
    assert diagnostics["current"]["unknown_location_boxes"] == 9
    assert diagnostics["current"]["unmapped_cell_boxes"] == 8


def test_different_shortage_cannot_create_false_saving_but_same_shortage_is_comparable():
    model, gate, demand = fixture()
    replay, _ = replay_outbound_on_simulation_states(model, state("far", 4), state("near", 2), demand, gate)
    comparison, _ = compare_simulation_outbound_replay(replay)
    assert comparison["coverage"]["strict_comparable_orders"] == 0
    assert comparison["summary"]["distance_saved_m"] == 0
    assert comparison["raw_summary"]["raw_distance_difference_is_business_effect"] is False
    same, _ = replay_outbound_on_simulation_states(model, state("far", 2), state("near", 2), demand, gate)
    same_comparison, _ = compare_simulation_outbound_replay(same)
    assert same_comparison["coverage"]["strict_comparable_orders"] == 1
    assert same_comparison["full_day_effect_valid"] is False


def test_explicit_replenishment_moves_whole_pallet_and_keeps_distances_separate():
    model, gate, demand = fixture()
    demand["orders"][0]["demands"][0]["requested_units"] = 15
    proposed = {
        "simulation_state_id": "role-state", "applied_event_ids": ["factual"],
        "stock_lots": [
            {"stock_lot_id": "pick-lot", "sku_key": "sku", "cell_key": "near", "position_id": "pick-pos",
             "location_status": "located", "location_role": "picking", "pallet_unit_id": "pick-pal", "qty_boxes": 10},
            {"stock_lot_id": "store-lot", "sku_key": "sku", "cell_key": "far", "position_id": "store-pos",
             "location_status": "located", "location_role": "storage", "pallet_unit_id": "store-pal", "qty_boxes": 20},
        ],
        "pallet_units": [
            {"pallet_unit_id": "pick-pal", "physical_status": "active", "location_status": "located",
             "placement_role": "picking", "position_id": "pick-pos", "cell_key": "near", "remaining_boxes": 10},
            {"pallet_unit_id": "store-pal", "physical_status": "active", "location_status": "located",
             "placement_role": "storage", "position_id": "store-pos", "cell_key": "far", "remaining_boxes": 20},
        ],
        "physical_positions": [
            {"position_id": "pick-pos", "cell_key": "near", "status": "occupied"},
            {"position_id": "store-pos", "cell_key": "far", "status": "occupied"},
        ],
    }
    rules, validation = build_placement_rule_set({"picking_storage": True, "replenishment": True})
    assert validation["valid"]
    original = copy.deepcopy(proposed)
    replay, diagnostics = replay_outbound_on_simulation_states(
        model, proposed, proposed, demand, gate, placement_rule_set=rules)
    result = replay["proposed"]
    assert [event["picked_boxes"] for event in result["orders"][0]["pick_events"]] == [10, 5]
    assert result["summary"]["initial_boxes"] == 30
    assert result["summary"]["picked_boxes"] == 15 and result["summary"]["final_boxes"] == 15
    assert result["summary"]["conservation_valid"] is True
    assert result["summary"]["replenishment_event_count"] == 1
    assert result["summary"]["total_movement_distance_m"] == (
        result["summary"]["picker_distance_m"] + result["summary"]["replenishment_distance_m"])
    assert replay["current"]["summary"]["replenishment_distance_m"] == 0
    event = result["replenishment_events"][0]
    assert event["pallet_unit_id"] == "store-pal" and event["boxes"] == 20
    working = result["working_state"]
    moved = next(p for p in working["pallet_units"] if p["pallet_unit_id"] == "store-pal")
    moved_lot = next(l for l in working["stock_lots"] if l["stock_lot_id"] == "store-lot")
    assert (moved["position_id"], moved["cell_key"], moved["placement_role"], moved["remaining_boxes"]) == ("pick-pos", "near", "picking", 15)
    assert (moved_lot["position_id"], moved_lot["cell_key"], moved_lot["location_role"], moved_lot["qty_boxes"]) == ("pick-pos", "near", "picking", 15)
    assert next(p for p in working["physical_positions"] if p["position_id"] == "store-pos")["status"] == "free"
    assert diagnostics["proposed"]["replenishment_fallbacks"] == []
    assert proposed == original


def test_unsupported_replenishment_falls_back_to_direct_pick_with_diagnostic():
    model, gate, demand = fixture(); demand["orders"][0]["demands"][0]["requested_units"] = 15
    proposed = state("far", 20)
    proposed["stock_lots"][0]["location_role"] = "storage"  # no authoritative pallet link
    proposed["stock_lots"].append({"stock_lot_id": "pick", "sku_key": "sku", "cell_key": "near",
        "position_id": "pick-pos", "location_status": "located", "location_role": "picking", "qty_boxes": 10})
    proposed["physical_positions"] = [{"position_id": "pick-pos", "cell_key": "near", "status": "occupied"}]
    rules = build_placement_rule_set({"picking_storage": True, "replenishment": True})[0]
    replay, diagnostics = replay_outbound_on_simulation_states(model, proposed, proposed, demand, gate,
                                                               placement_rule_set=rules)
    assert replay["proposed"]["orders"][0]["shortage_boxes"] == 0
    assert replay["proposed"]["summary"]["replenishment_event_count"] == 0
    assert replay["proposed"]["summary"]["replenishment_distance_m"] == 0
    assert diagnostics["proposed"]["replenishment_fallbacks"][0]["code"] == "unsupported_replenishment_direct_pick_fallback"
