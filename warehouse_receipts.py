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

RECEIPT_COLUMNS = [
    "receipt_id",
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
    for _, row in df.iterrows():
        receipt = {
            "receipt_id": str(uuid.uuid4()),
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
        }
        rows.append(receipt)
    result = pd.DataFrame(rows, columns=RECEIPT_COLUMNS)
    diagnostics = build_receipt_diagnostics(rows, len(df), messages)
    return result, diagnostics, messages


def build_receipt_diagnostics(receipts: list[dict[str, Any]], source_rows: int, messages: list[dict[str, str]] | None = None) -> dict[str, Any]:
    messages = list(messages or [])
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
        "Всего SKU": len({item.get("sku_code") for item in receipts if item.get("sku_code")}),
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
    }


def make_receipts_state(model: dict[str, Any], source_file_name: str, source_file_hash: str, receipts_df: pd.DataFrame, diagnostics: dict[str, Any], column_mapping: dict[str, str | None]) -> dict[str, Any]:
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
    }


def load_receipts_state(model: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
    if not RECEIPTS_PATH.exists():
        return empty_receipts_state(model), None
    try:
        state = json.loads(RECEIPTS_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return empty_receipts_state(model), "Файл receipts.json повреждён и не был загружен."
    state.setdefault("receipts", [])
    state.setdefault("diagnostics", build_receipt_diagnostics(state.get("receipts", []), len(state.get("receipts", []))))
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
