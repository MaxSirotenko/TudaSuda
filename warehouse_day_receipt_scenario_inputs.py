from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from warehouse_business_identity import normalize_warehouse

BOX_UNIT = "короб"
BOX_UNITS = {"короб", "короба", "коробов"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _normalize_warehouse(value: Any) -> str:
    return normalize_warehouse(value)


def _parse_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = _text(value)
    if not text:
        return None
    normalized = text.replace("T", " ")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return dt.datetime.strptime(normalized[: len(dt.datetime.now().strftime(fmt))], fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def _canonical_hash(payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _batch_key(operational_date: str, normalized_warehouse: str, sku_key: str) -> str:
    return _canonical_hash({"operational_date": operational_date, "normalized_warehouse": normalized_warehouse, "sku_key": sku_key})


def _dedupe_sorted_text(values: list[Any]) -> list[str]:
    return sorted({_text(value) for value in values if _text(value)})


def _source_count(state: dict[str, Any], key: str) -> int:
    value = state.get(key, []) if isinstance(state, dict) else []
    return len(value) if isinstance(value, list) else 0


def _empty_state(op_date: str | None, warehouses: list[str], normalized: list[str], doc_filter: list[str] | None, diagnostics: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    dataset_id = _make_dataset_id(op_date or "", normalized, doc_filter, [])
    scenario = _scenario(dataset_id, op_date or "", [], [], [], 0)
    state = {
        "dataset_id": dataset_id,
        "operational_date": op_date,
        "selected_warehouses": warehouses,
        "selected_normalized_warehouses": normalized,
        "selected_document_keys_filter": doc_filter,
        "receipt_lines": [],
        "receipt_sku_batches": [],
        "selected_documents": [],
        "scenario_inputs": scenario,
        "invalid_accepted_rows": [],
    }
    _finish_diagnostics(diagnostics, state)
    return state, diagnostics


def _make_dataset_id(op_date: str, normalized_warehouses: list[str], doc_filter: list[str] | None, lines: list[dict[str, Any]]) -> str:
    rows = [
        {
            "receipt_line_key": line["receipt_line_key"],
            "document_key": line["document_key"],
            "normalized_warehouse": line["normalized_warehouse"],
            "sku_key": line["sku_key"],
            "qty_units": line["qty_units"],
            "unit_name": line["unit_name"],
        }
        for line in sorted(lines, key=lambda x: (x["receipt_line_key"], x["document_key"], x["normalized_warehouse"], x["sku_key"]))
    ]
    return _canonical_hash({
        "operational_date": op_date,
        "selected_normalized_warehouses": normalized_warehouses,
        "document_filter_mode": "all" if doc_filter is None else "selected",
        "selected_document_keys": [] if doc_filter is None else doc_filter,
        "receipt_lines": rows,
    })


def _scenario(dataset_id: str, op_date: str, line_keys: list[str], batch_keys: list[str], document_keys: list[str], total: int) -> dict[str, dict[str, Any]]:
    base = {
        "receipt_dataset_id": dataset_id,
        "operational_date": op_date,
        "receipt_line_keys": list(line_keys),
        "receipt_batch_keys": list(batch_keys),
        "document_keys": list(document_keys),
        "total_boxes": int(total),
        "unit_name": BOX_UNIT,
    }
    return {"current": copy.deepcopy(base), "proposed": copy.deepcopy(base)}


def _line_sort_key(line: dict[str, Any]) -> tuple[Any, ...]:
    try:
        num = int(_text(line.get("line_number")))
    except ValueError:
        num = 10**12
    try:
        src = int(_text(line.get("source_index")))
    except ValueError:
        src = 10**12
    return (_text(line.get("receipt_date")), _text(line.get("receipt_number")), num, src, _text(line.get("receipt_line_key")))


def _invalid(row: Any, reason: str) -> dict[str, Any]:
    return {"reason": reason, "row": copy.deepcopy(row)}


def _validate(row: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(row, Mapping):
        return None, "accepted_row_not_mapping"
    line_key = _text(row.get("receipt_line_key"))
    if not line_key:
        return None, "receipt_line_key_missing"
    document_key = _text(row.get("document_key"))
    if not document_key:
        return None, "document_key_missing"
    receipt_date = _parse_date(row.get("receipt_date"))
    if receipt_date is None:
        return None, "receipt_date_invalid"
    warehouse = _text(row.get("warehouse"))
    if not warehouse:
        return None, "warehouse_missing"
    sku_key = _text(row.get("sku_key"))
    if not sku_key:
        return None, "sku_missing"
    if "qty_units" not in row or row.get("qty_units") in (None, ""):
        return None, "qty_units_missing"
    qty = row.get("qty_units")
    if isinstance(qty, bool) or not isinstance(qty, int):
        return None, "qty_units_invalid"
    if qty <= 0:
        return None, "qty_units_non_positive"
    if _text(row.get("unit_name")).casefold() not in BOX_UNITS:
        return None, "unit_not_boxes"
    if row.get("terminal_receipt_completed") is not True:
        return None, "terminal_receipt_not_completed"
    result = copy.deepcopy(dict(row))
    result.update({"receipt_line_key": line_key, "document_key": document_key, "sku_key": sku_key, "warehouse": warehouse, "qty_units": int(qty), "unit_name": BOX_UNIT, "_receipt_day": receipt_date, "normalized_warehouse": _normalize_warehouse(warehouse)})
    return result, None


def _finish_diagnostics(diag: dict[str, Any], state: dict[str, Any]) -> None:
    current = state["scenario_inputs"]["current"]
    proposed = state["scenario_inputs"]["proposed"]
    diag.update({
        "selected_receipt_rows": len(state["receipt_lines"]),
        "selected_receipt_boxes": sum(line["qty_units"] for line in state["receipt_lines"]),
        "selected_documents": len(state["selected_documents"]),
        "selected_unique_skus": len({(line["normalized_warehouse"], line["sku_key"]) for line in state["receipt_lines"]}),
        "receipt_sku_batches": len(state["receipt_sku_batches"]),
        "invalid_accepted_rows": len(state["invalid_accepted_rows"]),
        "current_receipt_rows": len(current["receipt_line_keys"]),
        "proposed_receipt_rows": len(proposed["receipt_line_keys"]),
        "current_receipt_boxes": current["total_boxes"],
        "proposed_receipt_boxes": proposed["total_boxes"],
        "scenario_inputs_equal": current == proposed,
    })
    reduction = diag["selected_receipt_rows"] - diag["receipt_sku_batches"]
    diag["aggregation_reduction_rows"] = reduction
    diag["aggregation_reduction_percent"] = 0.0 if diag["selected_receipt_rows"] == 0 else round(reduction / diag["selected_receipt_rows"] * 100, 4)


def build_day_receipt_scenario_inputs(receipt_import_state: dict[str, Any], *, operational_date: Any, selected_warehouses: list[Any], selected_document_keys: list[Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    state_in = receipt_import_state if isinstance(receipt_import_state, dict) else {}
    diagnostics: dict[str, Any] = {
        "source_accepted_rows": _source_count(state_in, "accepted_rows"),
        "source_pending_receipt_rows": _source_count(state_in, "pending_receipt_rows"),
        "source_zero_receipt_rows": _source_count(state_in, "zero_receipt_rows"),
        "source_excluded_receipt_rows": _source_count(state_in, "excluded_receipt_rows"),
        "source_unassigned_rows": _source_count(state_in, "unassigned_rows"),
        "duplicate_receipt_line_keys": 0,
        "rows_wrong_operational_date": 0,
        "rows_wrong_warehouse": 0,
        "rows_filtered_by_document": 0,
        "unknown_selected_document_keys": [],
        "selected_document_keys_outside_scope": [],
        "configuration_errors": [],
    }
    op_date = _parse_date(operational_date)
    selected_wh = _dedupe_sorted_text(selected_warehouses or [])
    normalized_wh = sorted({_normalize_warehouse(value) for value in (selected_warehouses or []) if _normalize_warehouse(value)})
    doc_filter = None if selected_document_keys is None else _dedupe_sorted_text(selected_document_keys)
    if op_date is None:
        diagnostics["configuration_errors"].append("invalid_operational_date")
    if not normalized_wh:
        diagnostics["configuration_errors"].append("selected_warehouses_empty")
    if diagnostics["configuration_errors"]:
        return _empty_state(op_date, selected_wh, normalized_wh, doc_filter, diagnostics)

    valid_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    accepted = state_in.get("accepted_rows", [])
    if not isinstance(accepted, list):
        accepted = []
    for row in accepted:
        valid, reason = _validate(row)
        if reason:
            invalid_rows.append(_invalid(row, reason))
            continue
        assert valid is not None
        if valid["receipt_line_key"] in seen:
            invalid_rows.append(_invalid(row, "duplicate_receipt_line_key"))
            diagnostics["duplicate_receipt_line_keys"] += 1
            continue
        seen.add(valid["receipt_line_key"])
        valid_rows.append(valid)

    existing_keys = {row["document_key"] for row in valid_rows}
    in_scope_keys = {row["document_key"] for row in valid_rows if row["_receipt_day"] == op_date and row["normalized_warehouse"] in normalized_wh}
    if doc_filter is not None:
        diagnostics["unknown_selected_document_keys"] = sorted(set(doc_filter) - existing_keys)
        diagnostics["selected_document_keys_outside_scope"] = sorted((set(doc_filter) & existing_keys) - in_scope_keys)

    selected: list[dict[str, Any]] = []
    for row in valid_rows:
        if row["_receipt_day"] != op_date:
            diagnostics["rows_wrong_operational_date"] += 1
            continue
        if row["normalized_warehouse"] not in normalized_wh:
            diagnostics["rows_wrong_warehouse"] += 1
            continue
        if doc_filter is not None and row["document_key"] not in doc_filter:
            diagnostics["rows_filtered_by_document"] += 1
            continue
        line = {k: v for k, v in row.items() if k != "_receipt_day"}
        line["operational_date"] = op_date
        line["receipt_batch_key"] = _batch_key(op_date, line["normalized_warehouse"], line["sku_key"])
        selected.append(line)
    receipt_lines = sorted(selected, key=_line_sort_key)

    batches_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in receipt_lines:
        key = (op_date, line["normalized_warehouse"], line["sku_key"])
        batch = batches_by_key.setdefault(key, {**{k: line.get(k) for k in ["receipt_batch_key", "operational_date", "warehouse", "normalized_warehouse", "sku_key", "nomenclature_code", "nomenclature", "characteristic_code", "characteristic"]}, "qty_units": 0, "unit_name": BOX_UNIT, "source_line_count": 0, "receipt_line_keys": set(), "document_keys": set(), "receipt_numbers": set(), "first_receipt_date": line.get("receipt_date"), "last_receipt_date": line.get("receipt_date")})
        batch["qty_units"] += line["qty_units"]
        batch["source_line_count"] += 1
        batch["receipt_line_keys"].add(line["receipt_line_key"])
        batch["document_keys"].add(line["document_key"])
        if _text(line.get("receipt_number")):
            batch["receipt_numbers"].add(_text(line.get("receipt_number")))
        batch["first_receipt_date"] = min(_text(batch["first_receipt_date"]), _text(line.get("receipt_date")))
        batch["last_receipt_date"] = max(_text(batch["last_receipt_date"]), _text(line.get("receipt_date")))
    batches = []
    for batch in batches_by_key.values():
        batch["receipt_line_keys"] = sorted(batch["receipt_line_keys"])
        batch["document_keys"] = sorted(batch["document_keys"])
        batch["receipt_numbers"] = sorted(batch["receipt_numbers"])
        batches.append(batch)
    receipt_sku_batches = sorted(batches, key=lambda b: (b["normalized_warehouse"], b["sku_key"], b["receipt_batch_key"]))

    docs: dict[str, dict[str, Any]] = {}
    for line in receipt_lines:
        doc = docs.setdefault(line["document_key"], {"document_key": line["document_key"], "receipt_ref": line.get("receipt_ref"), "receipt_number": line.get("receipt_number"), "warehouse": line.get("warehouse"), "normalized_warehouse": line["normalized_warehouse"], "row_count": 0, "unique_sku_count": 0, "total_boxes": 0, "first_receipt_date": line.get("receipt_date"), "last_receipt_date": line.get("receipt_date"), "receipt_line_keys": [], "_skus": set()})
        doc["row_count"] += 1
        doc["total_boxes"] += line["qty_units"]
        doc["receipt_line_keys"].append(line["receipt_line_key"])
        doc["_skus"].add(line["sku_key"])
        doc["first_receipt_date"] = min(_text(doc["first_receipt_date"]), _text(line.get("receipt_date")))
        doc["last_receipt_date"] = max(_text(doc["last_receipt_date"]), _text(line.get("receipt_date")))
    selected_documents = []
    for doc in docs.values():
        doc["receipt_line_keys"] = sorted(doc["receipt_line_keys"])
        doc["unique_sku_count"] = len(doc.pop("_skus"))
        selected_documents.append(doc)
    selected_documents.sort(key=lambda d: (_text(d["first_receipt_date"]), _text(d.get("receipt_number")), d["normalized_warehouse"], d["document_key"]))

    dataset_id = _make_dataset_id(op_date, normalized_wh, doc_filter, receipt_lines)
    scenario = _scenario(dataset_id, op_date, [l["receipt_line_key"] for l in receipt_lines], [b["receipt_batch_key"] for b in receipt_sku_batches], [d["document_key"] for d in selected_documents], sum(l["qty_units"] for l in receipt_lines))
    state = {"dataset_id": dataset_id, "operational_date": op_date, "selected_warehouses": selected_wh, "selected_normalized_warehouses": normalized_wh, "selected_document_keys_filter": doc_filter, "receipt_lines": receipt_lines, "receipt_sku_batches": receipt_sku_batches, "selected_documents": selected_documents, "scenario_inputs": scenario, "invalid_accepted_rows": invalid_rows}
    _finish_diagnostics(diagnostics, state)
    return state, diagnostics
