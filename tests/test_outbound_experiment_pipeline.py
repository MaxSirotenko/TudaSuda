import copy
import importlib
import json
from pathlib import Path

import pytest

import warehouse_outbound_experiment_pipeline as pipeline


def inputs():
    day = {
        "dataset_id": "D", "operational_date": "2026-08-07",
        "selected_normalized_warehouses": ["  СКЛАД Ё  "],
        "receipt_sku_batches": [{"normalized_warehouse": "склад е"}],
    }
    return ({"model_id": "M", "source_file_hash": "H"}, day, {}, {}, {},
            {"target_normalized_warehouse": "СКЛАД Е"}, {}, {},
            {"target_normalized_warehouse": "склад ё"})


def install_stages(monkeypatch, *, error_stage=None, graph_ready=True,
                   comparison_status="full_day_valid", unresolved=False, shortage=False):
    calls = []
    ids = {
        "transition_analysis": ("analysis_id", "A"),
        "virtual_slots": ("virtual_slot_state_id", "V"),
        "current_receipt_placements": ("current_receipt_placement_state_id", "C"),
        "proposed_receipt_placements": ("proposed_receipt_placement_state_id", "P"),
        "physical_graph": ("physical_graph_state_id", "G"),
        "outbound_replay": ("outbound_replay_state_id", "R"),
        "comparison": ("outbound_comparison_state_id", "X"),
    }
    names = {
        "transition_analysis": "analyze_receipt_snapshot_transitions",
        "virtual_slots": "build_receipt_virtual_slots",
        "current_receipt_placements": "build_current_receipt_placements",
        "proposed_receipt_placements": "build_proposed_receipt_placements",
        "physical_graph": "build_physical_warehouse_graph",
        "outbound_replay": "replay_outbound_scenarios",
        "comparison": "compare_outbound_replay_scenarios",
    }
    made = {}

    for stage, function_name in names.items():
        def fake(*args, _stage=stage):
            calls.append((_stage, args))
            key, value = ids[_stage]
            state = {key: value, "marker": _stage}
            if _stage == "physical_graph":
                state["summary"] = {"graph_ready_for_replay": graph_ready}
            if _stage in {"current_receipt_placements", "proposed_receipt_placements"} and unresolved:
                state["summary"] = {"unresolved_receipt_batches": 2}
            if _stage == "outbound_replay" and shortage:
                state["summary"] = {"shortage_units": 3}
            if _stage == "comparison":
                state.update({
                    "quality": {"comparison_status": comparison_status, "scope": "sentinel_scope",
                                "full_day_effect_valid": comparison_status == "full_day_valid"},
                    "raw_summary": {"current_route_distance_m": 999, "proposed_route_distance_m": 1,
                                    "current_shortage_units": 7, "proposed_shortage_units": 8,
                                    "current_receipt_unresolved_qty_units": 9,
                                    "proposed_receipt_unresolved_qty_units": 10},
                    "comparable_summary": {"non_comparable_orders": 4,
                                           "current_comparable_distance_m": 1000,
                                           "proposed_comparable_distance_m": 20,
                                           "distance_saved_m": 123.456, "improvement_percent": 12.3},
                    "coverage": {"accepted_orders": 6, "strict_comparable_orders": 2,
                                 "comparable_route_orders": 1, "order_comparability_percent": 33.3,
                                 "route_order_coverage_percent": 16.7,
                                 "requested_units_coverage_percent": 44.4},
                })
            diagnostics = {"configuration_errors": [f"{_stage}_error"] if error_stage == _stage else []}
            made[_stage] = (state, diagnostics)
            return state, diagnostics
        monkeypatch.setattr(pipeline, function_name, fake)
    return calls, made


def test_complete_call_order_exact_objects_retention_summary_and_ids(monkeypatch):
    calls, made = install_stages(monkeypatch)
    args = inputs()
    state, diagnostics = pipeline.run_outbound_distance_experiment(*args)
    assert [name for name, _ in calls] == list(pipeline.STAGES)
    assert calls[0][1] == args[:4]
    assert calls[1][1] == (args[0], made["transition_analysis"][0])
    assert calls[2][1] == (args[0], args[1], made["transition_analysis"][0], made["virtual_slots"][0])
    assert calls[5][1][1] is made["physical_graph"][0]
    assert calls[6][1] == (made["outbound_replay"][0],)
    assert state["execution_status"] == "complete" and state["blocked_stage"] is None
    assert state["completed_stages"] == list(pipeline.STAGES)
    assert state["states"] == {stage: made[stage][0] for stage in pipeline.STAGES}
    assert state["stage_diagnostics"] == {stage: made[stage][1] for stage in pipeline.STAGES}
    assert state["stage_ids"] == dict(zip(pipeline.STAGES, "AVCPGRX"))
    assert state["summary"]["distance_saved_m"] == 123.456
    assert state["summary"]["current_raw_distance_m"] == 999
    assert diagnostics["comparison_status"] == "full_day_valid"


@pytest.mark.parametrize("blocked", pipeline.STAGES)
def test_configuration_error_blocks_every_downstream_stage(monkeypatch, blocked):
    calls, _ = install_stages(monkeypatch, error_stage=blocked)
    state, diagnostics = pipeline.run_outbound_distance_experiment(*inputs())
    index = pipeline.STAGES.index(blocked)
    assert [name for name, _ in calls] == list(pipeline.STAGES[:index + 1])
    assert state["execution_status"] == "blocked"
    assert state["blocked_stage"] == blocked
    assert state["completed_stages"] == list(pipeline.STAGES[:index])
    assert all(state["states"][stage] is None for stage in pipeline.STAGES[index + 1:])
    assert all(state["stage_diagnostics"][stage] is None for stage in pipeline.STAGES[index + 1:])
    assert diagnostics["stage_configuration_error_counts"][blocked] == 1


def test_graph_not_ready_blocks_replay_and_graph_is_not_completed(monkeypatch):
    calls, _ = install_stages(monkeypatch, graph_ready=False)
    state, diagnostics = pipeline.run_outbound_distance_experiment(*inputs())
    assert [name for name, _ in calls][-1] == "physical_graph"
    assert state["blocked_stage"] == "physical_graph"
    assert state["blocked_reasons"] == ["physical_graph_not_ready_for_replay"]
    assert state["states"]["outbound_replay"] is None
    assert "physical_graph" not in state["completed_stages"]
    assert diagnostics["graph_ready_for_replay"] is False


@pytest.mark.parametrize("status", ["partial", "not_comparable", "full_day_valid"])
def test_business_quality_results_do_not_block_execution(monkeypatch, status):
    install_stages(monkeypatch, comparison_status=status, unresolved=True, shortage=True)
    state, _ = pipeline.run_outbound_distance_experiment(*inputs())
    assert state["execution_status"] == "complete"
    assert state["summary"]["comparison_status"] == status
    assert state["summary"]["full_day_effect_valid"] is (status == "full_day_valid")


def test_deterministic_json_serializable_and_inputs_not_mutated(monkeypatch):
    install_stages(monkeypatch)
    args = inputs()
    before = copy.deepcopy(args)
    first = pipeline.run_outbound_distance_experiment(*args)
    second = pipeline.run_outbound_distance_experiment(*args)
    assert args == before and first == second
    assert len(first[0]["experiment_state_id"]) == 64
    json.dumps(first, ensure_ascii=False)


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda values: values.__setitem__(1, {}), "receipt_dataset_id_missing"),
        (lambda values: values[1].update(selected_normalized_warehouses=["a", "b"]), "multiple_normalized_warehouses"),
        (lambda values: values[5].update(target_normalized_warehouse="other"), "slotting_target_normalized_warehouse_mismatch"),
        (lambda values: values[8].update(target_normalized_warehouse="other"), "replay_target_normalized_warehouse_mismatch"),
        (lambda values: values.__setitem__(0, []), "invalid_top_level_input_contracts"),
    ],
)
def test_invalid_pipeline_inputs_block_before_business_stages(monkeypatch, change, reason):
    calls, _ = install_stages(monkeypatch)
    values = list(inputs())
    change(values)
    state, diagnostics = pipeline.run_outbound_distance_experiment(*values)
    assert not calls and state["blocked_stage"] == "transition_analysis"
    assert reason in diagnostics["configuration_errors"]
    assert all(value is None for value in state["states"].values())
    assert state["summary"]["distance_saved_m"] is None


def test_real_receipt_stage_contracts_connect_end_to_end(monkeypatch):
    # Real transition, virtual-slot, CURRENT and PROPOSED implementations are used.
    model = {"model_id": "M", "source_file_hash": "H", "cells": [
        {"cell_key": "C1", "row_number": 1, "row_order": 1, "cell_number": 1,
         "tier": 1, "weight_zone": "heavy", "physical_index": 1},
        {"cell_key": "C2", "row_number": 1, "row_order": 1, "cell_number": 2,
         "tier": 1, "weight_zone": "heavy", "physical_index": 2},
    ]}
    scenario = {"receipt_dataset_id": "D", "receipt_batch_keys": ["B"], "total_boxes": 2}
    day = {"dataset_id": "D", "operational_date": "2026-08-07",
           "selected_normalized_warehouses": ["w"], "receipt_sku_batches": [
               {"receipt_batch_key": "B", "dataset_id": "D", "operational_date": "2026-08-07",
                "warehouse": "W", "normalized_warehouse": "w", "sku_key": "S", "qty_units": 2,
                "unit_name": "короб"}],
           "scenario_inputs": {"current": copy.deepcopy(scenario), "proposed": copy.deepcopy(scenario)}}
    start = {"placements": [], "excluded_inventory": [], "unmatched_inventory": [], "unplaced_inventory": []}
    end = {"placements": [{"placement_id": "E", "warehouse": "W", "cell_key": "C1",
                            "sku_key": "S", "production_date": "2026-01-01"}],
           "excluded_inventory": [], "unmatched_inventory": [], "unplaced_inventory": []}
    rules = {"slotting_rule_state_id": "SR", "dataset_id": "D", "target_normalized_warehouse": "w",
             "zone_order": ["heavy", "medium", "light", "fragile"], "sku_rules": [
                 {"rule_key": "RULE", "normalized_warehouse": "w", "sku_key": "S",
                  "weight_zone": "heavy", "priority_rank": 1, "source": "test"}]}
    monkeypatch.setattr(pipeline, "build_physical_warehouse_graph", lambda *_: (
        {"physical_graph_state_id": "G", "summary": {"graph_ready_for_replay": True}},
        {"configuration_errors": []}))
    monkeypatch.setattr(pipeline, "replay_outbound_scenarios", lambda *args: (
        {"outbound_replay_state_id": "R", "received_current": args[4]["current_receipt_placement_state_id"],
         "received_proposed": args[5]["proposed_receipt_placement_state_id"]}, {"configuration_errors": []}))
    monkeypatch.setattr(pipeline, "compare_outbound_replay_scenarios", lambda state: (
        {"outbound_comparison_state_id": "X", "quality": {"comparison_status": "partial",
         "scope": "comparable_orders_only", "full_day_effect_valid": False}}, {"configuration_errors": []}))
    state, _ = pipeline.run_outbound_distance_experiment(
        model, day, start, end, {"model_id": "M", "placements": []}, rules, {}, {},
        {"target_normalized_warehouse": "w"})
    assert state["execution_status"] == "complete"
    assert state["states"]["current_receipt_placements"]["placements"][0]["cell_key"] == "C1"
    assert state["states"]["proposed_receipt_placements"]["placements"][0]["cell_key"] == "C1"


def test_module_has_no_boundary_or_nondeterministic_dependencies():
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    for forbidden in ("streamlit", "pandas", "uuid", "random", "datetime.now", "perf_counter", "open("):
        assert forbidden not in source.casefold()
    assert importlib.reload(pipeline)
