"""Deterministic, carry-forward-ready warehouse simulation state.

The geometry model is immutable evidence of capacity.  This module builds the
separate dynamic view of authoritative box stock and explicitly known (or
unknown) physical occupancy.  It deliberately performs no event execution.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from warehouse_business_identity import (
    CANONICAL_BOX_UNIT,
    canonical_sku_key,
    normalize_unit_name,
    normalize_warehouse,
    validate_box_quantity,
)

SIMULATION_STATE_VERSION = 3
POSITION_STATUSES = frozenset({"free", "occupied", "unknown"})

LIMITATIONS = [
    "simulation_state_uses_canonical_sku_v2",
    "authoritative_operational_quantity_is_boxes",
    "opening_inventory_quantity_is_authoritative",
    "opening_snapshot_location_is_evidence",
    "physical_pallet_count_is_not_inferred_from_boxes",
    "unknown_pallet_count_is_never_treated_as_zero",
    "unknown_location_stock_is_preserved",
    "normal_single_position_occupancy_can_be_exact_from_positive_location_evidence",
    "deep_lane_opening_occupancy_count_is_unknown_without_pallet_evidence",
    "deep_lane_depth_assignment_is_not_inferred",
    "location_role_is_unassigned",
    "production_date_quantity_split_is_not_inferred",
    "no_receipt_events_are_applied",
    "no_outbound_events_are_applied",
    "no_pallet_release_is_applied",
    "no_dynamic_routing_overlay_is_calculated",
    "no_replenishment_is_modeled",
    "state_is_pure_and_not_persisted",
]

_DIAGNOSTIC_NAMES = (
    "invalid_sku_identity unsupported_unit invalid_box_quantity "
    "zero_quantity_opening_placement unknown_location_stock unknown_cell_reference "
    "physical_slot_contract_invalid duplicate_position_id duplicate_stock_lot_id "
    "multiple_stock_lots_single_position multiple_sku_deep_lane "
    "occupancy_not_authoritative stock_conservation_failed"
).split()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _cell_key(cell: Mapping[str, Any]) -> str:
    return _text(cell.get("cell_key")) or "|".join(
        (_text(cell.get("row_number")), _text(cell.get("cell_number")), _text(cell.get("tier") or "1"))
    )


def _lot_from_opening(record: Mapping[str, Any], *, located: bool) -> tuple[dict[str, Any] | None, str | None]:
    quantity, error = validate_box_quantity(record.get("qty_units"))
    if error:
        return None, "invalid_box_quantity"
    if quantity == 0:
        return None, "zero_quantity_opening_placement"
    sku_key = canonical_sku_key(record)
    if not sku_key or (record.get("sku_key") and record.get("sku_key") != sku_key):
        return None, "invalid_sku_identity"
    if normalize_unit_name(record.get("unit_name")) != CANONICAL_BOX_UNIT:
        return None, "unsupported_unit"
    production_dates = sorted({_text(value) for value in (record.get("production_dates") or []) if _text(value)})
    cell_key = _text(record.get("cell_key")) if located else None
    lot = {
        "sku_key": sku_key,
        "nomenclature": _text(record.get("nomenclature") or record.get("sku_name") or record.get("item_name")),
        "characteristic": _text(record.get("characteristic") or record.get("characteristic_name")),
        "qty_boxes": quantity,
        "unit_name": CANONICAL_BOX_UNIT,
        "location_status": "located" if located else "unknown",
        "cell_key": cell_key,
        "location_role": "unassigned",
        "production_dates": production_dates,
        "source": "opening_stock",
        "location_confidence": _text(record.get("confidence") or record.get("allocation_confidence")),
        "allocation_method": _text(record.get("allocation_method")),
        "source_placement_id": _text(record.get("placement_id")) or None,
        "pallet_unit_id": None,
        "position_id": None,
        "pallet_count": None,
        "pallet_count_status": "unknown",
    }
    identity = {key: lot[key] for key in (
        "sku_key", "source", "cell_key", "production_dates", "allocation_method", "source_placement_id"
    )}
    lot["stock_lot_id"] = _sha256(identity)
    return lot, None


def _state_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: state.get(key) for key in (
        "simulation_state_version", "model_id", "target_normalized_warehouse", "simulation_time",
        "stock_lots", "pallet_units", "physical_positions", "cell_occupancy", "unresolved_stock", "applied_event_ids",
    )}


def compute_simulation_state_id(state: Mapping[str, Any]) -> str:
    """Compute the business identity, excluding diagnostics and daily metadata."""
    return _sha256(_state_identity(state))


def summarize_simulation_state(state: Mapping[str, Any]) -> dict[str, Any]:
    lots = state.get("stock_lots", []) or []
    positions = state.get("physical_positions", []) or []
    occupancy = state.get("cell_occupancy", []) or []
    readiness = state.get("readiness", {}) or {}
    located = [lot for lot in lots if lot.get("location_status") == "located"]
    unknown = [lot for lot in lots if lot.get("location_status") == "unknown"]
    normal = [cell for cell in occupancy if cell.get("storage_type") != "deep_lane"]
    deep = [cell for cell in occupancy if cell.get("storage_type") == "deep_lane"]
    pallets = state.get("pallet_units", []) or []
    palletized_boxes = sum(unit.get("remaining_boxes", 0) for unit in pallets
                           if unit.get("physical_status") == "active")
    total_boxes = sum(lot.get("qty_boxes", 0) for lot in lots)
    return {
        "stock_lots": len(lots), "sku_count": len({lot.get("sku_key") for lot in lots}),
        "total_boxes": total_boxes,
        "located_stock_lots": len(located), "located_boxes": sum(lot["qty_boxes"] for lot in located),
        "unknown_location_stock_lots": len(unknown), "unknown_location_boxes": sum(lot["qty_boxes"] for lot in unknown),
        "physical_positions_total": len(positions),
        "positions_occupied": sum(p.get("status") == "occupied" for p in positions),
        "positions_free": sum(p.get("status") == "free" for p in positions),
        "positions_unknown": sum(p.get("status") == "unknown" for p in positions),
        "physical_positions_occupied": sum(p.get("status") == "occupied" for p in positions),
        "physical_positions_free": sum(p.get("status") == "free" for p in positions),
        "physical_positions_unknown": sum(p.get("status") == "unknown" for p in positions),
        "pallet_units_total": len(pallets),
        "active_pallet_units": sum(p.get("physical_status") == "active" for p in pallets),
        "depleted_pallet_units": sum(p.get("physical_status") == "depleted" for p in pallets),
        "unassigned_pallet_units": sum(p.get("physical_status") == "active" and p.get("location_status") == "unassigned" for p in pallets),
        "exact_positioned_pallet_units": sum(p.get("physical_status") == "active" and p.get("location_status") == "located" for p in pallets),
        "full_pallet_units": sum(not p.get("is_partial") for p in pallets),
        "partial_pallet_units": sum(bool(p.get("is_partial")) for p in pallets),
        "palletized_boxes": palletized_boxes,
        "palletized_stock_boxes": palletized_boxes,
        "unpalletized_boxes": total_boxes - palletized_boxes,
        "non_palletized_stock_boxes": total_boxes - palletized_boxes,
        "palletization_rule_coverage_boxes_percent": (100.0 * palletized_boxes / total_boxes) if total_boxes else 100.0,
        "positions_released_total": state.get("positions_released_total", 0),
        "normal_cells_occupied": sum(c.get("exact_occupied_positions") == 1 for c in normal),
        "normal_cells_free": sum(c.get("exact_occupied_positions") == 0 for c in normal),
        "deep_lane_cells_total": len(deep),
        "deep_lane_cells_with_stock": sum(bool(c.get("stock_lot_ids")) for c in deep),
        "deep_lane_cells_unknown_occupancy": sum(c.get("exact_occupied_positions") is None for c in deep),
        "occupancy_conflicts": sum(c.get("occupancy_conflict", False) for c in occupancy),
        "stock_ready": bool(readiness.get("stock_ready")),
        "physical_occupancy_ready": bool(readiness.get("physical_occupancy_ready")),
        "capacity_sensitive_placement_ready": bool(readiness.get("capacity_sensitive_placement_ready")),
        "stock_conservation_ok": bool(state.get("stock_conservation", {}).get("stock_conservation_ok")),
    }


def rebuild_simulation_state_views(model: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Derive occupancy and unresolved-stock views from active lots, without mutation."""
    lots = list(state.get("stock_lots", []) or [])
    pallets = list(state.get("pallet_units", []) or [])
    cells = sorted((model.get("cells", []) or []), key=_cell_key)
    cell_by_key = {_cell_key(cell): cell for cell in cells}
    by_cell: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    diagnostics = {name: 0 for name in _DIAGNOSTIC_NAMES}
    configuration_errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for lot in lots:
        if lot.get("location_status") == "located":
            if lot.get("cell_key") not in cell_by_key:
                diagnostics["unknown_cell_reference"] += 1
                configuration_errors.append({"reason": "unknown_cell_reference", "cell_key": lot.get("cell_key")})
            else:
                by_cell[lot["cell_key"]].append(lot)
    has_unknown = any(lot.get("location_status") == "unknown" for lot in lots)
    active_positioned = {p.get("position_id"): p for p in pallets
                         if p.get("physical_status") == "active" and p.get("location_status") == "located"}
    positions, occupancies = [], []
    for cell in cells:
        key, cell_lots = _cell_key(cell), by_cell.get(_cell_key(cell), [])
        raw_capacity = cell.get("capacity_pallets", 1)
        capacity = raw_capacity if isinstance(raw_capacity, int) and not isinstance(raw_capacity, bool) else -1
        storage_type = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
        deep = storage_type == "deep_lane" or capacity > 1
        valid = capacity >= 1
        slots: list[tuple[int, Mapping[str, Any]]] = []
        if deep:
            raw_slots = cell.get("physical_slots", []) or []
            valid = valid and len(raw_slots) == capacity
            for fallback, slot in enumerate(raw_slots, 1):
                index = slot.get("slot_index", fallback)
                valid = valid and isinstance(index, int) and not isinstance(index, bool) and index >= 1
                slots.append((index, slot))
            valid = valid and len({index for index, _ in slots}) == len(slots)
        else:
            valid = valid and capacity == 1
            slots = [(1, {})]
        if not valid:
            diagnostics["physical_slot_contract_invalid"] += 1
            configuration_errors.append({"reason": "physical_slot_contract_invalid", "cell_key": key})
            slots = []
        if deep and len({lot["sku_key"] for lot in cell_lots}) > 1:
            diagnostics["multiple_sku_deep_lane"] += 1
            warnings.append({"reason": "multiple_sku_deep_lane", "cell_key": key})
        conflict = not deep and len(cell_lots) > 1
        if conflict:
            diagnostics["multiple_stock_lots_single_position"] += 1
            configuration_errors.append({"reason": "multiple_stock_lots_single_position", "cell_key": key})
        exact_lots = [lot for lot in cell_lots if lot.get("pallet_unit_id") and lot.get("position_id")]
        legacy_lots = [lot for lot in cell_lots if not (lot.get("pallet_unit_id") and lot.get("position_id"))]
        exact_positions = {lot.get("position_id") for lot in exact_lots}
        legacy_receipt_allocation = any(lot.get("source") == "receipt_event" and not lot.get("position_id")
                                        for lot in cell_lots)
        if legacy_receipt_allocation and not deep:
            status, minimum, maximum, exact, occupancy_status = "unknown", 0, 1, None, "unknown"
        elif deep and legacy_lots:
            minimum = len(exact_positions) + 1
            if minimum > capacity:
                configuration_errors.append({"reason": "physical_position_capacity_exceeded", "cell_key": key})
                conflict = True
                minimum = capacity
            status, maximum, exact, occupancy_status = "unknown", capacity, None, "mixed_exact_unknown"
        elif deep and exact_lots:
            status = "exact"
            minimum = maximum = exact = len(exact_positions)
            occupancy_status = "occupied" if exact else "free"
        elif cell_lots and deep:
            status, minimum, maximum, exact, occupancy_status = "unknown", 1, capacity, None, "unknown_count"
        elif cell_lots:
            status, minimum, maximum, exact, occupancy_status = "occupied", 1, 1, 1, "occupied"
        elif has_unknown:
            status, minimum, maximum, exact, occupancy_status = "unknown", 0, capacity, None, "unknown"
        else:
            status, minimum, maximum, exact, occupancy_status = "free", 0, 0, 0, "free"
        if status == "unknown":
            diagnostics["occupancy_not_authoritative"] += 1
        for index, _ in sorted(slots):
            position_id = f"position:{key}:{index}"
            occupant = active_positioned.get(position_id)
            if occupant:
                position_status = "occupied"
            elif deep and legacy_lots:
                position_status = "unknown"
            elif status == "exact":
                position_status = "free"
            else:
                position_status = status
            occupied_ids = sorted(lot["stock_lot_id"] for lot in cell_lots
                                  if lot.get("position_id") == position_id)
            if position_status == "occupied" and not occupied_ids and not deep:
                occupied_ids = sorted(lot["stock_lot_id"] for lot in cell_lots)
            positions.append({"position_id": position_id, "cell_key": key, "slot_index": index,
                "depth_index": index if deep else None, "row_number": cell.get("row_number"),
                "cell_number": cell.get("cell_number"), "tier": cell.get("tier") or "1", "status": position_status,
                "occupied_stock_lot_ids": occupied_ids if position_status == "occupied" else [],
                "pallet_unit_id": occupant.get("pallet_unit_id") if occupant else None})
        occupancies.append({"cell_key": key, "storage_type": "deep_lane" if deep else "normal",
            "capacity_pallet_positions": max(capacity, 0), "stock_lot_ids": sorted(lot["stock_lot_id"] for lot in cell_lots),
            "qty_boxes": sum(lot["qty_boxes"] for lot in cell_lots), "occupancy_status": occupancy_status,
            "min_occupied_positions": minimum, "max_occupied_positions": max(maximum, 0),
            "exact_occupied_positions": exact, "capacity_available_for_new_receipt": exact == 0 and valid,
            "occupancy_conflict": conflict})
    positions.sort(key=lambda item: item["position_id"])
    unresolved = [{"stock_lot_id": lot["stock_lot_id"], "sku_key": lot["sku_key"], "qty_boxes": lot["qty_boxes"],
                   "reason": lot.get("unresolved_reason") or "unknown_location"}
                  for lot in lots if lot.get("location_status") == "unknown"]
    physical_ready = not has_unknown and not any(p["status"] == "unknown" for p in positions) and not any(
        diagnostics[name] for name in ("unknown_cell_reference", "physical_slot_contract_invalid", "multiple_stock_lots_single_position"))
    return {"physical_positions": positions, "cell_occupancy": occupancies, "unresolved_stock": unresolved,
            "physical_occupancy_ready": physical_ready, "diagnostics": diagnostics,
            "configuration_errors": configuration_errors, "warnings": warnings}


def refresh_simulation_state(model: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Return state with every derived view, conservation total, and ID rebuilt."""
    import copy
    result = copy.deepcopy(dict(state))
    result["stock_lots"] = sorted(result.get("stock_lots", []), key=lambda lot: (lot["stock_lot_id"], _canonical_json(lot)))
    result["pallet_units"] = sorted(result.get("pallet_units", []), key=lambda pallet: pallet["pallet_unit_id"])
    view = rebuild_simulation_state_views(model, result)
    result["physical_positions"] = view["physical_positions"]
    result["cell_occupancy"] = view["cell_occupancy"]
    result["unresolved_stock"] = view["unresolved_stock"]
    conservation = result["stock_conservation"]
    total = sum(lot["qty_boxes"] for lot in result["stock_lots"])
    expected = conservation["opening_boxes_input"] + conservation["cumulative_receipt_boxes"] - conservation["cumulative_picked_boxes"]
    conservation.update({"expected_stock_boxes": expected, "stock_boxes_state": total, "stock_conservation_ok": expected == total})
    stock_ready = expected == total
    physical_ready = view["physical_occupancy_ready"]
    result["physical_capacity_status"] = "exact" if physical_ready else ("partial" if result["physical_positions"] else "unknown")
    result["readiness"] = {"stock_ready": stock_ready, "physical_occupancy_ready": physical_ready,
                           "capacity_sensitive_placement_ready": stock_ready and physical_ready}
    result["summary"] = summarize_simulation_state(result)
    result["simulation_state_id"] = compute_simulation_state_id(result)
    return result


def build_initial_simulation_state(
    model: dict[str, Any], opening_stock_state: dict[str, Any], *,
    target_normalized_warehouse: str, simulation_time: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Adapt reconciled opening stock into authoritative dynamic state."""
    diagnostics: dict[str, Any] = {name: 0 for name in _DIAGNOSTIC_NAMES}
    diagnostics.update({"configuration_errors": [], "warnings": []})
    lots: list[dict[str, Any]] = []
    opening_boxes = 0
    for located, records in ((True, opening_stock_state.get("placements", []) or []),
                             (False, opening_stock_state.get("unknown_location_inventory", []) or [])):
        for record in records:
            lot, error = _lot_from_opening(record, located=located)
            if error:
                diagnostics[error] += 1
                if error != "zero_quantity_opening_placement":
                    diagnostics["configuration_errors"].append({"reason": error})
                continue
            opening_boxes += lot["qty_boxes"]
            lots.append(lot)
            if not located:
                diagnostics["unknown_location_stock"] += 1
    lots.sort(key=lambda lot: (lot["stock_lot_id"], _canonical_json(lot)))
    duplicate_lots = [key for key, count in Counter(lot["stock_lot_id"] for lot in lots).items() if count > 1]
    diagnostics["duplicate_stock_lot_id"] = len(duplicate_lots)
    diagnostics["configuration_errors"].extend({"reason": "duplicate_stock_lot_id", "stock_lot_id": key} for key in duplicate_lots)

    view = rebuild_simulation_state_views(model, {"stock_lots": lots})
    positions = view["physical_positions"]
    occupancies = view["cell_occupancy"]
    for name, count in view["diagnostics"].items():
        diagnostics[name] += count
    diagnostics["configuration_errors"].extend(view["configuration_errors"])
    diagnostics["warnings"].extend(view["warnings"])
    has_unknown_stock = bool(view["unresolved_stock"])
    physical_ready = view["physical_occupancy_ready"]

    state_boxes = sum(lot["qty_boxes"] for lot in lots)
    conservation_ok = opening_boxes == state_boxes
    if not conservation_ok:
        diagnostics["stock_conservation_failed"] += 1
    stock_errors = {"invalid_sku_identity", "unsupported_unit", "invalid_box_quantity", "duplicate_stock_lot_id", "stock_conservation_failed"}
    stock_ready = conservation_ok and not any(diagnostics[name] for name in stock_errors)
    warehouse = normalize_warehouse(target_normalized_warehouse)
    state = {
        "simulation_state_version": SIMULATION_STATE_VERSION, "simulation_state_id": "",
        "model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"),
        "target_normalized_warehouse": warehouse, "simulation_time": simulation_time,
        "stock_lots": lots, "pallet_units": [], "physical_positions": positions, "cell_occupancy": occupancies,
        "unresolved_stock": view["unresolved_stock"],
        "applied_event_ids": [],
        "positions_released_total": 0,
        "stock_conservation": {"opening_boxes_input": opening_boxes, "cumulative_receipt_boxes": 0,
                               "cumulative_picked_boxes": 0, "expected_stock_boxes": opening_boxes,
                               "stock_boxes_state": state_boxes,
                               "stock_conservation_ok": conservation_ok},
        "physical_capacity_status": "exact" if physical_ready else ("partial" if positions else "unknown"),
        "readiness": {"stock_ready": stock_ready, "physical_occupancy_ready": physical_ready,
                      "capacity_sensitive_placement_ready": physical_ready and stock_ready},
        "summary": {}, "limitations": list(LIMITATIONS),
    }
    state["summary"] = summarize_simulation_state(state)
    state["simulation_state_id"] = compute_simulation_state_id(state)
    return state, diagnostics


def validate_simulation_state(state: Mapping[str, Any], model: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return validation findings without mutating state or model."""
    errors: list[dict[str, Any]] = []
    def error(reason: str, **details: Any) -> None:
        errors.append({"reason": reason, **details})
    if state.get("simulation_state_version") != SIMULATION_STATE_VERSION:
        error("invalid_simulation_state_version")
    if normalize_warehouse(state.get("target_normalized_warehouse")) != state.get("target_normalized_warehouse") or not state.get("target_normalized_warehouse"):
        error("invalid_normalized_warehouse")
    if state.get("simulation_state_id") != compute_simulation_state_id(state):
        error("simulation_state_id_mismatch")
    lots = state.get("stock_lots", []) or []
    lot_ids = [lot.get("stock_lot_id") for lot in lots]
    if len(lot_ids) != len(set(lot_ids)):
        error("duplicate_stock_lot_id")
    model_cells = {_cell_key(cell): cell for cell in (model.get("cells", []) or [])} if model else None
    for lot in lots:
        if not str(lot.get("sku_key", "")).startswith("sku:v2:") or canonical_sku_key(lot) != lot.get("sku_key"):
            error("invalid_sku_identity", stock_lot_id=lot.get("stock_lot_id"))
        quantity, quantity_error = validate_box_quantity(lot.get("qty_boxes"), positive=True)
        if quantity_error:
            error("invalid_box_quantity", stock_lot_id=lot.get("stock_lot_id"))
        if normalize_unit_name(lot.get("unit_name")) != CANONICAL_BOX_UNIT or lot.get("unit_name") != CANONICAL_BOX_UNIT:
            error("unsupported_unit", stock_lot_id=lot.get("stock_lot_id"))
        linked = bool(lot.get("pallet_unit_id"))
        valid_pallet_count = ((lot.get("pallet_count") is None and lot.get("pallet_count_status") == "unknown")
                              or (linked and lot.get("pallet_count") == 1 and lot.get("pallet_count_status") == "exact"))
        if not valid_pallet_count:
            error("invalid_pallet_count_authority", stock_lot_id=lot.get("stock_lot_id"))
        if lot.get("location_status") == "located" and model_cells is not None and lot.get("cell_key") not in model_cells:
            error("unknown_cell_reference", cell_key=lot.get("cell_key"))
    positions = state.get("physical_positions", []) or []
    position_ids = [position.get("position_id") for position in positions]
    if len(position_ids) != len(set(position_ids)):
        error("duplicate_position_id")
    for position in positions:
        if position.get("status") not in POSITION_STATUSES:
            error("invalid_position_status", position_id=position.get("position_id"))
        if model_cells is not None and position.get("cell_key") not in model_cells:
            error("unknown_cell_reference", cell_key=position.get("cell_key"))
        if any(lot_id not in lot_ids for lot_id in position.get("occupied_stock_lot_ids", []) or []):
            error("invalid_occupied_stock_lot_reference", position_id=position.get("position_id"))
    position_by_id = {position.get("position_id"): position for position in positions}
    pallets = state.get("pallet_units", []) or []
    pallet_ids = [pallet.get("pallet_unit_id") for pallet in pallets]
    if len(pallet_ids) != len(set(pallet_ids)):
        error("duplicate_pallet_unit_id")
    pallet_by_id = {pallet.get("pallet_unit_id"): pallet for pallet in pallets}
    active_positions: list[str] = []
    for pallet in pallets:
        pallet_id = pallet.get("pallet_unit_id")
        if not str(pallet.get("sku_key", "")).startswith("sku:v2:"):
            error("invalid_pallet_unit_sku", pallet_unit_id=pallet_id)
        capacity, capacity_error = validate_box_quantity(pallet.get("capacity_boxes"), positive=True)
        initial, initial_error = validate_box_quantity(pallet.get("initial_boxes"), positive=True)
        remaining = pallet.get("remaining_boxes")
        if capacity_error or initial_error or initial > capacity:
            error("invalid_pallet_unit_capacity", pallet_unit_id=pallet_id)
        if (not isinstance(remaining, int) or isinstance(remaining, bool) or
                (not initial_error and not 0 <= remaining <= initial)):
            error("invalid_pallet_unit_remaining_boxes", pallet_unit_id=pallet_id)
            continue
        expected_status = "active" if remaining > 0 else "depleted"
        if pallet.get("physical_status") != expected_status:
            error("invalid_pallet_unit_physical_status", pallet_unit_id=pallet_id)
        if pallet.get("location_status") == "located" and remaining > 0:
            position = position_by_id.get(pallet.get("position_id"))
            if not position:
                error("invalid_pallet_position", pallet_unit_id=pallet_id)
            else:
                active_positions.append(pallet["position_id"])
                if position.get("cell_key") != pallet.get("cell_key"):
                    error("pallet_position_cell_mismatch", pallet_unit_id=pallet_id)
                if position.get("status") != "occupied":
                    error("active_pallet_at_non_occupied_position", pallet_unit_id=pallet_id)
    if len(active_positions) != len(set(active_positions)):
        error("multiple_active_pallets_same_position")
    for lot in lots:
        pallet_id = lot.get("pallet_unit_id")
        if not pallet_id:
            continue
        pallet = pallet_by_id.get(pallet_id)
        if not pallet:
            error("invalid_stock_lot_pallet_reference", stock_lot_id=lot.get("stock_lot_id"))
        elif (lot.get("sku_key") != pallet.get("sku_key") or lot.get("qty_boxes") != pallet.get("remaining_boxes")
              or lot.get("position_id") != pallet.get("position_id") or lot.get("cell_key") != pallet.get("cell_key")):
            error("stock_lot_pallet_link_mismatch", stock_lot_id=lot.get("stock_lot_id"))
    occupancy = state.get("cell_occupancy", []) or []
    if len([cell.get("cell_key") for cell in occupancy]) != len({cell.get("cell_key") for cell in occupancy}):
        error("duplicate_cell_occupancy")
    position_counts = Counter(position.get("cell_key") for position in positions)
    for cell in occupancy:
        key, capacity = cell.get("cell_key"), cell.get("capacity_pallet_positions")
        if position_counts[key] != capacity and not (position_counts[key] == 0 and capacity > 0):
            error("physical_slot_contract_invalid", cell_key=key)
        if not (0 <= cell.get("min_occupied_positions", -1) <= cell.get("max_occupied_positions", -1) <= capacity):
            error("invalid_cell_occupancy_constraints", cell_key=key)
        if sum(lot.get("qty_boxes", 0) for lot in lots if lot.get("stock_lot_id") in cell.get("stock_lot_ids", [])) != cell.get("qty_boxes"):
            error("cell_occupancy_box_total_mismatch", cell_key=key)
    if model_cells is not None:
        occupancy_by_key = {cell.get("cell_key"): cell for cell in occupancy}
        for key, geometry_cell in model_cells.items():
            capacity = geometry_cell.get("capacity_pallets", 1)
            storage_type = _text(geometry_cell.get("storage_type") or geometry_cell.get("row_storage_type") or "normal")
            deep_lane = storage_type == "deep_lane" or (isinstance(capacity, int) and capacity > 1)
            slots = geometry_cell.get("physical_slots", []) or []
            geometry_valid = isinstance(capacity, int) and not isinstance(capacity, bool) and capacity >= 1
            geometry_valid = geometry_valid and ((deep_lane and len(slots) == capacity) or (not deep_lane and capacity == 1))
            if deep_lane:
                indexes = [slot.get("slot_index", index) for index, slot in enumerate(slots, 1)]
                geometry_valid = geometry_valid and all(isinstance(index, int) and not isinstance(index, bool) and index >= 1 for index in indexes)
                geometry_valid = geometry_valid and len(indexes) == len(set(indexes))
            expected_positions = capacity if geometry_valid else 0
            if position_counts[key] != expected_positions:
                error("physical_slot_contract_invalid", cell_key=key)
            dynamic_cell = occupancy_by_key.get(key)
            if dynamic_cell is None or dynamic_cell.get("capacity_pallet_positions") != capacity:
                error("cell_occupancy_capacity_mismatch", cell_key=key)
    events = state.get("applied_event_ids", []) or []
    if len(events) != len(set(events)):
        error("duplicate_applied_event_id")
    conservation = state.get("stock_conservation", {}) or {}
    total = sum(lot.get("qty_boxes", 0) for lot in lots if isinstance(lot.get("qty_boxes"), int) and not isinstance(lot.get("qty_boxes"), bool))
    opening = conservation.get("opening_boxes_input")
    receipts = conservation.get("cumulative_receipt_boxes")
    picked = conservation.get("cumulative_picked_boxes")
    valid_counters = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                         for value in (opening, receipts, picked))
    expected = opening + receipts - picked if valid_counters else None
    if (not valid_counters or expected < 0 or conservation.get("expected_stock_boxes") != expected
            or conservation.get("stock_boxes_state") != total
            or conservation.get("stock_conservation_ok") != (expected == total)):
        error("stock_conservation_metadata_mismatch")
    palletized = sum(pallet.get("remaining_boxes", 0) for pallet in pallets
                     if pallet.get("physical_status") == "active")
    linked_total = sum(lot.get("qty_boxes", 0) for lot in lots if lot.get("pallet_unit_id"))
    if palletized != linked_total or palletized > total:
        error("pallet_stock_conservation_mismatch")
    return {"valid": not errors, "errors": errors}
