from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd


OUTBOUND_ORDERS_PATH = Path("data/last_import/outbound_orders.json")
OUTBOUND_EXECUTION_STATE_PATH = Path("data/last_import/outbound_execution_state.json")
OUTBOUND_EXECUTION_LOG_PATH = Path("data/last_import/outbound_execution_log.json")
PRE_OUTBOUND_SNAPSHOT_PATH = Path("data/last_import/pre_outbound_snapshot.json")

FIELD_ALIASES = {
    "outbound_order_number": ["outbound_order_number", "номер ро", "расходный ордер", "номер расходного ордера", "ро"],
    "created_at": ["created_at", "дата создания", "дата и время", "дата ро"],
    "nomenclature": ["nomenclature", "номенклатура", "наименование", "товар", "sku_name"],
    "characteristic": ["characteristic", "характеристика", "характеристика номенклатуры", "characteristic_name"],
    "qty_units": ["qty_units", "количество", "количество юнитов", "юниты"],
    "unit_name": ["unit_name", "единица", "единица измерения", "ед. изм."],
    "warehouse": ["warehouse", "склад"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return re.sub(r"\s+", " ", str(value).strip())


def _label(value: Any) -> str:
    return _text(value).casefold().replace("ё", "е")


def _find_column(columns: list[str], aliases: list[str]) -> str | None:
    normalized = {_label(column): column for column in columns}
    for alias in aliases:
        if _label(alias) in normalized:
            return normalized[_label(alias)]
    for normalized_name, original in normalized.items():
        if any(_label(alias) in normalized_name for alias in aliases):
            return original
    return None


def make_outbound_sku_key(nomenclature: Any, characteristic: Any = "") -> str:
    name = _text(nomenclature)
    char = _text(characteristic)
    if not name:
        return ""
    return f"name:{name}|char_name:{char}" if char else f"name:{name}"


def placement_sku_key(placement: dict[str, Any]) -> str:
    return make_outbound_sku_key(
        placement.get("nomenclature") or placement.get("sku_name") or placement.get("item_name"),
        placement.get("characteristic") or placement.get("characteristic_name"),
    )


def get_outbound_sheet_names(file_bytes: bytes) -> list[str]:
    with pd.ExcelFile(BytesIO(file_bytes)) as workbook:
        return list(workbook.sheet_names)


def read_outbound_table(file_bytes: bytes, sheet_name: str, header_rows: int = 1) -> pd.DataFrame:
    header: int | list[int] = 0 if header_rows <= 1 else list(range(header_rows))
    table = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=header)
    if isinstance(table.columns, pd.MultiIndex):
        table.columns = [" / ".join(str(part).strip() for part in column if str(part).strip() and not str(part).startswith("Unnamed")) for column in table.columns]
    else:
        table.columns = [str(column).strip() for column in table.columns]
    return table.dropna(how="all")


def detect_outbound_columns(table: pd.DataFrame) -> dict[str, str | None]:
    columns = [str(column) for column in table.columns]
    return {field: _find_column(columns, aliases) for field, aliases in FIELD_ALIASES.items()}


def _parse_units(value: Any) -> tuple[int | None, str]:
    raw = _text(value)
    if not raw:
        return None, "quantity_missing"
    try:
        number = float(raw.replace(",", "."))
    except ValueError:
        return None, "quantity_not_numeric"
    if number < 0:
        return None, "quantity_negative"
    if not number.is_integer():
        return None, "quantity_fractional"
    return int(number), ""


def _created_sort_value(value: Any) -> str:
    text = _text(value)
    if not text:
        return "9999-12-31T23:59:59"
    try:
        return pd.Timestamp(value).isoformat()
    except (TypeError, ValueError):
        return text


def outbound_order_key(warehouse: Any, order_number: Any, created_at: Any) -> str:
    return "|".join((_label(warehouse), _text(order_number), _created_sort_value(created_at)))


def normalize_outbound_table(table: pd.DataFrame, mapping: dict[str, str | None]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    required = ["outbound_order_number", "created_at", "nomenclature", "qty_units", "warehouse"]
    missing = [field for field in required if not mapping.get(field)]
    if missing:
        return [], [{"level": "error", "message": "Не выбраны обязательные колонки: " + ", ".join(missing)}]
    for source_index, (_, source) in enumerate(table.iterrows(), start=1):
        def value(field: str) -> Any:
            column = mapping.get(field)
            return source.get(column) if column else ""

        raw_qty = value("qty_units")
        qty_units, quantity_reason = _parse_units(raw_qty)
        warehouse = _text(value("warehouse"))
        order_number = _text(value("outbound_order_number"))
        created_at = _created_sort_value(value("created_at"))
        nomenclature = _text(value("nomenclature"))
        characteristic = _text(value("characteristic"))
        row = {
            "outbound_order_number": order_number,
            "created_at": created_at,
            "nomenclature": nomenclature,
            "characteristic": characteristic,
            "sku_key": make_outbound_sku_key(nomenclature, characteristic),
            "qty_units": qty_units,
            "qty_units_raw": _text(raw_qty),
            "quantity_validation_reason": quantity_reason,
            "unit_name": _text(value("unit_name")),
            "warehouse": warehouse,
            "source_index": source_index,
            "order_key": outbound_order_key(warehouse, order_number, created_at),
            "line_status": "not_processed",
        }
        rows.append(row)
        if quantity_reason:
            diagnostics.append({"level": "warning", "source_index": source_index, "outbound_order_number": order_number, "reason": quantity_reason})
        if _label(warehouse) != "вешки":
            diagnostics.append({"level": "warning", "source_index": source_index, "outbound_order_number": order_number, "reason": "wrong_warehouse"})
    return rows, diagnostics


def empty_outbound_orders_state(model: dict[str, Any]) -> dict[str, Any]:
    return {"model_id": model.get("model_id"), "loaded_at": "", "source_file_name": "", "source_file_hash": "", "rows": []}


def empty_execution_state(model: dict[str, Any]) -> dict[str, Any]:
    return {"model_id": model.get("model_id"), "updated_at": "", "processed_orders": {}, "line_results": [], "technical_errors": []}


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(default)


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_outbound_orders(model: dict[str, Any]) -> dict[str, Any]:
    state = _load_json(OUTBOUND_ORDERS_PATH, empty_outbound_orders_state(model))
    return state if state.get("model_id") in {None, model.get("model_id")} else empty_outbound_orders_state(model)


def save_outbound_orders(state: dict[str, Any]) -> None:
    _save_json(OUTBOUND_ORDERS_PATH, state)


def load_outbound_execution_state(model: dict[str, Any]) -> dict[str, Any]:
    state = _load_json(OUTBOUND_EXECUTION_STATE_PATH, empty_execution_state(model))
    return state if state.get("model_id") in {None, model.get("model_id")} else empty_execution_state(model)


def save_outbound_execution_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    _save_json(OUTBOUND_EXECUTION_STATE_PATH, state)


def load_outbound_execution_log(model: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = _load_json(OUTBOUND_EXECUTION_LOG_PATH, {"entries": []})
    if not isinstance(payload, dict):
        return []
    if model is not None and payload.get("model_id") not in {None, model.get("model_id")}:
        return []
    return list(payload.get("entries", []))


def save_outbound_execution_log(entries: list[dict[str, Any]], model_id: Any = None) -> None:
    _save_json(OUTBOUND_EXECUTION_LOG_PATH, {"model_id": model_id, "entries": entries})


def ensure_pre_outbound_snapshot(placement_state: dict[str, Any]) -> None:
    existing = _load_json(PRE_OUTBOUND_SNAPSHOT_PATH, {}) if PRE_OUTBOUND_SNAPSHOT_PATH.exists() else {}
    if not PRE_OUTBOUND_SNAPSHOT_PATH.exists() or existing.get("placement_state", {}).get("model_id") != placement_state.get("model_id"):
        _save_json(PRE_OUTBOUND_SNAPSHOT_PATH, {"created_at": _now_iso(), "placement_state": copy.deepcopy(placement_state)})


def summarize_outbound_orders(rows: list[dict[str, Any]], execution_state: dict[str, Any]) -> list[dict[str, Any]]:
    processed = execution_state.get("processed_orders", {})
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(row["order_key"], {
            "order_key": row["order_key"],
            "created_at": row.get("created_at", ""),
            "outbound_order_number": row.get("outbound_order_number", ""),
            "lines_count": 0,
            "requested_units": 0,
            "status": "not_processed",
            "processed": False,
        })
        entry["lines_count"] += 1
        if isinstance(row.get("qty_units"), int):
            entry["requested_units"] += row["qty_units"]
    for key, entry in grouped.items():
        if key in processed:
            entry["status"] = processed[key].get("status", "failed")
            entry["processed"] = True
    return sorted(grouped.values(), key=lambda item: (item["created_at"], item["outbound_order_number"], item["order_key"]))


def _placement_units(placement: dict[str, Any]) -> int:
    value, reason = _parse_units(placement.get("qty_units"))
    return value if not reason and value is not None else 0


def _cell_sort_key(placement: dict[str, Any], model: dict[str, Any]) -> tuple[Any, ...]:
    rows = {str(row.get("row_number")): row for row in model.get("rows", [])}
    cells = {f"{cell.get('row_number')}|{cell.get('cell_number')}|{cell.get('tier') or '1'}": cell for cell in model.get("cells", [])}
    cell = cells.get(str(placement.get("cell_key")), {})
    units = _placement_units(placement)
    capacity_units, _ = _parse_units(placement.get("capacity_units"))
    partial_rank = 0 if capacity_units and 0 < units < capacity_units else 1
    row = rows.get(str(placement.get("row_number")), {})
    row_order = float(row.get("row_order", placement.get("row_order", 10**9)) or 10**9)
    physical_order = float(cell.get("y_center", 10**9) or 10**9)
    cell_number = _text(placement.get("cell_number"))
    try:
        cell_number_key: tuple[int, Any] = (0, int(float(cell_number)))
    except ValueError:
        cell_number_key = (1, cell_number)
    return (partial_rank, row_order, physical_order, cell_number_key, _text(placement.get("cell_key")))


def _line_result(row: dict[str, Any], **updates: Any) -> dict[str, Any]:
    result = {
        "outbound_order_number": row.get("outbound_order_number", ""),
        "created_at": row.get("created_at", ""),
        "nomenclature": row.get("nomenclature", ""),
        "characteristic": row.get("characteristic", ""),
        "sku_key": row.get("sku_key", ""),
        "requested_units": row.get("qty_units") if isinstance(row.get("qty_units"), int) else row.get("qty_units_raw", ""),
        "available_before_units": 0,
        "picked_units": 0,
        "shortage_units": 0,
        "unit_name": row.get("unit_name", ""),
        "line_status": "failed",
        "failure_reason": "",
        "source_index": row.get("source_index", 0),
    }
    result.update(updates)
    return result


def _execute_order(model: dict[str, Any], placement_state: dict[str, Any], order_rows: list[dict[str, Any]], log: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    state = copy.deepcopy(placement_state)
    entries = list(log)
    results: list[dict[str, Any]] = []
    for row in sorted(order_rows, key=lambda item: item.get("source_index", 0)):
        qty = row.get("qty_units")
        if not isinstance(qty, int) or qty < 0:
            results.append(_line_result(row, line_status="invalid_quantity", failure_reason=row.get("quantity_validation_reason") or "invalid_quantity"))
            continue
        if _label(row.get("warehouse")) != "вешки":
            results.append(_line_result(row, requested_units=qty, line_status="wrong_warehouse", failure_reason="wrong_warehouse"))
            continue
        candidates = [placement for placement in state.get("placements", []) if placement_sku_key(placement) == row.get("sku_key") and _placement_units(placement) > 0]
        requested_unit = _label(row.get("unit_name"))
        mismatched = [placement for placement in candidates if requested_unit and _label(placement.get("unit_name")) and _label(placement.get("unit_name")) != requested_unit]
        if mismatched:
            available = sum(_placement_units(placement) for placement in candidates)
            results.append(_line_result(row, requested_units=qty, available_before_units=available, shortage_units=qty, line_status="unit_mismatch", failure_reason="unit_mismatch"))
            continue
        candidates.sort(key=lambda placement: _cell_sort_key(placement, model))
        available_before = sum(_placement_units(placement) for placement in candidates)
        remaining = qty
        picked_total = 0
        for placement in candidates:
            if remaining <= 0:
                break
            before = _placement_units(placement)
            picked = min(remaining, before)
            after = before - picked
            placement["qty_units"] = after
            placement["outbound_units_before"] = before
            placement["outbound_units_picked"] = int(placement.get("outbound_units_picked", 0) or 0) + picked
            placement["last_outbound_order_number"] = row.get("outbound_order_number", "")
            entries.append({
                "sequence_number": len(entries) + 1,
                "outbound_order_number": row.get("outbound_order_number", ""),
                "created_at": row.get("created_at", ""),
                "sku_key": row.get("sku_key", ""),
                "row_number": placement.get("row_number", ""),
                "cell_number": placement.get("cell_number", ""),
                "tier": placement.get("tier", ""),
                "cell_key": placement.get("cell_key", ""),
                "units_before": before,
                "picked_units": picked,
                "units_after": after,
                "unit_name": row.get("unit_name") or placement.get("unit_name", ""),
            })
            remaining -= picked
            picked_total += picked
        state["placements"] = [placement for placement in state.get("placements", []) if _placement_units(placement) > 0]
        shortage = qty - picked_total
        if shortage == 0:
            status, reason = "completed", ""
        elif picked_total > 0:
            status, reason = "partially_completed", "insufficient_units"
        else:
            status, reason = "failed", "insufficient_units"
        result = _line_result(row, requested_units=qty, available_before_units=available_before, picked_units=picked_total, shortage_units=shortage, line_status=status, failure_reason=reason)
        if not requested_unit or any(not _label(placement.get("unit_name")) for placement in candidates):
            result["warning"] = "Единица измерения не указана. Предполагается, что приход и РО используют одинаковые юниты"
        results.append(result)
    return state, results, entries


def execute_outbound_orders(
    model: dict[str, Any],
    placement_state: dict[str, Any],
    order_rows: list[dict[str, Any]],
    execution_state: dict[str, Any] | None = None,
    execution_log: list[dict[str, Any]] | None = None,
    selected_order_keys: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    state = copy.deepcopy(placement_state)
    execution = copy.deepcopy(execution_state or empty_execution_state(model))
    log = copy.deepcopy(execution_log or [])
    processed = execution.setdefault("processed_orders", {})
    selected = set(selected_order_keys) if selected_order_keys is not None else {row.get("order_key") for row in order_rows}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in order_rows:
        if row.get("order_key") in selected and row.get("order_key") not in processed:
            grouped.setdefault(row["order_key"], []).append(row)
    ordered_keys = sorted(grouped, key=lambda key: (grouped[key][0].get("created_at", ""), grouped[key][0].get("outbound_order_number", ""), min(row.get("source_index", 0) for row in grouped[key])))
    occupied_before_run = {item.get("cell_key") for item in state.get("placements", []) if _placement_units(item) > 0}
    for order_key in ordered_keys:
        before_order = copy.deepcopy(state)
        before_log = list(log)
        rows = grouped[order_key]
        try:
            candidate, line_results, candidate_log = _execute_order(model, state, rows, log)
            picked = sum(int(result.get("picked_units", 0) or 0) for result in line_results)
            has_issue = any(result.get("line_status") != "completed" for result in line_results)
            status = "completed" if not has_issue else ("partially_completed" if picked > 0 else "failed")
            state, log = candidate, candidate_log
            processed[order_key] = {"status": status, "processed_at": _now_iso(), "picked_units": picked, "outbound_order_number": rows[0].get("outbound_order_number", ""), "created_at": rows[0].get("created_at", "")}
            execution.setdefault("line_results", []).extend(line_results)
        except Exception as error:  # transaction boundary for one outbound order
            state, log = before_order, before_log
            execution.setdefault("technical_errors", []).append({"order_key": order_key, "outbound_order_number": rows[0].get("outbound_order_number", ""), "created_at": _now_iso(), "error": str(error)})
            processed[order_key] = {"status": "failed", "processed_at": _now_iso(), "technical_error": str(error), "outbound_order_number": rows[0].get("outbound_order_number", ""), "created_at": rows[0].get("created_at", "")}
    occupied_after_run = {item.get("cell_key") for item in state.get("placements", []) if _placement_units(item) > 0}
    freed_cells = set(execution.get("freed_cell_keys", []))
    freed_cells.update(key for key in occupied_before_run - occupied_after_run if key)
    execution["freed_cell_keys"] = sorted(freed_cells)
    summary = outbound_execution_summary(order_rows, execution, execution.get("line_results", []), before_placements=placement_state.get("placements", []), after_placements=state.get("placements", []))
    execution["last_summary"] = summary
    return state, execution, log, summary


def outbound_execution_summary(order_rows: list[dict[str, Any]], execution_state: dict[str, Any], results: list[dict[str, Any]] | None = None, before_placements: list[dict[str, Any]] | None = None, after_placements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    all_results = results if results is not None else execution_state.get("line_results", [])
    statuses = [item.get("status") for item in execution_state.get("processed_orders", {}).values()]
    before_cells = {item.get("cell_key") for item in before_placements or [] if _placement_units(item) > 0}
    after_cells = {item.get("cell_key") for item in after_placements or [] if _placement_units(item) > 0}
    return {
        "Загружено РО": len({row.get("order_key") for row in order_rows}),
        "Обработано РО": len(statuses),
        "Полностью собрано": statuses.count("completed"),
        "Частично собрано": statuses.count("partially_completed"),
        "Не собрано": statuses.count("failed"),
        "Строк с дефицитом": sum(1 for result in all_results if int(result.get("shortage_units", 0) or 0) > 0),
        "Запрошено юнитов": sum(int(result.get("requested_units", 0) or 0) for result in all_results if isinstance(result.get("requested_units"), int)),
        "Собрано юнитов": sum(int(result.get("picked_units", 0) or 0) for result in all_results),
        "Дефицит юнитов": sum(int(result.get("shortage_units", 0) or 0) for result in all_results),
        "Освобождено ячеек": max(len(before_cells - after_cells), len(execution_state.get("freed_cell_keys", []))),
    }


def reset_outbound_execution(model: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    snapshot = _load_json(PRE_OUTBOUND_SNAPSHOT_PATH, {})
    placement_state = snapshot.get("placement_state")
    if placement_state is None or placement_state.get("model_id") != model.get("model_id"):
        return None, {"success": False, "message": "Снимок до моделирования сборки не найден."}
    from warehouse_inventory_placement import save_placement_state

    save_placement_state(placement_state)
    save_outbound_execution_state(empty_execution_state(model))
    save_outbound_execution_log([], model.get("model_id"))
    if PRE_OUTBOUND_SNAPSHOT_PATH.exists():
        PRE_OUTBOUND_SNAPSHOT_PATH.unlink()
    return placement_state, {"success": True, "message": "Размещения восстановлены из снимка. Загруженные РО сохранены."}


def enrich_model_with_outbound_diagnostics(model: dict[str, Any], placement_state: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(model)
    snapshot = _load_json(PRE_OUTBOUND_SNAPSHOT_PATH, {}).get("placement_state", {})
    log = load_outbound_execution_log(model)
    before_by_cell: dict[str, int] = {}
    units: dict[str, set[str]] = {}
    skus: dict[str, set[str]] = {}
    for placement in snapshot.get("placements", []):
        key = placement.get("cell_key", "")
        before_by_cell[key] = before_by_cell.get(key, 0) + _placement_units(placement)
        units.setdefault(key, set()).add(_text(placement.get("unit_name")) or "не указана")
        skus.setdefault(key, set()).add(placement_sku_key(placement) or "Нет данных")
    current_by_cell: dict[str, int] = {}
    for placement in placement_state.get("placements", []):
        key = placement.get("cell_key", "")
        current_by_cell[key] = current_by_cell.get(key, 0) + _placement_units(placement)
        units.setdefault(key, set()).add(_text(placement.get("unit_name")) or "не указана")
        skus.setdefault(key, set()).add(placement_sku_key(placement) or "Нет данных")
    last_by_cell: dict[str, dict[str, Any]] = {}
    for entry in log:
        last_by_cell[entry.get("cell_key", "")] = entry
    for cell in updated.get("cells", []):
        key = f"{cell.get('row_number')}|{cell.get('cell_number')}|{cell.get('tier') or '1'}"
        if key not in before_by_cell and key not in current_by_cell and key not in last_by_cell:
            continue
        before = before_by_cell.get(key, current_by_cell.get(key, 0))
        current = current_by_cell.get(key, 0)
        last = last_by_cell.get(key, {})
        extra = (
            f"\nSKU: {', '.join(sorted(skus.get(key, {'Нет данных'})))}"
            f"\nЕдиница хранения: {', '.join(sorted(units.get(key, {'не указана'})))}"
            f"\nЮнитов до моделирования: {before}"
            f"\nТекущий остаток юнитов: {current}"
            f"\nСписано юнитов: {before - current}"
            f"\nПоследний РО: {last.get('outbound_order_number', '—')}"
        )
        cell["placement_tooltip"] = str(cell.get("placement_tooltip", "")).rstrip() + extra
    return updated
