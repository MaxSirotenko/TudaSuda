from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from warehouse_placement_diagnostics import placement_reason_text

PLACEMENTS_PATH = Path("data/last_import/placements.json")

SKU_ALIASES = ["sku", "sku_code", "код", "код товара", "номенклатура код", "товар код"]
NAME_ALIASES = ["товар", "наименование", "номенклатура", "item", "item_name", "sku_name"]
PALLET_ALIASES = ["паллет", "паллеты", "количество паллет", "qty_pallets", "pallets"]
BOX_ALIASES = ["короб", "короба", "количество коробов", "qty_boxes", "boxes"]
ADDRESS_ALIASES = ["адрес", "ячейка", "адрес ячейки", "cell", "cell_address"]
ROW_ALIASES = ["ряд", "row", "row_number"]
CELL_ALIASES = ["ячейка", "cell_number", "номер ячейки"]
TIER_ALIASES = ["ярус", "tier"]
WEIGHT_CLASS_ALIASES = ["weight_class", "weight_zone", "зона", "весоваякатегория", "весовая категория", "категориявеса", "зонаразмещения", "зона размещения"]
WEIGHT_CLASS_LABELS = {"heavy": "Тяжёлое", "medium": "Среднее", "light": "Лёгкое", "fragile": "Хрупкое", "unclassified": "Не классифицировано"}
WEIGHT_ZONE_LABELS = {"heavy": "Тяжёлое", "medium": "Среднее", "light": "Лёгкое", "fragile": "Хрупкое", "unassigned": "Не назначено"}
OPTIONAL_ALIASES = {
    "expiry_date": ["дата срока годности", "срок годности", "expiry", "expiry_date"],
    "batch": ["партия", "batch"],
    "characteristic": ["характеристика", "characteristic"],
    "characteristic_code": ["характеристика.код", "код характеристики", "characteristic_code"],
    "characteristic_name": ["характеристика.наименование", "характеристика", "characteristic_name"],
    "weight": ["вес", "weight"],
    "volume": ["объём", "объем", "volume"],
    "weight_class": WEIGHT_CLASS_ALIASES,
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


def normalize_weight_class(value: Any) -> str:
    text = _clean_label(value).replace("ё", "е")
    text = text.replace(" ", "")
    if text in {"heavy", "тяжелое", "тяжелый"}:
        return "heavy"
    if text in {"medium", "среднее", "средний"}:
        return "medium"
    if text in {"light", "легкое", "легкий"}:
        return "light"
    if text in {"fragile", "хрупкое", "хрупкий"}:
        return "fragile"
    return "unclassified"


def make_sku_key(item: dict[str, Any]) -> str:
    sku_code = _display_value(item.get("sku_code"))
    sku_name = _display_value(item.get("sku_name") or item.get("item_name"))
    characteristic_code = _display_value(item.get("characteristic_code"))
    characteristic_name = _display_value(item.get("characteristic_name") or item.get("characteristic"))
    if sku_code and characteristic_code:
        return f"code:{sku_code}|char_code:{characteristic_code}"
    if sku_code and characteristic_name:
        return f"code:{sku_code}|char_name:{characteristic_name}"
    if sku_name and characteristic_name:
        return f"name:{sku_name}|char_name:{characteristic_name}"
    if sku_name:
        return f"name:{sku_name}"
    if sku_code:
        return f"code:{sku_code}"
    return ""


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


def get_row_direction(model: dict[str, Any], row_number: Any) -> str:
    row_text = _display_value(row_number)
    for row in model.get("rows", []):
        if _display_value(row.get("row_number")) == row_text and row.get("cell_direction"):
            return _display_value(row.get("cell_direction"))
    for row_setting in model.get("row_settings", []):
        if _display_value(row_setting.get("row_number")) == row_text and row_setting.get("cell_direction"):
            return _display_value(row_setting.get("cell_direction"))
    for cell in model.get("cells", []):
        if _display_value(cell.get("row_number")) == row_text and cell.get("cell_direction"):
            return _display_value(cell.get("cell_direction"))
    return "bottom_to_top"


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
    result["weight_class"] = result["weight_class"].map(normalize_weight_class) if "weight_class" in result else "unclassified"
    if "characteristic_code" not in result:
        result["characteristic_code"] = ""
    if "characteristic_name" not in result:
        result["characteristic_name"] = result.get("characteristic", "")
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
        "sku_key": _display_value(row.get("sku_key")) or make_sku_key(row),
        "sku_code": _display_value(row.get("sku_code")),
        "sku_name": _display_value(row.get("sku_name")),
        "item_name": _display_value(row.get("item_name")) or _display_value(row.get("sku_name")),
        "row_number": _display_value(cell.get("row_number")),
        "cell_number": _display_value(cell.get("cell_number")),
        "tier": _display_value(cell.get("tier")) or "1",
        "cell_key": cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier")),
        "qty_pallets": qty_pallets,
        "qty_boxes": _safe_float(row.get("qty_boxes")),
        "characteristic_code": _display_value(row.get("characteristic_code")),
        "characteristic_name": _display_value(row.get("characteristic_name")) or _display_value(row.get("characteristic")),
        "weight_class": _display_value(row.get("weight_class")) or "unclassified",
        "weight_zone": _display_value(cell.get("weight_zone")) or "unassigned",
        "occupied_capacity_pallets": qty_pallets,
        "source": source,
        "confidence": confidence,
        "placement_mode": placement_mode,
        "placement_status": "placed",
        "unplaced_reason": "",
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
                "sku_key": _display_value(row.get("sku_key")) or make_sku_key(row),
                "sku_code": _display_value(row.get("sku_code")),
                "sku_name": _display_value(row.get("sku_name")),
                "item_name": _display_value(row.get("item_name")) or _display_value(row.get("sku_name")),
                "qty_pallets": _safe_float(row.get("qty_pallets")),
                "qty_boxes": _safe_float(row.get("qty_boxes")),
                "characteristic_code": _display_value(row.get("characteristic_code")),
                "characteristic_name": _display_value(row.get("characteristic_name")) or _display_value(row.get("characteristic")),
                "weight_class": _display_value(row.get("weight_class")) or "unclassified",
                "reason": "В исходных данных нет адреса ячейки" if not row.get("row_number") or not row.get("cell_number") else "Ячейка не найдена в модели склада",
            })
    state["source_unplaced_inventory"] = [dict(item) for item in state.get("unplaced_inventory", [])]
    _append_journal(state, "импорт инвента", qty_pallets=sum(item.get("qty_pallets", 0) for item in state["unplaced_inventory"]), source="inventory")
    diagnostics.append({"level": "info", "message": f"Размещено по адресам: {len(state['placements'])}; без ячейки: {len(state['unplaced_inventory'])}."})
    save_placement_state(state)
    return state, diagnostics


def _cell_sort_key(cell: dict[str, Any], model: dict[str, Any] | None = None) -> tuple[Any, Any]:
    row_order = cell.get("row_order", 10**9)
    cell_num = _number_key(cell.get("cell_number"))
    if cell_num[0] == 0:
        return (row_order, cell_num[1], 0)
    return (row_order, _safe_float(cell.get("y_center")), 1)


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
    cells = sorted(model.get("cells", []), key=lambda cell: _cell_sort_key(cell, model))
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


def _sku_weight_classes(items: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, list[str]]]:
    values: dict[str, set[str]] = {}
    for item in items:
        sku_key = _display_value(item.get("sku_key")) or make_sku_key(item)
        if not sku_key:
            continue
        weight_class_source = item.get("calculated_zone") if "calculated_zone" in item else item.get("weight_class")
        weight_class = normalize_weight_class(weight_class_source)
        if weight_class != "unclassified":
            values.setdefault(sku_key, set()).add(weight_class)
    classes: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    for sku, sku_values in values.items():
        if len(sku_values) > 1:
            conflicts[sku] = sorted(sku_values)
            classes[sku] = "unclassified"
        elif sku_values:
            classes[sku] = next(iter(sku_values))
    for item in items:
        sku = _display_value(item.get("sku_key")) or make_sku_key(item)
        if sku and sku not in classes:
            classes[sku] = "unclassified"
    return classes, conflicts


def _row_zones(model: dict[str, Any]) -> dict[str, str]:
    zones = {}
    for row in model.get("rows", []):
        zone = _display_value(row.get("weight_zone"))
        zones[_display_value(row.get("row_number"))] = zone if zone in {"heavy", "medium", "light", "fragile"} else "unassigned"
    return zones


def _zone_capacity(model: dict[str, Any], occupied: dict[str, float]) -> dict[str, float]:
    free = {"heavy": 0.0, "medium": 0.0, "light": 0.0, "fragile": 0.0, "unassigned": 0.0}
    for cell in model.get("cells", []):
        zone = _display_value(cell.get("weight_zone")) or "unassigned"
        if zone not in free:
            zone = "unassigned"
        key = cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier"))
        free[zone] += max(_safe_float(cell.get("capacity_pallets"), 1.0) - occupied.get(key, 0.0), 0.0)
    return {key: round(value, 4) for key, value in free.items()}


def _unplaced_record(item: dict[str, Any], qty: float, reason: str, source: str, weight_class: str, weight_zone: str = "") -> dict[str, Any]:
    return {
        "sku_key": _display_value(item.get("sku_key")) or make_sku_key(item),
        "sku_code": _display_value(item.get("sku_code")),
        "sku_name": _display_value(item.get("sku_name")),
        "characteristic_code": _display_value(item.get("characteristic_code")),
        "characteristic_name": _display_value(item.get("characteristic_name")) or _display_value(item.get("characteristic")),
        "weight_class": weight_class,
        "source_zone": _display_value(item.get("source_zone")),
        "calculated_zone": _display_value(item.get("calculated_zone")) or weight_class,
        "zone_calculation_reason": _display_value(item.get("zone_calculation_reason")),
        "source_weight": _display_value(item.get("source_weight")),
        "fragile_flag": bool(item.get("fragile_flag")),
        "zone_calculation_status": _display_value(item.get("zone_calculation_status")),
        "receipt_line_ids": [_display_value(item.get("receipt_line_id"))] if item.get("receipt_line_id") else [],
        "receipt_numbers": [_display_value(item.get("receipt_number"))] if item.get("receipt_number") else [],
        "cell_key": "",
        "row_number": "",
        "cell_number": "",
        "weight_zone": weight_zone,
        "qty_pallets": round(qty, 4),
        "source": source,
        "placement_status": "not_placed",
        "placement_mode": "not_calculated",
        "unplaced_reason": reason,
        "reason": reason,
    }


def _basic_placement_record(item: dict[str, Any], cell: dict[str, Any], qty: float, source: str, mode: str, weight_class: str, reason: str = "", reason_code: str = "fallback", quantity_before: float = 0.0) -> dict[str, Any]:
    placement = _placement_record({**item, "weight_class": weight_class}, cell, qty, source, "estimated" if mode != "factual" else "exact", mode, "Базовое механическое размещение")
    placement.update({
        "sku_key": _display_value(item.get("sku_key")) or make_sku_key(item),
        "weight_class": weight_class,
        "source_zone": _display_value(item.get("source_zone")),
        "calculated_zone": _display_value(item.get("calculated_zone")) or weight_class,
        "zone_calculation_reason": _display_value(item.get("zone_calculation_reason")),
        "source_weight": _display_value(item.get("source_weight")),
        "fragile_flag": bool(item.get("fragile_flag")),
        "zone_calculation_status": _display_value(item.get("zone_calculation_status")),
        "receipt_line_ids": [_display_value(item.get("receipt_line_id"))] if item.get("receipt_line_id") else [],
        "receipt_numbers": [_display_value(item.get("receipt_number"))] if item.get("receipt_number") else [],
        "weight_zone": _display_value(cell.get("weight_zone")) or "unassigned",
        "placement_status": "placed" if not reason else "error",
        "placement_mode": mode,
        "unplaced_reason": reason,
        "placement_reason_code": reason_code,
        "placement_reason_text": placement_reason_text(reason_code),
        "placement_source": source,
        "was_occupied_before": quantity_before > 0,
        "quantity_before": round(quantity_before, 4),
        "quantity_added": round(qty, 4),
        "quantity_after": round(quantity_before + qty, 4),
    })
    return placement


def _same_item(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (_display_value(a.get("sku_key")) or make_sku_key(a)) == (_display_value(b.get("sku_key")) or make_sku_key(b))


def calculate_basic_weight_placement(model: dict[str, Any], state: dict[str, Any], receipts_state: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    receipts = [dict(item) for item in (receipts_state or {}).get("receipts", [])]
    for item in receipts:
        item.setdefault("source", "receipt")
        item["sku_key"] = _display_value(item.get("sku_key")) or make_sku_key(item)
    receipts = sorted(receipts, key=lambda item: (_display_value(item.get("receipt_date")), _display_value(item.get("receipt_number")), _number_key(item.get("source_row_number"))))
    source_unplaced = [dict(item) for item in state.get("source_unplaced_inventory") or state.get("unplaced_inventory", [])]
    for item in source_unplaced:
        item.setdefault("source", "inventory_without_cell")
        item["sku_key"] = _display_value(item.get("sku_key")) or make_sku_key(item)
    factual = [dict(p) for p in state.get("placements", []) if p.get("placement_mode") == "factual" or p.get("source") == "inventory_with_cell"]
    manual = [dict(p) for p in state.get("placements", []) if p.get("placement_mode") == "manual" or p.get("source") == "manual"]
    all_items = factual + source_unplaced + receipts
    sku_classes, conflicts = _sku_weight_classes(all_items)
    row_zones = _row_zones(model)
    unassigned_rows = [row for row, zone in row_zones.items() if zone == "unassigned"]
    cells = _cell_map(model)
    ordered_cells = sorted(model.get("cells", []), key=lambda cell: _cell_sort_key(cell, model))
    occupied: dict[str, float] = {}
    sku_by_cell: dict[str, set[str]] = {}
    placed: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    zone_mismatches: list[dict[str, Any]] = []

    for placement in factual + manual:
        key = placement.get("cell_key") or cell_key(placement.get("row_number"), placement.get("cell_number"), placement.get("tier"))
        cell = cells.get(key)
        sku = _display_value(placement.get("sku_key")) or make_sku_key(placement)
        placement["sku_key"] = sku
        weight_class = sku_classes.get(sku, normalize_weight_class(placement.get("weight_class")))
        qty = _safe_float(placement.get("qty_pallets"))
        if cell:
            zone = _display_value(cell.get("weight_zone")) or "unassigned"
            before_qty = occupied.get(key, 0.0)
            placement.update({
                "cell_key": key,
                "weight_class": weight_class,
                "weight_zone": zone,
                "placement_status": "placed",
                "placement_mode": "factual" if placement in factual else placement.get("placement_mode", "manual"),
                "unplaced_reason": "",
                "placement_reason_code": placement.get("placement_reason_code") or "existing_stock",
                "placement_reason_text": placement.get("placement_reason_text") or placement_reason_text("existing_stock"),
                "placement_source": placement.get("placement_source") or placement.get("source") or "existing_stock",
                "was_occupied_before": True,
                "quantity_before": before_qty,
                "quantity_added": 0.0,
                "quantity_after": before_qty + qty,
            })
            if weight_class in {"heavy", "medium", "light", "fragile"} and zone != weight_class:
                placement["zone_mismatch"] = True
                placement["unplaced_reason"] = "zone_mismatch"
                zone_mismatches.append({"sku_key": sku, "sku_code": placement.get("sku_code", ""), "cell_key": key, "weight_class": weight_class, "weight_zone": zone})
            occupied[key] = occupied.get(key, 0.0) + qty
            sku_by_cell.setdefault(key, set()).add(sku)
        placed.append(placement)

    def merge_or_append(record: dict[str, Any]) -> None:
        for existing in placed:
            if existing.get("cell_key") == record.get("cell_key") and _same_item(existing, record) and existing.get("source") == record.get("source") and existing.get("placement_mode") == record.get("placement_mode"):
                existing["qty_pallets"] = round(_safe_float(existing.get("qty_pallets")) + _safe_float(record.get("qty_pallets")), 4)
                existing["occupied_capacity_pallets"] = round(_safe_float(existing.get("occupied_capacity_pallets")) + _safe_float(record.get("occupied_capacity_pallets")), 4)
                existing["quantity_added"] = round(_safe_float(existing.get("quantity_added")) + _safe_float(record.get("quantity_added")), 4)
                existing["quantity_after"] = round(_safe_float(existing.get("quantity_before")) + _safe_float(existing.get("quantity_added")), 4)
                for field in ["receipt_line_ids", "receipt_numbers"]:
                    merged = list(existing.get(field) or [])
                    for value in record.get(field) or []:
                        if value and value not in merged:
                            merged.append(value)
                    existing[field] = merged
                return
        placed.append(record)

    def candidate_cells_for_sku(zone_cells: list[dict[str, Any]], sku_key: str) -> list[dict[str, Any]]:
        sku_cells = [cells[p.get("cell_key")] for p in placed if _display_value(p.get("sku_key")) == sku_key and p.get("cell_key") in cells and cells[p.get("cell_key")].get("weight_zone") in {p.get("weight_zone"), sku_classes.get(sku_key)}]
        if not sku_cells:
            return zone_cells
        row_orders = [_safe_float(cell.get("row_order"), 10**9) for cell in sku_cells]
        same_rows = {_display_value(cell.get("row_number")) for cell in sku_cells}
        cell_positions = {(_display_value(cell.get("row_number")), _number_key(cell.get("cell_number"))[1]) for cell in sku_cells if _number_key(cell.get("cell_number"))[0] == 0}

        def rank(cell: dict[str, Any]) -> tuple[float, float, Any, Any]:
            row = _display_value(cell.get("row_number"))
            cell_num_key = _number_key(cell.get("cell_number"))
            cell_num = cell_num_key[1] if cell_num_key[0] == 0 else 10**9
            if row in same_rows:
                row_positions = [pos for sku_row, pos in cell_positions if sku_row == row]
                min_cell_delta = min(abs(cell_num - pos) for pos in row_positions) if row_positions and cell_num != 10**9 else 10**9
                priority = 0 if min_cell_delta == 1 else 1
            else:
                min_cell_delta = 10**9
                priority = 2
            min_row_delta = min(abs(_safe_float(cell.get("row_order"), 10**9) - row_order) for row_order in row_orders)
            return (priority, min_cell_delta if priority < 2 else min_row_delta, *_cell_sort_key(cell, model))

        return sorted(zone_cells, key=rank)

    def place_item(item: dict[str, Any], source: str) -> None:
        sku = _display_value(item.get("sku_key")) or make_sku_key(item)
        item["sku_key"] = sku
        total_qty = _safe_float(item.get("qty_pallets"))
        weight_class = sku_classes.get(sku, "unclassified")
        if total_qty <= 0:
            unplaced.append(_unplaced_record(item, total_qty, "invalid_qty_pallets", source, weight_class))
            return
        if sku in conflicts:
            unplaced.append(_unplaced_record(item, total_qty, "conflicting_weight_class", source, weight_class))
            return
        if weight_class == "unclassified":
            reason = "missing_calculated_zone" if "calculated_zone" in item else "missing_weight_class"
            unplaced.append(_unplaced_record(item, total_qty, reason, source, weight_class))
            return
        zone_cells = [cell for cell in ordered_cells if cell.get("weight_zone") == weight_class]
        if not zone_cells:
            unplaced.append(_unplaced_record(item, total_qty, "no_rows_for_zone", source, weight_class, weight_class))
            return
        remaining = total_qty
        # first fill same SKU/characteristic partial cells in the same zone
        same_cells = []
        for cell in zone_cells:
            key = cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier"))
            if occupied.get(key, 0.0) <= 0:
                continue
            if any(_same_item(p, item) and p.get("weight_zone") == weight_class for p in placed if p.get("cell_key") == key):
                same_cells.append(cell)
        for cell in same_cells + candidate_cells_for_sku(zone_cells, sku):
            if remaining <= 0:
                break
            key = cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier"))
            capacity = _safe_float(cell.get("capacity_pallets"), 1.0)
            used = occupied.get(key, 0.0)
            if used >= capacity:
                continue
            existing_skus = sku_by_cell.get(key, set())
            if existing_skus and sku not in existing_skus:
                continue
            if existing_skus and cell not in same_cells:
                continue
            qty = min(remaining, capacity - used)
            if qty <= 0:
                continue
            reason_code = "same_sku_partial_cell" if used > 0 else ("fragile_priority" if weight_class == "fragile" else ("adjacent_to_same_sku" if any(_display_value(p.get("sku_key")) == sku for p in placed) else "matching_weight_zone"))
            record = _basic_placement_record(item, cell, qty, source, "calculated", weight_class, reason_code=reason_code, quantity_before=used)
            merge_or_append(record)
            occupied[key] = used + qty
            sku_by_cell.setdefault(key, set()).add(sku)
            remaining = round(remaining - qty, 4)
        if remaining > 0:
            unplaced.append(_unplaced_record(item, remaining, "insufficient_zone_capacity", source, weight_class, weight_class))

    for item in source_unplaced:
        place_item(item, "inventory_without_cell")
    for item in receipts:
        place_item(item, "receipt")

    diagnostics = {
        "Всего паллет": round(sum(_safe_float(p.get("qty_pallets")) for p in placed) + sum(_safe_float(u.get("qty_pallets")) for u in unplaced), 4),
        "Размещено": round(sum(_safe_float(p.get("qty_pallets")) for p in placed), 4),
        "Не размещено": round(sum(_safe_float(u.get("qty_pallets")) for u in unplaced), 4),
        "Размещено heavy": round(sum(_safe_float(p.get("qty_pallets")) for p in placed if p.get("weight_zone") == "heavy"), 4),
        "Размещено medium": round(sum(_safe_float(p.get("qty_pallets")) for p in placed if p.get("weight_zone") == "medium"), 4),
        "Размещено light": round(sum(_safe_float(p.get("qty_pallets")) for p in placed if p.get("weight_zone") == "light"), 4),
        "Размещено fragile": round(sum(_safe_float(p.get("qty_pallets")) for p in placed if p.get("weight_zone") == "fragile"), 4),
        "Свободная вместимость зон": _zone_capacity(model, occupied),
        "SKU без весовой категории": sorted([sku for sku, cls in sku_classes.items() if cls == "unclassified" and sku not in conflicts]),
        "Ряды без назначенной зоны": unassigned_rows,
        "Конфликты категорий SKU": conflicts,
        "Фактические остатки с zone_mismatch": zone_mismatches,
        "Неразмещённые позиции": unplaced,
    }
    state["placements"] = placed
    state["unplaced_inventory"] = unplaced
    state["basic_placement_diagnostics"] = diagnostics
    state.setdefault("settings", {})["basic_weight_placement_enabled"] = True
    _append_journal(state, "базовое размещение по весовым зонам", qty_pallets=diagnostics["Размещено"], source="basic_weight_placement")
    save_placement_state(state)
    return state, diagnostics


def clear_calculated_placements(state: dict[str, Any]) -> dict[str, Any]:
    state["placements"] = [p for p in state.get("placements", []) if p.get("placement_mode") in {"factual", "manual"}]
    if "source_unplaced_inventory" in state:
        state["unplaced_inventory"] = [dict(item) for item in state.get("source_unplaced_inventory", [])]
    else:
        state["unplaced_inventory"] = [u for u in state.get("unplaced_inventory", []) if u.get("source") not in {"receipt", "inventory_without_cell"}]
    state.pop("basic_placement_diagnostics", None)
    save_placement_state(state)
    return state
