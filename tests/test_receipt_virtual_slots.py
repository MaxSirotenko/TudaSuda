import copy
import json

from warehouse_receipt_snapshot_transitions import analyze_receipt_snapshot_transitions
from warehouse_receipt_virtual_slots import LIMITATIONS, build_receipt_virtual_slots


def model(*keys):
    return {"cells": [{"cell_key": k, "row_number": i + 10, "cell_number": i + 20, "tier": i + 1, "weight_zone": "heavy", "physical_index": i, "x": i, "y": i + 1, "capacity_pallets": 9, "deep_lane_width": 2, "occupied": True, "available_capacity": 0, "pallet_slots": 9} for i, k in enumerate(keys)]}


def state(reqs=None, **overrides):
    s = {"analysis_id": "A", "dataset_id": "D", "operational_date": "2026-08-05", "start_snapshot_id": "S1", "end_snapshot_id": "S2", "virtual_slot_requirements": list(reqs or [])}
    s.update(overrides)
    return s


def req(key="R1", batch="B1", wh="W", sku="SKU B", cell="C1", **extra):
    r = {"requirement_key": key, "dataset_id": "D", "start_snapshot_id": "S1", "end_snapshot_id": "S2", "receipt_batch_key": batch, "normalized_warehouse": wh, "warehouse": wh, "sku_key": sku, "physical_cell_key": cell, "row_number": 999, "cell_number": 999, "tier": 999, "start_sku_keys": ["SKU A"], "end_sku_keys": [sku], "reason": "physical_cell_reused_between_snapshots", "virtual_slot_count_hint": 1, "quantity_allocation_pending": True}
    r.update(extra)
    return r


def keys(vs):
    return [s["virtual_slot_key"] for s in vs["virtual_slots"]]


def assert_forbidden_absent(obj):
    for name in ("allocated_qty_units", "placement_qty_units", "virtual_slot_qty_units", "occupied_qty_units", "remaining_qty_units", "allocation_weight", "percentage", "placements", "current_placements", "proposed_placements", "route", "distance"):
        assert name not in obj
    text = json.dumps(obj.get("virtual_slots", []) + obj.get("requirement_slot_links", []), ensure_ascii=False)
    for name in ("allocated_qty_units", "placement_qty_units", "virtual_slot_qty_units", "occupied_qty_units", "remaining_qty_units", "allocation_weight", "percentage", "placements", "current_placements", "proposed_placements", "distance"):
        assert name not in text


def test_empty_valid_transition_analysis_creates_empty_state():
    vs, d = build_receipt_virtual_slots(model("C1"), state())
    assert vs["virtual_slots"] == []
    assert vs["requirement_slot_links"] == []
    assert vs["limitations"] == LIMITATIONS
    assert d["virtual_slots_created"] == 0


def test_one_requirement_creates_c1_v01_slot_without_quantity_or_placements():
    vs, d = build_receipt_virtual_slots(model("C1"), state([req()]))
    slot = vs["virtual_slots"][0]
    assert keys(vs) == ["C1-V01"]
    assert slot["cell_key"] == "C1-V01"
    assert slot["parent_physical_cell_key"] == "C1"
    assert slot["sku_key"] == "SKU B"
    assert slot["is_virtual"] is True
    assert slot["inherits_route_from_parent"] is True
    assert slot["changes_physical_capacity"] is False
    assert d["virtual_slots_created"] == 1
    assert_forbidden_absent(vs)


def test_two_requirements_same_physical_cell_are_numbered():
    vs, _ = build_receipt_virtual_slots(model("C1"), state([req("R2", "B2", sku="SKU C"), req("R1", "B1")]))
    assert keys(vs) == ["C1-V01", "C1-V02"]


def test_requirement_permutation_does_not_change_output_ids_keys_or_order():
    reqs = [req("R2", "B2", sku="SKU C"), req("R1", "B1")]
    a, _ = build_receipt_virtual_slots(model("C1"), state(reqs))
    b, _ = build_receipt_virtual_slots(model("C1"), state(list(reversed(reqs))))
    assert a == b
    assert [s["virtual_slot_id"] for s in a["virtual_slots"]] == [s["virtual_slot_id"] for s in b["virtual_slots"]]


def test_different_physical_cells_start_numbering_separately():
    vs, _ = build_receipt_virtual_slots(model("C1", "C2"), state([req("R1", "B1", cell="C1"), req("R2", "B2", cell="C2", sku="SKU C")]))
    assert keys(vs) == ["C1-V01", "C2-V01"]


def test_same_physical_cell_key_on_different_warehouses_has_distinct_identity():
    vs, d = build_receipt_virtual_slots(model("C1"), state([req("R1", "B1", wh="W1"), req("R2", "B2", wh="W2", sku="SKU C")]))
    assert keys(vs) == ["C1-V01", "C1-V02"]
    assert len({s["virtual_slot_id"] for s in vs["virtual_slots"]}) == 2
    assert d["physical_cells_with_virtual_slots"] == 2


def test_existing_model_cell_key_collision_skips_to_next_free_key():
    vs, d = build_receipt_virtual_slots(model("C1", "C1-V01"), state([req()]))
    assert keys(vs) == ["C1-V02"]
    assert d["generated_key_collisions_avoided"] == 1


def test_invalid_unknown_parent_reason_pending_start_and_end_sku_rules():
    cases = [
        req("R1", cell="BAD"),
        req("R2", reason="newly_occupied_physical_cell"),
        req("R3", quantity_allocation_pending=False),
        req("R4", start_sku_keys=[]),
        req("R5", sku="SKU Z", end_sku_keys=["OTHER"]),
    ]
    vs, d = build_receipt_virtual_slots(model("C1"), state(cases))
    assert vs["virtual_slots"] == []
    assert d["invalid_virtual_slot_requirements"] == 5
    reason_codes = {r["reason_code"] for r in vs["invalid_virtual_slot_requirements"]}
    assert {"unknown_parent_physical_cell", "invalid_reason", "quantity_allocation_pending_not_true", "start_sku_keys_empty", "sku_key_not_in_end_sku_keys"} <= reason_codes


def test_duplicate_requirement_key_and_combination_are_all_rejected_deterministically():
    dup_key = [req("R1", "B1"), req("R1", "B2", sku="SKU C")]
    a, da = build_receipt_virtual_slots(model("C1"), state(dup_key))
    b, db = build_receipt_virtual_slots(model("C1"), state(list(reversed(dup_key))))
    assert a == b and da == db
    assert da["duplicate_requirement_keys"] == 1
    assert a["virtual_slots"] == []
    dup_combo = [req("R1", "B1"), req("R2", "B1", sku="SKU C")]
    vs, d = build_receipt_virtual_slots(model("C1"), state(dup_combo))
    assert vs["virtual_slots"] == []
    assert d["duplicate_requirement_combinations"] == 1


def test_dataset_and_snapshot_mismatches_are_invalid_and_diagnosed():
    vs, d = build_receipt_virtual_slots(model("C1"), state([req(dataset_id="OTHER"), req("R2", "B2", start_snapshot_id="OTHER", sku="SKU C"), req("R3", "B3", end_snapshot_id="OTHER", sku="SKU D")]))
    assert vs["virtual_slots"] == []
    assert d["invalid_virtual_slot_requirements"] == 3
    assert {r["reason_code"] for r in vs["invalid_virtual_slot_requirements"]} == {"dataset_id_mismatch", "end_snapshot_id_mismatch", "start_snapshot_id_mismatch"}


def test_parent_metadata_and_coordinates_are_from_model_capacity_not_copied():
    vs, _ = build_receipt_virtual_slots(model("C1"), state([req(row_number=999, cell_number=999, tier=999)]))
    slot = vs["virtual_slots"][0]
    assert (slot["row_number"], slot["cell_number"], slot["tier"], slot["weight_zone"], slot["physical_index"]) == (10, 20, 1, "heavy", 0)
    assert slot["parent_location"] == {"x": 0, "y": 1}
    for field in ("capacity_pallets", "deep_lane_width", "occupied", "available_capacity", "pallet_slots"):
        assert field not in slot


def test_inputs_unchanged_json_serializable_and_repeated_calls_identical():
    m, s = model("C1"), state([req()])
    before = copy.deepcopy((m, s))
    a = build_receipt_virtual_slots(m, s)
    b = build_receipt_virtual_slots(m, s)
    assert (m, s) == before
    json.dumps(a, ensure_ascii=False)
    assert a == b


def receipt(*items):
    keys = [i[0] for i in items]
    total = sum(i[3] for i in items)
    return {"dataset_id": "D", "operational_date": "2026-08-05", "receipt_sku_batches": [{"receipt_batch_key": k, "operational_date": "2026-08-05", "warehouse": wh, "normalized_warehouse": wh, "sku_key": sku, "qty_units": qty, "unit_name": "короб"} for k, wh, sku, qty in items], "scenario_inputs": {"current": {"receipt_dataset_id": "D", "total_boxes": total, "receipt_batch_keys": keys}, "proposed": {"receipt_dataset_id": "D", "total_boxes": total, "receipt_batch_keys": keys}}}


def p(pid, wh, cell, sku):
    return {"placement_id": pid, "warehouse": wh, "cell_key": cell, "sku_key": sku}


def test_integration_c1_c4_scenario_creates_only_c1_virtual_slot():
    m = model("C1", "C2", "C3", "C4")
    day = receipt(("B-B", "W", "SKU B", 100), ("B-C", "W", "SKU C", 50), ("B-D", "W", "SKU D", 20))
    start = {"placements": [p("s1", "W", "C1", "SKU A"), p("s3", "W", "C3", "SKU C"), p("s4", "W", "C4", "SKU X")]}
    end = {"placements": [p("e1", "W", "C1", "SKU B"), p("e2", "W", "C2", "SKU D"), p("e3", "W", "C3", "SKU C")]}
    analysis, _ = analyze_receipt_snapshot_transitions(m, day, start, end)
    vs, _ = build_receipt_virtual_slots(m, analysis)
    assert keys(vs) == ["C1-V01"]
    slot = vs["virtual_slots"][0]
    assert slot["receipt_batch_key"] == "B-B"
    assert slot["sku_key"] == "SKU B"
    assert {"C2-V01", "C3-V01", "C4-V01"}.isdisjoint(keys(vs))
    assert_forbidden_absent(vs)
