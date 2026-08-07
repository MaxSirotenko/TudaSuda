import copy
import json
import math

import pytest

from warehouse_physical_graph import build_physical_warehouse_graph, find_shortest_path


def model(**changes):
    value = {"model_id": "m", "source_file_hash": "source", "roads": [], "aisles": [], "cross_aisles": [], "rows": [], "cells": [], "navigation_nodes": [{"node_id": "legacy"}], "navigation_edges": [{"distance_m": -999}]}
    value.update(changes)
    return value


def road(key="road", x1=0, x2=10, y1=0, y2=2, road_type="bottom"):
    return {"road_id": key, "road_type": road_type, "x_min": x1, "x_max": x2, "y_min": y1, "y_max": y2}


def aisle(key="aisle", x1=0, x2=2, y1=2, y2=10, **extra):
    return {"aisle_id": key, "x_min": x1, "x_max": x2, "y_min": y1, "y_max": y2, **extra}


def test_empty_geometry_is_deterministic_and_ignores_legacy_graph():
    source = model(); before = copy.deepcopy(source)
    state, diagnostics = build_physical_warehouse_graph(source)
    assert state["nodes"] == state["edges"] == []
    assert diagnostics["source_gates"] == 0
    assert source == before
    assert all(n.get("node_id") != "legacy" for n in state["nodes"])
    assert state == build_physical_warehouse_graph(source)[0]
    json.dumps(state)


def test_one_corridor_has_two_endpoints_and_one_positive_axis_edge():
    state, _ = build_physical_warehouse_graph(model(roads=[road()]))
    assert len(state["nodes"]) == 2 and len(state["edges"]) == 1
    edge = state["edges"][0]
    assert edge["axis"] == "horizontal" and edge["distance_m"] == 10
    assert edge["from_node_id"] < edge["to_node_id"]


def test_perpendicular_intersection_has_one_junction_and_no_diagonal_or_zero_edges():
    state, diagnostics = build_physical_warehouse_graph(model(roads=[road()], aisles=[aisle(y1=0, y2=8)]))
    assert sum(n["node_type"] == "corridor_junction" for n in state["nodes"]) == 1
    assert all(e["distance_m"] > 0 and e["axis"] in {"horizontal", "vertical"} for e in state["edges"])
    assert diagnostics["diagonal_edges_prevented"] == 0


def test_touching_misaligned_centerlines_create_portal():
    state, _ = build_physical_warehouse_graph(model(roads=[road()], aisles=[aisle(x1=10, x2=12, y1=2, y2=8)]))
    assert any(n["node_type"] == "corridor_portal" for n in state["nodes"])
    assert any(e["edge_type"] == "corridor_portal" for e in state["edges"])
    assert len(state["connected_components"]) == 1


def test_collinear_touching_corridors_form_connected_line():
    state, _ = build_physical_warehouse_graph(model(roads=[road("a", 0, 5), road("b", 5, 10)]))
    assert len(state["connected_components"]) == 1
    assert sum(e["distance_m"] for e in state["edges"]) == 10


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_invalid_geometry_is_reported(bad):
    item = road(); item["x_max"] = bad
    state, diagnostics = build_physical_warehouse_graph(model(roads=[item]))
    assert not state["walkable_regions"] and state["invalid_walkable_regions"]
    assert diagnostics["invalid_roads"] == 1


def test_zero_width_and_wrong_orientation_are_invalid():
    zero = road("zero"); zero["x_max"] = zero["x_min"]
    vertical_road = road("wrong", 0, 1, 0, 10)
    state, _ = build_physical_warehouse_graph(model(roads=[zero, vertical_road]))
    assert len(state["invalid_walkable_regions"]) == 2


def test_duplicate_keys_and_duplicate_geometry_reject_all_conflicts():
    duplicates = [road("same"), road("same", 0, 20), road("x", 20, 30), road("y", 20, 30)]
    state, diagnostics = build_physical_warehouse_graph(model(roads=duplicates))
    assert state["walkable_regions"] == []
    assert diagnostics["duplicate_region_keys"] == 1
    assert diagnostics["duplicate_region_geometries"] == 1


def cell(key, row, x1, x2, y1=3, y2=5, **extra):
    return {"cell_key": key, "row_number": row, "cell_number": 1, "tier": 1, "physical_index": 0, "x_min": x1, "x_max": x2, "y_min": y1, "y_max": y2, **extra}


def test_unique_adjacent_aisle_maps_cell_but_road_and_far_aisle_do_not():
    source = model(rows=[{"row_number": "1"}], roads=[road()], aisles=[aisle("near", 0, 2, 2, 10), aisle("far", 8, 10, 2, 10)], cells=[cell("C", "1", 2, 3)])
    state, _ = build_physical_warehouse_graph(source)
    assert state["cell_access_links"][0]["longitudinal_region_key"] == "near"
    assert state["cell_access_links"][0]["x"] == 1
    assert sum(n["node_type"] == "cell_access" for n in state["nodes"]) == 1


def test_ambiguous_side_unknown_side_and_no_access_are_not_connected():
    aisles = [aisle("left", 0, 2), aisle("right", 3, 5)]
    cells = [cell("amb", "1", 2, 3), cell("badside", "1", 2, 3, side="верх"), cell("none", "1", 20, 21)]
    state, _ = build_physical_warehouse_graph(model(rows=[{"row_number": "1"}], aisles=aisles, cells=cells))
    assert {x["reason_code"] for x in state["ambiguous_cell_access"]} == {"multiple_adjacent_longitudinal_aisles", "unsupported_side_value"}
    assert state["inaccessible_cells"] == [{"cell_key": "none", "reason_code": "no_adjacent_longitudinal_aisle"}]


def test_explicit_russian_side_selects_one_aisle():
    source = model(rows=[{"row_number": "1"}], aisles=[aisle("left", 0, 2), aisle("right", 3, 5)], cells=[cell("C", "1", 2, 3, side="Слева")])
    state, _ = build_physical_warehouse_graph(source)
    assert state["cell_access_links"][0]["longitudinal_region_key"] == "left"


def test_duplicate_unknown_invalid_and_virtual_cells():
    cells = [cell("dup", "1", 2, 3), cell("dup", "1", 2, 3), cell("unknown", "9", 2, 3), {"cell_key": "invalid", "row_number": "1"}, cell("virtual", "1", 2, 3, is_virtual=True)]
    state, diagnostics = build_physical_warehouse_graph(model(rows=[{"row_number": "1"}], aisles=[aisle()], cells=cells))
    assert diagnostics["duplicate_cell_keys"] == 1
    assert {x["reason_code"] for x in state["inaccessible_cells"]} == {"duplicate_cell_key", "unknown_row", "invalid_cell_geometry"}
    assert "virtual" not in json.dumps(state["cell_access_links"])


def test_deep_lane_and_physical_slots_still_create_exactly_one_access_node():
    c = cell("deep", "1", 2, 3, storage_type="deep_lane", deep_lane_width=99, physical_slots=[{}, {}, {}])
    state, _ = build_physical_warehouse_graph(model(rows=[{"row_number": "1"}], aisles=[aisle()], cells=[c]))
    assert len(state["cell_access_links"]) == 1
    assert sum(n["node_type"] == "cell_access" for n in state["nodes"]) == 1
    assert "deep_lane_internal_depth_is_not_routed" in state["limitations"]


def test_gate_validation_duplicate_and_model_mismatch():
    source = model(roads=[road()])
    valid = {"model_id": "m", "gates": [{"gate_key": "g", "gate_name": "Gate", "road_type": "bottom", "x": 5, "y": .5}]}
    before = copy.deepcopy(valid); state, _ = build_physical_warehouse_graph(source, valid)
    assert len(state["gate_links"]) == 1 and any(e["edge_type"] == "gate_access" for e in state["edges"])
    assert valid == before
    outside = build_physical_warehouse_graph(source, {"gates": [{"gate_key": "g", "road_type": "bottom", "x": 50, "y": 1}]})[0]
    duplicate = build_physical_warehouse_graph(source, {"gates": [valid["gates"][0], valid["gates"][0]]})[0]
    mismatch = build_physical_warehouse_graph(source, {"model_id": "other", "gates": valid["gates"]})[0]
    assert outside["invalid_gates"][0]["reason_code"] == "gate_not_on_matching_road"
    assert len(duplicate["invalid_gates"]) == 2
    assert mismatch["invalid_gates"][0]["reason_code"] == "model_id_mismatch"


def graph_state(edges):
    ids = sorted({x for e in edges for x in e[:2]})
    return {"nodes": [{"node_id": x} for x in ids], "edges": [{"from_node_id": a, "to_node_id": b, "distance_m": d, "edge_id": key, "traversable": True} for a, b, d, key in edges]}


def test_shortest_path_contract_direct_multi_missing_same_and_unreachable():
    state = graph_state([("a", "b", 2, "ab"), ("b", "c", 3, "bc"), ("x", "y", 1, "xy")])
    assert find_shortest_path(state, "a", "b")["distance_m"] == 2
    result = find_shortest_path(state, "a", "c")
    assert result["distance_m"] == 5 and result["path_edge_ids"] == ["ab", "bc"]
    assert find_shortest_path(state, "missing", "a")["reason_code"] == "start_node_not_found"
    assert find_shortest_path(state, "a", "missing")["reason_code"] == "end_node_not_found"
    assert find_shortest_path(state, "a", "x")["reason_code"] == "unreachable"
    assert find_shortest_path(state, "a", "a")["distance_m"] == 0


def test_equal_paths_and_reordered_graph_choose_same_lexical_path():
    edges = [("s", "b", 1, "sb"), ("b", "t", 1, "bt"), ("s", "a", 1, "sa"), ("a", "t", 1, "at")]
    one = find_shortest_path(graph_state(edges), "s", "t")
    shuffled = graph_state(list(reversed(edges))); shuffled["nodes"].reverse(); shuffled["edges"].reverse()
    assert one == find_shortest_path(shuffled, "s", "t")
    assert one["path_node_ids"] == ["s", "a", "t"]


def integration(cross=True):
    source = model(
        roads=[road("bottom"), road("top", 0, 10, 10, 12, "top")],
        aisles=[aisle("left"), aisle("right", 8, 10)],
        cross_aisles=[{"id": "cross", "x_min": 2, "x_max": 8, "y_min": 5, "y_max": 7}] if cross else [],
        rows=[{"row_number": "L"}, {"row_number": "R"}],
        cells=[cell("L1", "L", 2, 3, 3, 5, side="left"), cell("R1", "R", 7, 8, 7, 9, side="right", physical_index=1)],
    )
    gates = {"model_id": "m", "gates": [{"gate_key": "main_gate", "gate_name": "Main", "road_type": "bottom", "x": 5, "y": .5}]}
    state, diagnostics = build_physical_warehouse_graph(source, gates)
    ids = {x["cell_key"]: x["access_node_id"] for x in state["cell_access_links"]}
    return source, state, diagnostics, find_shortest_path(state, ids["L1"], ids["R1"])


def test_integration_cross_aisle_is_shorter_and_graph_is_physical_and_ready():
    source, state, diagnostics, with_cross = integration(True)
    _, without_state, _, without_cross = integration(False)
    assert state["summary"] | {} == state["summary"]
    assert state["summary"]["walkable_regions"] == 5
    assert (len(state["nodes"]), len(state["edges"])) == (20, 21)
    assert state["summary"]["mapped_physical_cells"] == 2
    assert state["summary"]["inaccessible_physical_cells"] == state["summary"]["ambiguous_physical_cells"] == 0
    assert state["summary"]["connected_components"] == 1 and state["summary"]["graph_ready_for_replay"]
    assert with_cross["distance_m"] == 12 and without_cross["distance_m"] == 18
    assert with_cross["distance_m"] < without_cross["distance_m"]
    assert not any(e["axis"] == "horizontal" and e["distance_m"] == 8 for e in state["edges"] if e["edge_type"] == "cell_access")
    assert all(math.isfinite(e["distance_m"]) and e["distance_m"] > 0 for e in state["edges"] + without_state["edges"])
    assert diagnostics["configuration_errors"] == 0


def test_all_input_list_permutations_preserve_complete_result():
    source, expected, _, _ = integration(True)
    gates = {"model_id": "m", "gates": [{"gate_key": "g2", "road_type": "top", "x": 5, "y": 11}, {"gate_key": "g1", "road_type": "bottom", "x": 5, "y": 1}]}
    expected = build_physical_warehouse_graph(source, gates)
    changed = copy.deepcopy(source)
    for key in ("roads", "aisles", "cross_aisles", "cells"): changed[key].reverse()
    reversed_gates = copy.deepcopy(gates); reversed_gates["gates"].reverse()
    assert build_physical_warehouse_graph(changed, reversed_gates) == expected
