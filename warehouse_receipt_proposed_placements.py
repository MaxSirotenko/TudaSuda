from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

BOX_UNIT = "короб"
ZONES = ("heavy", "medium", "light", "fragile")
LIMITATIONS = [
    "proposed_placements_cover_day_receipts_only", "opening_stock_is_preserved_and_not_moved",
    "opening_stock_must_be_pre_filtered_to_target_warehouse", "one_physical_model_warehouse_per_call",
    "receipt_quantities_come_only_from_receipt_sku_batches", "one_receipt_batch_is_not_split",
    "only_free_physical_cells_are_used", "occupied_cells_are_not_reused_even_for_the_same_sku",
    "exact_weight_zone_match_is_required", "cross_zone_fallback_is_not_used", "unassigned_cells_are_not_used",
    "physical_box_capacity_is_not_evaluated", "deep_lane_box_capacity_is_not_evaluated",
    "physical_capacity_is_not_recalculated", "virtual_slots_are_not_used", "current_placements_are_not_modified",
    "deterministic_physical_order_is_not_a_route", "routes_and_distances_are_not_created",
    "abc_priority_is_only_applied_when_explicitly_supplied",
]


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canon(value).encode()).hexdigest()


def _filled(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _natural(value: Any) -> tuple:
    if value is None or value == "":
        return (1, ())
    return (0, tuple((0, int(x)) if x.isdigit() else (1, x.casefold()) for x in re.split(r"(\d+)", str(value))))


def _invalid(row: Any, reasons: list[str], fields: tuple[str, ...]) -> dict[str, Any]:
    result = {field: row.get(field) if isinstance(row, Mapping) else None for field in fields}
    result["reasons"] = sorted(set(reasons)); result["reason_code"] = result["reasons"][0]
    return result


def _diagnostics() -> dict[str, Any]:
    keys = "model_cells duplicate_model_cell_keys eligible_model_cells unsupported_zone_model_cells source_receipt_batches valid_receipt_batches invalid_receipt_batches source_slotting_rules valid_slotting_rules invalid_slotting_rules duplicate_rule_keys duplicate_rule_combinations source_opening_stock_placements valid_opening_stock_placements invalid_opening_stock_placements unknown_opening_stock_cells non_positive_opening_stock_placements opening_stock_occupied_cells receipt_batches_placed receipt_batches_unresolved proposed_placements_created receipt_boxes_total placed_boxes unresolved_boxes missing_slotting_rule_batches invalid_slotting_rule_batches no_free_cell_batches free_cells_before_placement free_cells_after_placement"
    return {key: 0 for key in keys.split()} | {"configuration_errors": []}


def _model_cells(model: Any, d: dict[str, Any]):
    source = model.get("cells", []) if isinstance(model, Mapping) else []
    source = source if isinstance(source, list) else []
    counts = Counter(x.get("cell_key") for x in source if isinstance(x, Mapping) and _filled(x.get("cell_key")))
    d["model_cells"] = len(source); d["duplicate_model_cell_keys"] = sum(v > 1 for v in counts.values())
    cells, unsupported = {}, 0
    for cell in source:
        if not isinstance(cell, Mapping) or not _filled(cell.get("cell_key")) or counts[cell.get("cell_key")] != 1:
            continue
        if cell.get("weight_zone") not in ZONES:
            unsupported += 1; continue
        cells[cell["cell_key"]] = dict(cell)
    d["eligible_model_cells"] = len(cells); d["unsupported_zone_model_cells"] = unsupported
    return cells


def _batches(day: Mapping[str, Any], dataset: Any, d: dict[str, Any]):
    source = day.get("receipt_sku_batches", []); source = source if isinstance(source, list) else []
    d["source_receipt_batches"] = len(source)
    counts = Counter(x.get("receipt_batch_key") for x in source if isinstance(x, Mapping) and _filled(x.get("receipt_batch_key")))
    valid, invalid = [], []
    fields = ("receipt_batch_key", "dataset_id", "normalized_warehouse", "warehouse", "sku_key", "qty_units", "unit_name")
    for row in source:
        reasons = []
        if not isinstance(row, Mapping): reasons.append("receipt_batch_not_mapping")
        else:
            for field in ("receipt_batch_key", "normalized_warehouse", "sku_key"):
                if not _filled(row.get(field)): reasons.append(field + "_missing")
            qty = row.get("qty_units")
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0: reasons.append("qty_units_not_positive_int")
            if row.get("unit_name") != BOX_UNIT: reasons.append("unit_name_not_boxes")
            if _filled(row.get("dataset_id")) and row.get("dataset_id") != dataset: reasons.append("dataset_id_mismatch")
            if _filled(row.get("receipt_batch_key")) and counts[row["receipt_batch_key"]] > 1: reasons.append("duplicate_receipt_batch_key")
        (invalid if reasons else valid).append(_invalid(row, reasons, fields) if reasons else dict(row))
    valid.sort(key=_canon); invalid.sort(key=_canon)
    d["valid_receipt_batches"] = len(valid); d["invalid_receipt_batches"] = len(invalid)
    return valid, invalid


def _rules(state: Mapping[str, Any], target: Any, d: dict[str, Any]):
    source = state.get("sku_rules", []); source = source if isinstance(source, list) else []
    d["source_slotting_rules"] = len(source)
    keys = Counter(x.get("rule_key") for x in source if isinstance(x, Mapping) and _filled(x.get("rule_key")))
    combos = Counter((x.get("normalized_warehouse"), x.get("sku_key")) for x in source if isinstance(x, Mapping) and _filled(x.get("normalized_warehouse")) and _filled(x.get("sku_key")))
    d["duplicate_rule_keys"] = sum(v > 1 for v in keys.values()); d["duplicate_rule_combinations"] = sum(v > 1 for v in combos.values())
    valid, invalid, invalid_combos = [], [], set()
    fields = ("rule_key", "normalized_warehouse", "sku_key", "weight_zone", "priority_rank", "source")
    for row in source:
        reasons = []
        if not isinstance(row, Mapping): reasons.append("slotting_rule_not_mapping")
        else:
            for field in ("rule_key", "normalized_warehouse", "sku_key"):
                if not _filled(row.get(field)): reasons.append(field + "_missing")
            if row.get("weight_zone") not in ZONES: reasons.append("invalid_weight_zone")
            rank = row.get("priority_rank")
            if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 0): reasons.append("invalid_priority_rank")
            if _filled(target) and row.get("normalized_warehouse") != target: reasons.append("normalized_warehouse_mismatch")
            if _filled(row.get("rule_key")) and keys[row["rule_key"]] > 1: reasons.append("duplicate_rule_key")
            combo = (row.get("normalized_warehouse"), row.get("sku_key"))
            if all(_filled(x) for x in combo) and combos[combo] > 1: reasons.append("duplicate_rule_combination")
        if reasons:
            invalid.append(_invalid(row, reasons, fields))
            if isinstance(row, Mapping): invalid_combos.add((row.get("normalized_warehouse"), row.get("sku_key")))
        else: valid.append(dict(row))
    valid.sort(key=_canon); invalid.sort(key=_canon)
    d["valid_slotting_rules"] = len(valid); d["invalid_slotting_rules"] = len(invalid)
    return valid, invalid, invalid_combos


def _opening(state: Mapping[str, Any], cells: dict, d: dict[str, Any]):
    source = state.get("placements", []); source = source if isinstance(source, list) else []
    d["source_opening_stock_placements"] = len(source); valid, invalid, occupied = [], [], set()
    fields = ("placement_id", "cell_key", "sku_key", "qty_units", "unit_name")
    for row in source:
        reasons = []
        if not isinstance(row, Mapping): reasons.append("opening_stock_placement_not_mapping")
        else:
            if not _filled(row.get("cell_key")): reasons.append("cell_key_missing")
            elif row.get("cell_key") not in cells: reasons.append("unknown_cell_key"); d["unknown_opening_stock_cells"] += 1
            if not _filled(row.get("sku_key")): reasons.append("sku_key_missing")
            qty = row.get("qty_units")
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                reasons.append("qty_units_not_positive_int"); d["non_positive_opening_stock_placements"] += 1
            if row.get("unit_name") != BOX_UNIT: reasons.append("unit_name_not_boxes")
        if reasons: invalid.append(_invalid(row, reasons, fields))
        else: valid.append(dict(row)); occupied.add(row["cell_key"])
    valid.sort(key=_canon); invalid.sort(key=_canon)
    d["valid_opening_stock_placements"] = len(valid); d["invalid_opening_stock_placements"] = len(invalid); d["opening_stock_occupied_cells"] = len(occupied)
    return valid, invalid, occupied


def build_proposed_receipt_placements(model: dict[str, Any], day_receipt_state: dict[str, Any], opening_stock_state: dict[str, Any], slotting_rule_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Place each valid day-receipt batch in one free, exact-zone physical cell."""
    day = day_receipt_state if isinstance(day_receipt_state, Mapping) else {}
    opening = opening_stock_state if isinstance(opening_stock_state, Mapping) else {}
    rules_state = slotting_rule_state if isinstance(slotting_rule_state, Mapping) else {}
    d = _diagnostics(); dataset = day.get("dataset_id"); cells = _model_cells(model, d)
    batches, invalid_batches = _batches(day, dataset, d)
    warehouses = sorted({x["normalized_warehouse"] for x in batches}, key=str)
    declared_target = rules_state.get("target_normalized_warehouse")
    target = warehouses[0] if len(warehouses) == 1 else declared_target
    rules, invalid_rules, invalid_combos = _rules(rules_state, target, d)
    _, invalid_opening, occupied = _opening(opening, cells, d)
    errors = d["configuration_errors"]
    if not _filled(dataset): errors.append("day_receipt_dataset_id_missing")
    proposed = day.get("scenario_inputs", {}).get("proposed", {}) if isinstance(day.get("scenario_inputs"), Mapping) else {}
    if proposed.get("receipt_dataset_id") != dataset: errors.append("proposed_receipt_dataset_id_mismatch")
    expected_keys = sorted(x["receipt_batch_key"] for x in batches)
    if not isinstance(proposed.get("receipt_batch_keys"), list) or sorted(set(proposed["receipt_batch_keys"])) != expected_keys: errors.append("proposed_receipt_batch_keys_mismatch")
    total = sum(x["qty_units"] for x in batches)
    if proposed.get("total_boxes") != total: errors.append("proposed_total_boxes_mismatch")
    if rules_state.get("dataset_id") != dataset: errors.append("slotting_rule_dataset_id_mismatch")
    if len(warehouses) > 1: errors.append("multiple_receipt_normalized_warehouses")
    if warehouses and _filled(declared_target) and declared_target != warehouses[0]: errors.append("target_normalized_warehouse_mismatch")
    if _filled(model.get("model_id")) and _filled(opening.get("model_id")) and model.get("model_id") != opening.get("model_id"): errors.append("opening_stock_model_id_mismatch")
    errors.sort()
    order = rules_state.get("zone_order") if isinstance(rules_state.get("zone_order"), list) else list(ZONES)
    order = [z for z in order if z in ZONES] + [z for z in ZONES if z not in order]; zone_pos = {z: i for i, z in enumerate(order)}
    cell_sort = lambda x: (_natural(x.get("row_order")), _natural(x.get("physical_index")), _natural(x.get("row_number")), _natural(x.get("cell_number")), _natural(x.get("tier")), _natural(x.get("cell_key")))
    free_by_zone = {z: sorted((c for c in cells.values() if c["weight_zone"] == z and c["cell_key"] not in occupied), key=cell_sort) for z in ZONES}
    before = {z: len(v) for z, v in free_by_zone.items()}; d["free_cells_before_placement"] = sum(before.values())
    by_combo = {(r["normalized_warehouse"], r["sku_key"]): r for r in rules}
    def batch_sort(batch):
        rule = by_combo.get((batch["normalized_warehouse"], batch["sku_key"])); zone = rule.get("weight_zone") if rule else None; rank = rule.get("priority_rank") if rule else None
        return (zone_pos.get(zone, len(ZONES)), (0, rank) if rank is not None else (1, 0), -batch["qty_units"], str(batch["normalized_warehouse"]), str(batch["sku_key"]), str(batch["receipt_batch_key"]))
    placements, unresolved, used = [], [], set()
    for batch in sorted(batches, key=batch_sort):
        combo = (batch["normalized_warehouse"], batch["sku_key"]); rule = by_combo.get(combo); reason = None
        if errors: reason = "configuration_error"
        elif rule is None:
            reason = "slotting_rule_missing_or_invalid" if combo in invalid_combos else "slotting_rule_missing"
            d["invalid_slotting_rule_batches" if combo in invalid_combos else "missing_slotting_rule_batches"] += 1
        elif not free_by_zone[rule["weight_zone"]]: reason = "no_free_cell_in_weight_zone"; d["no_free_cell_batches"] += 1
        if reason:
            unresolved.append({"receipt_batch_key": batch["receipt_batch_key"], "dataset_id": dataset, "operational_date": day.get("operational_date"), "normalized_warehouse": batch["normalized_warehouse"], "warehouse": batch.get("warehouse"), "sku_key": batch["sku_key"], "qty_units": batch["qty_units"], "unit_name": BOX_UNIT, "requested_weight_zone": rule.get("weight_zone") if rule else None, "slotting_rule_key": rule.get("rule_key") if rule else None, "priority_rank": rule.get("priority_rank") if rule else None, "candidate_free_cell_keys": [c["cell_key"] for c in free_by_zone.get(rule.get("weight_zone"), [])] if rule else [], "reason_code": reason, "reasons": [reason]})
            continue
        cell = free_by_zone[rule["weight_zone"]].pop(0); used.add(cell["cell_key"])
        identity = {"dataset_id": dataset, "slotting_rule_state_id": rules_state.get("slotting_rule_state_id"), "receipt_batch_key": batch["receipt_batch_key"], "normalized_warehouse": batch["normalized_warehouse"], "sku_key": batch["sku_key"], "cell_key": cell["cell_key"], "qty_units": batch["qty_units"], "scenario": "proposed"}
        placements.append({"placement_id": _hash(identity), "scenario": "proposed", "dataset_id": dataset, "slotting_rule_state_id": rules_state.get("slotting_rule_state_id"), "operational_date": day.get("operational_date"), "receipt_batch_key": batch["receipt_batch_key"], "normalized_warehouse": batch["normalized_warehouse"], "warehouse": batch.get("warehouse"), "sku_key": batch["sku_key"], "qty_units": batch["qty_units"], "qty_boxes": batch["qty_units"], "unit_name": BOX_UNIT, "cell_key": cell["cell_key"], "physical_cell_key": cell["cell_key"], "route_physical_cell_key": cell["cell_key"], "is_virtual": False, "virtual_slot_id": None, "parent_physical_cell_key": None, "row_number": cell.get("row_number"), "row_order": cell.get("row_order"), "cell_number": cell.get("cell_number"), "tier": cell.get("tier"), "physical_index": cell.get("physical_index"), "weight_zone": cell["weight_zone"], "requested_weight_zone": rule["weight_zone"], "slotting_rule_key": rule["rule_key"], "slotting_priority_rank": rule.get("priority_rank"), "slotting_rule_source": rule.get("source"), "quantity_source": "day_receipt_sku_batch", "location_source": "proposed_weight_zone_slotting", "allocation_method": "one_batch_one_free_physical_cell", "placement_mode": "algorithmic_proposed_day_receipt", "placement_status": "placed", "occupancy_not_authoritative": True, "physical_capacity_recalculated": False, "opening_stock_preserved": True})
    placements.sort(key=lambda x: (zone_pos[x["weight_zone"]],) + cell_sort(x) + (str(x["sku_key"]), str(x["receipt_batch_key"]), x["placement_id"]))
    unresolved.sort(key=lambda x: (zone_pos.get(x["requested_weight_zone"], len(ZONES)), (0, x["priority_rank"]) if x["priority_rank"] is not None else (1, 0), str(x["normalized_warehouse"]), str(x["sku_key"]), str(x["receipt_batch_key"]), x["reason_code"]))
    placed_qty = sum(x["qty_units"] for x in placements); unresolved_qty = sum(x["qty_units"] for x in unresolved)
    after = {z: len(v) for z, v in free_by_zone.items()}; d["free_cells_after_placement"] = sum(after.values())
    zone_summary = {z: {"physical_cells": sum(c["weight_zone"] == z for c in cells.values()), "opening_stock_occupied_cells": sum(cells[k]["weight_zone"] == z for k in occupied), "free_cells_before_placement": before[z], "placements_created": sum(x["weight_zone"] == z for x in placements), "free_cells_after_placement": after[z], "unresolved_batches_no_free_cell": sum(x["requested_weight_zone"] == z and x["reason_code"] == "no_free_cell_in_weight_zone" for x in unresolved)} for z in ZONES}
    summary = {"valid_receipt_batches": len(batches), "placed_receipt_batches": len(placements), "unresolved_receipt_batches": len(unresolved), "valid_receipt_qty_units": total, "placed_qty_units": placed_qty, "unresolved_qty_units": unresolved_qty, "opening_stock_occupied_cells": len(occupied), "physical_cells_total": d["model_cells"], "eligible_physical_cells": len(cells), "free_cells_before_placement": d["free_cells_before_placement"], "free_cells_after_placement": d["free_cells_after_placement"], "quantity_conservation_ok": placed_qty + unresolved_qty == total, "one_batch_one_placement_ok": len({x["receipt_batch_key"] for x in placements}) == len(placements), "opening_stock_preserved": True}
    d.update({"receipt_batches_placed": len(placements), "receipt_batches_unresolved": len(unresolved), "proposed_placements_created": len(placements), "receipt_boxes_total": total, "placed_boxes": placed_qty, "unresolved_boxes": unresolved_qty})
    identity = {"dataset_id": dataset, "slotting_rule_state_id": rules_state.get("slotting_rule_state_id"), "target_normalized_warehouse": target, "placement_ids": sorted(x["placement_id"] for x in placements), "unresolved": sorted(unresolved, key=_canon), "invalid": sorted(invalid_batches + invalid_rules + invalid_opening, key=_canon), "occupied": sorted(occupied), "configuration_errors": errors}
    state = {"proposed_receipt_placement_state_id": _hash(identity), "scenario": "proposed", "dataset_id": dataset, "slotting_rule_state_id": rules_state.get("slotting_rule_state_id"), "operational_date": day.get("operational_date"), "target_normalized_warehouse": target, "placements": placements, "unresolved_receipt_batches": unresolved, "invalid_receipt_batches": invalid_batches, "invalid_slotting_rules": invalid_rules, "invalid_opening_stock_placements": invalid_opening, "occupied_opening_stock_cell_keys": sorted(occupied), "limitations": list(LIMITATIONS), "summary": summary, "zone_summary": zone_summary}
    return state, d
