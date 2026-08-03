"""Build a read-only view of stock that can be used by future pick logic."""

from __future__ import annotations

import math
from typing import Any

from warehouse_inventory_placement import cell_key as _canonical_cell_key
from warehouse_inventory_placement import make_sku_key as _make_sku_key


_SKU_METADATA_FIELDS = (
    "sku_code",
    "sku_name",
    "characteristic",
    "characteristic_name",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _number(value: Any) -> float:
    try:
        if value is None or str(value).strip() == "":
            return 0.0
        number = float(str(value).replace(",", "."))
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _unit_quantity(value: Any) -> tuple[int | None, bool]:
    """Return an integral unit quantity and whether a value was supplied."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None, False
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None, True
    return int(number), True


def _unit_names(value: Any) -> tuple[str, str]:
    """Return a stable display name and its private grouping key."""

    display_name = " ".join(str(value).split()) if value is not None else ""
    return display_name, display_name.casefold().replace("ё", "е")


def _number_sort_key(value: Any) -> tuple[int, float | str]:
    text = _text(value)
    try:
        number = float(text)
        if math.isfinite(number):
            return (0, number)
    except ValueError:
        pass
    return (1, text)


def _compatible_value(model: dict[str, Any], state: dict[str, Any], field: str) -> Any:
    model_value = model.get(field)
    state_value = state.get(field)
    if model_value not in (None, "") and state_value not in (None, "") and model_value != state_value:
        raise ValueError(
            f"Placement state {field} does not match warehouse model: "
            f"{state_value!r} != {model_value!r}"
        )
    return model_value if model_value not in (None, "") else state_value


def _location_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _number_sort_key(record.get("row_order")),
        _number_sort_key(record.get("row_number")),
        _number_sort_key(record.get("cell_number")),
        _number_sort_key(record.get("tier")),
        record["cell_key"],
    )


def _sku_metadata(placement: dict[str, Any]) -> dict[str, str]:
    characteristic = _text(placement.get("characteristic"))
    return {
        "sku_code": _text(placement.get("sku_code")),
        "sku_name": _text(placement.get("sku_name") or placement.get("item_name")),
        "characteristic": characteristic,
        "characteristic_name": _text(placement.get("characteristic_name")) or characteristic,
    }


def build_pickable_inventory_index(
    model: dict[str, Any], placement_state: dict[str, Any]
) -> dict[str, Any]:
    """Return deterministic SKU and cell indexes for positive placed stock.

    Geometry and cell metadata come from ``model``. Placements contribute SKU
    metadata, stock amounts, source, and (only when necessary) a zone fallback.
    Neither input is modified and no persisted state is read or written.
    """

    model_id = _compatible_value(model, placement_state, "model_id")
    source_file_hash = _compatible_value(model, placement_state, "source_file_hash")

    cells_by_key: dict[str, dict[str, Any]] = {}
    for cell in model.get("cells", []) or []:
        key = _canonical_cell_key(
            cell.get("row_number"), cell.get("cell_number"), cell.get("tier")
        )
        cells_by_key[key] = cell

    diagnostics = {
        "placements_total": 0,
        "indexed_records": 0,
        "sku_count": 0,
        "cell_count": 0,
        "merged_duplicate_records": 0,
        "skipped_missing_sku": 0,
        "skipped_missing_cell_key": 0,
        "skipped_unknown_cell": 0,
        "skipped_non_positive_stock": 0,
        "sku_metadata_conflicts": 0,
        "placements_with_positive_qty_units": 0,
        "placements_missing_unit_name": 0,
        "invalid_qty_units": 0,
        "non_positive_qty_units": 0,
        "unit_variants_in_same_cell": 0,
        "indexed_unit_variants": 0,
        "cells_with_unit_stock": 0,
        "skus_with_unit_stock": 0,
    }
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    sku_metadata: dict[str, dict[str, str]] = {}

    placements = placement_state.get("placements", []) or []
    diagnostics["placements_total"] = len(placements)
    for placement in placements:
        sku_key = _text(placement.get("sku_key")) or _make_sku_key(placement)
        if not sku_key:
            diagnostics["skipped_missing_sku"] += 1
            continue
        placement_cell_key = _text(placement.get("cell_key"))
        if not placement_cell_key:
            diagnostics["skipped_missing_cell_key"] += 1
            continue
        cell = cells_by_key.get(placement_cell_key)
        if cell is None:
            diagnostics["skipped_unknown_cell"] += 1
            continue

        quantity = _number(placement.get("quantity"))
        occupied = _number(placement.get("occupied_capacity_pallets"))
        unit_quantity, unit_quantity_supplied = _unit_quantity(placement.get("qty_units"))
        positive_unit_quantity = unit_quantity is not None and unit_quantity > 0
        if unit_quantity_supplied and unit_quantity is None:
            diagnostics["invalid_qty_units"] += 1
        elif unit_quantity is not None and unit_quantity <= 0:
            diagnostics["non_positive_qty_units"] += 1
        if positive_unit_quantity:
            diagnostics["placements_with_positive_qty_units"] += 1
        if quantity <= 0 and occupied <= 0 and not positive_unit_quantity:
            diagnostics["skipped_non_positive_stock"] += 1
            continue

        unit_display_name, unit_key = _unit_names(placement.get("unit_name"))
        if positive_unit_quantity and not unit_display_name:
            diagnostics["placements_missing_unit_name"] += 1

        stable_metadata = sku_metadata.setdefault(
            sku_key, {field: "" for field in _SKU_METADATA_FIELDS}
        )
        placement_metadata = _sku_metadata(placement)
        for field in _SKU_METADATA_FIELDS:
            candidate = placement_metadata[field]
            if not stable_metadata[field] and candidate:
                stable_metadata[field] = candidate
            elif candidate and stable_metadata[field] != candidate:
                diagnostics["sku_metadata_conflicts"] += 1

        pair = (sku_key, placement_cell_key)
        existing = aggregated.get(pair)
        if existing is not None:
            diagnostics["merged_duplicate_records"] += 1
            existing["quantity"] += quantity
            existing["occupied_capacity_pallets"] += occupied
            if positive_unit_quantity:
                variant = existing["_unit_quantities"].setdefault(
                    unit_key, {"unit_name": unit_display_name, "qty_units": 0}
                )
                variant["qty_units"] += unit_quantity
            continue

        zone = cell.get("weight_zone")
        if zone in (None, ""):
            zone = placement.get("calculated_zone")
        aggregated[pair] = {
            "sku_key": sku_key,
            **placement_metadata,
            "cell_key": placement_cell_key,
            "row_number": cell.get("row_number"),
            "cell_number": cell.get("cell_number"),
            "tier": cell.get("tier") or "1",
            "row_order": cell.get("row_order"),
            "zone": zone,
            "storage_type": cell.get("storage_type"),
            "quantity": quantity,
            "occupied_capacity_pallets": occupied,
            "capacity_pallets": _number(cell.get("capacity_pallets")),
            "x_center": _number(cell.get("x_center")),
            "y_center": _number(cell.get("y_center")),
            "source": _text(placement.get("source")),
            "_unit_quantities": (
                {unit_key: {"unit_name": unit_display_name, "qty_units": unit_quantity}}
                if positive_unit_quantity
                else {}
            ),
        }

    by_sku: dict[str, list[dict[str, Any]]] = {}
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for record in aggregated.values():
        variants_by_key = record.pop("_unit_quantities")
        variants = [
            variants_by_key[key]
            for key in sorted(
                variants_by_key,
                key=lambda key: (
                    not bool(variants_by_key[key]["unit_name"]),
                    key,
                    variants_by_key[key]["unit_name"],
                ),
            )
        ]
        record["unit_quantities"] = variants
        if len(variants) == 1:
            record["qty_units"] = variants[0]["qty_units"]
            record["unit_name"] = variants[0]["unit_name"]
        else:
            record["qty_units"] = None
            record["unit_name"] = ""
        if len(variants) > 1:
            diagnostics["unit_variants_in_same_cell"] += 1
        record.update(sku_metadata[record["sku_key"]])
        by_sku.setdefault(record["sku_key"], []).append(record)
        by_cell.setdefault(record["cell_key"], []).append(record)
    for records in by_sku.values():
        records.sort(key=_location_sort_key)
    for records in by_cell.values():
        records.sort(key=lambda record: (record["sku_key"], _location_sort_key(record)))

    diagnostics["indexed_records"] = len(aggregated)
    diagnostics["sku_count"] = len(by_sku)
    diagnostics["cell_count"] = len(by_cell)
    diagnostics["indexed_unit_variants"] = sum(
        len(record["unit_quantities"]) for record in aggregated.values()
    )
    diagnostics["cells_with_unit_stock"] = len(
        {record["cell_key"] for record in aggregated.values() if record["unit_quantities"]}
    )
    diagnostics["skus_with_unit_stock"] = len(
        {record["sku_key"] for record in aggregated.values() if record["unit_quantities"]}
    )
    return {
        "model_id": model_id,
        "source_file_hash": source_file_hash,
        "by_sku": dict(sorted(by_sku.items())),
        "by_cell": dict(sorted(by_cell.items())),
        "diagnostics": diagnostics,
    }
