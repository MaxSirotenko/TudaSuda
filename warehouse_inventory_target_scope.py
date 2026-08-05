"""Diagnostic inventory-vs-placement target row scope analysis."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from warehouse_outbound_orders import make_outbound_sku_key

ACCEPTED = "accepted_location_evidence"
NON_QUANTIFIED = "non_quantified_location_evidence"
UNKNOWN = "unknown_model_cell_evidence"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm_warehouse(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).casefold().replace("ё", "е")


def _norm_row(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        dec = Decimal(raw.replace(",", "."))
    except (InvalidOperation, ValueError):
        return raw
    if dec == dec.to_integral_value():
        return str(int(dec))
    return raw


def _sku_key(row: dict[str, Any]) -> str:
    ready = _text(row.get("sku_key"))
    if ready:
        return ready
    nomenclature = row.get("nomenclature") or row.get("sku_name") or row.get("item_name")
    characteristic = row.get("characteristic") or row.get("characteristic_name")
    return make_outbound_sku_key(nomenclature, characteristic)


def _qty_units(value: Any) -> tuple[int | None, str | None]:
    if value in (None, "") or isinstance(value, bool):
        return None, "inventory_quantity_missing"
    try:
        dec = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None, "inventory_quantity_invalid"
    if dec != dec.to_integral_value():
        return None, "inventory_quantity_invalid"
    qty = int(dec)
    if qty <= 0:
        return None, "inventory_quantity_non_positive"
    return qty, None


def _sort_value(value: Any) -> tuple[int, Any]:
    txt = _text(value)
    try:
        dec = Decimal(txt.replace(",", "."))
    except (InvalidOperation, ValueError):
        return (1, txt)
    return (0, dec)


def _cell_sort_key(cell_key: str, cells_by_key: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    cell = cells_by_key.get(cell_key, {})
    return (_sort_value(cell.get("row_order")), _sort_value(cell.get("row_number")), _sort_value(cell.get("cell_number")), _sort_value(cell.get("tier")), cell_key)


def _sorted_cells(keys: set[str], cells_by_key: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(keys, key=lambda key: _cell_sort_key(key, cells_by_key))


def _sorted_text(values: set[Any]) -> list[str]:
    return sorted({_text(v) for v in values if _text(v)})


def _invalid_inventory(row: dict[str, Any], index: int, reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "document_key": row.get("document_key"),
        "inventory_number": row.get("inventory_number"),
        "source_index": row.get("source_index", index),
        "warehouse": row.get("warehouse"),
        "nomenclature": row.get("nomenclature") or row.get("sku_name") or row.get("item_name"),
        "characteristic": row.get("characteristic") or row.get("characteristic_name"),
    }


def _build_evidence(model: dict[str, Any], state: dict[str, Any], target_set: set[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], dict[tuple[str, str], list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    cells_by_key = {_text(c.get("cell_key")) or f"{c.get('row_number')}|{c.get('cell_number')}|{c.get('tier') or '1'}": c for c in model.get("cells", []) if isinstance(c, dict)}
    buckets = {"in_target_rows": [], "outside_target_rows": [], "unknown_model_cells": [], "unusable_records": []}
    diag = {"placement_records_total": 0, "accepted_location_records": 0, "non_quantified_location_records": 0, "unmatched_location_records": 0, "unusable_location_records": 0, "placement_warehouse_missing": 0, "placement_sku_missing": 0, "placement_cell_key_missing": 0}
    dedup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    sources = [("placements", ACCEPTED), ("excluded_inventory", NON_QUANTIFIED), ("unmatched_inventory", UNKNOWN)]
    for name, quality in sources:
        for source_index, row in enumerate(state.get(name, []) or []):
            if not isinstance(row, dict):
                continue
            diag["placement_records_total"] += 1
            diag[{ACCEPTED: "accepted_location_records", NON_QUANTIFIED: "non_quantified_location_records", UNKNOWN: "unmatched_location_records"}[quality]] += 1
            wh = row.get("warehouse")
            norm_wh = _norm_warehouse(wh)
            sku = _sku_key(row)
            cell_key = _text(row.get("cell_key"))
            reason = ""
            if not norm_wh:
                reason = "placement_warehouse_missing"; diag[reason] += 1
            elif not sku:
                reason = "placement_sku_missing"; diag[reason] += 1
            elif not cell_key:
                reason = "placement_cell_key_missing"; diag[reason] += 1
            if reason:
                diag["unusable_location_records"] += 1
                buckets["unusable_records"].append({"source": name, "source_index": source_index, "reason": reason})
                continue
            key = (norm_wh, sku, cell_key, quality)
            rec = dedup.setdefault(key, {"warehouse": _text(wh), "normalized_warehouse": norm_wh, "sku_key": sku, "cell_key": cell_key, "evidence_quality": quality, "production_dates": set(), "reported_location_qty_units": 0, "reported_quantity_is_authoritative": False})
            if row.get("production_date") or row.get("production_dates"):
                dates = row.get("production_dates") if isinstance(row.get("production_dates"), list) else [row.get("production_date")]
                rec["production_dates"].update(_text(d) for d in dates if _text(d))
            q, _ = _qty_units(row.get("qty_units"))
            if quality == ACCEPTED and q:
                rec["reported_location_qty_units"] += q
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_sku: dict[str, list[dict[str, Any]]] = {}
    for rec in dedup.values():
        rec["production_dates"] = sorted(rec["production_dates"])
        cell = cells_by_key.get(rec["cell_key"])
        row_num = _norm_row(cell.get("row_number")) if cell else ""
        rec["model_row_number"] = row_num
        bucket = "unknown_model_cells" if not cell else ("in_target_rows" if row_num in target_set else "outside_target_rows")
        buckets[bucket].append(rec)
        by_pair.setdefault((rec["normalized_warehouse"], rec["sku_key"]), []).append(rec)
        by_sku.setdefault(rec["sku_key"], []).append(rec)
    for name in ("in_target_rows", "outside_target_rows", "unknown_model_cells"):
        buckets[name].sort(key=lambda r: (r["warehouse"], r["sku_key"], _cell_sort_key(r["cell_key"], cells_by_key), r["evidence_quality"]))
    return buckets, diag, by_pair, by_sku, cells_by_key


def analyze_inventory_rows_target_scope(model: dict[str, Any], inventory_rows: list[dict[str, Any]], actual_placement_state: dict[str, Any], target_rows: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    norm_targets = sorted({_norm_row(r) for r in target_rows if _norm_row(r)}, key=_sort_value)
    target_set = set(norm_targets)
    model_rows = {_norm_row(c.get("row_number")) for c in model.get("cells", []) if isinstance(c, dict) and _norm_row(c.get("row_number"))}
    evidence, ediag, by_pair, by_sku, cells_by_key = _build_evidence(model, actual_placement_state, target_set)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    invalid = []
    for i, row in enumerate(inventory_rows or []):
        qty, reason = _qty_units(row.get("qty_units"))
        norm_wh = _norm_warehouse(row.get("warehouse"))
        sku = _sku_key(row)
        if reason or not norm_wh or not sku:
            invalid.append(_invalid_inventory(row, i, reason or ("inventory_warehouse_missing" if not norm_wh else "inventory_sku_missing")))
            continue
        key = (norm_wh, sku)
        g = groups.setdefault(key, {"warehouse": _text(row.get("warehouse")), "normalized_warehouse": norm_wh, "sku_key": sku, "nomenclature_code": row.get("nomenclature_code"), "nomenclature": row.get("nomenclature") or row.get("sku_name") or row.get("item_name"), "characteristic_code": row.get("characteristic_code"), "characteristic": row.get("characteristic") or row.get("characteristic_name"), "qty_units": 0, "unit_name": "короб", "document_keys": set(), "inventory_numbers": set(), "source_indexes": set()})
        g["qty_units"] += qty or 0
        g["document_keys"].add(row.get("document_key")); g["inventory_numbers"].add(row.get("inventory_number")); g["source_indexes"].add(row.get("source_index", i))
    records = []
    status_boxes: dict[str, int] = {}
    for key, g in groups.items():
        evs = by_pair.get(key, [])
        in_keys = {e["cell_key"] for e in evs if e in evidence["in_target_rows"]}
        out_keys = {e["cell_key"] for e in evs if e in evidence["outside_target_rows"]}
        unk_keys = {e["cell_key"] for e in evs if e in evidence["unknown_model_cells"]}
        if not target_set: status = "target_scope_not_configured"
        elif in_keys and out_keys: status = "mixed_target_and_outside_rows"
        elif in_keys: status = "confirmed_in_target_rows"
        elif out_keys: status = "confirmed_outside_target_rows"
        elif unk_keys: status = "unknown_model_cell_evidence"
        elif by_sku.get(g["sku_key"]): status = "location_evidence_in_other_warehouse_only"
        else: status = "no_location_evidence"
        status_boxes[status] = status_boxes.get(status, 0) + g["qty_units"]
        qualities = {e["evidence_quality"] for e in evs}
        rec = {**g, "document_keys": _sorted_text(g["document_keys"]), "inventory_numbers": _sorted_text(g["inventory_numbers"]), "source_indexes": sorted(g["source_indexes"]), "status": status, "in_target_cell_keys": _sorted_cells(in_keys, cells_by_key), "outside_target_cell_keys": _sorted_cells(out_keys, cells_by_key), "unknown_cell_keys": _sorted_cells(unk_keys, cells_by_key), "evidence_warehouses": _sorted_text({e["warehouse"] for e in evs}), "evidence_qualities": sorted(qualities), "has_unknown_model_cell_evidence": bool(unk_keys), "reported_in_target_qty_units": sum(e.get("reported_location_qty_units", 0) for e in evs if e["cell_key"] in in_keys), "reported_outside_qty_units": sum(e.get("reported_location_qty_units", 0) for e in evs if e["cell_key"] in out_keys), "reported_unknown_qty_units": sum(e.get("reported_location_qty_units", 0) for e in evs if e["cell_key"] in unk_keys), "reported_quantity_is_authoritative": False}
        records.append(rec)
    records.sort(key=lambda r: (r["warehouse"], r.get("nomenclature") or "", r.get("characteristic") or "", r["sku_key"]))
    boxes_total = sum(r["qty_units"] for r in records)
    def rate(n: int) -> float: return round((n / boxes_total * 100), 4) if boxes_total else 0.0
    diag = {"inventory_rows_total": len(inventory_rows or []), "inventory_groups_total": len(records), "inventory_boxes_total": boxes_total, "invalid_inventory_rows": len(invalid), "target_rows_requested": len(norm_targets), "target_rows_present_in_model": len([r for r in norm_targets if r in model_rows]), "target_rows_missing_in_model": len([r for r in norm_targets if r not in model_rows]), "no_target_rows_configured": 0 if target_set else 1, **ediag, "deduplicated_location_records": sum(len(evidence[k]) for k in ("in_target_rows", "outside_target_rows", "unknown_model_cells")), "in_target_location_records": len(evidence["in_target_rows"]), "outside_target_location_records": len(evidence["outside_target_rows"]), "unknown_model_cell_records": len(evidence["unknown_model_cells"])}
    for st in ["confirmed_in_target_rows", "confirmed_outside_target_rows", "mixed_target_and_outside_rows", "unknown_model_cell_evidence", "location_evidence_in_other_warehouse_only", "no_location_evidence", "target_scope_not_configured"]:
        diag[f"{st}_groups"] = sum(1 for r in records if r["status"] == st)
        diag[f"{st}_boxes"] = status_boxes.get(st, 0)
    diag["confirmed_in_target_rows_rate_percent"] = rate(diag["confirmed_in_target_rows_boxes"])
    diag["confirmed_outside_target_rows_rate_percent"] = rate(diag["confirmed_outside_target_rows_boxes"])
    diag["mixed_scope_rate_percent"] = rate(diag["mixed_target_and_outside_rows_boxes"])
    diag["unknown_location_rate_percent"] = rate(diag["unknown_model_cell_evidence_boxes"] + diag["location_evidence_in_other_warehouse_only_boxes"])
    diag["no_location_evidence_rate_percent"] = rate(diag["no_location_evidence_boxes"])
    return {"model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"), "target_rows": norm_targets, "target_rows_present_in_model": [r for r in norm_targets if r in model_rows], "target_rows_missing_in_model": [r for r in norm_targets if r not in model_rows], "inventory_scope_records": records, "invalid_inventory_rows": invalid, "placement_evidence": evidence}, diag


def analyze_inventory_selection_target_scope(model: dict[str, Any], selection_state: dict[str, Any], actual_placement_state: dict[str, Any], target_rows: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    selected, sdiag = analyze_inventory_rows_target_scope(model, selection_state.get("inventory_rows", []), actual_placement_state, target_rows)
    deferred, ddiag = analyze_inventory_rows_target_scope(model, selection_state.get("deferred_inventory_rows", []), actual_placement_state, target_rows)
    state = {"model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"), "target_rows": selected["target_rows"], "selected_inventory_scope": selected, "deferred_inventory_scope": deferred}
    diag = {"selected_inventory_diagnostics": sdiag, "deferred_inventory_diagnostics": ddiag, "selected_inventory_boxes_total": sdiag["inventory_boxes_total"], "deferred_inventory_boxes_total": ddiag["inventory_boxes_total"], "selected_confirmed_in_target_boxes": sdiag["confirmed_in_target_rows_boxes"], "deferred_confirmed_in_target_boxes": ddiag["confirmed_in_target_rows_boxes"], "deferred_confirmed_outside_target_boxes": ddiag["confirmed_outside_target_rows_boxes"], "deferred_mixed_scope_boxes": ddiag["mixed_target_and_outside_rows_boxes"], "deferred_unknown_location_boxes": ddiag["unknown_model_cell_evidence_boxes"] + ddiag["location_evidence_in_other_warehouse_only_boxes"], "deferred_no_location_evidence_boxes": ddiag["no_location_evidence_boxes"]}
    return state, diag
