"""Replay one canonical outbound-demand set on two SimulationState snapshots."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from warehouse_physical_graph import build_physical_warehouse_graph, find_shortest_path
from warehouse_placement_zones import DEFAULT_PLACEMENT_ZONE_ORDER

REPLAY_VERSION = 1
PICK_POLICY = "zone_then_nearest_access_node_v1"
LIMITATIONS = [
    "deep_lane_internal_access_not_modeled",
    "dynamic_passage_opening_not_modeled",
    "intermediate_pallet_return_not_modeled",
    "opening_stock_only_no_receipts",
    "static_physical_graph_independent_of_occupancy",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _quantity(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _display_quantity(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _demand_quantity(demand: Mapping[str, Any]) -> float:
    return _quantity(demand.get("requested_boxes", demand.get("requested_units")))


def _build_working_stock(
    state: Mapping[str, Any], valid_cells: set[str], access_by_cell: Mapping[str, str],
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    stock: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    diagnostics: dict[str, Any] = {
        "unknown_location_stock": [], "invalid_stock_lots": [], "unmapped_cell_stock": [],
        "unknown_location_boxes": 0, "unmapped_cell_boxes": 0,
    }
    for lot in state.get("stock_lots", []) or []:
        if not isinstance(lot, Mapping):
            diagnostics["invalid_stock_lots"].append({"reason": "invalid_stock_lot"})
            continue
        qty = _quantity(lot.get("qty_boxes"))
        if not qty:
            continue
        sku, cell = str(lot.get("sku_key") or "").strip(), str(lot.get("cell_key") or "").strip()
        if lot.get("location_status") != "located":
            diagnostics["unknown_location_stock"].append({"stock_lot_id": lot.get("stock_lot_id"), "sku_key": sku or None, "qty_boxes": _display_quantity(qty)})
            diagnostics["unknown_location_boxes"] += qty
        elif not sku or not cell or cell not in valid_cells:
            diagnostics["invalid_stock_lots"].append({"stock_lot_id": lot.get("stock_lot_id"), "sku_key": sku or None, "cell_key": cell or None, "qty_boxes": _display_quantity(qty), "reason": "invalid_sku_or_cell"})
        elif cell not in access_by_cell:
            diagnostics["unmapped_cell_stock"].append({"stock_lot_id": lot.get("stock_lot_id"), "sku_key": sku, "cell_key": cell, "qty_boxes": _display_quantity(qty), "reason": "unmapped_cell_stock"})
            diagnostics["unmapped_cell_boxes"] += qty
        else:
            stock[sku][cell] += qty
    diagnostics["unknown_location_boxes"] = _display_quantity(float(diagnostics["unknown_location_boxes"]))
    diagnostics["unmapped_cell_boxes"] = _display_quantity(float(diagnostics["unmapped_cell_boxes"]))
    return {sku: dict(cells) for sku, cells in stock.items()}, diagnostics


def _replay_scenario(
    state: Mapping[str, Any], orders: list[Any], graph: dict[str, Any], gate_node: str,
    access_by_cell: Mapping[str, str], zone_by_cell: Mapping[str, str], zone_rank: Mapping[str, int],
    path_cache: dict[tuple[str, str], dict[str, Any]], valid_cells: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    stock, diagnostics = _build_working_stock(state, valid_cells, access_by_cell)
    initial = sum(qty for cells in stock.values() for qty in cells.values())

    def path(start: str, end: str) -> dict[str, Any]:
        key = (start, end)
        if key not in path_cache:
            path_cache[key] = find_shortest_path(graph, start, end)
        return path_cache[key]

    results: list[dict[str, Any]] = []
    total_picked = 0.0
    for raw_order in orders:
        order = raw_order if isinstance(raw_order, Mapping) else {}
        current_node, distance, route_valid = gate_node, 0.0, True
        events: list[dict[str, Any]] = []
        legs: list[dict[str, Any]] = []
        demand_results: list[dict[str, Any]] = []
        for raw_demand in order.get("demands", []) or []:
            demand = raw_demand if isinstance(raw_demand, Mapping) else {}
            sku, requested = str(demand.get("sku_key") or "").strip(), _demand_quantity(demand)
            remaining, demand_picks = requested, []
            while remaining > 0:
                candidates = []
                for cell, available in stock.get(sku, {}).items():
                    if available <= 0:
                        continue
                    candidate_path = path(current_node, access_by_cell[cell])
                    if candidate_path.get("reachable"):
                        candidates.append((zone_rank.get(zone_by_cell.get(cell, ""), len(zone_rank)),
                                           float(candidate_path["distance_m"]), cell, access_by_cell[cell], candidate_path))
                if not candidates:
                    break
                _, leg_distance, cell, node, selected_path = min(candidates, key=lambda item: item[:4])
                picked = min(remaining, stock[sku][cell])
                stock[sku][cell] -= picked
                remaining -= picked
                total_picked += picked
                leg = {"from_node_id": current_node, "to_node_id": node, "from_kind": "gate" if current_node == gate_node else "pick", "to_kind": "pick", "distance_m": leg_distance, "path_node_ids": selected_path["path_node_ids"], "path_edge_ids": selected_path["path_edge_ids"]}
                legs.append(leg); distance += leg_distance; current_node = node
                event = {"demand_key": demand.get("demand_key"), "sku_key": sku, "cell_key": cell, "access_node_id": node, "picked_boxes": _display_quantity(picked), "remaining_demand_boxes": _display_quantity(remaining)}
                events.append(event); demand_picks.append(event)
            picked_for_demand = requested - remaining
            demand_results.append({"demand_key": demand.get("demand_key"), "sku_key": sku,
                                   "requested_boxes": _display_quantity(requested),
                                   "picked_boxes": _display_quantity(picked_for_demand),
                                   "shortage_boxes": _display_quantity(remaining),
                                   "split": len(demand_picks) > 1, "pick_events": demand_picks})
        if events:
            return_path = path(current_node, gate_node)
            if return_path.get("reachable"):
                return_distance = float(return_path["distance_m"])
                legs.append({"from_node_id": current_node, "to_node_id": gate_node, "from_kind": "pick", "to_kind": "gate", "distance_m": return_distance, "path_node_ids": return_path["path_node_ids"], "path_edge_ids": return_path["path_edge_ids"]})
                distance += return_distance
            else:
                route_valid = False
        requested_total = sum(float(item["requested_boxes"]) for item in demand_results)
        picked_total = sum(float(item["picked_boxes"]) for item in demand_results)
        shortage = requested_total - picked_total
        status = "invalid_route" if not route_valid else "fulfilled" if not shortage else "partial" if picked_total else "shortage"
        results.append({"order_key": order.get("order_key"), "outbound_order_number": order.get("outbound_order_number"),
                        "created_at": order.get("created_at"), "requested_boxes": _display_quantity(requested_total),
                        "picked_boxes": _display_quantity(picked_total), "shortage_boxes": _display_quantity(shortage),
                        "route_distance_m": round(distance, 6) if route_valid else None, "demands": demand_results,
                        "pick_events": events, "route_legs": legs, "returned_to_gate": bool(route_valid and (not events or legs[-1]["to_kind"] == "gate")), "status": status})
    final = sum(qty for cells in stock.values() for qty in cells.values())
    scenario = {"simulation_state_id": state.get("simulation_state_id"), "orders": results,
                "summary": {"orders": len(results), "initial_boxes": _display_quantity(initial),
                            "picked_boxes": _display_quantity(total_picked), "final_boxes": _display_quantity(final),
                            "route_distance_m": round(sum(float(o["route_distance_m"] or 0) for o in results), 6),
                            "conservation_valid": math.isclose(initial - total_picked, final, abs_tol=1e-9)}}
    return scenario, diagnostics


def replay_outbound_on_simulation_states(
    model: dict[str, Any], current_state: dict[str, Any], proposed_state: dict[str, Any],
    outbound_demand_state: dict[str, Any], gate_state: dict[str, Any], *,
    zone_order: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay orders independently, using one graph, gate, policy, and demand sequence."""
    graph, graph_diagnostics = build_physical_warehouse_graph(model, gate_state)
    gate_links = graph.get("gate_links", [])
    diagnostics: dict[str, Any] = {"configuration_errors": [], "physical_graph": graph_diagnostics}
    if len(gate_links) != 1:
        diagnostics["configuration_errors"].append("exactly_one_mapped_gate_required")
    orders = outbound_demand_state.get("orders", []) if isinstance(outbound_demand_state, Mapping) else []
    if not isinstance(orders, list):
        diagnostics["configuration_errors"].append("invalid_outbound_demand_orders")
        orders = []
    zones = list(zone_order) if zone_order is not None else list(DEFAULT_PLACEMENT_ZONE_ORDER)
    if diagnostics["configuration_errors"]:
        return {}, diagnostics
    gate = gate_links[0]
    access = {str(link["cell_key"]): str(link["access_node_id"]) for link in graph.get("cell_access_links", [])}
    model_cells = {str(cell.get("cell_key")): cell for cell in model.get("cells", []) if isinstance(cell, Mapping) and cell.get("cell_key") not in (None, "")}
    zone_by_cell = {key: str(cell.get("weight_zone") or cell.get("placement_zone") or "") for key, cell in model_cells.items()}
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    args = (orders, graph, str(gate["gate_node_id"]), access, zone_by_cell,
            {zone: index for index, zone in enumerate(zones)}, cache, set(model_cells))
    current, current_diagnostics = _replay_scenario(current_state, *args)
    proposed, proposed_diagnostics = _replay_scenario(proposed_state, *args)
    demand_id = outbound_demand_state.get("outbound_demand_state_id") or _hash(outbound_demand_state)
    identity = {"version": REPLAY_VERSION, "model_id": model.get("model_id"),
                "current_state_id": current_state.get("simulation_state_id"),
                "proposed_state_id": proposed_state.get("simulation_state_id"),
                "outbound_demand_state_id": demand_id, "physical_graph_state_id": graph.get("physical_graph_state_id"),
                "gate_key": gate.get("gate_key"), "pick_policy": PICK_POLICY, "zone_order": zones,
                "current": current, "proposed": proposed}
    replay = {"simulation_outbound_replay_version": REPLAY_VERSION,
              "simulation_outbound_replay_id": _hash(identity), **identity,
              "shared_controls": {"same_outbound_demand_set": True, "same_physical_graph": True,
                                  "same_gate": True, "same_pick_policy": True},
              "summary": {"orders": len(orders), "current_distance_m": current["summary"]["route_distance_m"],
                          "proposed_distance_m": proposed["summary"]["route_distance_m"]},
              "limitations": LIMITATIONS.copy()}
    diagnostics.update({"current": current_diagnostics, "proposed": proposed_diagnostics,
                        "shortest_path_cache_entries": len(cache)})
    return replay, diagnostics
