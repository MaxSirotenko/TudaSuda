"""Read-only scenario adapter for authoritative factual effective views.

Registry metadata is used for selectors. Canonical partitions are opened only
once a day has been selected. This module never parses workbooks or persists a
second source of truth.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from warehouse_actual_inventory_import import classify_physical_pallet_evidence
from warehouse_business_identity import normalize_warehouse
from warehouse_factual_data import (DATA_ROOT, active_datasets, load_effective_placement,
    load_effective_rows, load_registry, normalize_operational_day)
from warehouse_outbound_orders import outbound_order_key
from warehouse_receipts import calculate_receipt_zones
from warehouse_weight_rules import load_weight_rules


def _registry(registry: Mapping[str, Any] | None, root: Path) -> Mapping[str, Any]:
    return registry if registry is not None else load_registry(root)


def _index_warehouses(dataset: Mapping[str, Any], day: str | None = None) -> set[str]:
    index = dataset.get("index", {})
    values = ((index.get("warehouses_by_date", {}).get(day) or
               index.get("daily", {}).get(day, {}).get("warehouses", [])) if day else index.get("warehouses", []))
    return {value for raw in values if (value := normalize_warehouse(raw))}


def available_warehouses(*, source_types: tuple[str, ...] = ("outbound",), registry: Mapping[str, Any] | None = None,
                         root: Path = DATA_ROOT) -> list[str]:
    """Return compact indexed warehouse scopes without opening row artifacts."""
    reg = _registry(registry, root); scopes: list[set[str]] = []
    for source_type in source_types:
        values = set().union(*(_index_warehouses(dataset) for dataset in active_datasets(reg, source_type)))
        if values: scopes.append(values)
    return sorted(set.intersection(*scopes) if scopes else set())


def available_operational_dates(*, warehouse: Any,
                                required_sources: tuple[str, ...] = ("historical_placement", "outbound"),
                                registry: Mapping[str, Any] | None = None, root: Path = DATA_ROOT) -> list[str]:
    """Intersect compact date/warehouse indexes; do not read daily partitions."""
    reg = _registry(registry, root); target = normalize_warehouse(warehouse); date_sets: list[set[str]] = []
    for source_type in required_sources:
        values: set[str] = set()
        for dataset in active_datasets(reg, source_type):
            for raw_day in dataset.get("index", {}).get("dates", dataset.get("partitions", [])):
                day = normalize_operational_day(raw_day)
                if day and (source_type == "historical_placement" or target in _index_warehouses(dataset, day)):
                    values.add(day)
        date_sets.append(values)
    return sorted(set.intersection(*date_sets) if date_sets else set())


def available_source_dates(source_type: str, *, warehouse: Any = None,
                           registry: Mapping[str, Any] | None = None, root: Path = DATA_ROOT) -> list[str]:
    """Return compact indexed dates for one source and optional warehouse."""
    reg = _registry(registry, root); target = normalize_warehouse(warehouse); values: set[str] = set()
    for dataset in active_datasets(reg, source_type):
        for raw_day in dataset.get("index", {}).get("dates", dataset.get("partitions", [])):
            day = normalize_operational_day(raw_day)
            if day and (not target or target in _index_warehouses(dataset, day)):
                values.add(day)
    return sorted(values)


def _result(source_type: str, day: str | None, warehouse: str | None,
            view: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"source": "factual", "source_type": source_type, "operational_date": day, "warehouse": warehouse,
            "rows": rows, "authoritative": bool(view.get("authoritative")) and not view.get("conflicts"),
            "blockers": list(view.get("conflicts") or []), "duplicates": list(view.get("duplicates") or [])}


def load_outbound_for_day(operational_date: Any, warehouse: Any, *, registry: Mapping[str, Any] | None = None,
                          root: Path = DATA_ROOT) -> dict[str, Any]:
    """Adapt canonical box demand; outbound pick order is optional evidence."""
    day = normalize_operational_day(operational_date); target = normalize_warehouse(warehouse)
    view = load_effective_rows("outbound", day, registry=_registry(registry, root), root=root, strict=False); rows = []
    if not view["conflicts"]:
        for factual in view["rows"]:
            if normalize_warehouse(factual.get("warehouse")) != target: continue
            quantity = factual.get("quantity")
            if not isinstance(quantity, (int, float)) or quantity <= 0: continue
            created = factual.get("occurred_at") or day
            number = factual.get("document_number") or factual.get("document_ref") or ""
            pick_order = factual.get("source_pick_order")
            rows.append({"outbound_order_ref": factual.get("document_ref"), "outbound_order_number": number,
                "created_at": created, "warehouse": factual.get("warehouse"), "nomenclature": factual.get("nomenclature"),
                "characteristic": factual.get("characteristic"), "sku_key": factual.get("sku_key"),
                "qty_units": int(quantity) if float(quantity).is_integer() else quantity, "calculated_box_qty": quantity,
                "unit_name": "короб", "line_number": factual.get("line_number"), "pick_order": pick_order,
                "pick_order_validation_reason": "", "route_sequence_authoritative": True,
                "route_sequence_source": "optional_outbound_order" if pick_order is not None else "physical_pick_locations",
                "order_key": outbound_order_key(factual.get("warehouse"), number, created), "line_status": "not_processed",
                "factual_row": factual})
    return _result("outbound", day, target, view, rows)


def build_start_state(operational_date: Any, warehouse: Any, model: Mapping[str, Any], *,
                      warehouse_binding: Any = None, registry: Mapping[str, Any] | None = None,
                      root: Path = DATA_ROOT) -> dict[str, Any]:
    """Build the existing physical opening contract from an exact snapshot.

    Historical placement has no warehouse column. The caller must provide an
    explicit binding (or a persisted ``factual_warehouse_binding`` on the model)
    matching the selected outbound scope; absence/mismatch is a blocker.
    """
    day = normalize_operational_day(operational_date); target = normalize_warehouse(warehouse)
    binding = normalize_warehouse(warehouse_binding or model.get("factual_warehouse_binding"))
    view = load_effective_placement(day or "", model, registry=_registry(registry, root), root=root, strict=False)
    blockers = list(view.get("conflicts") or [])
    if not binding: blockers.append({"code": "historical_placement_warehouse_binding_required", "warehouse": target})
    elif binding != target: blockers.append({"code": "historical_placement_warehouse_binding_mismatch",
                                              "binding": binding, "warehouse": target})
    unresolved = [row for row in view["rows"] if row.get("cell_resolution_status") != "resolved"]
    if unresolved: blockers.append({"code": "historical_cell_resolution_blocked", "rows": len(unresolved)})
    cells = {str(cell.get("cell_key")): cell for cell in model.get("cells", []) if isinstance(cell, Mapping)}
    placements = []
    if not blockers:
        for row in view["rows"]:
            quantity = row.get("source_stock_quantity"); cell_key = str(row.get("resolved_geometry_cell_key"))
            cell = cells.get(cell_key, {})
            seed = json.dumps([row.get("dataset_id"), row.get("source_row"), row.get("sku_key"), row.get("source_cell")], ensure_ascii=False)
            placements.append({"placement_id": "factual-" + hashlib.sha256(seed.encode()).hexdigest()[:20],
                "sku_key": row.get("sku_key"), "sku_name": row.get("nomenclature"), "item_name": row.get("nomenclature"),
                "nomenclature": row.get("nomenclature"), "characteristic": row.get("characteristic"),
                "characteristic_name": row.get("characteristic"), "cell_key": cell_key,
                "row_number": cell.get("row_number"), "cell_number": cell.get("cell_number"), "tier": cell.get("tier"),
                "source_cell": row.get("source_cell"), "source_pallet_ref": row.get("source_pallet_ref"),
                "qty_units": quantity, "qty_boxes": quantity, "unit_name": "короб", "warehouse": target,
                "normalized_warehouse": target, "source": "factual_historical_placement", "confidence": "exact",
                "placement_mode": "factual", "placement_status": "placed", "factual_row": row})
    state = {"model_id": model.get("model_id"), "placements": placements, "excluded_inventory": [],
             "unmatched_inventory": unresolved, "unplaced_inventory": [], "settings": {}, "journal": []}
    if not blockers:
        diagnostics = {"excluded_rows": 0, "unmatched_rows": 0, "unknown_cell": 0}
        classify_physical_pallet_evidence(dict(model), state, diagnostics)
    result = _result("historical_placement", day, target, view, placements)
    result.update(state=state, blockers=blockers, authoritative=not blockers and bool(view.get("authoritative")))
    return result


def _scoped(source_type: str, operational_date: Any = None, warehouse: Any = None, *, registry=None,
            root=DATA_ROOT) -> dict[str, Any]:
    day = normalize_operational_day(operational_date) if operational_date else None; target = normalize_warehouse(warehouse)
    view = load_effective_rows(source_type, day, registry=_registry(registry, root), root=root, strict=False)
    rows = [] if view["conflicts"] else [dict(row) for row in view["rows"]
        if not target or source_type == "vgh" or normalize_warehouse(row.get("warehouse")) == target]
    return _result(source_type, day, target or None, view, rows)


def _receipt_overlay(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {"receipt_id": f"factual:{row.get('dataset_id')}:{row.get('source_row', index)}",
        "receipt_line_id": f"factual:{row.get('dataset_id')}:{row.get('source_row', index)}",
        "source_row_number": row.get("source_row", index), "sku_key": row.get("sku_key"),
        "receipt_date": row.get("occurred_at"), "receipt_number": row.get("document_number"),
        "receipt_document": row.get("document_ref"), "warehouse": row.get("warehouse"),
        "sku_name": row.get("nomenclature"), "nomenclature": row.get("nomenclature"),
        "characteristic_name": row.get("characteristic"), "characteristic": row.get("characteristic"),
        "qty_units": row.get("box_quantity") or 0, "qty_boxes": row.get("box_quantity") or 0,
        "qty_pallets": row.get("reported_pallets") or 0, "placement_status": "not_placed",
        "placement_mode": "not_calculated", "source_zone": "", "calculated_zone": "unassigned",
        "source_weight": None, "source_weight_raw": "", "weight_parse_status": "not_supplied",
        "weight_parse_reason": "Вес берётся из factual VGH", "fragile_flag": False,
        "zone_calculation_status": "not_calculated", "factual_row": dict(row)}


def load_receipts_for_day(operational_date: Any, warehouse: Any, **kwargs: Any) -> dict[str, Any]:
    result = _scoped("receipts", operational_date, warehouse, **kwargs)
    accepted = [{"receipt_line_key": f"factual:{row.get('dataset_id')}:{row.get('source_row', index)}",
        "document_key": f"ref:{row.get('document_ref') or row.get('document_number') or ''}",
        "receipt_ref": row.get("document_ref"), "receipt_number": row.get("document_number"),
        "receipt_date": row.get("occurred_at"), "warehouse": row.get("warehouse"), "line_number": row.get("line_number"),
        "sku_key": row.get("sku_key"), "nomenclature": row.get("nomenclature"), "characteristic": row.get("characteristic"),
        "source_box_quantity": row.get("box_quantity"), "qty_units": row.get("box_quantity"), "unit_name": "короб",
        "terminal_receipt_completed": row.get("terminal_completed"), "expected_receipt": row.get("expected_receipt"),
        "source_index": row.get("source_row", index), "factual_row": row} for index, row in enumerate(result["rows"])]
    result["state"] = {"accepted_rows": accepted, "pending_receipt_rows": [], "zero_receipt_rows": [],
        "excluded_receipt_rows": [], "documents": [], "document_keys": sorted({r["document_key"] for r in accepted})}
    result["classification_inputs"] = [_receipt_overlay(row, index) for index, row in enumerate(result["rows"])]
    return result


def load_inventory_for_day(operational_date: Any, warehouse: Any, **kwargs: Any) -> dict[str, Any]:
    result = _scoped("inventory", operational_date, warehouse, **kwargs)
    result["rows"] = [{"sku_key": row.get("sku_key"), "nomenclature": row.get("nomenclature"),
        "characteristic": row.get("characteristic"), "warehouse": row.get("warehouse"),
        "normalized_warehouse": normalize_warehouse(row.get("warehouse")), "qty_units": row.get("actual_quantity"),
        "unit_name": "короб", "inventory_ref": row.get("inventory_ref"), "line_number": row.get("line_number"),
        "factual_row": row} for row in result["rows"]]
    return result


def load_vgh_attributes(**kwargs: Any) -> dict[str, Any]:
    return _scoped("vgh", **kwargs)


def build_factual_weight_classifications(operational_date: Any, warehouse: Any, *,
                                         registry: Mapping[str, Any] | None = None, root: Path = DATA_ROOT,
                                         rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Apply the existing PR #190 VGH workflow and persisted user bands."""
    reg = _registry(registry, root)
    receipts = load_receipts_for_day(operational_date, warehouse, registry=reg, root=root)
    vgh = load_vgh_attributes(registry=reg, root=root)
    blockers = [*receipts["blockers"], *vgh["blockers"]]
    if blockers: return {"source": "factual", "rows": [], "diagnostics": {}, "blockers": blockers, "authoritative": False}
    configured = dict(rules) if rules is not None else load_weight_rules()
    rows, diagnostics = calculate_receipt_zones(receipts["classification_inputs"],
                                                {"weight_bands": configured.get("bands", {})}, vgh["rows"])
    return {"source": "factual_receipts_vgh", "rows": rows, "diagnostics": diagnostics,
            "blockers": [], "authoritative": True}
