"""Deterministic routing graph built from physical warehouse geometry."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

COORDINATE_PRECISION = 6
COORDINATE_TOLERANCE_M = 0.000001
LIMITATIONS = [
    "graph_represents_walkable_centerlines_not_full_free_space",
    "legacy_navigation_nodes_and_edges_are_not_used",
    "racks_and_storage_cells_are_not_traversable",
    "cell_routes_start_at_aisle_access_points",
    "cells_without_unambiguous_aisle_access_are_not_connected",
    "virtual_slots_are_not_graph_nodes",
    "virtual_placements_use_parent_physical_cell_access_in_future_replay",
    "deep_lane_internal_depth_is_not_routed",
    "gate_locations_must_be_explicitly_supplied",
    "graph_distances_are_geometric_meters",
    "graph_build_does_not_replay_outbound_orders",
    "graph_build_does_not_calculate_current_or_proposed_totals",
    "graph_build_does_not_modify_geometry",
    "disconnected_components_are_not_auto_connected",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError
    result = round(float(value), COORDINATE_PRECISION)
    if not math.isfinite(result):
        raise ValueError
    return result


def _rect(item: Mapping[str, Any]) -> dict[str, float]:
    result = {key: _number(item[key]) for key in ("x_min", "x_max", "y_min", "y_max")}
    if result["x_max"] <= result["x_min"] or result["y_max"] <= result["y_min"]:
        raise ValueError
    result["x_center"] = _number(item.get("x_center", (result["x_min"] + result["x_max"]) / 2))
    result["y_center"] = _number(item.get("y_center", (result["y_min"] + result["y_max"]) / 2))
    if not (result["x_min"] <= result["x_center"] <= result["x_max"] and result["y_min"] <= result["y_center"] <= result["y_max"]):
        raise ValueError
    return result


def _source_key(item: Mapping[str, Any]) -> str | None:
    for key in ("aisle_id", "road_id", "cross_aisle_id", "region_id", "id"):
        if item.get(key) not in (None, ""):
            return str(item[key])
    return None


def _regions(model: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    candidates: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    counts = {"source_roads": 0, "source_longitudinal_aisles": 0, "source_cross_aisles": 0}
    specs = (("roads", "horizontal"), ("aisles", "vertical"), ("cross_aisles", "horizontal"))
    for collection, orientation in specs:
        raw = model.get(collection, [])
        raw = raw if isinstance(raw, list) else []
        counts[{"roads": "source_roads", "aisles": "source_longitudinal_aisles", "cross_aisles": "source_cross_aisles"}[collection]] = len(raw)
        for item in raw:
            if not isinstance(item, Mapping):
                invalid.append({"collection": collection, "reason_code": "invalid_record", "record": item})
                continue
            try:
                rect = _rect(item)
                if orientation == "horizontal" and rect["x_max"] - rect["x_min"] < rect["y_max"] - rect["y_min"]:
                    raise ValueError
                if orientation == "vertical" and rect["y_max"] - rect["y_min"] < rect["x_max"] - rect["x_min"]:
                    raise ValueError
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid.append({"collection": collection, "reason_code": "invalid_geometry", "source_key": _source_key(item)})
                continue
            road_type = str(item.get("road_type", "")).strip().lower()
            region_type = ("top_road" if road_type == "top" else "bottom_road" if road_type == "bottom" else "other_road") if collection == "roads" else ("longitudinal_aisle" if collection == "aisles" else "cross_aisle")
            identity = {"region_type": region_type, **rect, **{k: item.get(k) for k in ("row_from", "row_to", "row_number", "after_cell_number", "road_type")}}
            source = _source_key(item)
            key = source or _hash(identity)
            width = rect["y_max"] - rect["y_min"] if orientation == "horizontal" else rect["x_max"] - rect["x_min"]
            length = rect["x_max"] - rect["x_min"] if orientation == "horizontal" else rect["y_max"] - rect["y_min"]
            candidates.append({"region_key": key, "region_type": region_type, "source_key": source, "orientation": orientation, **rect, "width_m": _number(width), "length_m": _number(length), "row_from": item.get("row_from"), "row_to": item.get("row_to"), "road_type": road_type or None})

    key_counts = Counter(x["region_key"] for x in candidates)
    geometry = lambda x: (x["orientation"], x["x_min"], x["x_max"], x["y_min"], x["y_max"])
    geometry_counts = Counter(geometry(x) for x in candidates)
    source_geometries: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    for item in candidates:
        if item["source_key"]:
            source_geometries[item["source_key"]].add(geometry(item))
    rejected = {x["region_key"] for x in candidates if key_counts[x["region_key"]] > 1 or geometry_counts[geometry(x)] > 1 or (x["source_key"] and len(source_geometries[x["source_key"]]) > 1)}
    valid = [x for x in candidates if x["region_key"] not in rejected]
    for item in candidates:
        if item["region_key"] in rejected:
            reason = "duplicate_region_key" if key_counts[item["region_key"]] > 1 else "duplicate_region_geometry" if geometry_counts[geometry(item)] > 1 else "conflicting_source_identity"
            invalid.append({"collection": item["region_type"], "reason_code": reason, "region_key": item["region_key"]})
    valid.sort(key=lambda x: (x["region_type"], x["x_min"], x["y_min"], x["x_max"], x["y_max"], x["region_key"]))
    invalid.sort(key=_canonical)
    counts["duplicate_region_keys"] = sum(1 for n in key_counts.values() if n > 1)
    counts["duplicate_region_geometries"] = sum(1 for n in geometry_counts.values() if n > 1)
    return valid, invalid, counts


def _touch(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    t = COORDINATE_TOLERANCE_M
    return a["x_min"] <= b["x_max"] + t and b["x_min"] <= a["x_max"] + t and a["y_min"] <= b["y_max"] + t and b["y_min"] <= a["y_max"] + t


def _side(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().replace("ё", "е")
    if not normalized:
        return None
    if normalized in {"left", "лево", "левая", "левый", "слева", "l"}:
        return "left"
    if normalized in {"right", "право", "правая", "правый", "справа", "r"}:
        return "right"
    return "unsupported"


def build_physical_warehouse_graph(model: dict[str, Any], gate_state: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a JSON-serializable centerline graph without mutating inputs."""
    source = model if isinstance(model, Mapping) else {}
    regions, invalid_regions, diagnostics = _regions(source)
    events: dict[tuple[float, float], dict[str, Any]] = {}
    portal_segments: list[tuple[tuple[float, float], tuple[float, float], tuple[str, ...]]] = []

    def event(point: tuple[float, float], keys: list[str] | tuple[str, ...], kind: str = "corridor_endpoint", cell: str | None = None, gate: str | None = None) -> None:
        point = (_number(point[0]), _number(point[1]))
        entry = events.setdefault(point, {"keys": set(), "kinds": set(), "cells": set(), "gates": set()})
        entry["keys"].update(keys); entry["kinds"].add(kind)
        if cell: entry["cells"].add(cell)
        if gate: entry["gates"].add(gate)

    for r in regions:
        if r["orientation"] == "horizontal":
            event((r["x_min"], r["y_center"]), [r["region_key"]]); event((r["x_max"], r["y_center"]), [r["region_key"]])
        else:
            event((r["x_center"], r["y_min"]), [r["region_key"]]); event((r["x_center"], r["y_max"]), [r["region_key"]])
    for index, a in enumerate(regions):
        for b in regions[index + 1:]:
            if not _touch(a, b): continue
            keys = tuple(sorted((a["region_key"], b["region_key"])))
            if a["orientation"] != b["orientation"]:
                h, v = (a, b) if a["orientation"] == "horizontal" else (b, a)
                x, y = v["x_center"], h["y_center"]
                if h["x_min"] - COORDINATE_TOLERANCE_M <= x <= h["x_max"] + COORDINATE_TOLERANCE_M and v["y_min"] - COORDINATE_TOLERANCE_M <= y <= v["y_max"] + COORDINATE_TOLERANCE_M:
                    event((x, y), list(keys), "corridor_junction")
                else:
                    px = min(max(x, h["x_min"]), h["x_max"]); py = min(max(y, v["y_min"]), v["y_max"])
                    points = [(px, y), (px, py), (x, py)]
                    for p in points: event(p, list(keys), "corridor_portal")
                    portal_segments.extend((points[i], points[i + 1], keys) for i in range(2) if points[i] != points[i + 1])
            else:
                if a["orientation"] == "horizontal" and abs(a["y_center"] - b["y_center"]) <= COORDINATE_TOLERANCE_M:
                    x = _number((max(a["x_min"], b["x_min"]) + min(a["x_max"], b["x_max"])) / 2); event((x, a["y_center"]), list(keys), "corridor_junction")
                elif a["orientation"] == "vertical" and abs(a["x_center"] - b["x_center"]) <= COORDINATE_TOLERANCE_M:
                    y = _number((max(a["y_min"], b["y_min"]) + min(a["y_max"], b["y_max"])) / 2); event((a["x_center"], y), list(keys), "corridor_junction")
                else:
                    # Parallel overlapping/touching rectangles need a short
                    # orthogonal portal between their distinct centerlines.
                    if a["orientation"] == "horizontal":
                        x = _number((max(a["x_min"], b["x_min"]) + min(a["x_max"], b["x_max"])) / 2)
                        points = [(x, a["y_center"]), (x, b["y_center"])]
                    else:
                        y = _number((max(a["y_min"], b["y_min"]) + min(a["y_max"], b["y_max"])) / 2)
                        points = [(a["x_center"], y), (b["x_center"], y)]
                    for p in points: event(p, list(keys), "corridor_portal")
                    if points[0] != points[1]: portal_segments.append((points[0], points[1], keys))

    rows = {str(r.get("row_number")): r for r in source.get("rows", []) if isinstance(r, Mapping) and r.get("row_number") is not None}
    raw_cells = [c for c in source.get("cells", []) if isinstance(c, Mapping) and not c.get("is_virtual")]
    cell_counts = Counter(str(c.get("cell_key")) for c in raw_cells if c.get("cell_key") not in (None, ""))
    inaccessible: list[dict[str, Any]] = []; ambiguous: list[dict[str, Any]] = []; invalid_cells: list[dict[str, Any]] = []; links: list[dict[str, Any]] = []
    aisles = [r for r in regions if r["region_type"] == "longitudinal_aisle"]
    for cell in sorted(raw_cells, key=_canonical):
        key = str(cell.get("cell_key") or "")
        row_number = str(cell.get("row_number")) if cell.get("row_number") is not None else ""
        if not key or cell_counts[key] > 1:
            reason = "duplicate_cell_key" if key and cell_counts[key] > 1 else "invalid_cell_geometry"
            record = {"cell_key": key or None, "reason_code": reason}; inaccessible.append(record); invalid_cells.append(record); continue
        if row_number not in rows:
            inaccessible.append({"cell_key": key, "reason_code": "unknown_row"}); continue
        try: rect = _rect(cell)
        except (KeyError, TypeError, ValueError, OverflowError):
            record = {"cell_key": key, "reason_code": "invalid_cell_geometry"}; inaccessible.append(record); invalid_cells.append(record); continue
        candidates = []
        for aisle in aisles:
            y_overlap = min(rect["y_max"], aisle["y_max"]) >= max(rect["y_min"], aisle["y_min"]) - COORDINATE_TOLERANCE_M
            left_touch = abs(rect["x_min"] - aisle["x_max"]) <= COORDINATE_TOLERANCE_M
            right_touch = abs(rect["x_max"] - aisle["x_min"]) <= COORDINATE_TOLERANCE_M
            relation = "left" if left_touch else "right" if right_touch else None
            confirmed = str(aisle.get("row_to")) == row_number if relation == "left" else str(aisle.get("row_from")) == row_number if relation == "right" else False
            if y_overlap and relation and (confirmed or aisle.get("row_from") is None and aisle.get("row_to") is None): candidates.append((aisle, relation))
        side_value = cell.get("side", rows[row_number].get("side")); wanted = _side(side_value)
        if wanted == "unsupported": ambiguous.append({"cell_key": key, "reason_code": "unsupported_side_value"}); continue
        if wanted:
            candidates = [x for x in candidates if x[1] == wanted]
            if not candidates: ambiguous.append({"cell_key": key, "reason_code": "side_does_not_match_adjacent_aisle"}); continue
        if not candidates: inaccessible.append({"cell_key": key, "reason_code": "no_adjacent_longitudinal_aisle"}); continue
        if len(candidates) != 1: ambiguous.append({"cell_key": key, "reason_code": "multiple_adjacent_longitudinal_aisles"}); continue
        aisle, access_side = candidates[0]; point = (aisle["x_center"], rect["y_center"])
        event(point, [aisle["region_key"]], "cell_access", cell=key)
        links.append({"cell_key": key, "row_number": cell.get("row_number"), "cell_number": cell.get("cell_number"), "tier": cell.get("tier"), "storage_type": cell.get("storage_type", cell.get("row_storage_type")), "longitudinal_region_key": aisle["region_key"], "access_side": access_side, "x": point[0], "y": point[1], "physical_index": cell.get("physical_index"), "status": "mapped"})

    invalid_gates: list[dict[str, Any]] = []; gate_links: list[dict[str, Any]] = []
    raw_gates = gate_state.get("gates", []) if isinstance(gate_state, Mapping) and isinstance(gate_state.get("gates", []), list) else []
    gate_counts = Counter(str(g.get("gate_key")) for g in raw_gates if isinstance(g, Mapping) and g.get("gate_key") not in (None, ""))
    model_mismatch = isinstance(gate_state, Mapping) and gate_state.get("model_id") is not None and source.get("model_id") is not None and str(gate_state["model_id"]) != str(source["model_id"])
    for gate in sorted(raw_gates, key=_canonical):
        if not isinstance(gate, Mapping): invalid_gates.append({"reason_code": "invalid_gate_record"}); continue
        key = str(gate.get("gate_key") or "")
        if not key or gate_counts[key] > 1: invalid_gates.append({"gate_key": key or None, "reason_code": "duplicate_gate_key" if key else "invalid_gate_key"}); continue
        if model_mismatch: invalid_gates.append({"gate_key": key, "reason_code": "model_id_mismatch"}); continue
        try: x, y = _number(gate["x"]), _number(gate["y"])
        except (KeyError, TypeError, ValueError, OverflowError): invalid_gates.append({"gate_key": key, "reason_code": "invalid_gate_coordinates"}); continue
        road_type = str(gate.get("road_type", "")).lower()
        roads = [r for r in regions if r["region_type"] in {"top_road", "bottom_road", "other_road"} and r.get("road_type") == road_type and r["x_min"] - COORDINATE_TOLERANCE_M <= x <= r["x_max"] + COORDINATE_TOLERANCE_M and r["y_min"] - COORDINATE_TOLERANCE_M <= y <= r["y_max"] + COORDINATE_TOLERANCE_M]
        if len(roads) != 1: invalid_gates.append({"gate_key": key, "reason_code": "gate_not_on_matching_road"}); continue
        road = roads[0]; event((x, y), [road["region_key"]], "gate", gate=key); event((x, road["y_center"]), [road["region_key"]], "corridor_portal")
        if y != road["y_center"]: portal_segments.append(((x, y), (x, road["y_center"]), (road["region_key"],)))
        gate_links.append({"gate_key": key, "gate_name": gate.get("gate_name"), "road_region_key": road["region_key"], "road_type": road_type, "x": x, "y": y, "status": "mapped"})

    nodes: list[dict[str, Any]] = []; coordinate_node: dict[tuple[float, float], str] = {}
    priority = ("gate", "cell_access", "corridor_junction", "corridor_portal", "corridor_endpoint")
    for (x, y), data in sorted(events.items()):
        node_type = next(k for k in priority if k in data["kinds"])
        identity = {"node_type": node_type, "x": x, "y": y, "source_region_keys": sorted(data["keys"]), "cell_key": sorted(data["cells"]) or None, "gate_key": sorted(data["gates"]) or None}
        node_id = _hash(identity); coordinate_node[(x, y)] = node_id
        nodes.append({"node_id": node_id, "node_type": node_type, "x": x, "y": y, "source_region_keys": sorted(data["keys"]), "physical": True, "traversable": True})
    for link in links: link["access_node_id"] = coordinate_node[(link["x"], link["y"])]
    for link in gate_links: link["gate_node_id"] = coordinate_node[(link["x"], link["y"])]

    edge_map: dict[tuple[str, str, str], dict[str, Any]] = {}; prevented = {"zero": 0, "duplicate": 0, "diagonal": 0}
    def edge(p: tuple[float, float], q: tuple[float, float], edge_type: str, keys: tuple[str, ...] | list[str]) -> None:
        if p == q: prevented["zero"] += 1; return
        if p[0] != q[0] and p[1] != q[1]: prevented["diagonal"] += 1; return
        a, b = sorted((coordinate_node[p], coordinate_node[q])); distance = _number(abs(p[0] - q[0]) + abs(p[1] - q[1])); source_keys = sorted(keys)
        identity = {"node_ids": [a, b], "edge_type": edge_type, "distance_m": distance, "source_region_keys": source_keys}; edge_id = _hash(identity)
        marker = (a, b, edge_type)
        if marker in edge_map: prevented["duplicate"] += 1; return
        edge_map[marker] = {"edge_id": edge_id, "from_node_id": a, "to_node_id": b, "edge_type": edge_type, "distance_m": distance, "bidirectional": True, "source_region_keys": source_keys, "axis": "horizontal" if p[1] == q[1] else "vertical", "physical": True, "traversable": True}
    for r in regions:
        points = [p for p in coordinate_node if (r["orientation"] == "horizontal" and p[1] == r["y_center"] and r["x_min"] <= p[0] <= r["x_max"]) or (r["orientation"] == "vertical" and p[0] == r["x_center"] and r["y_min"] <= p[1] <= r["y_max"])]
        points.sort(key=lambda p: p[0] if r["orientation"] == "horizontal" else p[1])
        for p, q in zip(points, points[1:]): edge(p, q, "corridor_segment", [r["region_key"]])
    for p, q, keys in portal_segments:
        kind = "gate_access" if any(events[z]["gates"] for z in (p, q)) else "corridor_portal"; edge(p, q, kind, keys)
    edges = sorted(edge_map.values(), key=lambda x: (x["from_node_id"], x["to_node_id"], x["edge_type"], x["edge_id"]))
    nodes.sort(key=lambda x: (x["x"], x["y"], x["node_type"], x["node_id"])); links.sort(key=lambda x: (x.get("physical_index") if isinstance(x.get("physical_index"), (int, float)) else math.inf, str(x.get("row_number")), str(x.get("cell_number")), str(x.get("tier")), x["cell_key"])); gate_links.sort(key=lambda x: (x["gate_key"], x["gate_node_id"]))

    adjacency: dict[str, set[str]] = {n["node_id"]: set() for n in nodes}
    for e in edges: adjacency[e["from_node_id"]].add(e["to_node_id"]); adjacency[e["to_node_id"]].add(e["from_node_id"])
    components = []; remaining = set(adjacency); node_types = {n["node_id"]: n["node_type"] for n in nodes}
    while remaining:
        stack = [min(remaining)]; found = set()
        while stack:
            current = stack.pop()
            if current in found: continue
            found.add(current); stack.extend(sorted(adjacency[current] - found, reverse=True))
        remaining -= found; ids = sorted(found)
        components.append({"component_id": _hash(ids), "node_ids": ids, "node_count": len(ids), "edge_count": sum(e["from_node_id"] in found and e["to_node_id"] in found for e in edges), "cell_access_node_count": sum(node_types[x] == "cell_access" for x in found), "gate_node_count": sum(node_types[x] == "gate" for x in found)})
    components.sort(key=lambda x: x["component_id"])
    gate_components = {c["component_id"] for c in components if c["gate_node_count"]}; cells_outside = sum(c["cell_access_node_count"] for c in components if c["component_id"] not in gate_components)
    configuration_errors = int(model_mismatch)
    summary = {"walkable_regions": len(regions), "horizontal_regions": sum(r["orientation"] == "horizontal" for r in regions), "vertical_regions": sum(r["orientation"] == "vertical" for r in regions), "graph_nodes": len(nodes), "graph_edges": len(edges), "corridor_nodes": sum(n["node_type"].startswith("corridor_") for n in nodes), "cell_access_nodes": sum(n["node_type"] == "cell_access" for n in nodes), "gate_nodes": sum(n["node_type"] == "gate" for n in nodes), "physical_cells_total": len(raw_cells), "mapped_physical_cells": len(links), "inaccessible_physical_cells": len(inaccessible), "ambiguous_physical_cells": len(ambiguous), "gates_total": len(raw_gates), "gates_mapped": len(gate_links), "connected_components": len(components), "largest_component_nodes": max((c["node_count"] for c in components), default=0), "graph_has_gate": bool(gate_links), "graph_has_cell_access": bool(links), "graph_ready_for_replay": bool(gate_links and links and not cells_outside and not configuration_errors)}
    state = {"physical_graph_state_id": None, "model_id": source.get("model_id"), "source_file_hash": source.get("source_file_hash"), "coordinate_precision": COORDINATE_PRECISION, "coordinate_tolerance_m": COORDINATE_TOLERANCE_M, "walkable_regions": regions, "nodes": nodes, "edges": edges, "cell_access_links": links, "gate_links": gate_links, "inaccessible_cells": sorted(inaccessible, key=_canonical), "ambiguous_cell_access": sorted(ambiguous, key=_canonical), "invalid_walkable_regions": invalid_regions, "invalid_cells": sorted(invalid_cells, key=_canonical), "invalid_gates": sorted(invalid_gates, key=_canonical), "connected_components": components, "limitations": LIMITATIONS.copy(), "summary": summary}
    identity = {"model_id": state["model_id"], "source_file_hash": state["source_file_hash"], "coordinate_precision": COORDINATE_PRECISION, "coordinate_tolerance_m": COORDINATE_TOLERANCE_M, "region_keys": sorted(r["region_key"] for r in regions), "node_ids": sorted(n["node_id"] for n in nodes), "edge_ids": sorted(e["edge_id"] for e in edges), "cell_access_links": links, "gate_links": gate_links, "invalid_records": [state[k] for k in ("invalid_walkable_regions", "invalid_cells", "invalid_gates")]}; state["physical_graph_state_id"] = _hash(identity)
    diagnostics.update({"valid_roads": sum(r["region_type"].endswith("road") for r in regions), "invalid_roads": sum(x["collection"] in {"roads", "top_road", "bottom_road", "other_road"} for x in invalid_regions), "valid_longitudinal_aisles": sum(r["region_type"] == "longitudinal_aisle" for r in regions), "invalid_longitudinal_aisles": sum(x["collection"] in {"aisles", "longitudinal_aisle"} for x in invalid_regions), "valid_cross_aisles": sum(r["region_type"] == "cross_aisle" for r in regions), "invalid_cross_aisles": sum(x["collection"] in {"cross_aisles", "cross_aisle"} for x in invalid_regions), "walkable_regions": len(regions), "corridor_nodes": summary["corridor_nodes"], "corridor_edges": sum(e["edge_type"].startswith("corridor_") for e in edges), "corridor_junctions": sum(n["node_type"] == "corridor_junction" for n in nodes), "corridor_portals": sum(n["node_type"] == "corridor_portal" for n in nodes), "source_model_cells": len(source.get("cells", [])), "valid_model_cells": len(raw_cells) - len(invalid_cells), "invalid_model_cells": len(invalid_cells), "duplicate_cell_keys": sum(n > 1 for n in cell_counts.values()), "mapped_cell_access": len(links), "inaccessible_cells": len(inaccessible), "ambiguous_cell_access": len(ambiguous), "source_gates": len(raw_gates), "valid_gates": len(gate_links), "invalid_gates": len(invalid_gates), "duplicate_gate_keys": sum(n > 1 for n in gate_counts.values()), "gate_links": len(gate_links), "graph_nodes": len(nodes), "graph_edges": len(edges), "connected_components": len(components), "isolated_nodes": sum(not v for v in adjacency.values()), "cells_outside_gate_components": cells_outside, "zero_length_edges_prevented": prevented["zero"], "duplicate_edges_prevented": prevented["duplicate"], "diagonal_edges_prevented": prevented["diagonal"], "configuration_errors": configuration_errors})
    return state, diagnostics


def find_shortest_path(physical_graph_state: dict[str, Any], start_node_id: str, end_node_id: str) -> dict[str, Any]:
    """Return a deterministic Dijkstra shortest path."""
    node_ids = {n.get("node_id") for n in physical_graph_state.get("nodes", []) if isinstance(n, Mapping)}
    base = {"start_node_id": start_node_id, "end_node_id": end_node_id, "reachable": False, "distance_m": None, "path_node_ids": [], "path_edge_ids": [], "visited_nodes": 0, "reason_code": None}
    if start_node_id not in node_ids: return base | {"reason_code": "start_node_not_found"}
    if end_node_id not in node_ids: return base | {"reason_code": "end_node_not_found"}
    if start_node_id == end_node_id: return base | {"reachable": True, "distance_m": 0.0, "path_node_ids": [start_node_id]}
    adjacency: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
    for e in physical_graph_state.get("edges", []):
        if not isinstance(e, Mapping) or not e.get("traversable", True): continue
        a, b, edge_id, distance = e.get("from_node_id"), e.get("to_node_id"), e.get("edge_id"), e.get("distance_m")
        if a in node_ids and b in node_ids and isinstance(distance, (int, float)) and math.isfinite(distance) and distance >= 0:
            adjacency[a].append((b, float(distance), edge_id)); adjacency[b].append((a, float(distance), edge_id))
    heap = [(0.0, (start_node_id,), (), start_node_id)]; best: dict[str, tuple[float, tuple[str, ...], tuple[str, ...]]] = {}; visited = 0
    while heap:
        distance, path_nodes, path_edges, current = heapq.heappop(heap)
        candidate = (distance, path_nodes, path_edges)
        if current in best and best[current] <= candidate: continue
        best[current] = candidate; visited += 1
        if current == end_node_id:
            return base | {"reachable": True, "distance_m": _number(distance), "path_node_ids": list(path_nodes), "path_edge_ids": list(path_edges), "visited_nodes": visited}
        for neighbor, weight, edge_id in sorted(adjacency[current], key=lambda x: (x[0], x[2])):
            heapq.heappush(heap, (distance + weight, path_nodes + (neighbor,), path_edges + (edge_id,), neighbor))
    return base | {"visited_nodes": visited, "reason_code": "unreachable"}
