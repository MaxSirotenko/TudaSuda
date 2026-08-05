from __future__ import annotations

import hashlib
import math
import re
from io import BytesIO
from typing import Any

import pandas as pd

from warehouse_outbound_orders import make_outbound_sku_key

FIELDS = {
    "receipt_ref": "СсылкаПриходногоОрдера",
    "receipt_number": "НомерПриходногоОрдера",
    "receipt_date": "ДатаПриходногоОрдера",
    "warehouse": "Склад",
    "distribution_center": "РЦ",
    "line_number": "НомерСтроки",
    "nomenclature_code": "КодНоменклатуры",
    "nomenclature": "Номенклатура",
    "characteristic_code": "КодХарактеристики",
    "characteristic": "Характеристика",
    "source_box_quantity": "КоличествоКоробок",
    "reported_pallets": "КоличествоПаллет",
    "terminal_receipt_completed": "ПриемкаТерминаломЗакончена",
    "expected_receipt": "ОжидаемыйПриход",
    "quantity_control": "КонтрольКоличества",
    "sku_key": "sku_key",
}
REQUIRED_FIELDS = ["receipt_number", "receipt_date", "warehouse", "line_number", "nomenclature", "characteristic", "source_box_quantity", "terminal_receipt_completed", "quantity_control"]
ALIASES = {
    "receipt_ref": ["receipt_ref"], "receipt_number": ["receipt_number"], "receipt_date": ["receipt_date"],
    "warehouse": ["warehouse"], "distribution_center": ["distribution_center", "dc"], "line_number": ["line_number"],
    "nomenclature_code": ["nomenclature_code", "sku_code"], "nomenclature": ["nomenclature", "sku_name"],
    "characteristic_code": ["characteristic_code"], "characteristic": ["characteristic", "characteristic_name"],
    "source_box_quantity": ["box_quantity", "source_box_quantity"], "reported_pallets": ["reported_pallets", "pallet_quantity"],
    "terminal_receipt_completed": ["terminal_receipt_completed"], "expected_receipt": ["expected_receipt"],
    "quantity_control": ["quantity_control"], "sku_key": ["sku_key"],
}
DIRECT_CONTROL = "прямое количество коробок"
NON_POSITIVE_CONTROL = "количество коробок не положительное"
NEGATIVE_CONTROL = "количество коробок отрицательное"
MISSING_CONTROL = "количество коробок отсутствует"


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value))


def _json_value(value: Any) -> Any:
    if _is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def _text(value: Any) -> str:
    value = _json_value(value)
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return re.sub(r"\s+", " ", str(value).strip())


def _norm(value: Any) -> str:
    return _text(value).casefold().replace("ё", "е")


def _compact(value: Any) -> str:
    return re.sub(r"[\s_\-/]+", "", _norm(value))


def _hash(parts: list[Any]) -> str:
    payload = "\x1f".join(_text(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_day_receipts_sheet_names(file_bytes: bytes) -> list[str]:
    with pd.ExcelFile(BytesIO(file_bytes)) as workbook:
        return list(workbook.sheet_names)


def read_day_receipts_table(file_bytes: bytes, sheet_name: str, header_row: int | None = None) -> pd.DataFrame:
    if header_row is not None:
        return pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=header_row)
    preview = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=None, nrows=20)
    required = {_compact(x) for x in ["НомерПриходногоОрдера", "Номенклатура", "КоличествоКоробок", "КонтрольКоличества"]}
    for idx, row in preview.iterrows():
        cells = {_compact(value) for value in row.tolist() if _text(value)}
        if required.issubset(cells):
            return pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=int(idx))
    return pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=0)


def _find_column(columns: list[str], canonical: str, aliases: list[str]) -> str | None:
    normalized = {_compact(column): column for column in columns}
    exact = _compact(canonical)
    if exact in normalized:
        return normalized[exact]
    for alias in aliases:
        key = _compact(alias)
        if key in normalized:
            return normalized[key]
    needles = [_compact(canonical), *[_compact(alias) for alias in aliases]]
    candidates = {original for key, original in normalized.items() if any(needle and needle in key for needle in needles)}
    return next(iter(candidates)) if len(candidates) == 1 else None


def detect_day_receipts_columns(table: pd.DataFrame) -> dict[str, str | None]:
    columns = [str(column).strip() for column in table.columns]
    return {field: _find_column(columns, canonical, ALIASES.get(field, [])) for field, canonical in FIELDS.items()}


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
    text = _norm(value)
    if not text:
        return None
    if text in {"1", "да", "истина", "true", "yes"}:
        return True
    if text in {"0", "нет", "ложь", "false", "no"}:
        return False
    return None


def _box_qty(value: Any) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or _is_missing(value) or _text(value) == "":
        return None, "box_quantity_missing"
    try:
        number = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None, "box_quantity_invalid"
    if not math.isfinite(number):
        return None, "box_quantity_invalid"
    if number < 0:
        return None, "box_quantity_negative"
    if number == 0:
        return None, "box_quantity_non_positive"
    if not number.is_integer():
        return None, "box_quantity_invalid"
    return int(number), None


def _empty_state(diag: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return ({"documents": [], "document_keys": [], "accepted_rows": [], "pending_receipt_rows": [], "zero_receipt_rows": [], "excluded_receipt_rows": [], "unassigned_rows": []}, diag)


def _base_diag(rows_total: int) -> dict[str, Any]:
    keys = ["documents_total","accepted_rows","accepted_boxes","accepted_unique_sku_count","pending_receipt_rows","pending_receipt_boxes","zero_receipt_rows","excluded_receipt_rows","terminal_completed_rows","terminal_not_completed_rows","terminal_completion_unknown_rows","expected_receipt_true_rows","expected_receipt_false_rows","expected_receipt_unknown_rows","missing_document_key","missing_warehouse","missing_sku","receipt_quantity_missing","negative_receipt_quantity","box_quantity_missing","box_quantity_invalid","box_quantity_non_positive","box_quantity_negative","quantity_control_missing","quantity_control_not_supported","duplicate_sku_rows_within_document","repeated_sku_across_documents","documents_with_terminal_completion_inconsistency","documents_with_expected_receipt_inconsistency","documents_not_completed","documents_with_unknown_completion"]
    diag = {key: 0 for key in keys}
    diag["rows_total"] = rows_total
    diag["missing_required_columns"] = []
    for key in ["processing_rate_percent", "pending_rate_percent", "zero_rate_percent", "excluded_rate_percent"]:
        diag[key] = 0.0
    return diag


def build_day_receipts_import(table: pd.DataFrame, mapping: dict[str, str | None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    mapping = dict(mapping or detect_day_receipts_columns(table))
    diag = _base_diag(int(len(table)))
    missing = [FIELDS[field] for field in REQUIRED_FIELDS if not mapping.get(field)]
    diag["missing_required_columns"] = missing
    if missing or table.empty:
        return _empty_state(diag)
    rows: list[dict[str, Any]] = []
    for source_index, (_, src) in enumerate(table.iterrows()):
        def val(field: str) -> Any:
            col = mapping.get(field)
            return src[col] if col else None
        receipt_ref, receipt_number, receipt_date, warehouse = val("receipt_ref"), val("receipt_number"), val("receipt_date"), val("warehouse")
        document_key = f"ref:{_norm(receipt_ref)}" if _text(receipt_ref) else f"sha256:{_hash([receipt_number, receipt_date, warehouse])}"
        sku_key = _text(val("sku_key")) if mapping.get("sku_key") else make_outbound_sku_key(val("nomenclature"), val("characteristic"))
        terminal, expected = _bool(val("terminal_receipt_completed")), _bool(val("expected_receipt"))
        qty, qty_reason = _box_qty(val("source_box_quantity"))
        control = _norm(val("quantity_control"))
        line_key = f"sha256:{_hash([document_key, val('line_number'), sku_key, source_index])}"
        row = {"receipt_line_key": line_key, "document_key": document_key, "receipt_ref": _text(receipt_ref), "receipt_number": _text(receipt_number), "receipt_date": _json_value(receipt_date), "warehouse": _text(warehouse), "distribution_center": _text(val("distribution_center")), "line_number": _json_value(val("line_number")), "sku_key": sku_key, "nomenclature_code": _text(val("nomenclature_code")), "nomenclature": _text(val("nomenclature")), "characteristic_code": _text(val("characteristic_code")), "characteristic": _text(val("characteristic")), "source_box_quantity": _json_value(val("source_box_quantity")), "reported_pallets": _json_value(val("reported_pallets")), "terminal_receipt_completed": terminal, "terminal_receipt_completed_raw": _json_value(val("terminal_receipt_completed")), "expected_receipt": expected, "expected_receipt_raw": _json_value(val("expected_receipt")), "quantity_control": _text(val("quantity_control")), "source_index": int(source_index)}
        if terminal is True: diag["terminal_completed_rows"] += 1
        elif terminal is False: diag["terminal_not_completed_rows"] += 1
        else: diag["terminal_completion_unknown_rows"] += 1
        if expected is True: diag["expected_receipt_true_rows"] += 1
        elif expected is False: diag["expected_receipt_false_rows"] += 1
        else: diag["expected_receipt_unknown_rows"] += 1
        if not document_key: diag["missing_document_key"] += 1
        if not row["warehouse"]: diag["missing_warehouse"] += 1
        if not sku_key: diag["missing_sku"] += 1
        bucket, reason = "excluded_receipt_rows", None
        if control == DIRECT_CONTROL:
            if qty_reason:
                reason = qty_reason; diag[reason] += 1
            elif terminal is True and document_key and row["warehouse"] and sku_key:
                bucket = "accepted_rows"; row["qty_units"] = qty; row["unit_name"] = "короб"
            elif terminal is False:
                bucket = "pending_receipt_rows"; reason = "terminal_receipt_not_completed"; row["qty_units"] = qty; row["unit_name"] = "короб"
            else:
                bucket = "pending_receipt_rows"; reason = "terminal_receipt_completion_unknown"; row["qty_units"] = qty; row["unit_name"] = "короб"
        elif control == NON_POSITIVE_CONTROL:
            bucket = "zero_receipt_rows"; reason = "confirmed_non_positive_receipt"; row["qty_units"] = 0; row["unit_name"] = "короб"
        elif control == NEGATIVE_CONTROL:
            reason = "negative_receipt_quantity"; diag[reason] += 1
        elif control == MISSING_CONTROL:
            reason = "receipt_quantity_missing"; diag[reason] += 1
        elif not control:
            reason = "quantity_control_missing"; diag[reason] += 1
        else:
            reason = "quantity_control_not_supported"; diag[reason] += 1
        if reason:
            row["reason"] = reason
        rows.append({"bucket": bucket, "row": row})
    def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        r = item["row"] if "row" in item else item
        ln = r.get("line_number")
        try: ln_key = (0, float(ln))
        except (TypeError, ValueError): ln_key = (1, _text(ln))
        return (_text(r.get("receipt_date")), _text(r.get("receipt_number")), ln_key, r.get("source_index", -1), r.get("receipt_line_key", ""))
    state = {"documents": [], "document_keys": [], "accepted_rows": [], "pending_receipt_rows": [], "zero_receipt_rows": [], "excluded_receipt_rows": [], "unassigned_rows": []}
    for item in sorted(rows, key=sort_key):
        state[item["bucket"]].append(item["row"])
    all_rows = [item["row"] for item in rows]
    doc_keys = sorted({r["document_key"] for r in all_rows})
    state["document_keys"] = doc_keys
    diag["documents_total"] = len(doc_keys)
    for bucket in ["accepted_rows", "pending_receipt_rows", "zero_receipt_rows", "excluded_receipt_rows"]:
        diag[bucket] = len(state[bucket])
    diag["accepted_boxes"] = sum(r.get("qty_units", 0) for r in state["accepted_rows"])
    diag["pending_receipt_boxes"] = sum(r.get("qty_units", 0) for r in state["pending_receipt_rows"])
    diag["accepted_unique_sku_count"] = len({r["sku_key"] for r in state["accepted_rows"] if r.get("sku_key")})
    sku_docs: dict[str, set[str]] = {}; doc_sku_counts: dict[tuple[str,str], int] = {}
    for r in all_rows:
        if r.get("sku_key"):
            sku_docs.setdefault(r["sku_key"], set()).add(r["document_key"])
            key = (r["document_key"], r["sku_key"]); doc_sku_counts[key] = doc_sku_counts.get(key, 0) + 1
    diag["duplicate_sku_rows_within_document"] = sum(c - 1 for c in doc_sku_counts.values() if c > 1)
    diag["repeated_sku_across_documents"] = sum(1 for docs in sku_docs.values() if len(docs) > 1)
    for dk in doc_keys:
        dr = [r for r in all_rows if r["document_key"] == dk]; acc = [r for r in state["accepted_rows"] if r["document_key"] == dk]
        terms, exps = {r["terminal_receipt_completed"] for r in dr}, {r["expected_receipt"] for r in dr}
        warnings: list[str] = []
        term = next(iter(terms)) if len(terms) == 1 and None not in terms else None
        exp = next(iter(exps)) if len(exps) == 1 and None not in exps else None
        if term is None: warnings.append("terminal_receipt_completion_inconsistent"); diag["documents_with_terminal_completion_inconsistency"] += 1
        if exp is None: warnings.append("expected_receipt_flag_inconsistent"); diag["documents_with_expected_receipt_inconsistency"] += 1
        positive = [r for r in dr if r.get("quantity_control") and _norm(r.get("quantity_control")) == DIRECT_CONTROL and r.get("qty_units", 0) > 0]
        if positive and term is False: warnings.append("document_terminal_receipt_not_completed"); diag["documents_not_completed"] += 1
        if positive and term is None: warnings.append("document_terminal_receipt_completion_unknown"); diag["documents_with_unknown_completion"] += 1
        first = sorted(dr, key=lambda r: (_text(r.get("receipt_date")), _text(r.get("receipt_number"))))[0]
        state["documents"].append({"document_key": dk, "receipt_ref": first["receipt_ref"], "receipt_number": first["receipt_number"], "receipt_date": first["receipt_date"], "warehouse": first["warehouse"], "distribution_center": first["distribution_center"], "row_count": len(dr), "accepted_row_count": len(acc), "pending_row_count": len([r for r in state["pending_receipt_rows"] if r["document_key"] == dk]), "zero_row_count": len([r for r in state["zero_receipt_rows"] if r["document_key"] == dk]), "excluded_row_count": len([r for r in state["excluded_receipt_rows"] if r["document_key"] == dk]), "unique_sku_count": len({r["sku_key"] for r in dr if r.get("sku_key")}), "accepted_boxes": sum(r.get("qty_units", 0) for r in acc), "terminal_receipt_completed": term, "expected_receipt": exp, "warnings": sorted(set(warnings))})
    state["documents"].sort(key=lambda d: (_text(d["receipt_date"]), _text(d["receipt_number"]), _text(d["warehouse"]), d["document_key"]))
    total = diag["rows_total"] or 0
    if total:
        diag["processing_rate_percent"] = round(diag["accepted_rows"] / total * 100, 4)
        diag["pending_rate_percent"] = round(diag["pending_receipt_rows"] / total * 100, 4)
        diag["zero_rate_percent"] = round(diag["zero_receipt_rows"] / total * 100, 4)
        diag["excluded_rate_percent"] = round(diag["excluded_receipt_rows"] / total * 100, 4)
    return state, diag
