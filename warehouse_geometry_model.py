from __future__ import annotations

import html
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any

import pandas as pd

GEOMETRY_MODEL_VERSION = 1
GEOMETRY_MODEL_PATH = Path("data/last_import/warehouse_model.json")
GEOMETRY_META_PATH = Path("data/last_import/import_meta.json")
MANUAL_OVERRIDES_PATH = Path("data/last_import/manual_overrides.json")
ROW_SETTINGS_PATH = Path("data/last_import/row_settings.json")

CODE_ALIASES = ["code", "код"]
ROW_ALIASES = ["row_number", "row", "ряд", "ряд ссылка", "ряд/ссылка", "ряд / ссылка"]
CELL_ALIASES = ["cell_number", "cell", "ячейка", "ячейка ссылка", "ячейка/ссылка", "ячейка / ссылка"]
TIER_ALIASES = ["tier", "ярус", "уровень"]


@dataclass(frozen=True)
class GeometrySettings:
    cell_length_m: float = 1.2
    cell_width_m: float = 0.8
    aisle_width_m: float = 3.4
    top_road_width_m: float = 3.4
    bottom_road_width_m: float = 3.4
    pallet_height_m: float = 2.2
    selected_tier: str = "1"
    tier_mode: str = "selected"
    row_order_mode: str = "row_order_or_number"


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


def _number_key(value: Any) -> tuple[int, Any]:
    text = _display_value(value)
    try:
        return (0, int(float(text)))
    except ValueError:
        return (1, text)


def _safe_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
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


def get_excel_sheet_names(file_bytes: bytes) -> list[str]:
    with pd.ExcelFile(BytesIO(file_bytes)) as xls:
        return list(xls.sheet_names)


def read_cell_table(file_bytes: bytes, sheet_name: str, header_rows: int = 2) -> pd.DataFrame:
    header: int | list[int]
    if header_rows <= 1:
        header = 0
    else:
        header = list(range(header_rows))
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


def detect_column_mapping(df: pd.DataFrame) -> dict[str, str | None]:
    columns = [str(col) for col in df.columns]
    return {
        "code": _find_column(columns, CODE_ALIASES),
        "row_number": _find_column(columns, ROW_ALIASES),
        "cell_number": _find_column(columns, CELL_ALIASES),
        "tier": _find_column(columns, TIER_ALIASES),
    }


def normalize_cell_table(df: pd.DataFrame, mapping: dict[str, str | None]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    required = ["row_number", "cell_number"]
    missing = [key for key in required if not mapping.get(key)]
    if missing:
        diagnostics.append({"level": "error", "message": f"Не выбраны обязательные колонки: {', '.join(missing)}."})
        return pd.DataFrame(columns=["code", "row_number", "cell_number", "tier", "source_line"]), diagnostics

    result = pd.DataFrame()
    result["code"] = df[mapping["code"]].map(_display_value) if mapping.get("code") else ""
    result["row_number"] = df[mapping["row_number"]].map(_display_value)
    result["cell_number"] = df[mapping["cell_number"]].map(_display_value)
    result["tier"] = df[mapping["tier"]].map(_display_value) if mapping.get("tier") else "1"
    result["source_line"] = df.index.astype(int) + 2

    for col in ["row_number", "cell_number", "tier"]:
        empty_count = int((result[col].astype(str).str.strip() == "").sum())
        if empty_count:
            diagnostics.append({"level": "warning", "message": f"Пустых значений в колонке {col}: {empty_count}."})
    result = result[(result["row_number"] != "") & (result["cell_number"] != "")]
    return result, diagnostics


def _filter_tiers(df: pd.DataFrame, settings: GeometrySettings) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    if settings.tier_mode == "all":
        return df.copy(), diagnostics
    if settings.tier_mode == "min_per_cell":
        tmp = df.copy()
        tmp["__tier_key"] = tmp["tier"].map(_number_key)
        tmp = tmp.sort_values(["row_number", "cell_number", "__tier_key"], key=lambda col: col.map(str) if col.name != "__tier_key" else col)
        tmp = tmp.drop_duplicates(["row_number", "cell_number"], keep="first").drop(columns=["__tier_key"])
        diagnostics.append({"level": "info", "message": f"Оставлен минимальный ярус для каждой пары ряд+ячейка: {len(tmp)} строк."})
        return tmp, diagnostics
    filtered = df[df["tier"].astype(str) == str(settings.selected_tier)].copy()
    diagnostics.append({"level": "info", "message": f"Фильтр по ярусу {settings.selected_tier}: оставлено {len(filtered)} из {len(df)} строк."})
    return filtered, diagnostics


def default_row_config(df: pd.DataFrame) -> pd.DataFrame:
    rows = sorted({_display_value(v) for v in df["row_number"].dropna()}, key=_number_key)
    return pd.DataFrame(
        {
            "row_number": rows,
            "row_order": list(range(1, len(rows) + 1)),
            "row_storage_type": ["normal"] * len(rows),
            "deep_lane_width": [1] * len(rows),
            "cell_direction": ["bottom_to_top"] * len(rows),
            "row_group": [""] * len(rows),
            "side": [""] * len(rows),
            "comment": [""] * len(rows),
        }
    )


def empty_aisle_config() -> pd.DataFrame:
    return pd.DataFrame(columns=["row_from", "row_to", "aisle_width_m", "aisle_type", "comment"])


def _normalize_storage_type(value: Any) -> str:
    text = _clean_label(value)
    if text in {"deep_lane", "набивной ряд", "набивной", "deep lane"}:
        return "deep_lane"
    return "normal"


def _normalize_cell_direction(value: Any, storage_type: str = "normal") -> str:
    text = _clean_label(value)
    if text in {"top_to_bottom", "сверху вниз", "верх вниз", "top to bottom"}:
        return "top_to_bottom"
    if text in {"bottom_to_top", "снизу вверх", "низ вверх", "bottom to top"}:
        return "bottom_to_top"
    return "top_to_bottom" if storage_type == "deep_lane" else "bottom_to_top"


def _normalize_deep_lane_width(value: Any, storage_type: str = "normal") -> int:
    default = 5 if storage_type == "deep_lane" else 1
    try:
        width = int(float(_display_value(value)))
    except ValueError:
        return default
    if storage_type != "deep_lane":
        return 1
    return width if 2 <= width <= 7 else default


def _row_storage_label(storage_type: str) -> str:
    return "Набивной ряд" if storage_type == "deep_lane" else "Обычный ряд"


def _direction_label(direction: str) -> str:
    return "Сверху вниз" if direction == "top_to_bottom" else "Снизу вверх"


def _row_order_map(row_config: pd.DataFrame | None) -> dict[str, dict[str, Any]]:
    if row_config is None or row_config.empty:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for _, row in row_config.iterrows():
        row_number = _display_value(row.get("row_number"))
        if not row_number:
            continue
        storage_type = _normalize_storage_type(row.get("row_storage_type", "normal"))
        raw_deep_lane_width = row.get("deep_lane_width", "")
        deep_lane_width = _normalize_deep_lane_width(raw_deep_lane_width, storage_type)
        cell_direction = _normalize_cell_direction(row.get("cell_direction", ""), storage_type)
        deep_lane_width_invalid = False
        if storage_type == "deep_lane":
            try:
                raw_width_int = int(float(_display_value(raw_deep_lane_width)))
                deep_lane_width_invalid = raw_width_int not in range(2, 8)
            except ValueError:
                deep_lane_width_invalid = True
        result[row_number] = {
            "row_order": row.get("row_order"),
            "row_storage_type": storage_type,
            "deep_lane_width": deep_lane_width,
            "cell_direction": cell_direction,
            "deep_lane_width_invalid": deep_lane_width_invalid,
            "row_group": _display_value(row.get("row_group")),
            "side": _display_value(row.get("side")),
            "comment": _display_value(row.get("comment")),
        }
    return result


def _aisle_map(aisle_config: pd.DataFrame | None, default_width: float) -> dict[tuple[str, str], dict[str, Any]]:
    if aisle_config is None or aisle_config.empty:
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in aisle_config.iterrows():
        row_from = _display_value(row.get("row_from"))
        row_to = _display_value(row.get("row_to"))
        if not row_from or not row_to:
            continue
        result[(row_from, row_to)] = {
            "aisle_width_m": _safe_float(row.get("aisle_width_m"), default_width),
            "aisle_type": _display_value(row.get("aisle_type")) or "межрядный проезд",
            "comment": _display_value(row.get("comment")),
        }
    return result


def build_geometry_model(
    normalized_df: pd.DataFrame,
    settings: GeometrySettings,
    row_config: pd.DataFrame | None = None,
    aisle_config: pd.DataFrame | None = None,
    source_file_name: str = "",
    source_sheet_name: str = "",
    source_file_hash: str = "",
    existing_model_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = perf_counter()
    diagnostics: list[dict[str, Any]] = []
    diagnostics.append({"level": "info", "message": f"Всего строк в Excel после нормализации: {len(normalized_df)}."})
    work_df, tier_diag = _filter_tiers(normalized_df, settings)
    diagnostics.extend(tier_diag)
    duplicate_count = int(work_df.duplicated(["row_number", "cell_number", "tier"]).sum())
    if duplicate_count:
        diagnostics.append({"level": "warning", "message": f"Дубли row_number + cell_number + tier: {duplicate_count}."})
    work_df = work_df.drop_duplicates(["row_number", "cell_number", "tier"], keep="first")

    row_meta = _row_order_map(row_config)
    for row_number, meta in row_meta.items():
        if meta.get("deep_lane_width_invalid"):
            diagnostics.append({"level": "warning", "message": f"Ряд {row_number}: некорректная ширина набивного ряда; применено значение {meta.get('deep_lane_width', 5)}."})
        if meta.get("row_storage_type") == "deep_lane" and not meta.get("cell_direction"):
            diagnostics.append({"level": "warning", "message": f"Ряд {row_number}: набивной ряд без направления; применено направление сверху вниз."})
    aisle_by_pair = _aisle_map(aisle_config, settings.aisle_width_m)
    row_numbers = sorted(set(work_df["row_number"]), key=lambda row: (_safe_float(row_meta.get(row, {}).get("row_order"), 10**9), _number_key(row)))

    rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    aisles: list[dict[str, Any]] = []
    navigation_nodes: list[dict[str, Any]] = []
    navigation_edges: list[dict[str, Any]] = []
    x_cursor = 0.0
    previous_row: str | None = None
    max_row_y = 0.0

    for row_number in row_numbers:
        if previous_row is not None:
            aisle = aisle_by_pair.get((previous_row, row_number))
            if aisle:
                x_min = x_cursor
                x_max = x_min + aisle["aisle_width_m"]
                aisles.append({
                    "row_from": previous_row,
                    "row_to": row_number,
                    "aisle_width_m": aisle["aisle_width_m"],
                    "x_min": x_min,
                    "x_max": x_max,
                    "y_min": 0.0,
                    "y_max": 0.0,
                    "aisle_type": aisle["aisle_type"],
                    "comment": aisle["comment"],
                })
                x_cursor = x_max
        row_df = work_df[work_df["row_number"] == row_number].copy()
        row_df = row_df.sort_values("cell_number", key=lambda series: series.map(_number_key))
        meta = row_meta.get(row_number, {})
        storage_type = meta.get("row_storage_type", "normal")
        deep_lane_width = int(meta.get("deep_lane_width", 1))
        cell_direction = meta.get("cell_direction", "bottom_to_top")
        row_width_m = settings.cell_width_m * deep_lane_width
        row_order_value = meta.get("row_order") or len(rows) + 1
        row_x_min = x_cursor
        row_x_max = row_x_min + row_width_m
        row_x_center = (row_x_min + row_x_max) / 2
        row_cells = []
        row_count = len(row_df)
        for idx, (_, cell_row) in enumerate(row_df.iterrows()):
            position_from_bottom = row_count - 1 - idx if cell_direction == "top_to_bottom" else idx
            y_min = position_from_bottom * settings.cell_length_m
            y_max = y_min + settings.cell_length_m
            capacity_pallets = deep_lane_width if storage_type == "deep_lane" else 1
            volume_m3 = settings.cell_length_m * settings.cell_width_m * settings.pallet_height_m * capacity_pallets
            physical_slots = []
            if storage_type == "deep_lane":
                for slot_index in range(1, deep_lane_width + 1):
                    slot_x_min = row_x_min + (slot_index - 1) * settings.cell_width_m
                    physical_slots.append({
                        "slot_index": slot_index,
                        "x_min": slot_x_min,
                        "x_max": slot_x_min + settings.cell_width_m,
                        "y_min": y_min,
                        "y_max": y_max,
                        "capacity_pallets": 1,
                    })
            cell = {
                "code": _display_value(cell_row.get("code")),
                "row_number": row_number,
                "cell_number": _display_value(cell_row.get("cell_number")),
                "tier": _display_value(cell_row.get("tier")),
                "x_min": row_x_min,
                "x_max": row_x_max,
                "y_min": y_min,
                "y_max": y_max,
                "x_center": row_x_center,
                "y_center": (y_min + y_max) / 2,
                "width_m": row_width_m,
                "length_m": settings.cell_length_m,
                "source_line": int(cell_row.get("source_line", 0)),
                "source": _display_value(cell_row.get("source")) or "excel",
                "storage_type": storage_type,
                "deep_lane_width": deep_lane_width,
                "capacity_pallets": capacity_pallets,
                "volume_m3": round(volume_m3, 4),
                "cell_direction": cell_direction,
                "row_order": row_order_value,
                "physical_slots": physical_slots,
            }
            cells.append(cell)
            row_cells.append(cell)
        numeric_cells = [int(float(c["cell_number"])) for c in row_cells if re.fullmatch(r"\d+(?:\.0)?", c["cell_number"])]
        if numeric_cells:
            missing = sorted(set(range(min(numeric_cells), max(numeric_cells) + 1)) - set(numeric_cells))
            if missing:
                diagnostics.append({"level": "warning", "message": f"В ряду {row_number} есть пропуск номеров: {', '.join(map(str, missing))}."})
        row_y_max = len(row_cells) * settings.cell_length_m
        max_row_y = max(max_row_y, row_y_max)
        rows.append({
            "row_number": row_number,
            "row_order": row_order_value,
            "row_storage_type": storage_type,
            "deep_lane_width": deep_lane_width,
            "cell_direction": cell_direction,
            "row_group": meta.get("row_group", ""),
            "side": meta.get("side", ""),
            "comment": meta.get("comment", ""),
            "x_min": row_x_min,
            "x_max": row_x_max,
            "y_min": 0.0,
            "y_max": row_y_max,
            "x_center": row_x_center,
            "width_m": row_width_m,
            "cells_count": len(row_cells),
            "capacity_pallets": sum(cell["capacity_pallets"] for cell in row_cells),
        })
        bottom_node = {"node_id": f"row:{row_number}:bottom", "node_type": "row_bottom_entry", "x": row_x_center, "y": 0.0, "row_number": row_number}
        top_node = {"node_id": f"row:{row_number}:top", "node_type": "row_top_entry", "x": row_x_center, "y": row_y_max, "row_number": row_number}
        navigation_nodes.extend([bottom_node, top_node])
        navigation_edges.append({"from_node": bottom_node["node_id"], "to_node": top_node["node_id"], "distance_m": row_y_max, "edge_type": "row_walk"})
        x_cursor = row_x_max
        previous_row = row_number

    total_width = x_cursor if rows else 0.0
    roads = [
        {"road_type": "bottom", "x_min": 0.0, "x_max": total_width, "y_min": -settings.bottom_road_width_m, "y_max": 0.0, "width_m": settings.bottom_road_width_m},
        {"road_type": "top", "x_min": 0.0, "x_max": total_width, "y_min": max_row_y, "y_max": max_row_y + settings.top_road_width_m, "width_m": settings.top_road_width_m},
    ]
    navigation_nodes.extend([
        {"node_id": "road:bottom", "node_type": "bottom_road", "x": total_width / 2 if total_width else 0.0, "y": -settings.bottom_road_width_m / 2},
        {"node_id": "road:top", "node_type": "top_road", "x": total_width / 2 if total_width else 0.0, "y": max_row_y + settings.top_road_width_m / 2},
    ])
    for row in rows:
        navigation_edges.append({"from_node": "road:bottom", "to_node": f"row:{row['row_number']}:bottom", "distance_m": settings.bottom_road_width_m / 2, "edge_type": "road_to_row"})
        navigation_edges.append({"from_node": f"row:{row['row_number']}:top", "to_node": "road:top", "distance_m": settings.top_road_width_m / 2, "edge_type": "row_to_road"})

    normal_rows = [row for row in rows if row.get("row_storage_type") != "deep_lane"]
    deep_rows = [row for row in rows if row.get("row_storage_type") == "deep_lane"]
    normal_cells = [cell for cell in cells if cell.get("storage_type") != "deep_lane"]
    deep_cells = [cell for cell in cells if cell.get("storage_type") == "deep_lane"]
    deep_slots = sum(int(cell.get("capacity_pallets", 1)) for cell in deep_cells)
    total_capacity = sum(int(cell.get("capacity_pallets", 1)) for cell in cells)
    for row in deep_rows:
        diagnostics.append({"level": "info", "message": f"Ряд {row['row_number']}: логических ячеек {row['cells_count']}; набивных мест в каждой ячейке {row['deep_lane_width']}; физических паллетомест {row['capacity_pallets']}; направление {_direction_label(row['cell_direction'])}."})
    diagnostics.extend([
        {"level": "info", "message": f"Всего уникальных рядов: {len(rows)}."},
        {"level": "info", "message": f"Обычных рядов: {len(normal_rows)}."},
        {"level": "info", "message": f"Набивных рядов: {len(deep_rows)}."},
        {"level": "info", "message": f"Всего уникальных ячеек: {len(cells)}."},
        {"level": "info", "message": f"Обычных ячеек: {len(normal_cells)}."},
        {"level": "info", "message": f"Набивных логических ячеек: {len(deep_cells)}."},
        {"level": "info", "message": f"Физических паллетомест в набивных ячейках: {deep_slots}."},
        {"level": "info", "message": f"Общая вместимость в паллетах: {total_capacity}."},
        {"level": "info", "message": f"Всего ярусов в исходных данных: {normalized_df['tier'].nunique() if not normalized_df.empty else 0}."},
        {"level": "info", "message": f"Количество заданных межрядных проездов: {len(aisles)}."},
        {"level": "info", "message": f"Максимальная длина ряда: {max_row_y:.2f} м."},
        {"level": "info", "message": f"Общая ширина склада: {total_width:.2f} м."},
        {"level": "info", "message": f"Общая длина склада: {max_row_y + settings.top_road_width_m + settings.bottom_road_width_m:.2f} м."},
    ])
    model = {
        "model_type": "excel_rows_cells_aisles_geometry",
        "model_version": GEOMETRY_MODEL_VERSION,
        "model_id": existing_model_id or str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source_file_name": source_file_name,
        "source_sheet_name": source_sheet_name,
        "source_file_hash": source_file_hash,
        "settings": asdict(settings),
        "rows": rows,
        "row_settings": [
            {
                "row_number": row["row_number"],
                "row_storage_type": row.get("row_storage_type", "normal"),
                "deep_lane_width": row.get("deep_lane_width", 1),
                "cell_direction": row.get("cell_direction", "bottom_to_top"),
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "comment": row.get("comment", ""),
            }
            for row in rows
        ],
        "cells": cells,
        "base_cells": [dict(cell) for cell in cells],
        "aisles": aisles,
        "roads": roads,
        "navigation_nodes": navigation_nodes,
        "navigation_edges": navigation_edges,
        "diagnostics": diagnostics,
        "performance": {"build_geometry_seconds": perf_counter() - started},
    }
    return model, diagnostics


def save_geometry_model(model: dict[str, Any]) -> None:
    GEOMETRY_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEOMETRY_MODEL_PATH.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    GEOMETRY_META_PATH.write_text(json.dumps({"model_type": model.get("model_type"), "model_id": model.get("model_id"), "created_at": model.get("created_at"), "source_file_name": model.get("source_file_name"), "source_sheet_name": model.get("source_sheet_name"), "source_file_hash": model.get("source_file_hash")}, ensure_ascii=False, indent=2), encoding="utf-8")
    ROW_SETTINGS_PATH.write_text(json.dumps({"model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"), "rows": model.get("row_settings", [])}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_geometry_model() -> dict[str, Any] | None:
    if not GEOMETRY_MODEL_PATH.exists():
        return None
    data = json.loads(GEOMETRY_MODEL_PATH.read_text(encoding="utf-8-sig"))
    if data.get("model_type") != "excel_rows_cells_aisles_geometry":
        return None
    if "base_cells" not in data:
        data["base_cells"] = [dict(cell) for cell in data.get("cells", [])]
    overrides = load_manual_overrides()
    if overrides and overrides.get("source_model_id") == data.get("model_id"):
        data = apply_manual_overrides(data, overrides)
    return data


def cell_key(cell: dict[str, Any]) -> str:
    return f"{_display_value(cell.get('row_number'))}|{_display_value(cell.get('cell_number'))}|{_display_value(cell.get('tier'))}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_manual_overrides() -> dict[str, Any] | None:
    if not MANUAL_OVERRIDES_PATH.exists():
        return None
    try:
        return json.loads(MANUAL_OVERRIDES_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None


def save_manual_overrides(payload: dict[str, Any]) -> None:
    MANUAL_OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANUAL_OVERRIDES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def empty_manual_overrides(model: dict[str, Any]) -> dict[str, Any]:
    now = _now_iso()
    return {
        "source_model_id": model.get("model_id"),
        "source_file_hash": model.get("source_file_hash", ""),
        "source_file_name": model.get("source_file_name", ""),
        "created_at": now,
        "updated_at": now,
        "changes": [],
    }


def clear_manual_overrides() -> None:
    if MANUAL_OVERRIDES_PATH.exists():
        MANUAL_OVERRIDES_PATH.unlink()


def clear_row_settings() -> None:
    if ROW_SETTINGS_PATH.exists():
        ROW_SETTINGS_PATH.unlink()


def append_manual_change(model: dict[str, Any], change_type: str, old_value: dict[str, Any] | None, new_value: dict[str, Any] | None, comment: str = "") -> dict[str, Any]:
    overrides = load_manual_overrides()
    if not overrides or overrides.get("source_model_id") != model.get("model_id"):
        overrides = empty_manual_overrides(model)
    key_source = new_value or old_value or {}
    change = {
        "change_id": str(uuid.uuid4()),
        "change_type": change_type,
        "created_at": _now_iso(),
        "cell_key": cell_key(key_source),
        "old_value": old_value,
        "new_value": new_value,
        "comment": comment,
    }
    overrides.setdefault("changes", []).append(change)
    overrides["updated_at"] = change["created_at"]
    save_manual_overrides(overrides)
    return overrides


def manual_change_counts(overrides: dict[str, Any] | None) -> dict[str, int]:
    changes = (overrides or {}).get("changes", [])
    return {
        "total": len(changes),
        "add": sum(1 for item in changes if item.get("change_type") == "add"),
        "update": sum(1 for item in changes if item.get("change_type") == "update"),
        "delete": sum(1 for item in changes if item.get("change_type") == "delete"),
    }


def _cells_to_df(cells: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for idx, cell in enumerate(cells):
        rows.append({
            "code": _display_value(cell.get("code")),
            "row_number": _display_value(cell.get("row_number")),
            "cell_number": _display_value(cell.get("cell_number")),
            "tier": _display_value(cell.get("tier")),
            "source": _display_value(cell.get("source")) or "excel",
            "source_line": int(cell.get("source_line", idx + 2) or idx + 2),
        })
    return pd.DataFrame(rows, columns=["code", "row_number", "cell_number", "tier", "source", "source_line"])


def _row_config_from_model(model: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "row_number": row.get("row_number"),
            "row_order": row.get("row_order"),
            "row_storage_type": row.get("row_storage_type", "normal"),
            "deep_lane_width": row.get("deep_lane_width", 1),
            "cell_direction": row.get("cell_direction", "bottom_to_top"),
            "row_group": row.get("row_group", ""),
            "side": row.get("side", ""),
            "comment": row.get("comment", ""),
        }
        for row in model.get("rows", [])
    ])


def _aisle_config_from_model(model: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "row_from": aisle.get("row_from"),
            "row_to": aisle.get("row_to"),
            "aisle_width_m": aisle.get("aisle_width_m"),
            "aisle_type": aisle.get("aisle_type", "межрядный проезд"),
            "comment": aisle.get("comment", ""),
        }
        for aisle in model.get("aisles", [])
    ])


def settings_from_model(model: dict[str, Any]) -> GeometrySettings:
    raw = model.get("settings", {})
    return GeometrySettings(
        cell_length_m=_safe_float(raw.get("cell_length_m"), 1.2),
        cell_width_m=_safe_float(raw.get("cell_width_m"), 0.8),
        aisle_width_m=_safe_float(raw.get("aisle_width_m"), 3.4),
        top_road_width_m=_safe_float(raw.get("top_road_width_m"), 3.4),
        bottom_road_width_m=_safe_float(raw.get("bottom_road_width_m"), 3.4),
        pallet_height_m=_safe_float(raw.get("pallet_height_m"), 2.2),
        selected_tier=_display_value(raw.get("selected_tier")) or "1",
        tier_mode=_display_value(raw.get("tier_mode")) or "selected",
        row_order_mode=_display_value(raw.get("row_order_mode")) or "row_order_or_number",
    )


def rebuild_geometry_from_cells(model: dict[str, Any], cells: list[dict[str, Any]], keep_base_cells: bool = True) -> dict[str, Any]:
    rebuilt, _ = build_geometry_model(
        _cells_to_df(cells),
        settings_from_model(model),
        _row_config_from_model(model),
        _aisle_config_from_model(model),
        source_file_name=model.get("source_file_name", ""),
        source_sheet_name=model.get("source_sheet_name", ""),
        source_file_hash=model.get("source_file_hash", ""),
        existing_model_id=model.get("model_id"),
    )
    rebuilt["created_at"] = model.get("created_at", rebuilt.get("created_at"))
    rebuilt["manual_updated_at"] = _now_iso()
    rebuilt["base_cells"] = [dict(cell) for cell in model.get("base_cells", model.get("cells", []))] if keep_base_cells else [dict(cell) for cell in rebuilt.get("cells", [])]
    return rebuilt


def apply_manual_overrides(model: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    cells_by_key = {cell_key(cell): dict(cell) for cell in model.get("base_cells", model.get("cells", []))}
    for change in overrides.get("changes", []):
        change_type = change.get("change_type")
        old_value = change.get("old_value")
        new_value = change.get("new_value")
        if change_type == "add" and new_value:
            updated = dict(new_value)
            updated["source"] = "manual_add"
            cells_by_key[cell_key(updated)] = updated
        elif change_type == "update" and old_value and new_value:
            cells_by_key.pop(cell_key(old_value), None)
            updated = dict(new_value)
            updated["source"] = "manual_update"
            cells_by_key[cell_key(updated)] = updated
        elif change_type == "delete" and old_value:
            cells_by_key.pop(cell_key(old_value), None)
    rebuilt = rebuild_geometry_from_cells(model, list(cells_by_key.values()), keep_base_cells=True)
    rebuilt["manual_overrides_applied"] = True
    rebuilt["manual_change_counts"] = manual_change_counts(overrides)
    return rebuilt


def export_current_model_excel_bytes(model: dict[str, Any]) -> bytes:
    buffer = BytesIO()
    columns = ["code", "row_number", "cell_number", "tier", "source", "storage_type", "deep_lane_width", "capacity_pallets", "volume_m3", "cell_direction", "x_min", "x_max", "y_min", "y_max", "x_center", "y_center"]
    pd.DataFrame(model.get("cells", [])).reindex(columns=columns).to_excel(buffer, index=False)
    return buffer.getvalue()


def build_geometry_html(model: dict[str, Any], scale: float = 18.0, detailed: bool = True) -> str:
    rows = model.get("rows", [])
    cells = model.get("cells", [])
    aisles = model.get("aisles", [])
    roads = model.get("roads", [])
    max_x = max([road.get("x_max", 0) for road in roads] + [row.get("x_max", 0) for row in rows] + [1])
    min_y = min([road.get("y_min", 0) for road in roads] + [0])
    max_y = max([road.get("y_max", 0) for road in roads] + [row.get("y_max", 0) for row in rows] + [1])
    width = max(900, int(max_x * scale + 160))
    height = max(600, int((max_y - min_y) * scale + 160))
    y_offset = -min_y * scale + 40
    parts = [f"<div style='position:relative;width:{width}px;height:{height}px;background:#f8fafc;border:1px solid #cbd5e1;overflow:auto'>"]

    def rect(x_min, y_min, x_max, y_max, color, border, label, title=""):
        left = x_min * scale + 60
        top = height - (y_max * scale + y_offset)
        w = max(2, (x_max - x_min) * scale)
        h = max(2, (y_max - y_min) * scale)
        parts.append(f"<div title='{html.escape(title or label)}' style='position:absolute;left:{left:.1f}px;top:{top:.1f}px;width:{w:.1f}px;height:{h:.1f}px;background:{color};border:{border};box-sizing:border-box;font:10px Arial;text-align:center;overflow:hidden;color:#0f172a'>{html.escape(label)}</div>")

    for road in roads:
        label = "верхний проезд" if road["road_type"] == "top" else "нижний проезд"
        rect(road["x_min"], road["y_min"], road["x_max"], road["y_max"], "#dbeafe", "1px solid #60a5fa", label, f"{label}: {road['width_m']} м")
    for aisle in aisles:
        y_max = max_y - model["settings"].get("top_road_width_m", 3.4)
        rect(aisle["x_min"], 0, aisle["x_max"], y_max, "#fef3c7", "1px solid #f59e0b", "проезд", f"{aisle['row_from']} → {aisle['row_to']}: {aisle['aisle_width_m']} м")
    if detailed:
        for cell in cells:
            source_label = {"excel": "Excel", "manual_add": "добавлена вручную", "manual_update": "изменена вручную"}.get(str(cell.get("source", "excel")), str(cell.get("source", "excel")))
            storage_label = "набивная" if cell.get("storage_type") == "deep_lane" else "обычная"
            capacity = float(cell.get("capacity_pallets", 1) or 1)
            occupied = float(cell.get("occupied_capacity_pallets", 0) or 0)
            free = max(capacity - occupied, 0.0)
            occupancy_label = cell.get("occupancy_label") or (f"{occupied:g}/{capacity:g}" if occupied else "")
            placements = cell.get("placements", [])
            sku_text = ", ".join(sorted({str(item.get("sku_code", "")) for item in placements if item.get("sku_code")})) or "—"
            item_text = ", ".join(sorted({str(item.get("sku_name") or item.get("item_name") or "") for item in placements if item.get("sku_name") or item.get("item_name")})) or "—"
            placement_source = ", ".join(sorted({str(item.get("source", "")) for item in placements if item.get("source")})) or "—"
            confidence = ", ".join(sorted({str(item.get("confidence", "")) for item in placements if item.get("confidence")})) or "—"
            title = f"Код: {cell['code']}\nРяд: {cell['row_number']}\nЯчейка: {cell['cell_number']}\nЯрус: {cell['tier']}\nТип: {storage_label}\nВместимость: {capacity:g} паллет\nЗанято: {occupied:g}\nСвободно: {free:g}\nSKU: {sku_text}\nНаименование: {item_text}\nИсточник размещения: {placement_source}\nТочность: {confidence}\nФизических мест: {cell.get('deep_lane_width', 1)}\nОбъём: {cell.get('volume_m3', 0)} м³\nНаправление: {_direction_label(cell.get('cell_direction', 'bottom_to_top'))}\nX: {cell['x_center']:.2f}\nY: {cell['y_center']:.2f}\nИсточник ячейки: {source_label}"
            if occupied > capacity:
                color = "#fecaca"
                border = "2px solid #dc2626"
            elif occupied >= capacity:
                color = "#bbf7d0"
                border = "2px solid #16a34a"
            elif occupied > 0:
                color = "#fde68a"
                border = "2px solid #d97706"
            else:
                color = "#fef3c7" if cell.get("storage_type") == "deep_lane" else "#e2e8f0"
                border = "2px solid #d97706" if cell.get("storage_type") == "deep_lane" else "1px solid #64748b"
            label = f"{cell['cell_number']} {occupancy_label}" if occupancy_label else str(cell["cell_number"])
            rect(cell["x_min"], cell["y_min"], cell["x_max"], cell["y_max"], color, border, label, title)
            occupied_slots = int(min(round(occupied), len(cell.get("physical_slots", []))))
            for slot in cell.get("physical_slots", []):
                slot_color = "rgba(34,197,94,0.45)" if slot.get("slot_index", 0) <= occupied_slots else "rgba(255,255,255,0.18)"
                rect(slot["x_min"], slot["y_min"], slot["x_max"], slot["y_max"], slot_color, "1px dashed #92400e", "", f"Физическое место {slot['slot_index']} из {cell.get('deep_lane_width', 1)}")
    else:
        for row in rows:
            rect(row["x_min"], row["y_min"], row["x_max"], row["y_max"], "#e2e8f0", "1px solid #64748b", f"ряд {row['row_number']} ({row['cells_count']})")
    for row in rows:
        rect(row["x_min"], row["y_max"], row["x_max"], row["y_max"] + 0.4, "#bfdbfe", "1px solid #2563eb", f"{row['row_number']}")
    parts.append("</div>")
    return "".join(parts)
