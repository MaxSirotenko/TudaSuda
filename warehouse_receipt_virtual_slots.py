from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

REUSED_REASON = "physical_cell_reused_between_snapshots"
LIMITATIONS = [
    "virtual_slots_are_logical_not_physical_capacity",
    "parent_physical_cells_remain_unchanged",
    "virtual_slots_inherit_parent_location",
    "virtual_slots_are_not_appended_to_model_cells",
    "receipt_quantity_is_not_allocated",
    "current_placements_are_not_created",
    "routes_and_distances_are_not_created",
]
COORDINATE_FIELDS = ("x", "y", "x_min", "x_max", "y_min", "y_max", "center_x", "center_y")
PARENT_FIELDS = ("row_number", "cell_number", "tier", "weight_zone", "physical_index")
INVALID_COPY_FIELDS = ("requirement_key", "receipt_batch_key", "normalized_warehouse", "sku_key", "physical_cell_key", "reason")


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _filled(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _sort_value(value: Any) -> str:
    return "" if value is None else str(value)


def _empty_diags() -> dict[str, Any]:
    return {
        "model_cells": 0,
        "duplicate_model_cell_keys": 0,
        "source_virtual_slot_requirements": 0,
        "valid_virtual_slot_requirements": 0,
        "invalid_virtual_slot_requirements": 0,
        "duplicate_requirement_keys": 0,
        "duplicate_requirement_combinations": 0,
        "unknown_parent_physical_cells": 0,
        "virtual_slots_created": 0,
        "physical_cells_with_virtual_slots": 0,
        "max_virtual_slots_per_physical_cell": 0,
        "generated_key_collisions_avoided": 0,
        "configuration_errors": [],
    }


def _model_cells(model: Mapping[str, Any], diagnostics: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    cells: dict[Any, dict[str, Any]] = {}
    for index, cell in enumerate(model.get("cells") or []):
        if not isinstance(cell, Mapping):
            continue
        key = cell.get("cell_key")
        if not _filled(key):
            continue
        if key in cells:
            diagnostics["duplicate_model_cell_keys"] += 1
            continue
        row = dict(cell)
        row.setdefault("physical_index", index)
        cells[key] = row
    diagnostics["model_cells"] = len(cells)
    return cells


def _invalid(requirement: Any, reasons: list[str], source_index: int | None = None) -> dict[str, Any]:
    row = {field: None for field in INVALID_COPY_FIELDS}
    if isinstance(requirement, Mapping):
        row.update({field: requirement.get(field) for field in INVALID_COPY_FIELDS})
    priority = [
        "requirement_not_mapping", "requirement_key_missing", "receipt_batch_key_missing",
        "normalized_warehouse_missing", "sku_key_missing", "physical_cell_key_missing",
        "invalid_reason", "quantity_allocation_pending_not_true", "start_sku_keys_empty",
        "sku_key_not_in_end_sku_keys", "unknown_parent_physical_cell",
        "duplicate_requirement_key", "duplicate_requirement_combination",
        "dataset_id_mismatch", "start_snapshot_id_mismatch", "end_snapshot_id_mismatch",
    ]
    unique_reasons = sorted(set(reasons), key=lambda r: (priority.index(r) if r in priority else len(priority), r))
    row["reasons"] = unique_reasons
    row["reason_code"] = unique_reasons[0] if unique_reasons else "invalid_virtual_slot_requirement"
    return row


def _requirement_sort_key(req: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        _sort_value(req.get("normalized_warehouse")),
        _sort_value(req.get("physical_cell_key")),
        _sort_value(req.get("receipt_batch_key")),
        _sort_value(req.get("sku_key")),
        _sort_value(req.get("requirement_key")),
    )


def _state_value(transition_analysis_state: Mapping[str, Any], key: str) -> Any:
    return transition_analysis_state.get(key)


def _validate_requirements(
    requirements: Any,
    transition_analysis_state: Mapping[str, Any],
    cells: Mapping[Any, Mapping[str, Any]],
    diagnostics: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = requirements if isinstance(requirements, list) else []
    diagnostics["source_virtual_slot_requirements"] = len(source)
    key_counts = Counter(req.get("requirement_key") for req in source if isinstance(req, Mapping) and _filled(req.get("requirement_key")))
    combo_counts = Counter(
        (req.get("normalized_warehouse"), req.get("physical_cell_key"), req.get("receipt_batch_key"))
        for req in source
        if isinstance(req, Mapping)
        and _filled(req.get("normalized_warehouse"))
        and _filled(req.get("physical_cell_key"))
        and _filled(req.get("receipt_batch_key"))
    )
    duplicate_keys = sorted([key for key, count in key_counts.items() if count > 1], key=_sort_value)
    duplicate_combos = sorted([combo for combo, count in combo_counts.items() if count > 1], key=lambda x: tuple(_sort_value(v) for v in x))
    diagnostics["duplicate_requirement_keys"] = len(duplicate_keys)
    diagnostics["duplicate_requirement_combinations"] = len(duplicate_combos)

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for index, req in enumerate(source):
        reasons: list[str] = []
        if not isinstance(req, Mapping):
            invalid.append(_invalid(req, ["requirement_not_mapping"], index))
            continue
        for field in ("requirement_key", "receipt_batch_key", "normalized_warehouse", "sku_key", "physical_cell_key"):
            if not _filled(req.get(field)):
                reasons.append(f"{field}_missing")
        if req.get("reason") != REUSED_REASON:
            reasons.append("invalid_reason")
        if req.get("quantity_allocation_pending") is not True:
            reasons.append("quantity_allocation_pending_not_true")
        if not isinstance(req.get("start_sku_keys"), list) or not req.get("start_sku_keys"):
            reasons.append("start_sku_keys_empty")
        if not isinstance(req.get("end_sku_keys"), list) or req.get("sku_key") not in req.get("end_sku_keys", []):
            reasons.append("sku_key_not_in_end_sku_keys")
        if _filled(req.get("physical_cell_key")) and req.get("physical_cell_key") not in cells:
            reasons.append("unknown_parent_physical_cell")
            diagnostics["unknown_parent_physical_cells"] += 1
        if _filled(req.get("requirement_key")) and key_counts[req.get("requirement_key")] > 1:
            reasons.append("duplicate_requirement_key")
        combo = (req.get("normalized_warehouse"), req.get("physical_cell_key"), req.get("receipt_batch_key"))
        if all(_filled(v) for v in combo) and combo_counts[combo] > 1:
            reasons.append("duplicate_requirement_combination")
        for field in ("dataset_id", "start_snapshot_id", "end_snapshot_id"):
            top = _state_value(transition_analysis_state, field)
            if _filled(req.get(field)) and _filled(top) and req.get(field) != top:
                reasons.append(f"{field}_mismatch")
        if reasons:
            invalid.append(_invalid(req, reasons, index))
        else:
            valid.append(dict(req))
    valid.sort(key=_requirement_sort_key)
    invalid.sort(key=lambda x: tuple(_sort_value(x.get(f)) for f in INVALID_COPY_FIELDS) + (_sort_value(x.get("reason_code")), _canon(x.get("reasons"))))
    diagnostics["valid_virtual_slot_requirements"] = len(valid)
    diagnostics["invalid_virtual_slot_requirements"] = len(invalid)
    return valid, invalid


def _next_slot_key(physical_cell_key: Any, used: set[Any], start: int) -> tuple[str, int, int]:
    ordinal = start
    collisions = 0
    while True:
        key = f"{physical_cell_key}-V{ordinal:02d}"
        if key not in used:
            used.add(key)
            return key, ordinal, collisions
        collisions += 1
        ordinal += 1


def build_receipt_virtual_slots(
    model: dict[str, Any],
    transition_analysis_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    diagnostics = _empty_diags()
    source_state = transition_analysis_state if isinstance(transition_analysis_state, Mapping) else {}
    cells = _model_cells(model if isinstance(model, Mapping) else {}, diagnostics)
    valid, invalid = _validate_requirements(source_state.get("virtual_slot_requirements"), source_state, cells, diagnostics)

    used_keys = set(cells)
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for req in valid:
        groups[(req.get("normalized_warehouse"), req.get("physical_cell_key"))].append(req)

    virtual_slots: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    group_counts: dict[tuple[Any, Any], int] = {}
    for group_key in sorted(groups, key=lambda x: (_sort_value(x[0]), _sort_value(x[1]))):
        next_ordinal = 1
        for req in sorted(groups[group_key], key=lambda r: (_sort_value(r.get("receipt_batch_key")), _sort_value(r.get("sku_key")), _sort_value(r.get("requirement_key")))):
            parent = cells[req["physical_cell_key"]]
            virtual_key, ordinal, collisions = _next_slot_key(req["physical_cell_key"], used_keys, next_ordinal)
            next_ordinal = ordinal + 1
            diagnostics["generated_key_collisions_avoided"] += collisions
            identity = {
                "analysis_id": source_state.get("analysis_id"),
                "dataset_id": source_state.get("dataset_id"),
                "normalized_warehouse": req.get("normalized_warehouse"),
                "parent_physical_cell_key": req.get("physical_cell_key"),
                "requirement_key": req.get("requirement_key"),
                "virtual_slot_key": virtual_key,
            }
            slot = {
                "virtual_slot_id": _hash(identity),
                "virtual_slot_key": virtual_key,
                "cell_key": virtual_key,
                "is_virtual": True,
                "parent_physical_cell_key": req.get("physical_cell_key"),
                "requirement_key": req.get("requirement_key"),
                "receipt_batch_key": req.get("receipt_batch_key"),
                "dataset_id": req.get("dataset_id", source_state.get("dataset_id")),
                "start_snapshot_id": req.get("start_snapshot_id", source_state.get("start_snapshot_id")),
                "end_snapshot_id": req.get("end_snapshot_id", source_state.get("end_snapshot_id")),
                "normalized_warehouse": req.get("normalized_warehouse"),
                "warehouse": req.get("warehouse"),
                "sku_key": req.get("sku_key"),
                "slot_ordinal": ordinal,
                "parent_location": {field: parent[field] for field in COORDINATE_FIELDS if field in parent},
                "inherits_route_from_parent": True,
                "changes_physical_capacity": False,
                "quantity_allocation_pending": True,
                "reason": REUSED_REASON,
            }
            for field in PARENT_FIELDS:
                slot[field] = parent.get(field)
            slot["_physical_order"] = parent.get("physical_index", 0)
            virtual_slots.append(slot)
            links.append({
                "requirement_key": req.get("requirement_key"),
                "virtual_slot_id": slot["virtual_slot_id"],
                "virtual_slot_key": virtual_key,
                "receipt_batch_key": req.get("receipt_batch_key"),
                "normalized_warehouse": req.get("normalized_warehouse"),
                "physical_cell_key": req.get("physical_cell_key"),
            })
            group_counts[group_key] = group_counts.get(group_key, 0) + 1

    virtual_slots.sort(key=lambda s: (_sort_value(s.get("normalized_warehouse")), _sort_value(s.get("_physical_order")), _sort_value(s.get("parent_physical_cell_key")), s.get("slot_ordinal", 0), _sort_value(s.get("requirement_key"))))
    for slot in virtual_slots:
        slot.pop("_physical_order", None)
    links.sort(key=lambda x: (_sort_value(x.get("normalized_warehouse")), _sort_value(x.get("physical_cell_key")), _sort_value(x.get("virtual_slot_key")), _sort_value(x.get("requirement_key"))))

    diagnostics["virtual_slots_created"] = len(virtual_slots)
    diagnostics["physical_cells_with_virtual_slots"] = len(group_counts)
    diagnostics["max_virtual_slots_per_physical_cell"] = max(group_counts.values(), default=0)

    state_identity = {
        "analysis_id": source_state.get("analysis_id"),
        "dataset_id": source_state.get("dataset_id"),
        "start_snapshot_id": source_state.get("start_snapshot_id"),
        "end_snapshot_id": source_state.get("end_snapshot_id"),
        "virtual_slot_ids": sorted(slot["virtual_slot_id"] for slot in virtual_slots),
        "invalid_virtual_slot_requirements": sorted(invalid, key=_canon),
    }
    virtual_slot_state = {
        "virtual_slot_state_id": _hash(state_identity),
        "analysis_id": source_state.get("analysis_id"),
        "dataset_id": source_state.get("dataset_id"),
        "operational_date": source_state.get("operational_date"),
        "start_snapshot_id": source_state.get("start_snapshot_id"),
        "end_snapshot_id": source_state.get("end_snapshot_id"),
        "virtual_slots": virtual_slots,
        "requirement_slot_links": links,
        "invalid_virtual_slot_requirements": invalid,
        "limitations": list(LIMITATIONS),
    }
    return virtual_slot_state, diagnostics
