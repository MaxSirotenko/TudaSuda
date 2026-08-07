import copy
import json

from warehouse_outbound_scenario_replay import replay_outbound_scenarios


def _inputs():
    cells = [
        {"row_number": "1", "cell_number": str(i), "tier": "1", "row_order": 1,
         "weight_zone": "heavy", "storage_type": "normal"}
        for i in range(1, 4)
    ]
    model = {"model_id": "m", "source_file_hash": "s", "cells": cells}
    nodes = [{"node_id": x} for x in ("G", "N1", "N2", "N3")]
    edges = [{"from_node_id": "G", "to_node_id": f"N{i}", "distance_m": d,
              "edge_id": f"E{i}", "traversable": True} for i, d in ((1, 2), (2, 5), (3, 9))]
    graph = {"physical_graph_state_id": "graph", "model_id": "m", "nodes": nodes, "edges": edges,
             "cell_access_links": [{"cell_key": f"1|{i}|1", "access_node_id": f"N{i}", "status": "valid"} for i in range(1, 4)],
             "gate_links": [{"gate_key": "main_gate", "gate_node_id": "G", "status": "valid"}],
             "summary": {"graph_ready_for_replay": True}}
    opening = {"placements": [{"sku_key": "O", "cell_key": "1|1|1", "qty_units": 8,
                                "unit_name": "короб", "quantity": 0, "occupied_capacity_pallets": 0}]}
    current = {"scenario": "current", "receipt_dataset_id": "receipts", "current_receipt_placement_state_id": "cur",
               "placements": [{"sku_key": "R", "cell_key": "virtual", "is_virtual": True,
                                "route_physical_cell_key": "1|3|1", "parent_physical_cell_key": "1|3|1",
                                "qty_units": 10, "unit_name": "короб", "quantity": 0, "occupied_capacity_pallets": 0}],
               "unresolved_receipt_batches": []}
    proposed = {"scenario": "proposed", "receipt_dataset_id": "receipts", "proposed_receipt_placement_state_id": "prop",
                "placements": [{"sku_key": "R", "cell_key": "1|2|1", "route_physical_cell_key": "1|2|1",
                                 "is_virtual": False, "qty_units": 10, "unit_name": "короб",
                                 "quantity": 0, "occupied_capacity_pallets": 0}], "unresolved_receipt_batches": []}
    outbound = {"orders": [
        {"order_key": "RO1", "outbound_order_number": "1", "created_at": "2025-01-01", "warehouse": " СКЛАД ", "source_indexes": [1],
         "demands": [{"demand_key": "D-O", "sku_key": "O", "requested_units": 4, "unit_name": "короба", "source_indexes": [1]},
                     {"demand_key": "D-R1", "sku_key": "R", "requested_units": 6, "unit_name": "коробов", "source_indexes": [2]}]},
        {"order_key": "RO2", "outbound_order_number": "2", "created_at": "2025-01-02", "warehouse": "склад", "source_indexes": [2],
         "demands": [{"demand_key": "D-R2", "sku_key": "R", "requested_units": 6, "unit_name": "короб", "source_indexes": [3]}]},
    ]}
    rules = {"replay_rule_state_id": "rules", "target_normalized_warehouse": "склад", "gate_key": "main_gate",
             "zone_order": ["heavy", "medium", "light", "fragile"]}
    return model, graph, outbound, opening, current, proposed, rules


def test_integration_sequential_shared_replay_and_virtual_routing():
    args = _inputs(); before = copy.deepcopy(args)
    state, diagnostics = replay_outbound_scenarios(*args)
    assert args == before
    assert state["shared_controls"]["same_outbound_demand_set"]
    assert state["shared_controls"]["same_opening_stock"]
    assert state["current"]["orders"][1]["shortage_units"] == 2
    assert state["proposed"]["orders"][1]["shortage_units"] == 2
    assert state["current"]["orders"][0]["picks"][1]["cell_key"] == "1|3|1"
    assert state["proposed"]["orders"][0]["picks"][1]["cell_key"] == "1|2|1"
    assert state["current"]["summary"]["route_distance_m"] != state["proposed"]["summary"]["route_distance_m"]
    for scenario in ("current", "proposed"):
        result = state[scenario]
        assert result["summary"]["stock_conservation_ok"]
        assert result["summary"]["demand_conservation_ok"]
        assert result["orders"][0]["start_node_id"] == result["orders"][1]["start_node_id"] == "G"
        assert all(order["end_node_id"] == "G" for order in result["orders"])
        assert all(leg["from_node_id"] != leg["to_node_id"] for order in result["orders"] for leg in order["route_legs"])
    assert diagnostics["shortest_path_calls"] > 0 and diagnostics["shortest_path_cache_hits"] > 0
    json.dumps((state, diagnostics), ensure_ascii=False)


def test_empty_accepted_set_is_deterministic_and_has_two_empty_scenarios():
    args = list(_inputs()); args[2] = {"orders": []}
    one = replay_outbound_scenarios(*args); two = replay_outbound_scenarios(*args)
    assert one == two
    assert one[0]["current"]["orders"] == one[0]["proposed"]["orders"] == []


def test_split_only_when_no_single_cell_covers_remaining_demand():
    args = list(_inputs()); args[4]["placements"] = []; args[5]["placements"] = []
    args[3]["placements"] = [
        {"sku_key": "S", "cell_key": f"1|{cell}|1", "qty_units": qty, "unit_name": "короб", "quantity": 0, "occupied_capacity_pallets": 0}
        for cell, qty in ((1, 3), (2, 4))]
    args[2] = {"orders": [{"order_key": "RO", "outbound_order_number": "1", "created_at": "1", "warehouse": "склад", "source_indexes": [1],
                            "demands": [{"demand_key": "D", "sku_key": "S", "requested_units": 6, "unit_name": "короб", "source_indexes": [1]}]}]}
    split = replay_outbound_scenarios(*args)[0]["current"]["orders"][0]["demands"][0]
    assert split["picked_units"] == 6 and split["pick_count"] == 2 and split["split"]
    args[3]["placements"].append({"sku_key": "S", "cell_key": "1|3|1", "qty_units": 10, "unit_name": "короб", "quantity": 0, "occupied_capacity_pallets": 0})
    full = replay_outbound_scenarios(*args)[0]["current"]["orders"][0]["demands"][0]
    assert full["picked_units"] == 6 and full["pick_count"] == 1 and not full["split"]


def test_configuration_errors_block_both_scenarios():
    args = list(_inputs()); args[6]["zone_order"] = ["heavy"]
    state, diagnostics = replay_outbound_scenarios(*args)
    assert "invalid_zone_order" in diagnostics["configuration_errors"]
    assert state["current"]["orders"] == state["proposed"]["orders"] == []
    args = list(_inputs()); args[4]["receipt_dataset_id"] = "different"
    state, diagnostics = replay_outbound_scenarios(*args)
    assert "receipt_dataset_id_mismatch" in diagnostics["configuration_errors"]
    assert not state["shared_controls"]["receipt_dataset_ids_match"]


def test_unsupported_unit_and_unresolved_receipts_are_not_stock():
    args = list(_inputs()); args[4]["unresolved_receipt_batches"] = [{"qty_units": 7}]
    args[2]["orders"][0]["demands"][0]["unit_name"] = "штука"
    state, diagnostics = replay_outbound_scenarios(*args)
    demand = state["current"]["orders"][0]["demands"][0]
    assert demand["status"] == "invalid" and demand["reasons"] == ["unsupported_unit"]
    assert state["current"]["receipt_unavailable"]["unresolved_qty_units"] == 7
    assert diagnostics["unsupported_unit_demands"] == 1


def test_unmapped_and_unreachable_stock_have_distinct_reasons():
    args = list(_inputs()); args[4]["placements"] = args[5]["placements"] = []
    args[2]["orders"] = [args[2]["orders"][0]]; args[2]["orders"][0]["demands"] = [args[2]["orders"][0]["demands"][0]]
    args[1]["cell_access_links"] = [x for x in args[1]["cell_access_links"] if x["cell_key"] != "1|1|1"]
    demand = replay_outbound_scenarios(*args)[0]["current"]["orders"][0]["demands"][0]
    assert "unmapped_cell_stock" in demand["reasons"]
    args = list(_inputs()); args[4]["placements"] = args[5]["placements"] = []
    args[2]["orders"] = [args[2]["orders"][0]]; args[2]["orders"][0]["demands"] = [args[2]["orders"][0]["demands"][0]]
    args[1]["edges"] = [x for x in args[1]["edges"] if x["to_node_id"] != "N1"]
    demand = replay_outbound_scenarios(*args)[0]["current"]["orders"][0]["demands"][0]
    assert "unreachable_cell_stock" in demand["reasons"]
