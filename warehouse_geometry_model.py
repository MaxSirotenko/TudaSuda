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
            "weight_zone": ["unassigned"] * len(rows),
            "top_offset_cells": [0] * len(rows),
            "bottom_offset_cells": [0] * len(rows),
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
            "weight_zone": _display_value(row.get("weight_zone")) if _display_value(row.get("weight_zone")) in {"heavy", "medium", "light", "fragile", "unassigned"} else "unassigned",
            "top_offset_cells": max(0, int(_safe_float(row.get("top_offset_cells"), 0))),
            "bottom_offset_cells": max(0, int(_safe_float(row.get("bottom_offset_cells"), 0))),
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
        weight_zone = meta.get("weight_zone", "unassigned")
        initial_weight_zone = meta.get("initial_weight_zone", weight_zone)
        top_offset_cells = int(meta.get("top_offset_cells", 0) or 0)
        bottom_offset_cells = int(meta.get("bottom_offset_cells", 0) or 0)
        top_offset_m = top_offset_cells * settings.cell_length_m
        bottom_offset_m = bottom_offset_cells * settings.cell_length_m
        row_width_m = settings.cell_width_m * deep_lane_width
        row_order_value = meta.get("row_order") or len(rows) + 1
        row_x_min = x_cursor
        row_x_max = row_x_min + row_width_m
        row_x_center = (row_x_min + row_x_max) / 2
        row_cells = []
        row_count = len(row_df)
        for idx, (_, cell_row) in enumerate(row_df.iterrows()):
            position_from_bottom = row_count - 1 - idx if cell_direction == "top_to_bottom" else idx
            y_min = top_offset_m + position_from_bottom * settings.cell_length_m
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
                "weight_zone": weight_zone,
                "initial_weight_zone": initial_weight_zone,
                "physical_slots": physical_slots,
            }
            cells.append(cell)
            row_cells.append(cell)
        numeric_cells = [int(float(c["cell_number"])) for c in row_cells if re.fullmatch(r"\d+(?:\.0)?", c["cell_number"])]
        if numeric_cells:
            missing = sorted(set(range(min(numeric_cells), max(numeric_cells) + 1)) - set(numeric_cells))
            if missing:
                diagnostics.append({"level": "warning", "message": f"В ряду {row_number} есть пропуск номеров: {', '.join(map(str, missing))}."})
        row_y_max = top_offset_m + len(row_cells) * settings.cell_length_m + bottom_offset_m
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
            "weight_zone": weight_zone,
            "initial_weight_zone": initial_weight_zone,
            "top_offset_cells": top_offset_cells,
            "bottom_offset_cells": bottom_offset_cells,
            "top_offset_m": top_offset_m,
            "bottom_offset_m": bottom_offset_m,
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
    for aisle in aisles:
        aisle["y_max"] = max_row_y
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
                "weight_zone": row.get("weight_zone", "unassigned"),
                "initial_weight_zone": row.get("initial_weight_zone", row.get("weight_zone", "unassigned")),
                "top_offset_cells": row.get("top_offset_cells", 0),
                "bottom_offset_cells": row.get("bottom_offset_cells", 0),
                "top_offset_m": row.get("top_offset_m", 0.0),
                "bottom_offset_m": row.get("bottom_offset_m", 0.0),
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
    ROW_SETTINGS_PATH.write_text(json.dumps({"model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"), "rows": model.get("row_settings", []), "zone_boundary_settings": model.get("zone_boundary_settings", {})}, ensure_ascii=False, indent=2), encoding="utf-8")


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
            "weight_zone": row.get("weight_zone", "unassigned"),
            "top_offset_cells": row.get("top_offset_cells", 0),
            "bottom_offset_cells": row.get("bottom_offset_cells", 0),
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


def rebuild_geometry_from_cells(
    model: dict[str, Any],
    cells: list[dict[str, Any]],
    keep_base_cells: bool = True,
    settings: GeometrySettings | None = None,
    row_config: pd.DataFrame | None = None,
    aisle_config: pd.DataFrame | None = None,
) -> dict[str, Any]:
    rebuilt, _ = build_geometry_model(
        _cells_to_df(cells),
        settings or settings_from_model(model),
        row_config if row_config is not None else _row_config_from_model(model),
        aisle_config if aisle_config is not None else _aisle_config_from_model(model),
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


def build_geometry_html(model: dict[str, Any], scale: float = 18.0, detailed: bool = True, label_settings: dict[str, Any] | None = None) -> str:
    label_settings = label_settings or {}
    scale = float(scale) * float(label_settings.get("visual_cell_scale", 1.2) or 1.0)
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
    settings = {
        "show_row_labels": True,
        "show_cell_labels": True,
        "show_occupancy_labels": True,
        "show_aisle_labels": True,
        "label_mode": "Авто",
        "row_label_position": "авто",
    }
    default_colors = {
        "cell_color": "#DCEBFF",
        "deep_lane_cell_color": "#CFE8D5",
        "aisle_color": "#F2F2F2",
        "top_road_color": "#FFE8A3",
        "bottom_road_color": "#FFE8A3",
        "exit_color": "#FFCC80",
        "selected_cell_color": "#FF7043",
        "hover_cell_color": "#FFF59D",
        "occupied_cell_color": "#90CAF9",
        "deep_lane_partial_color": "#A5D6A7",
        "deep_lane_full_color": "#66BB6A",
    }
    category_colors = {
        "heavy": "#F4A6A6",
        "medium": "#F7D486",
        "light": "#BFE3B4",
        "fragile": "#D8B4FE",
        "unclassified": "#CBD5E1",
        "unassigned": "#E5E7EB",
    }
    settings.update(label_settings)
    colors = dict(default_colors)
    colors.update(settings.get("colors", {}))
    label_mode = str(settings.get("label_mode", "Авто"))
    edit_mode = bool(settings.get("edit_mode", False))
    selected_cell_key = str(settings.get("selected_cell_key", ""))
    selected_row_number = str(settings.get("selected_row_number", ""))
    edit_tool = str(settings.get("edit_tool", "Выбор"))
    snap_enabled = bool(settings.get("snap_enabled", True))
    snap_step = float(settings.get("snap_step", 0.1) or 0.1)
    model_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(model.get("model_id") or "warehouse"))
    root_id = f"warehouse-map-{model_id}"
    canvas_id = f"warehouse-map-canvas-{model_id}"
    zoom_label_id = f"warehouse-map-zoom-{model_id}"
    storage_key = f"warehouse-map-view:{model_id}"
    parts = [
        f"<div id='{root_id}' data-storage-key='{html.escape(storage_key, quote=True)}' "
        f"style='position:relative;width:100%;height:calc(100vh - 4px);min-height:720px;background:#F7F8FA;border:1px solid #D7DEE8;overflow:hidden;touch-action:none;user-select:none;'>"
        f"<div style='position:absolute;z-index:20;left:10px;top:10px;display:flex;gap:6px;align-items:center;"
        f"padding:6px 8px;background:rgba(255,255,255,0.92);border:1px solid #cbd5e1;border-radius:10px;"
        f"box-shadow:0 4px 14px rgba(15,23,42,0.12);font:13px Arial;color:#253247;'>"
        f"<button type='button' data-action='zoom-in' style='width:30px;height:28px;border:1px solid #94a3b8;border-radius:7px;background:#fff;cursor:pointer;font-weight:700'>+</button>"
        f"<button type='button' data-action='zoom-out' style='width:30px;height:28px;border:1px solid #94a3b8;border-radius:7px;background:#fff;cursor:pointer;font-weight:700'>−</button>"
        f"<button type='button' data-action='reset' style='height:28px;border:1px solid #94a3b8;border-radius:7px;background:#fff;cursor:pointer'>100%</button>"
        f"<button type='button' data-action='fit' style='height:28px;border:1px solid #94a3b8;border-radius:7px;background:#fff;cursor:pointer'>Весь склад</button>"
        f"<span id='{zoom_label_id}' style='min-width:48px;text-align:right;font-weight:700'>100%</span>"
        f"</div>"
        f"<div id='{canvas_id}' style='position:absolute;left:0;top:0;width:{width}px;height:{height}px;"
        f"transform-origin:0 0;will-change:transform;cursor:grab;'>"
    ]

    def label_font_size(label_lines, w, h) -> int:
        lines = [str(line) for line in label_lines if str(line)]
        if not lines:
            return 0
        max_len = max(len(line) for line in lines)
        width_size = int((w - 4) / max(max_len * 0.58, 1))
        height_size = int((h - 4) / max(len(lines) * 1.15, 1))
        size = min(12, width_size, height_size)
        return size if size >= 4 else 0

    def fit_label(full_lines, short_lines, w, h, mode: str | None = None):
        mode = mode or label_mode
        if mode == "Только при наведении":
            return [], 0
        candidates = []
        if mode == "Короткие":
            candidates = [short_lines]
        elif mode == "Полные":
            candidates = [full_lines, short_lines]
        else:
            candidates = [full_lines, short_lines]
        for candidate in candidates:
            lines = [str(line) for line in candidate if str(line)]
            size = label_font_size(lines, w, h)
            if size:
                return lines, size
        return [], 0

    def rect(x_min, y_min, x_max, y_max, color, border, label="", title="", short_label="", label_lines=None, short_lines=None, force_label=False, vertical=False, hover_color="", extra_attrs="", extra_style=""):
        left = x_min * scale + 60
        top = height - (y_max * scale + y_offset)
        w = max(2, (x_max - x_min) * scale)
        h = max(2, (y_max - y_min) * scale)
        full = label_lines if label_lines is not None else ([label] if label else [])
        short = short_lines if short_lines is not None else ([short_label or label] if (short_label or label) else [])
        lines, font_size = fit_label(full, short, w, h, "Полные" if force_label else None)
        content = ""
        if lines:
            line_height = max(font_size + 1, int(font_size * 1.15))
            if vertical and h > w and len(lines) == 1:
                content = f"<span style='display:inline-block;transform:rotate(-90deg);white-space:nowrap'>{html.escape(lines[0])}</span>"
            else:
                content = "<br>".join(html.escape(line) for line in lines)
            content_style = f"display:flex;align-items:center;justify-content:center;width:100%;height:100%;padding:1px;box-sizing:border-box;font:{font_size}px/{line_height}px Arial;text-align:center;overflow:hidden;color:#253247;"
        else:
            content_style = "display:block;width:100%;height:100%;overflow:hidden;"
        hover_attrs = ""
        if hover_color:
            safe_hover = html.escape(str(hover_color), quote=True)
            safe_color = html.escape(str(color), quote=True)
            hover_attrs = f" onmouseenter=\"this.dataset.bg=this.style.background;this.style.background=\'{safe_hover}\'\" onmouseleave=\"this.style.background=this.dataset.bg||\'{safe_color}\'\""
        parts.append(f"<div title='{html.escape(title or label)}'{hover_attrs}{extra_attrs} style='position:absolute;left:{left:.1f}px;top:{top:.1f}px;width:{w:.1f}px;height:{h:.1f}px;background:{color};border:{border};box-sizing:border-box;overflow:hidden;clip-path:inset(0);{extra_style}'><div style='{content_style}'>{content}</div></div>")

    def _placement_display_name(placement: dict[str, Any]) -> str:
        return str(placement.get("sku_name") or placement.get("item_name") or placement.get("sku_code") or "").strip()

    def _cell_sku_label(placements: list[dict[str, Any]]) -> str:
        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for placement in placements:
            name = _placement_display_name(placement)
            if not name:
                continue
            key = str(placement.get("sku_key") or placement.get("sku_code") or name).strip()
            if key in seen:
                continue
            seen.add(key)
            unique.append((key, name))
        if not unique:
            return ""
        first_name = unique[0][1]
        return f"{first_name} +{len(unique) - 1}" if len(unique) > 1 else first_name

    def _ellipsis(text: str, max_chars: int) -> str:
        text = " ".join(str(text).split())
        if max_chars <= 1:
            return "…" if text else ""
        if len(text) <= max_chars:
            return text
        suffix_match = re.search(r"\s\+\d+$", text)
        if suffix_match:
            suffix = suffix_match.group(0)
            if max_chars <= len(suffix) + 2:
                return "…" + suffix
            head_limit = max_chars - len(suffix) - 1
            return text[:head_limit].rstrip() + "…" + suffix
        return text[: max_chars - 1].rstrip() + "…"

    def cell_label_overlay(cell: dict[str, Any], occupied: float, placements: list[dict[str, Any]]) -> None:
        if not settings.get("show_cell_labels", True):
            return
        left = cell["x_min"] * scale + 60
        top = height - (cell["y_max"] * scale + y_offset)
        w = max(2, (cell["x_max"] - cell["x_min"]) * scale)
        h = max(2, (cell["y_max"] - cell["y_min"]) * scale)
        number = str(cell.get("cell_number", ""))
        lines = [number] if number else []
        if occupied > 0:
            name = _cell_sku_label(placements)
            if name:
                max_chars = max(4, int((w - 6) / 5.6))
                lines.append(_ellipsis(name, max_chars))
        if not lines:
            return
        longest = max(len(line) for line in lines)
        width_size = int((w - 6) / max(longest * 0.55, 1))
        height_size = int((h - 4) / max(len(lines) * 1.1, 1))
        font_size = max(7, min(12, width_size, height_size))
        line_height = max(font_size + 1, int(font_size * 1.15))
        safe_lines = "<br>".join(html.escape(line) for line in lines)
        parts.append(
            f"<div aria-hidden='true' style='position:absolute;left:{left:.1f}px;top:{top:.1f}px;width:{w:.1f}px;height:{h:.1f}px;"
            f"display:flex;align-items:center;justify-content:center;padding:2px;box-sizing:border-box;overflow:hidden;"
            f"font:600 {font_size}px/{line_height}px Arial;text-align:center;color:#1f2937;text-shadow:0 1px 1px rgba(255,255,255,0.72);"
            f"white-space:normal;pointer-events:none;z-index:8;'><div style='max-width:100%;overflow:hidden;text-overflow:ellipsis;'>{safe_lines}</div></div>"
        )

    for road in roads:
        label = "верхний проезд" if road["road_type"] == "top" else "нижний проезд"
        road_label = label if settings.get("show_aisle_labels", True) else ""
        road_color = colors["top_road_color"] if road["road_type"] == "top" else colors["bottom_road_color"]
        if road.get("road_type") == "exit":
            road_color = colors["exit_color"]
        rect(road["x_min"], road["y_min"], road["x_max"], road["y_max"], road_color, "1px solid #C9D2E0", road_label, f"{label}: {road['width_m']} м", short_label="проезд")
    for aisle in aisles:
        y_max = max_y - model["settings"].get("top_road_width_m", 3.4)
        aisle_label = "проезд" if settings.get("show_aisle_labels", True) else ""
        rect(aisle["x_min"], 0, aisle["x_max"], y_max, colors["aisle_color"], "1px solid #D5DAE2", aisle_label, f"{aisle['row_from']} → {aisle['row_to']}: {aisle['aisle_width_m']} м", short_label="↕")
    def row_title(row: dict[str, Any]) -> str:
        return (
            f"Ряд {row.get('row_number', '')}\n"
            f"Отступ сверху: {row.get('top_offset_cells', 0)} яч. / {float(row.get('top_offset_m', 0) or 0):g} м\n"
            f"Отступ снизу: {row.get('bottom_offset_cells', 0)} яч. / {float(row.get('bottom_offset_m', 0) or 0):g} м"
        )

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
            if cell.get("placement_tooltip"):
                title = str(cell.get("placement_tooltip"))
            current_cell_key = f"{cell.get('row_number')}|{cell.get('cell_number')}|{cell.get('tier') or '1'}"
            if occupied > capacity:
                color = "#fecaca"
                border = "2px solid #DC5A5A"
            elif occupied >= capacity:
                color = category_colors.get(str(cell.get("placement_category", "")), colors["deep_lane_full_color"] if cell.get("storage_type") == "deep_lane" else colors["occupied_cell_color"])
                border = "2px solid #4F8F5B"
            elif occupied > 0:
                color = category_colors.get(str(cell.get("placement_category", "")), colors["deep_lane_partial_color"] if cell.get("storage_type") == "deep_lane" else colors["occupied_cell_color"])
                border = "2px solid #82A878"
            else:
                color = colors["deep_lane_cell_color"] if cell.get("storage_type") == "deep_lane" else colors["cell_color"]
                border = "1px solid #8FB39A" if cell.get("storage_type") == "deep_lane" else "1px solid #AAB4C3"
            if current_cell_key == selected_cell_key:
                color = colors["selected_cell_color"]
                border = "2px solid #E5532D"
            cell_attrs = f" data-edit-select='cell' data-cell-key='{html.escape(current_cell_key, quote=True)}' data-row-number='{html.escape(str(cell.get('row_number')), quote=True)}'" if edit_mode else ""
            rect(cell["x_min"], cell["y_min"], cell["x_max"], cell["y_max"], color, border, "", title, hover_color=colors["hover_cell_color"], extra_attrs=cell_attrs)
            occupied_slots = int(min(round(occupied), len(cell.get("physical_slots", []))))
            for slot in cell.get("physical_slots", []):
                slot_color = "rgba(34,197,94,0.45)" if slot.get("slot_index", 0) <= occupied_slots else "rgba(255,255,255,0.18)"
                rect(slot["x_min"], slot["y_min"], slot["x_max"], slot["y_max"], slot_color, "1px dashed #93A4B8", "", title, extra_style="pointer-events:none;")
            cell_label_overlay(cell, occupied, placements)
    else:
        for row in rows:
            row_label = f"ряд {row['row_number']} ({row['cells_count']})" if settings.get("show_row_labels", True) else ""
            row_color = colors["selected_cell_color"] if str(row.get("row_number", "")) == selected_row_number else colors["cell_color"]
            row_attrs = f" data-edit-select='row' data-row-number='{html.escape(str(row.get('row_number', '')), quote=True)}'" if edit_mode else ""
            rect(row["x_min"], row["y_min"], row["x_max"], row["y_max"], row_color, "1px solid #AAB4C3", row_label, row_title(row), short_label=str(row.get("row_number", "")), vertical=True, extra_attrs=row_attrs)
    if settings.get("show_row_labels", True):
        row_position = str(settings.get("row_label_position", "авто"))
        for idx, row in enumerate(rows):
            if label_mode == "Авто" and len(rows) > 60 and idx % 2:
                continue
            row_label = str(row["row_number"])
            positions = ["top", "bottom"] if row_position == "сверху и снизу" else (["bottom"] if row_position == "снизу" else ["top"])
            row_height = max(float(row.get("y_max", 0)) - float(row.get("y_min", 0)), 0.01)
            row_width = max(float(row.get("x_max", 0)) - float(row.get("x_min", 0)), 0.01)
            badge_height = min(0.55, max(row_height * 0.18, 0.24))
            badge_margin_y = min(0.08, max(row_height * 0.03, 0.02))
            badge_margin_x = min(0.06, max(row_width * 0.05, 0.01))
            x_min = float(row["x_min"]) + badge_margin_x
            x_max = float(row["x_max"]) - badge_margin_x
            if x_max <= x_min:
                x_min, x_max = float(row["x_min"]), float(row["x_max"])
            label_color = colors["selected_cell_color"] if row_label == selected_row_number else "rgba(255,255,255,0.86)"
            label_border = "2px solid #E5532D" if row_label == selected_row_number else "1px solid #AAB4C3"
            row_attrs = f" data-edit-select='row' data-row-number='{html.escape(row_label, quote=True)}'" if edit_mode else ""
            for position in positions:
                if position == "bottom":
                    y_min = float(row["y_min"]) + badge_margin_y
                    y_max = min(y_min + badge_height, float(row["y_max"]))
                else:
                    y_max = float(row["y_max"]) - badge_margin_y
                    y_min = max(float(row["y_min"]), y_max - badge_height)
                if y_max > y_min:
                    rect(x_min, y_min, x_max, y_max, label_color, label_border, row_label, row_title(row), short_label=row_label, force_label=True, vertical=True, extra_attrs=row_attrs)
    parts.append("</div>")
    script = f"""
<script>
(function() {{
  const root = document.getElementById({json.dumps(root_id)});
  const canvas = document.getElementById({json.dumps(canvas_id)});
  const zoomLabel = document.getElementById({json.dumps(zoom_label_id)});
  if (!root || !canvas || root.dataset.navReady === '1') return;
  root.dataset.navReady = '1';
  const minScale = 0.25;
  const maxScale = 8;
  const buttonFactor = 1.2;
  const storageKey = root.dataset.storageKey || {json.dumps(storage_key)};
  const editTool = {json.dumps(edit_tool)};
  const snapEnabled = {json.dumps(snap_enabled)};
  const snapStep = {json.dumps(snap_step)};
  let spacePressed = false;
  const base = {{ scale: 1, x: 0, y: 0 }};
  const view = {{ scale: 1, x: 0, y: 0 }};

  function clamp(value, min, max) {{ return Math.max(min, Math.min(max, value)); }}
  function viewportSize() {{
    return {{ width: Math.max(root.clientWidth, 1), height: Math.max(root.clientHeight, 1) }};
  }}
  function canvasSize() {{
    return {{ width: Math.max(canvas.offsetWidth, 1), height: Math.max(canvas.offsetHeight, 1) }};
  }}
  function saveView() {{
    try {{ localStorage.setItem(storageKey, JSON.stringify(view)); }} catch (err) {{}}
  }}
  function applyView(shouldSave = true) {{
    view.scale = clamp(view.scale, minScale, maxScale);
    canvas.style.transform = `translate(${{view.x}}px, ${{view.y}}px) scale(${{view.scale}})`;
    if (zoomLabel) zoomLabel.textContent = `${{Math.round(view.scale * 100)}}%`;
    if (shouldSave) saveView();
  }}
  function centerAt(scale) {{
    const vp = viewportSize();
    const cs = canvasSize();
    view.scale = clamp(scale, minScale, maxScale);
    view.x = (vp.width - cs.width * view.scale) / 2;
    view.y = (vp.height - cs.height * view.scale) / 2;
    applyView();
  }}
  function fitAll() {{
    const vp = viewportSize();
    const cs = canvasSize();
    const padding = 24;
    const sx = (vp.width - padding * 2) / cs.width;
    const sy = (vp.height - padding * 2) / cs.height;
    const nextScale = clamp(Math.min(sx, sy), minScale, maxScale);
    view.scale = nextScale;
    view.x = (vp.width - cs.width * nextScale) / 2;
    view.y = (vp.height - cs.height * nextScale) / 2;
    applyView();
  }}
  function reset100() {{ centerAt(base.scale); }}
  function zoomAt(clientX, clientY, factor) {{
    const rect = root.getBoundingClientRect();
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    const oldScale = view.scale;
    const nextScale = clamp(oldScale * factor, minScale, maxScale);
    if (nextScale === oldScale) return;
    const worldX = (px - view.x) / oldScale;
    const worldY = (py - view.y) / oldScale;
    view.scale = nextScale;
    view.x = px - worldX * nextScale;
    view.y = py - worldY * nextScale;
    applyView();
  }}
  function zoomCenter(factor) {{
    const rect = root.getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
  }}

  root.addEventListener('click', function(event) {{
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    event.preventDefault();
    const action = button.dataset.action;
    if (action === 'zoom-in') zoomCenter(buttonFactor);
    if (action === 'zoom-out') zoomCenter(1 / buttonFactor);
    if (action === 'reset') reset100();
    if (action === 'fit') fitAll();
  }});
  root.addEventListener('wheel', function(event) {{
    event.preventDefault();
    const factor = event.deltaY < 0 ? buttonFactor : 1 / buttonFactor;
    zoomAt(event.clientX, event.clientY, factor);
  }}, {{ passive: false }});

  let dragging = false;
  let movingRow = null;
  let selectingBox = null;
  let dragStart = {{ x: 0, y: 0, viewX: 0, viewY: 0 }};
  window.addEventListener('keydown', function(event) {{ if (event.code === 'Space') spacePressed = true; if (event.code === 'Escape') cancelTransient(); }});
  window.addEventListener('keyup', function(event) {{ if (event.code === 'Space') spacePressed = false; }});
  function rowElements(rowNumber) {{ return Array.from(root.querySelectorAll(`[data-row-number="${{CSS.escape(rowNumber)}}"]`)); }}
  function snappedMeters(pixelDelta) {{
    const meters = pixelDelta / (view.scale * {scale});
    const snapped = snapEnabled && snapStep > 0 ? Math.round(meters / snapStep) * snapStep : meters;
    return snapped * view.scale * {scale};
  }}
  function cancelTransient() {{
    if (movingRow) {{ movingRow.items.forEach(function(item) {{ item.style.transform = ''; item.style.opacity = ''; }}); movingRow = null; }}
    if (selectingBox) {{ selectingBox.el.remove(); selectingBox = null; }}
  }}
  root.addEventListener('pointerdown', function(event) {{
    if (event.target.closest('button')) return;
    const editTarget = event.target.closest('[data-edit-select]');
    if (!spacePressed && editTool === 'Перемещение' && editTarget && editTarget.dataset.rowNumber) {{
      movingRow = {{ row: editTarget.dataset.rowNumber, x: event.clientX, y: event.clientY, items: rowElements(editTarget.dataset.rowNumber) }};
      movingRow.items.forEach(function(item) {{ item.style.opacity = '0.58'; }});
      root.setPointerCapture(event.pointerId);
      return;
    }}
    if (!spacePressed && editTool === 'Выделение рамкой') {{
      const rect = root.getBoundingClientRect();
      const el = document.createElement('div');
      el.style.cssText = 'position:absolute;z-index:25;border:1px dashed #2563eb;background:rgba(37,99,235,0.12);pointer-events:none;';
      root.appendChild(el);
      selectingBox = {{ el, x: event.clientX - rect.left, y: event.clientY - rect.top, shift: event.shiftKey, ctrl: event.ctrlKey }};
      root.setPointerCapture(event.pointerId);
      return;
    }}
    dragging = true;
    dragStart = {{ x: event.clientX, y: event.clientY, viewX: view.x, viewY: view.y }};
    canvas.style.cursor = 'grabbing';
    root.setPointerCapture(event.pointerId);
  }});
  root.addEventListener('pointermove', function(event) {{
    if (movingRow) {{
      const dx = snappedMeters(event.clientX - movingRow.x);
      const dy = snappedMeters(event.clientY - movingRow.y);
      movingRow.items.forEach(function(item) {{ item.style.transform = `translate(${{dx}}px, ${{dy}}px)`; }});
      return;
    }}
    if (selectingBox) {{
      const rect = root.getBoundingClientRect();
      const x2 = event.clientX - rect.left;
      const y2 = event.clientY - rect.top;
      const left = Math.min(selectingBox.x, x2);
      const top = Math.min(selectingBox.y, y2);
      selectingBox.el.style.left = `${{left}}px`;
      selectingBox.el.style.top = `${{top}}px`;
      selectingBox.el.style.width = `${{Math.abs(x2 - selectingBox.x)}}px`;
      selectingBox.el.style.height = `${{Math.abs(y2 - selectingBox.y)}}px`;
      return;
    }}
    if (!dragging) return;
    view.x = dragStart.viewX + event.clientX - dragStart.x;
    view.y = dragStart.viewY + event.clientY - dragStart.y;
    applyView();
  }});
  function stopDrag(event) {{
    if (movingRow) {{
      try {{ localStorage.setItem(storageKey + ':row-preview:' + movingRow.row, JSON.stringify({{ row: movingRow.row }})); }} catch (err) {{}}
      movingRow.items.forEach(function(item) {{ item.style.opacity = ''; }});
      movingRow = null;
      try {{ root.releasePointerCapture(event.pointerId); }} catch (err) {{}}
      return;
    }}
    if (selectingBox) {{
      const box = selectingBox.el.getBoundingClientRect();
      if (!selectingBox.shift && !selectingBox.ctrl) root.querySelectorAll('[data-edit-selected="1"]').forEach(function(item) {{ item.dataset.editSelected = '0'; item.style.outline = ''; item.style.outlineOffset = ''; }});
      root.querySelectorAll('[data-edit-select]').forEach(function(item) {{
        const r = item.getBoundingClientRect();
        const hit = r.left < box.right && r.right > box.left && r.top < box.bottom && r.bottom > box.top;
        if (!hit) return;
        if (selectingBox.ctrl) {{ item.dataset.editSelected = '0'; item.style.outline = ''; item.style.outlineOffset = ''; }}
        else {{ item.dataset.editSelected = '1'; item.style.outline = '3px solid #ff7043'; item.style.outlineOffset = '2px'; }}
      }});
      selectingBox.el.remove(); selectingBox = null;
      try {{ root.releasePointerCapture(event.pointerId); }} catch (err) {{}}
      return;
    }}
    if (!dragging) return;
    dragging = false;
    canvas.style.cursor = 'grab';
    try {{ root.releasePointerCapture(event.pointerId); }} catch (err) {{}}
    saveView();
  }}
  root.addEventListener('pointerup', stopDrag);
  root.addEventListener('pointercancel', stopDrag);
  root.addEventListener('dblclick', function(event) {{
    if (event.target.closest('button')) return;
    event.preventDefault();
    fitAll();
  }});
  root.addEventListener('click', function(event) {{
    const target = event.target.closest('[data-edit-select]');
    if (!target) return;
    root.querySelectorAll('[data-edit-selected="1"]').forEach(function(item) {{
      item.dataset.editSelected = '0';
      item.style.outline = '';
      item.style.outlineOffset = '';
    }});
    target.dataset.editSelected = '1';
    target.style.outline = '3px solid #ff7043';
    target.style.outlineOffset = '2px';
    try {{ localStorage.setItem(storageKey + ':selected', JSON.stringify(target.dataset)); }} catch (err) {{}}
  }});

  let restored = false;
  try {{
    const stored = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (stored && Number.isFinite(stored.scale) && Number.isFinite(stored.x) && Number.isFinite(stored.y)) {{
      view.scale = clamp(stored.scale, minScale, maxScale);
      view.x = stored.x;
      view.y = stored.y;
      restored = true;
    }}
  }} catch (err) {{}}
  if (restored) applyView(false); else fitAll();
  window.addEventListener('resize', function() {{ applyView(false); }});
}})();
</script>
"""
    parts.append(f"</div>{script}")
    return "".join(parts)
