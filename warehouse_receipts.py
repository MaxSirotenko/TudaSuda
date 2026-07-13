from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

RECEIPTS_PATH = Path("data/last_import/receipts.json")

SKU_ALIASES = ["sku_code", "код", "код товара", "номенклатура.код", "кодноменклатуры"]
NAME_ALIASES = ["sku_name", "наименование", "номенклатура", "номенклатура.наименование", "товар"]
PALLET_ALIASES = ["qty_pallets", "паллет", "паллеты", "количествопаллет", "количество паллет"]
BOX_ALIASES = ["qty_boxes", "короба", "количествокоробов", "количество коробов"]
UNIT_ALIASES = ["qty_units", "количество", "количествофакт", "количествоприход"]
RECEIPT_DATE_ALIASES = ["receipt_date", "дата", "датаприхода", "датадокумента"]
RECEIPT_NUMBER_ALIASES = ["receipt_number", "номер", "номер документа", "номердокумента"]
RECEIPT_DOCUMENT_ALIASES = ["receipt_document", "документ", "ссылка", "документприхода", "заданиенаприемку", "приходныйордер"]
WAREHOUSE_ALIASES = ["warehouse", "склад"]
WAREHOUSE_ZONE_ALIASES = ["warehouse_zone", "зона", "зона склада", "складская зона"]
CHARACTERISTIC_CODE_ALIASES = ["characteristic_code", "характеристика.код", "код характеристики"]
CHARACTERISTIC_NAME_ALIASES = ["характеристика", "характеристика.наименование", "characteristic_name"]
BATCH_ALIASES = ["batch", "партия"]
EXPIRY_DATE_ALIASES = ["expiry_date", "срок годности", "датасрокагодности", "годен до"]
COMMENT_ALIASES = ["comment", "комментарий", "примечание"]
WEIGHT_CLASS_ALIASES = ["weight_class", "weight_zone", "весоваякатегория", "весовая категория", "категориявеса", "зонаразмещения", "зона размещения"]
WEIGHT_ALIASES = ["weight_kg", "weight", "вес", "вескг", "вес, кг", "вес товара", "вес брутто", "масса"]
FRAGILE_ALIASES = ["fragile", "is_fragile", "хрупкое", "хрупкий", "признакхрупкости", "признак хрупкости"]
SOURCE_ZONE_ALIASES = ["source_zone", "зона", "зона 1с", "исходная зона", "исходная зона 1с"]

RECEIPT_COLUMNS = [
    "receipt_id",
    "receipt_line_id",
    "source_row_number",
    "sku_key",
    "receipt_date",
    "receipt_number",
    "receipt_document",
    "warehouse",
    "warehouse_zone",
    "sku_code",
    "sku_name",
    "characteristic_code",
    "characteristic_name",
    "batch",
    "expiry_date",
    "qty_units",
    "qty_boxes",
    "qty_pallets",
    "placement_status",
    "placement_mode",
    "comment",
    "weight_class",
    "source_zone",
    "calculated_zone",
    "zone_calculation_reason",
    "source_weight_raw",
    "source_weight",
    "weight_parse_status",
    "weight_parse_reason",
    "fragile_flag",
    "zone_calculation_status",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_label(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip().lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    text = text.replace(" / ", "/")
    return text


def _display_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalize_weight_class(value: Any) -> str:
    text = _clean_label(value).replace("ё", "е").replace(" ", "")
    if text in {"heavy", "тяжелое", "тяжелый"}:
        return "heavy"
    if text in {"medium", "среднее", "средний"}:
        return "medium"
    if text in {"light", "легкое", "легкий"}:
        return "light"
    if text in {"fragile", "хрупкое", "хрупкий"}:
        return "fragile"
    return "unclassified"



def _truthy_flag(value: Any) -> bool:
    text = _clean_label(value).replace(" ", "")
    return text in {"1", "true", "yes", "y", "да", "истина", "хрупкое", "хрупкий", "fragile"}


def parse_weight_value(value: Any) -> tuple[str, float | None, str, str]:
    raw = "" if value is None or pd.isna(value) else str(value)
    text = raw.strip()
    if not text:
        return raw, None, "empty", "Пустое значение веса"
    normalized = text.replace("\u00a0", "").replace(" ", "").lower()
    normalized = normalized.replace("кг", "").replace("kg", "").replace(",", ".")
    normalized = re.sub(r"[^0-9.+-]", "", normalized)
    try:
        weight = float(normalized)
    except ValueError:
        return raw, None, "error", "Ошибка преобразования веса"
    if weight <= 0:
        return raw, weight, "error", "Вес меньше или равен 0"
    return raw, weight, "ok", ""


def make_sku_key(item: dict[str, Any]) -> str:
    sku_code = _display_value(item.get("sku_code"))
    sku_name = _display_value(item.get("sku_name"))
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


def make_receipt_line_id(item: dict[str, Any]) -> str:
    receipt_number = _display_value(item.get("receipt_number")) or "без_ордера"
    source_row = _display_value(item.get("source_row_number")) or _display_value(item.get("row_index")) or "0"
    sku_key = _display_value(item.get("sku_key")) or make_sku_key(item) or "без_sku"
    safe = re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", f"{receipt_number}|{source_row}|{sku_key}")
    return safe.strip("_")


def default_zone_classification_settings() -> dict[str, Any]:
    return {
        "weight_column": None,
        "fragile_column": None,
        "source_zone_column": None,
        "max_light_weight_kg": 5.0,
        "max_medium_weight_kg": 15.0,
        "calculated_at": "",
        "settings_hash": "",
    }


def zone_classification_settings_hash(settings: dict[str, Any]) -> str:
    payload = {
        "weight_column": _display_value(settings.get("weight_column")),
        "fragile_column": _display_value(settings.get("fragile_column")),
        "source_zone_column": _display_value(settings.get("source_zone_column")),
        "max_light_weight_kg": _safe_float(settings.get("max_light_weight_kg")),
        "max_medium_weight_kg": _safe_float(settings.get("max_medium_weight_kg")),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def detect_zone_classification_columns(df: pd.DataFrame) -> dict[str, str | None]:
    columns = [str(col) for col in df.columns]
    return {
        "weight_column": _find_column(columns, WEIGHT_ALIASES),
        "fragile_column": _find_column(columns, FRAGILE_ALIASES),
        "source_zone_column": _find_column(columns, SOURCE_ZONE_ALIASES),
    }


def _calculated_zone_for(weight: float | None, fragile: bool, light_limit: float, medium_limit: float) -> tuple[str, str]:
    if fragile:
        return "fragile", "Признак хрупкости"
    if weight is None:
        return "unclassified", "Вес отсутствует"
    if weight <= light_limit:
        return "light", "Вес входит в диапазон лёгкого"
    if weight <= medium_limit:
        return "medium", "Вес входит в диапазон среднего"
    return "heavy", "Вес выше границы среднего"


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _weight_conflict(values: list[float]) -> tuple[bool, float | None]:
    if not values:
        return False, None
    median = _median(values)
    tolerance = max(0.05, abs(median) * 0.02)
    return any(abs(value - median) > tolerance for value in values), median


def calculate_receipt_zones(receipts: list[dict[str, Any]], settings: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    light_limit = _safe_float(settings.get("max_light_weight_kg"), 0.0)
    medium_limit = _safe_float(settings.get("max_medium_weight_kg"), 0.0)
    rows = [dict(item) for item in receipts]
    by_sku: dict[str, dict[str, Any]] = {}
    for item in rows:
        sku_key = _display_value(item.get("sku_key")) or make_sku_key(item)
        item["sku_key"] = sku_key
        item["receipt_line_id"] = _display_value(item.get("receipt_line_id")) or make_receipt_line_id(item)
        if not sku_key:
            continue
        weight = _safe_float(item.get("source_weight"), None) if item.get("weight_parse_status") == "ok" else None
        fragile = bool(item.get("fragile_flag"))
        bucket = by_sku.setdefault(sku_key, {"weights": [], "fragile": set(), "receipts": set(), "rows": 0, "qty_pallets": 0.0})
        if weight is not None and weight > 0:
            bucket["weights"].append(weight)
        bucket["fragile"].add(fragile)
        if item.get("receipt_number"):
            bucket["receipts"].add(_display_value(item.get("receipt_number")))
        bucket["rows"] += 1
        bucket["qty_pallets"] += _safe_float(item.get("qty_pallets"))
    conflicts: set[str] = set()
    sku_result: dict[str, tuple[str, str, str]] = {}
    for sku_key, values in by_sku.items():
        has_weight_conflict, median_weight = _weight_conflict(values["weights"])
        has_fragile_conflict = len(values["fragile"]) > 1
        if has_weight_conflict or has_fragile_conflict:
            conflicts.add(sku_key)
            sku_result[sku_key] = ("unclassified", "Конфликт данных SKU", "conflict")
            continue
        fragile = next(iter(values["fragile"]), False)
        zone, reason = _calculated_zone_for(median_weight, fragile, light_limit, medium_limit)
        status = "ok" if zone != "unclassified" else "error"
        sku_result[sku_key] = (zone, reason, status)
    mismatches = 0
    for item in rows:
        sku_key = _display_value(item.get("sku_key")) or make_sku_key(item)
        zone, reason, status = sku_result.get(sku_key, ("unclassified", "Вес отсутствует", "error"))
        source_zone = _normalize_weight_class(item.get("source_zone"))
        if source_zone != "unclassified" and source_zone != zone:
            mismatches += 1
        item["calculated_zone"] = zone
        item["weight_class"] = zone
        item["zone_calculation_reason"] = reason
        item["zone_calculation_status"] = status if source_zone in {"unclassified", zone} else "mismatch"
    sku_zones = {sku_key: result[0] for sku_key, result in sku_result.items()}
    multi_receipt_sku = sorted([sku_key for sku_key, values in by_sku.items() if len(values["receipts"]) > 1])
    repeated_rows = sum(max(int(values["rows"]) - 1, 0) for values in by_sku.values())
    diagnostics = {
        "Количество приходных ордеров": len({item.get("receipt_number") for item in rows if item.get("receipt_number")}),
        "Количество строк прихода": len(rows),
        "Всего SKU": len(sku_zones),
        "Лёгких SKU": sum(1 for value in sku_zones.values() if value == "light"),
        "Средних SKU": sum(1 for value in sku_zones.values() if value == "medium"),
        "Тяжёлых SKU": sum(1 for value in sku_zones.values() if value == "heavy"),
        "Хрупких SKU": sum(1 for value in sku_zones.values() if value == "fragile"),
        "SKU без рассчитанной категории": sum(1 for value in sku_zones.values() if value == "unclassified"),
        "Конфликтов данных": len(conflicts),
        "SKU в нескольких приходах": len(multi_receipt_sku),
        "Повторных строк одинакового SKU": repeated_rows,
        "Паллет по SKU": {sku_key: round(values["qty_pallets"], 4) for sku_key, values in by_sku.items()},
        "Несовпадений с исходной зоной 1С": mismatches,
        "conflicts": sorted(conflicts),
        "multi_receipt_sku": multi_receipt_sku,
        "settings_hash": zone_classification_settings_hash(settings),
    }
    return rows, diagnostics


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = _display_value(value).replace(" ", "").replace(",", ".")
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


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


def get_receipt_sheet_names(file_bytes: bytes) -> list[str]:
    with pd.ExcelFile(BytesIO(file_bytes)) as xls:
        return list(xls.sheet_names)


def read_receipt_table(file_bytes: bytes, sheet_name: str, header_rows: int = 1) -> pd.DataFrame:
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


def detect_receipt_columns(df: pd.DataFrame) -> dict[str, str | None]:
    columns = [str(col) for col in df.columns]
    return {
        "sku_code": _find_column(columns, SKU_ALIASES),
        "sku_name": _find_column(columns, NAME_ALIASES),
        "qty_pallets": _find_column(columns, PALLET_ALIASES),
        "qty_boxes": _find_column(columns, BOX_ALIASES),
        "qty_units": _find_column(columns, UNIT_ALIASES),
        "receipt_date": _find_column(columns, RECEIPT_DATE_ALIASES),
        "receipt_number": _find_column(columns, RECEIPT_NUMBER_ALIASES),
        "receipt_document": _find_column(columns, RECEIPT_DOCUMENT_ALIASES),
        "warehouse": _find_column(columns, WAREHOUSE_ALIASES),
        "warehouse_zone": _find_column(columns, WAREHOUSE_ZONE_ALIASES),
        "characteristic_code": _find_column(columns, CHARACTERISTIC_CODE_ALIASES),
        "characteristic_name": _find_column(columns, CHARACTERISTIC_NAME_ALIASES),
        "batch": _find_column(columns, BATCH_ALIASES),
        "expiry_date": _find_column(columns, EXPIRY_DATE_ALIASES),
        "comment": _find_column(columns, COMMENT_ALIASES),
        "weight_class": _find_column(columns, WEIGHT_CLASS_ALIASES),
        "source_zone": _find_column(columns, SOURCE_ZONE_ALIASES),
        "source_weight": _find_column(columns, WEIGHT_ALIASES),
        "fragile_flag": _find_column(columns, FRAGILE_ALIASES),
    }


def normalize_receipt_table(df: pd.DataFrame, mapping: dict[str, str | None]) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, str]]]:
    messages: list[dict[str, str]] = []
    if not mapping.get("sku_code"):
        messages.append({"level": "error", "message": "Не выбрана обязательная колонка: Код товара."})
    if not mapping.get("sku_name"):
        messages.append({"level": "error", "message": "Не выбрана обязательная колонка: Наименование товара."})
    if not mapping.get("qty_pallets"):
        messages.append({"level": "error", "message": "Не выбрана обязательная колонка: Количество паллет."})
    if any(item["level"] == "error" for item in messages):
        return pd.DataFrame(columns=RECEIPT_COLUMNS), build_receipt_diagnostics([], len(df), messages), messages

    rows: list[dict[str, Any]] = []
    for source_index, (_, row) in enumerate(df.iterrows(), start=1):
        source_row_number = source_index
        weight_raw, parsed_weight, weight_status, weight_reason = parse_weight_value(row.get(mapping.get("source_weight"))) if mapping.get("source_weight") else ("", None, "empty", "Колонка веса не выбрана")
        receipt = {
            "receipt_id": str(uuid.uuid4()),
            "receipt_line_id": "",
            "source_row_number": source_row_number,
            "sku_key": "",
            "receipt_date": _display_value(row.get(mapping.get("receipt_date"))) if mapping.get("receipt_date") else "",
            "receipt_number": _display_value(row.get(mapping.get("receipt_number"))) if mapping.get("receipt_number") else "",
            "receipt_document": _display_value(row.get(mapping.get("receipt_document"))) if mapping.get("receipt_document") else "",
            "warehouse": _display_value(row.get(mapping.get("warehouse"))) if mapping.get("warehouse") else "",
            "warehouse_zone": _display_value(row.get(mapping.get("warehouse_zone"))) if mapping.get("warehouse_zone") else "",
            "sku_code": _display_value(row.get(mapping.get("sku_code"))) if mapping.get("sku_code") else "",
            "sku_name": _display_value(row.get(mapping.get("sku_name"))) if mapping.get("sku_name") else "",
            "characteristic_code": _display_value(row.get(mapping.get("characteristic_code"))) if mapping.get("characteristic_code") else "",
            "characteristic_name": _display_value(row.get(mapping.get("characteristic_name"))) if mapping.get("characteristic_name") else "",
            "batch": _display_value(row.get(mapping.get("batch"))) if mapping.get("batch") else "",
            "expiry_date": _display_value(row.get(mapping.get("expiry_date"))) if mapping.get("expiry_date") else "",
            "qty_units": _safe_float(row.get(mapping.get("qty_units"))) if mapping.get("qty_units") else 0.0,
            "qty_boxes": _safe_float(row.get(mapping.get("qty_boxes"))) if mapping.get("qty_boxes") else 0.0,
            "qty_pallets": _safe_float(row.get(mapping.get("qty_pallets"))) if mapping.get("qty_pallets") else 0.0,
            "placement_status": "not_placed",
            "placement_mode": "not_calculated",
            "comment": _display_value(row.get(mapping.get("comment"))) if mapping.get("comment") else "",
            "weight_class": "unclassified",
            "source_zone": _display_value(row.get(mapping.get("source_zone"))) if mapping.get("source_zone") else (_display_value(row.get(mapping.get("weight_class"))) if mapping.get("weight_class") else ""),
            "calculated_zone": "unclassified",
            "zone_calculation_reason": "Вес отсутствует",
            "source_weight_raw": weight_raw,
            "source_weight": parsed_weight if parsed_weight is not None else "",
            "weight_parse_status": weight_status,
            "weight_parse_reason": weight_reason,
            "fragile_flag": _truthy_flag(row.get(mapping.get("fragile_flag"))) if mapping.get("fragile_flag") else False,
            "zone_calculation_status": "not_calculated",
        }
        receipt["sku_key"] = make_sku_key(receipt)
        receipt["receipt_line_id"] = make_receipt_line_id(receipt)
        rows.append(receipt)
    result = pd.DataFrame(rows, columns=RECEIPT_COLUMNS)
    diagnostics = build_receipt_diagnostics(rows, len(df), messages)
    return result, diagnostics, messages


def build_receipt_diagnostics(receipts: list[dict[str, Any]], source_rows: int, messages: list[dict[str, str]] | None = None) -> dict[str, Any]:
    messages = list(messages or [])
    sku_keys = [_display_value(item.get("sku_key")) or make_sku_key(item) for item in receipts]
    receipt_numbers_by_sku: dict[str, set[str]] = {}
    rows_by_sku: dict[str, int] = {}
    pallets_by_sku: dict[str, float] = {}
    sku_valid_weight: dict[str, bool] = {}
    sku_weight_conflicts = set()
    for item, sku_key in zip(receipts, sku_keys):
        if not sku_key:
            continue
        rows_by_sku[sku_key] = rows_by_sku.get(sku_key, 0) + 1
        pallets_by_sku[sku_key] = pallets_by_sku.get(sku_key, 0.0) + _safe_float(item.get("qty_pallets"))
        sku_valid_weight[sku_key] = sku_valid_weight.get(sku_key, False) or item.get("weight_parse_status") == "ok"
        if item.get("zone_calculation_reason") == "Конфликт данных SKU":
            sku_weight_conflicts.add(sku_key)
        if item.get("receipt_number"):
            receipt_numbers_by_sku.setdefault(sku_key, set()).add(_display_value(item.get("receipt_number")))
    dates = [item.get("receipt_date", "") for item in receipts if item.get("receipt_date")]
    expiry = [item for item in receipts if item.get("expiry_date")]
    no_qty = [item for item in receipts if _safe_float(item.get("qty_pallets")) == 0 and _safe_float(item.get("qty_boxes")) == 0 and _safe_float(item.get("qty_units")) == 0]
    negative = [item for item in receipts if _safe_float(item.get("qty_pallets")) < 0 or _safe_float(item.get("qty_boxes")) < 0 or _safe_float(item.get("qty_units")) < 0]
    zero_qty = [item for item in receipts if _safe_float(item.get("qty_pallets")) == 0 and _safe_float(item.get("qty_boxes")) == 0 and _safe_float(item.get("qty_units")) == 0]
    for item in receipts:
        if not item.get("sku_code"):
            messages.append({"level": "error", "message": "Строка без кода товара."})
        if not item.get("sku_name"):
            messages.append({"level": "warning", "message": f"SKU {item.get('sku_code') or '—'} без наименования."})
        if _safe_float(item.get("qty_pallets")) < 0 or _safe_float(item.get("qty_boxes")) < 0 or _safe_float(item.get("qty_units")) < 0:
            messages.append({"level": "error", "message": f"SKU {item.get('sku_code') or '—'}: отрицательное количество."})
        if item in no_qty:
            messages.append({"level": "warning", "message": f"SKU {item.get('sku_code') or '—'}: не заполнено количество."})
    return {
        "Всего строк в файле": source_rows,
        "Количество приходных ордеров": len({item.get("receipt_number") for item in receipts if item.get("receipt_number")}),
        "Количество строк прихода": len(receipts),
        "Всего SKU": len({sku_key for sku_key in sku_keys if sku_key}),
        "SKU в нескольких приходах": sum(1 for receipt_numbers in receipt_numbers_by_sku.values() if len(receipt_numbers) > 1),
        "Повторных строк одинакового SKU": sum(max(count - 1, 0) for count in rows_by_sku.values()),
        "Паллет по SKU": {sku_key: round(qty, 4) for sku_key, qty in pallets_by_sku.items()},
        "Строк с исходным весом": sum(1 for item in receipts if _display_value(item.get("source_weight_raw"))),
        "Вес успешно преобразован": sum(1 for item in receipts if item.get("weight_parse_status") == "ok"),
        "Пустых значений веса": sum(1 for item in receipts if item.get("weight_parse_status") == "empty"),
        "Ошибок преобразования веса": sum(1 for item in receipts if item.get("weight_parse_status") == "error"),
        "SKU без любого валидного веса": sum(1 for value in sku_valid_weight.values() if not value),
        "SKU с конфликтом веса": len(sku_weight_conflicts),
        "Всего паллет": sum(_safe_float(item.get("qty_pallets")) for item in receipts),
        "Всего коробов": sum(_safe_float(item.get("qty_boxes")) for item in receipts),
        "Строк без кода товара": sum(1 for item in receipts if not item.get("sku_code")),
        "Строк без наименования": sum(1 for item in receipts if not item.get("sku_name")),
        "Строк без количества": len(no_qty),
        "Строк с нулевым количеством": len(zero_qty),
        "Строк с отрицательным количеством": len(negative),
        "Строк без паллет, но с коробами": sum(1 for item in receipts if _safe_float(item.get("qty_pallets")) == 0 and _safe_float(item.get("qty_boxes")) > 0),
        "Строк без паллет и без коробов": sum(1 for item in receipts if _safe_float(item.get("qty_pallets")) == 0 and _safe_float(item.get("qty_boxes")) == 0),
        "Количество документов прихода": len({item.get("receipt_document") or item.get("receipt_number") for item in receipts if item.get("receipt_document") or item.get("receipt_number")}),
        "Минимальная дата прихода": min(dates) if dates else "—",
        "Максимальная дата прихода": max(dates) if dates else "—",
        "Количество строк со сроком годности": len(expiry),
        "Количество строк без срока годности": len(receipts) - len(expiry),
        "Количество строк со статусом Не размещено": sum(1 for item in receipts if item.get("placement_status") == "not_placed"),
        "messages": messages,
    }


def empty_receipts_state(model: dict[str, Any] | None = None) -> dict[str, Any]:
    now = _now_iso()
    return {
        "model_id": (model or {}).get("model_id"),
        "source_file_name": "",
        "source_file_hash": "",
        "created_at": now,
        "updated_at": now,
        "receipts": [],
        "diagnostics": build_receipt_diagnostics([], 0),
        "column_mapping": {},
        "zone_classification_settings": default_zone_classification_settings(),
        "zone_classification_diagnostics": {},
    }


def make_receipts_state(model: dict[str, Any], source_file_name: str, source_file_hash: str, receipts_df: pd.DataFrame, diagnostics: dict[str, Any], column_mapping: dict[str, str | None], zone_classification_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    now = _now_iso()
    return {
        "model_id": model.get("model_id"),
        "source_file_name": source_file_name,
        "source_file_hash": source_file_hash,
        "created_at": now,
        "updated_at": now,
        "receipts": receipts_df.to_dict("records"),
        "diagnostics": diagnostics,
        "column_mapping": column_mapping,
        "zone_classification_settings": zone_classification_settings or default_zone_classification_settings(),
        "zone_classification_diagnostics": {},
    }


def load_receipts_state(model: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
    if not RECEIPTS_PATH.exists():
        return empty_receipts_state(model), None
    try:
        state = json.loads(RECEIPTS_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return empty_receipts_state(model), "Файл receipts.json повреждён и не был загружен."
    state.setdefault("receipts", [])
    for index, receipt in enumerate(state["receipts"], start=1):
        receipt.setdefault("source_row_number", index)
        receipt.setdefault("sku_key", make_sku_key(receipt))
        receipt.setdefault("receipt_line_id", make_receipt_line_id(receipt))
        if "source_weight_raw" not in receipt:
            receipt["source_weight_raw"] = _display_value(receipt.get("source_weight"))
        if "weight_parse_status" not in receipt:
            raw, parsed, status, reason = parse_weight_value(receipt.get("source_weight"))
            receipt["source_weight_raw"] = raw
            receipt["source_weight"] = parsed if parsed is not None else ""
            receipt["weight_parse_status"] = status
            receipt["weight_parse_reason"] = reason
    state.setdefault("diagnostics", build_receipt_diagnostics(state.get("receipts", []), len(state.get("receipts", []))))
    state.setdefault("zone_classification_settings", default_zone_classification_settings())
    state.setdefault("zone_classification_diagnostics", {})
    state.setdefault("column_mapping", {})
    warning = None
    if model and state.get("model_id") and state.get("model_id") != model.get("model_id"):
        warning = "Приходы были загружены для другой версии склада. Проверьте актуальность."
    return state, warning


def save_receipts_state(state: dict[str, Any]) -> None:
    RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    RECEIPTS_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_receipts_state() -> None:
    if RECEIPTS_PATH.exists():
        RECEIPTS_PATH.unlink()


def export_receipts_excel_bytes(state: dict[str, Any]) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(state.get("receipts", [])).to_excel(writer, sheet_name="Приходы", index=False)
        diagnostics = state.get("diagnostics", {})
        pd.DataFrame([{"Показатель": key, "Значение": value} for key, value in diagnostics.items() if key != "messages"]).to_excel(writer, sheet_name="Диагностика", index=False)
        pd.DataFrame(diagnostics.get("messages", [])).to_excel(writer, sheet_name="Ошибки", index=False)
    return buffer.getvalue()
