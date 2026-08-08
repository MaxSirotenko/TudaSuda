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

SIMULATION_STATE_VERSION = 1
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
        "pallet_count": None,
        "pallet_count_status": "unknown",
    }
    identity = {key: lot[key] for key in (
        "sku_key", "source", "cell_key", "qty_boxes", "production_dates", "allocation_method", "source_placement_id"
    )}
    lot["stock_lot_id"] = _sha256(identity)
    return lot, None


def _state_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: state.get(key) for key in (
        "simulation_state_version", "model_id", "target_normalized_warehouse", "simulation_time",
        "stock_lots", "physical_positions", "cell_occupancy", "unresolved_stock", "applied_event_ids",
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
    return {
        "stock_lots": len(lots), "sku_count": len({lot.get("sku_key") for lot in lots}),
        "total_boxes": sum(lot.get("qty_boxes", 0) for lot in lots),
        "located_stock_lots": len(located), "located_boxes": sum(lot["qty_boxes"] for lot in located),
        "unknown_location_stock_lots": len(unknown), "unknown_location_boxes": sum(lot["qty_boxes"] for lot in unknown),
        "physical_positions_total": len(positions),
        "positions_occupied": sum(p.get("status") == "occupied" for p in positions),
        "positions_free": sum(p.get("status") == "free" for p in positions),
        "positions_unknown": sum(p.get("status") == "unknown" for p in positions),
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

    cells = sorted((model.get("cells", []) or []), key=lambda cell: _cell_key(cell))
    cell_by_key = {_cell_key(cell): cell for cell in cells}
    by_cell: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lot in lots:
        if lot["location_status"] == "located":
            if lot["cell_key"] not in cell_by_key:
                diagnostics["unknown_cell_reference"] += 1
                diagnostics["configuration_errors"].append({"reason": "unknown_cell_reference", "cell_key": lot["cell_key"]})
            else:
                by_cell[lot["cell_key"]].append(lot)
    has_unknown_stock = any(lot["location_status"] == "unknown" for lot in lots)
    positions: list[dict[str, Any]] = []
    occupancies: list[dict[str, Any]] = []
    for cell in cells:
        key = _cell_key(cell)
        cell_lots = by_cell.get(key, [])
        capacity_raw = cell.get("capacity_pallets", 1)
        capacity = capacity_raw if isinstance(capacity_raw, int) and not isinstance(capacity_raw, bool) else -1
        storage_type = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
        is_deep = storage_type == "deep_lane" or capacity > 1
        contract_valid = capacity >= 1
        slot_specs: list[tuple[int, Mapping[str, Any]]] = []
        if is_deep:
            raw_slots = cell.get("physical_slots", []) or []
            contract_valid = contract_valid and len(raw_slots) == capacity
            for fallback, slot in enumerate(raw_slots, 1):
                index = slot.get("slot_index", fallback)
                if isinstance(index, bool) or not isinstance(index, int) or index < 1:
                    contract_valid = False
                slot_specs.append((index, slot))
            if len({index for index, _ in slot_specs}) != len(slot_specs):
                contract_valid = False
        else:
            contract_valid = contract_valid and capacity == 1
            slot_specs = [(1, {})]
        if not contract_valid:
            diagnostics["physical_slot_contract_invalid"] += 1
            diagnostics["configuration_errors"].append({"reason": "physical_slot_contract_invalid", "cell_key": key})
            slot_specs = []
        if is_deep and len({lot["sku_key"] for lot in cell_lots}) > 1:
            diagnostics["multiple_sku_deep_lane"] += 1
            diagnostics["warnings"].append({"reason": "multiple_sku_deep_lane", "cell_key": key})
        conflict = not is_deep and len(cell_lots) > 1
        if conflict:
            diagnostics["multiple_stock_lots_single_position"] += 1
            diagnostics["configuration_errors"].append({"reason": "multiple_stock_lots_single_position", "cell_key": key})
        if cell_lots and is_deep:
            status, minimum, maximum, exact, occupancy_status = "unknown", 1, capacity, None, "unknown_count"
        elif cell_lots:
            status, minimum, maximum, exact, occupancy_status = "occupied", 1, 1, 1, "occupied"
        elif has_unknown_stock:
            status, minimum, maximum, exact, occupancy_status = "unknown", 0, capacity, None, "unknown"
        else:
            status, minimum, maximum, exact, occupancy_status = "free", 0, 0, 0, "free"
        if status == "unknown":
            diagnostics["occupancy_not_authoritative"] += 1
        for index, _slot in sorted(slot_specs, key=lambda item: item[0]):
            positions.append({
                "position_id": f"position:{key}:{index}", "cell_key": key, "slot_index": index,
                "depth_index": index if is_deep else None, "row_number": cell.get("row_number"),
                "cell_number": cell.get("cell_number"), "tier": cell.get("tier") or "1", "status": status,
                "occupied_stock_lot_ids": sorted(lot["stock_lot_id"] for lot in cell_lots) if status == "occupied" else [],
            })
        occupancies.append({
            "cell_key": key, "storage_type": "deep_lane" if is_deep else "normal",
            "capacity_pallet_positions": max(capacity, 0),
            "stock_lot_ids": sorted(lot["stock_lot_id"] for lot in cell_lots),
            "qty_boxes": sum(lot["qty_boxes"] for lot in cell_lots), "occupancy_status": occupancy_status,
            "min_occupied_positions": minimum, "max_occupied_positions": max(maximum, 0),
            "exact_occupied_positions": exact,
            "capacity_available_for_new_receipt": exact == 0 and contract_valid,
            "occupancy_conflict": conflict,
        })
    positions.sort(key=lambda position: position["position_id"])
    duplicate_positions = [key for key, count in Counter(p["position_id"] for p in positions).items() if count > 1]
    diagnostics["duplicate_position_id"] = len(duplicate_positions)
    diagnostics["configuration_errors"].extend({"reason": "duplicate_position_id", "position_id": key} for key in duplicate_positions)

    state_boxes = sum(lot["qty_boxes"] for lot in lots)
    conservation_ok = opening_boxes == state_boxes
    if not conservation_ok:
        diagnostics["stock_conservation_failed"] += 1
    stock_errors = {"invalid_sku_identity", "unsupported_unit", "invalid_box_quantity", "duplicate_stock_lot_id", "stock_conservation_failed"}
    stock_ready = conservation_ok and not any(diagnostics[name] for name in stock_errors)
    physical_ready = not has_unknown_stock and not any(position["status"] == "unknown" for position in positions) and not any(
        diagnostics[name] for name in ("unknown_cell_reference", "physical_slot_contract_invalid", "duplicate_position_id", "multiple_stock_lots_single_position")
    )
    warehouse = normalize_warehouse(target_normalized_warehouse)
    state = {
        "simulation_state_version": SIMULATION_STATE_VERSION, "simulation_state_id": "",
        "model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"),
        "target_normalized_warehouse": warehouse, "simulation_time": simulation_time,
        "stock_lots": lots, "physical_positions": positions, "cell_occupancy": occupancies,
        "unresolved_stock": [{"stock_lot_id": lot["stock_lot_id"], "sku_key": lot["sku_key"], "qty_boxes": lot["qty_boxes"], "reason": "unknown_location"}
                             for lot in lots if lot["location_status"] == "unknown"],
        "applied_event_ids": [],
        "stock_conservation": {"opening_boxes_input": opening_boxes, "stock_boxes_state": state_boxes,
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
        if lot.get("pallet_count") == 0 or lot.get("pallet_count_status") != "unknown":
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
    if conservation.get("stock_boxes_state") != total or conservation.get("stock_conservation_ok") != (conservation.get("opening_boxes_input") == total):
        error("stock_conservation_metadata_mismatch")
    return {"valid": not errors, "errors": errors}
