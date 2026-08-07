from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest

import warehouse_outbound_experiment_inputs as inputs_module
from warehouse_outbound_experiment_pipeline import run_outbound_distance_experiment


def fixture_inputs(*, slotting=True):
    model = {"model_id": "M", "source_file_hash": "H", "cells": [
        {"cell_key": "C1", "row_number": 1, "cell_number": 1, "tier": 1,
         "row_order": 1, "physical_index": 1, "weight_zone": "light"},
        {"cell_key": "C2", "row_number": 2, "cell_number": 1, "tier": 1,
         "row_order": 2, "physical_index": 2, "weight_zone": "heavy"},
    ]}
    scenario = {"receipt_dataset_id": "D", "receipt_batch_keys": ["B"], "total_boxes": 2}
    day = {"dataset_id": "D", "operational_date": "2026-08-07",
           "selected_normalized_warehouses": ["  СКЛАД   Ё  "],
           "receipt_sku_batches": [{"receipt_batch_key": "B", "dataset_id": "D",
               "operational_date": "2026-08-07", "warehouse": "Склад Е",
               "normalized_warehouse": "склад е", "sku_key": "R", "qty_units": 2,
               "unit_name": "короб"}],
           "scenario_inputs": {"current": copy.deepcopy(scenario), "proposed": copy.deepcopy(scenario)}}
    start = {"model_id": "M", "source_file_hash": "H", "settings": {}, "journal": [],
             "placements": [
                 {"placement_id": "A", "warehouse": " СКЛАД Е ", "sku_key": "X", "cell_key": "C1", "qty_units": 1},
                 {"placement_id": "B", "warehouse": "other", "sku_key": "X", "cell_key": "C2", "qty_units": 99},
                 {"placement_id": "missing", "sku_key": "X", "cell_key": "C2", "qty_units": 99}],
             "excluded_inventory": [], "unmatched_inventory": [], "unplaced_inventory": []}
    end = {"placements": [
                {"placement_id": "E", "warehouse": "склад е", "sku_key": "R", "cell_key": "C2", "qty_units": 2,
                 "weight_zone": "heavy"},
                {"placement_id": "F", "warehouse": "other", "sku_key": "R", "cell_key": "C1", "qty_units": 2}],
           "excluded_inventory": [], "unmatched_inventory": [], "unplaced_inventory": []}
    inventory = [
        {"warehouse": "Склад Ё", "sku_key": "X", "nomenclature": "X", "characteristic": "",
         "qty_units": 10, "unit_name": "короб"},
        {"warehouse": "other", "sku_key": "X", "nomenclature": "X", "characteristic": "",
         "qty_units": 50, "unit_name": "короб"},
        {"sku_key": "X", "qty_units": 70, "unit_name": "короб"},
    ]
    outbound = [{"outbound_order_number": "O", "created_at": "2026-08-07", "warehouse": "Склад Е",
                 "sku_key": "X", "qty_units": 1, "unit_name": "короб"}]
    rules = [{"sku_key": "R", "weight_zone": "light", "priority_rank": None}] if slotting else []
    gate = {"gate_key": "main_gate", "gate_name": "Главные ворота", "road_type": "bottom", "x": 5, "y": 1}
    return model, day, start, end, inventory, outbound, rules, gate


def build(*, slotting=True, transform=None, **kwargs):
    values = list(fixture_inputs(slotting=slotting))
    if transform: transform(values)
    return inputs_module.build_outbound_experiment_inputs(*values, **kwargs)


def test_exact_scoping_opening_stock_and_no_end_location_leak():
    state, diagnostics = build(slotting=False)
    pipeline = state["pipeline_inputs"]
    assert inputs_module.normalize_warehouse("  СКЛАД   Ё ") == "склад е"
    assert [x["cell_key"] for x in pipeline["start_placement_state"]["placements"]] == ["C1"]
    assert [x["cell_key"] for x in pipeline["end_placement_state"]["placements"]] == ["C2"]
    assert diagnostics["start_snapshot"]["excluded_other_warehouse"] == 1
    assert diagnostics["start_snapshot"]["excluded_missing_warehouse"] == 1
    assert diagnostics["end_snapshot"]["excluded_other_warehouse"] == 1
    assert diagnostics["opening_inventory"]["accepted"] == 1
    assert diagnostics["opening_inventory"]["excluded_other_warehouse"] == 1
    assert pipeline["opening_stock_state"]["placements"][0]["cell_key"] == "C1"
    assert pipeline["opening_stock_state"]["placements"][0]["qty_units"] == 10
    assert pipeline["slotting_rule_state"]["sku_rules"] == []
    assert diagnostics["slotting_rules"]["receipt_sku_keys_without_slotting_rule"] == ["R"]
    assert state["pipeline_inputs_ready"] is True


def test_existing_opening_and_outbound_builders_are_called(monkeypatch):
    seen = {}
    real_reconcile = inputs_module.reconcile_opening_stock
    real_demands = inputs_module.build_outbound_pick_demands
    def reconcile(model, rows, snapshot):
        seen["opening"] = (rows, snapshot)
        return real_reconcile(model, rows, snapshot)
    def demands(rows):
        seen["outbound"] = rows
        return real_demands(rows)
    monkeypatch.setattr(inputs_module, "reconcile_opening_stock", reconcile)
    monkeypatch.setattr(inputs_module, "build_outbound_pick_demands", demands)
    state, _ = build()
    assert seen["opening"][1] is state["pipeline_inputs"]["start_placement_state"]
    assert seen["opening"][1] is not state["pipeline_inputs"]["end_placement_state"]
    assert seen["outbound"] == fixture_inputs()[5]


def test_slotting_scope_validation_duplicates_and_determinism():
    def change(values):
        values[6] = [
            {"sku_key": "X", "weight_zone": "heavy"},
            {"sku_key": "R", "weight_zone": "light", "priority_rank": None},
            {"sku_key": "R", "weight_zone": "light", "priority_rank": None},
            {"sku_key": "R", "weight_zone": "unassigned"},
            {"sku_key": "R", "weight_zone": "heavy", "priority_rank": True},
        ]
    first, diagnostics = build(transform=change)
    second, _ = build(transform=lambda values: (change(values), values[6].reverse()))
    rule = first["pipeline_inputs"]["slotting_rule_state"]["sku_rules"][0]
    assert {k: rule[k] for k in ("normalized_warehouse", "sku_key", "weight_zone", "priority_rank")} == {
        "normalized_warehouse": "склад е", "sku_key": "R", "weight_zone": "light", "priority_rank": None}
    assert len(rule["rule_key"]) == 64
    assert diagnostics["slotting_rules"]["slotting_rows_outside_receipt_scope"] == 1
    assert diagnostics["slotting_rules"]["duplicate_rows"] == 1
    assert diagnostics["slotting_rules"]["invalid_rows"] == 2
    assert first["pipeline_inputs"]["slotting_rule_state"] == second["pipeline_inputs"]["slotting_rule_state"]
    assert first["experiment_input_state_id"] == second["experiment_input_state_id"]


@pytest.mark.parametrize("zone_order", [[], ["heavy"] * 4, ["heavy", "medium", "light", "unassigned"]])
def test_invalid_zone_order_is_fatal(zone_order):
    state, diagnostics = build(zone_order=zone_order)
    assert state["pipeline_inputs"] == {} and state["pipeline_inputs_ready"] is False
    assert "invalid_zone_order" in diagnostics["configuration_errors"]


@pytest.mark.parametrize("coordinates", [(True, 1), (float("nan"), 1), (1, float("inf")), ("1", 1)])
def test_invalid_gate_coordinates_are_fatal(coordinates):
    state, diagnostics = build(transform=lambda values: values[7].update(x=coordinates[0], y=coordinates[1]))
    assert not state["pipeline_inputs_ready"]
    assert "gate_coordinates_invalid" in diagnostics["configuration_errors"]


def test_gate_replay_and_complete_pipeline_contract():
    state, _ = build()
    pipeline = state["pipeline_inputs"]
    assert set(pipeline) == set(inputs_module.PIPELINE_INPUT_KEYS)
    assert pipeline["gate_state"] == {"model_id": "M", "gates": [fixture_inputs()[7]]}
    assert pipeline["replay_rule_state"]["gate_key"] == "main_gate"
    assert pipeline["replay_rule_state"]["zone_order"] == inputs_module.DEFAULT_ZONE_ORDER
    assert pipeline["slotting_rule_state"]["zone_order"] == inputs_module.DEFAULT_ZONE_ORDER
    assert len(pipeline["replay_rule_state"]["replay_rule_state_id"]) == 64


@pytest.mark.parametrize("change,error", [
    (lambda values: values[1].update(dataset_id=""), "receipt_dataset_id_missing"),
    (lambda values: values[1].update(selected_normalized_warehouses=[] , receipt_sku_batches=[]), "one_normalized_warehouse_required"),
    (lambda values: values[1].update(selected_normalized_warehouses=["a", "b"]), "multiple_normalized_warehouses"),
])
def test_day_receipt_fatal_contracts(change, error):
    state, diagnostics = build(transform=change)
    assert not state["pipeline_inputs_ready"] and error in diagnostics["configuration_errors"]


def test_inputs_unchanged_deterministic_and_json_serializable():
    values = fixture_inputs(); before = copy.deepcopy(values)
    first = inputs_module.build_outbound_experiment_inputs(*values)
    second = inputs_module.build_outbound_experiment_inputs(*values)
    assert values == before and first == second
    json.dumps(first, ensure_ascii=False)


def test_actual_pipeline_accepts_bundle_from_builder():
    state, _ = build()
    experiment, diagnostics = run_outbound_distance_experiment(**state["pipeline_inputs"])
    assert experiment["receipt_dataset_id"] == "D"
    assert isinstance(diagnostics, dict)
    assert experiment["blocked_stage"] in (None, "physical_graph", "transition_analysis")


def test_module_has_no_ui_io_or_pipeline_execution_dependencies():
    source = Path(inputs_module.__file__).read_text(encoding="utf-8").casefold()
    for forbidden in ("pandas", "openpyxl", "streamlit", "path(", "open(", "run_outbound_distance_experiment"):
        assert forbidden not in source
    assert importlib.reload(inputs_module)
