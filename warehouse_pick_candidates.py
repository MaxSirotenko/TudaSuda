"""Find physical inventory locations that can satisfy one pick demand."""

from __future__ import annotations

import math
from typing import Any


_CANDIDATE_FIELDS = (
    "sku_key",
    "cell_key",
    "row_number",
    "cell_number",
    "tier",
    "row_order",
    "zone",
    "storage_type",
    "quantity",
    "occupied_capacity_pallets",
    "capacity_pallets",
    "x_center",
    "y_center",
    "source",
)


def _unit_key(value: Any) -> str:
    return " ".join(str(value).split()).casefold().replace("ё", "е")


def _display_unit(value: Any) -> str:
    return " ".join(str(value).split())


def _number_sort_key(value: Any) -> tuple[int, float | str]:
    if isinstance(value, bool):
        return (1, str(value))
    try:
        number = float(value)
        if math.isfinite(number):
            return (0, number)
    except (TypeError, ValueError):
        pass
    return (1, "" if value is None else str(value))


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _number_sort_key(candidate.get("row_order")),
        _number_sort_key(candidate.get("row_number")),
        _number_sort_key(candidate.get("cell_number")),
        _number_sort_key(candidate.get("tier")),
        "" if candidate.get("cell_key") is None else str(candidate.get("cell_key")),
    )


def _diagnostics() -> dict[str, Any]:
    return {
        "sku_found": False,
        "source_location_count": 0,
        "source_unit_variant_count": 0,
        "matched_candidate_count": 0,
        "unmatched_unit_variant_count": 0,
        "invalid_unit_variant_count": 0,
        "non_positive_unit_variant_count": 0,
        "duplicate_matching_unit_variants": 0,
        "cells_without_matching_unit": 0,
        "cells_with_matching_unit": 0,
        "demand_validation_reason": "",
    }


def _result_shell(demand: Any, inventory_index: Any) -> dict[str, Any]:
    demand_values = demand if isinstance(demand, dict) else {}
    index_values = inventory_index if isinstance(inventory_index, dict) else {}
    requested = demand_values.get("requested_units")
    safe_requested = (
        requested
        if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0
        else 0
    )
    return {
        "demand_key": demand_values.get("demand_key", ""),
        "order_key": demand_values.get("order_key", ""),
        "outbound_order_number": demand_values.get("outbound_order_number", ""),
        "sku_key": demand_values.get("sku_key", ""),
        "requested_units": requested if safe_requested else safe_requested,
        "unit_name": demand_values.get("unit_name", ""),
        "model_id": index_values.get("model_id", ""),
        "source_file_hash": index_values.get("source_file_hash", ""),
        "status": "invalid_demand",
        "can_fulfill": False,
        "total_available_units": 0,
        "shortage_units": safe_requested,
        "candidate_count": 0,
        "candidates": [],
        "diagnostics": _diagnostics(),
    }


def _validation_reason(demand: Any) -> str:
    if not isinstance(demand, dict):
        return "demand_not_dict"
    if not isinstance(demand.get("sku_key"), str) or not demand["sku_key"].strip():
        return "sku_key_missing"
    requested = demand.get("requested_units")
    if isinstance(requested, bool):
        return "requested_units_boolean"
    if not isinstance(requested, int):
        return "requested_units_not_integer"
    if requested <= 0:
        return "requested_units_not_positive"
    if "unit_name" not in demand:
        return "unit_name_missing"
    return ""


def find_pick_candidates_for_demand(demand: Any, inventory_index: Any) -> dict[str, Any]:
    """Return unit-compatible physical locations and aggregate availability.

    SKU lookup is an exact lookup in ``inventory_index["by_sku"]``. The
    function creates compact result records and never mutates either input.
    """

    result = _result_shell(demand, inventory_index)
    diagnostics = result["diagnostics"]
    reason = _validation_reason(demand)
    if reason:
        diagnostics["demand_validation_reason"] = reason
        return result

    sku_key = demand["sku_key"]
    requested_units = demand["requested_units"]
    wanted_unit = _unit_key(demand["unit_name"])
    by_sku = inventory_index.get("by_sku", {}) if isinstance(inventory_index, dict) else {}
    records = by_sku.get(sku_key, []) if isinstance(by_sku, dict) else []
    if not isinstance(records, list):
        records = []
    sku_present = isinstance(by_sku, dict) and sku_key in by_sku
    if not sku_present:
        result["status"] = "sku_not_found"
        return result

    diagnostics["sku_found"] = True
    diagnostics["source_location_count"] = len(records)
    candidates: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            diagnostics["cells_without_matching_unit"] += 1
            continue
        variants = record.get("unit_quantities", [])
        if not isinstance(variants, list):
            variants = []
        available_units = 0
        matching_variants = 0
        display_unit = ""
        for variant in variants:
            diagnostics["source_unit_variant_count"] += 1
            if not isinstance(variant, dict):
                diagnostics["invalid_unit_variant_count"] += 1
                continue
            quantity = variant.get("qty_units")
            valid_quantity = True
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                diagnostics["invalid_unit_variant_count"] += 1
                valid_quantity = False
            elif quantity <= 0:
                diagnostics["non_positive_unit_variant_count"] += 1
                valid_quantity = False
            matches = _unit_key(variant.get("unit_name", "")) == wanted_unit
            if not matches:
                diagnostics["unmatched_unit_variant_count"] += 1
                continue
            matching_variants += 1
            if matching_variants == 1:
                display_unit = _display_unit(variant.get("unit_name", ""))
            if valid_quantity:
                available_units += quantity
        if matching_variants > 1:
            diagnostics["duplicate_matching_unit_variants"] += matching_variants - 1
        if available_units <= 0:
            diagnostics["cells_without_matching_unit"] += 1
            continue
        candidate = {field: record.get(field) for field in _CANDIDATE_FIELDS}
        candidate["available_units"] = available_units
        candidate["unit_name"] = display_unit
        candidates.append(candidate)
        diagnostics["cells_with_matching_unit"] += 1

    candidates.sort(key=_candidate_sort_key)
    total = sum(candidate["available_units"] for candidate in candidates)
    result["candidates"] = candidates
    result["candidate_count"] = len(candidates)
    result["total_available_units"] = total
    result["shortage_units"] = max(requested_units - total, 0)
    result["can_fulfill"] = total >= requested_units
    result["status"] = (
        "unit_not_found"
        if not candidates
        else "sufficient_stock" if result["can_fulfill"] else "insufficient_stock"
    )
    diagnostics["matched_candidate_count"] = len(candidates)
    return result
