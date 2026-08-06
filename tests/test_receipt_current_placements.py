import copy
import json

import pytest

from warehouse_day_receipt_scenario_inputs import BOX_UNIT
from warehouse_receipt_current_placements import LIMITATIONS, build_current_receipt_placements
from warehouse_receipt_snapshot_transitions import analyze_receipt_snapshot_transitions
from warehouse_receipt_virtual_slots import build_receipt_virtual_slots


def model(*keys):
    return {"cells": [{"cell_key": key, "row_number": i + 1, "cell_number": i + 2, "tier": 1, "weight_zone": "heavy", "physical_index": i, "capacity_pallets": 9} for i, key in enumerate(keys)]}


def day(*rows):
    batches = [{"receipt_batch_key": key, "dataset_id": "D", "normalized_warehouse": "w", "warehouse": "W", "sku_key": sku, "qty_units": qty, "unit_name": BOX_UNIT} for key, sku, qty in rows]
    scenario = {"receipt_dataset_id": "D", "receipt_batch_keys": [x[0] for x in rows], "total_boxes": sum(x[2] for x in rows)}
    return {"dataset_id": "D", "operational_date": "2026-08-05", "receipt_sku_batches": batches, "scenario_inputs": {"current": copy.deepcopy(scenario), "proposed": copy.deepcopy(scenario)}}


def candidate(key="CAND", batch="B", sku="SKU", cell="C1", kind="newly_occupied_physical_cell", confidence="strong_snapshot_delta", qty=10, **extra):
    value = {"candidate_key": key, "receipt_batch_key": batch, "dataset_id": "D", "normalized_warehouse": "w", "warehouse": "W", "sku_key": sku, "receipt_qty_units": qty, "physical_cell_key": cell, "transition_key": "T-" + key, "candidate_type": kind, "confidence": confidence, "requires_virtual_slot": kind == "reused_physical_cell"}
    value.update(extra)
    return value


def analysis(candidates=(), evidence=(), **extra):
    value = {"analysis_id": "A", "dataset_id": "D", "operational_date": "2026-08-05", "start_snapshot_id": "S1", "end_snapshot_id": "S2", "receipt_cell_candidates": list(candidates), "receipt_batch_evidence": list(evidence)}
    value.update(extra)
    return value


def slot(slot_id="VS", key="C1-V01", batch="B", sku="SKU", parent="C1", **extra):
    value = {"virtual_slot_id": slot_id, "virtual_slot_key": key, "cell_key": key, "is_virtual": True, "parent_physical_cell_key": parent, "receipt_batch_key": batch, "dataset_id": "D", "normalized_warehouse": "w", "warehouse": "W", "sku_key": sku, "changes_physical_capacity": False, "inherits_route_from_parent": True, "quantity_allocation_pending": True, "reason": "physical_cell_reused_between_snapshots"}
    value.update(extra)
    return value


def virtual(slots=(), **extra):
    value = {"virtual_slot_state_id": "VSTATE", "analysis_id": "A", "dataset_id": "D", "start_snapshot_id": "S1", "end_snapshot_id": "S2", "virtual_slots": list(slots), "requirement_slot_links": []}
    value.update(extra)
    return value


def build(d=None, a=None, v=None, m=None):
    return build_current_receipt_placements(m or model("C1", "C2"), d or day(("B", "SKU", 10)), a or analysis(), v or virtual())


def test_empty_valid_batches_create_empty_state():
    state, diagnostics = build(d=day())
    assert state["placements"] == state["unresolved_receipt_batches"] == []
    assert state["limitations"] == LIMITATIONS
    assert state["summary"]["quantity_conservation_ok"] is True
    assert diagnostics["receipt_boxes_total"] == 0


def test_strong_newly_occupied_creates_physical_placement_from_model_metadata():
    state, _ = build(a=analysis([candidate()]))
    placement = state["placements"][0]
    assert (placement["cell_key"], placement["physical_cell_key"], placement["is_virtual"], placement["virtual_slot_id"]) == ("C1", "C1", False, None)
    assert (placement["row_number"], placement["cell_number"], placement["qty_units"]) == (1, 2, 10)


def test_reused_candidate_uses_existing_virtual_slot_not_physical_cell():
    state, _ = build(a=analysis([candidate(kind="reused_physical_cell")]), v=virtual([slot()]))
    placement = state["placements"][0]
    assert (placement["cell_key"], placement["physical_cell_key"], placement["route_physical_cell_key"]) == ("C1-V01", "C1", "C1")
    assert placement["is_virtual"] is True and placement["parent_physical_cell_key"] == "C1"


def test_candidate_quantity_is_audit_only_but_mismatch_is_rejected():
    good, _ = build(a=analysis([candidate(qty=10)]))
    assert good["placements"][0]["qty_units"] == 10
    bad, diagnostics = build(a=analysis([candidate(qty=999)]))
    assert bad["placements"] == [] and bad["unresolved_receipt_batches"][0]["reason_code"] == "candidate_quantity_mismatch"
    assert diagnostics["candidate_quantity_mismatches"] == 1


@pytest.mark.parametrize("candidates,reason", [
    ([candidate(confidence="ambiguous_snapshot_delta")], "ambiguous_snapshot_delta"),
    ([candidate("X", cell="C1"), candidate("Y", cell="C2")], "multiple_receipt_cell_candidates"),
    ([candidate("X"), candidate("Y", cell="C2", confidence="ambiguous_snapshot_delta")], "multiple_receipt_cell_candidates"),
])
def test_ambiguous_or_multiple_candidates_are_unresolved(candidates, reason):
    state, _ = build(a=analysis(candidates))
    assert state["placements"] == []
    assert state["unresolved_receipt_batches"][0]["reason_code"] == reason


@pytest.mark.parametrize("status,reason", [("persistent_same_sku_only", "same_sku_replenishment_not_observable"), ("no_end_snapshot_evidence", "no_end_snapshot_evidence")])
def test_no_candidate_uses_evidence_reason(status, reason):
    state, _ = build(a=analysis(evidence=[{"receipt_batch_key": "B", "evidence_status": status}]))
    assert state["unresolved_receipt_batches"][0]["reason_code"] == reason


def test_reused_missing_or_multiple_slots_is_unresolved():
    a = analysis([candidate(kind="reused_physical_cell")])
    missing, _ = build(a=a)
    multiple, _ = build(a=a, v=virtual([slot("V1", "C1-V01"), slot("V2", "C1-V02")]))
    assert missing["unresolved_receipt_batches"][0]["reason_code"] == "matching_virtual_slot_missing"
    assert multiple["unresolved_receipt_batches"][0]["reason_code"] == "multiple_matching_virtual_slots"


@pytest.mark.parametrize("a_override,v_override", [({"dataset_id": "OTHER"}, {}), ({}, {"analysis_id": "OTHER"}), ({"start_snapshot_id": "OTHER"}, {}), ({}, {"end_snapshot_id": "OTHER"})])
def test_cross_state_mismatch_blocks_all_placements(a_override, v_override):
    a = analysis([candidate()], **a_override); v = virtual(**v_override)
    state, diagnostics = build(a=a, v=v)
    assert state["placements"] == [] and diagnostics["configuration_errors"]


def test_invalid_unknown_cell_and_duplicate_contracts_are_deterministic():
    d = day(("B", "SKU", 10))
    unknown, diagnostics = build(d=d, a=analysis([candidate(cell="BAD")]))
    assert unknown["placements"] == [] and diagnostics["unknown_physical_cells"] == 1
    duplicate_candidates = [candidate("SAME"), candidate("SAME", cell="C2")]
    first = build(d=d, a=analysis(duplicate_candidates))
    second = build(d=d, a=analysis(list(reversed(duplicate_candidates))))
    assert first == second and first[1]["duplicate_candidate_keys"] == 1
    logical = [candidate("X"), candidate("Y")]
    assert build(d=d, a=analysis(logical))[1]["duplicate_candidate_combinations"] == 1


def test_duplicate_batches_and_slots_are_rejected_regardless_of_order():
    d = day(("B", "SKU", 10), ("B", "SKU", 10))
    one = build(d=d); two = build(d={**d, "receipt_sku_batches": list(reversed(d["receipt_sku_batches"]))})
    assert one == two and one[0]["invalid_receipt_batches"]
    slots = [slot("SAME", "K1"), slot("SAME", "K2"), slot("S3", "K1")]
    result, diagnostics = build(v=virtual(slots))
    assert result["invalid_virtual_slots"] and diagnostics["duplicate_virtual_slot_ids"] == diagnostics["duplicate_virtual_slot_keys"] == 1


def test_permutation_repeat_immutability_json_and_conservation():
    d = day(("B1", "S1", 7), ("B2", "S2", 11)); a = analysis([candidate("C2", "B2", "S2", "C2", qty=11), candidate("C1", "B1", "S1", qty=7)])
    v = virtual(); m = model("C1", "C2"); before = copy.deepcopy((m, d, a, v))
    first = build_current_receipt_placements(m, d, a, v)
    second = build_current_receipt_placements(m, {**d, "receipt_sku_batches": list(reversed(d["receipt_sku_batches"]))}, {**a, "receipt_cell_candidates": list(reversed(a["receipt_cell_candidates"]))}, v)
    assert first == second == build_current_receipt_placements(m, d, a, v)
    assert (m, d, a, v) == before
    json.dumps(first, ensure_ascii=False)
    assert first[0]["summary"]["quantity_conservation_ok"] is True


def test_integration_c1_c4_expected_current_placements():
    m = model("C1", "C2", "C3", "C4")
    d = day(("B-B", "SKU B", 100), ("B-C", "SKU C", 50), ("B-D", "SKU D", 20))
    def p(key, cell, sku): return {"placement_id": key, "warehouse": "W", "cell_key": cell, "sku_key": sku}
    start = {"placements": [p("s1", "C1", "SKU A"), p("s3", "C3", "SKU C"), p("s4", "C4", "SKU X")]}
    end = {"placements": [p("e1", "C1", "SKU B"), p("e2", "C2", "SKU D"), p("e3", "C3", "SKU C")]}
    a, _ = analyze_receipt_snapshot_transitions(m, d, start, end); v, _ = build_receipt_virtual_slots(m, a)
    state, _ = build_current_receipt_placements(m, d, a, v)
    assert {(x["sku_key"], x["cell_key"], x["qty_units"]) for x in state["placements"]} == {("SKU B", "C1-V01", 100), ("SKU D", "C2", 20)}
    assert [(x["sku_key"], x["qty_units"], x["reason_code"]) for x in state["unresolved_receipt_batches"]] == [("SKU C", 50, "same_sku_replenishment_not_observable")]
    assert state["summary"] == {"valid_receipt_batches": 3, "placed_receipt_batches": 2, "unresolved_receipt_batches": 1, "valid_receipt_qty_units": 170, "placed_qty_units": 120, "unresolved_qty_units": 50, "physical_placements": 1, "virtual_placements": 1, "quantity_conservation_ok": True}
    forbidden = ("proposed_placements", "route", "distance", "warehouse_graph", "working_stock", "allocated_qty_units", "allocation_weight", "percentage")
    def keys(value):
        if isinstance(value, dict): return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list): return set().union(*(keys(item) for item in value), set())
        return set()
    assert not set(forbidden) & keys(state)
