"""Transactional materialization of a proposed target layout.

This module does not make placement decisions or execute warehouse events.  It
only validates and atomically applies an existing ``ProposedPlacementPlan`` to
a deep copy of its factual baseline ``SimulationState``.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from warehouse_proposed_placement_optimizer import (
    compute_proposed_placement_plan_id,
    validate_proposed_placement_plan,
)
from warehouse_simulation_state import refresh_simulation_state, validate_simulation_state


_LIMITATIONS = [
    "logical_target_layout_only",
    "no_relocation_sequence_or_distance",
    "deep_lane_target_is_counterfactual_not_relocation_sequence",
    "no_deep_lane_replenishment",
    "no_event_execution",
]


def _blocked(code: str, *, baseline_id: Any = None, plan_id: Any = None,
             validation: Mapping[str, Any] | None = None) -> tuple[None, dict[str, Any]]:
    return None, {
        "status": "blocked",
        "baseline_state_id": baseline_id,
        "proposed_placement_plan_id": plan_id,
        "proposed_state_id": None,
        "blocked_reasons": [{"code": code}],
        "validation": copy.deepcopy(dict(validation or {"valid": False, "errors": [{"code": code}]})),
        "limitations": list(_LIMITATIONS),
    }


def _boxes(state: Mapping[str, Any]) -> int | float:
    return sum(lot.get("qty_boxes", 0) for lot in state.get("stock_lots", []) or [])


def _occupied(state: Mapping[str, Any]) -> int:
    return sum(position.get("status") == "occupied" for position in state.get("physical_positions", []) or [])


def apply_proposed_placement_plan(
    model: dict[str, Any],
    baseline_state: dict[str, Any],
    proposed_placement_plan: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Apply an exact target layout to an independent state, or fail closed."""
    baseline_id = baseline_state.get("simulation_state_id") if isinstance(baseline_state, Mapping) else None
    plan_id = (proposed_placement_plan.get("proposed_placement_plan_id")
               if isinstance(proposed_placement_plan, Mapping) else None)
    if not isinstance(model, Mapping) or not isinstance(baseline_state, Mapping):
        return _blocked("invalid_apply_input", baseline_id=baseline_id, plan_id=plan_id)
    if not isinstance(proposed_placement_plan, Mapping):
        return _blocked("invalid_proposed_placement_plan", baseline_id=baseline_id, plan_id=plan_id)

    baseline_validation = validate_simulation_state(baseline_state, model)
    if not baseline_validation.get("valid"):
        return _blocked("invalid_baseline_simulation_state", baseline_id=baseline_id,
                        plan_id=plan_id, validation=baseline_validation)
    status = proposed_placement_plan.get("status")
    if status not in {"ready", "partial"}:
        return _blocked("proposed_placement_plan_not_applicable", baseline_id=baseline_id, plan_id=plan_id)
    if proposed_placement_plan.get("baseline_state_id") != baseline_id:
        return _blocked("baseline_state_id_mismatch", baseline_id=baseline_id, plan_id=plan_id)
    if proposed_placement_plan.get("model_id") != model.get("model_id"):
        return _blocked("model_id_mismatch", baseline_id=baseline_id, plan_id=plan_id)
    if baseline_state.get("model_id") != model.get("model_id"):
        return _blocked("baseline_model_id_mismatch", baseline_id=baseline_id, plan_id=plan_id)
    if proposed_placement_plan.get("target_normalized_warehouse") != baseline_state.get("target_normalized_warehouse"):
        return _blocked("target_normalized_warehouse_mismatch", baseline_id=baseline_id, plan_id=plan_id)
    if plan_id != compute_proposed_placement_plan_id(proposed_placement_plan):
        return _blocked("proposed_placement_plan_id_mismatch", baseline_id=baseline_id, plan_id=plan_id)

    cells_by_key = {cell.get("cell_key"): cell for cell in model.get("cells", []) or []}
    for row in proposed_placement_plan.get("placements", []) or []:
        target_cell = cells_by_key.get(row.get("target_cell_key"), {})
        target_storage = target_cell.get("storage_type") or target_cell.get("row_storage_type") or "normal"
        if row.get("moved") and target_storage == "deep_lane" and row.get("unit_type") != "pallet":
            return _blocked("deep_lane_unit_moved", baseline_id=baseline_id, plan_id=plan_id)

    plan_validation = validate_proposed_placement_plan(
        proposed_placement_plan, model, baseline_state
    )
    if not plan_validation.get("valid"):
        return _blocked("invalid_proposed_placement_plan", baseline_id=baseline_id,
                        plan_id=plan_id, validation=plan_validation)

    positions = {row.get("position_id"): row for row in baseline_state.get("physical_positions", []) or []}
    cells = {cell.get("cell_key"): cell for cell in model.get("cells", []) or []}
    lots = {lot.get("stock_lot_id"): lot for lot in baseline_state.get("stock_lots", []) or []}
    pallets = {unit.get("pallet_unit_id"): unit for unit in baseline_state.get("pallet_units", []) or []}
    placements = proposed_placement_plan.get("placements", []) or []
    origins = {row.get("origin_position_id") for row in placements}
    targets: set[Any] = set()
    mappings: list[tuple[str, str, list[str], Any, Any, Any]] = []

    for row in placements:
        unit_type = row.get("unit_type")
        origin_id, target_id = row.get("origin_position_id"), row.get("target_position_id")
        origin, target = positions.get(origin_id), positions.get(target_id)
        if target_id in targets:
            return _blocked("duplicate_target_position_id", baseline_id=baseline_id, plan_id=plan_id)
        targets.add(target_id)
        if not origin or not target or target.get("status") == "unknown":
            code = "unknown_target_position" if target and target.get("status") == "unknown" else "invalid_position_mapping"
            return _blocked(code, baseline_id=baseline_id, plan_id=plan_id)
        origin_cell, target_cell = cells.get(origin.get("cell_key"), {}), cells.get(target.get("cell_key"), {})
        origin_storage = origin_cell.get("storage_type") or origin_cell.get("row_storage_type") or "normal"
        target_storage = target_cell.get("storage_type") or target_cell.get("row_storage_type") or "normal"
        if (origin_storage == "deep_lane" or target_storage == "deep_lane") and unit_type != "pallet":
            return _blocked("deep_lane_requires_authoritative_pallet", baseline_id=baseline_id, plan_id=plan_id)
        if row.get("moved") and target.get("status") == "occupied" and target_id not in origins:
            return _blocked("fixed_reserved_position_reused", baseline_id=baseline_id, plan_id=plan_id)
        lot_ids = row.get("stock_lot_ids")
        if not isinstance(lot_ids, list) or len(lot_ids) != 1 or lot_ids[0] not in lots:
            return _blocked("invalid_linked_stock_lots", baseline_id=baseline_id, plan_id=plan_id)
        lot = lots[lot_ids[0]]
        if lot.get("sku_key") != row.get("sku_key") or lot.get("cell_key") != row.get("origin_cell_key"):
            return _blocked("placement_unit_origin_mismatch", baseline_id=baseline_id, plan_id=plan_id)
        if unit_type == "pallet":
            pallet_id = row.get("pallet_unit_id")
            pallet = pallets.get(pallet_id)
            linked = [item for item in lots.values() if item.get("pallet_unit_id") == pallet_id]
            if (not pallet or pallet_id != row.get("placement_unit_id") or len(linked) != 1
                    or linked[0].get("stock_lot_id") != lot_ids[0]
                    or pallet.get("physical_status") != "active"
                    or pallet.get("location_status") != "located"
                    or pallet.get("position_id") != origin_id or pallet.get("cell_key") != row.get("origin_cell_key")
                    or lot.get("position_id") != origin_id):
                return _blocked("invalid_pallet_placement_unit", baseline_id=baseline_id, plan_id=plan_id)
        elif unit_type == "opaque_opening_position":
            if (row.get("pallet_unit_id") is not None or lot.get("pallet_unit_id") is not None
                    or origin.get("status") != "occupied" or lot.get("location_status") != "located"):
                return _blocked("invalid_opaque_opening_placement_unit", baseline_id=baseline_id, plan_id=plan_id)
        else:
            return _blocked("unsupported_placement_unit_type", baseline_id=baseline_id, plan_id=plan_id)
        mappings.append((unit_type, row.get("pallet_unit_id"), lot_ids, target_id,
                         row.get("target_cell_key"), row.get("stock_role")))

    # Mutation starts only after every mapping and final-position constraint passed.
    proposed = copy.deepcopy(dict(baseline_state))
    proposed_lots = {lot["stock_lot_id"]: lot for lot in proposed.get("stock_lots", [])}
    proposed_pallets = {unit["pallet_unit_id"]: unit for unit in proposed.get("pallet_units", [])}
    for unit_type, pallet_id, lot_ids, target_id, target_cell, stock_role in mappings:
        lot = proposed_lots[lot_ids[0]]
        lot["cell_key"] = target_cell
        lot["location_status"] = "located"
        if stock_role in {"picking", "storage"}:
            lot["location_role"] = stock_role
        if unit_type == "pallet":
            pallet = proposed_pallets[pallet_id]
            pallet.update(position_id=target_id, cell_key=target_cell, location_status="located")
            if stock_role in {"picking", "storage"}:
                pallet["placement_role"] = stock_role
            lot["position_id"] = target_id
        else:
            lot["position_id"] = None
    proposed = refresh_simulation_state(model, proposed)
    result_validation = validate_simulation_state(proposed, model)
    boxes_before, boxes_after = _boxes(baseline_state), _boxes(proposed)
    invariant_ok = (
        boxes_before == boxes_after
        and proposed.get("applied_event_ids") == baseline_state.get("applied_event_ids")
        and proposed.get("simulation_time") == baseline_state.get("simulation_time")
        and proposed.get("positions_released_total") == baseline_state.get("positions_released_total")
        and proposed.get("stock_conservation") == baseline_state.get("stock_conservation")
    )
    if not result_validation.get("valid") or not invariant_ok:
        validation = copy.deepcopy(result_validation)
        if not invariant_ok:
            validation.setdefault("errors", []).append({"code": "counterfactual_invariant_failed"})
            validation["valid"] = False
        return _blocked("invalid_proposed_simulation_state", baseline_id=baseline_id,
                        plan_id=plan_id, validation=validation)

    moved_rows = [row for row in placements if row.get("moved")]
    placement_units_total = proposed_placement_plan.get("summary", {}).get("placement_units_total", len(placements))
    report = {
        "status": "applied_partial" if status == "partial" else "applied",
        "baseline_state_id": baseline_id,
        "proposed_placement_plan_id": plan_id,
        "proposed_state_id": proposed.get("simulation_state_id"),
        "placement_units_total": placement_units_total,
        "units_kept": placement_units_total - len(moved_rows),
        "units_moved": len(moved_rows),
        "pallet_units_moved": sum(row.get("unit_type") == "pallet" for row in moved_rows),
        "opaque_opening_units_moved": sum(row.get("unit_type") == "opaque_opening_position" for row in moved_rows),
        "boxes_before": boxes_before,
        "boxes_after": boxes_after,
        "box_conservation_ok": boxes_before == boxes_after and proposed["stock_conservation"]["stock_conservation_ok"],
        "positions_occupied_before": _occupied(baseline_state),
        "positions_occupied_after": _occupied(proposed),
        "applied_event_ids_unchanged": proposed.get("applied_event_ids") == baseline_state.get("applied_event_ids"),
        "simulation_time_unchanged": proposed.get("simulation_time") == baseline_state.get("simulation_time"),
        "validation": result_validation,
        "limitations": list(_LIMITATIONS),
    }
    return proposed, report
