"""Scenario-facing adapter for the authoritative factual Data Layer.

This module deliberately contains no workbook parser and no persistence.  It
only scopes effective factual views and translates their canonical records to
the stable input shapes consumed by the existing one-day simulators.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from warehouse_business_identity import normalize_warehouse
from warehouse_factual_data import DATA_ROOT, active_datasets, load_effective_placement, load_effective_rows, load_registry, normalize_operational_day
from warehouse_outbound_orders import outbound_order_key


def _registry(registry: Mapping[str, Any] | None, root: Path) -> Mapping[str, Any]:
    return registry if registry is not None else load_registry(root)


def available_warehouses(*, source_types: tuple[str, ...] = ("outbound",), registry: Mapping[str, Any] | None = None,
                         root: Path = DATA_ROOT) -> list[str]:
    """Return exact warehouse scopes advertised by authoritative sources."""
    reg = _registry(registry, root)
    scopes: list[set[str]] = []
    for source_type in source_types:
        rows = load_effective_rows(source_type, registry=reg, root=root, strict=False)
        if rows["conflicts"]:
            continue
        values = {value for row in rows["rows"] if (value := normalize_warehouse(row.get("warehouse")))}
        if values:
            scopes.append(values)
    return sorted(set.intersection(*scopes) if scopes else set())


def available_operational_dates(*, warehouse: Any, required_sources: tuple[str, ...] = ("historical_placement", "outbound"),
                                registry: Mapping[str, Any] | None = None, root: Path = DATA_ROOT) -> list[str]:
    """Return days where every mandatory factual family is present for scope."""
    reg = _registry(registry, root); target = normalize_warehouse(warehouse)
    dates: list[set[str]] = []
    for source_type in required_sources:
        source_dates: set[str] = set()
        for dataset in active_datasets(reg, source_type):
            for raw_day in dataset.get("index", {}).get("dates", dataset.get("partitions", [])):
                day = normalize_operational_day(raw_day)
                if not day:
                    continue
                if source_type == "historical_placement":
                    source_dates.add(day)
                    continue
                view = load_effective_rows(source_type, day, registry=reg, root=root, strict=False)
                if not view["conflicts"] and any(normalize_warehouse(row.get("warehouse")) == target for row in view["rows"]):
                    source_dates.add(day)
        dates.append(source_dates)
    return sorted(set.intersection(*dates) if dates else set())


def _result(source_type: str, day: str | None, warehouse: str | None, view: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"source": "factual", "source_type": source_type, "operational_date": day, "warehouse": warehouse,
            "rows": rows, "authoritative": bool(view.get("authoritative")) and not view.get("conflicts"),
            "blockers": list(view.get("conflicts") or []), "duplicates": list(view.get("duplicates") or [])}


def load_outbound_for_day(operational_date: Any, warehouse: Any, *, registry: Mapping[str, Any] | None = None,
                          root: Path = DATA_ROOT) -> dict[str, Any]:
    day = normalize_operational_day(operational_date); target = normalize_warehouse(warehouse)
    view = load_effective_rows("outbound", day, registry=_registry(registry, root), root=root, strict=False)
    rows = []
    if not view["conflicts"]:
        for factual in view["rows"]:
            if normalize_warehouse(factual.get("warehouse")) != target:
                continue
            quantity = factual.get("quantity")
            if not isinstance(quantity, (int, float)) or quantity <= 0:
                continue
            created = factual.get("occurred_at") or day
            number = factual.get("document_number") or factual.get("document_ref") or ""
            pick_order = factual.get("source_pick_order")
            rows.append({"outbound_order_ref": factual.get("document_ref"), "outbound_order_number": number,
                "created_at": created, "warehouse": factual.get("warehouse"), "nomenclature": factual.get("nomenclature"),
                "characteristic": factual.get("characteristic"), "sku_key": factual.get("sku_key"),
                "qty_units": int(quantity) if float(quantity).is_integer() else quantity, "calculated_box_qty": quantity,
                "unit_name": "короб", "line_number": factual.get("line_number"), "pick_order": pick_order,
                "pick_order_validation_reason": "" if isinstance(pick_order, (int, float)) and pick_order > 0 else "pick_order_missing",
                "route_sequence_authoritative": isinstance(pick_order, (int, float)) and pick_order > 0,
                "order_key": outbound_order_key(factual.get("warehouse"), number, created), "line_status": "not_processed",
                "factual_row": factual})
    return _result("outbound", day, target, view, rows)


def build_start_state(operational_date: Any, warehouse: Any, model: Mapping[str, Any], *,
                      registry: Mapping[str, Any] | None = None, root: Path = DATA_ROOT) -> dict[str, Any]:
    day = normalize_operational_day(operational_date); target = normalize_warehouse(warehouse)
    view = load_effective_placement(day or "", model, registry=_registry(registry, root), root=root, strict=False)
    blockers = list(view.get("conflicts") or [])
    unresolved = [row for row in view["rows"] if row.get("cell_resolution_status") != "resolved"]
    if unresolved:
        blockers.append({"code": "historical_cell_resolution_blocked", "rows": len(unresolved)})
    placements = []
    if not blockers:
        for row in view["rows"]:
            quantity = row.get("source_stock_quantity")
            seed = json.dumps([row.get("dataset_id"), row.get("source_row"), row.get("sku_key"), row.get("source_cell")], ensure_ascii=False)
            placements.append({"placement_id": "factual-" + hashlib.sha256(seed.encode()).hexdigest()[:20],
                "sku_key": row.get("sku_key"), "sku_name": row.get("nomenclature"), "item_name": row.get("nomenclature"),
                "nomenclature": row.get("nomenclature"), "characteristic": row.get("characteristic"),
                "characteristic_name": row.get("characteristic"), "cell_key": row.get("resolved_geometry_cell_key"),
                "source_cell": row.get("source_cell"), "source_pallet_ref": row.get("source_pallet_ref"),
                "qty_units": quantity, "qty_boxes": quantity, "unit_name": "короб", "warehouse": target,
                "normalized_warehouse": target, "source": "factual_historical_placement", "confidence": "exact",
                "placement_mode": "factual", "placement_status": "placed", "factual_row": row})
    state = {"model_id": model.get("model_id"), "placements": placements, "excluded_inventory": [],
             "unmatched_inventory": unresolved, "unplaced_inventory": [], "settings": {}, "journal": []}
    result = _result("historical_placement", day, target, view, placements)
    result.update(state=state, blockers=blockers, authoritative=not blockers and bool(view.get("authoritative")))
    return result


def _scoped(source_type: str, operational_date: Any = None, warehouse: Any = None, *, registry=None, root=DATA_ROOT) -> dict[str, Any]:
    day = normalize_operational_day(operational_date) if operational_date else None; target = normalize_warehouse(warehouse)
    view = load_effective_rows(source_type, day, registry=_registry(registry, root), root=root, strict=False)
    rows = [] if view["conflicts"] else [dict(row) for row in view["rows"]
        if not target or source_type == "vgh" or normalize_warehouse(row.get("warehouse")) == target]
    return _result(source_type, day, target or None, view, rows)


def load_receipts_for_day(operational_date: Any, warehouse: Any, **kwargs: Any) -> dict[str, Any]:
    result = _scoped("receipts", operational_date, warehouse, **kwargs)
    accepted = []
    for index, row in enumerate(result["rows"]):
        document = row.get("document_ref") or row.get("document_number") or ""
        accepted.append({"receipt_line_key": f"factual:{row.get('dataset_id')}:{row.get('source_row', index)}",
            "document_key": f"ref:{document}", "receipt_ref": row.get("document_ref"),
            "receipt_number": row.get("document_number"), "receipt_date": row.get("occurred_at"),
            "warehouse": row.get("warehouse"), "line_number": row.get("line_number"), "sku_key": row.get("sku_key"),
            "nomenclature": row.get("nomenclature"), "characteristic": row.get("characteristic"),
            "source_box_quantity": row.get("box_quantity"), "qty_units": row.get("box_quantity"), "unit_name": "короб",
            "terminal_receipt_completed": row.get("terminal_completed"), "expected_receipt": row.get("expected_receipt"),
            "source_index": row.get("source_row", index), "factual_row": row})
    result["state"] = {"accepted_rows": accepted, "pending_receipt_rows": [], "zero_receipt_rows": [],
                       "excluded_receipt_rows": [], "documents": [], "document_keys": sorted({r["document_key"] for r in accepted})}
    return result


def load_inventory_for_day(operational_date: Any, warehouse: Any, **kwargs: Any) -> dict[str, Any]:
    return _scoped("inventory", operational_date, warehouse, **kwargs)


def load_vgh_attributes(**kwargs: Any) -> dict[str, Any]:
    return _scoped("vgh", **kwargs)
