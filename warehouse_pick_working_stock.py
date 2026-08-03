"""Build isolated unit stock for future sequential pick simulation."""

from __future__ import annotations

import copy
import json
import math
from typing import Any


_METADATA_FIELDS = (
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


def _diagnostics(reason: str = "") -> dict[str, Any]:
    return {
        "source_sku_bucket_count": 0,
        "source_location_record_count": 0,
        "source_unit_variant_count": 0,
        "working_stock_record_count": 0,
        "sku_count": 0,
        "cell_count": 0,
        "total_initial_units": 0,
        "merged_duplicate_stock_records": 0,
        "skipped_invalid_sku_bucket": 0,
        "mismatched_sku_records": 0,
        "skipped_invalid_location_record": 0,
        "skipped_missing_cell_key": 0,
        "skipped_invalid_unit_variant": 0,
        "skipped_non_positive_units": 0,
        "stock_metadata_conflicts": 0,
        "cells_with_working_stock": 0,
        "skus_with_working_stock": 0,
        "input_validation_reason": reason,
    }


def _empty_result(inventory_index: Any, reason: str) -> dict[str, Any]:
    values = inventory_index if isinstance(inventory_index, dict) else {}
    return {
        "model_id": copy.deepcopy(values.get("model_id", "")),
        "source_file_hash": copy.deepcopy(values.get("source_file_hash", "")),
        "stock_by_key": {},
        "stock_keys_by_sku": {},
        "stock_keys_by_cell": {},
        "diagnostics": _diagnostics(reason),
    }


def _unit_names(value: Any) -> tuple[str, str]:
    display = " ".join(str(value).split()) if value is not None else ""
    return display, display.casefold().replace("ё", "е")


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


def _sku_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _number_sort_key(record.get("row_order")),
        _number_sort_key(record.get("row_number")),
        _number_sort_key(record.get("cell_number")),
        _number_sort_key(record.get("tier")),
        record["cell_key"],
        record["normalized_unit_name"],
        record["stock_key"],
    )


def build_pick_working_stock(inventory_index: Any) -> dict[str, Any]:
    """Return independent canonical SKU/cell/unit stock built only from ``by_sku``.

    Unit quantities are copied into a single ``stock_by_key`` source of truth.
    Secondary SKU and cell indexes contain only deterministic stock keys.
    """

    if not isinstance(inventory_index, dict):
        return _empty_result(inventory_index, "inventory_index_not_dict")
    by_sku = inventory_index.get("by_sku")
    if not isinstance(by_sku, dict):
        return _empty_result(inventory_index, "by_sku_not_dict")

    result = _empty_result(inventory_index, "")
    diagnostics = result["diagnostics"]
    diagnostics["source_sku_bucket_count"] = len(by_sku)
    stock: dict[str, dict[str, Any]] = {}

    for sku_key, records in by_sku.items():
        if not isinstance(sku_key, str) or not sku_key.strip():
            diagnostics["skipped_invalid_sku_bucket"] += 1
            continue
        if not isinstance(records, list):
            diagnostics["skipped_invalid_location_record"] += 1
            continue
        diagnostics["source_location_record_count"] += len(records)
        for source_record in records:
            if not isinstance(source_record, dict):
                diagnostics["skipped_invalid_location_record"] += 1
                continue
            record_sku = source_record.get("sku_key")
            if record_sku not in (None, "") and record_sku != sku_key:
                diagnostics["mismatched_sku_records"] += 1
                continue
            cell_key = source_record.get("cell_key")
            if not isinstance(cell_key, str) or not cell_key.strip():
                diagnostics["skipped_missing_cell_key"] += 1
                continue
            variants = source_record.get("unit_quantities")
            if not isinstance(variants, list):
                diagnostics["skipped_invalid_unit_variant"] += 1
                continue
            for variant in variants:
                diagnostics["source_unit_variant_count"] += 1
                if not isinstance(variant, dict):
                    diagnostics["skipped_invalid_unit_variant"] += 1
                    continue
                units = variant.get("qty_units")
                if isinstance(units, bool) or not isinstance(units, int):
                    diagnostics["skipped_invalid_unit_variant"] += 1
                    continue
                if units <= 0:
                    diagnostics["skipped_non_positive_units"] += 1
                    continue
                display_unit, normalized_unit = _unit_names(variant.get("unit_name", ""))
                stock_key = json.dumps(
                    [sku_key, cell_key, normalized_unit],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                incoming_metadata = {
                    field: copy.deepcopy(source_record.get(field))
                    for field in _METADATA_FIELDS
                }
                existing = stock.get(stock_key)
                if existing is not None:
                    diagnostics["merged_duplicate_stock_records"] += 1
                    existing["initial_units"] += units
                    existing["remaining_units"] += units
                    if any(existing[field] != incoming_metadata[field] for field in _METADATA_FIELDS):
                        diagnostics["stock_metadata_conflicts"] += 1
                    continue
                stock[stock_key] = {
                    "stock_key": stock_key,
                    "sku_key": sku_key,
                    "cell_key": cell_key,
                    "unit_name": display_unit,
                    "normalized_unit_name": normalized_unit,
                    "initial_units": units,
                    "remaining_units": units,
                    **incoming_metadata,
                }

    stock_by_key = dict(sorted(stock.items()))
    by_stock_sku: dict[str, list[str]] = {}
    by_stock_cell: dict[str, list[str]] = {}
    for stock_key, record in stock_by_key.items():
        by_stock_sku.setdefault(record["sku_key"], []).append(stock_key)
        by_stock_cell.setdefault(record["cell_key"], []).append(stock_key)
    for keys in by_stock_sku.values():
        keys.sort(key=lambda key: _sku_sort_key(stock_by_key[key]))
    for keys in by_stock_cell.values():
        keys.sort(key=lambda key: (
            stock_by_key[key]["sku_key"],
            stock_by_key[key]["normalized_unit_name"],
            key,
        ))

    result["stock_by_key"] = stock_by_key
    result["stock_keys_by_sku"] = dict(sorted(by_stock_sku.items()))
    result["stock_keys_by_cell"] = dict(sorted(by_stock_cell.items()))
    diagnostics["working_stock_record_count"] = len(stock_by_key)
    diagnostics["sku_count"] = len(by_stock_sku)
    diagnostics["cell_count"] = len(by_stock_cell)
    diagnostics["total_initial_units"] = sum(
        record["initial_units"] for record in stock_by_key.values()
    )
    diagnostics["cells_with_working_stock"] = len(by_stock_cell)
    diagnostics["skus_with_working_stock"] = len(by_stock_sku)
    return result
