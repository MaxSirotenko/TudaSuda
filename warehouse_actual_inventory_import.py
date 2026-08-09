"""Pure import of the checked 1C actual-inventory export.

The importer deliberately does not infer stock or placement: only rows whose
box calculation is authoritative and whose physical cell exists are accepted.
"""

from __future__ import annotations

import hashlib
import copy
import json
import math
import re
from io import BytesIO
from typing import Any

import pandas as pd

from warehouse_inventory_placement import cell_key
from warehouse_business_identity import canonical_sku_key


FIELD_ALIASES = {
    "warehouse": ["Склад", "warehouse"],
    "distribution_center": ["РЦ", "distribution_center"],
    "cell_code": ["КодЯчейки", "Код ячейки", "cell_code"],
    "cell_display": ["Ячейка", "cell"],
    "cell_address": ["АдресЯчейки", "Адрес ячейки", "cell_address"],
    "row_number": ["Ряд", "row_number", "row"],
    "cell_number": ["НомерЯчейки", "Номер ячейки", "cell_number"],
    "tier": ["Ярус", "tier"],
    "pick_order": ["ПорядокСборки", "Порядок сборки", "pick_order"],
    "nomenclature_code": ["КодНоменклатуры", "Код номенклатуры", "nomenclature_code", "sku_code"],
    "nomenclature": ["Номенклатура", "nomenclature", "sku_name"],
    "characteristic_code": ["КодХарактеристики", "Код характеристики", "characteristic_code"],
    "characteristic": ["Характеристика", "characteristic", "characteristic_name"],
    "production_date": ["ДатаПроизводства", "Дата производства", "production_date"],
    "source_pallet_ref": ["Паллета", "source_pallet_ref", "pallet_ref"],
    "source_unit_name": ["ЕдиницаИзмерения", "Единица измерения", "source_unit_name"],
    "source_quantity": ["Количество", "source_quantity"],
    "pallet_count": ["КоличествоПаллет", "Количество паллет", "pallet_count"],
    "quantity_per_box": ["КоличествоВКоробке", "Количество в коробке", "quantity_per_box"],
    "calculated_box_qty": ["РасчетноеКоличествоКоробов", "Расчетное количество коробов", "calculated_box_qty"],
    "calculation_control": ["КонтрольРасчета", "Контроль расчета", "calculation_control"],
}

SOURCE_FIELDS = tuple(FIELD_ALIASES)
REASON_FIELDS = (
    "missing_sku", "missing_cell_address", "unknown_cell",
    "quantity_per_box_missing", "calculation_not_successful",
    "box_quantity_missing", "box_quantity_invalid", "box_quantity_non_positive",
    "duplicate_export_row",
)


def _label(value: Any) -> str:
    return " ".join(str(value).strip().casefold().replace("ё", "е").split())


def _text(value: Any) -> str:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _json_value(value: Any) -> Any:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def get_actual_inventory_sheet_names(file_bytes: bytes) -> list[str]:
    with pd.ExcelFile(BytesIO(file_bytes)) as workbook:
        return list(workbook.sheet_names)


def read_actual_inventory_table(file_bytes: bytes, sheet_name: str, header_rows: int = 1) -> pd.DataFrame:
    header: int | list[int] = 0 if header_rows <= 1 else list(range(header_rows))
    table = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=header)
    if isinstance(table.columns, pd.MultiIndex):
        table.columns = [
            " / ".join(str(part).strip() for part in column
                       if str(part).strip() and not str(part).startswith("Unnamed"))
            for column in table.columns
        ]
    else:
        table.columns = [str(column).strip() for column in table.columns]
    return table.dropna(how="all")


def detect_actual_inventory_columns(table: pd.DataFrame) -> dict[str, str | None]:
    columns = [(str(column), _label(column)) for column in table.columns]
    result: dict[str, str | None] = {}
    for field, aliases in FIELD_ALIASES.items():
        normalized_aliases = [_label(alias) for alias in aliases]
        exact = [original for original, normalized in columns if normalized in normalized_aliases]
        if exact:
            result[field] = exact[0]
            continue
        candidates = {
            original for original, normalized in columns
            if any(alias in normalized for alias in normalized_aliases)
        }
        result[field] = next(iter(candidates)) if len(candidates) == 1 else None
    return result


def _integer(value: Any) -> tuple[int | None, str]:
    if isinstance(value, bool) or not _text(value):
        return None, "box_quantity_missing" if not _text(value) else "box_quantity_invalid"
    try:
        number = float(_text(value).replace(",", "."))
    except ValueError:
        return None, "box_quantity_invalid"
    if not math.isfinite(number) or not number.is_integer():
        return None, "box_quantity_invalid"
    if number <= 0:
        return None, "box_quantity_non_positive"
    return int(number), ""


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not _text(value):
        return None
    try:
        number = float(_text(value).replace(",", "."))
    except ValueError:
        return None
    return number if math.isfinite(number) and number > 0 else None


def _row_number(value: Any) -> str:
    text = _text(value)
    match = re.match(r"^\s*(\d+)\s*-", text)
    return match.group(1) if match else text


def _ordinal(value: Any) -> str:
    text = _label(value)
    if not text:
        return "1"
    words = {"первый": "1", "второй": "2", "третий": "3", "четвертый": "4", "пятый": "5"}
    if text in words:
        return words[text]
    match = re.fullmatch(r"(\d+)(?:\s*-?\s*й)?", text)
    if match:
        return str(int(match.group(1)))
    try:
        number = float(text.replace(",", "."))
        return str(int(number)) if number.is_integer() else text
    except ValueError:
        return text


def _address(value: Any) -> tuple[str, str]:
    match = re.search(r"(\d+)\s*(?:-|/)\s*(\d+)", _text(value))
    return (match.group(1), match.group(2)) if match else ("", "")


def _source_record(row: pd.Series, mapping: dict[str, str | None], source_index: Any) -> dict[str, Any]:
    record = {
        field: _json_value(row[column]) if column is not None else ""
        for field, column in mapping.items()
    }
    record["row_number"] = _row_number(record["row_number"])
    record["cell_number"] = _ordinal(record["cell_number"]) if _text(record["cell_number"]) else ""
    record["tier"] = _ordinal(record["tier"])
    if not record["row_number"] or not record["cell_number"]:
        for field in ("cell_address", "cell_display"):
            parsed_row, parsed_cell = _address(record[field])
            record["row_number"] = record["row_number"] or parsed_row
            record["cell_number"] = record["cell_number"] or parsed_cell
            if record["row_number"] and record["cell_number"]:
                break
    record["cell_key"] = (
        cell_key(record["row_number"], record["cell_number"], record["tier"])
        if record["row_number"] and record["cell_number"] else ""
    )
    if _label(record["production_date"]) == "<пустая дата>":
        record["production_date"] = ""
    record["source_index"] = _json_value(source_index)
    record["reason"] = ""
    return record


def build_actual_inventory_placement_state(
    model: dict[str, Any], table: pd.DataFrame,
    mapping: dict[str, str | None] | None = None,
    *, inventory_results_rows: list[dict[str, Any]] | None = None,
    palletization_rule_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a placement state without changing either input or doing I/O."""
    columns = dict(mapping) if mapping is not None else detect_actual_inventory_columns(table)
    columns = {field: columns.get(field) for field in FIELD_ALIASES}
    missing = [field for field in ("nomenclature", "calculated_box_qty", "quantity_per_box", "calculation_control") if not columns[field]]
    if not ((columns["row_number"] and columns["cell_number"]) or columns["cell_address"] or columns["cell_display"]):
        missing.append("cell_location")

    state = {
        "model_id": model.get("model_id"),
        "source_file_hash": model.get("source_file_hash", ""),
        "placements": [], "excluded_inventory": [], "unmatched_inventory": [],
        "unplaced_inventory": [],
        "settings": {"allow_mixed_sku_in_deep_lane": False}, "journal": [],
    }
    diagnostics: dict[str, Any] = {
        "rows_total": int(len(table)), "accepted_rows": 0, "excluded_rows": 0,
        "unmatched_rows": 0, "processing_rate_percent": 0.0,
        "accepted_boxes": 0, "excluded_boxes": 0, "accepted_sku_count": 0,
        "accepted_cell_count": 0, "missing_required_columns": missing,
        **{reason: 0 for reason in REASON_FIELDS},
        "empty_production_date_rows": 0, "excluded_source_quantity_by_unit": {},
        "excluded_pallet_count": 0.0, "pallet_occupancy_not_derived": 0,
    }
    if missing:
        return state, diagnostics

    cells = {}
    for model_cell in model.get("cells", []) or []:
        key = cell_key(model_cell.get("row_number"), model_cell.get("cell_number"), model_cell.get("tier"))
        cells[key] = model_cell
    seen: set[tuple[Any, ...]] = set()
    accepted_skus: set[str] = set()
    accepted_cells: set[str] = set()

    for source_index, row in table.iterrows():
        record = _source_record(row, columns, source_index)
        if not record["production_date"]:
            diagnostics["empty_production_date_rows"] += 1
        sku_key = canonical_sku_key(record)
        boxes, box_reason = _integer(record["calculated_box_qty"])
        control = _label(record["calculation_control"])
        reason = ""
        if not _text(record["nomenclature"]):
            reason = "missing_sku"
        elif control == "количество в коробке не найдено":
            reason = "quantity_per_box_missing"
        elif control != "расчет выполнен":
            reason = "calculation_not_successful"
        elif _positive_number(record["quantity_per_box"]) is None:
            reason = "quantity_per_box_missing"
        elif box_reason:
            reason = box_reason
        elif not record["cell_key"]:
            reason = "missing_cell_address"

        if not reason and record["cell_key"] not in cells:
            record["reason"] = "unknown_cell"
            state["unmatched_inventory"].append(record)
            diagnostics["unknown_cell"] += 1
            diagnostics["unmatched_rows"] += 1
            continue

        duplicate_key = (
            record["cell_key"], _text(record["source_pallet_ref"]), sku_key, record["production_date"],
            _text(record["source_quantity"]), boxes, _text(record["quantity_per_box"]),
        )
        if not reason and duplicate_key in seen:
            reason = "duplicate_export_row"
        if reason:
            record["reason"] = reason
            state["excluded_inventory"].append(record)
            diagnostics[reason] += 1
            diagnostics["excluded_rows"] += 1
            if boxes is not None:
                diagnostics["excluded_boxes"] += boxes
            unit = _text(record["source_unit_name"])
            quantity = _positive_number(record["source_quantity"])
            if unit and quantity is not None:
                totals = diagnostics["excluded_source_quantity_by_unit"]
                totals[unit] = totals.get(unit, 0.0) + quantity
            pallets = _positive_number(record["pallet_count"])
            if pallets is not None:
                diagnostics["excluded_pallet_count"] += pallets
            continue

        seen.add(duplicate_key)
        model_cell = cells[record["cell_key"]]
        pallet_value = _positive_number(record["pallet_count"])
        placement_seed = json.dumps(
            [model.get("model_id"), record["warehouse"], record["cell_key"],
             _text(record["source_pallet_ref"]), sku_key, record["production_date"], boxes,
             _text(record["source_quantity"])],
            ensure_ascii=False, separators=(",", ":"),
        )
        placement = {
            "placement_id": "actual-" + hashlib.sha256(placement_seed.encode()).hexdigest()[:20],
            "sku_key": sku_key, "sku_code": _text(record["nomenclature_code"]),
            "nomenclature_code": _text(record["nomenclature_code"]),
            "sku_name": _text(record["nomenclature"]), "item_name": _text(record["nomenclature"]),
            "nomenclature": _text(record["nomenclature"]),
            "characteristic_code": _text(record["characteristic_code"]),
            "characteristic_name": _text(record["characteristic"]),
            "characteristic": _text(record["characteristic"]),
            "row_number": record["row_number"], "cell_number": record["cell_number"],
            "tier": record["tier"], "cell_key": record["cell_key"],
            "qty_units": boxes, "unit_name": "короб", "qty_boxes": boxes,
            "qty_pallets": pallet_value or 0.0, "pallet_count": record["pallet_count"],
            "quantity": 0.0, "occupied_capacity_pallets": 0.0,
            "weight_zone": model_cell.get("weight_zone", ""),
            "source": "actual_inventory_1c", "confidence": "exact",
            "placement_mode": "factual", "placement_status": "placed", "unplaced_reason": "",
            "production_date": record["production_date"], "source_quantity": record["source_quantity"],
            "source_unit_name": record["source_unit_name"], "quantity_per_box": record["quantity_per_box"],
            "calculation_control": record["calculation_control"], "warehouse": record["warehouse"],
            "distribution_center": record["distribution_center"], "cell_code": record["cell_code"],
            "cell_display": record["cell_display"], "cell_address": record["cell_address"],
            "pick_order": record["pick_order"], "calculated_box_qty": record["calculated_box_qty"],
            "source_pallet_ref": _text(record["source_pallet_ref"]),
            "reason": "",
            "source_index": record["source_index"], "occupancy_not_authoritative": True,
        }
        state["placements"].append(placement)
        diagnostics["accepted_rows"] += 1
        diagnostics["accepted_boxes"] += boxes
        if pallet_value is not None:
            diagnostics["pallet_occupancy_not_derived"] += 1
        accepted_skus.add(sku_key)
        accepted_cells.add(record["cell_key"])

    diagnostics["accepted_sku_count"] = len(accepted_skus)
    diagnostics["accepted_cell_count"] = len(accepted_cells)
    diagnostics["processing_rate_percent"] = (
        round(100.0 * diagnostics["accepted_rows"] / diagnostics["rows_total"], 2)
        if diagnostics["rows_total"] else 0.0
    )
    _classify_physical_pallet_evidence(model, state, diagnostics)
    rules = {r.get("sku_key"): r for r in (palletization_rule_state or {}).get("rules", []) or []}
    for placement in state["placements"]:
        rule = rules.get(placement["sku_key"])
        if rule and placement.get("physical_pallet_authority") == "exact_normal_pallet":
            placement["capacity_boxes"] = rule.get("boxes_per_pallet")
    _cross_check_inventory_totals(state, diagnostics, inventory_results_rows)
    return state, diagnostics


def _classify_physical_pallet_evidence(
    model: dict[str, Any], state: dict[str, Any], diagnostics: dict[str, Any],
) -> None:
    """Annotate exact pallet footprints; never choose among conflicting facts."""
    placements = state["placements"]
    cells = {cell_key(c.get("row_number"), c.get("cell_number"), c.get("tier")): c
             for c in model.get("cells", []) or []}
    by_pallet: dict[str, list[dict[str, Any]]] = {}
    for row in placements:
        ref = _text(row.get("source_pallet_ref"))
        row["physical_pallet_authority"] = "missing_identity" if not ref else "pending"
        row["occupancy_not_authoritative"] = True
        if ref:
            by_pallet.setdefault(ref, []).append(row)

    counts = {
        "source_rows": len(placements), "accepted_boxes": sum(r["qty_boxes"] for r in placements),
        "unique_source_pallets": len(by_pallet), "exact_normal_pallets": 0,
        "deep_lane_pallets_with_unknown_depth": 0,
        "pallets_missing_identity": sum(not _text(r.get("source_pallet_ref")) for r in placements),
        "multi_sku_source_pallets": 0, "pallet_in_multiple_cell_conflicts": 0,
        "normal_cell_overoccupancy_conflicts": 0, "unknown_model_cells": diagnostics.get("unknown_cell", 0),
        "inventory_total_mismatches": 0, "unresolved_boxes": 0,
    }
    usable_by_cell: dict[str, list[str]] = {}
    for ref, rows in sorted(by_pallet.items()):
        warehouses = {_text(r.get("warehouse")) for r in rows}
        cell_keys = {r["cell_key"] for r in rows}
        skus = {r["sku_key"] for r in rows}
        if len(warehouses) != 1 or len(cell_keys) != 1:
            reason = "pallet_in_multiple_cells" if len(cell_keys) != 1 else "pallet_in_multiple_warehouses"
            counts["pallet_in_multiple_cell_conflicts"] += len(cell_keys) != 1
        elif len(skus) != 1:
            reason = "multi_sku_source_pallet"
            counts["multi_sku_source_pallets"] += 1
        else:
            key = next(iter(cell_keys)); cell = cells[key]
            storage = _text(cell.get("storage_type") or cell.get("row_storage_type") or "normal")
            if storage == "deep_lane" or cell.get("capacity_pallets", 1) > 1:
                reason = "deep_lane_depth_unknown"
                counts["deep_lane_pallets_with_unknown_depth"] += 1
            elif cell.get("capacity_pallets", 1) != 1:
                reason = "unsupported_normal_cell_capacity"
            else:
                reason = "exact_normal_pallet"
                usable_by_cell.setdefault(key, []).append(ref)
        for row in rows:
            row["physical_pallet_authority"] = reason

    conflicts = {key for key, refs in usable_by_cell.items() if len(set(refs)) > 1}
    counts["normal_cell_overoccupancy_conflicts"] = len(conflicts)
    for row in placements:
        if row["cell_key"] in conflicts and row["physical_pallet_authority"] == "exact_normal_pallet":
            row["physical_pallet_authority"] = "normal_cell_overoccupancy"
        elif row["physical_pallet_authority"] == "exact_normal_pallet":
            row["occupancy_not_authoritative"] = False
    counts["exact_normal_pallets"] = len({r["source_pallet_ref"] for r in placements
                                          if r["physical_pallet_authority"] == "exact_normal_pallet"})
    counts["unresolved_boxes"] = sum(r["qty_boxes"] for r in placements
                                      if r["physical_pallet_authority"] != "exact_normal_pallet")
    authoritative_boxes = counts["accepted_boxes"] - counts["unresolved_boxes"]
    counts["physical_pallet_coverage_percent"] = (
        round(100 * authoritative_boxes / counts["accepted_boxes"], 4) if counts["accepted_boxes"] else 100.0)
    counts["physical_pallet_coverage_denominator"] = "accepted_factual_boxes"
    counts.update({
        "stock_quantity_authoritative": diagnostics.get("excluded_rows", 0) == 0,
        "stock_location_authoritative": diagnostics.get("unmatched_rows", 0) == 0,
        "normal_pallet_footprint_authoritative": not any((counts["pallets_missing_identity"], counts["multi_sku_source_pallets"],
            counts["pallet_in_multiple_cell_conflicts"], counts["normal_cell_overoccupancy_conflicts"])),
        "deep_lane_depth_authoritative": counts["deep_lane_pallets_with_unknown_depth"] == 0,
    })
    counts["opening_stock_business_ready"] = (counts["stock_quantity_authoritative"] and
        counts["stock_location_authoritative"] and counts["normal_pallet_footprint_authoritative"] and
        counts["inventory_total_mismatches"] == 0)
    diagnostics.update(counts)
    state["physical_opening_readiness"] = counts


def _cross_check_inventory_totals(
    state: dict[str, Any], diagnostics: dict[str, Any], inventory_rows: list[dict[str, Any]] | None,
) -> None:
    """Compare independent totals without ever changing the physical snapshot."""
    if inventory_rows is None:
        diagnostics["inventory_totals_control_status"] = "not_supplied"
        return
    if not inventory_rows:
        diagnostics["inventory_totals_control_status"] = "supplied_but_no_valid_rows"
        diagnostics["inventory_control_diagnostic"] = "inventory_control_supplied_but_no_valid_rows"
        state["physical_opening_readiness"]["opening_stock_business_ready"] = False
        return
    physical: dict[str, int] = {}
    control: dict[str, int] = {}
    for row in state["placements"]:
        physical[row["sku_key"]] = physical.get(row["sku_key"], 0) + int(row["qty_boxes"])
    for row in inventory_rows:
        sku = canonical_sku_key(row)
        boxes, error = _integer(row.get("qty_units") if "qty_units" in row else row.get("qty_boxes"))
        if sku and not error:
            control[sku] = control.get(sku, 0) + boxes
    mismatches = [{"sku_key": sku, "physical_boxes": physical.get(sku, 0),
                   "inventory_control_boxes": control.get(sku, 0),
                   "delta_boxes": physical.get(sku, 0) - control.get(sku, 0)}
                  for sku in sorted(set(physical) | set(control)) if physical.get(sku, 0) != control.get(sku, 0)]
    diagnostics["inventory_total_mismatch_details"] = mismatches
    diagnostics["inventory_total_mismatches"] = len(mismatches)
    diagnostics["inventory_totals_control_status"] = "mismatch" if mismatches else "agrees"
    readiness = state["physical_opening_readiness"]
    readiness["inventory_total_mismatches"] = len(mismatches)
    if mismatches:
        readiness["opening_stock_business_ready"] = False


def cross_check_physical_opening_stock(
    actual_placement_state: dict[str, Any], inventory_results_rows: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an unchanged-quantity snapshot plus independent total controls."""
    state = copy.deepcopy(actual_placement_state)
    diagnostics: dict[str, Any] = {}
    state.setdefault("physical_opening_readiness", {
        "stock_quantity_authoritative": True, "stock_location_authoritative": True,
        "normal_pallet_footprint_authoritative": False, "deep_lane_depth_authoritative": False,
        "opening_stock_business_ready": False, "inventory_total_mismatches": 0,
    })
    _cross_check_inventory_totals(state, diagnostics, inventory_results_rows)
    diagnostics["allocation_contract"] = "exact_pallet_cell_snapshot"
    diagnostics["legacy_redistribution_used"] = False
    return state, diagnostics
