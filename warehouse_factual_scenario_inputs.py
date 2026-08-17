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
from datetime import date, timedelta

from warehouse_actual_inventory_import import classify_physical_pallet_evidence
from warehouse_business_identity import normalize_warehouse
from warehouse_factual_data import (DATA_ROOT, PARSER_VERSION, active_datasets, load_effective_placement,
    load_effective_rows, load_registry, normalize_operational_day)
from warehouse_outbound_orders import outbound_order_key
from warehouse_receipts import calculate_receipt_zones
from warehouse_weight_rules import load_weight_rules
from warehouse_monthly_fact_replay import resolve_factual_route_order
from warehouse_physical_graph import build_physical_warehouse_graph
from warehouse_day_receipts_import import normalize_receipt_boolean


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


def _lifecycle_blockers(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    blockers = [dict(item) for item in registry.get("diagnostics", [])
                if item.get("code") == "registry_activation_review_required"]
    incompatible = [item.get("dataset_id") for item in registry.get("datasets", [])
                    if item.get("active", True) and item.get("parser_version")
                    and item.get("parser_version") != PARSER_VERSION]
    if incompatible: blockers.append({"code": "parser_reimport_required", "dataset_ids": incompatible})
    return blockers


def load_outbound_for_day(operational_date: Any, warehouse: Any, *, registry: Mapping[str, Any] | None = None,
                          root: Path = DATA_ROOT) -> dict[str, Any]:
    """Adapt canonical box demand; outbound pick order is optional evidence."""
    day = normalize_operational_day(operational_date); target = normalize_warehouse(warehouse)
    reg = _registry(registry, root); lifecycle = _lifecycle_blockers(reg)
    if lifecycle:
        return {"source": "factual", "source_type": "outbound", "operational_date": day,
                "warehouse": target, "rows": [], "authoritative": False, "blockers": lifecycle, "duplicates": []}
    view = load_effective_rows("outbound", day, registry=reg, root=root, strict=False,
                               warehouse=target); rows = []; quantity_blockers = []; confirmed_zero = 0
    if not view["conflicts"]:
        for factual in view["rows"]:
            row_warehouse = normalize_warehouse(factual.get("warehouse"))
            if not row_warehouse:
                quantity_blockers.append({"code": "factual_outbound_warehouse_missing",
                                          "dataset_id": factual.get("dataset_id"), "source_row": factual.get("source_row")})
                continue
            if row_warehouse != target: continue
            if not str(factual.get("sku_key") or "").strip():
                quantity_blockers.append({"code": "factual_outbound_sku_identity_missing",
                    "dataset_id": factual.get("dataset_id"), "source_row": factual.get("source_row")})
                continue
            quantity = factual.get("quantity"); reason = None
            if quantity is None: reason = "factual_outbound_quantity_missing"
            elif isinstance(quantity, bool) or not isinstance(quantity, (int, float)): reason = "factual_outbound_quantity_invalid"
            elif quantity < 0: reason = "factual_outbound_quantity_negative"
            elif not float(quantity).is_integer(): reason = "factual_outbound_quantity_fractional"
            elif quantity == 0:
                confirmed_zero += 1; continue
            if reason:
                quantity_blockers.append({"code": reason, "dataset_id": factual.get("dataset_id"),
                                          "source_row": factual.get("source_row")})
            created = factual.get("occurred_at") or day
            number = factual.get("document_number") or factual.get("document_ref") or ""
            if not number:
                quantity_blockers.append({"code": "factual_outbound_document_identity_missing",
                                          "dataset_id": factual.get("dataset_id"), "source_row": factual.get("source_row")})
            pick_order = factual.get("source_pick_order")
            pick_order_valid = (pick_order is None or
                isinstance(pick_order, (int, float)) and not isinstance(pick_order, bool)
                and float(pick_order).is_integer() and pick_order >= 0)
            rows.append({"outbound_order_ref": factual.get("document_ref"), "outbound_order_number": number,
                "created_at": created, "warehouse": factual.get("warehouse"), "nomenclature": factual.get("nomenclature"),
                "characteristic": factual.get("characteristic"), "sku_key": factual.get("sku_key"),
                "qty_units": None if reason else int(quantity), "calculated_box_qty": quantity,
                "unit_name": "короб", "line_number": factual.get("line_number"), "pick_order": pick_order,
                "quantity_validation_reason": reason or "",
                "pick_order_validation_reason": "" if pick_order_valid else "optional_source_pick_order_invalid",
                "route_sequence_authoritative": False, "route_sequence_source": "unresolved",
                "order_key": outbound_order_key(factual.get("warehouse"), number, created), "line_status": "not_processed",
                "factual_row": factual})
    result = _result("outbound", day, target, view, rows)
    result["blockers"] = [*result["blockers"], *quantity_blockers]
    result["authoritative"] = result["authoritative"] and not quantity_blockers
    result["diagnostics"] = {"confirmed_zero_non_demand_rows": confirmed_zero,
                             "invalid_quantity_rows": len(quantity_blockers)}
    return result


def load_routed_outbound_for_day(operational_date: Any, warehouse: Any, model: Mapping[str, Any], *,
                                 warehouse_binding: Any = None, registry: Mapping[str, Any] | None = None,
                                 root: Path = DATA_ROOT) -> dict[str, Any]:
    """Resolve one-day demand order from the same historical cell authority as monthly FACT."""
    reg = _registry(registry, root); outbound = load_outbound_for_day(operational_date, warehouse, registry=reg, root=root)
    if outbound["blockers"]: return outbound
    target = normalize_warehouse(warehouse); binding = normalize_warehouse(warehouse_binding or model.get("factual_warehouse_binding"))
    if not binding or binding != target:
        outbound.update(rows=[], authoritative=False, blockers=[{"code": "historical_placement_warehouse_binding_required"
            if not binding else "historical_placement_warehouse_binding_mismatch", "warehouse": target, "binding": binding}])
        return outbound
    placement = load_effective_placement(normalize_operational_day(operational_date) or "", model,
                                         registry=reg, root=root, strict=False)
    graph, _ = build_physical_warehouse_graph(dict(model), None)
    routable_cells = {str(item.get("cell_key")) for item in graph.get("cell_access_links", []) if item.get("cell_key")}
    blockers = list(placement.get("conflicts") or []); by_sku: dict[str, list[Mapping[str, Any]]] = {}
    for row in placement.get("rows", []):
        quantity = row.get("source_stock_quantity")
        if isinstance(quantity, (int, float)) and not isinstance(quantity, bool) and quantity > 0:
            by_sku.setdefault(str(row.get("sku_key") or ""), []).append(row)
    routed = []
    for row in outbound["rows"]:
        candidates = by_sku.get(str(row.get("sku_key") or ""), [])
        authority = resolve_factual_route_order(candidates, routable_cells=routable_cells); code = authority["code"]
        if code:
            blockers.append({"code": code, "sku_key": row.get("sku_key")}); continue
        routed.append({**row, "pick_order": authority["cell_picking_order"],
                       "route_sequence_authoritative": True, "route_sequence_source": "historical_cell_picking_order",
                       "factual_source_cell": authority["factual_source_cells"][0],
                       "factual_geometry_cell": authority["factual_geometry_cells"][0]})
    outbound.update(rows=routed if not blockers else [], blockers=blockers,
                    authoritative=outbound["authoritative"] and not blockers)
    return outbound


def load_outbound_history(operational_date: Any, warehouse: Any, *, days: int = 28,
                          registry: Mapping[str, Any] | None = None, root: Path = DATA_ROOT) -> dict[str, Any]:
    """Read only daily partitions in ``[D-days, D)`` for velocity."""
    end = date.fromisoformat(normalize_operational_day(operational_date) or "")
    reg = _registry(registry, root); rows = []; blockers = _lifecycle_blockers(reg)
    if blockers: return {"source": "factual", "rows": [], "blockers": blockers, "authoritative": False}
    indexed_days = {normalize_operational_day(raw) for dataset in active_datasets(reg, "outbound")
                    for raw in dataset.get("index", {}).get("dates", dataset.get("partitions", []))}
    for offset in range(days, 0, -1):
        day = (end - timedelta(days=offset)).isoformat()
        if day not in indexed_days: continue
        result = load_outbound_for_day(day, warehouse, registry=reg, root=root)
        blockers.extend(result["blockers"]); rows.extend(result["rows"])
    return {"source": "factual_outbound_history", "rows": rows, "blockers": blockers,
            "authoritative": not blockers, "period_from": (end-timedelta(days=days)).isoformat(),
            "period_to_exclusive": end.isoformat()}


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
    reg = _registry(registry, root); lifecycle = _lifecycle_blockers(reg)
    view = load_effective_placement(day or "", model, registry=reg, root=root, strict=False)
    blockers = [*lifecycle, *(view.get("conflicts") or [])]
    if not binding: blockers.append({"code": "historical_placement_warehouse_binding_required", "warehouse": target})
    elif binding != target: blockers.append({"code": "historical_placement_warehouse_binding_mismatch",
                                              "binding": binding, "warehouse": target})
    unresolved = [row for row in view["rows"] if row.get("cell_resolution_status") != "resolved"]
    if unresolved: blockers.append({"code": "historical_cell_resolution_blocked", "rows": len(unresolved)})
    invalid_quantities = [row for row in view["rows"] if isinstance(row.get("source_stock_quantity"), bool)
        or not isinstance(row.get("source_stock_quantity"), (int, float))
        or row.get("source_stock_quantity") < 0 or not float(row.get("source_stock_quantity")).is_integer()]
    if invalid_quantities:
        blockers.append({"code": "historical_stock_quantity_invalid", "rows": len(invalid_quantities),
                         "source_rows": [row.get("source_row") for row in invalid_quantities[:20]]})
    invalid_skus = [row for row in view["rows"] if row.get("source_stock_quantity") not in (None, 0)
                    and not str(row.get("sku_key") or "").strip()]
    if invalid_skus:
        blockers.append({"code": "historical_sku_identity_invalid", "rows": len(invalid_skus),
                         "source_rows": [row.get("source_row") for row in invalid_skus[:20]]})
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
    reg = _registry(registry, root); lifecycle = _lifecycle_blockers(reg)
    if lifecycle: return {"source": "factual", "source_type": source_type, "operational_date": day,
        "warehouse": target or None, "rows": [], "authoritative": False, "blockers": lifecycle, "duplicates": []}
    view = load_effective_rows(source_type, day, registry=reg, root=root, strict=False,
                               warehouse=target or None)
    rows = [] if view["conflicts"] else [dict(row) for row in view["rows"]
        if not target or source_type == "vgh" or not normalize_warehouse(row.get("warehouse"))
        or normalize_warehouse(row.get("warehouse")) == target]
    return _result(source_type, day, target or None, view, rows)


def _receipt_overlay(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    terminal = normalize_receipt_boolean(row.get("terminal_completed"))
    expected = normalize_receipt_boolean(row.get("expected_receipt"))
    pallets = row.get("reported_pallets")
    usable_pallets = (isinstance(pallets, (int, float)) and not isinstance(pallets, bool) and pallets > 0)
    return {"receipt_id": f"factual:{row.get('dataset_id')}:{row.get('source_row', index)}",
        "receipt_line_id": f"factual:{row.get('dataset_id')}:{row.get('source_row', index)}",
        "source_row_number": row.get("source_row", index), "sku_key": row.get("sku_key"),
        "receipt_date": row.get("occurred_at"), "receipt_number": row.get("document_number"),
        "receipt_document": row.get("document_ref"), "warehouse": row.get("warehouse"),
        "sku_name": row.get("nomenclature"), "nomenclature": row.get("nomenclature"),
        "characteristic_name": row.get("characteristic"), "characteristic": row.get("characteristic"),
        "qty_units": row.get("box_quantity") or 0, "qty_boxes": row.get("box_quantity") or 0,
        "qty_pallets": pallets if usable_pallets else None, "placement_status": "not_placed",
        "placement_mode": "not_calculated", "source_zone": "", "calculated_zone": "unassigned",
        "source_weight": None, "source_weight_raw": "", "weight_parse_status": "not_supplied",
        "weight_parse_reason": "Вес берётся из factual VGH", "fragile_flag": False,
        "terminal_receipt_completed": terminal, "expected_receipt": expected,
        "placement_eligible": terminal is True and usable_pallets,
        "zone_calculation_status": "not_calculated", "factual_row": dict(row)}


def load_receipts_for_day(operational_date: Any, warehouse: Any, **kwargs: Any) -> dict[str, Any]:
    result = _scoped("receipts", operational_date, warehouse, **kwargs)
    identityless = [row for row in result["rows"] if not str(row.get("document_ref") or "").strip()]
    unscoped = [row for row in result["rows"] if not normalize_warehouse(row.get("warehouse"))]
    source_blockers = []
    if identityless:
        source_blockers.append({"code": "factual_receipt_document_identity_missing", "rows": len(identityless),
            "source_rows": [row.get("source_row") for row in identityless[:20]]})
    if unscoped:
        source_blockers.append({"code": "factual_receipt_warehouse_missing", "rows": len(unscoped),
            "source_rows": [row.get("source_row") for row in unscoped[:20]]})
    if source_blockers:
        result["blockers"] = [*result["blockers"], *source_blockers]
        result["authoritative"] = False
    completed = {id(row): normalize_receipt_boolean(row.get("terminal_completed")) for row in result["rows"]}
    invalid = [row for row in result["rows"] if completed[id(row)] is True and
        (isinstance(row.get("box_quantity"), bool) or not isinstance(row.get("box_quantity"), (int, float))
         or row.get("box_quantity") <= 0 or not float(row.get("box_quantity")).is_integer())]
    invalid_pallets = [row for row in result["rows"] if completed[id(row)] is True and
        (isinstance(row.get("reported_pallets"), bool)
         or not isinstance(row.get("reported_pallets"), (int, float)) or row.get("reported_pallets") <= 0)]
    invalid_skus = [row for row in result["rows"] if completed[id(row)] is True
                    and not str(row.get("sku_key") or "").strip()]
    if invalid:
        result["blockers"] = [*result["blockers"], {"code": "factual_receipt_box_quantity_invalid",
            "rows": len(invalid), "source_rows": [row.get("source_row") for row in invalid[:20]]}]
        result["authoritative"] = False
    if invalid_pallets:
        result["blockers"] = [*result["blockers"], {"code": "factual_receipt_pallet_quantity_missing_or_invalid",
            "rows": len(invalid_pallets), "source_rows": [row.get("source_row") for row in invalid_pallets[:20]]}]
        result["authoritative"] = False
    if invalid_skus:
        result["blockers"] = [*result["blockers"], {"code": "factual_receipt_sku_identity_missing",
            "rows": len(invalid_skus), "evidence": [{"dataset_id": row.get("dataset_id"),
            "source_row": row.get("source_row")} for row in invalid_skus[:20]]}]
        result["authoritative"] = False
    accepted = [{"receipt_line_key": f"factual:{row.get('dataset_id')}:{row.get('source_row', index)}",
        "document_key": f"ref:{row.get('document_ref') or row.get('document_number') or ''}",
        "receipt_ref": row.get("document_ref"), "receipt_number": row.get("document_number"),
        "receipt_date": row.get("occurred_at"), "warehouse": row.get("warehouse"), "line_number": row.get("line_number"),
        "sku_key": row.get("sku_key"), "nomenclature": row.get("nomenclature"), "characteristic": row.get("characteristic"),
        "source_box_quantity": row.get("box_quantity"), "qty_units": row.get("box_quantity"), "unit_name": "короб",
        "terminal_receipt_completed": completed[id(row)],
        "expected_receipt": normalize_receipt_boolean(row.get("expected_receipt")),
        "source_index": row.get("source_row", index), "factual_row": row} for index, row in enumerate(result["rows"])
        if completed[id(row)] is True and row not in invalid and row not in invalid_pallets and row not in invalid_skus
        and row not in identityless and row not in unscoped]
    if result["blockers"]:
        accepted = []
    pending = [{"receipt_line_key": f"factual:{row.get('dataset_id')}:{row.get('source_row', index)}",
        "document_key": f"ref:{row.get('document_ref') or row.get('document_number') or ''}",
        "receipt_number": row.get("document_number"), "receipt_date": row.get("occurred_at"),
        "warehouse": row.get("warehouse"), "sku_key": row.get("sku_key"), "qty_units": row.get("box_quantity"),
        "reason": "terminal_receipt_not_completed"} for index, row in enumerate(result["rows"])
        if completed[id(row)] is not True]
    result["state"] = {"accepted_rows": accepted, "pending_receipt_rows": [], "zero_receipt_rows": [],
        "excluded_receipt_rows": [], "documents": [], "document_keys": sorted({r["document_key"] for r in accepted})}
    result["state"]["pending_receipt_rows"] = pending
    result["classification_inputs"] = [_receipt_overlay(row, index) for index, row in enumerate(result["rows"])]
    return result


def load_inventory_for_day(operational_date: Any, warehouse: Any, **kwargs: Any) -> dict[str, Any]:
    result = _scoped("inventory", operational_date, warehouse, **kwargs)
    result["evidence_rows"] = result.pop("rows")
    result["rows"] = []
    result["diagnostics"] = {"automatic_box_control_available": False,
        "reason": "factual_inventory_has_source_quantities_without_authoritative_box_conversion",
        "evidence_rows": len(result["evidence_rows"])}
    return result


def load_vgh_attributes(**kwargs: Any) -> dict[str, Any]:
    return _scoped("vgh", **kwargs)


def build_factual_weight_classifications(operational_date: Any, warehouse: Any, *,
                                         registry: Mapping[str, Any] | None = None, root: Path = DATA_ROOT,
                                         rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Apply the existing PR #190 VGH workflow and persisted user bands."""
    reg = _registry(registry, root)
    receipts = load_receipts_for_day(operational_date, warehouse, registry=reg, root=root)
    lifecycle = _lifecycle_blockers(reg)
    view = load_effective_rows("vgh", registry=reg, root=root, strict=False)
    relevant_skus = {str(row.get("sku_key") or "") for row in receipts["classification_inputs"]}
    relevant_conflicts = [conflict for conflict in view.get("conflicts", [])
        if str((conflict.get("business_key") or [""])[0]) in relevant_skus]
    blockers = [*receipts["blockers"], *lifecycle, *relevant_conflicts]
    if blockers: return {"source": "factual", "rows": [], "diagnostics": {}, "blockers": blockers, "authoritative": False}
    configured = dict(rules) if rules is not None else load_weight_rules()
    rows, diagnostics = calculate_receipt_zones(receipts["classification_inputs"],
                                                {"weight_bands": configured.get("bands", {})}, view["rows"])
    diagnostics["unrelated_vgh_conflicts"] = len(view.get("conflicts", [])) - len(relevant_conflicts)
    return {"source": "factual_receipts_vgh", "rows": rows, "diagnostics": diagnostics,
            "blockers": [], "authoritative": True}


def build_scenario_weight_classifications(relevant_rows: list[Mapping[str, Any]], *,
                                          registry: Mapping[str, Any] | None = None, root: Path = DATA_ROOT,
                                          rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Classify opening/demand SKU from factual VGH, independently of receipts D."""
    reg = _registry(registry, root); lifecycle = _lifecycle_blockers(reg)
    if lifecycle: return {"source": "factual", "rows": [], "diagnostics": {},
                           "blockers": lifecycle, "authoritative": False}
    unique: dict[str, Mapping[str, Any]] = {}
    for row in relevant_rows:
        sku = str(row.get("sku_key") or "")
        if sku: unique.setdefault(sku, row)
    view = load_effective_rows("vgh", registry=reg, root=root, strict=False)
    relevant_conflicts = [conflict for conflict in view.get("conflicts", [])
        if str((conflict.get("business_key") or [""])[0]) in unique]
    if relevant_conflicts: return {"source": "factual", "rows": [], "diagnostics": {},
                                   "blockers": relevant_conflicts, "authoritative": False}
    inputs = [_receipt_overlay({"sku_key": sku,
        "nomenclature": row.get("nomenclature") or row.get("sku_name") or row.get("item_name"),
        "characteristic": row.get("characteristic") or row.get("characteristic_name")}, index)
        for index, (sku, row) in enumerate(sorted(unique.items()))]
    configured = dict(rules) if rules is not None else load_weight_rules()
    rows, diagnostics = calculate_receipt_zones(inputs, {"weight_bands": configured.get("bands", {})}, view.get("rows", []))
    diagnostics["unrelated_vgh_conflicts"] = len(view.get("conflicts", [])) - len(relevant_conflicts)
    return {"source": "factual_scenario_sku_vgh", "rows": rows, "diagnostics": diagnostics,
            "blockers": [], "authoritative": True}
