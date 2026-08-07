"""Deterministic sequential outbound replay for CURRENT and PROPOSED stock."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from warehouse_inventory_placement import cell_key as canonical_cell_key
from warehouse_physical_graph import find_shortest_path
from warehouse_pick_inventory import build_pickable_inventory_index
from warehouse_pick_working_stock import build_pick_working_stock


ZONES = {"heavy", "medium", "light", "fragile"}
BOX_UNITS = {"короб", "короба", "коробов"}
LIMITATIONS = [
    "replay_uses_same_opening_stock_for_both_scenarios",
    "replay_uses_same_outbound_demands_for_both_scenarios",
    "only_day_receipt_placement_differs_between_scenarios",
    "outbound_orders_are_processed_sequentially",
    "each_order_starts_and_ends_at_configured_gate",
    "zone_order_precedes_nearest_neighbor_choice",
    "nearest_neighbor_is_greedy_not_globally_optimal",
    "split_is_used_only_when_no_single_cell_can_cover_remaining_demand",
    "working_stock_is_isolated_per_scenario",
    "virtual_current_placements_route_through_parent_physical_cell",
    "virtual_slots_are_not_graph_nodes",
    "unmapped_or_unreachable_stock_is_not_pickable",
    "unresolved_receipt_batches_are_not_added_to_stock",
    "intermediate_pallet_capacity_returns_are_not_modeled",
    "no_local_route_improvement_is_applied",
    "replay_distances_are_raw_scenario_outputs_not_comparison_metrics",
    "comparison_and_savings_are_deferred_to_next_stage",
    "opening_stock_must_be_pre_filtered_to_target_warehouse_if_warehouse_is_absent",
    "replay_does_not_write_persisted_state",
    "replay_does_not_modify_warehouse_geometry",
]


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _normalized(value: Any) -> str:
    return _text(value).casefold().replace("ё", "е")


def _box(value: Any) -> bool:
    return _normalized(value) in BOX_UNITS


def _source_index(value: Any) -> int:
    values = value if isinstance(value, list) else []
    valid = [x for x in values if isinstance(x, int) and not isinstance(x, bool)]
    return min(valid, default=2**63 - 1)


def _cell_keys(model: Mapping[str, Any]) -> set[str]:
    result = set()
    for cell in model.get("cells", []) or []:
        if not isinstance(cell, Mapping):
            continue
        key = _text(cell.get("cell_key")) or canonical_cell_key(
            cell.get("row_number"), cell.get("cell_number"), cell.get("tier")
        )
        if key:
            result.add(key)
    return result


def _stock_id(stock: Mapping[str, Any]) -> str:
    records = []
    for key, record in sorted((stock.get("stock_by_key") or {}).items()):
        records.append({field: record.get(field) for field in (
            "stock_key", "sku_key", "cell_key", "normalized_unit_name",
            "initial_units", "remaining_units",
        )})
    return _hash(records)


def _physical_placements(model: Mapping[str, Any], opening: Mapping[str, Any],
                         receipt: Mapping[str, Any], scenario: str) -> tuple[dict[str, Any], dict[str, Any]]:
    cells = _cell_keys(model)
    placements = []
    invalid = []
    for source in opening.get("placements", []) or []:
        if isinstance(source, Mapping) and _text(source.get("cell_key")) in cells:
            placements.append(copy.deepcopy(dict(source)))
        else:
            invalid.append({"source": "opening_stock", "placement": copy.deepcopy(source)})
    for source in receipt.get("placements", []) or []:
        if not isinstance(source, Mapping):
            invalid.append({"source": "receipt", "placement": copy.deepcopy(source)})
            continue
        virtual = source.get("is_virtual") is True
        route = _text(source.get("route_physical_cell_key"))
        parent = _text(source.get("parent_physical_cell_key"))
        if scenario == "current":
            physical = route or ("" if virtual else _text(source.get("physical_cell_key")))
            valid = bool(physical in cells and (not virtual or (route and (not parent or parent == route))))
        else:
            physical = route or _text(source.get("physical_cell_key")) or _text(source.get("cell_key"))
            valid = not virtual and physical in cells
        if not valid:
            invalid.append({"source": "receipt", "placement": copy.deepcopy(dict(source))})
            continue
        item = copy.deepcopy(dict(source)); item["cell_key"] = physical
        placements.append(item)
    unresolved = copy.deepcopy(receipt.get("unresolved_receipt_batches", []) or [])
    unavailable = {
        "unresolved_batches": unresolved,
        "unresolved_qty_units": sum(x.get("qty_units", 0) for x in unresolved
                                    if isinstance(x, Mapping) and isinstance(x.get("qty_units", 0), int)
                                    and not isinstance(x.get("qty_units", 0), bool)),
        "invalid_batches": invalid,
    }
    return {"model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"),
            "placements": placements}, unavailable


def _empty_scenario(scenario: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    key = f"{scenario}_receipt_placement_state_id"
    summary = {k: 0 for k in (
        "orders_total", "orders_fully_fulfilled", "orders_with_shortage", "demands_total",
        "demands_fulfilled", "demands_with_shortage", "requested_units", "picked_units",
        "shortage_units", "pick_events", "split_demands", "route_legs", "return_legs",
        "initial_stock_units", "final_stock_units", "receipt_unresolved_batches",
        "receipt_unresolved_qty_units", "returned_orders_to_gate")}
    summary.update(route_distance_m=0.0, stock_conservation_ok=True, demand_conservation_ok=True)
    return {"scenario": scenario, "source_receipt_placement_state_id": receipt.get(key, ""),
            "initial_working_stock_id": "", "final_working_stock_id": "", "orders": [],
            "shortages": [], "final_working_stock": {}, "receipt_unavailable": {
                "unresolved_batches": [], "unresolved_qty_units": 0, "invalid_batches": []}, "summary": summary}


def _replay_one_scenario(scenario: str, receipt: Mapping[str, Any], model: Mapping[str, Any],
                         opening: Mapping[str, Any], orders: list[dict[str, Any]], demand_set_id: str,
                         graph: Mapping[str, Any], gate_key: str, gate_node: str,
                         zone_order: list[str], route_cache: dict[tuple[str, str], dict[str, Any]],
                         route_stats: dict[str, int]) -> tuple[dict[str, Any], dict[str, int]]:
    placement_state, unavailable = _physical_placements(model, opening, receipt, scenario)
    inventory = build_pickable_inventory_index(dict(model), placement_state)
    working = copy.deepcopy(build_pick_working_stock(inventory))
    initial_id = _stock_id(working)
    initial_units = sum(x["initial_units"] for x in working["stock_by_key"].values())
    nodes = {x.get("node_id") for x in graph.get("nodes", []) if isinstance(x, Mapping)}
    access: dict[str, list[str]] = {}
    for link in sorted(graph.get("cell_access_links", []) or [], key=lambda x: _hash(x)):
        if isinstance(link, Mapping) and link.get("status", "valid") in {"valid", "mapped", "ok", None, ""}:
            access.setdefault(_text(link.get("cell_key")), []).append(_text(link.get("access_node_id")))
    ranks = {zone: rank for rank, zone in enumerate(zone_order)}
    counters = {"unmapped": 0, "unreachable": 0}

    def path(start: str, end: str) -> dict[str, Any]:
        pair = (start, end)
        if pair in route_cache:
            route_stats["hits"] += 1
            return route_cache[pair]
        route_stats["calls"] += 1
        route_cache[pair] = find_shortest_path(dict(graph), start, end)
        return route_cache[pair]

    results = []
    for order in orders:
        demand_results = []
        for demand in order.get("demands", []) or []:
            requested = demand.get("requested_units")
            valid_qty = isinstance(requested, int) and not isinstance(requested, bool) and requested > 0
            reasons = []
            if not valid_qty: reasons.append("invalid_demand")
            if not _box(demand.get("unit_name")): reasons.append("unsupported_unit")
            demand_results.append({"demand_key": _text(demand.get("demand_key")), "sku_key": _text(demand.get("sku_key")),
                "requested_units": requested if valid_qty else 0, "picked_units": 0,
                "shortage_units": requested if valid_qty else 0, "unit_name": "короб" if _box(demand.get("unit_name")) else _text(demand.get("unit_name")),
                "pick_event_ids": [], "pick_count": 0, "split": False, "status": "invalid" if reasons else "shortage",
                "reasons": reasons, "_source_index": _source_index(demand.get("source_indexes")), "_stock_keys": set()})
        current = gate_node; picks = []; legs = []; route_invalid = False
        while True:
            pairs = []
            for demand in demand_results:
                remaining = demand["requested_units"] - demand["picked_units"]
                if remaining <= 0 or demand["reasons"]:
                    continue
                sku_keys = working["stock_keys_by_sku"].get(demand["sku_key"], [])
                if not sku_keys:
                    demand["reasons"].append("stock_not_found"); continue
                possible = []; saw_units = False; saw_zone = False; saw_map = False; saw_reachable = False
                for stock_key in sku_keys:
                    stock = working["stock_by_key"][stock_key]
                    if stock["remaining_units"] <= 0 or not _box(stock["normalized_unit_name"]): continue
                    saw_units = True
                    zone = _normalized(stock.get("zone"))
                    if zone not in ranks: continue
                    saw_zone = True
                    links = access.get(stock["cell_key"], [])
                    if len(links) != 1 or links[0] not in nodes: continue
                    saw_map = True
                    route = path(current, links[0])
                    if not route.get("reachable"): continue
                    saw_reachable = True
                    possible.append((stock, zone, links[0], route))
                if not possible:
                    if saw_units and not saw_zone: demand["reasons"].append("unsupported_stock_zone")
                    elif saw_zone and not saw_map: demand["reasons"].append("unmapped_cell_stock"); counters["unmapped"] += 1
                    elif saw_map and not saw_reachable: demand["reasons"].append("unreachable_cell_stock"); counters["unreachable"] += 1
                    elif saw_units: demand["reasons"].append("insufficient_stock")
                    continue
                full = [x for x in possible if x[0]["remaining_units"] >= remaining]
                for stock, zone, node, route in full or possible:
                    pairs.append((ranks[zone], float(route["distance_m"]), demand["_source_index"], demand["demand_key"],
                                  stock["cell_key"], stock["stock_key"], demand, stock, zone, node, route))
            if not pairs: break
            chosen = min(pairs, key=lambda x: x[:6]); demand, stock, zone, node, route = chosen[6:]
            remaining = demand["requested_units"] - demand["picked_units"]
            picked = min(remaining, stock["remaining_units"]); sequence = len(picks) + 1
            stock["remaining_units"] -= picked; demand["picked_units"] += picked; demand["_stock_keys"].add(stock["stock_key"])
            event_identity = {"scenario": scenario, "order_key": order.get("order_key"), "demand_key": demand["demand_key"],
                "stock_key": stock["stock_key"], "sequence_in_order": sequence, "picked_units": picked,
                "route_from_node_id": current, "route_to_node_id": node}
            event = event_identity | {"pick_event_id": _hash(event_identity), "outbound_order_number": order.get("outbound_order_number"),
                "sku_key": demand["sku_key"], "cell_key": stock["cell_key"], "access_node_id": node, "zone": zone,
                "unit_name": "короб", "route_distance_m": float(route["distance_m"]), "path_node_ids": route["path_node_ids"],
                "path_edge_ids": route["path_edge_ids"], "remaining_demand_units_after": remaining-picked,
                "remaining_stock_units_after": stock["remaining_units"]}
            picks.append(event); demand["pick_event_ids"].append(event["pick_event_id"])
            if current != node:
                leg_type = "gate_to_pick" if not legs else "pick_to_pick"
                identity = {"scenario": scenario, "order_key": order.get("order_key"), "leg_type": leg_type,
                    "from_node_id": current, "to_node_id": node, "distance_m": float(route["distance_m"]),
                    "path_node_ids": route["path_node_ids"], "path_edge_ids": route["path_edge_ids"], "sequence_in_order": len(legs)+1}
                legs.append({"route_leg_id": _hash(identity), **identity})
            current = node
        if picks and current != gate_node:
            route = path(current, gate_node)
            if route.get("reachable"):
                identity = {"scenario": scenario, "order_key": order.get("order_key"), "leg_type": "return_to_gate",
                    "from_node_id": current, "to_node_id": gate_node, "distance_m": float(route["distance_m"]),
                    "path_node_ids": route["path_node_ids"], "path_edge_ids": route["path_edge_ids"], "sequence_in_order": len(legs)+1}
                legs.append({"route_leg_id": _hash(identity), **identity}); current = gate_node
            else:
                route_invalid = True
                for demand in demand_results: demand["reasons"].append("return_to_gate_unreachable")
        for demand in demand_results:
            demand["shortage_units"] = demand["requested_units"] - demand["picked_units"]
            demand["pick_count"] = len(demand["pick_event_ids"]); demand["split"] = len(demand.pop("_stock_keys")) > 1
            demand.pop("_source_index")
            demand["reasons"] = sorted(set(demand["reasons"]))
            if demand["status"] != "invalid":
                demand["status"] = "fulfilled" if not demand["shortage_units"] else ("partial" if demand["picked_units"] else "shortage")
                if demand["shortage_units"] and not demand["reasons"]: demand["reasons"] = ["insufficient_stock"]
        requested = sum(x["requested_units"] for x in demand_results); picked_units = sum(x["picked_units"] for x in demand_results)
        shortage = sum(x["shortage_units"] for x in demand_results)
        identity = {"scenario": scenario, "outbound_demand_set_id": demand_set_id, "order_key": order.get("order_key"),
            "gate_key": gate_key, "pick_event_ids": [x["pick_event_id"] for x in picks],
            "route_leg_ids": [x["route_leg_id"] for x in legs], "requested_units": requested, "picked_units": picked_units,
            "shortage_units": shortage}
        results.append({"order_replay_id": _hash(identity), "scenario": scenario, "order_key": order.get("order_key"),
            "outbound_order_number": order.get("outbound_order_number"), "created_at": order.get("created_at"),
            "warehouse": order.get("warehouse"), "gate_key": gate_key, "start_node_id": gate_node, "end_node_id": current,
            "demands": demand_results, "picks": picks, "route_legs": legs, "requested_units": requested,
            "picked_units": picked_units, "shortage_units": shortage,
            "route_distance_m": sum(x["distance_m"] for x in legs), "returned_to_gate": current == gate_node,
            "status": "invalid_route" if route_invalid else ("fulfilled" if shortage == 0 else ("partial" if picked_units else "shortage"))})
    final_units = sum(x["remaining_units"] for x in working["stock_by_key"].values())
    demands = [d for o in results for d in o["demands"]]
    summary = {"orders_total": len(results), "orders_fully_fulfilled": sum(o["status"] == "fulfilled" for o in results),
        "orders_with_shortage": sum(o["shortage_units"] > 0 for o in results), "demands_total": len(demands),
        "demands_fulfilled": sum(d["status"] == "fulfilled" for d in demands), "demands_with_shortage": sum(d["shortage_units"] > 0 for d in demands),
        "requested_units": sum(o["requested_units"] for o in results), "picked_units": sum(o["picked_units"] for o in results),
        "shortage_units": sum(o["shortage_units"] for o in results), "pick_events": sum(len(o["picks"]) for o in results),
        "split_demands": sum(d["split"] for d in demands), "route_legs": sum(len(o["route_legs"]) for o in results),
        "route_distance_m": sum(o["route_distance_m"] for o in results), "return_legs": sum(l["leg_type"] == "return_to_gate" for o in results for l in o["route_legs"]),
        "initial_stock_units": initial_units, "final_stock_units": final_units,
        "receipt_unresolved_batches": len(unavailable["unresolved_batches"]), "receipt_unresolved_qty_units": unavailable["unresolved_qty_units"],
        "stock_conservation_ok": initial_units - sum(o["picked_units"] for o in results) == final_units,
        "demand_conservation_ok": sum(o["requested_units"] for o in results) == sum(o["picked_units"]+o["shortage_units"] for o in results),
        "returned_orders_to_gate": sum(o["returned_to_gate"] for o in results)}
    key = f"{scenario}_receipt_placement_state_id"
    result = {"scenario": scenario, "source_receipt_placement_state_id": receipt.get(key, ""),
        "initial_working_stock_id": initial_id, "final_working_stock_id": _stock_id(working), "orders": results,
        "shortages": [{"order_key": o["order_key"], "demand_key": d["demand_key"], "shortage_units": d["shortage_units"], "reasons": d["reasons"]}
                      for o in results for d in o["demands"] if d["shortage_units"]],
        "final_working_stock": working, "receipt_unavailable": unavailable, "summary": summary}
    return result, counters


def replay_outbound_scenarios(model: dict[str, Any], physical_graph_state: dict[str, Any],
    outbound_demand_state: dict[str, Any], opening_stock_state: dict[str, Any],
    current_receipt_placement_state: dict[str, Any], proposed_receipt_placement_state: dict[str, Any],
    replay_rule_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay one shared demand stream against two isolated physical stocks."""
    inputs = [model, physical_graph_state, outbound_demand_state, opening_stock_state,
              current_receipt_placement_state, proposed_receipt_placement_state, replay_rule_state]
    errors = []
    if not all(isinstance(x, Mapping) for x in inputs): errors.append("invalid_top_level_input_contracts")
    model = model if isinstance(model, Mapping) else {}; graph = physical_graph_state if isinstance(physical_graph_state, Mapping) else {}
    rules = replay_rule_state if isinstance(replay_rule_state, Mapping) else {}
    current_receipt = current_receipt_placement_state if isinstance(current_receipt_placement_state, Mapping) else {}
    proposed_receipt = proposed_receipt_placement_state if isinstance(proposed_receipt_placement_state, Mapping) else {}
    if not graph.get("physical_graph_state_id"): errors.append("physical_graph_id_missing")
    if graph.get("model_id") not in (None, "", model.get("model_id")): errors.append("model_id_mismatch")
    if graph.get("summary", {}).get("graph_ready_for_replay") is False: errors.append("graph_explicitly_not_ready_for_replay")
    for field in ("nodes", "edges", "cell_access_links", "gate_links"):
        if not isinstance(graph.get(field), list): errors.append(f"invalid_graph_{field}")
    if not rules.get("replay_rule_state_id"): errors.append("replay_rule_state_id_missing")
    zone_order = rules.get("zone_order") if isinstance(rules.get("zone_order"), list) else []
    if len(zone_order) != 4 or set(zone_order) != ZONES: errors.append("invalid_zone_order")
    target = _normalized(rules.get("target_normalized_warehouse")); gate_key = _text(rules.get("gate_key"))
    links = [x for x in graph.get("gate_links", []) or [] if isinstance(x, Mapping) and _text(x.get("gate_key")) == gate_key
             and x.get("status", "valid") in {"valid", "mapped", "ok", None, ""}]
    if not links: errors.append("configured_gate_missing")
    elif len(links) > 1: errors.append("configured_gate_duplicated")
    gate_node = _text(links[0].get("gate_node_id")) if len(links) == 1 else ""
    node_ids = {_text(x.get("node_id")) for x in graph.get("nodes", []) or [] if isinstance(x, Mapping)}
    if links and (not gate_node or gate_node not in node_ids): errors.append("gate_node_missing")
    if current_receipt.get("scenario") not in (None, "", "current", "CURRENT"): errors.append("current_scenario_type_mismatch")
    if proposed_receipt.get("scenario") not in (None, "", "proposed", "PROPOSED"): errors.append("proposed_scenario_type_mismatch")
    current_dataset = current_receipt.get("receipt_dataset_id", ""); proposed_dataset = proposed_receipt.get("receipt_dataset_id", "")
    if current_dataset != proposed_dataset: errors.append("receipt_dataset_id_mismatch")
    orders_source = outbound_demand_state.get("orders", []) if isinstance(outbound_demand_state, Mapping) else []
    if not isinstance(orders_source, list): errors.append("invalid_top_level_input_contracts"); orders_source = []
    sorted_orders = sorted((copy.deepcopy(x) for x in orders_source if isinstance(x, Mapping)), key=lambda x: (
        _text(x.get("created_at")), _text(x.get("outbound_order_number")), _source_index(x.get("source_indexes")), _text(x.get("order_key"))))
    accepted = [x for x in sorted_orders if _normalized(x.get("warehouse")) == target]
    excluded = [{"order_key": x.get("order_key"), "reason": "warehouse_mismatch"} for x in sorted_orders if _normalized(x.get("warehouse")) != target]
    demand_identity = [{"order_key": o.get("order_key"), "created_at": o.get("created_at"), "warehouse": o.get("warehouse"),
        "outbound_order_number": o.get("outbound_order_number"), "source_indexes": o.get("source_indexes", []),
        "demands": [{"demand_key": d.get("demand_key"), "sku_key": d.get("sku_key"), "requested_units": d.get("requested_units"),
                     "unit_name": "короб" if _box(d.get("unit_name")) else _text(d.get("unit_name")), "source_indexes": d.get("source_indexes", [])}
                    for d in o.get("demands", []) if isinstance(d, Mapping)]} for o in accepted]
    demand_set_id = _hash(demand_identity); opening_identity = _hash(sorted(copy.deepcopy(opening_stock_state.get("placements", []) or []), key=_hash)) if isinstance(opening_stock_state, Mapping) else _hash([])
    shared = {"same_outbound_demand_set": True, "same_opening_stock": True, "same_physical_graph": True,
        "same_gate": True, "same_zone_order": True, "current_receipt_dataset_id": current_dataset,
        "proposed_receipt_dataset_id": proposed_dataset, "receipt_dataset_ids_match": current_dataset == proposed_dataset,
        "only_receipt_placement_differs": True}
    diagnostics = {"configuration_errors": sorted(set(errors)), "source_outbound_orders": len(orders_source),
        "accepted_outbound_orders": len(accepted), "excluded_outbound_orders": len(excluded),
        "source_outbound_demands": sum(len(x.get("demands", [])) for x in sorted_orders),
        "accepted_outbound_demands": sum(len(x.get("demands", [])) for x in accepted),
        "unsupported_unit_demands": sum(not _box(d.get("unit_name")) for o in accepted for d in o.get("demands", []) if isinstance(d, Mapping)),
        "graph_nodes": len(graph.get("nodes", []) or []), "graph_edges": len(graph.get("edges", []) or []),
        "graph_cell_access_links": len(graph.get("cell_access_links", []) or []), "configured_gate_found": bool(gate_node),
        "shortest_path_calls": 0, "shortest_path_cache_hits": 0}
    if errors:
        current = _empty_scenario("current", current_receipt); proposed = _empty_scenario("proposed", proposed_receipt)
    else:
        cache = {}; stats = {"calls": 0, "hits": 0}
        current, cc = _replay_one_scenario("current", current_receipt, model, opening_stock_state, accepted, demand_set_id, graph, gate_key, gate_node, zone_order, cache, stats)
        proposed, pc = _replay_one_scenario("proposed", proposed_receipt, model, opening_stock_state, accepted, demand_set_id, graph, gate_key, gate_node, zone_order, cache, stats)
        diagnostics.update({"current_unmapped_stock_candidates": cc["unmapped"], "proposed_unmapped_stock_candidates": pc["unmapped"],
            "current_unreachable_stock_candidates": cc["unreachable"], "proposed_unreachable_stock_candidates": pc["unreachable"],
            "shortest_path_calls": stats["calls"], "shortest_path_cache_hits": stats["hits"]})
    for name, result in (("current", current), ("proposed", proposed)):
        summary = result["summary"]
        diagnostics.update({f"{name}_initial_stock_records": len(result.get("final_working_stock", {}).get("stock_by_key", {})),
            f"{name}_initial_stock_units": summary["initial_stock_units"], f"{name}_receipt_unresolved_batches": summary["receipt_unresolved_batches"],
            f"{name}_orders_replayed": summary["orders_total"], f"{name}_pick_events": summary["pick_events"],
            f"{name}_shortage_units": summary["shortage_units"], f"{name}_split_demands": summary["split_demands"]})
    state = {"outbound_replay_state_id": "", "outbound_demand_set_id": demand_set_id,
        "physical_graph_state_id": graph.get("physical_graph_state_id", ""), "opening_stock_identity": opening_identity,
        "receipt_dataset_id": current_dataset if current_dataset == proposed_dataset else "", "replay_rule_state_id": rules.get("replay_rule_state_id", ""),
        "target_normalized_warehouse": target, "gate_key": gate_key, "zone_order": copy.deepcopy(zone_order),
        "accepted_order_keys": [x.get("order_key") for x in accepted], "excluded_outbound_orders": excluded,
        "current": current, "proposed": proposed, "shared_controls": shared, "limitations": LIMITATIONS.copy()}
    state["outbound_replay_state_id"] = _hash({k: v for k, v in state.items() if k != "outbound_replay_state_id"} | {"configuration_errors": diagnostics["configuration_errors"]})
    return state, diagnostics
