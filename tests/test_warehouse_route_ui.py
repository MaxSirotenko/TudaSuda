import copy

from warehouse_route_ui import ROUTE_COLORS, build_route_overlay, select_replay_order


def fixture():
    graph = {"nodes": [{"node_id": "g", "x": 0, "y": 0}, {"node_id": "a", "x": 0, "y": 2}, {"node_id": "b", "x": 3, "y": 2}],
             "edges": [{"edge_id": "e1", "from_node_id": "g", "to_node_id": "a"}, {"edge_id": "e2", "from_node_id": "a", "to_node_id": "b"}]}
    order = {"order_key": "RO-1", "picker_distance_m": 10, "returned_to_gate": True,
             "pick_events": [{"cell_key": "152/150"}, {"cell_key": "154/142"}],
             "route_legs": [{"from_kind": "gate", "to_kind": "pick", "distance_m": 5, "path_node_ids": ["g", "a", "b"], "path_edge_ids": ["e1", "e2"]},
                            {"from_kind": "pick", "to_kind": "gate", "distance_m": 5, "path_node_ids": ["b", "a", "g"], "path_edge_ids": ["e2", "e1"]}]}
    return graph, order


def test_overlay_uses_authoritative_legs_nodes_edges_order_and_distance_without_mutation():
    graph, order = fixture(); original = copy.deepcopy(order)
    payload = build_route_overlay(order, graph, "current")
    assert order == original
    assert payload["route_distance_m"] == order["picker_distance_m"] == 10
    assert payload["route_points"][:3] == [{"node_id": "g", "x": 0, "y": 0}, {"node_id": "a", "x": 0, "y": 2}, {"node_id": "b", "x": 3, "y": 2}]
    assert payload["legs"][0]["path_edge_ids"] == ["e1", "e2"]
    assert [x["number"] for x in payload["pick_stops"]] == [1, 2]
    assert payload["starts_at_gate"] and payload["returns_to_gate"] and payload["distance_consistent"]


def test_unresolved_node_is_diagnostic_and_never_gets_invented_coordinates():
    graph, order = fixture(); order["route_legs"][0]["path_node_ids"].insert(1, "missing")
    payload = build_route_overlay(order, graph, "proposed")
    assert payload["unresolved_node_ids"] == ["missing"] and not payload["visualization_ready"]
    assert "часть пути" in payload["visualization_message"]
    assert all(p["node_id"] != "missing" for p in payload["route_points"])


def test_scenarios_use_distinct_central_colors_and_selection_does_not_build_geometry():
    graph, order = fixture(); replay = {"current": {"orders": [order]}, "proposed": {"orders": [{**order, "order_key": "RO-2"}]}}
    assert ROUTE_COLORS["current"] != ROUTE_COLORS["proposed"]
    assert select_replay_order(replay, "current", "RO-1") is order
    assert select_replay_order(replay, "proposed", "RO-1") is None
