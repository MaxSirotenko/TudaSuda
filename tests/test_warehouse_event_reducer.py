from __future__ import annotations

import copy
import json

import pytest

from warehouse_business_identity import canonical_sku_key
from warehouse_event_reducer import apply_warehouse_event, reduce_warehouse_timeline
from warehouse_event_timeline import build_warehouse_event_timeline
from warehouse_simulation_state import build_initial_simulation_state, validate_simulation_state


def cell(key="1|1|1", *, deep=False):
    row, number, tier = key.split("|"); capacity = 3 if deep else 1
    return {"cell_key": key, "row_number": row, "cell_number": number, "tier": tier,
            "storage_type": "deep_lane" if deep else "normal", "capacity_pallets": capacity,
            "physical_slots": [{"slot_index": i} for i in range(1, capacity + 1)] if deep else []}


def sku(name="A"):
    return canonical_sku_key({"nomenclature": name, "characteristic": "red"})


def model(cells=None):
    return {"model_id": "m", "source_file_hash": "h", "cells": cells or [cell(), cell("1|2|1"), cell("2|1|1", deep=True)]}


def opening_lot(name="A", qty=10, location="1|1|1", placement="p1"):
    return {"sku_key": sku(name), "nomenclature": name, "characteristic": "red", "qty_units": qty,
            "unit_name": "короб", "cell_key": location, "placement_id": placement, "production_dates": []}


def state(placements=None, unknown=None, cells=None, time="2026-07-15T00:00:00"):
    return build_initial_simulation_state(model(cells), {"placements": placements or [], "unknown_location_inventory": unknown or []},
        target_normalized_warehouse="вешки", simulation_time=time)[0]


def receipt(event_id="r1", at="2026-07-15T09:00:00", qty=10, name="A"):
    return {"event_id": event_id, "event_type": "receipt", "occurred_at": at, "normalized_warehouse": "вешки",
            "receipt_batches": [{"sku_key": sku(name), "nomenclature": name, "characteristic": "red",
                                 "qty_units": qty, "unit_name": "короб", "receipt_line_keys": [event_id + "-line"]}]}


def outbound(event_id="o1", at="2026-07-15T10:00:00", qty=10, name="A", demand_key=None):
    return {"event_id": event_id, "event_type": "outbound_order", "occurred_at": at, "normalized_warehouse": "вешки",
            "demands": [{"demand_key": demand_key or event_id + "-d", "sku_key": sku(name), "nomenclature": name,
                         "characteristic": "red", "requested_units": qty, "unit_name": "короб"}]}


def timeline(events, ready=True):
    groups = []
    for at in dict.fromkeys(event["occurred_at"] for event in events):
        members = [copy.deepcopy(event) for event in events if event["occurred_at"] == at]
        groups.append({"occurred_at": at, "events": members, "event_count": len(members)})
    return {"strict_chronology_ready": ready, "target_normalized_warehouse": "вешки",
            "events": copy.deepcopy(events), "time_groups": groups, "invalid_events": []}


def allocation(event, location="1|1|1"):
    return [{"sku_key": event["receipt_batches"][0]["sku_key"], "qty_boxes": event["receipt_batches"][0]["qty_units"], "cell_key": location}]


def test_invalid_initial_timeline_and_warehouse_block_before_events():
    initial = state(); bad = copy.deepcopy(initial); bad["simulation_state_id"] = "bad"
    assert reduce_warehouse_timeline(model(), bad, timeline([receipt()]))[1]["blocked_reason"] == "invalid_initial_simulation_state"
    assert reduce_warehouse_timeline(model(), initial, timeline([receipt()], False))[1]["blocked_reason"] == "invalid_timeline"
    wrong = timeline([receipt()]); wrong["target_normalized_warehouse"] = "другой"
    assert reduce_warehouse_timeline(model(), initial, wrong)[1]["blocked_reason"] == "warehouse_mismatch"


def test_event_before_time_duplicate_and_input_immutability():
    initial = state(time="2026-07-15T12:00:00"); original = copy.deepcopy(initial)
    unchanged, result = apply_warehouse_event(model(), initial, receipt(at="2026-07-15T09:00:00"))
    assert result["reasons"] == ["event_before_simulation_time"] and unchanged == initial == original
    once, _ = apply_warehouse_event(model(), state(), receipt(), receipt_allocations=allocation(receipt()))
    twice, duplicate = apply_warehouse_event(model(), once, receipt(), receipt_allocations=allocation(receipt()))
    assert twice == once and duplicate["reasons"] == ["duplicate_event_already_applied"]


def test_receipt_without_allocation_preserves_unknown_boxes_and_is_not_pickable():
    initial = state(); event = receipt(qty=10)
    received, result = apply_warehouse_event(model(), initial, event)
    assert result["status"] == "partial" and result["reasons"] == ["receipt_allocation_missing"]
    assert received["summary"]["total_boxes"] == 10 and received["stock_lots"][0]["location_status"] == "unknown"
    after, picked = apply_warehouse_event(model(), received, outbound(qty=10))
    assert picked["picked_boxes"] == 0 and picked["shortage_boxes"] == 10
    assert "stock_location_unknown" in picked["reasons"] and after["summary"]["total_boxes"] == 10


def test_explicit_receipt_allocation_conservation_and_bad_plans_are_fatal():
    initial = state(); event = receipt(qty=10); plan = allocation(event)
    original_plan = copy.deepcopy(plan)
    after, result = apply_warehouse_event(model(), initial, event, receipt_allocations=plan)
    assert plan == original_plan and result["boxes_before"] + result["receipt_boxes"] - result["picked_boxes"] == result["boxes_after"] == 10
    assert after["stock_lots"][0]["cell_key"] == "1|1|1" and after["stock_conservation"]["cumulative_receipt_boxes"] == 10
    for bad in ([{"sku_key": sku(), "qty_boxes": 9, "cell_key": "1|1|1"}],
                [{"sku_key": sku(), "qty_boxes": 10, "cell_key": "missing"}]):
        unchanged, blocked = apply_warehouse_event(model(), initial, event, receipt_allocations=bad)
        assert unchanged == initial and blocked["status"] == "blocked"


def test_single_lot_auto_pick_partial_shortage_stable_id_and_removal_release():
    initial = state([opening_lot(qty=10)]); lot_id = initial["stock_lots"][0]["stock_lot_id"]
    reduced, partial = apply_warehouse_event(model(), initial, outbound(qty=4))
    assert partial["picked_boxes"] == 4 and partial["shortage_boxes"] == 0
    assert reduced["stock_lots"][0]["stock_lot_id"] == lot_id and reduced["stock_lots"][0]["qty_boxes"] == 6
    empty, shortage = apply_warehouse_event(model(), reduced, outbound("o2", qty=9))
    assert shortage["picked_boxes"] == 6 and shortage["shortage_boxes"] == 3
    assert empty["stock_lots"] == [] and empty["cell_occupancy"][0]["occupancy_status"] == "free"


def test_ambiguous_requires_plan_and_explicit_plan_selects_only_named_lot():
    initial = state([opening_lot(qty=10, location="1|1|1", placement="p1"),
                     opening_lot(qty=10, location="1|2|1", placement="p2")])
    unchanged_boxes, result = apply_warehouse_event(model(), initial, outbound(qty=5))
    assert _total(unchanged_boxes) == 20 and result["reasons"] == ["outbound_pick_plan_required"] and result["shortage_boxes"] == 5
    target = next(lot for lot in initial["stock_lots"] if lot["cell_key"] == "1|2|1")
    plan = [{"demand_key": "o1-d", "stock_lot_id": target["stock_lot_id"], "qty_boxes": 5}]
    after, result = apply_warehouse_event(model(), initial, outbound(qty=5), outbound_pick_plan=plan)
    assert result["picked_boxes"] == 5
    assert {lot["cell_key"]: lot["qty_boxes"] for lot in after["stock_lots"]} == {"1|1|1": 10, "1|2|1": 5}


def _total(value):
    return value["summary"]["total_boxes"]


@pytest.mark.parametrize("plan", [
    [{"demand_key": "o1-d", "stock_lot_id": "missing", "qty_boxes": 1}],
    [{"demand_key": "o1-d", "stock_lot_id": "LOT", "qty_boxes": 11}],
])
def test_invalid_pick_lot_and_overpick_rejected(plan):
    initial = state([opening_lot(qty=10)]); plan = copy.deepcopy(plan)
    if plan[0]["stock_lot_id"] == "LOT": plan[0]["stock_lot_id"] = initial["stock_lots"][0]["stock_lot_id"]
    after, result = apply_warehouse_event(model(), initial, outbound(qty=10), outbound_pick_plan=plan)
    assert after == initial and result["status"] == "blocked"


def test_wrong_sku_pick_and_unknown_lot_pick_rejected():
    initial = state([opening_lot("B", 10)]); lot_id = initial["stock_lots"][0]["stock_lot_id"]
    plan = [{"demand_key": "o1-d", "stock_lot_id": lot_id, "qty_boxes": 1}]
    assert apply_warehouse_event(model(), initial, outbound(), outbound_pick_plan=plan)[1]["status"] == "blocked"
    unknown = state(unknown=[opening_lot(location=None)])
    plan[0]["stock_lot_id"] = unknown["stock_lots"][0]["stock_lot_id"]
    assert apply_warehouse_event(model(), unknown, outbound(), outbound_pick_plan=plan)[1]["status"] == "blocked"


def test_deep_lane_pick_does_not_infer_position_release():
    cells = [cell("2|1|1", deep=True)]; initial = state([opening_lot(qty=10, location="2|1|1")], cells=cells)
    after, _ = apply_warehouse_event(model(cells), initial, outbound(qty=4))
    assert after["stock_lots"][0]["qty_boxes"] == 6
    assert all(position["status"] == "unknown" and position["occupied_stock_lot_ids"] == [] for position in after["physical_positions"])


def test_outbound_before_receipt_does_not_use_future_stock_and_reverse_does():
    initial = state(); out = outbound(at="2026-07-15T09:00:00"); rec = receipt(at="2026-07-15T15:00:00")
    final, report = reduce_warehouse_timeline(model(), initial, timeline([out, rec]))
    assert report["shortage_boxes"] == 10 and report["closing_boxes"] == 10 and _total(final) == 10
    rec = receipt(at="2026-07-15T09:00:00"); out = outbound(at="2026-07-15T15:00:00")
    final, report = reduce_warehouse_timeline(model(), initial, timeline([rec, out]),
        receipt_allocations_by_event_id={rec["event_id"]: allocation(rec)})
    assert report["picked_boxes"] == 10 and report["shortage_boxes"] == 0 and report["closing_boxes"] == 0


def test_alternating_events_and_cumulative_conservation_validate_json():
    initial = state([opening_lot(qty=5)])
    events = [receipt("r1", "2026-07-15T08:00:00", 10), outbound("o1", "2026-07-15T09:00:00", 8),
              receipt("r2", "2026-07-15T10:00:00", 4), outbound("o2", "2026-07-15T11:00:00", 7)]
    allocations = {event["event_id"]: allocation(event, "1|1|1") for event in events if event["event_type"] == "receipt"}
    # Explicit picks are required once receipt and opening provenance coexist in one cell.
    first_receipt_state, _ = apply_warehouse_event(model(), initial, events[0], receipt_allocations=allocations["r1"])
    opening_id = next(l["stock_lot_id"] for l in first_receipt_state["stock_lots"] if l["source"] == "opening_stock")
    receipt_id = next(l["stock_lot_id"] for l in first_receipt_state["stock_lots"] if l["source"] == "receipt_event")
    plans = {"o1": [{"demand_key": "o1-d", "stock_lot_id": opening_id, "qty_boxes": 5},
                     {"demand_key": "o1-d", "stock_lot_id": receipt_id, "qty_boxes": 3}]}
    # r2 ID is deterministic from its own event; its receipt lot ID can be obtained from the same prefix execution.
    prefix, _ = reduce_warehouse_timeline(model(), initial, timeline(events[:3]), receipt_allocations_by_event_id=allocations,
                                          outbound_pick_plans_by_event_id=plans)
    r2_id = next(l["stock_lot_id"] for l in prefix["stock_lots"] if l.get("source_event_id") == "r2")
    r1_id = next(l["stock_lot_id"] for l in prefix["stock_lots"] if l.get("source_event_id") == "r1")
    plans["o2"] = [{"demand_key": "o2-d", "stock_lot_id": r1_id, "qty_boxes": 7}]
    final, report = reduce_warehouse_timeline(model(), initial, timeline(events), receipt_allocations_by_event_id=allocations,
                                              outbound_pick_plans_by_event_id=plans)
    assert _total(final) == 4 and final["stock_conservation"] == {"opening_boxes_input": 5, "cumulative_receipt_boxes": 14,
        "cumulative_picked_boxes": 15, "expected_stock_boxes": 4, "stock_boxes_state": 4, "stock_conservation_ok": True}
    assert validate_simulation_state(final, model())["valid"] and len(final["applied_event_ids"]) == 4
    assert all(r["boxes_before"] + r["receipt_boxes"] - r["picked_boxes"] == r["boxes_after"] for r in report["event_results"])
    json.dumps(final, ensure_ascii=False)


def test_same_timestamp_requires_policy_and_policies_change_result_deterministically():
    initial = state(); rec = receipt("z-receipt", "2026-07-15T10:00:00"); out = outbound("a-out", "2026-07-15T10:00:00")
    tied = timeline([rec, out]); plans = {rec["event_id"]: allocation(rec)}
    assert reduce_warehouse_timeline(model(), initial, tied)[1]["blocked_reason"] == "same_timestamp_policy_required"
    receipt_first = reduce_warehouse_timeline(model(), initial, tied, receipt_allocations_by_event_id=plans,
                                               same_timestamp_policy="receipts_first")[1]
    outbound_first = reduce_warehouse_timeline(model(), initial, tied, receipt_allocations_by_event_id=plans,
                                                same_timestamp_policy="outbound_first")[1]
    event_id = reduce_warehouse_timeline(model(), initial, tied, receipt_allocations_by_event_id=plans,
                                         same_timestamp_policy="event_id_order")[1]
    assert receipt_first["shortage_boxes"] == 0 and receipt_first["closing_boxes"] == 0
    assert outbound_first["shortage_boxes"] == 10 and outbound_first["closing_boxes"] == 10
    assert event_id["event_results"][0]["event_id"] == "a-out" and event_id["closing_boxes"] == 10


def test_timeline_and_plan_inputs_are_not_mutated():
    initial = state(); rec = receipt(); tl = timeline([rec]); plans = {"r1": allocation(rec)}
    originals = copy.deepcopy((initial, tl, plans))
    reduce_warehouse_timeline(model(), initial, tl, receipt_allocations_by_event_id=plans)
    assert (initial, tl, plans) == originals


@pytest.mark.parametrize("receipt_first,expected_shortage,expected_closing", [
    (False, 10, 10), (True, 0, 0),
])
def test_real_timeline_contract_proves_chronological_integration(receipt_first, expected_shortage, expected_closing):
    first, second = (("09:00:00", "15:00:00") if receipt_first else ("15:00:00", "09:00:00"))
    receipt_state = {"dataset_id": "receipts", "operational_date": "2026-07-15",
        "selected_normalized_warehouses": ["вешки"], "receipt_lines": [{
            "receipt_line_key": "line-1", "document_key": "document-1", "receipt_number": "R1",
            "receipt_date": f"2026-07-15T{first}", "normalized_warehouse": "вешки",
            "sku_key": sku(), "nomenclature": "A", "characteristic": "red",
            "qty_units": 10, "unit_name": "короб"}]}
    outbound_state = {"outbound_demand_state_id": "outbound", "orders": [{
        "order_key": "order-1", "created_at": f"2026-07-15T{second}", "warehouse": "вешки",
        "normalized_warehouse": "вешки", "demands": [{"demand_key": "demand-1", "sku_key": sku(),
            "nomenclature": "A", "characteristic": "red", "requested_units": 10, "unit_name": "короб"}]}]}
    real_timeline, diagnostics = build_warehouse_event_timeline(
        receipt_state, outbound_state, operational_date="2026-07-15", target_normalized_warehouse="вешки")
    assert diagnostics["strict_chronology_ready"] and real_timeline["strict_chronology_ready"]
    receipt_event = next(event for event in real_timeline["events"] if event["event_type"] == "receipt")
    final, report = reduce_warehouse_timeline(model(), state(), real_timeline,
        receipt_allocations_by_event_id={receipt_event["event_id"]: allocation(receipt_event)})
    assert report["shortage_boxes"] == expected_shortage
    assert report["closing_boxes"] == expected_closing == final["summary"]["total_boxes"]
