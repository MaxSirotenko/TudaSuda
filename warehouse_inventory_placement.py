from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

PLACEMENTS_PATH = Path("data/last_import/placements.json")

SKU_ALIASES = ["sku", "sku_code", "код", "код товара", "номенклатура код", "товар код"]
NAME_ALIASES = ["товар", "наименование", "номенклатура", "item", "item_name", "sku_name"]
PALLET_ALIASES = ["паллет", "паллеты", "количество паллет", "qty_pallets", "pallets"]
BOX_ALIASES = ["короб", "короба", "количество коробов", "qty_boxes", "boxes"]
ADDRESS_ALIASES = ["адрес", "ячейка", "адрес ячейки", "cell", "cell_address"]
ROW_ALIASES = ["ряд", "row", "row_number"]
CELL_ALIASES = ["ячейка", "cell_number", "номер ячейки"]
TIER_ALIASES = ["ярус", "tier"]
OPTIONAL_ALIASES = {
    "expiry_date": ["дата срока годности", "срок годности", "expiry", "expiry_date"],
    "batch": ["партия", "batch"],
    "characteristic": ["характеристика", "characteristic"],
    "weight": ["вес", "weight"],
    "volume": ["объём", "объем", "volume"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_label(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = text.replace(" / ", "/")
    return text


def _display_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _number_key(value: Any) -> tuple[int, Any]:
    text = _display_value(value)
    try:
        return (0, int(float(text)))
    except ValueError:
        return (1, text)


def _find_column(columns: list[str], aliases: list[str]) -> str | None:
    normalized = {_clean_label(col): col for col in columns}
    alias_set = {_clean_label(alias) for alias in aliases}
    for alias in alias_set:
        if alias in normalized:
            return normalized[alias]
    for norm, original in normalized.items():
        if any(alias in norm for alias in alias_set):
            return original
    return None


def cell_key(row_number: Any, cell_number: Any, tier: Any) -> str:
    return f"{_display_value(row_number)}|{_display_value(cell_number)}|{_display_value(tier) or '1'}"


def get_inventory_sheet_names(file_bytes: bytes) -> list[str]:
    with pd.ExcelFile(BytesIO(file_bytes)) as xls:
        return list(xls.sheet_names)


def read_inventory_table(file_bytes: bytes, sheet_name: str, header_rows: int = 1) -> pd.DataFrame:
    header: int | list[int] = 0 if header_rows <= 1 else list(range(header_rows))
    df = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=header)
    if isinstance(df.columns, pd.MultiIndex):
        flattened = []
        for col in df.columns:
            parts = [str(part).strip() for part in col if str(part).strip() and not str(part).startswith("Unnamed")]
            flattened.append(" / ".join(parts))
        df.columns = flattened
    else:
        df.columns = [str(col).strip() for col in df.columns]
    df = df.dropna(how="all")
    df = df.loc[:, [bool(str(col).strip()) for col in df.columns]]
    return df


def detect_inventory_columns(df: pd.DataFrame) -> dict[str, str | None]:
    columns = [str(col) for col in df.columns]
    mapping = {
        "sku_code": _find_column(columns, SKU_ALIASES),
        "sku_name": _find_column(columns, NAME_ALIASES),
        "qty_pallets": _find_column(columns, PALLET_ALIASES),
        "qty_boxes": _find_column(columns, BOX_ALIASES),
        "cell_address": _find_column(columns, ADDRESS_ALIASES),
        "row_number": _find_column(columns, ROW_ALIASES),
        "cell_number": _find_column(columns, CELL_ALIASES),
        "tier": _find_column(columns, TIER_ALIASES),
    }
    for key, aliases in OPTIONAL_ALIASES.items():
        mapping[key] = _find_column(columns, aliases)
    return mapping


def _parse_address(address: str) -> tuple[str, str, str]:
    text = _display_value(address)
    if not text:
        return "", "", ""
    numbers = re.findall(r"\d+", text)
    if len(numbers) >= 3:
        return numbers[0], numbers[1], numbers[2]
    if len(numbers) == 2:
        return numbers[0], numbers[1], "1"
    return "", "", ""


def normalize_inventory_table(df: pd.DataFrame, mapping: dict[str, str | None]) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    diagnostics: list[dict[str, str]] = []
    if not mapping.get("sku_code"):
        diagnostics.append({"level": "error", "message": "Не выбрана колонка SKU / Код товара."})
    if not mapping.get("qty_pallets"):
        diagnostics.append({"level": "error", "message": "Не выбрана колонка Количество паллет."})
    if any(item["level"] == "error" for item in diagnostics):
        return pd.DataFrame(), diagnostics

    result = pd.DataFrame()
    result["sku_code"] = df[mapping["sku_code"]].map(_display_value)
    result["sku_name"] = df[mapping["sku_name"]].map(_display_value) if mapping.get("sku_name") else ""
    result["item_name"] = result["sku_name"]
    result["qty_pallets"] = df[mapping["qty_pallets"]].map(_safe_float)
    result["qty_boxes"] = df[mapping["qty_boxes"]].map(_safe_float) if mapping.get("qty_boxes") else 0.0
    result["row_number"] = df[mapping["row_number"]].map(_display_value) if mapping.get("row_number") else ""
    result["cell_number"] = df[mapping["cell_number"]].map(_display_value) if mapping.get("cell_number") else ""
    result["tier"] = df[mapping["tier"]].map(_display_value) if mapping.get("tier") else ""
    result["cell_address"] = df[mapping["cell_address"]].map(_display_value) if mapping.get("cell_address") else ""
    for idx, row in result.iterrows():
        if (not row["row_number"] or not row["cell_number"]) and row["cell_address"]:
            parsed_row, parsed_cell, parsed_tier = _parse_address(row["cell_address"])
            result.at[idx, "row_number"] = row["row_number"] or parsed_row
            result.at[idx, "cell_number"] = row["cell_number"] or parsed_cell
            result.at[idx, "tier"] = row["tier"] or parsed_tier
        if not result.at[idx, "tier"]:
            result.at[idx, "tier"] = "1"
    for key in OPTIONAL_ALIASES:
        result[key] = df[mapping[key]].map(_display_value) if mapping.get(key) else ""
    result = result[(result["sku_code"] != "") & (result["qty_pallets"] > 0)].copy()
    diagnostics.append({"level": "info", "message": f"Строк инвента после нормализации: {len(result)}."})
    return result, diagnostics


def empty_placement_state(model: dict[str, Any]) -> dict[str, Any]:
    now = _now_iso()
    return {
        "model_id": model.get("model_id"),
        "source_file_hash": model.get("source_file_hash", ""),
        "created_at": now,
        "updated_at": now,
        "placements": [],
        "unplaced_inventory": [],
        "settings": {"allow_mixed_sku_in_deep_lane": False},
        "journal": [],
    }


def load_placement_state(model: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if not PLACEMENTS_PATH.exists():
        return empty_placement_state(model), None
    try:
        state = json.loads(PLACEMENTS_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return empty_placement_state(model), "Файл placements.json повреждён и не был загружен."
    if state.get("model_id") != model.get("model_id"):
        return empty_placement_state(model), "Найдены старые данные размещения, но они относятся к другой модели склада."
    state.setdefault("settings", {"allow_mixed_sku_in_deep_lane": False})
    state.setdefault("placements", [])
    state.setdefault("unplaced_inventory", [])
    state.setdefault("journal", [])
    return state, None


def save_placement_state(state: dict[str, Any]) -> None:
    PLACEMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    PLACEMENTS_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_placement_state() -> None:
    if PLACEMENTS_PATH.exists():
        PLACEMENTS_PATH.unlink()


def _append_journal(state: dict[str, Any], action: str, sku_code: str = "", cell: str = "", qty_pallets: float = 0.0, source: str = "") -> None:
    state.setdefault("journal", []).append({
        "created_at": _now_iso(),
        "action": action,
        "sku_code": sku_code,
        "cell": cell,
        "qty_pallets": qty_pallets,
        "source": source,
    })


def _cell_map(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {cell_key(c.get("row_number"), c.get("cell_number"), c.get("tier")): c for c in model.get("cells", [])}


def _placement_record(row: dict[str, Any], cell: dict[str, Any], qty_pallets: float, source: str, confidence: str, placement_mode: str, comment: str = "") -> dict[str, Any]:
    return {
        "placement_id": str(uuid.uuid4()),
        "sku_code": _display_value(row.get("sku_code")),
        "sku_name": _display_value(row.get("sku_name")),
        "item_name": _display_value(row.get("item_name")) or _display_value(row.get("sku_name")),
        "row_number": _display_value(cell.get("row_number")),
        "cell_number": _display_value(cell.get("cell_number")),
        "tier": _display_value(cell.get("tier")) or "1",
        "cell_key": cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier")),
        "qty_pallets": qty_pallets,
        "qty_boxes": _safe_float(row.get("qty_boxes")),
        "occupied_capacity_pallets": qty_pallets,
        "source": source,
        "confidence": confidence,
        "placement_mode": placement_mode,
        "comment": comment,
    }


def import_inventory(model: dict[str, Any], normalized_df: pd.DataFrame, allow_replace: bool = True) -> tuple[dict[str, Any], list[dict[str, str]]]:
    state = empty_placement_state(model) if allow_replace else load_placement_state(model)[0]
    diagnostics: list[dict[str, str]] = []
    cells = _cell_map(model)
    for row in normalized_df.to_dict("records"):
        key = cell_key(row.get("row_number"), row.get("cell_number"), row.get("tier"))
        if row.get("row_number") and row.get("cell_number") and key in cells:
            placement = _placement_record(row, cells[key], _safe_float(row.get("qty_pallets")), "inventory_with_cell", "exact", "factual", "Импортировано из инвента с адресом ячейки")
            state["placements"].append(placement)
        else:
            state["unplaced_inventory"].append({
                "sku_code": _display_value(row.get("sku_code")),
                "sku_name": _display_value(row.get("sku_name")),
                "item_name": _display_value(row.get("item_name")) or _display_value(row.get("sku_name")),
                "qty_pallets": _safe_float(row.get("qty_pallets")),
                "qty_boxes": _safe_float(row.get("qty_boxes")),
                "reason": "В исходных данных нет адреса ячейки" if not row.get("row_number") or not row.get("cell_number") else "Ячейка не найдена в модели склада",
            })
    _append_journal(state, "импорт инвента", qty_pallets=sum(item.get("qty_pallets", 0) for item in state["unplaced_inventory"]), source="inventory")
    diagnostics.append({"level": "info", "message": f"Размещено по адресам: {len(state['placements'])}; без ячейки: {len(state['unplaced_inventory'])}."})
    save_placement_state(state)
    return state, diagnostics


def _cell_sort_key(cell: dict[str, Any]) -> tuple[Any, Any]:
    row_order = cell.get("row_order", 10**9)
    cell_num = _number_key(cell.get("cell_number"))
    if cell.get("cell_direction") == "top_to_bottom":
        return (row_order, (0, -cell_num[1]) if cell_num[0] == 0 else cell_num)
    return (row_order, cell_num)


def _occupied_by_cell(state: dict[str, Any]) -> dict[str, float]:
    occupied: dict[str, float] = {}
    for placement in state.get("placements", []):
        key = placement.get("cell_key", "")
        occupied[key] = occupied.get(key, 0.0) + _safe_float(placement.get("occupied_capacity_pallets"))
    return occupied


def auto_place_unplaced(model: dict[str, Any], state: dict[str, Any], allow_mixed_sku_in_deep_lane: bool = False) -> tuple[dict[str, Any], list[dict[str, str]]]:
    diagnostics: list[dict[str, str]] = []
    occupied = _occupied_by_cell(state)
    sku_by_cell: dict[str, set[str]] = {}
    for placement in state.get("placements", []):
        sku_by_cell.setdefault(placement.get("cell_key", ""), set()).add(placement.get("sku_code", ""))
    cells = sorted(model.get("cells", []), key=_cell_sort_key)
    remaining_unplaced = []
    for item in sorted(state.get("unplaced_inventory", []), key=lambda x: _safe_float(x.get("qty_pallets")), reverse=True):
        remaining = _safe_float(item.get("qty_pallets"))
        boxes_total = _safe_float(item.get("qty_boxes"))
        for cell in cells:
            if remaining <= 0:
                break
            key = cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier"))
            capacity = _safe_float(cell.get("capacity_pallets"), 1.0)
            used = occupied.get(key, 0.0)
            if used >= capacity:
                continue
            existing_skus = sku_by_cell.get(key, set())
            if existing_skus and item.get("sku_code") not in existing_skus:
                if not (allow_mixed_sku_in_deep_lane and cell.get("storage_type") == "deep_lane"):
                    continue
            free = capacity - used
            qty = min(remaining, free)
            placement = _placement_record(item, cell, qty, "auto_placed", "estimated", "simulated", "Модельное размещение: в инвенте нет адреса ячейки")
            placement["qty_boxes"] = min(boxes_total, boxes_total * (qty / max(_safe_float(item.get("qty_pallets")), 1.0))) if boxes_total else 0.0
            state["placements"].append(placement)
            occupied[key] = used + qty
            sku_by_cell.setdefault(key, set()).add(item.get("sku_code", ""))
            remaining -= qty
        if remaining > 0:
            leftover = dict(item)
            leftover["qty_pallets"] = round(remaining, 4)
            leftover["reason"] = "Не хватило свободной вместимости склада"
            remaining_unplaced.append(leftover)
    state["unplaced_inventory"] = remaining_unplaced
    state.setdefault("settings", {})["allow_mixed_sku_in_deep_lane"] = bool(allow_mixed_sku_in_deep_lane)
    _append_journal(state, "автоматическое размещение", qty_pallets=sum(_safe_float(p.get("qty_pallets")) for p in state.get("placements", []) if p.get("source") == "auto_placed"), source="auto_placed")
    diagnostics.append({"level": "info", "message": f"Автоматически размещено записей: {sum(1 for p in state.get('placements', []) if p.get('source') == 'auto_placed')}; осталось без ячейки: {len(remaining_unplaced)}."})
    save_placement_state(state)
    return state, diagnostics


def manual_place(model: dict[str, Any], state: dict[str, Any], unplaced_index: int, row_number: str, cell_number: str, tier: str, qty_pallets: float, allow_mixed_sku_in_deep_lane: bool = False) -> tuple[dict[str, Any], str | None]:
    if unplaced_index < 0 or unplaced_index >= len(state.get("unplaced_inventory", [])):
        return state, "Выберите товар из списка без ячейки."
    cells = _cell_map(model)
    key = cell_key(row_number, cell_number, tier)
    if key not in cells:
        return state, "Ячейка не найдена в модели склада."
    item = state["unplaced_inventory"][unplaced_index]
    if qty_pallets <= 0 or qty_pallets > _safe_float(item.get("qty_pallets")):
        return state, "Количество паллет должно быть больше 0 и не больше остатка товара."
    occupied = _occupied_by_cell(state)
    cell = cells[key]
    capacity = _safe_float(cell.get("capacity_pallets"), 1.0)
    used = occupied.get(key, 0.0)
    if used + qty_pallets > capacity:
        return state, "Недостаточно свободной вместимости в ячейке."
    existing_skus = {p.get("sku_code") for p in state.get("placements", []) if p.get("cell_key") == key}
    if existing_skus and item.get("sku_code") not in existing_skus:
        if not (allow_mixed_sku_in_deep_lane and cell.get("storage_type") == "deep_lane"):
            return state, "В ячейке уже лежит другой SKU, смешивание выключено."
    placement = _placement_record(item, cell, qty_pallets, "manual", "manual", "simulated", "Ручное размещение")
    state["placements"].append(placement)
    item["qty_pallets"] = round(_safe_float(item.get("qty_pallets")) - qty_pallets, 4)
    if item["qty_pallets"] <= 0:
        state["unplaced_inventory"].pop(unplaced_index)
    _append_journal(state, "ручное размещение", item.get("sku_code", ""), key, qty_pallets, "manual")
    save_placement_state(state)
    return state, None


def delete_placement(state: dict[str, Any], placement_id: str) -> tuple[dict[str, Any], str | None]:
    for idx, placement in enumerate(state.get("placements", [])):
        if placement.get("placement_id") == placement_id:
            state["placements"].pop(idx)
            state.setdefault("unplaced_inventory", []).append({
                "sku_code": placement.get("sku_code", ""),
                "sku_name": placement.get("sku_name", ""),
                "item_name": placement.get("item_name", ""),
                "qty_pallets": _safe_float(placement.get("qty_pallets")),
                "qty_boxes": _safe_float(placement.get("qty_boxes")),
                "reason": "Размещение удалено пользователем",
            })
            _append_journal(state, "удаление размещения", placement.get("sku_code", ""), placement.get("cell_key", ""), _safe_float(placement.get("qty_pallets")), placement.get("source", ""))
            save_placement_state(state)
            return state, None
    return state, "Размещение не найдено."


def update_placement_qty(model: dict[str, Any], state: dict[str, Any], placement_id: str, new_qty_pallets: float) -> tuple[dict[str, Any], str | None]:
    if new_qty_pallets <= 0:
        return state, "Количество паллет должно быть больше 0."
    placement = next((item for item in state.get("placements", []) if item.get("placement_id") == placement_id), None)
    if placement is None:
        return state, "Размещение не найдено."

    cells = _cell_map(model)
    cell = cells.get(placement.get("cell_key", ""))
    if cell is None:
        return state, "Ячейка размещения не найдена в модели склада."
    old_qty = _safe_float(placement.get("qty_pallets"))
    occupied_without_current = sum(
        _safe_float(item.get("occupied_capacity_pallets"))
        for item in state.get("placements", [])
        if item.get("cell_key") == placement.get("cell_key") and item.get("placement_id") != placement_id
    )
    capacity = _safe_float(cell.get("capacity_pallets"), 1.0)
    if occupied_without_current + new_qty_pallets > capacity:
        return state, "Нельзя превысить вместимость ячейки."

    diff = new_qty_pallets - old_qty
    if diff > 0:
        remaining = diff
        for item in list(state.get("unplaced_inventory", [])):
            if item.get("sku_code") != placement.get("sku_code"):
                continue
            take = min(_safe_float(item.get("qty_pallets")), remaining)
            item["qty_pallets"] = round(_safe_float(item.get("qty_pallets")) - take, 4)
            remaining = round(remaining - take, 4)
            if item["qty_pallets"] <= 0:
                state["unplaced_inventory"].remove(item)
            if remaining <= 0:
                break
        if remaining > 0:
            return state, "Недостаточно остатка товара без ячейки для увеличения размещения."
    elif diff < 0:
        state.setdefault("unplaced_inventory", []).append({
            "sku_code": placement.get("sku_code", ""),
            "sku_name": placement.get("sku_name", ""),
            "item_name": placement.get("item_name", ""),
            "qty_pallets": round(abs(diff), 4),
            "qty_boxes": 0.0,
            "reason": "Количество размещения уменьшено пользователем",
        })

    placement["qty_pallets"] = new_qty_pallets
    placement["occupied_capacity_pallets"] = new_qty_pallets
    _append_journal(state, "изменение количества", placement.get("sku_code", ""), placement.get("cell_key", ""), new_qty_pallets, placement.get("source", ""))
    save_placement_state(state)
    return state, None


def move_placement(model: dict[str, Any], state: dict[str, Any], placement_id: str, row_number: str, cell_number: str, tier: str, allow_mixed_sku_in_deep_lane: bool = False) -> tuple[dict[str, Any], str | None]:
    cells = _cell_map(model)
    target_key = cell_key(row_number, cell_number, tier)
    if target_key not in cells:
        return state, "Целевая ячейка не найдена в модели склада."
    placement = next((item for item in state.get("placements", []) if item.get("placement_id") == placement_id), None)
    if placement is None:
        return state, "Размещение не найдено."
    occupied = _occupied_by_cell(state)
    old_key = placement.get("cell_key", "")
    occupied[old_key] = max(0.0, occupied.get(old_key, 0.0) - _safe_float(placement.get("occupied_capacity_pallets")))
    target = cells[target_key]
    capacity = _safe_float(target.get("capacity_pallets"), 1.0)
    qty = _safe_float(placement.get("occupied_capacity_pallets"))
    if occupied.get(target_key, 0.0) + qty > capacity:
        return state, "В целевой ячейке недостаточно свободной вместимости."
    existing_skus = {p.get("sku_code") for p in state.get("placements", []) if p.get("cell_key") == target_key and p.get("placement_id") != placement_id}
    if existing_skus and placement.get("sku_code") not in existing_skus:
        if not (allow_mixed_sku_in_deep_lane and target.get("storage_type") == "deep_lane"):
            return state, "В целевой ячейке уже лежит другой SKU, смешивание выключено."
    placement["row_number"] = _display_value(target.get("row_number"))
    placement["cell_number"] = _display_value(target.get("cell_number"))
    placement["tier"] = _display_value(target.get("tier")) or "1"
    placement["cell_key"] = target_key
    _append_journal(state, "перенос", placement.get("sku_code", ""), target_key, qty, placement.get("source", ""))
    save_placement_state(state)
    return state, None


def placement_summary_by_cell(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for placement in state.get("placements", []):
        key = placement.get("cell_key", "")
        entry = result.setdefault(key, {"occupied_capacity_pallets": 0.0, "placements": [], "sku_codes": set()})
        entry["occupied_capacity_pallets"] += _safe_float(placement.get("occupied_capacity_pallets"))
        entry["placements"].append(placement)
        entry["sku_codes"].add(placement.get("sku_code", ""))
    for entry in result.values():
        entry["sku_codes"] = sorted(entry["sku_codes"])
    return result


def attach_placements_to_model(model: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    updated = dict(model)
    summary = placement_summary_by_cell(state)
    cells = []
    for cell in model.get("cells", []):
        item = dict(cell)
        key = cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier"))
        cell_summary = summary.get(key, {"occupied_capacity_pallets": 0.0, "placements": [], "sku_codes": []})
        capacity = _safe_float(cell.get("capacity_pallets"), 1.0)
        occupied = _safe_float(cell_summary.get("occupied_capacity_pallets"))
        item["occupied_capacity_pallets"] = occupied
        item["free_capacity_pallets"] = max(capacity - occupied, 0.0)
        item["occupancy_label"] = f"{occupied:g}/{capacity:g}"
        item["placement_sku_codes"] = cell_summary.get("sku_codes", [])
        item["placements"] = cell_summary.get("placements", [])
        cells.append(item)
    updated["cells"] = cells
    updated["placements"] = state.get("placements", [])
    updated["unplaced_inventory"] = state.get("unplaced_inventory", [])
    return updated


def placement_diagnostics(model: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    placements = state.get("placements", [])
    unplaced = state.get("unplaced_inventory", [])
    capacity_total = sum(_safe_float(cell.get("capacity_pallets"), 1.0) for cell in model.get("cells", []))
    occupied_total = sum(_safe_float(p.get("occupied_capacity_pallets")) for p in placements)
    deep_cells = [cell for cell in model.get("cells", []) if cell.get("storage_type") == "deep_lane"]
    deep_keys = {cell_key(c.get("row_number"), c.get("cell_number"), c.get("tier")) for c in deep_cells}
    deep_occupied = sum(_safe_float(p.get("occupied_capacity_pallets")) for p in placements if p.get("cell_key") in deep_keys)
    multi_sku_cells = sum(1 for item in placement_summary_by_cell(state).values() if len(item.get("sku_codes", [])) > 1)
    overfilled = []
    by_cell = placement_summary_by_cell(state)
    cells = _cell_map(model)
    for key, summary in by_cell.items():
        if key in cells and _safe_float(summary.get("occupied_capacity_pallets")) > _safe_float(cells[key].get("capacity_pallets"), 1.0):
            overfilled.append(key)
    return {
        "Всего SKU в инвенте": len({p.get("sku_code") for p in placements} | {u.get("sku_code") for u in unplaced}),
        "Всего паллет в инвенте": occupied_total + sum(_safe_float(u.get("qty_pallets")) for u in unplaced),
        "SKU без адреса ячейки": len(unplaced),
        "Размещено SKU": len({p.get("sku_code") for p in placements}),
        "Размещено паллет": occupied_total,
        "Не размещено SKU": len(unplaced),
        "Не размещено паллет": sum(_safe_float(u.get("qty_pallets")) for u in unplaced),
        "Свободная вместимость склада": max(capacity_total - occupied_total, 0.0),
        "Занятая вместимость склада": occupied_total,
        "Всего набивных ячеек": len(deep_cells),
        "Вместимость набивных ячеек в паллетах": sum(_safe_float(c.get("capacity_pallets"), 1.0) for c in deep_cells),
        "Занято в набивных ячейках": deep_occupied,
        "Свободно в набивных ячейках": max(sum(_safe_float(c.get("capacity_pallets"), 1.0) for c in deep_cells) - deep_occupied, 0.0),
        "Переполненные ячейки": len(overfilled),
        "Ячейки с несколькими SKU": multi_sku_cells,
        "Размещение exact": sum(1 for p in placements if p.get("confidence") == "exact"),
        "Размещение estimated": sum(1 for p in placements if p.get("confidence") == "estimated"),
        "Размещение manual": sum(1 for p in placements if p.get("confidence") == "manual"),
    }


def export_placements_excel_bytes(model: dict[str, Any], state: dict[str, Any]) -> bytes:
    cells = _cell_map(model)
    rows = []
    for placement in state.get("placements", []):
        cell = cells.get(placement.get("cell_key", ""), {})
        capacity = _safe_float(cell.get("capacity_pallets"), 1.0)
        occupied = _safe_float(placement.get("occupied_capacity_pallets"))
        rows.append({**placement, "storage_type": cell.get("storage_type", "normal"), "deep_lane_width": cell.get("deep_lane_width", 1), "capacity_pallets": capacity, "free_capacity_pallets": max(capacity - occupied, 0.0)})
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Размещение", index=False)
        pd.DataFrame(state.get("unplaced_inventory", [])).to_excel(writer, sheet_name="Не размещено", index=False)
        pd.DataFrame(state.get("journal", [])).to_excel(writer, sheet_name="Журнал", index=False)
    return buffer.getvalue()
