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

PROPOSED_PLACEMENT_PLAN_VERSION = 1
_SUPPORTED_RULES = frozenset({"weight_zones"})


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


def _plan_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable business fields (never messages, labels, or timestamps)."""
    return {
        "proposed_placement_plan_version": plan.get("proposed_placement_plan_version"),
        "baseline_state_id": plan.get("baseline_state_id"),
        "model_id": plan.get("model_id"),
        "placement_rule_set_id": plan.get("placement_rule_set_id"),
        "sku_zone_assignments": plan.get("sku_zone_assignments", []),
        "status": plan.get("status"),
        "blocked_reasons": [
            {key: value for key, value in reason.items() if key != "message"}
            for reason in plan.get("blocked_reasons", [])
        ],
        "placements": plan.get("placements", []),
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


def _placement_row(unit: Mapping[str, Any], target: Mapping[str, Any], cells: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
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
        linked_lots.add(lot.get("stock_lot_id"))
        units.append({
            "placement_unit_id": pallet.get("pallet_unit_id"), "unit_type": "pallet",
            "sku_key": pallet.get("sku_key"), "stock_lot_ids": [lot.get("stock_lot_id")],
            "pallet_unit_id": pallet.get("pallet_unit_id"), "origin_position_id": pallet.get("position_id"),
            "origin_cell_key": pallet.get("cell_key"), "qty_boxes": pallet.get("remaining_boxes"),
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
        "unresolved_units": [], "zone_summary": [], "summary": {},
        "limitations": [
            "weight_zones_only", "target_layout_not_move_sequence", "simulation_state_not_mutated",
            "deep_lane_optimization_not_implemented", "missing_sku_zone_is_not_inferred",
        ],
    }


def build_proposed_placement_plan(
    model: dict[str, Any], baseline_state: dict[str, Any], placement_rule_set: dict[str, Any],
    sku_zone_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a deterministic target layout exclusively from the given baseline."""
    rule_validation = validate_placement_rule_set(placement_rule_set)
    assignments, mapping_errors = _canonical_sku_zones(sku_zone_rows)
    errors = list(rule_validation.get("errors", [])) + mapping_errors
    rules_id = None
    if rule_validation.get("valid"):
        rules_id = compute_placement_rule_set_id(placement_rule_set)
        supplied_id = placement_rule_set.get("placement_rule_set_id")
        if supplied_id is not None and supplied_id != rules_id:
            errors.append({"code": "placement_rule_set_id_mismatch"})
    rules_copy = dict(placement_rule_set) if isinstance(placement_rule_set, Mapping) else {}
    rules_copy["placement_rule_set_id"] = rules_id or rules_copy.get("placement_rule_set_id")
    plan = _base_plan(model, baseline_state, rules_copy, assignments)

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
    if errors:
        plan["blocked_reasons"] = sorted(errors, key=_canonical_json)
        plan["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(plan)
        return plan, _diagnostics(errors)

    units, unresolved, cells, positions = _extract_units(model, baseline_state)
    occupancy_by_cell = {row.get("cell_key"): row for row in baseline_state.get("cell_occupancy", []) or []}
    weight_enabled = "weight_zones" in enabled
    fixed: list[dict[str, Any]] = []
    applicable: list[tuple[dict[str, Any], str]] = []
    kept: list[tuple[dict[str, Any], str]] = []
    identity_units: list[dict[str, Any]] = []

    if not weight_enabled:
        identity_units = units
    else:
        for unit in units:
            cell = cells.get(unit.get("origin_cell_key"), {})
            position = positions.get(unit.get("origin_position_id"), {})
            occupancy = occupancy_by_cell.get(unit.get("origin_cell_key"), {})
            storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
            reason = None
            if storage == "deep_lane" or cell.get("capacity_pallets", 1) != 1:
                reason = "deep_lane_optimization_not_enabled"
            elif position.get("status") == "unknown":
                reason = "unknown_position_status"
            elif occupancy.get("occupancy_conflict"):
                reason = "occupancy_conflict"
            elif not cell or not position or position.get("cell_key") != unit.get("origin_cell_key"):
                reason = "invalid_physical_reference"
            elif unit.get("sku_key") not in assignments:
                reason = "missing_sku_zone_assignment"
            if reason:
                record = dict(unit) | {"reason": reason}
                fixed.append(record)
                if reason == "missing_sku_zone_assignment":
                    unresolved.append({"placement_unit_id": unit["placement_unit_id"], "sku_key": unit.get("sku_key"),
                                       "origin_position_id": unit.get("origin_position_id"),
                                       "origin_cell_key": unit.get("origin_cell_key"), "reason": reason})
                identity_units.append(unit)
                continue
            target_zone = assignments[unit["sku_key"]]
            if normalize_placement_zone(cell.get("weight_zone")) == target_zone:
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
        normal_position_totals[zone] += 1
        if position.get("status") in {"free", "occupied"} and position.get("position_id") not in reserved:
            available[zone].append(position)
    for zone in available:
        available[zone].sort(key=lambda p: _position_key(p, cells))

    needed = Counter(zone for _, zone in applicable)
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
            placements.append(_placement_row(unit, positions[unit["origin_position_id"]], cells))
        for unit, _ in kept:
            placements.append(_placement_row(unit, positions[unit["origin_position_id"]], cells))
        zone_rank = {zone: index for index, zone in enumerate(DEFAULT_PLACEMENT_ZONE_ORDER)}
        ordered = sorted(applicable, key=lambda item: (zone_rank[item[1]], _text(item[0].get("sku_key")),
                                                       _text(item[0].get("placement_unit_id"))))
        offsets: Counter[str] = Counter()
        for unit, zone in ordered:
            target = available[zone][offsets[zone]]
            offsets[zone] += 1
            placements.append(_placement_row(unit, target, cells))
        plan["status"] = "partial" if unresolved or fixed else "ready"

    placements.sort(key=lambda row: _text(row["placement_unit_id"]))
    fixed.sort(key=lambda unit: _text(unit["placement_unit_id"]))
    unresolved.sort(key=lambda unit: (_text(unit.get("placement_unit_id")), _text(unit.get("stock_lot_id")), unit["reason"]))
    plan["placements"], plan["fixed_units"], plan["unresolved_units"] = placements, fixed, unresolved
    valid_assignment_units = [(unit, assignments[unit["sku_key"]]) for unit in units if unit.get("sku_key") in assignments]
    before = sum(normalize_placement_zone(cells.get(unit.get("origin_cell_key"), {}).get("weight_zone")) == zone
                 for unit, zone in valid_assignment_units)
    placement_by_unit = {row["placement_unit_id"]: row for row in placements}
    after = sum(placement_by_unit.get(unit["placement_unit_id"], {}).get("target_zone") == zone
                for unit, zone in valid_assignment_units)
    moved = sum(row["moved"] for row in placements)
    denominator = len(valid_assignment_units)
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
    }
    plan["proposed_placement_plan_id"] = compute_proposed_placement_plan_id(plan)
    validation = validate_proposed_placement_plan(plan, model, baseline_state, placement_rule_set, sku_zone_rows)
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
    fixed_positions = {unit.get("origin_position_id") for unit in plan.get("fixed_units", []) or [] if unit.get("origin_position_id")}
    fixed_unit_ids = {unit.get("placement_unit_id") for unit in plan.get("fixed_units", []) or []}
    weight_enabled = bool(rule_validation and rule_validation["valid"]
                          and "weight_zones" in get_enabled_rule_ids(placement_rule_set))
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
        storage = _text(origin_cell.get("storage_type") or origin_cell.get("row_storage_type") or "normal")
        if row.get("moved") and (storage == "deep_lane" or origin_cell.get("capacity_pallets", 1) != 1):
            error("deep_lane_unit_moved", placement_unit_id=row.get("placement_unit_id"))
        mapped = assignments.get(row.get("sku_key"))
        if (weight_enabled and mapped and row.get("placement_unit_id") not in fixed_unit_ids
                and row.get("target_zone") != mapped):
            error("weight_zone_target_mismatch", placement_unit_id=row.get("placement_unit_id"))
    return _diagnostics(errors)
