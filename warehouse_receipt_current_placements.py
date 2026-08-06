from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

BOX_UNIT = "короб"
REUSED_REASON = "physical_cell_reused_between_snapshots"
LIMITATIONS = [
    "current_placements_cover_day_receipts_only",
    "opening_stock_is_not_modified",
    "receipt_quantities_come_only_from_receipt_sku_batches",
    "one_receipt_batch_is_not_split_without_an_explicit_rule",
    "ambiguous_or_multiple_candidates_remain_unresolved",
    "same_sku_replenishment_in_same_cell_is_not_observable",
    "virtual_slots_do_not_change_physical_capacity",
    "physical_capacity_is_not_recalculated",
    "proposed_placements_are_not_created",
    "routes_and_distances_are_not_created",
]


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canon(value).encode()).hexdigest()


def _filled(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _sv(value: Any) -> str:
    return "" if value is None else str(value)


def _diag() -> dict[str, Any]:
    return {key: 0 for key in (
        "model_cells duplicate_model_cell_keys source_receipt_batches valid_receipt_batches "
        "invalid_receipt_batches source_receipt_cell_candidates valid_receipt_cell_candidates "
        "invalid_receipt_cell_candidates duplicate_candidate_keys duplicate_candidate_combinations "
        "candidate_quantity_mismatches source_virtual_slots valid_virtual_slots invalid_virtual_slots "
        "duplicate_virtual_slot_ids duplicate_virtual_slot_keys receipt_batches_placed "
        "receipt_batches_unresolved current_placements_created physical_placements_created "
        "virtual_placements_created receipt_boxes_total placed_boxes unresolved_boxes no_candidate_batches "
        "ambiguous_candidate_batches multiple_candidate_batches missing_virtual_slot_batches "
        "multiple_virtual_slot_batches same_sku_replenishment_unobservable_batches unknown_physical_cells"
    ).split()} | {"configuration_errors": []}


def _invalid(row: Any, reasons: list[str], fields: tuple[str, ...]) -> dict[str, Any]:
    result = {field: row.get(field) if isinstance(row, Mapping) else None for field in fields}
    result["reasons"] = sorted(set(reasons))
    result["reason_code"] = result["reasons"][0]
    return result


def _cells(model: Any, d: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    source = model.get("cells", []) if isinstance(model, Mapping) else []
    source = source if isinstance(source, list) else []
    counts = Counter(c.get("cell_key") for c in source if isinstance(c, Mapping) and _filled(c.get("cell_key")))
    d["duplicate_model_cell_keys"] = sum(1 for count in counts.values() if count > 1)
    cells = {}
    for index, cell in enumerate(source):
        if isinstance(cell, Mapping) and _filled(cell.get("cell_key")) and counts[cell.get("cell_key")] == 1:
            cells[cell["cell_key"]] = dict(cell) | {"physical_index": cell.get("physical_index", index)}
    d["model_cells"] = len(cells)
    return cells


def _batches(state: Mapping[str, Any], dataset: Any, d: dict[str, Any]):
    source = state.get("receipt_sku_batches", [])
    source = source if isinstance(source, list) else []
    d["source_receipt_batches"] = len(source)
    counts = Counter(x.get("receipt_batch_key") for x in source if isinstance(x, Mapping) and _filled(x.get("receipt_batch_key")))
    valid, invalid = [], []
    fields = ("receipt_batch_key", "normalized_warehouse", "warehouse", "sku_key", "qty_units", "unit_name")
    for row in source:
        reasons = []
        if not isinstance(row, Mapping):
            reasons.append("receipt_batch_not_mapping")
        else:
            for field in ("receipt_batch_key", "normalized_warehouse", "sku_key"):
                if not _filled(row.get(field)): reasons.append(field + "_missing")
            qty = row.get("qty_units")
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0: reasons.append("qty_units_not_positive_int")
            if row.get("unit_name") != BOX_UNIT: reasons.append("unit_name_not_boxes")
            if _filled(row.get("dataset_id")) and row.get("dataset_id") != dataset: reasons.append("dataset_id_mismatch")
            if _filled(row.get("receipt_batch_key")) and counts[row.get("receipt_batch_key")] > 1: reasons.append("duplicate_receipt_batch_key")
        if reasons: invalid.append(_invalid(row, reasons, fields))
        else: valid.append(dict(row))
    valid.sort(key=lambda x: (_sv(x["normalized_warehouse"]), _sv(x["sku_key"]), _sv(x["receipt_batch_key"])))
    invalid.sort(key=_canon)
    d["valid_receipt_batches"], d["invalid_receipt_batches"] = len(valid), len(invalid)
    return valid, invalid


def _candidates(source: Any, dataset: Any, batches: dict[Any, dict], cells: dict, d: dict[str, Any]):
    source = source if isinstance(source, list) else []
    d["source_receipt_cell_candidates"] = len(source)
    key_counts = Counter(x.get("candidate_key") for x in source if isinstance(x, Mapping) and _filled(x.get("candidate_key")))
    combo = lambda x: (x.get("receipt_batch_key"), x.get("normalized_warehouse"), x.get("physical_cell_key"), x.get("candidate_type"))
    combo_counts = Counter(combo(x) for x in source if isinstance(x, Mapping) and all(_filled(v) for v in combo(x)))
    d["duplicate_candidate_keys"] = sum(v > 1 for v in key_counts.values())
    d["duplicate_candidate_combinations"] = sum(v > 1 for v in combo_counts.values())
    valid, invalid = [], []
    fields = ("candidate_key", "receipt_batch_key", "normalized_warehouse", "sku_key", "physical_cell_key", "candidate_type")
    for row in source:
        reasons = []
        if not isinstance(row, Mapping): reasons.append("candidate_not_mapping")
        else:
            for field in ("candidate_key", "receipt_batch_key", "normalized_warehouse", "sku_key", "physical_cell_key", "transition_key"):
                if not _filled(row.get(field)): reasons.append(field + "_missing")
            typ = row.get("candidate_type")
            if typ not in ("newly_occupied_physical_cell", "reused_physical_cell"): reasons.append("invalid_candidate_type")
            if row.get("confidence") not in ("strong_snapshot_delta", "ambiguous_snapshot_delta"): reasons.append("invalid_confidence")
            if row.get("dataset_id") != dataset: reasons.append("dataset_id_mismatch")
            batch = batches.get(row.get("receipt_batch_key"))
            if batch is None: reasons.append("valid_receipt_batch_missing")
            elif row.get("normalized_warehouse") != batch["normalized_warehouse"] or row.get("sku_key") != batch["sku_key"]: reasons.append("receipt_batch_identity_mismatch")
            if row.get("physical_cell_key") not in cells:
                reasons.append("unknown_physical_cell"); d["unknown_physical_cells"] += 1
            if typ == "newly_occupied_physical_cell" and row.get("requires_virtual_slot") is not False: reasons.append("requires_virtual_slot_mismatch")
            if typ == "reused_physical_cell" and row.get("requires_virtual_slot") is not True: reasons.append("requires_virtual_slot_mismatch")
            if _filled(row.get("candidate_key")) and key_counts[row.get("candidate_key")] > 1: reasons.append("duplicate_candidate_key")
            if all(_filled(v) for v in combo(row)) and combo_counts[combo(row)] > 1: reasons.append("duplicate_candidate_combination")
            if batch is not None and "receipt_qty_units" in row and row.get("receipt_qty_units") != batch["qty_units"]:
                reasons.append("candidate_quantity_mismatch"); d["candidate_quantity_mismatches"] += 1
        if reasons: invalid.append(_invalid(row, reasons, fields))
        else: valid.append(dict(row))
    valid.sort(key=_canon); invalid.sort(key=_canon)
    d["valid_receipt_cell_candidates"], d["invalid_receipt_cell_candidates"] = len(valid), len(invalid)
    return valid, invalid


def _slots(source: Any, dataset: Any, cells: dict, d: dict[str, Any]):
    source = source if isinstance(source, list) else []
    d["source_virtual_slots"] = len(source)
    ids = Counter(x.get("virtual_slot_id") for x in source if isinstance(x, Mapping) and _filled(x.get("virtual_slot_id")))
    keys = Counter(x.get("virtual_slot_key") for x in source if isinstance(x, Mapping) and _filled(x.get("virtual_slot_key")))
    d["duplicate_virtual_slot_ids"] = sum(v > 1 for v in ids.values()); d["duplicate_virtual_slot_keys"] = sum(v > 1 for v in keys.values())
    valid, invalid = [], []
    fields = ("virtual_slot_id", "virtual_slot_key", "receipt_batch_key", "normalized_warehouse", "sku_key", "parent_physical_cell_key")
    for row in source:
        reasons = []
        if not isinstance(row, Mapping): reasons.append("virtual_slot_not_mapping")
        else:
            for field in fields:
                if not _filled(row.get(field)): reasons.append(field + "_missing")
            if row.get("cell_key") != row.get("virtual_slot_key"): reasons.append("cell_key_mismatch")
            if row.get("is_virtual") is not True: reasons.append("is_virtual_not_true")
            if row.get("dataset_id") != dataset: reasons.append("dataset_id_mismatch")
            if row.get("parent_physical_cell_key") not in cells: reasons.append("unknown_parent_physical_cell")
            if row.get("changes_physical_capacity") is not False: reasons.append("changes_physical_capacity_not_false")
            if row.get("inherits_route_from_parent") is not True: reasons.append("inherits_route_from_parent_not_true")
            if row.get("quantity_allocation_pending") is not True: reasons.append("quantity_allocation_pending_not_true")
            if row.get("reason") != REUSED_REASON: reasons.append("invalid_reason")
            if _filled(row.get("virtual_slot_id")) and ids[row.get("virtual_slot_id")] > 1: reasons.append("duplicate_virtual_slot_id")
            if _filled(row.get("virtual_slot_key")) and keys[row.get("virtual_slot_key")] > 1: reasons.append("duplicate_virtual_slot_key")
        if reasons: invalid.append(_invalid(row, reasons, fields))
        else: valid.append(dict(row))
    valid.sort(key=_canon); invalid.sort(key=_canon)
    d["valid_virtual_slots"], d["invalid_virtual_slots"] = len(valid), len(invalid)
    return valid, invalid


def build_current_receipt_placements(model: dict[str, Any], day_receipt_state: dict[str, Any], transition_analysis_state: dict[str, Any], virtual_slot_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build factual, unsplit CURRENT placements for valid day-receipt batches."""
    day = day_receipt_state if isinstance(day_receipt_state, Mapping) else {}
    analysis = transition_analysis_state if isinstance(transition_analysis_state, Mapping) else {}
    slots_state = virtual_slot_state if isinstance(virtual_slot_state, Mapping) else {}
    d = _diag(); cells = _cells(model, d); dataset = day.get("dataset_id")
    valid, invalid_batches = _batches(day, dataset, d); batch_by_key = {x["receipt_batch_key"]: x for x in valid}
    candidates, invalid_candidates = _candidates(analysis.get("receipt_cell_candidates"), dataset, batch_by_key, cells, d)
    slots, invalid_slots = _slots(slots_state.get("virtual_slots"), dataset, cells, d)
    errors = d["configuration_errors"]
    if not _filled(dataset): errors.append("day_receipt_dataset_id_missing")
    for state, name in ((analysis, "transition_analysis"), (slots_state, "virtual_slot")):
        if state.get("dataset_id") != dataset: errors.append(name + "_dataset_id_mismatch")
    if slots_state.get("analysis_id") != analysis.get("analysis_id"): errors.append("analysis_id_mismatch")
    for field in ("start_snapshot_id", "end_snapshot_id"):
        if slots_state.get(field) != analysis.get(field): errors.append(field + "_mismatch")
    current = day.get("scenario_inputs", {}).get("current", {}) if isinstance(day.get("scenario_inputs"), Mapping) else {}
    if current.get("receipt_dataset_id") != dataset: errors.append("current_receipt_dataset_id_mismatch")
    expected_keys = sorted(batch_by_key, key=_sv)
    actual_keys = current.get("receipt_batch_keys")
    if not isinstance(actual_keys, list) or sorted(set(actual_keys), key=_sv) != expected_keys: errors.append("current_receipt_batch_keys_mismatch")
    total = sum(x["qty_units"] for x in valid)
    if current.get("total_boxes") != total: errors.append("current_total_boxes_mismatch")
    errors.sort()
    evidence = {x.get("receipt_batch_key"): x for x in analysis.get("receipt_batch_evidence", []) if isinstance(x, Mapping) and _filled(x.get("receipt_batch_key"))}
    by_batch = defaultdict(list)
    for candidate in candidates: by_batch[candidate["receipt_batch_key"]].append(candidate)
    placements, unresolved = [], []
    invalid_by_batch = defaultdict(list)
    for item in invalid_candidates:
        if _filled(item.get("receipt_batch_key")): invalid_by_batch[item["receipt_batch_key"]].append(item)
    for batch in valid:
        if errors: break
        key = batch["receipt_batch_key"]; cs = by_batch[key]; ev = evidence.get(key, {}); reason = None; matching_slots = []
        if len(cs) > 1: reason = "multiple_receipt_cell_candidates"; d["multiple_candidate_batches"] += 1
        elif not cs:
            d["no_candidate_batches"] += 1
            if any("candidate_quantity_mismatch" in x["reasons"] for x in invalid_by_batch[key]): reason = "candidate_quantity_mismatch"
            elif invalid_by_batch[key]: reason = "candidate_contract_missing"
            else:
                status = ev.get("evidence_status")
                reason = "same_sku_replenishment_not_observable" if status == "persistent_same_sku_only" else (status or "receipt_batch_evidence_missing")
                if reason == "same_sku_replenishment_not_observable": d["same_sku_replenishment_unobservable_batches"] += 1
        elif cs[0]["confidence"] == "ambiguous_snapshot_delta": reason = "ambiguous_snapshot_delta"; d["ambiguous_candidate_batches"] += 1
        else:
            candidate = cs[0]; cell_key = candidate["physical_cell_key"]; slot = None
            if candidate["candidate_type"] == "reused_physical_cell":
                matching_slots = [s for s in slots if (s["dataset_id"], s["normalized_warehouse"], s["receipt_batch_key"], s["sku_key"], s["parent_physical_cell_key"]) == (dataset, batch["normalized_warehouse"], key, batch["sku_key"], cell_key)]
                if len(matching_slots) == 0: reason = "matching_virtual_slot_missing"; d["missing_virtual_slot_batches"] += 1
                elif len(matching_slots) > 1: reason = "multiple_matching_virtual_slots"; d["multiple_virtual_slot_batches"] += 1
                else: slot = matching_slots[0]; cell_key = slot["virtual_slot_key"]
            if reason is None:
                parent_key = candidate["physical_cell_key"]; physical = cells[parent_key]
                identity = {"dataset_id": dataset, "analysis_id": analysis.get("analysis_id"), "virtual_slot_state_id": slots_state.get("virtual_slot_state_id"), "receipt_batch_key": key, "normalized_warehouse": batch["normalized_warehouse"], "sku_key": batch["sku_key"], "cell_key": cell_key, "qty_units": batch["qty_units"], "scenario": "current"}
                placements.append({"placement_id": _hash(identity), "scenario": "current", "dataset_id": dataset, "analysis_id": analysis.get("analysis_id"), "virtual_slot_state_id": slots_state.get("virtual_slot_state_id"), "operational_date": day.get("operational_date"), "receipt_batch_key": key, "normalized_warehouse": batch["normalized_warehouse"], "warehouse": batch.get("warehouse"), "sku_key": batch["sku_key"], "qty_units": batch["qty_units"], "qty_boxes": batch["qty_units"], "unit_name": BOX_UNIT, "cell_key": cell_key, "physical_cell_key": parent_key, "route_physical_cell_key": parent_key, "is_virtual": slot is not None, "virtual_slot_id": slot.get("virtual_slot_id") if slot else None, "parent_physical_cell_key": parent_key if slot else None, "row_number": physical.get("row_number"), "cell_number": physical.get("cell_number"), "tier": physical.get("tier"), "weight_zone": physical.get("weight_zone"), "physical_index": physical.get("physical_index"), "candidate_key": candidate["candidate_key"], "candidate_type": candidate["candidate_type"], "candidate_confidence": "strong_snapshot_delta", "transition_key": candidate["transition_key"], "quantity_source": "day_receipt_sku_batch", "location_source": "snapshot_delta", "allocation_method": "single_strong_snapshot_candidate", "placement_mode": "factual_current_day_receipt", "placement_status": "placed", "occupancy_not_authoritative": True, "physical_capacity_recalculated": False})
        if reason:
            all_cs = cs + [x for x in invalid_by_batch[key]]
            unresolved.append({"receipt_batch_key": key, "dataset_id": dataset, "operational_date": day.get("operational_date"), "normalized_warehouse": batch["normalized_warehouse"], "warehouse": batch.get("warehouse"), "sku_key": batch["sku_key"], "qty_units": batch["qty_units"], "unit_name": batch["unit_name"], "evidence_status": ev.get("evidence_status"), "candidate_keys": sorted({_sv(x.get("candidate_key")) for x in all_cs if _filled(x.get("candidate_key"))}), "candidate_physical_cell_keys": sorted({_sv(x.get("physical_cell_key")) for x in all_cs if _filled(x.get("physical_cell_key"))}), "candidate_virtual_slot_keys": sorted({_sv(x.get("virtual_slot_key")) for x in matching_slots}), "reason_code": reason, "reasons": [reason]})
    placements.sort(key=lambda x: (_sv(x["normalized_warehouse"]), x.get("physical_index") if isinstance(x.get("physical_index"), (int, float)) else 10**18, _sv(x["route_physical_cell_key"]), _sv(x["cell_key"]), _sv(x["sku_key"]), _sv(x["receipt_batch_key"]), x["placement_id"]))
    unresolved.sort(key=lambda x: (_sv(x["normalized_warehouse"]), _sv(x["sku_key"]), _sv(x["receipt_batch_key"]), x["reason_code"]))
    placed = sum(x["qty_units"] for x in placements); unresolved_qty = sum(x["qty_units"] for x in unresolved)
    summary = {"valid_receipt_batches": len(valid), "placed_receipt_batches": len(placements), "unresolved_receipt_batches": len(unresolved), "valid_receipt_qty_units": total, "placed_qty_units": placed, "unresolved_qty_units": unresolved_qty, "physical_placements": sum(not x["is_virtual"] for x in placements), "virtual_placements": sum(x["is_virtual"] for x in placements), "quantity_conservation_ok": not errors and placed + unresolved_qty == total}
    d.update({"receipt_batches_placed": len(placements), "receipt_batches_unresolved": len(unresolved), "current_placements_created": len(placements), "physical_placements_created": summary["physical_placements"], "virtual_placements_created": summary["virtual_placements"], "receipt_boxes_total": total, "placed_boxes": placed, "unresolved_boxes": unresolved_qty})
    identity = {"dataset_id": dataset, "analysis_id": analysis.get("analysis_id"), "virtual_slot_state_id": slots_state.get("virtual_slot_state_id"), "placement_ids": sorted(x["placement_id"] for x in placements), "unresolved": sorted(({"receipt_batch_key": x["receipt_batch_key"], "reasons": x["reasons"]} for x in unresolved), key=_canon), "invalid": sorted(invalid_batches + invalid_candidates + invalid_slots, key=_canon), "configuration_errors": errors}
    state = {"current_receipt_placement_state_id": _hash(identity), "scenario": "current", "dataset_id": dataset, "analysis_id": analysis.get("analysis_id"), "virtual_slot_state_id": slots_state.get("virtual_slot_state_id"), "operational_date": day.get("operational_date"), "start_snapshot_id": analysis.get("start_snapshot_id"), "end_snapshot_id": analysis.get("end_snapshot_id"), "placements": placements, "unresolved_receipt_batches": unresolved, "invalid_receipt_batches": invalid_batches, "invalid_receipt_cell_candidates": invalid_candidates, "invalid_virtual_slots": invalid_slots, "limitations": list(LIMITATIONS), "summary": summary}
    return state, d
