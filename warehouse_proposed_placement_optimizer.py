"""Pure, deterministic weight-zone placement planning from a baseline state.

The planner produces a target layout; it neither executes moves nor mutates a
``SimulationState``.  In particular, a previous PROPOSED result is deliberately
not an input: every invocation rebuilds from the supplied factual baseline.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from warehouse_placement_rules import (
    compute_placement_rule_set_id,
    get_enabled_rule_ids,
    validate_placement_rule_set,
)
from warehouse_placement_zones import (
    ASSIGNABLE_PLACEMENT_ZONE_IDS,
    DEFAULT_PLACEMENT_ZONE_ORDER,
    normalize_placement_zone,
)
from warehouse_physical_graph import build_physical_warehouse_graph, find_shortest_path

PROPOSED_PLACEMENT_PLAN_VERSION = 6
_SUPPORTED_RULES = frozenset({"weight_zones", "velocity", "adjacency", "base_sku_capacity", "picking_storage", "replenishment", "deep_lane_optimization"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _number_key(value: Any) -> tuple[int, float | str]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, float(value))
    try:
        return (0, float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return (1, _text(value))


def _cell_key(cell: Mapping[str, Any]) -> str:
    return _text(cell.get("cell_key")) or "|".join(
        (_text(cell.get("row_number")), _text(cell.get("cell_number")), _text(cell.get("tier") or "1"))
    )


def _position_key(position: Mapping[str, Any], cells: Mapping[str, Mapping[str, Any]]) -> tuple[Any, ...]:
    cell = cells.get(position.get("cell_key"), {})
    return (
        _number_key(cell.get("row_order")),
        _number_key(cell.get("physical_index", position.get("physical_index"))),
        _number_key(position.get("slot_index")),
        _number_key(position.get("row_number", cell.get("row_number"))),
        _number_key(position.get("cell_number", cell.get("cell_number"))),
        _number_key(position.get("tier", cell.get("tier"))),
        _text(position.get("position_id")),
    )


def _diagnostics(errors: list[dict[str, Any]], warnings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"valid": not errors, "errors": errors, "warnings": warnings or []}


def _canonical_sku_zones(rows: Any) -> tuple[dict[str, str], list[dict[str, Any]]]:
    assignments: dict[str, set[str]] = defaultdict(set)
    errors: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return {}, [{"code": "invalid_sku_zone_rows"}]
    for row in rows:
        if not isinstance(row, Mapping) or not _text(row.get("sku_key")):
            errors.append({"code": "invalid_sku_zone_assignment"})
            continue
        sku = _text(row["sku_key"])
        zone = normalize_placement_zone(row.get("target_zone"))
        if zone not in ASSIGNABLE_PLACEMENT_ZONE_IDS:
            errors.append({"code": "invalid_target_zone", "sku_key": sku, "target_zone": row.get("target_zone")})
            continue
        assignments[sku].add(zone)
    for sku, zones in sorted(assignments.items()):
        if len(zones) > 1:
            errors.append({"code": "conflicting_sku_zone_assignment", "sku_key": sku, "target_zones": sorted(zones)})
    return {sku: next(iter(zones)) for sku, zones in sorted(assignments.items()) if len(zones) == 1}, errors


def _canonical_velocity(rows: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    result: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    if rows is None:
        return result, errors
    if not isinstance(rows, list):
        return result, [{"code": "invalid_sku_velocity_rows"}]
    for row in rows:
        sku = _text(row.get("sku_key")) if isinstance(row, Mapping) else ""
        rank = row.get("velocity_rank") if isinstance(row, Mapping) else None
        if not sku or rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 6):
            errors.append({"code": "invalid_velocity_assignment", "sku_key": sku or None})
            continue
        canonical = {"sku_key": sku, "velocity_rank": rank,
                     "velocity_class": _text(row.get("velocity_class")) or ("no_history" if rank is None else "")}
        if sku in result and result[sku] != canonical:
            errors.append({"code": "conflicting_velocity_assignment", "sku_key": sku})
        result[sku] = canonical
    return dict(sorted(result.items())), errors


def _plan_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable business fields (never messages, labels, or timestamps)."""
    return {
        "proposed_placement_plan_version": plan.get("proposed_placement_plan_version"),
        "baseline_state_id": plan.get("baseline_state_id"),
        "model_id": plan.get("model_id"),
        "placement_rule_set_id": plan.get("placement_rule_set_id"),
        "sku_zone_assignments": plan.get("sku_zone_assignments", []),
        "sku_velocity_assignments": plan.get("sku_velocity_assignments", []),
        "adjacency_profile_id": plan.get("adjacency_profile_id"),
        "sku_adjacency_assignments": plan.get("sku_adjacency_assignments", []),
        "gate_identity": plan.get("gate_identity"),
        "status": plan.get("status"),
        "blocked_reasons": [
            {key: value for key, value in reason.items() if key != "message"}
            for reason in plan.get("blocked_reasons", [])
        ],
        "placements": plan.get("placements", []),
        "sku_capacity_allocations": plan.get("sku_capacity_allocations", []),
        "fixed_units": [
            {key: unit.get(key) for key in ("placement_unit_id", "origin_position_id", "reason")}
            for unit in plan.get("fixed_units", [])
        ],
        "unresolved_units": [
            {key: unit.get(key) for key in ("placement_unit_id", "stock_lot_id", "origin_position_id", "reason")}
            for unit in plan.get("unresolved_units", [])
        ],
    }


def compute_proposed_placement_plan_id(plan: Mapping[str, Any]) -> str:
    """Compute the canonical SHA-256 identity of a proposed placement plan."""
    return _sha256(_plan_identity(plan))


def _placement_row(unit: Mapping[str, Any], target: Mapping[str, Any], cells: Mapping[str, Mapping[str, Any]],
                   velocity: Mapping[str, Any] | None = None, distances: Mapping[str, float] | None = None,
                   role: str | None = None) -> dict[str, Any]:
    origin_cell = unit.get("origin_cell_key")
    target_cell = target.get("cell_key")
    row = {
        "placement_unit_id": unit["placement_unit_id"],
        "unit_type": unit["unit_type"],
        "sku_key": unit["sku_key"],
        "stock_lot_ids": list(unit.get("stock_lot_ids", [])),
        "pallet_unit_id": unit.get("pallet_unit_id"),
        "origin_position_id": unit.get("origin_position_id"),
        "origin_cell_key": origin_cell,
        "origin_zone": normalize_placement_zone(cells.get(origin_cell, {}).get("weight_zone")),
        "target_position_id": target.get("position_id"),
        "target_cell_key": target_cell,
        "target_zone": normalize_placement_zone(cells.get(target_cell, {}).get("weight_zone")),
        "moved": target.get("position_id") != unit.get("origin_position_id"),
    }
    if unit.get("qty_boxes") is not None:
        row["qty_boxes"] = unit["qty_boxes"]
    if role is not None:
        row["stock_role"] = role
    if unit.get("origin_depth_index") is not None:
        row["origin_depth_index"] = unit["origin_depth_index"]
    if target.get("depth_index") is not None:
        row["target_depth_index"] = target["depth_index"]
    if velocity is not None:
        row.update({"velocity_rank": velocity.get("velocity_rank"),
                    "velocity_class": velocity.get("velocity_class"),
                    "origin_gate_distance_m": (distances or {}).get(origin_cell),
                    "target_gate_distance_m": (distances or {}).get(target_cell)})
    return row


def _extract_units(
    model: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    cells = {_cell_key(cell): cell for cell in model.get("cells", []) or [] if isinstance(cell, Mapping)}
    positions = {p.get("position_id"): p for p in state.get("physical_positions", []) or [] if isinstance(p, Mapping)}
    lots = {lot.get("stock_lot_id"): lot for lot in state.get("stock_lots", []) or [] if isinstance(lot, Mapping)}
    lots_by_cell: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for lot in lots.values():
        if lot.get("location_status") == "located" and isinstance(lot.get("qty_boxes"), (int, float)) and lot.get("qty_boxes", 0) > 0:
            lots_by_cell[lot.get("cell_key")].append(lot)

    units: list[dict[str, Any]] = []
    linked_lots: set[str] = set()
    for pallet in sorted((state.get("pallet_units", []) or []), key=lambda p: _text(p.get("pallet_unit_id"))):
        if not isinstance(pallet, Mapping) or pallet.get("physical_status") != "active" or pallet.get("location_status") != "located":
            continue
        position = positions.get(pallet.get("position_id"))
        linked = [lot for lot in lots.values() if lot.get("pallet_unit_id") == pallet.get("pallet_unit_id")]
        if not position or position.get("cell_key") != pallet.get("cell_key") or len(linked) != 1:
            continue
        lot = linked[0]
        quantity = pallet.get("remaining_boxes")
        if (isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0
                or lot.get("qty_boxes") != quantity or lot.get("position_id") != pallet.get("position_id")
                or lot.get("cell_key") != pallet.get("cell_key")
                or lot.get("sku_key") != pallet.get("sku_key")):
            continue
        linked_lots.add(lot.get("stock_lot_id"))
        units.append({
            "placement_unit_id": pallet.get("pallet_unit_id"), "unit_type": "pallet",
            "sku_key": pallet.get("sku_key"), "stock_lot_ids": [lot.get("stock_lot_id")],
            "pallet_unit_id": pallet.get("pallet_unit_id"), "origin_position_id": pallet.get("position_id"),
            "origin_cell_key": pallet.get("cell_key"), "origin_depth_index": position.get("depth_index"),
            "qty_boxes": pallet.get("remaining_boxes"),
        })

    occupied_by_unit = {unit["origin_position_id"] for unit in units}
    for position in sorted(positions.values(), key=lambda p: _position_key(p, cells)):
        if position.get("position_id") in occupied_by_unit or position.get("status") != "occupied":
            continue
        cell_key = position.get("cell_key")
        cell = cells.get(cell_key)
        storage = _text((cell or {}).get("storage_type") or (cell or {}).get("row_storage_type") or "normal")
        cell_lots = [lot for lot in lots_by_cell.get(cell_key, []) if lot.get("stock_lot_id") not in linked_lots]
        if cell and storage != "deep_lane" and (cell.get("capacity_pallets", 1) == 1) and len(cell_lots) == 1:
            lot = cell_lots[0]
            identity = {"unit_type": "opaque_opening_position", "position_id": position.get("position_id"),
                        "stock_lot_ids": [lot.get("stock_lot_id")]}
            units.append({
                "placement_unit_id": _sha256(identity), "unit_type": "opaque_opening_position",
                "sku_key": lot.get("sku_key"), "stock_lot_ids": [lot.get("stock_lot_id")],
                "pallet_unit_id": None, "origin_position_id": position.get("position_id"),
                "origin_cell_key": cell_key, "qty_boxes": lot.get("qty_boxes"),
            })

    represented = {lot_id for unit in units for lot_id in unit["stock_lot_ids"]}
    unresolved = []
    for lot_id, lot in sorted(lots.items(), key=lambda item: _text(item[0])):
        if lot_id in represented or not (isinstance(lot.get("qty_boxes"), (int, float)) and lot.get("qty_boxes", 0) > 0):
            continue
        unresolved.append({"stock_lot_id": lot_id, "sku_key": lot.get("sku_key"),
                           "origin_position_id": lot.get("position_id"),
                           "origin_cell_key": lot.get("cell_key"),
                           "reason": "unknown_location" if lot.get("location_status") != "located" else "unsupported_physical_footprint"})
    return sorted(units, key=lambda u: _text(u["placement_unit_id"])), unresolved, cells, positions


def _base_plan(model: Mapping[str, Any], state: Mapping[str, Any], rules: Mapping[str, Any], assignments: Mapping[str, str]) -> dict[str, Any]:
    return {
        "proposed_placement_plan_version": PROPOSED_PLACEMENT_PLAN_VERSION,
        "proposed_placement_plan_id": None,
        "baseline_state_id": state.get("simulation_state_id"),
        "model_id": model.get("model_id"),
        "placement_rule_set_id": rules.get("placement_rule_set_id"),
        "target_normalized_warehouse": state.get("target_normalized_warehouse"),
        "sku_zone_assignments": [{"sku_key": sku, "target_zone": zone} for sku, zone in sorted(assignments.items())],
        "status": "blocked", "blocked_reasons": [], "placements": [], "fixed_units": [],
        "unresolved_units": [], "sku_capacity_allocations": [], "zone_summary": [], "summary": {},
        "limitations": [
            "weight_zones_only", "target_layout_not_move_sequence", "simulation_state_not_mutated",
            "missing_sku_zone_is_not_inferred",
        ],
    }


def _deep_lane_inventory(cells, positions, units, unresolved, occupancy_by_cell):
    """Return authoritative lanes and deterministic reasons for unavailable lanes."""
    units_by_cell = defaultdict(list)
    for unit in units:
        units_by_cell[unit.get("origin_cell_key")].append(unit)
    unresolved_cells = {row.get("origin_cell_key") for row in unresolved}
    lanes, locked = [], []
    for cell_key, cell in sorted(cells.items(), key=lambda item: _cell_key(item[1])):
        storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
        if storage != "deep_lane" and cell.get("capacity_pallets", 1) == 1:
            continue
        lane_positions = sorted((p for p in positions.values() if p.get("cell_key") == cell_key),
                                key=lambda p: (_number_key(p.get("depth_index")), _position_key(p, cells)))
        reason = None
        capacity = cell.get("capacity_pallets")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1 or len(lane_positions) != capacity:
            reason = "deep_lane_geometry_invalid"
        elif any(p.get("depth_index") not in range(1, capacity + 1) for p in lane_positions) or len({p.get("depth_index") for p in lane_positions}) != capacity:
            reason = "deep_lane_access_or_depth_invalid"
        elif any(p.get("status") == "unknown" for p in lane_positions) or occupancy_by_cell.get(cell_key, {}).get("exact_occupied_positions") is None:
            reason = "deep_lane_unknown_occupancy"
        elif cell_key in unresolved_cells:
            reason = "deep_lane_unresolved_stock"
        occupied = {p.get("position_id") for p in lane_positions if p.get("status") == "occupied"}
        lane_units = units_by_cell.get(cell_key, [])
        represented = {u.get("origin_position_id") for u in lane_units if u.get("unit_type") == "pallet"}
        if reason is None and (occupied != represented or any(u.get("unit_type") != "pallet" for u in lane_units)):
            reason = "deep_lane_unsupported_footprint"
        skus = {_text(u.get("sku_key")) for u in lane_units}
        if reason is None and len(skus) > 1:
            reason = "deep_lane_mixed_sku"
        if reason:
            locked.append({"cell_key": cell_key, "reason": reason, "occupied": bool(occupied)})
        else:
            lanes.append({"cell_key": cell_key, "cell": cell, "positions": lane_positions,
                          "units": sorted(lane_units, key=lambda u: _text(u["placement_unit_id"])),
                          "sku_key": next(iter(skus), None)})
    return lanes, locked


def _compact_existing_deep_lanes(lanes, cells):
    rows = []
    for lane in lanes:
        units = lane["units"]
        targets = lane["positions"][-len(units):] if units else []
        by_origin = {u.get("origin_position_id"): u for u in units}
        # Preserve a fully packed suffix byte-for-byte; otherwise stable unit identity
        # makes the counterfactual independent of input ordering.
        if set(by_origin) == {p.get("position_id") for p in targets}:
            ordered_units = [by_origin[p.get("position_id")] for p in targets]
        else:
            ordered_units = units
        rows.extend(_placement_row(unit, target, cells) for unit, target in zip(ordered_units, targets))
    return rows


def build_proposed_placement_plan(
    model: dict[str, Any], baseline_state: dict[str, Any], placement_rule_set: dict[str, Any],
    sku_zone_rows: list[dict[str, Any]], *, sku_velocity_rows: list[dict[str, Any]] | None = None,
    adjacency_profile: dict[str, Any] | None = None,
    gate_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a deterministic target layout exclusively from the given baseline."""
    rule_validation = validate_placement_rule_set(placement_rule_set)
    assignments, mapping_errors = _canonical_sku_zones(sku_zone_rows)
    velocities, velocity_errors = _canonical_velocity(sku_velocity_rows)
    adjacency_rows = adjacency_profile.get("rows", []) if isinstance(adjacency_profile, Mapping) else []
    adjacency = {_text(row.get("sku_key")): _text(row.get("adjacency_group"))
                 for row in adjacency_rows if isinstance(row, Mapping) and _text(row.get("sku_key"))}
    adjacency_errors = []
    if adjacency_profile is not None and (not isinstance(adjacency_profile, Mapping)
                                          or not adjacency_profile.get("adjacency_profile_id")):
        adjacency_errors.append({"code": "invalid_adjacency_profile"})
    if isinstance(adjacency_profile, Mapping):
        adjacency_errors.extend(adjacency_profile.get("validation_errors", []) or [])
    errors = list(rule_validation.get("errors", [])) + mapping_errors + velocity_errors + adjacency_errors
    rules_id = None
    if rule_validation.get("valid"):
        rules_id = compute_placement_rule_set_id(placement_rule_set)
        supplied_id = placement_rule_set.get("placement_rule_set_id")
        if supplied_id is not None and supplied_id != rules_id:
            errors.append({"code": "placement_rule_set_id_mismatch"})
    rules_copy = dict(placement_rule_set) if isinstance(placement_rule_set, Mapping) else {}
    rules_copy["placement_rule_set_id"] = rules_id or rules_copy.get("placement_rule_set_id")
    plan = _base_plan(model, baseline_state, rules_copy, assignments)
    plan["sku_velocity_assignments"] = list(velocities.values())
    plan["adjacency_profile_id"] = adjacency_profile.get("adjacency_profile_id") if isinstance(adjacency_profile, Mapping) else None
    plan["sku_adjacency_assignments"] = [{"sku_key": sku, "adjacency_group": group}
                                          for sku, group in sorted(adjacency.items())]
    plan["gate_identity"] = None

    enabled = get_enabled_rule_ids(placement_rule_set) if rule_validation.get("valid") else []
    for rule_id in enabled:
        if rule_id not in _SUPPORTED_RULES:
            errors.append({"code": "unsupported_enabled_rule", "rule_id": rule_id})
    if not isinstance(model, Mapping) or not model.get("model_id"):
        errors.append({"code": "invalid_model_identity"})
    if not isinstance(baseline_state, Mapping) or not baseline_state.get("simulation_state_id"):
        errors.append({"code": "invalid_baseline_state_identity"})
    if baseline_state.get("model_id") != model.get("model_id"):
        errors.append({"code": "baseline_model_mismatch"})
    if "velocity" in enabled and not velocities:
        errors.append({"code": "velocity_profile_empty"})
    if errors:
        plan["blocked_reasons"] = sorted(errors, key=_canonical_json)
        plan["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(plan)
        return plan, _diagnostics(errors)

    units, unresolved, cells, positions = _extract_units(model, baseline_state)
    occupancy_by_cell = {row.get("cell_key"): row for row in baseline_state.get("cell_occupancy", []) or []}
    weight_enabled = "weight_zones" in enabled
    velocity_enabled = "velocity" in enabled
    adjacency_enabled = "adjacency" in enabled
    capacity_enabled = "base_sku_capacity" in enabled
    picking_enabled = "picking_storage" in enabled
    deep_enabled = "deep_lane_optimization" in enabled
    deep_lanes, locked_deep_lanes = _deep_lane_inventory(cells, positions, units, unresolved, occupancy_by_cell)
    eligible_deep_cells = {lane["cell_key"] for lane in deep_lanes}
    minimum_positions = next(
        (rule["parameters"]["minimum_positions_per_sku"] for rule in placement_rule_set["rules"]
         if rule["rule_id"] == "base_sku_capacity"), 1)
    if deep_enabled and not any((weight_enabled, velocity_enabled, adjacency_enabled, picking_enabled)):
        eligible_cells = {lane["cell_key"] for lane in deep_lanes}
        locked_cells = {row["cell_key"] for row in locked_deep_lanes}
        deep_rows = _compact_existing_deep_lanes(deep_lanes, cells)
        other_rows = [_placement_row(unit, positions[unit["origin_position_id"]], cells)
                      for unit in units if unit.get("origin_cell_key") not in eligible_cells]
        plan["placements"] = sorted(deep_rows + other_rows, key=lambda row: _text(row["placement_unit_id"]))
        plan["fixed_units"] = sorted([
            dict(unit, reason=next(row["reason"] for row in locked_deep_lanes
                                   if row["cell_key"] == unit.get("origin_cell_key")))
            for unit in units if unit.get("origin_cell_key") in locked_cells
        ], key=lambda row: _text(row["placement_unit_id"]))
        plan["unresolved_units"] = sorted(unresolved, key=_canonical_json)
        plan["status"] = "partial" if any(row["occupied"] for row in locked_deep_lanes) or unresolved else "ready"
        moved = [row for row in deep_rows if row["moved"]]
        used = sum(bool(lane["units"]) for lane in deep_lanes)
        plan["summary"] = {
            "placement_units_total": len(plan["placements"]), "units_moved": len(moved),
            "deep_lane_eligible_cells": len(deep_lanes), "deep_lane_locked_or_unconfigured_cells": len(locked_deep_lanes),
            "deep_lane_pallets_before": sum(len(lane["units"]) for lane in deep_lanes),
            "deep_lane_pallets_after": sum(len(lane["units"]) for lane in deep_lanes),
            "deep_lane_cells_used_before": used, "deep_lane_cells_used_after": used,
            "deep_lane_units_moved_into": 0, "deep_lane_units_moved_out": 0,
            "deep_lane_units_moved_within": len(moved),
            "deep_lane_affected_skus": sorted({_text(row.get("sku_key")) for row in moved}),
            "deep_lane_unresolved_skus": sorted({_text(row.get("sku_key")) for row in unresolved if row.get("sku_key")}),
        }
        plan["limitations"].extend(["counterfactual_target_not_relocation_sequence", "no_deep_lane_replenishment"])
        plan["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(plan)
        validation = validate_proposed_placement_plan(plan, model, baseline_state, placement_rule_set, sku_zone_rows)
        validation["warnings"] = sorted(locked_deep_lanes, key=_canonical_json)
        return plan, validation
    distances: dict[str, float] = {}
    if velocity_enabled or picking_enabled:
        graph, graph_diagnostics = build_physical_warehouse_graph(model, gate_state)
        gates = graph.get("gate_links", [])
        if len(gates) != 1 or graph_diagnostics.get("configuration_errors"):
            error = {"code": "picking_storage_gate_required" if picking_enabled else "velocity_gate_required"}
            plan["blocked_reasons"] = [error]
            plan["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(plan)
            return plan, _diagnostics([error])
        plan["gate_identity"] = {key: gates[0].get(key) for key in ("gate_key", "gate_node_id")}
        start_node = gates[0]["gate_node_id"]
        for link in graph.get("cell_access_links", []):
            path = find_shortest_path(graph, start_node, link.get("access_node_id"))
            if path.get("reachable"):
                distances[link["cell_key"]] = path["distance_m"]
    fixed: list[dict[str, Any]] = []
    applicable: list[tuple[dict[str, Any], str]] = []
    kept: list[tuple[dict[str, Any], str]] = []
    identity_units: list[dict[str, Any]] = []

    ambiguous_picking_skus: set[str] = set()
    if picking_enabled and not weight_enabled:
        zones_by_sku: dict[str, set[str]] = defaultdict(set)
        for unit in units:
            zone = normalize_placement_zone(cells.get(unit.get("origin_cell_key"), {}).get("weight_zone"))
            if zone in ASSIGNABLE_PLACEMENT_ZONE_IDS:
                zones_by_sku[_text(unit.get("sku_key"))].add(zone)
        ambiguous_picking_skus = {sku for sku, zones in zones_by_sku.items() if len(zones) != 1}

    if not weight_enabled and not velocity_enabled and not adjacency_enabled and not picking_enabled:
        identity_units = units
    else:
        for unit in units:
            cell = cells.get(unit.get("origin_cell_key"), {})
            position = positions.get(unit.get("origin_position_id"), {})
            occupancy = occupancy_by_cell.get(unit.get("origin_cell_key"), {})
            storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
            reason = None
            if picking_enabled and _text(unit.get("sku_key")) in ambiguous_picking_skus:
                reason = "ambiguous_picking_weight_zone"
            if ((storage == "deep_lane" or cell.get("capacity_pallets", 1) != 1)
                    and not (deep_enabled and unit.get("origin_cell_key") in eligible_deep_cells)):
                reason = ("deep_lane_adjacency_optimization_not_implemented" if adjacency_enabled
                          else "deep_lane_optimization_not_enabled")
            elif position.get("status") == "unknown":
                reason = "unknown_position_status"
            elif occupancy.get("occupancy_conflict"):
                reason = "occupancy_conflict"
            elif not cell or not position or position.get("cell_key") != unit.get("origin_cell_key"):
                reason = "invalid_physical_reference"
            elif ((velocity_enabled or picking_enabled) and storage != "deep_lane"
                  and unit.get("origin_cell_key") not in distances):
                reason = "picking_unreachable_position" if picking_enabled else "velocity_unreachable_position"
            elif weight_enabled and unit.get("sku_key") not in assignments:
                reason = "missing_sku_zone_assignment"
            if reason:
                record = dict(unit) | {"reason": reason}
                if picking_enabled:
                    record["stock_role"] = "storage"
                fixed.append(record)
                if reason in {"missing_sku_zone_assignment", "ambiguous_picking_weight_zone"}:
                    unresolved.append({"placement_unit_id": unit["placement_unit_id"], "sku_key": unit.get("sku_key"),
                                       "origin_position_id": unit.get("origin_position_id"),
                                       "origin_cell_key": unit.get("origin_cell_key"), "reason": reason})
                identity_units.append(unit)
                continue
            target_zone = assignments[unit["sku_key"]] if weight_enabled else normalize_placement_zone(cell.get("weight_zone"))
            if (not deep_enabled and not velocity_enabled and not adjacency_enabled and not picking_enabled
                    and normalize_placement_zone(cell.get("weight_zone")) == target_zone):
                kept.append((unit, target_zone))
            else:
                applicable.append((unit, target_zone))

    reserved = {unit.get("origin_position_id") for unit in fixed}
    reserved.update(unit.get("origin_position_id") for unit, _ in kept)
    # Unknown positions and occupied positions with no represented footprint are never released.
    represented_origins = {unit.get("origin_position_id") for unit in units}
    reserved.update(p.get("position_id") for p in positions.values() if p.get("status") == "unknown")
    reserved.update(p.get("position_id") for p in positions.values()
                    if p.get("status") == "occupied" and p.get("position_id") not in represented_origins)

    available: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    normal_position_totals: Counter[str] = Counter()
    for position in positions.values():
        cell = cells.get(position.get("cell_key"), {})
        storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
        zone = normalize_placement_zone(cell.get("weight_zone"))
        if storage == "deep_lane" or cell.get("capacity_pallets", 1) != 1 or zone not in ASSIGNABLE_PLACEMENT_ZONE_IDS:
            continue
        if (velocity_enabled or picking_enabled) and position.get("cell_key") not in distances:
            continue
        normal_position_totals[zone] += 1
        if position.get("status") in {"free", "occupied"} and position.get("position_id") not in reserved:
            available[zone].append(position)
    for zone in available:
        available[zone].sort(key=lambda p: ((distances.get(p.get("cell_key"), float("inf")), _position_key(p, cells))
                                            if (velocity_enabled or picking_enabled) else _position_key(p, cells)))

    # Secure one normal picking target per SKU first, then allocate only storage
    # pallets to whole deep lanes.  Deep slots deliberately never enter the
    # generic free-position pool: ownership and suffix packing are decided here.
    picking_unit_ids: set[str] = set()
    if picking_enabled:
        first_by_sku: dict[str, tuple[dict[str, Any], str]] = {}
        for item in sorted(applicable, key=lambda item: (
                velocities.get(_text(item[0].get("sku_key")), {}).get("velocity_rank") or 7,
                _text(item[0].get("sku_key")),
                _position_key(positions.get(item[0].get("origin_position_id"), {}), cells),
                _text(item[0].get("placement_unit_id")))):
            first_by_sku.setdefault(_text(item[0].get("sku_key")), item)
        picking_unit_ids = {item[0]["placement_unit_id"] for item in first_by_sku.values()}

    deep_target_by_unit: dict[str, Mapping[str, Any]] = {}
    if deep_enabled:
        storage_by_sku_zone: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for unit, zone in applicable:
            if unit.get("unit_type") == "pallet" and unit["placement_unit_id"] not in picking_unit_ids:
                storage_by_sku_zone[(_text(unit.get("sku_key")), zone)].append(unit)
        claimed_lanes: set[str] = set()
        preliminary_normal_need = Counter(zone for _, zone in applicable)
        preliminary_normal_need.subtract(Counter(
            zone for unit, zone in applicable if unit["placement_unit_id"] not in picking_unit_ids
            and unit.get("origin_cell_key") in eligible_deep_cells))
        for (sku, zone), sku_units in sorted(storage_by_sku_zone.items()):
            sku_units.sort(key=lambda unit: (
                unit.get("origin_cell_key") not in eligible_deep_cells,
                _position_key(positions.get(unit.get("origin_position_id"), {}), cells),
                _text(unit.get("placement_unit_id"))))
            compatible = [lane for lane in deep_lanes
                          if normalize_placement_zone(lane["cell"].get("weight_zone")) == zone]
            same = [lane for lane in compatible if lane.get("sku_key") == sku]
            empty = [lane for lane in compatible if not lane.get("units")]
            lane_order = sorted(same, key=lambda lane: _cell_key(lane["cell"]))
            normal_short = max(0, preliminary_normal_need[zone] - len(available[zone]))
            # Opening an empty lane for a single otherwise-placeable pallet is
            # churn, not useful compaction. Existing same-SKU lanes are always
            # filled before an empty lane is considered.
            if len(sku_units) >= 2 or normal_short:
                lane_order += sorted(empty, key=lambda lane: _cell_key(lane["cell"]))
            remaining_units = list(sku_units)
            for lane in lane_order:
                if lane["cell_key"] in claimed_lanes or not remaining_units:
                    continue
                take = min(len(lane["positions"]), len(remaining_units))
                selected, remaining_units = remaining_units[:take], remaining_units[take:]
                targets = lane["positions"][-take:]
                for unit, target in zip(selected, targets):
                    deep_target_by_unit[unit["placement_unit_id"]] = target
                claimed_lanes.add(lane["cell_key"])

    needed = Counter(zone for unit, zone in applicable
                     if unit["placement_unit_id"] not in deep_target_by_unit)
    shortages = {zone: max(0, needed[zone] - len(available[zone])) for zone in ASSIGNABLE_PLACEMENT_ZONE_IDS}
    zone_summary = []
    fixed_positions = Counter(normalize_placement_zone(cells.get(unit.get("origin_cell_key"), {}).get("weight_zone")) for unit in fixed)
    correct_counts = Counter(zone for _, zone in kept)
    for zone in DEFAULT_PLACEMENT_ZONE_ORDER:
        zone_summary.append({
            "zone": zone, "physical_positions_total": normal_position_totals[zone],
            "fixed_positions": fixed_positions[zone], "correct_units_kept": correct_counts[zone],
            "units_to_move_in": needed[zone], "available_positions_for_moves": len(available[zone]),
            "planned_units": 0 if any(shortages.values()) else needed[zone] + correct_counts[zone],
            "capacity_shortage": shortages[zone],
        })
    plan["zone_summary"] = zone_summary

    placements: list[dict[str, Any]] = []
    if any(shortages.values()):
        plan["blocked_reasons"] = [{"code": "insufficient_capacity_in_target_zone", "zone": zone,
                                    "required_units": needed[zone], "available_positions": len(available[zone]),
                                    "shortage_positions": shortages[zone]}
                                   for zone in DEFAULT_PLACEMENT_ZONE_ORDER if shortages[zone]]
    else:
        for unit in identity_units:
            placements.append(_placement_row(unit, positions[unit["origin_position_id"]], cells,
                                              role="storage" if picking_enabled else None))
        for unit, _ in kept:
            placements.append(_placement_row(unit, positions[unit["origin_position_id"]], cells))
        zone_rank = {zone: index for index, zone in enumerate(DEFAULT_PLACEMENT_ZONE_ORDER)}
        origin_order = {unit["placement_unit_id"]: _position_key(
            positions.get(unit.get("origin_position_id"), {}), cells) for unit, _ in applicable}
        sku_first = {}
        group_first = {}
        for unit, zone in applicable:
            sku = _text(unit.get("sku_key"))
            key = (zone, velocities.get(sku, {}).get("velocity_rank") or 7)
            order = origin_order[unit["placement_unit_id"]]
            sku_first[(key, sku)] = min(sku_first.get((key, sku), order), order)
            group = adjacency.get(sku, "")
            if group:
                group_first[(key, group)] = min(group_first.get((key, group), order), order)

        def adjacency_key(item: tuple[dict[str, Any], str]) -> tuple[Any, ...]:
            sku = _text(item[0].get("sku_key"))
            group = adjacency.get(sku, "") if adjacency_enabled else ""
            bucket = (item[1], velocities.get(sku, {}).get("velocity_rank") or 7)
            # Explicit groups make SKU blocks neighbours; the SKU key keeps each block compact.
            return ((0, group_first[(bucket, group)], sku_first[(bucket, sku)], sku)
                    if group else (1, sku_first.get((bucket, sku), ()), sku))
        ordered = sorted(applicable, key=lambda item: (zone_rank[item[1]],
                         velocities.get(_text(item[0].get("sku_key")), {}).get("velocity_rank") or 7,
                         adjacency_key(item),
                         origin_order.get(item[0]["placement_unit_id"], ()),
                         _text(item[0].get("placement_unit_id"))))
        remaining = {zone: list(values) for zone, values in available.items()}
        allocation_order = ordered
        if picking_enabled:
            picking_items = sorted((item for item in ordered
                                    if item[0]["placement_unit_id"] in picking_unit_ids), key=lambda item: (
                velocities.get(_text(item[0].get("sku_key")), {}).get("velocity_rank") or 7,
                _text(item[0].get("sku_key")), _text(item[0].get("placement_unit_id"))))
            allocation_order = picking_items + [item for item in ordered if item[0]["placement_unit_id"] not in picking_unit_ids]
        for unit, zone in allocation_order:
            is_picking = unit["placement_unit_id"] in picking_unit_ids
            deep_target = deep_target_by_unit.get(unit["placement_unit_id"])
            origin = None
            if (deep_target is None and not is_picking and not adjacency_enabled
                    and not velocities.get(_text(unit.get("sku_key")), {}).get("velocity_rank")):
                origin = next((p for p in remaining[zone] if p.get("position_id") == unit.get("origin_position_id")), None)
            target = deep_target or origin or remaining[zone][0]
            if deep_target is None:
                remaining[zone].remove(target)
            sku = _text(unit.get("sku_key"))
            velocity = velocities.get(sku, {"velocity_rank": None, "velocity_class": "no_history"})
            row = _placement_row(unit, target, cells, velocity if velocity_enabled else None, distances,
                                 "picking" if is_picking else ("storage" if picking_enabled else None))
            if adjacency_enabled:
                row["adjacency_group"] = adjacency.get(sku, "")
            placements.append(row)
        plan["status"] = "partial" if unresolved or fixed else "ready"

    placements.sort(key=lambda row: _text(row["placement_unit_id"]))
    fixed.sort(key=lambda unit: _text(unit["placement_unit_id"]))
    unresolved.sort(key=lambda unit: (_text(unit.get("placement_unit_id")), _text(unit.get("stock_lot_id")), unit["reason"]))
    plan["placements"], plan["fixed_units"], plan["unresolved_units"] = placements, fixed, unresolved
    capacity_allocations: list[dict[str, Any]] = []
    if capacity_enabled and plan["status"] != "blocked":
        final_targets = {row["target_position_id"] for row in placements}
        placement_by_unit = {row["placement_unit_id"]: row for row in placements}
        actual_positions_by_sku: dict[str, set[str]] = defaultdict(set)
        for unit in units:
            placement = placement_by_unit.get(unit["placement_unit_id"])
            position_id = placement.get("target_position_id") if placement else unit.get("origin_position_id")
            if position_id in positions and (
                    placement or positions[position_id].get("cell_key") == unit.get("origin_cell_key")):
                actual_positions_by_sku[_text(unit.get("sku_key"))].add(position_id)

        # Unsupported footprints can still provide reliable capacity evidence: an
        # explicit physical origin, or one occupied position for one SKU in a cell.
        unresolved_by_cell: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in unresolved:
            position = positions.get(row.get("origin_position_id"))
            if position and position.get("cell_key") == row.get("origin_cell_key"):
                actual_positions_by_sku[_text(row.get("sku_key"))].add(position["position_id"])
            elif row.get("origin_cell_key"):
                unresolved_by_cell[row["origin_cell_key"]].append(row)
        for cell_key, rows in unresolved_by_cell.items():
            occupied = [p for p in positions.values()
                        if p.get("cell_key") == cell_key and p.get("status") == "occupied"]
            skus = {_text(row.get("sku_key")) for row in rows if _text(row.get("sku_key"))}
            if len(occupied) == 1 and len(skus) == 1:
                actual_positions_by_sku[next(iter(skus))].add(occupied[0]["position_id"])

        fixed_origins = {unit.get("origin_position_id") for unit in fixed}
        released_origins = {
            unit.get("origin_position_id") for unit in units
            if unit.get("origin_position_id") not in fixed_origins
            and placement_by_unit.get(unit["placement_unit_id"], {}).get("target_position_id")
            != unit.get("origin_position_id")
        }
        capacity_candidates: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for position in positions.values():
            cell = cells.get(position.get("cell_key"), {})
            storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
            zone = normalize_placement_zone(cell.get("weight_zone"))
            position_id = position.get("position_id")
            is_vacant = position.get("status") == "free" or position_id in released_origins
            if (position_id not in final_targets and is_vacant and storage != "deep_lane"
                    and position_id not in fixed_origins and position.get("status") != "unknown"
                    and cell.get("capacity_pallets", 1) == 1 and zone in ASSIGNABLE_PLACEMENT_ZONE_IDS
                    and (not velocity_enabled or position.get("cell_key") in distances)):
                capacity_candidates[zone].append(position)
        capacity_rank: dict[str, int] = {}
        for zone, candidates in capacity_candidates.items():
            all_zone_positions = [p for p in positions.values()
                                  if normalize_placement_zone(cells.get(p.get("cell_key"), {}).get("weight_zone")) == zone]
            capacity_rank.update({p.get("position_id"): index for index, p in enumerate(
                sorted(all_zone_positions, key=lambda item: _position_key(item, cells)))})
        claimed: set[str] = set()
        for sku in sorted(actual_positions_by_sku,
                          key=lambda item: (velocities.get(item, {}).get("velocity_rank") or 7, item)):
            occupied_ids = sorted(actual_positions_by_sku[sku])
            occupied_zones = {normalize_placement_zone(
                cells.get(positions[position_id].get("cell_key"), {}).get("weight_zone"))
                              for position_id in occupied_ids}
            occupied_zones &= set(ASSIGNABLE_PLACEMENT_ZONE_IDS)
            target_zone = assignments.get(sku) if weight_enabled else (
                next(iter(occupied_zones)) if len(occupied_zones) == 1 else None)
            required = max(len(occupied_ids), minimum_positions)
            needed_reserve = required - len(occupied_ids)
            reason = None
            if target_zone is None and needed_reserve:
                reason = "missing_target_zone" if weight_enabled else "ambiguous_target_zone"
            candidates = [p for p in capacity_candidates.get(target_zone, [])
                          if p.get("position_id") not in claimed] if target_zone else []
            anchor_ranks = [capacity_rank[position_id] for position_id in occupied_ids
                            if position_id in capacity_rank]
            def capacity_key(position: Mapping[str, Any]) -> tuple[Any, ...]:
                rank = capacity_rank.get(position.get("position_id"), 0)
                compactness = min((abs(rank - anchor) for anchor in anchor_ranks), default=0)
                if velocity_enabled:
                    return (distances.get(position.get("cell_key"), float("inf")), compactness,
                            _position_key(position, cells))
                return (compactness, _position_key(position, cells))
            selected = sorted(candidates, key=capacity_key)[:needed_reserve]
            reserved_ids = sorted(p["position_id"] for p in selected)
            claimed.update(reserved_ids)
            shortage = needed_reserve - len(reserved_ids)
            if shortage and reason is None:
                reason = "insufficient_eligible_capacity"
            capacity_allocations.append({
                "sku_key": sku, "target_zone": target_zone,
                "minimum_positions": minimum_positions,
                "stock_positions_required": len(occupied_ids), "positions_required": required,
                "occupied_target_position_ids": occupied_ids, "reserved_position_ids": reserved_ids,
                "positions_allocated": len(occupied_ids) + len(reserved_ids),
                "shortage_positions": shortage, "status": "partial" if shortage else "satisfied",
                "reason": reason,
            })
        plan["sku_capacity_allocations"] = capacity_allocations
        if any(row["shortage_positions"] for row in capacity_allocations):
            plan["status"] = "partial"
        zone_rows = {row["zone"]: row for row in plan["zone_summary"]}
        for zone, row in zone_rows.items():
            row["reserved_capacity_positions"] = sum(
                len(allocation["reserved_position_ids"]) for allocation in capacity_allocations
                if allocation["target_zone"] == zone)
            row["capacity_shortage_positions"] = sum(
                allocation["shortage_positions"] for allocation in capacity_allocations
                if allocation["target_zone"] == zone)
    valid_assignment_units = [(unit, assignments[unit["sku_key"]]) for unit in units if unit.get("sku_key") in assignments]
    before = sum(normalize_placement_zone(cells.get(unit.get("origin_cell_key"), {}).get("weight_zone")) == zone
                 for unit, zone in valid_assignment_units)
    placement_by_unit = {row["placement_unit_id"]: row for row in placements}
    after = sum(placement_by_unit.get(unit["placement_unit_id"], {}).get("target_zone") == zone
                for unit, zone in valid_assignment_units)
    moved = sum(row["moved"] for row in placements)
    denominator = len(valid_assignment_units)
    ordered_by_zone: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for position in positions.values():
        zone = normalize_placement_zone(cells.get(position.get("cell_key"), {}).get("weight_zone"))
        if zone in ASSIGNABLE_PLACEMENT_ZONE_IDS:
            ordered_by_zone[zone].append(position)
    physical_rank = {}
    for zone, zone_positions in ordered_by_zone.items():
        for index, position in enumerate(sorted(zone_positions, key=lambda p: _position_key(p, cells))):
            physical_rank[position.get("position_id")] = (zone, index)

    def fragments(rows: list[dict[str, Any]], position_key: str, grouping: str) -> int:
        grouped: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for row in rows:
            group = row.get(grouping)
            rank = physical_rank.get(row.get(position_key))
            if group and rank:
                grouped[str(group)].append(rank)
        total = 0
        for ranks in grouped.values():
            ordered_ranks = sorted(ranks)
            total += sum(index == 0 or rank[0] != ordered_ranks[index - 1][0]
                         or rank[1] != ordered_ranks[index - 1][1] + 1
                         for index, rank in enumerate(ordered_ranks))
        return total

    movable_ids = {unit["placement_unit_id"] for unit, _ in applicable + kept}
    adjacency_rows_for_metrics = [row for row in placements if row["placement_unit_id"] in movable_ids]
    for row in adjacency_rows_for_metrics:
        row.setdefault("adjacency_group", adjacency.get(_text(row.get("sku_key")), ""))
    sku_counts = Counter(row["sku_key"] for row in adjacency_rows_for_metrics)
    multi_unit_skus = {sku for sku, count in sku_counts.items() if count > 1}
    sku_metric_rows = [row for row in adjacency_rows_for_metrics if row["sku_key"] in multi_unit_skus]
    same_before = fragments(sku_metric_rows, "origin_position_id", "sku_key")
    same_after = fragments(sku_metric_rows, "target_position_id", "sku_key")
    group_metric_rows = [row for row in adjacency_rows_for_metrics if row.get("adjacency_group")]
    group_before = fragments(group_metric_rows, "origin_position_id", "adjacency_group")
    group_after = fragments(group_metric_rows, "target_position_id", "adjacency_group")
    plan["summary"] = {
        "placement_units_total": len(units), "movable_units": len(applicable) + len(kept),
        "fixed_units": len(fixed), "unresolved_units": len(unresolved),
        "units_kept": len(placements) - moved, "units_moved": moved,
        "move_rate_percent": 100.0 * moved / len(units) if units else 0.0,
        "zones_used": sorted({row["target_zone"] for row in placements if row.get("target_zone") in ASSIGNABLE_PLACEMENT_ZONE_IDS},
                             key=lambda zone: list(DEFAULT_PLACEMENT_ZONE_ORDER).index(zone)),
        "weight_zone_assignment_units": denominator,
        "weight_zone_compliant_before": before, "weight_zone_compliant_after": after,
        "weight_zone_compliance_before_percent": 100.0 * before / denominator if denominator else 100.0,
        "weight_zone_compliance_after_percent": 100.0 * after / denominator if denominator else 100.0,
        "weight_zone_compliance_complete": weight_enabled and not unresolved and not any(shortages.values()) and after == denominator,
        "adjacency_enabled": adjacency_enabled,
        "adjacency_profile_skus": len(adjacency),
        "adjacency_groups_total": len({group for group in adjacency.values() if group}),
        "multi_unit_skus": len(multi_unit_skus),
        "same_sku_fragments_before": same_before, "same_sku_fragments_after": same_after,
        "adjacency_group_fragments_before": group_before, "adjacency_group_fragments_after": group_after,
        "same_sku_fragment_reduction": same_before - same_after,
        "adjacency_group_fragment_reduction": group_before - group_after,
        "adjacency_units_moved": sum(bool(row["moved"]) for row in adjacency_rows_for_metrics) if adjacency_enabled else 0,
        "capacity_skus_total": len(capacity_allocations),
        "capacity_skus_satisfied": sum(row["status"] == "satisfied" for row in capacity_allocations),
        "capacity_skus_short": sum(row["status"] == "partial" for row in capacity_allocations),
        "capacity_positions_required": sum(row["positions_required"] for row in capacity_allocations),
        "capacity_positions_occupied": sum(row["stock_positions_required"] for row in capacity_allocations),
        "capacity_positions_reserved": sum(len(row["reserved_position_ids"]) for row in capacity_allocations),
        "capacity_shortage_positions": sum(row["shortage_positions"] for row in capacity_allocations),
    }
    picking_rows = [row for row in placements if row.get("stock_role") == "picking"]
    storage_rows = [row for row in placements if row.get("stock_role") == "storage"]
    represented_skus = ({_text(unit.get("sku_key")) for unit in units if _text(unit.get("sku_key"))}
                        | {_text(unit.get("sku_key")) for unit in unresolved if _text(unit.get("sku_key"))})
    picking_skus = {_text(row.get("sku_key")) for row in picking_rows}
    plan["summary"].update({
        "picking_storage_enabled": picking_enabled,
        "picking_storage_participating_skus": len(represented_skus) if picking_enabled else 0,
        "skus_with_picking_position": len(picking_skus),
        "picking_positions": len(picking_rows),
        "storage_positions": len(storage_rows),
        "skus_without_supported_picking_position": len(represented_skus - picking_skus) if picking_enabled else 0,
    })
    if deep_enabled:
        before_deep = [unit for unit in units if unit.get("origin_cell_key") in eligible_deep_cells]
        after_deep = [row for row in placements if row.get("target_cell_key") in eligible_deep_cells]
        before_cells = {unit.get("origin_cell_key") for unit in before_deep}
        after_cells = {row.get("target_cell_key") for row in after_deep}
        plan["summary"].update({
            "deep_lane_eligible_cells": len(deep_lanes),
            "deep_lane_locked_or_unconfigured_cells": len(locked_deep_lanes),
            "deep_lane_pallets_before": len(before_deep), "deep_lane_pallets_after": len(after_deep),
            "deep_lane_cells_used_before": len(before_cells), "deep_lane_cells_used_after": len(after_cells),
            "deep_lane_units_moved_into": sum(row.get("origin_cell_key") not in eligible_deep_cells for row in after_deep),
            "deep_lane_units_moved_out": sum(row.get("target_cell_key") not in eligible_deep_cells
                                              for row in placements if row.get("origin_cell_key") in eligible_deep_cells),
            "deep_lane_units_moved_within": sum(row.get("moved") and row.get("origin_cell_key") in eligible_deep_cells
                                                 and row.get("target_cell_key") in eligible_deep_cells for row in placements),
            "deep_lane_affected_skus": sorted({_text(row.get("sku_key")) for row in placements
                                                 if row.get("moved") and (row.get("origin_cell_key") in eligible_deep_cells
                                                                          or row.get("target_cell_key") in eligible_deep_cells)}),
            "deep_lane_unresolved_skus": sorted({_text(row.get("sku_key")) for row in unresolved
                                                   if _text(row.get("sku_key"))}),
        })
    velocity_rows = [row for row in placements if "velocity_rank" in row]
    ranked_rows = [row for row in velocity_rows if row.get("velocity_rank") is not None]
    def average(rows: list[dict[str, Any]], key: str) -> float | None:
        values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
        return round(sum(values) / len(values), 6) if values else None
    plan["summary"].update({
        "velocity_profile_skus": len(velocities), "velocity_ranked_units": len(ranked_rows),
        "velocity_unranked_units": len(velocity_rows) - len(ranked_rows),
        "velocity_units_moved": sum(bool(row.get("moved")) for row in velocity_rows),
        "average_gate_distance_before_m": average(velocity_rows, "origin_gate_distance_m"),
        "average_gate_distance_after_m": average(velocity_rows, "target_gate_distance_m"),
    })
    for rank in range(1, 7):
        rank_rows = [row for row in velocity_rows if row.get("velocity_rank") == rank]
        plan["summary"][f"rank_{rank}_average_gate_distance_before_m"] = average(rank_rows, "origin_gate_distance_m")
        plan["summary"][f"rank_{rank}_average_gate_distance_after_m"] = average(rank_rows, "target_gate_distance_m")
    if velocity_enabled:
        plan["limitations"] = [item for item in plan["limitations"] if item != "weight_zones_only"]
        plan["limitations"].append("velocity_priority_uses_gate_distance_not_global_route_optimization")
        if not deep_enabled:
            plan["limitations"].append("deep_lane_velocity_optimization_not_implemented")
    if adjacency_enabled:
        plan["limitations"] = [item for item in plan["limitations"] if item != "weight_zones_only"]
        plan["limitations"].extend(["physical_order_contiguity_v1", "cross_rank_adjacency_not_optimized_v1",
                                    "explicit_adjacency_groups_only_no_fuzzy_matching"])
        if not deep_enabled:
            plan["limitations"].append("deep_lane_adjacency_optimization_not_implemented")
    if deep_enabled:
        plan["limitations"].extend(["counterfactual_target_not_relocation_sequence", "no_deep_lane_replenishment"])
    plan["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(plan)
    validation = validate_proposed_placement_plan(plan, model, baseline_state, placement_rule_set, sku_zone_rows)
    validation["warnings"] = sorted(locked_deep_lanes, key=_canonical_json) if deep_enabled else []
    return plan, validation


def validate_proposed_placement_plan(
    plan: dict[str, Any], model: dict[str, Any], baseline_state: dict[str, Any],
    placement_rule_set: dict[str, Any] | None = None,
    sku_zone_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate plan identity and physical target-layout invariants.

    The planning inputs are optional for downstream consumers which receive an
    already-built plan.  When supplied, rule and zone-decision consistency is
    validated in addition to the self-contained physical plan contract.
    """
    errors: list[dict[str, Any]] = []
    def error(code: str, **details: Any) -> None:
        errors.append({"code": code, **details})
    if plan.get("proposed_placement_plan_version") != PROPOSED_PLACEMENT_PLAN_VERSION:
        error("unsupported_proposed_placement_plan_version")
    if plan.get("proposed_placement_plan_id") != compute_proposed_placement_plan_id(plan):
        error("proposed_placement_plan_id_mismatch")
    if plan.get("baseline_state_id") != baseline_state.get("simulation_state_id"):
        error("baseline_state_id_mismatch")
    if plan.get("model_id") != model.get("model_id"):
        error("model_id_mismatch")
    rule_validation = validate_placement_rule_set(placement_rule_set) if placement_rule_set is not None else None
    if rule_validation is not None:
        if not rule_validation["valid"]:
            errors.extend(rule_validation["errors"])
        elif plan.get("placement_rule_set_id") != compute_placement_rule_set_id(placement_rule_set):
            error("placement_rule_set_id_mismatch")
    assignments, mapping_errors = _canonical_sku_zones(sku_zone_rows) if sku_zone_rows is not None else ({}, [])
    errors.extend(mapping_errors)
    cells = {_cell_key(cell): cell for cell in model.get("cells", []) or []}
    positions = {position.get("position_id"): position for position in baseline_state.get("physical_positions", []) or []}
    placements = plan.get("placements", []) or []
    unit_ids = [row.get("placement_unit_id") for row in placements]
    targets = [row.get("target_position_id") for row in placements]
    if len(unit_ids) != len(set(unit_ids)):
        error("duplicate_placement_unit_id")
    if len(targets) != len(set(targets)):
        error("duplicate_target_position_id")
    reservation_rows = plan.get("sku_capacity_allocations", []) or []
    reserved_capacity = [position_id for row in reservation_rows
                         for position_id in row.get("reserved_position_ids", [])]
    if len(reserved_capacity) != len(set(reserved_capacity)):
        error("duplicate_reserved_capacity_position")
    if set(reserved_capacity) & set(targets):
        error("reserved_capacity_overlaps_stock")
    fixed_positions = {unit.get("origin_position_id") for unit in plan.get("fixed_units", []) or []
                       if unit.get("origin_position_id")}
    if set(reserved_capacity) & fixed_positions:
        error("reserved_capacity_overlaps_fixed_stock")
    for position_id in reserved_capacity:
        position = positions.get(position_id)
        cell = cells.get((position or {}).get("cell_key"), {})
        storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
        if not position:
            error("invalid_reserved_capacity_position", position_id=position_id)
        elif position.get("status") == "unknown" or storage == "deep_lane" or cell.get("capacity_pallets", 1) != 1:
            error("ineligible_reserved_capacity_position", position_id=position_id)
    fixed_unit_ids = {unit.get("placement_unit_id") for unit in plan.get("fixed_units", []) or []}
    weight_enabled = bool(rule_validation and rule_validation["valid"]
                          and "weight_zones" in get_enabled_rule_ids(placement_rule_set))
    picking_enabled = bool(rule_validation and rule_validation["valid"]
                           and "picking_storage" in get_enabled_rule_ids(placement_rule_set))
    picking_counts: Counter[str] = Counter()
    for row in placements:
        origin, target = positions.get(row.get("origin_position_id")), positions.get(row.get("target_position_id"))
        if not origin:
            error("invalid_origin_position", placement_unit_id=row.get("placement_unit_id"))
        if not target:
            error("invalid_target_position", placement_unit_id=row.get("placement_unit_id"))
            continue
        if row.get("origin_cell_key") != (origin or {}).get("cell_key"):
            error("origin_position_cell_mismatch", placement_unit_id=row.get("placement_unit_id"))
        if row.get("target_cell_key") != target.get("cell_key"):
            error("target_position_cell_mismatch", placement_unit_id=row.get("placement_unit_id"))
        cell = cells.get(target.get("cell_key"), {})
        zone = normalize_placement_zone(cell.get("weight_zone"))
        if row.get("target_zone") != zone:
            error("target_zone_cell_mismatch", placement_unit_id=row.get("placement_unit_id"))
        if row.get("moved") != (row.get("origin_position_id") != row.get("target_position_id")):
            error("inconsistent_moved_flag", placement_unit_id=row.get("placement_unit_id"))
        if row.get("moved") and row.get("target_position_id") in fixed_positions:
            error("fixed_reserved_position_reused", target_position_id=row.get("target_position_id"))
        origin_cell = cells.get(row.get("origin_cell_key"), {})
        origin_storage = _text(origin_cell.get("storage_type") or origin_cell.get("row_storage_type") or "normal")
        target_storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
        if origin_storage == "deep_lane" or target_storage == "deep_lane":
            if row.get("unit_type") != "pallet":
                error("deep_lane_requires_authoritative_pallet", placement_unit_id=row.get("placement_unit_id"))
            if row.get("origin_depth_index") != (origin or {}).get("depth_index"):
                error("origin_depth_index_mismatch", placement_unit_id=row.get("placement_unit_id"))
            if row.get("target_depth_index") != target.get("depth_index") or target.get("depth_index") is None:
                error("target_depth_index_mismatch", placement_unit_id=row.get("placement_unit_id"))
        mapped = assignments.get(row.get("sku_key"))
        if (weight_enabled and mapped and row.get("placement_unit_id") not in fixed_unit_ids
                and row.get("target_zone") != mapped):
            error("weight_zone_target_mismatch", placement_unit_id=row.get("placement_unit_id"))
        if picking_enabled:
            if row.get("stock_role") not in {"picking", "storage"}:
                error("missing_stock_role", placement_unit_id=row.get("placement_unit_id"))
            if row.get("stock_role") == "picking":
                picking_counts[_text(row.get("sku_key"))] += 1
                target_storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
                if target_storage == "deep_lane" or cell.get("capacity_pallets", 1) != 1:
                    error("ineligible_picking_position", placement_unit_id=row.get("placement_unit_id"))
    for sku, count in sorted(picking_counts.items()):
        if count != 1:
            error("invalid_picking_position_count", sku_key=sku, count=count)
    deep_targets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in placements:
        cell = cells.get(row.get("target_cell_key"), {})
        if _text(cell.get("storage_type") or cell.get("row_storage_type")) == "deep_lane":
            deep_targets[row["target_cell_key"]].append(row)
    for cell_key, rows in sorted(deep_targets.items()):
        if len({_text(row.get("sku_key")) for row in rows}) != 1:
            error("mixed_sku_deep_lane_target", cell_key=cell_key)
        capacity = cells[cell_key].get("capacity_pallets")
        depths = sorted(row.get("target_depth_index") for row in rows)
        if not isinstance(capacity, int) or depths != list(range(capacity - len(rows) + 1, capacity + 1)):
            error("deep_lane_not_back_packed", cell_key=cell_key)
        if any(row.get("stock_role") == "picking" for row in rows):
            error("picking_role_in_deep_lane", cell_key=cell_key)
    return _diagnostics(errors)
