"""Deterministic outbound replay with optional PROPOSED picking replenishment."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from warehouse_physical_graph import build_physical_warehouse_graph, find_shortest_path
from warehouse_placement_rules import get_enabled_rule_ids, validate_placement_rule_set
from warehouse_placement_zones import DEFAULT_PLACEMENT_ZONE_ORDER

REPLAY_VERSION = 2
PICK_POLICY = "zone_then_nearest_access_node_v1"
LIMITATIONS = [
    "deep_lane_internal_access_not_modeled", "dynamic_passage_opening_not_modeled",
    "intermediate_pallet_return_not_modeled", "opening_stock_only_no_receipts",
    "static_physical_graph_independent_of_occupancy",
    "replenishment_distance_is_loaded_one_way_transfer_only",
    "replenishment_empty_and_return_travel_not_modeled",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _quantity(value: Any) -> float:
    if isinstance(value, bool): return 0.0
    try: number = float(value)
    except (TypeError, ValueError): return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _display(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _enabled(rule_set: Mapping[str, Any] | None) -> set[str]:
    if rule_set is None: return set()
    validation = validate_placement_rule_set(dict(rule_set))
    if not validation["valid"]: raise ValueError(f"invalid placement_rule_set: {validation['errors']}")
    return set(get_enabled_rule_ids(dict(rule_set)))


def _replay_scenario(state: Mapping[str, Any], orders: list[Any], graph: dict[str, Any], gate_node: str,
                     access: Mapping[str, str], cells: Mapping[str, Mapping[str, Any]], zone_rank: Mapping[str, int],
                     cache: dict[tuple[str, str], dict[str, Any]], enabled: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    working = copy.deepcopy(dict(state))
    lots = [lot for lot in working.get("stock_lots", []) or [] if isinstance(lot, dict)]
    pallets = {str(p.get("pallet_unit_id")): p for p in working.get("pallet_units", []) or [] if isinstance(p, dict) and p.get("pallet_unit_id") is not None}
    positions = {str(p.get("position_id")): p for p in working.get("physical_positions", []) or [] if isinstance(p, dict) and p.get("position_id") is not None}
    diagnostics: dict[str, Any] = {"unknown_location_stock": [], "invalid_stock_lots": [], "unmapped_cell_stock": [],
                                   "unknown_location_boxes": 0, "unmapped_cell_boxes": 0,
                                   "replenishment_fallbacks": []}
    usable: list[dict[str, Any]] = []
    for lot in lots:
        qty = _quantity(lot.get("qty_boxes")); sku = str(lot.get("sku_key") or "").strip(); cell = str(lot.get("cell_key") or "").strip()
        if not qty: continue
        if lot.get("location_status") != "located":
            diagnostics["unknown_location_stock"].append({"stock_lot_id": lot.get("stock_lot_id"), "sku_key": sku or None, "qty_boxes": _display(qty)})
            diagnostics["unknown_location_boxes"] += qty
        elif not sku or cell not in cells:
            diagnostics["invalid_stock_lots"].append({"stock_lot_id": lot.get("stock_lot_id"), "sku_key": sku or None, "cell_key": cell or None, "qty_boxes": _display(qty), "reason": "invalid_sku_or_cell"})
        elif cell not in access:
            diagnostics["unmapped_cell_stock"].append({"stock_lot_id": lot.get("stock_lot_id"), "sku_key": sku, "cell_key": cell, "qty_boxes": _display(qty), "reason": "unmapped_cell_stock"})
            diagnostics["unmapped_cell_boxes"] += qty
        else: usable.append(lot)
    initial = sum(_quantity(l.get("qty_boxes")) for l in usable)
    picking_storage = "picking_storage" in enabled
    replenishment = picking_storage and "replenishment" in enabled
    replenishment_events: list[dict[str, Any]] = []
    replenishment_distance = 0.0

    def path(a: str, b: str) -> dict[str, Any]:
        if (a, b) not in cache: cache[(a, b)] = find_shortest_path(graph, a, b)
        return cache[(a, b)]

    def role(lot: Mapping[str, Any]) -> str:
        return str(lot.get("location_role") or pallets.get(str(lot.get("pallet_unit_id")), {}).get("placement_role") or "unassigned")

    def candidates(sku: str, node: str, *, role_filter: str | None = None) -> list[tuple[Any, ...]]:
        result = []
        for lot in usable:
            if lot.get("sku_key") != sku or _quantity(lot.get("qty_boxes")) <= 0 or role_filter and role(lot) != role_filter: continue
            cell = str(lot["cell_key"]); route = path(node, access[cell])
            if route.get("reachable"):
                zone = str(cells[cell].get("weight_zone") or cells[cell].get("placement_zone") or "")
                result.append((zone_rank.get(zone, len(zone_rank)), float(route["distance_m"]), cell,
                               str(lot.get("position_id") or ""), str(lot.get("stock_lot_id") or ""), lot, route))
        return sorted(result, key=lambda x: x[:5])

    def replenish(sku: str, demand: Mapping[str, Any], order: Mapping[str, Any]) -> bool:
        nonlocal replenishment_distance
        depleted = [l for l in usable if l.get("sku_key") == sku and role(l) == "picking" and _quantity(l.get("qty_boxes")) == 0]
        depleted.sort(key=lambda l: (str(l.get("position_id") or ""), str(l.get("stock_lot_id") or "")))
        if not depleted: return False
        target_lot = depleted[0]; target_pos = str(target_lot.get("position_id") or ""); target_cell = str(target_lot.get("cell_key") or "")
        destination_ok = bool(target_pos and target_pos in positions and target_cell in access and
                              positions[target_pos].get("cell_key") == target_cell and
                              (cells[target_cell].get("storage_type") or cells[target_cell].get("row_storage_type") or "normal") != "deep_lane" and
                              cells[target_cell].get("capacity_pallets", 1) == 1)
        eligible = []
        for lot in usable:
            if lot.get("sku_key") != sku or role(lot) != "storage" or _quantity(lot.get("qty_boxes")) <= 0: continue
            pallet = pallets.get(str(lot.get("pallet_unit_id"))); source_pos = str(lot.get("position_id") or ""); source_cell = str(lot.get("cell_key") or "")
            source_model = cells.get(source_cell, {})
            linked = [l for l in lots if str(l.get("pallet_unit_id")) == str(lot.get("pallet_unit_id"))]
            contract = bool(destination_ok and pallet and len(linked) == 1 and pallet.get("physical_status") == "active" and
                            pallet.get("location_status") == "located" and str(pallet.get("position_id") or "") == source_pos and
                            str(pallet.get("cell_key") or "") == source_cell and source_pos in positions and
                            positions[source_pos].get("cell_key") == source_cell and
                            (source_model.get("storage_type") or source_model.get("row_storage_type") or "normal") != "deep_lane" and
                            source_model.get("capacity_pallets", 1) == 1 and
                            math.isclose(_quantity(pallet.get("remaining_boxes")), _quantity(lot.get("qty_boxes")), abs_tol=1e-9))
            transfer = path(access[source_cell], access[target_cell]) if contract and source_cell in access else {}
            if contract and transfer.get("reachable"):
                physical_order = (source_model.get("physical_index", 10**12), source_pos)
                eligible.append((float(transfer["distance_m"]), physical_order, str(pallet.get("pallet_unit_id")), lot, pallet, transfer))
        if not eligible: return False
        distance, _, pallet_id, source_lot, pallet, transfer = min(eligible, key=lambda x: x[:3])
        source_pos, source_cell = str(source_lot["position_id"]), str(source_lot["cell_key"])
        boxes = _quantity(pallet["remaining_boxes"])
        source_lot.update(cell_key=target_cell, position_id=target_pos, location_status="located", location_role="picking")
        pallet.update(cell_key=target_cell, position_id=target_pos, location_status="located", placement_role="picking")
        positions[source_pos]["status"] = "free"; positions[target_pos]["status"] = "occupied"
        event = {"sku_key": sku, "pallet_unit_id": pallet_id, "source_position_id": source_pos, "source_cell_key": source_cell,
                 "target_position_id": target_pos, "target_cell_key": target_cell, "boxes": _display(boxes),
                 "replenishment_distance_m": round(distance, 6), "order_key": order.get("order_key"),
                 "demand_key": demand.get("demand_key")}
        replenishment_events.append(event); replenishment_distance += distance
        return True

    results = []; total_picked = 0.0
    for raw_order in orders:
        order = raw_order if isinstance(raw_order, Mapping) else {}; current_node = gate_node; picker_distance = 0.0; valid = True
        events = []; legs = []; demand_results = []
        for raw_demand in order.get("demands", []) or []:
            demand = raw_demand if isinstance(raw_demand, Mapping) else {}; sku = str(demand.get("sku_key") or "").strip()
            requested = _quantity(demand.get("requested_boxes", demand.get("requested_units"))); remaining = requested; picks = []
            fallback_recorded = False
            while remaining > 0:
                selection_role = "picking" if picking_storage else None
                choice = candidates(sku, current_node, role_filter=selection_role)
                if not choice and replenishment and replenish(sku, demand, order):
                    selection_role = "picking"
                    choice = candidates(sku, current_node, role_filter="picking")
                if not choice and picking_storage:
                    direct = candidates(sku, current_node)
                    if replenishment and direct and not fallback_recorded:
                        diagnostic = {"code": "unsupported_replenishment_direct_pick_fallback", "sku_key": sku,
                                      "order_key": order.get("order_key"), "demand_key": demand.get("demand_key"),
                                      "remaining_demand_boxes": _display(remaining)}
                        diagnostics["replenishment_fallbacks"].append(diagnostic); fallback_recorded = True
                    choice = direct; selection_role = None
                if not choice: break
                _, leg_distance, cell, _, _, lot, selected = choice[0]
                cell_lots = sorted((item for item in usable if item.get("sku_key") == sku
                                    and item.get("cell_key") == cell and _quantity(item.get("qty_boxes")) > 0
                                    and (selection_role is None or role(item) == selection_role)),
                                   key=lambda item: str(item.get("stock_lot_id") or ""))
                picked = min(remaining, sum(_quantity(item.get("qty_boxes")) for item in cell_lots))
                to_consume = picked
                for consumed_lot in cell_lots:
                    amount = min(to_consume, _quantity(consumed_lot.get("qty_boxes")))
                    consumed_lot["qty_boxes"] = _quantity(consumed_lot.get("qty_boxes")) - amount
                    pallet = pallets.get(str(consumed_lot.get("pallet_unit_id")))
                    if pallet and math.isclose(_quantity(pallet.get("remaining_boxes")), amount + _quantity(consumed_lot.get("qty_boxes")), abs_tol=1e-9):
                        pallet["remaining_boxes"] = _quantity(consumed_lot.get("qty_boxes"))
                        if not pallet["remaining_boxes"]:
                            pallet.update(physical_status="depleted", location_status="unassigned",
                                          position_id=None, cell_key=None)
                    to_consume -= amount
                    if to_consume <= 0: break
                remaining -= picked; total_picked += picked; picker_distance += leg_distance
                node = access[cell]; legs.append({"from_node_id": current_node, "to_node_id": node, "from_kind": "gate" if current_node == gate_node else "pick", "to_kind": "pick", "distance_m": leg_distance, "path_node_ids": selected["path_node_ids"], "path_edge_ids": selected["path_edge_ids"]}); current_node = node
                event = {"demand_key": demand.get("demand_key"), "sku_key": sku, "cell_key": cell, "access_node_id": node, "picked_boxes": _display(picked), "remaining_demand_boxes": _display(remaining)}
                events.append(event); picks.append(event)
            demand_results.append({"demand_key": demand.get("demand_key"), "sku_key": sku, "requested_boxes": _display(requested), "picked_boxes": _display(requested-remaining), "shortage_boxes": _display(remaining), "split": len(picks)>1, "pick_events": picks})
        if events:
            back = path(current_node, gate_node)
            if back.get("reachable"):
                d = float(back["distance_m"]); picker_distance += d; legs.append({"from_node_id": current_node, "to_node_id": gate_node, "from_kind": "pick", "to_kind": "gate", "distance_m": d, "path_node_ids": back["path_node_ids"], "path_edge_ids": back["path_edge_ids"]})
            else: valid = False
        requested_total = sum(float(x["requested_boxes"]) for x in demand_results); picked_total = sum(float(x["picked_boxes"]) for x in demand_results); shortage = requested_total-picked_total
        results.append({"order_key": order.get("order_key"), "outbound_order_number": order.get("outbound_order_number"), "created_at": order.get("created_at"), "requested_boxes": _display(requested_total), "picked_boxes": _display(picked_total), "shortage_boxes": _display(shortage), "route_distance_m": round(picker_distance,6) if valid else None, "picker_distance_m": round(picker_distance,6) if valid else None, "demands": demand_results, "pick_events": events, "route_legs": legs, "returned_to_gate": bool(valid and (not events or legs[-1]["to_kind"]=="gate")), "status": "invalid_route" if not valid else "fulfilled" if not shortage else "partial" if picked_total else "shortage"})
    final = sum(_quantity(l.get("qty_boxes")) for l in usable)
    picker_total = round(sum(float(o["picker_distance_m"] or 0) for o in results), 6); replenishment_total = round(replenishment_distance, 6)
    scenario = {"simulation_state_id": state.get("simulation_state_id"), "orders": results, "replenishment_events": replenishment_events,
                "working_state": working, "summary": {"orders": len(results), "initial_boxes": _display(initial), "picked_boxes": _display(total_picked), "final_boxes": _display(final), "route_distance_m": picker_total, "picker_distance_m": picker_total, "replenishment_distance_m": replenishment_total, "total_movement_distance_m": round(picker_total+replenishment_total,6), "replenishment_event_count": len(replenishment_events), "replenishment_fallback_count": len(diagnostics["replenishment_fallbacks"]), "replenishment_modeled_coverage_percent": 100.0 * len(replenishment_events)/(len(replenishment_events)+len(diagnostics["replenishment_fallbacks"])) if replenishment_events or diagnostics["replenishment_fallbacks"] else 100.0, "conservation_valid": math.isclose(initial-total_picked,final,abs_tol=1e-9)}}
    diagnostics["unknown_location_boxes"] = _display(float(diagnostics["unknown_location_boxes"])); diagnostics["unmapped_cell_boxes"] = _display(float(diagnostics["unmapped_cell_boxes"]))
    return scenario, diagnostics


def replay_outbound_on_simulation_states(model: dict[str, Any], current_state: dict[str, Any], proposed_state: dict[str, Any], outbound_demand_state: dict[str, Any], gate_state: dict[str, Any], *, zone_order: list[str] | None = None, placement_rule_set: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay CURRENT without rules and PROPOSED with an explicit optional RuleSet."""
    graph, graph_diagnostics = build_physical_warehouse_graph(model, gate_state); links = graph.get("gate_links", [])
    diagnostics: dict[str, Any] = {"configuration_errors": [], "physical_graph": graph_diagnostics}
    try: proposed_enabled = _enabled(placement_rule_set)
    except ValueError as error: diagnostics["configuration_errors"].append(str(error)); proposed_enabled = set()
    orders = outbound_demand_state.get("orders", []) if isinstance(outbound_demand_state, Mapping) else []
    sequence_ready = (outbound_demand_state.get("readiness", {}).get("route_sequence_authoritative") is True
                      if isinstance(outbound_demand_state, Mapping) else False)
    if not sequence_ready:
        diagnostics["configuration_errors"].append("factual_pick_sequence_missing_or_invalid")
    if len(links) != 1: diagnostics["configuration_errors"].append("exactly_one_mapped_gate_required")
    if not isinstance(orders, list): diagnostics["configuration_errors"].append("invalid_outbound_demand_orders")
    if diagnostics["configuration_errors"]: return {}, diagnostics
    zones = list(zone_order) if zone_order is not None else list(DEFAULT_PLACEMENT_ZONE_ORDER)
    access = {str(x["cell_key"]): str(x["access_node_id"]) for x in graph.get("cell_access_links", [])}
    cells = {str(c.get("cell_key")): c for c in model.get("cells", []) if isinstance(c, Mapping) and c.get("cell_key") not in (None, "")}
    cache: dict[tuple[str,str],dict[str,Any]] = {}; args = (orders, graph, str(links[0]["gate_node_id"]), access, cells, {z:i for i,z in enumerate(zones)}, cache)
    current, cd = _replay_scenario(current_state, *args, set()); proposed, pd = _replay_scenario(proposed_state, *args, proposed_enabled)
    identity = {"version": REPLAY_VERSION, "model_id": model.get("model_id"), "current_state_id": current_state.get("simulation_state_id"), "proposed_state_id": proposed_state.get("simulation_state_id"), "outbound_demand_state_id": outbound_demand_state.get("outbound_demand_state_id") or _hash(outbound_demand_state), "physical_graph_state_id": graph.get("physical_graph_state_id"), "gate_key": links[0].get("gate_key"), "pick_policy": PICK_POLICY, "zone_order": zones, "proposed_placement_rule_set_id": placement_rule_set.get("placement_rule_set_id") if placement_rule_set else None, "current": current, "proposed": proposed}
    replay = {"simulation_outbound_replay_version": REPLAY_VERSION, "simulation_outbound_replay_id": _hash(identity), **identity, "shared_controls": {"same_outbound_demand_set": True, "same_physical_graph": True, "same_gate": True, "same_pick_policy": True, "current_rules_applied": False}, "summary": {"orders": len(orders), "current_distance_m": current["summary"]["picker_distance_m"], "proposed_distance_m": proposed["summary"]["picker_distance_m"], "current_picker_distance_m": current["summary"]["picker_distance_m"], "proposed_picker_distance_m": proposed["summary"]["picker_distance_m"], "proposed_replenishment_distance_m": proposed["summary"]["replenishment_distance_m"], "current_total_movement_distance_m": current["summary"]["total_movement_distance_m"], "proposed_total_movement_distance_m": proposed["summary"]["total_movement_distance_m"]}, "limitations": LIMITATIONS.copy()}
    diagnostics.update(current=cd, proposed=pd, shortest_path_cache_entries=len(cache)); return replay, diagnostics
