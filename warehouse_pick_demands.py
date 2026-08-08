"""Build read-only picking demands from normalized outbound-order rows."""

from __future__ import annotations

import json
import re
from typing import Any

from warehouse_business_identity import (
    build_canonical_sku_identity,
    find_canonical_identity_collisions,
    normalize_unit_name,
)
from warehouse_outbound_orders import outbound_order_key


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _first_text(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = _text(row.get(name))
        if value:
            return value
    return ""


def _unit_key(value: Any) -> str:
    return _text(value).casefold().replace("ё", "е")


def _quantity_reason(row: dict[str, Any]) -> str:
    supplied = _text(row.get("quantity_validation_reason"))
    if supplied:
        normalized = supplied.casefold().replace("-", "_").replace(" ", "_")
        return normalized if re.fullmatch(r"[a-z0-9_]{1,64}", normalized) else "quantity_invalid"
    value = row.get("qty_units")
    if value is None or value == "":
        return "quantity_missing"
    if isinstance(value, bool):
        return "quantity_boolean"
    if isinstance(value, float):
        return "quantity_fractional" if not value.is_integer() else "quantity_not_integer"
    if not isinstance(value, int):
        return "quantity_not_integer"
    if value < 0:
        return "quantity_negative"
    return "quantity_invalid"


def _keep_metadata(target: dict[str, Any], incoming: dict[str, str], counter: dict[str, Any], name: str) -> None:
    for field, value in incoming.items():
        current = target[field]
        if not current and value:
            target[field] = value
        elif current and value and current != value:
            counter[name] += 1


def build_outbound_pick_demands(order_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return deterministic order/SKU/unit demands without I/O or input mutation."""
    diagnostics: dict[str, Any] = {
        "rows_total": len(order_rows),
        "orders_count": 0,
        "demands_count": 0,
        "merged_duplicate_lines": 0,
        "skipped_missing_order": 0,
        "skipped_missing_sku": 0,
        "skipped_invalid_quantity": 0,
        "skipped_non_positive_quantity": 0,
        "orders_without_valid_demands": 0,
        "sku_metadata_conflicts": 0,
        "order_metadata_conflicts": 0,
        "unit_variants_for_same_sku": 0,
        "quantity_reason_counts": {},
        "legacy_sku_key_mismatch": 0,
        "unsupported_unit": 0,
        "sku_identity_conflict": 0,
    }
    orders_by_key: dict[str, dict[str, Any]] = {}
    conflicting_keys = {item["sku_key"] for item in find_canonical_identity_collisions(order_rows)}

    for row in order_rows:
        number = _text(row.get("outbound_order_number"))
        created_at = _text(row.get("created_at"))
        warehouse = _text(row.get("warehouse"))
        order_key = _text(row.get("order_key"))
        if not order_key:
            if not number:
                diagnostics["skipped_missing_order"] += 1
                continue
            order_key = outbound_order_key(warehouse, number, created_at)
        if not order_key:
            diagnostics["skipped_missing_order"] += 1
            continue

        order = orders_by_key.setdefault(order_key, {
            "order_key": order_key,
            "outbound_order_number": number,
            "created_at": created_at,
            "warehouse": warehouse,
            "source_indexes": set(),
            "demands_by_merge_key": {},
            "sku_metadata_by_key": {},
        })
        _keep_metadata(order, {
            "outbound_order_number": number,
            "created_at": created_at,
            "warehouse": warehouse,
        }, diagnostics, "order_metadata_conflicts")
        source_index = row.get("source_index")
        if isinstance(source_index, int) and not isinstance(source_index, bool):
            order["source_indexes"].add(source_index)

        sku_code = _text(row.get("sku_code"))
        sku_name = _first_text(row, "sku_name", "nomenclature", "item_name")
        characteristic_code = _text(row.get("characteristic_code"))
        characteristic_name = _first_text(row, "characteristic_name", "characteristic")
        identity = build_canonical_sku_identity({
            "sku_key": row.get("sku_key"),
            "sku_code": sku_code,
            "sku_name": sku_name,
            "characteristic_code": characteristic_code,
            "characteristic_name": characteristic_name,
        })
        sku_key = identity["sku_key"]
        diagnostics["legacy_sku_key_mismatch"] += "legacy_sku_key_mismatch" in identity["diagnostics"]
        if sku_key in conflicting_keys:
            diagnostics["sku_identity_conflict"] += 1
            continue
        if not sku_key:
            diagnostics["skipped_missing_sku"] += 1
            continue

        reason = _text(row.get("quantity_validation_reason"))
        quantity = row.get("qty_units")
        if reason or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            diagnostics["skipped_invalid_quantity"] += 1
            reason_key = _quantity_reason(row)
            counts = diagnostics["quantity_reason_counts"]
            counts[reason_key] = counts.get(reason_key, 0) + 1
            continue
        if quantity == 0:
            diagnostics["skipped_non_positive_quantity"] += 1
            continue

        sku_metadata = order["sku_metadata_by_key"].setdefault(sku_key, {
            "sku_code": sku_code,
            "sku_name": sku_name,
            "characteristic_code": characteristic_code,
            "characteristic_name": characteristic_name,
        })
        _keep_metadata(sku_metadata, {
            "sku_code": sku_code,
            "sku_name": sku_name,
            "characteristic_code": characteristic_code,
            "characteristic_name": characteristic_name,
        }, diagnostics, "sku_metadata_conflicts")

        normalized_unit = normalize_unit_name(row.get("unit_name"))
        if normalized_unit is None:
            diagnostics["unsupported_unit"] += 1
            continue
        display_unit = normalized_unit
        merge_key = (sku_key, normalized_unit)
        demands = order["demands_by_merge_key"]
        if merge_key in demands:
            demand = demands[merge_key]
            demand["requested_units"] += quantity
            diagnostics["merged_duplicate_lines"] += 1
        else:
            demand = demands[merge_key] = {
                "demand_key": json.dumps([order_key, sku_key, normalized_unit], ensure_ascii=False, separators=(",", ":")),
                "order_key": order_key,
                "outbound_order_number": order["outbound_order_number"],
                "sku_key": sku_key,
                "sku_code": sku_code,
                "sku_name": sku_name,
                "characteristic_code": characteristic_code,
                "characteristic_name": characteristic_name,
                "requested_units": quantity,
                "unit_name": display_unit,
                "source_indexes": set(),
                "_normalized_unit": normalized_unit,
            }
        if isinstance(source_index, int) and not isinstance(source_index, bool):
            demand["source_indexes"].add(source_index)

    orders = []
    for order in orders_by_key.values():
        demand_values = list(order["demands_by_merge_key"].values())
        if not demand_values:
            diagnostics["orders_without_valid_demands"] += 1
            continue
        sku_units: dict[str, set[str]] = {}
        for demand in demand_values:
            sku_units.setdefault(demand["sku_key"], set()).add(demand["_normalized_unit"])
        diagnostics["unit_variants_for_same_sku"] += sum(len(units) > 1 for units in sku_units.values())
        demand_values.sort(key=lambda item: (
            min(item["source_indexes"], default=float("inf")), item["sku_key"],
            item["_normalized_unit"], item["demand_key"],
        ))
        for demand in demand_values:
            demand["outbound_order_number"] = order["outbound_order_number"]
            demand.update(order["sku_metadata_by_key"][demand["sku_key"]])
            demand["source_indexes"] = sorted(demand["source_indexes"])
            demand.pop("_normalized_unit")
        orders.append({
            "order_key": order["order_key"],
            "outbound_order_number": order["outbound_order_number"],
            "created_at": order["created_at"],
            "warehouse": order["warehouse"],
            "source_indexes": sorted(order["source_indexes"]),
            "demands": demand_values,
        })
    orders.sort(key=lambda item: (
        item["created_at"], item["outbound_order_number"],
        min(item["source_indexes"], default=float("inf")), item["order_key"],
    ))
    diagnostics["orders_count"] = len(orders)
    diagnostics["demands_count"] = sum(len(order["demands"]) for order in orders)
    return {"orders": orders, "diagnostics": diagnostics}
