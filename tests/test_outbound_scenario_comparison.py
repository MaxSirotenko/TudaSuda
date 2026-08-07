import copy
import json
import math

import pytest

from warehouse_outbound_scenario_comparison import compare_outbound_replay_scenarios
from warehouse_outbound_scenario_replay import replay_outbound_scenarios
from tests.test_outbound_scenario_replay import _inputs


def _order(key="RO1", distance=100, picked=10, shortage=0):
    return {"order_key": key, "outbound_order_number": key, "created_at": "2025-01-01",
            "warehouse": "склад", "status": "fulfilled" if not shortage else "partial",
            "returned_to_gate": True, "requested_units": 10, "picked_units": picked,
            "shortage_units": shortage, "route_distance_m": distance,
            "picks": [{}] if picked else [], "route_legs": [{}],
            "demands": [{"demand_key": "D", "sku_key": "SKU", "unit_name": "короб",
                         "requested_units": 10, "picked_units": picked,
                         "shortage_units": shortage, "split": False}]}


def _state(current=None, proposed=None):
    current = [] if current is None else current
    proposed = copy.deepcopy(current) if proposed is None else proposed
    def summary(orders):
        return {"requested_units": sum(x["requested_units"] for x in orders),
                "picked_units": sum(x["picked_units"] for x in orders),
                "shortage_units": sum(x["shortage_units"] for x in orders),
                "orders_with_shortage": sum(x["shortage_units"] > 0 for x in orders),
                "receipt_unresolved_batches": 0, "receipt_unresolved_qty_units": 0,
                "stock_conservation_ok": True, "demand_conservation_ok": True}
    controls = {key: True for key in ("same_outbound_demand_set", "same_opening_stock",
        "same_physical_graph", "same_gate", "same_zone_order", "receipt_dataset_ids_match",
        "only_receipt_placement_differs")}
    return {"outbound_replay_state_id": "replay", "outbound_demand_set_id": "demand",
            "physical_graph_state_id": "graph", "opening_stock_identity": "stock",
            "receipt_dataset_id": "receipt", "target_normalized_warehouse": "склад",
            "gate_key": "gate", "zone_order": ["heavy", "medium", "light", "fragile"],
            "accepted_order_keys": [x["order_key"] for x in current],
            "shared_controls": controls,
            "current": {"scenario": "current", "orders": current, "summary": summary(current)},
            "proposed": {"scenario": "proposed", "orders": proposed, "summary": summary(proposed)}}


def test_empty_valid_state_is_deterministic_json_serializable_and_not_comparable():
    source = _state(); before = copy.deepcopy(source)
    one = compare_outbound_replay_scenarios(source)
    assert one == compare_outbound_replay_scenarios(source)
    assert source == before
    assert one[0]["quality"]["comparison_status"] == "full_day_valid"
    assert one[0]["coverage"]["order_comparability_percent"] is None
    json.dumps(one)


@pytest.mark.parametrize(("current", "proposed", "classification", "saved"), [
    (100, 70, "improved", 30), (70, 100, "worsened", -30),
    (10, 10.0000005, "equal", -0.000001), (0, 10, "worsened", -10),
    (0, 0, "no_route_activity", 0),
])
def test_distance_semantics(current, proposed, classification, saved):
    state = _state([_order(distance=current)], [_order(distance=proposed)])
    item = compare_outbound_replay_scenarios(state)[0]["order_comparisons"][0]
    assert item["strict_comparable"] and item["service_equivalent"]
    assert item["classification"] == classification and item["distance_saved_m"] == saved
    if current == 0: assert item["improvement_percent"] is None


@pytest.mark.parametrize(("mutation", "reason"), [
    (lambda o: o.update(requested_units=9), "requested_units_mismatch"),
    (lambda o: o["demands"].append({"demand_key": "D2"}), "demand_set_mismatch"),
    (lambda o: o["demands"][0].update(picked_units=9), "picked_units_mismatch"),
    (lambda o: o["demands"][0].update(shortage_units=1), "shortage_units_mismatch"),
    (lambda o: o["demands"][0].update(sku_key="OTHER"), "sku_mismatch"),
    (lambda o: o["demands"][0].update(unit_name="штука"), "unit_mismatch"),
    (lambda o: o.update(status="invalid_route"), "proposed_invalid_route"),
    (lambda o: o.update(returned_to_gate=False), "proposed_not_returned_to_gate"),
    (lambda o: o.update(route_distance_m=math.nan), "invalid_proposed_distance"),
    (lambda o: o.update(route_distance_m=math.inf), "invalid_proposed_distance"),
    (lambda o: o.update(route_distance_m=-1), "invalid_proposed_distance"),
])
def test_non_comparable_reasons_never_report_savings(mutation, reason):
    current, proposed = _order(), _order(distance=60)
    mutation(proposed)
    item = compare_outbound_replay_scenarios(_state([current], [proposed]))[0]["order_comparisons"][0]
    assert not item["strict_comparable"] and reason in item["reasons"]
    assert item["distance_saved_m"] is item["improvement_percent"] is None
    assert item["classification"] == "not_comparable"


def test_same_order_total_but_different_demand_results_is_not_comparable():
    current, proposed = _order(), _order(distance=60)
    for order, values in ((current, (5, 3)), (proposed, (3, 5))):
        order["picked_units"] = 8; order["shortage_units"] = 2
        order["demands"] = [{"demand_key": key, "sku_key": key, "unit_name": "короб",
            "requested_units": 5, "picked_units": picked, "shortage_units": 5-picked}
            for key, picked in zip(("A", "B"), values)]
    item = compare_outbound_replay_scenarios(_state([current], [proposed]))[0]["order_comparisons"][0]
    assert not item["service_equivalent"] and "picked_units_mismatch" in item["reasons"]
    assert item["distance_saved_m"] is None


def test_pick_split_cell_and_route_structure_differences_are_allowed():
    current, proposed = _order(distance=50), _order(distance=35)
    current["picks"] = [{"cell_key": "A"}, {"cell_key": "B"}]
    current["demands"][0]["split"] = True
    proposed["picks"] = [{"cell_key": "C"}]; proposed["route_legs"] = [{}, {}, {}]
    item = compare_outbound_replay_scenarios(_state([current], [proposed]))[0]["order_comparisons"][0]
    assert item["strict_comparable"] and item["distance_saved_m"] == 15
    assert item["current_pick_events"] == 2 and item["proposed_pick_events"] == 1


@pytest.mark.parametrize(("target", "expected"), [
    ("duplicate_current", "duplicate_current_order_keys"),
    ("duplicate_proposed", "duplicate_proposed_order_keys"),
    ("different_sets", "current_proposed_order_keys_mismatch"),
    ("accepted", "accepted_order_keys_mismatch"),
    ("control", "shared_control_same_gate_not_true"),
    ("demand_id", "outbound_demand_set_id_missing"),
    ("graph_id", "physical_graph_state_id_missing"),
    ("stock", "current_stock_conservation_ok_not_true"),
    ("demand", "proposed_demand_conservation_ok_not_true"),
])
def test_fatal_configuration_errors(target, expected):
    state = _state([_order()], [_order()])
    if target == "duplicate_current": state["current"]["orders"].append(_order())
    elif target == "duplicate_proposed": state["proposed"]["orders"].append(_order())
    elif target == "different_sets": state["proposed"]["orders"][0]["order_key"] = "X"
    elif target == "accepted": state["accepted_order_keys"] = ["X"]
    elif target == "control": state["shared_controls"]["same_gate"] = False
    elif target == "demand_id": state["outbound_demand_set_id"] = ""
    elif target == "graph_id": state["physical_graph_state_id"] = ""
    elif target == "stock": state["current"]["summary"]["stock_conservation_ok"] = False
    else: state["proposed"]["summary"]["demand_conservation_ok"] = False
    result, diagnostics = compare_outbound_replay_scenarios(state)
    assert expected in diagnostics["configuration_errors"]
    assert result["quality"]["comparison_status"] == "invalid_configuration"
    assert result["order_comparisons"] == []


def test_partial_coverage_and_primary_kpi_exclude_false_saving_and_zero_routes():
    current = [_order("RO1", 100), _order("RO2", 50), _order("RO3", 80), _order("RO4", 0, 0, 10)]
    proposed = [_order("RO1", 70), _order("RO2", 60), _order("RO3", 40, 7, 3), _order("RO4", 0, 0, 10)]
    state = _state(current, proposed)
    result, diagnostics = compare_outbound_replay_scenarios(state)
    summary, coverage = result["comparable_summary"], result["coverage"]
    assert summary["distance_saved_m"] == 20 and summary["comparable_route_orders"] == 2
    assert summary["non_comparable_orders"] == 1 and summary["no_route_activity_orders"] == 1
    assert summary["improved_orders"] == summary["worsened_orders"] == 1
    assert summary["share_improved_orders_percent"] == 50
    assert coverage["order_comparability_percent"] == 75
    assert coverage["requested_units_coverage_percent"] == 75
    assert result["quality"]["comparison_status"] == "partial"
    assert diagnostics["current_raw_distance_m"] == 230 and diagnostics["proposed_raw_distance_m"] == 170
    assert not result["raw_summary"]["raw_distance_difference_is_business_effect"]


@pytest.mark.parametrize(("scenario", "field"), [
    ("current", "receipt_unresolved_qty_units"), ("proposed", "receipt_unresolved_qty_units"),
    ("current", "shortage_units"), ("proposed", "shortage_units"),
])
def test_incomplete_day_evidence_blocks_full_day_claim(scenario, field):
    state = _state([_order()], [_order(distance=70)])
    state[scenario]["summary"][field] = 1
    result, _ = compare_outbound_replay_scenarios(state)
    assert not result["quality"]["full_day_effect_valid"]
    assert result["quality"]["comparison_status"] == "partial"
    assert result["quality"]["scope"] == "comparable_orders_only"


def test_order_and_demand_permutations_do_not_change_result_or_ids():
    current = [_order("RO2", 50), _order("RO1", 100)]
    proposed = [_order("RO1", 70), _order("RO2", 40)]
    one = compare_outbound_replay_scenarios(_state(current, proposed))[0]
    state = _state(list(reversed(current)), list(reversed(proposed)))
    state["accepted_order_keys"] = ["RO2", "RO1"]
    two = compare_outbound_replay_scenarios(state)[0]
    assert one["order_comparisons"] == two["order_comparisons"]
    assert one["outbound_comparison_state_id"] == two["outbound_comparison_state_id"]


def test_integration_replay_metrics_and_no_source_mutation():
    replay, _ = replay_outbound_scenarios(*_inputs()); before = copy.deepcopy(replay)
    result, diagnostics = compare_outbound_replay_scenarios(replay)
    assert replay == before
    assert diagnostics == {**diagnostics,
        "accepted_orders": 2, "strict_comparable_orders": 2, "non_comparable_orders": 0,
        "comparable_route_orders": 2, "current_raw_distance_m": 40.0,
        "proposed_raw_distance_m": 24.0, "current_comparable_distance_m": 40.0,
        "proposed_comparable_distance_m": 24.0, "comparable_distance_saved_m": 16.0,
        "improved_orders": 2, "worsened_orders": 0, "equal_orders": 0,
        "full_day_effect_valid": False}
    assert result["comparable_summary"]["improvement_percent"] == 40
    assert result["coverage"]["order_comparability_percent"] == 100
    assert result["coverage"]["requested_units_coverage_percent"] == 100
    assert result["quality"]["comparison_status"] == "partial"
    assert result["quality"]["scope"] == "comparable_orders_only"
    assert not any("financial" in key for key in result)
