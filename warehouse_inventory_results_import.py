"""Pure parser and explicit selector for 1C inventory result exports."""

from __future__ import annotations

import hashlib
import math
import re
from io import BytesIO
from typing import Any

import pandas as pd

from warehouse_business_identity import canonical_sku_key

CANONICAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "inventory_ref": ("СсылкаИнвентаризации", "inventory_ref"),
    "inventory_number": ("НомерИнвентаризации", "inventory_number"),
    "inventory_date": ("ДатаИнвентаризации", "inventory_date"),
    "warehouse": ("Склад", "warehouse"),
    "distribution_center": ("РЦ", "distribution_center", "dc"),
    "line_number": ("НомерСтроки", "line_number"),
    "nomenclature_code": ("КодНоменклатуры", "nomenclature_code"),
    "nomenclature": ("Номенклатура", "nomenclature"),
    "characteristic_code": ("КодХарактеристики", "characteristic_code"),
    "characteristic": ("Характеристика", "characteristic"),
    "source_unit_name": ("ЕдиницаИзмерения", "source_unit_name"),
    "actual_quantity": ("ФактическоеКоличество", "actual_quantity"),
    "accounting_quantity": ("УчетноеКоличество", "accounting_quantity"),
    "difference": ("Расхождение", "difference"),
    "quantity_per_box": ("КоличествоВКоробке", "quantity_per_box"),
    "calculated_box_qty": ("РасчетноеКоличествоКоробов", "calculated_box_qty"),
    "calculation_control": ("КонтрольРасчета", "calculation_control"),
    "sku_key": ("sku_key",),
}
REQUIRED = (
    "inventory_number", "inventory_date", "warehouse", "line_number", "nomenclature",
    "characteristic", "calculated_box_qty", "quantity_per_box", "calculation_control",
)
SUCCESS = "расчет выполнен"
NO_BOX = "количество в коробке не найдено"
NON_POSITIVE = "фактический остаток не положительный"


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return " ".join(str(value).split())


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value).casefold().replace("ё", "е"))


def _warehouse_key(value: Any) -> str:
    return " ".join(_text(value).casefold().replace("ё", "е").split())


def _json_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else (float(value) if math.isfinite(value) else "")
    return _text(value)


def _positive_number(value: Any) -> bool:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return False
    try:
        n = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return False
    return math.isfinite(n) and n > 0


def _integer_boxes(value: Any) -> tuple[int | None, str | None]:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None, "box_quantity_missing"
    try:
        n = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None, "box_quantity_invalid"
    if not math.isfinite(n) or not n.is_integer():
        return None, "box_quantity_invalid"
    if n < 0:
        return None, "box_quantity_non_positive"
    return int(n), None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        n = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def get_inventory_results_sheet_names(file_bytes: bytes) -> list[str]:
    with pd.ExcelFile(BytesIO(file_bytes)) as workbook:
        return list(workbook.sheet_names)


def read_inventory_results_table(file_bytes: bytes, sheet_name: str, header_rows: int = 1) -> pd.DataFrame:
    header = list(range(header_rows)) if header_rows > 1 else 0
    table = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=header)
    if isinstance(table.columns, pd.MultiIndex):
        table.columns = [" ".join(_text(part) for part in col if _text(part) and not _text(part).startswith("Unnamed:")) for col in table.columns]
    return table


def detect_inventory_results_columns(table: pd.DataFrame) -> dict[str, str | None]:
    columns = [str(c) for c in table.columns]
    normalized = {_norm(c): c for c in columns}
    result: dict[str, str | None] = {}
    for key, aliases in CANONICAL_COLUMNS.items():
        found = next((normalized[_norm(a)] for a in aliases if _norm(a) in normalized), None)
        if found is None:
            candidates = {c for c in columns for a in aliases if _norm(a) and _norm(a) in _norm(c)}
            found = next(iter(candidates)) if len(candidates) == 1 else None
        result[key] = found
    return result


def _empty_state() -> dict[str, Any]:
    return {"documents": [], "document_keys": [], "accepted_rows": [], "zero_inventory_rows": [], "excluded_inventory_rows": [], "unassigned_rows": []}


def _diag(rows_total: int) -> dict[str, Any]:
    keys = "documents_total accepted_rows accepted_boxes zero_inventory_rows excluded_inventory_rows unique_sku_count duplicate_sku_rows_within_document missing_document_key missing_warehouse missing_sku quantity_per_box_missing calculation_control_missing calculation_not_successful box_quantity_missing box_quantity_invalid box_quantity_non_positive document_without_variances_count".split()
    d = {k: 0 for k in keys}
    d.update({"rows_total": rows_total, "missing_required_columns": [], "processing_rate_percent": 0.0})
    return d


def _cell(row: pd.Series, mapping: dict[str, str | None], key: str) -> Any:
    col = mapping.get(key)
    return row[col] if col in row.index else ""


def _document_key(row: pd.Series, mapping: dict[str, str | None]) -> str:
    ref = _text(_cell(row, mapping, "inventory_ref"))
    if ref:
        return "ref:" + _norm(ref)
    parts = [_text(_cell(row, mapping, k)) for k in ("inventory_number", "inventory_date", "warehouse")]
    if not all(parts):
        return ""
    return "sha256:" + hashlib.sha256("\u241f".join(parts).encode("utf-8")).hexdigest()


def _base_row(row: pd.Series, mapping: dict[str, str | None], source_index: Any) -> dict[str, Any]:
    nomenclature = _text(_cell(row, mapping, "nomenclature"))
    characteristic = _text(_cell(row, mapping, "characteristic"))
    sku_key = canonical_sku_key({"sku_key": _cell(row, mapping, "sku_key"),
                                 "nomenclature": nomenclature, "characteristic": characteristic,
                                 "nomenclature_code": _cell(row, mapping, "nomenclature_code"),
                                 "characteristic_code": _cell(row, mapping, "characteristic_code")})
    return {
        "document_key": _document_key(row, mapping), "inventory_ref": _text(_cell(row, mapping, "inventory_ref")),
        "inventory_number": _text(_cell(row, mapping, "inventory_number")), "inventory_date": _json_value(_cell(row, mapping, "inventory_date")),
        "warehouse": _text(_cell(row, mapping, "warehouse")), "distribution_center": _text(_cell(row, mapping, "distribution_center")),
        "line_number": _json_value(_cell(row, mapping, "line_number")), "sku_key": sku_key,
        "nomenclature_code": _text(_cell(row, mapping, "nomenclature_code")), "nomenclature": nomenclature,
        "characteristic_code": _text(_cell(row, mapping, "characteristic_code")), "characteristic": characteristic,
        "actual_quantity": _json_value(_cell(row, mapping, "actual_quantity")), "accounting_quantity": _json_value(_cell(row, mapping, "accounting_quantity")),
        "difference": _json_value(_cell(row, mapping, "difference")), "source_unit_name": _text(_cell(row, mapping, "source_unit_name")),
        "quantity_per_box": _json_value(_cell(row, mapping, "quantity_per_box")), "calculated_box_qty": _json_value(_cell(row, mapping, "calculated_box_qty")),
        "calculation_control": _text(_cell(row, mapping, "calculation_control")), "source_index": _json_value(source_index),
    }


def build_inventory_results_import(table: pd.DataFrame, mapping: dict[str, str | None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    rows_total = int(len(table)) if table is not None else 0
    diagnostics = _diag(rows_total)
    state = _empty_state()
    if table is None or table.empty:
        return state, diagnostics
    mapping = dict(mapping or detect_inventory_results_columns(table))
    missing = [k for k in REQUIRED if not mapping.get(k)]
    diagnostics["missing_required_columns"] = missing
    if missing:
        return state, diagnostics
    for source_index, row in table.iterrows():
        base = _base_row(row, mapping, source_index)
        boxes, box_reason = _integer_boxes(_cell(row, mapping, "calculated_box_qty"))
        control = _norm(base["calculation_control"])
        reason = None
        if not base["document_key"]: reason = "missing_document_key"; diagnostics[reason] += 1
        elif not base["warehouse"]: reason = "missing_warehouse"; diagnostics[reason] += 1
        elif not base["sku_key"]: reason = "missing_sku"; diagnostics[reason] += 1
        elif control == _norm(NO_BOX) or not _positive_number(_cell(row, mapping, "quantity_per_box")): reason = "quantity_per_box_missing"; diagnostics[reason] += 1
        elif control == _norm(NON_POSITIVE) or boxes == 0:
            state["zero_inventory_rows"].append({**base, "qty_units": 0 if boxes is None else boxes, "unit_name": "короб", "reason": "confirmed_non_positive_inventory"}); diagnostics["zero_inventory_rows"] += 1; continue
        elif not control: reason = "calculation_control_missing"; diagnostics[reason] += 1
        elif control != _norm(SUCCESS): reason = "calculation_not_successful"; diagnostics[reason] += 1
        elif box_reason: reason = box_reason; diagnostics[reason] += 1
        if reason:
            state["excluded_inventory_rows"].append({**base, "reason": reason}); diagnostics["excluded_inventory_rows"] += 1; continue
        accepted = {**base, "qty_units": int(boxes), "unit_name": "короб"}
        state["accepted_rows"].append(accepted)
        diagnostics["accepted_rows"] += 1; diagnostics["accepted_boxes"] += int(boxes)
    all_rows = state["accepted_rows"] + state["zero_inventory_rows"] + state["excluded_inventory_rows"]
    diagnostics["unique_sku_count"] = len({r["sku_key"] for r in state["accepted_rows"] if r.get("sku_key")})
    seen: set[tuple[str, str]] = set(); dup = 0
    for r in state["accepted_rows"]:
        key = (r["document_key"], r["sku_key"])
        dup += 1 if key in seen else 0; seen.add(key)
    diagnostics["duplicate_sku_rows_within_document"] = dup
    docs = {}
    for r in all_rows:
        docs.setdefault(r["document_key"], []).append(r)
    for key, rows in docs.items():
        accepted = [r for r in rows if "qty_units" in r and r not in state["zero_inventory_rows"] and not r.get("reason")]
        zero = [r for r in rows if r.get("reason") == "confirmed_non_positive_inventory"]
        ex = [r for r in rows if r.get("reason") and r.get("reason") != "confirmed_non_positive_inventory"]
        relevant = accepted + zero
        no_var = bool(relevant) and all((_num(r.get("actual_quantity")) == _num(r.get("accounting_quantity")) and (_num(r.get("difference")) or 0) == 0) for r in relevant)
        warnings = ["document_without_variances"] if no_var else []
        diagnostics["document_without_variances_count"] += 1 if no_var else 0
        first = rows[0]
        state["documents"].append({"document_key": key, "inventory_ref": first["inventory_ref"], "inventory_number": first["inventory_number"], "inventory_date": first["inventory_date"], "warehouse": first["warehouse"], "distribution_center": first["distribution_center"], "row_count": len(rows), "accepted_row_count": len(accepted), "zero_row_count": len(zero), "excluded_row_count": len(ex), "unique_sku_count": len({r.get("sku_key") for r in accepted if r.get("sku_key")}), "accepted_boxes": sum(r["qty_units"] for r in accepted), "rows_with_difference": sum(1 for r in rows if (_num(r.get("difference")) or 0) != 0), "actual_equals_accounting_row_count": sum(1 for r in rows if _num(r.get("actual_quantity")) == _num(r.get("accounting_quantity"))), "warnings": warnings})
    state["documents"].sort(key=lambda d: (d["inventory_date"], d["inventory_number"], d["warehouse"], d["document_key"]))
    state["document_keys"] = [d["document_key"] for d in state["documents"]]
    diagnostics["documents_total"] = len(state["documents"])
    diagnostics["processing_rate_percent"] = round(diagnostics["accepted_rows"] / rows_total * 100, 4) if rows_total else 0.0
    return state, diagnostics


def select_inventory_rows_for_opening_stock(import_state: dict[str, Any], selected_document_keys: list[str], included_warehouses: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    docs = {d["document_key"]: d for d in import_state.get("documents", [])}
    found = [k for k in selected_document_keys if k in docs]
    included = {_warehouse_key(w) for w in included_warehouses if _warehouse_key(w)}
    diagnostics = {"requested_document_count": len(selected_document_keys), "found_document_count": len(found), "missing_document_count": len(selected_document_keys) - len(found), "selected_document_count": 0, "deferred_document_count": 0, "rejected_document_count": 0, "included_warehouse_count": len(included), "selected_inventory_rows": 0, "selected_inventory_boxes": 0, "selected_unique_sku_count": 0, "deferred_inventory_rows": 0, "deferred_inventory_boxes": 0, "zero_inventory_rows": 0, "excluded_inventory_rows": 0, "multiple_selected_documents_for_warehouse": 0, "no_documents_selected": 0, "selected_processing_rate_percent": 0.0}
    result = {"inventory_rows": [], "selected_documents": [], "deferred_documents": [], "rejected_documents": [], "deferred_inventory_rows": [], "zero_inventory_rows": [], "excluded_inventory_rows": []}
    if not selected_document_keys:
        diagnostics["no_documents_selected"] = 1; return result, diagnostics
    by_wh: dict[str, list[str]] = {}
    for k in found: by_wh.setdefault(_warehouse_key(docs[k].get("warehouse")), []).append(k)
    rejected = {k for keys in by_wh.values() if len(keys) > 1 for k in keys}
    for k in sorted(rejected):
        result["rejected_documents"].append({**docs[k], "reason": "multiple_selected_documents_for_warehouse"})
    diagnostics["rejected_document_count"] = len(result["rejected_documents"]); diagnostics["multiple_selected_documents_for_warehouse"] = len(result["rejected_documents"])
    selected_set, deferred_set = set(), set()
    for k in found:
        if k in rejected: continue
        if _warehouse_key(docs[k].get("warehouse")) in included:
            result["selected_documents"].append(docs[k]); selected_set.add(k)
        else:
            result["deferred_documents"].append({**docs[k], "reason": "warehouse_outside_target_scope"}); deferred_set.add(k)
    for r in import_state.get("accepted_rows", []):
        if r.get("document_key") in selected_set: result["inventory_rows"].append(dict(r))
        elif r.get("document_key") in deferred_set: result["deferred_inventory_rows"].append({**r, "reason": "warehouse_outside_target_scope"})
    for name in ("zero_inventory_rows", "excluded_inventory_rows"):
        for r in import_state.get(name, []):
            if r.get("document_key") in selected_set: result[name].append(dict(r))
            elif r.get("document_key") in deferred_set: result["deferred_inventory_rows"].append({**r, "reason": "warehouse_outside_target_scope"})
    diagnostics.update({"selected_document_count": len(result["selected_documents"]), "deferred_document_count": len(result["deferred_documents"]), "selected_inventory_rows": len(result["inventory_rows"]), "selected_inventory_boxes": sum(r["qty_units"] for r in result["inventory_rows"]), "selected_unique_sku_count": len({r["sku_key"] for r in result["inventory_rows"]}), "deferred_inventory_rows": len(result["deferred_inventory_rows"]), "deferred_inventory_boxes": sum(r.get("qty_units", 0) for r in result["deferred_inventory_rows"]), "zero_inventory_rows": len(result["zero_inventory_rows"]), "excluded_inventory_rows": len(result["excluded_inventory_rows"])})
    diagnostics["selected_processing_rate_percent"] = round(diagnostics["selected_inventory_rows"] / len(import_state.get("accepted_rows", []) or []) * 100, 4) if import_state.get("accepted_rows") else 0.0
    return result, diagnostics
