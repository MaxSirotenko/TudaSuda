from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from warehouse_day_receipt_scenario_inputs import build_day_receipt_scenario_inputs
from warehouse_event_timeline import build_warehouse_event_timeline
from warehouse_pick_demands import build_outbound_pick_demands


DAY = "2026-08-07"
WAREHOUSE = "Склад Ёж"


def receipt(line="l1", document="d1", when=f"{DAY}T08:00:00", sku="x", qty=10, warehouse=WAREHOUSE, **extra):
    value = {"receipt_line_key": line, "document_key": document, "receipt_number": document, "receipt_date": when, "warehouse": warehouse, "normalized_warehouse": warehouse, "sku_key": sku, "qty_units": qty, "unit_name": "короб"}
    value.update(extra)
    return value


def order(key="o1", when=f"{DAY}T09:00:00", qty=5, warehouse=WAREHOUSE, demands=None, **extra):
    value = {"order_key": key, "outbound_order_number": key, "created_at": when, "warehouse": warehouse, "source_indexes": [1], "demands": demands or [{"demand_key": f"dm-{key}", "sku_key": "x", "requested_units": qty, "unit_name": "короб", "source_indexes": [1]}]}
    value.update(extra)
    return value


def build(lines=None, orders=None, **kwargs):
    receipts = {"dataset_id": "sha256:receipts", "operational_date": DAY, "selected_normalized_warehouses": [WAREHOUSE], "receipt_lines": lines or []}
    return build_warehouse_event_timeline(receipts, {"orders": orders or []}, **kwargs)


def test_empty_valid_inputs_and_single_events():
    state, diag = build()
    assert state["events"] == state["time_groups"] == []
    assert state["strict_chronology_ready"] is True
    assert diag["receipt_quantity_conservation_ok"] and diag["outbound_quantity_conservation_ok"]
    assert build([receipt()])[0]["summary"]["receipt_events"] == 1
    assert build([], [order()])[0]["summary"]["outbound_events"] == 1


def test_alternating_chronology_uses_documents_not_daily_batches():
    state, _ = build(
        [receipt("a", "A", f"{DAY}T08:00:00", qty=10), receipt("b", "B", f"{DAY}T10:00:00", qty=20)],
        [order("one", f"{DAY}T09:00:00"), order("two", f"{DAY}T11:00:00")],
    )
    assert [group["events"][0]["event_type"] for group in state["time_groups"]] == ["receipt", "outbound_order", "receipt", "outbound_order"]
    assert [event["qty_units"] for event in state["events"] if event["event_type"] == "receipt"] == [10, 20]


def test_document_grouping_aggregates_sku_only_inside_document():
    state, _ = build([receipt("a", "A", sku="x", qty=10), receipt("b", "A", sku="y", qty=20), receipt("c", "A", sku="x", qty=5), receipt("d", "B", f"{DAY}T12:00:00", sku="x", qty=7)])
    events = [event for event in state["events"] if event["event_type"] == "receipt"]
    assert len(events) == 2
    assert [(batch["sku_key"], batch["qty_units"]) for batch in events[0]["receipt_batches"]] == [("x", 15), ("y", 20)]
    assert events[0]["qty_units"] == 35 and events[1]["qty_units"] == 7


def test_exact_warehouse_filters_and_normalizes_spaces_case_and_yo():
    state, diag = build([receipt(warehouse="  СКЛАД   ЕЖ "), receipt("b", "b", warehouse="Склад Ежик")], [order(warehouse="склад еж"), order("other", warehouse="Другой"), order("missing", warehouse="")])
    assert state["summary"]["total_events"] == 2
    assert diag["excluded_receipt_other_warehouse"] == 1
    assert diag["excluded_outbound_other_warehouse"] == 1
    assert diag["excluded_outbound_missing_warehouse"] == 1


def test_other_dates_are_excluded_not_fatal():
    state, diag = build([receipt(when="2026-08-08T08:00:00")], [order(when="2026-08-08T09:00:00")])
    assert state["events"] == [] and state["strict_chronology_ready"]
    assert diag["excluded_receipt_other_date"] == diag["excluded_outbound_other_date"] == 1


@pytest.mark.parametrize("kind", ["receipt", "outbound"])
def test_invalid_timestamps_are_surfaced(kind):
    state, diag = build([receipt(when="not-a-date")] if kind == "receipt" else [], [order(when="not-a-date")] if kind == "outbound" else [])
    assert not state["strict_chronology_ready"]
    assert diag["invalid_timestamp_events"] == 1 and len(state["invalid_events"]) == 1


@pytest.mark.parametrize("kind", ["receipt", "outbound"])
def test_date_only_is_not_midnight_and_blocks_strict_chronology(kind):
    state, diag = build([receipt(when=DAY)] if kind == "receipt" else [], [order(when=DAY)] if kind == "outbound" else [])
    assert state["events"][0]["occurred_at"] == DAY and state["events"][0]["time_precision"] == "date"
    assert diag["events_without_time_of_day"] == 1 and not state["strict_chronology_ready"]


@pytest.mark.parametrize("field,reason", [("receipt_date", "receipt_document_timestamp_conflict"), ("warehouse", "receipt_document_warehouse_conflict")])
def test_receipt_document_conflicts_are_fatal(field, reason):
    second = receipt("b", "d1")
    second[field] = "2026-08-07T10:00:00" if field == "receipt_date" else "Другой"
    if field == "warehouse":
        second["normalized_warehouse"] = "Другой"
    state, diag = build([receipt(), second])
    assert state["events"] == [] and reason in diag["blocking_reasons"]


def test_duplicates_are_surfaced_and_block_readiness():
    state, diag = build([receipt(), receipt(qty=99)], [order(), order(qty=99)])
    assert not state["strict_chronology_ready"]
    assert {x["reason"] for x in state["invalid_events"]} == {"duplicate_receipt_line_key", "duplicate_outbound_order_key"}
    assert "duplicate_receipt_line_key" in diag["blocking_reasons"]


def test_same_timestamp_groups_do_not_impose_business_order():
    state, _ = build([receipt(), receipt("b", "d2")], [order(when=f"{DAY}T08:00:00")])
    assert len(state["time_groups"]) == 1 and state["time_groups"][0]["event_count"] == 3
    assert state["time_groups"][0]["requires_tie_policy"]
    assert state["requires_same_timestamp_policy"] and state["strict_chronology_ready"]
    assert "simulation_sequence_index" not in json.dumps(state)


def test_same_timestamp_same_type_does_not_require_policy():
    state, _ = build([receipt(), receipt("b", "d2")])
    assert state["summary"]["simultaneous_groups"] == 1
    assert not state["requires_same_timestamp_policy"]


def test_naive_aware_and_mixed_timezone_modes():
    naive, _ = build([receipt()])
    aware, _ = build([receipt(when=f"{DAY}T10:00:00+03:00")], [order(when=f"{DAY}T08:00:00+00:00")])
    mixed, diag = build([receipt()], [order(when=f"{DAY}T09:00:00+03:00")])
    assert naive["summary"]["timezone_mode"] == "naive"
    assert aware["summary"]["timezone_mode"] == "aware" and aware["strict_chronology_ready"]
    assert mixed["summary"]["timezone_mode"] == "mixed" and not mixed["strict_chronology_ready"]
    assert "mixed_timezone_awareness" in diag["blocking_reasons"]


def test_quantity_conservation_and_deterministic_ids_under_permutation():
    lines = [receipt("a", "A", qty=60), receipt("b", "A", sku="y", qty=40)]
    demands = [{"demand_key": "z", "sku_key": "z", "requested_units": 30, "unit_name": "короб"}, {"demand_key": "a", "sku_key": "a", "requested_units": 50, "unit_name": "короб"}]
    orders = [order("two", f"{DAY}T11:00:00", demands=list(reversed(demands))), order("one", demands=demands)]
    first, _ = build(lines, orders)
    second, _ = build(list(reversed(lines)), list(reversed(orders)))
    assert first == second
    assert first["summary"]["receipt_qty_units"] == 100 and first["summary"]["outbound_requested_units"] == 160
    assert first["summary"]["receipt_quantity_conservation_ok"] and first["summary"]["outbound_quantity_conservation_ok"]
    assert first["event_timeline_state_id"].startswith("sha256:")
    assert all(event["event_id"].startswith("sha256:") for event in first["events"])
    assert all(group["time_group_id"].startswith("sha256:") for group in first["time_groups"])


def test_no_mutation_and_json_serializable():
    receipt_state = {"dataset_id": "id", "operational_date": DAY, "selected_normalized_warehouses": [WAREHOUSE], "receipt_lines": [receipt()]}
    outbound_state = {"orders": [order()]}
    before = copy.deepcopy((receipt_state, outbound_state))
    state, diag = build_warehouse_event_timeline(receipt_state, outbound_state)
    assert (receipt_state, outbound_state) == before
    json.dumps((state, diag), ensure_ascii=False)


def test_integration_with_existing_receipt_and_outbound_contracts():
    raw_receipts = []
    for line, document, when, qty in (("a", "A", f"{DAY}T08:00:00", 10), ("b", "B", f"{DAY}T10:00:00", 20)):
        raw_receipts.append({**receipt(line, document, when, qty=qty), "terminal_receipt_completed": True})
    day_state, _ = build_day_receipt_scenario_inputs({"accepted_rows": raw_receipts}, operational_date=DAY, selected_warehouses=[WAREHOUSE])
    outbound = build_outbound_pick_demands([{ "order_key": "o", "outbound_order_number": "O", "created_at": f"{DAY}T09:00:00", "warehouse": WAREHOUSE, "sku_key": "x", "nomenclature": "X", "characteristic": "", "qty_units": 5, "quantity_validation_reason": "", "unit_name": "короб", "source_index": 1}])
    state, _ = build_warehouse_event_timeline(day_state, outbound)
    assert [e["event_type"] for e in state["events"]] == ["receipt", "outbound_order", "receipt"]


def test_module_is_pure_stdlib_without_io_ui_or_pipeline_calls():
    source = Path("warehouse_event_timeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
    assert not imports & {"pandas", "streamlit", "openpyxl"}
    assert all(token not in source for token in ("open(", ".write_text(", ".write_bytes(", "warehouse_outbound_experiment_pipeline", "execute_outbound_orders("))
