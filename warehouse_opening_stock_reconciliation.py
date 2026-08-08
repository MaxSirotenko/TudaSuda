"""Reconcile inventory totals with actual, physical location hints.

The module is deliberately pure: inventory is authoritative for quantity and
the placement snapshot is used only for locations and distribution weights.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from warehouse_business_identity import build_canonical_sku_identity, normalize_unit_name


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _box_unit(value: Any) -> bool:
    return normalize_unit_name(value) == "короб"


def _positive_integer(value: Any) -> tuple[int | None, str | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "inventory_quantity_missing"
    if isinstance(value, bool):
        return None, "inventory_quantity_invalid"
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None, "inventory_quantity_invalid"
    if not math.isfinite(number) or not number.is_integer():
        return None, "inventory_quantity_invalid"
    if number <= 0:
        return None, "inventory_quantity_non_positive"
    return int(number), None


def _reported_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None


def _number_key(value: Any) -> tuple[int, float | str]:
    text = _text(value)
    try:
        number = float(text)
        if math.isfinite(number):
            return (0, number)
    except ValueError:
        pass
    return (1, text)


def _cell_key(cell: dict[str, Any]) -> str:
    existing = _text(cell.get("cell_key"))
    if existing:
        return existing
    return f'{_text(cell.get("row_number"))}|{_text(cell.get("cell_number"))}|{_text(cell.get("tier") or "1")}'


def _sort_key(hint: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(_number_key(hint.get(field)) for field in ("row_order", "row_number", "cell_number", "tier")) + (hint["cell_key"],)


def _clean_source_index(value: Any) -> str | int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value) if value.is_integer() else value
    return _text(value)


def _index_sort(value: Any) -> tuple[int, float | str]:
    return _number_key(value)


def _largest_remainders(total: int, weights: list[int]) -> list[int]:
    weight_total = sum(weights)
    numerators = [total * weight for weight in weights]
    result = [value // weight_total for value in numerators]
    remaining = total - sum(result)
    order = sorted(range(len(weights)), key=lambda index: (-(numerators[index] % weight_total), index))
    for index in order[:remaining]:
        result[index] += 1
    return result


def reconcile_opening_stock(
    model: dict[str, Any],
    inventory_rows: list[dict[str, Any]],
    actual_placement_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return reconciled opening stock and JSON-serializable diagnostics."""

    counter_names = (
        "inventory_rows_total inventory_rows_accepted inventory_rows_excluded merged_inventory_rows "
        "inventory_sku_count inventory_boxes_total located_sku_count located_boxes "
        "unknown_location_sku_count unknown_location_boxes stale_location_sku_count "
        "stale_location_record_count exact_single_cell_sku_count exact_single_cell_boxes "
        "exact_reported_distribution_sku_count exact_reported_distribution_boxes "
        "estimated_proportional_sku_count estimated_proportional_boxes estimated_even_sku_count "
        "estimated_even_boxes location_total_mismatch_sku_count technical_one_box_location_sku_count "
        "location_records_total location_records_used location_records_unknown_cell "
        "location_records_invalid_quantity missing_inventory_sku inventory_quantity_missing "
        "inventory_quantity_invalid inventory_quantity_non_positive unsupported_inventory_unit"
    ).split()
    diagnostics = {name: 0 for name in counter_names}
    diagnostics["inventory_rows_total"] = len(inventory_rows or [])
    excluded: list[dict[str, Any]] = []
    inventory: dict[str, dict[str, Any]] = {}
    legacy_aliases: dict[str, str] = {}

    for row in inventory_rows or []:
        nomenclature = _text(row.get("nomenclature") or row.get("sku_name"))
        characteristic = _text(row.get("characteristic") or row.get("characteristic_name"))
        identity = build_canonical_sku_identity(row)
        sku_key = identity["sku_key"]
        legacy_key = _text(row.get("sku_key"))
        if legacy_key and sku_key:
            legacy_aliases[legacy_key] = sku_key
        if "legacy_sku_key_mismatch" in identity["diagnostics"]:
            diagnostics["legacy_sku_key_mismatch"] = diagnostics.get("legacy_sku_key_mismatch", 0) + 1
        base = {
            "sku_key": sku_key, "sku_code": _text(row.get("nomenclature_code") or row.get("sku_code")),
            "sku_name": nomenclature, "nomenclature": nomenclature,
            "characteristic_code": _text(row.get("characteristic_code")),
            "characteristic_name": characteristic, "characteristic": characteristic,
            "source_index": _clean_source_index(row.get("source_index")),
        }
        reason = None
        if not sku_key:
            reason = "missing_inventory_sku"
        elif not _box_unit(row.get("unit_name")):
            reason = "unsupported_inventory_unit"
        else:
            quantity, reason = _positive_integer(row.get("qty_units"))
        if reason:
            diagnostics[reason] += 1
            diagnostics["inventory_rows_excluded"] += 1
            excluded.append({**base, "unit_name": _text(row.get("unit_name")), "reason": reason})
            continue
        diagnostics["inventory_rows_accepted"] += 1
        existing = inventory.get(sku_key)
        if existing:
            diagnostics["merged_inventory_rows"] += 1
            existing["qty_units"] += quantity
            if base["source_index"] is not None:
                existing["source_indexes"].append(base["source_index"])
            for field in ("sku_code", "sku_name", "nomenclature", "characteristic_code", "characteristic_name", "characteristic"):
                if not existing[field] and base[field]:
                    existing[field] = base[field]
        else:
            inventory[sku_key] = {**base, "qty_units": quantity, "unit_name": "короб", "source_indexes": [] if base["source_index"] is None else [base["source_index"]]}

    for item in inventory.values():
        item.pop("source_index", None)
        item["source_indexes"] = sorted(set(item["source_indexes"]), key=_index_sort)
    diagnostics["inventory_sku_count"] = len(inventory)
    diagnostics["inventory_boxes_total"] = sum(item["qty_units"] for item in inventory.values())

    cells = {_cell_key(cell): cell for cell in (model.get("cells", []) or [])}
    hints: dict[str, dict[str, dict[str, Any]]] = {}
    diagnostics["location_records_total"] = len(actual_placement_state.get("placements", []) or [])
    for record in actual_placement_state.get("placements", []) or []:
        raw_sku_key = _text(record.get("sku_key"))
        sku_key, cell_key = legacy_aliases.get(raw_sku_key, raw_sku_key), _text(record.get("cell_key"))
        if not sku_key:
            continue
        cell = cells.get(cell_key)
        if cell is None:
            diagnostics["location_records_unknown_cell"] += 1
            continue
        reported = _reported_integer(record.get("qty_units"))
        if reported is None:
            diagnostics["location_records_invalid_quantity"] += 1
            reported = 0
        diagnostics["location_records_used"] += 1
        by_cell = hints.setdefault(sku_key, {})
        hint = by_cell.setdefault(cell_key, {
            "sku_key": sku_key, "cell_key": cell_key,
            "row_number": cell.get("row_number"), "cell_number": cell.get("cell_number"),
            "tier": cell.get("tier") or "1", "row_order": cell.get("row_order"),
            "weight_zone": cell.get("weight_zone"), "reported_location_qty_units": 0,
            "production_dates": [],
        })
        hint["reported_location_qty_units"] += reported
        date = _text(record.get("production_date"))
        if date and date not in hint["production_dates"]:
            hint["production_dates"].append(date)

    placements: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for sku_key in sorted(inventory):
        item = inventory[sku_key]
        locations = sorted(hints.get(sku_key, {}).values(), key=_sort_key)
        total = item["qty_units"]
        if not locations:
            diagnostics["unknown_location_sku_count"] += 1
            diagnostics["unknown_location_boxes"] += total
            unknown.append({**item, "reason": "no_location_hint"})
            continue
        reported = [hint["reported_location_qty_units"] for hint in locations]
        if len(locations) == 1:
            method, confidence, allocations = "exact_single_cell", "exact_location_estimated_quantity_distribution", [total]
        elif sum(reported) == total:
            method, confidence, allocations = "exact_reported_distribution", "high", reported
        else:
            diagnostics["location_total_mismatch_sku_count"] += 1
            if reported and all(value == 1 for value in reported):
                diagnostics["technical_one_box_location_sku_count"] += 1
            informative = all(value > 0 for value in reported) and len(set(reported)) > 1 and not all(value == 1 for value in reported)
            if informative:
                method, confidence, allocations = "estimated_proportional", "estimated", _largest_remainders(total, reported)
            else:
                base, remainder = divmod(total, len(locations))
                method, confidence = "estimated_even", "estimated"
                allocations = [base + (index < remainder) for index in range(len(locations))]
        diagnostics[f"{method}_sku_count"] += 1
        diagnostics[f"{method}_boxes"] += total
        diagnostics["located_sku_count"] += 1
        diagnostics["located_boxes"] += total
        for hint, quantity in zip(locations, allocations):
            identity = "|".join((_text(model.get("model_id")), sku_key, hint["cell_key"], method))
            placements.append({
                "placement_id": "opening-" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "sku_key": sku_key, "sku_code": item["sku_code"], "sku_name": item["sku_name"],
                "item_name": item["sku_name"], "nomenclature": item["nomenclature"],
                "characteristic_code": item["characteristic_code"], "characteristic_name": item["characteristic_name"],
                "characteristic": item["characteristic"], "row_number": hint["row_number"],
                "cell_number": hint["cell_number"], "tier": hint["tier"], "cell_key": hint["cell_key"],
                "qty_units": int(quantity), "unit_name": "короб", "qty_boxes": int(quantity),
                "quantity": 0.0, "qty_pallets": 0.0, "occupied_capacity_pallets": 0.0,
                "occupancy_not_authoritative": True, "weight_zone": hint["weight_zone"],
                "source": "opening_inventory_reconciled", "confidence": confidence,
                "allocation_method": method, "allocation_confidence": confidence,
                "quantity_source": "inventory", "location_source": "actual_placement_snapshot",
                "reported_location_qty_units": hint["reported_location_qty_units"],
                "inventory_total_qty_units": total, "source_indexes": item["source_indexes"],
                "production_dates": sorted(hint["production_dates"]), "placement_mode": "factual",
                "placement_status": "placed", "unplaced_reason": "",
            })

    stale = []
    for sku_key in sorted(set(hints) - set(inventory)):
        for hint in sorted(hints[sku_key].values(), key=_sort_key):
            stale.append({**hint, "production_dates": sorted(hint["production_dates"]), "reason": "location_without_inventory"})
    diagnostics["stale_location_sku_count"] = len(set(hints) - set(inventory))
    diagnostics["stale_location_record_count"] = len(stale)
    total = diagnostics["inventory_boxes_total"]
    for name, numerator in (
        ("processing_rate_percent", diagnostics["located_boxes"]),
        ("exact_allocation_rate_percent", diagnostics["exact_single_cell_boxes"] + diagnostics["exact_reported_distribution_boxes"]),
        ("estimated_allocation_rate_percent", diagnostics["estimated_proportional_boxes"] + diagnostics["estimated_even_boxes"]),
        ("unknown_location_rate_percent", diagnostics["unknown_location_boxes"]),
    ):
        diagnostics[name] = round(numerator / total * 100, 4) if total else 0.0

    state = {
        "model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"),
        "placements": placements, "excluded_inventory": excluded,
        "unknown_location_inventory": unknown, "stale_location_hints": stale,
        "unplaced_inventory": [], "settings": {"allow_mixed_sku_in_deep_lane": False}, "journal": [],
    }
    return state, diagnostics
