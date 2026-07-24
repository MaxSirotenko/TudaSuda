from __future__ import annotations

import copy
import math
from typing import Any

import pandas as pd

VALID_DIRECTIONS = {"bottom_to_top", "top_to_bottom"}
VALID_STORAGE_TYPES = {"normal", "deep_lane"}
VALID_ZONES = {"heavy", "medium", "light", "fragile", "unassigned"}
SYNC_FIELDS = ["row_number", "row_order", "cell_direction", "weight_zone", "initial_weight_zone", "row_storage_type", "deep_lane_width", "capacity_pallets", "row_group", "side", "comment", "base_cell_width_m", "base_row_width_m", "top_offset_cells", "bottom_offset_cells", "top_offset_m", "bottom_offset_m"]
CELL_SYNC_FIELDS = ["row_order", "cell_direction", "weight_zone", "row_group", "side", "comment"]


def _display(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        result = float(str(value).replace(",", "."))
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


OFFSET_FIELDS = {"top_offset_cells", "bottom_offset_cells"}
DISPLAY_FIELDS = {"row_group", "side", "comment"}


def _offset_cells(value: Any) -> int:
    """Return the effective integer offset used by row geometry."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    try:
        if pd.isna(value):
            return 0
    except (TypeError, ValueError):
        pass
    return int(_float(value, 0))


def _comparison_value(field: str, value: Any) -> Any:
    if field in OFFSET_FIELDS:
        return _offset_cells(value)
    if field in DISPLAY_FIELDS:
        return _display(value)
    return value


def _effective_row(row: dict[str, Any]) -> dict[str, Any]:
    return {field: _comparison_value(field, value) for field, value in row.items()}


def _cell_key(cell: dict[str, Any]) -> str:
    return f"{_display(cell.get('row_number'))}|{_display(cell.get('cell_number'))}|{_display(cell.get('tier')) or '1'}"


def _row_fallback(model: dict[str, Any], row_number: str, field: str, default: Any = "") -> Any:
    for row in model.get("row_settings", []):
        if _display(row.get("row_number")) == row_number and row.get(field) not in {None, ""}:
            return row.get(field)
    for cell in model.get("cells", []):
        if _display(cell.get("row_number")) == row_number and cell.get(field) not in {None, ""}:
            return cell.get(field)
    return default


def build_row_settings_draft(model: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for row in sorted(model.get("rows", []), key=lambda item: _float(item.get("row_order"), 10**9)):
        row_number = _display(row.get("row_number"))
        row_cells = [cell for cell in model.get("cells", []) if _display(cell.get("row_number")) == row_number]
        storage = row.get("row_storage_type") or _row_fallback(model, row_number, "row_storage_type", "normal")
        deep_width = int(_float(row.get("deep_lane_width") or _row_fallback(model, row_number, "deep_lane_width", 1), 1))
        cell_capacity = deep_width if storage == "deep_lane" else 1
        rows.append({
            "row_number": row_number,
            "row_order": _float(row.get("row_order") or _row_fallback(model, row_number, "row_order", len(rows) + 1), len(rows) + 1),
            "cell_direction": row.get("cell_direction") or _row_fallback(model, row_number, "cell_direction", "bottom_to_top"),
            "weight_zone": row.get("weight_zone") or _row_fallback(model, row_number, "weight_zone", "unassigned"),
            "row_storage_type": storage,
            "cell_capacity_pallets": cell_capacity,
            "cells_count": len(row_cells) or int(_float(row.get("cells_count"), 0)),
            "row_capacity_pallets": (len(row_cells) or int(_float(row.get("cells_count"), 0))) * cell_capacity,
            "top_offset_cells": _offset_cells(row.get("top_offset_cells") if row.get("top_offset_cells") not in {None, ""} else _row_fallback(model, row_number, "top_offset_cells", 0)),
            "bottom_offset_cells": _offset_cells(row.get("bottom_offset_cells") if row.get("bottom_offset_cells") not in {None, ""} else _row_fallback(model, row_number, "bottom_offset_cells", 0)),
            "row_group": row.get("row_group") or _row_fallback(model, row_number, "row_group", ""),
            "side": row.get("side") or _row_fallback(model, row_number, "side", ""),
            "comment": row.get("comment") or _row_fallback(model, row_number, "comment", ""),
        })
    return pd.DataFrame(rows)


def create_row_settings_state(model: dict[str, Any]) -> dict[str, Any]:
    """Create an isolated, JSON-serializable editor state from the saved model."""
    records = build_row_settings_draft(model).to_dict(orient="records")
    return {
        "model_id": str(model.get("model_id") or model.get("source_file_hash") or "active"),
        "baseline": copy.deepcopy(records),
        "draft": copy.deepcopy(records),
        "editor_revision": 0,
    }


def update_row_settings_state(state: dict[str, Any], edited_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the draft with submitted editor values without touching the model."""
    updated = copy.deepcopy(state)
    updated["draft"] = copy.deepcopy(edited_rows)
    return updated


def reset_row_settings_state(state: dict[str, Any]) -> dict[str, Any]:
    """Discard draft edits while retaining the state scope for the active model."""
    reset = copy.deepcopy(state)
    reset["draft"] = copy.deepcopy(reset.get("baseline", []))
    reset["editor_revision"] = int(reset.get("editor_revision", 0)) + 1
    return reset


def changed_row_numbers(state: dict[str, Any]) -> list[str]:
    """Return row numbers whose submitted draft differs from its baseline."""
    baseline = {_display(row.get("row_number")): row for row in state.get("baseline", [])}
    changed = []
    for row in state.get("draft", []):
        row_number = _display(row.get("row_number"))
        baseline_row = baseline.get(row_number)
        if baseline_row is None or _effective_row(row) != _effective_row(baseline_row):
            changed.append(row_number)
    return changed


def _base_cell_width(model: dict[str, Any], row: dict[str, Any], cells: list[dict[str, Any]]) -> float:
    settings_width = _float((model.get("settings") or {}).get("cell_width_m"), 0.0)
    if row.get("base_cell_width_m"):
        return _float(row.get("base_cell_width_m"), settings_width or 1.0)
    for cell in cells:
        if cell.get("base_cell_width_m"):
            return _float(cell.get("base_cell_width_m"), settings_width or 1.0)
    if settings_width > 0:
        return settings_width
    if cells:
        width = _float(cells[0].get("x_max")) - _float(cells[0].get("x_min"))
        lane = int(_float(cells[0].get("deep_lane_width"), 1)) if cells[0].get("storage_type") == "deep_lane" else 1
        return width / max(lane, 1) if width > 0 else 1.0
    return 1.0


def _physical_slots(cell: dict[str, Any], capacity: int) -> list[dict[str, Any]]:
    if capacity <= 1:
        return []
    x_min = _float(cell.get("x_min"))
    x_max = _float(cell.get("x_max"), x_min)
    y_min = _float(cell.get("y_min"))
    y_max = _float(cell.get("y_max"), y_min)
    slot_width = (x_max - x_min) / capacity
    return [{"slot_index": idx, "x_min": x_min + (idx - 1) * slot_width, "x_max": x_min + idx * slot_width, "y_min": y_min, "y_max": y_max, "capacity_pallets": 1} for idx in range(1, capacity + 1)]




def _row_sort_key(row: dict[str, Any]) -> tuple[float, str]:
    return (_float(row.get("row_order"), 10**9), _display(row.get("row_number")))


def _cell_sort_key(cell: dict[str, Any]) -> tuple[float, str]:
    return (_float(cell.get("y_min"), 10**9), _display(cell.get("cell_number")))




def _cell_number_sort_key(cell: dict[str, Any]) -> tuple[float, str]:
    return (_float(cell.get("cell_number"), 10**9), _display(cell.get("cell_number")))


def _cell_length(model: dict[str, Any], cells: list[dict[str, Any]]) -> float:
    settings_length = _float((model.get("settings") or {}).get("cell_length_m"), 0.0)
    for cell in cells:
        length = _float(cell.get("length_m"), 0.0)
        if length > 0:
            return length
        diff = _float(cell.get("y_max"), 0.0) - _float(cell.get("y_min"), 0.0)
        if diff > 0:
            return diff
    return settings_length or 1.0


def _apply_row_y_layout(model: dict[str, Any], row: dict[str, Any], cells: list[dict[str, Any]]) -> None:
    ordered_cells = sorted(cells, key=_cell_number_sort_key)
    if not ordered_cells:
        row["y_min"] = 0.0
        row["y_max"] = 0.0
        return
    length = _cell_length(model, ordered_cells)
    top_offset_cells = int(_float(row.get("top_offset_cells"), 0))
    bottom_offset_cells = int(_float(row.get("bottom_offset_cells"), 0))
    top_offset_m = top_offset_cells * length
    bottom_offset_m = bottom_offset_cells * length
    row.update({"top_offset_cells": top_offset_cells, "bottom_offset_cells": bottom_offset_cells, "top_offset_m": top_offset_m, "bottom_offset_m": bottom_offset_m})
    direction = row.get("cell_direction", "bottom_to_top")
    count = len(ordered_cells)
    for idx, cell in enumerate(ordered_cells):
        position_from_bottom = count - 1 - idx if direction == "top_to_bottom" else idx
        y_min = top_offset_m + position_from_bottom * length
        y_max = y_min + length
        cell.update({"y_min": y_min, "y_max": y_max, "y_center": (y_min + y_max) / 2, "length_m": length})
    row["y_min"] = 0.0
    row["y_max"] = top_offset_m + count * length + bottom_offset_m


def _aisle_width(aisle: dict[str, Any]) -> float:
    width = _float(aisle.get("aisle_width_m"), 0.0)
    if width <= 0:
        width = _float(aisle.get("x_max"), 0.0) - _float(aisle.get("x_min"), 0.0)
    return max(width, 0.0)


def _relayout_rows_with_aisles(model: dict[str, Any], row_widths: dict[str, float]) -> None:
    """Recalculate row X coordinates with the same x_cursor layout used by geometry build."""
    ordered_rows = sorted(model.get("rows", []), key=_row_sort_key)
    aisles_by_pair = {
        (_display(aisle.get("row_from")), _display(aisle.get("row_to"))): aisle
        for aisle in model.get("aisles", [])
    }
    relaid_aisles: list[dict[str, Any]] = []
    x_cursor = 0.0
    previous_row_number = ""
    for row in ordered_rows:
        row_number = _display(row.get("row_number"))
        if previous_row_number:
            aisle = aisles_by_pair.get((previous_row_number, row_number))
            if aisle is not None:
                width = _aisle_width(aisle)
                aisle.update({"x_min": x_cursor, "x_max": x_cursor + width, "x_center": x_cursor + width / 2, "aisle_width_m": width})
                relaid_aisles.append(aisle)
                x_cursor += width
        width = row_widths.get(row_number, max(_float(row.get("x_max")) - _float(row.get("x_min")), 0.0))
        row.update({"x_min": x_cursor, "x_max": x_cursor + width, "x_center": x_cursor + width / 2, "width_m": width})
        x_cursor += width
        previous_row_number = row_number
    if relaid_aisles or model.get("aisles"):
        model["aisles"] = relaid_aisles


def _refresh_navigation(model: dict[str, Any]) -> None:
    rows = {_display(row.get("row_number")): row for row in model.get("rows", [])}
    for aisle in model.get("aisles", []):
        row_from = rows.get(_display(aisle.get("row_from")))
        row_to = rows.get(_display(aisle.get("row_to")))
        if row_from and row_to:
            aisle["x_min"] = _float(row_from.get("x_max"))
            aisle["x_max"] = _float(row_to.get("x_min"))
            aisle["aisle_width_m"] = max(_float(aisle.get("x_max")) - _float(aisle.get("x_min")), 0.0)
    total_width = max([_float(row.get("x_max")) for row in model.get("rows", [])] + [0.0])
    max_row_y = max([_float(row.get("y_max")) for row in model.get("rows", [])] + [0.0])
    settings = model.get("settings") or {}
    for aisle in model.get("aisles", []):
        aisle["y_min"] = 0.0
        aisle["y_max"] = max_row_y
    for road in model.get("roads", []):
        road["x_max"] = total_width
        if road.get("road_type") == "top":
            width = _float(road.get("width_m"), _float(settings.get("top_road_width_m"), 0.0))
            road.update({"y_min": max_row_y, "y_max": max_row_y + width})
    for node in model.get("navigation_nodes", []):
        row = rows.get(_display(node.get("row_number")))
        if row:
            node["x"] = _float(row.get("x_center"))
            if str(node.get("node_id", "")).endswith(":bottom") or node.get("node_type") == "row_bottom_entry":
                node["y"] = _float(row.get("y_min"))
            elif str(node.get("node_id", "")).endswith(":top") or node.get("node_type") == "row_top_entry":
                node["y"] = _float(row.get("y_max"))
        elif node.get("node_id") in {"road:bottom", "road:top"}:
            node["x"] = total_width / 2 if total_width else 0.0
            if node.get("node_id") == "road:top":
                node["y"] = max_row_y + _float(settings.get("top_road_width_m"), 0.0) / 2
    for edge in model.get("navigation_edges", []):
        if edge.get("edge_type") == "row_walk":
            row_number = _display(str(edge.get("from_node", "")).split(":")[1] if ":" in str(edge.get("from_node", "")) else "")
            row = rows.get(row_number)
            if row:
                edge["distance_m"] = max(_float(row.get("y_max")) - _float(row.get("y_min")), 0.0)


def _has_intersection(model: dict[str, Any], row_number: str, x_min: float, x_max: float, y_min: float, y_max: float) -> bool:
    for row in model.get("rows", []):
        if _display(row.get("row_number")) == row_number:
            continue
        overlap_x = x_min < _float(row.get("x_max")) and x_max > _float(row.get("x_min"))
        overlap_y = y_min < _float(row.get("y_max")) and y_max > _float(row.get("y_min"))
        if overlap_x and overlap_y:
            return True
    return False


def _occupied_by_cell(model: dict[str, Any]) -> dict[str, float]:
    occupied: dict[str, float] = {}
    for placement in model.get("placements", []):
        key = _display(placement.get("cell_key"))
        occupied[key] = occupied.get(key, 0.0) + _float(placement.get("occupied_capacity_pallets", placement.get("qty_pallets", 0)))
    return occupied


def _validate_edited_rows(model: dict[str, Any], edited_rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    model_rows = {_display(row.get("row_number")) for row in model.get("rows", [])}
    seen = set()
    seen_orders: dict[float, str] = {}

    occupied = _occupied_by_cell(model)
    cells = model.get("cells", [])

    for edited in edited_rows:
        row_number = _display(edited.get("row_number"))

        if not row_number or row_number not in model_rows:
            errors.append(f"Ошибка: ряд {row_number or '—'} не найден в модели.")
            continue

        if row_number in seen:
            errors.append(f"Ошибка: ряд {row_number}: дублирующая строка настроек.")

        seen.add(row_number)

        try:
            row_order = float(str(edited.get("row_order", "")).replace(",", "."))

            if row_order <= 0:
                raise ValueError

            if row_order in seen_orders:
                errors.append(
                    f"Ошибка: ряды {seen_orders[row_order]} и {row_number}: одинаковый порядок {row_order:g}."
                )
            else:
                seen_orders[row_order] = row_number

        except (TypeError, ValueError):
            errors.append(
                f"Ошибка: ряд {row_number}: порядок должен быть положительным числом."
            )

        if edited.get("cell_direction") not in VALID_DIRECTIONS:
            errors.append(f"Ошибка: ряд {row_number}: некорректное направление.")

        if edited.get("row_storage_type") not in VALID_STORAGE_TYPES:
            errors.append(f"Ошибка: ряд {row_number}: некорректный тип ряда.")

        if edited.get("weight_zone", "unassigned") not in VALID_ZONES:
            errors.append(f"Ошибка: ряд {row_number}: некорректная весовая зона.")

        for field, label in (
            ("top_offset_cells", "верхний отступ"),
            ("bottom_offset_cells", "нижний отступ"),
        ):
            raw = edited.get(field, 0)

            if raw is None or (isinstance(raw, str) and not raw.strip()) or pd.isna(raw):
                raw = 0

            try:
                value = float(str(raw).replace(",", "."))

            except (TypeError, ValueError):
                errors.append(
                    f"Ошибка: ряд {row_number}: {label} должен быть целым числом не меньше 0."
                )
                continue

            if value < 0 or not value.is_integer():
                errors.append(
                    f"Ошибка: ряд {row_number}: {label} должен быть целым числом не меньше 0."
                )

        capacity = (
            int(_float(edited.get("cell_capacity_pallets"), 1))
            if edited.get("row_storage_type") == "deep_lane"
            else 1
        )

        if capacity < 1:
            errors.append(
                f"Ошибка: ряд {row_number}: вместимость должна быть не меньше 1."
            )

        for cell in cells:
            if (
                _display(cell.get("row_number")) == row_number
                and occupied.get(_cell_key(cell), 0.0) > capacity
            ):
                errors.append(
                    f"Ошибка: ряд {row_number}, ячейка {_cell_key(cell)}: "
                    f"занято {occupied[_cell_key(cell)]:g}, "
                    f"новая вместимость {capacity:g}."
                )

    missing_rows = sorted(model_rows - seen)

    if missing_rows:
        errors.append(
            "Ошибка: в черновике отсутствуют ряды: "
            + ", ".join(missing_rows)
            + "."
        )
    return errors


def sync_row_settings_to_model(model: dict[str, Any]) -> dict[str, Any]:
    rows_by_number = {_display(row.get("row_number")): row for row in model.get("rows", [])}
    row_widths: dict[str, float] = {}
    row_capacity: dict[str, int] = {}
    row_base_width: dict[str, float] = {}
    cells_by_row = {
        row_number: sorted([cell for cell in model.get("cells", []) if _display(cell.get("row_number")) == row_number], key=_cell_sort_key)
        for row_number in rows_by_number
    }
    for row_number, row in rows_by_number.items():
        row_cells = cells_by_row.get(row_number, [])
        capacity = int(_float(row.get("deep_lane_width"), 1)) if row.get("row_storage_type") == "deep_lane" else 1
        base_width = _base_cell_width(model, row, row_cells)
        target_width = base_width * capacity
        row_capacity[row_number] = capacity
        row_base_width[row_number] = base_width
        row_widths[row_number] = target_width
        row.setdefault("initial_weight_zone", row.get("weight_zone", "unassigned"))
        row["top_offset_cells"] = int(_float(row.get("top_offset_cells"), 0))
        row["bottom_offset_cells"] = int(_float(row.get("bottom_offset_cells"), 0))
        row.update({"base_cell_width_m": base_width, "base_row_width_m": base_width, "deep_lane_width": capacity, "capacity_pallets": capacity * len(row_cells), "cells_count": len(row_cells)})

    _relayout_rows_with_aisles(model, row_widths)

    for row_number, row in rows_by_number.items():
        capacity = row_capacity.get(row_number, 1)
        base_width = row_base_width.get(row_number, 1.0)
        x_min = _float(row.get("x_min"))
        x_max = _float(row.get("x_max"), x_min)
        for cell_group_name in ["cells", "base_cells"]:
            group_cells = [cell for cell in model.get(cell_group_name, []) if _display(cell.get("row_number")) == row_number]
            _apply_row_y_layout(model, row, group_cells)
            for cell in sorted(group_cells, key=_cell_number_sort_key):
                for field in CELL_SYNC_FIELDS:
                    cell[field] = row.get(field, "")
                cell.setdefault("initial_weight_zone", row.get("initial_weight_zone", row.get("weight_zone", "unassigned")))
                cell.update({"storage_type": row.get("row_storage_type", "normal"), "deep_lane_width": capacity, "capacity_pallets": capacity, "base_cell_width_m": base_width, "base_row_width_m": base_width, "x_min": x_min, "x_max": x_max, "x_center": (x_min + x_max) / 2, "width_m": x_max - x_min})
                cell["physical_slots"] = _physical_slots(cell, capacity) if row.get("row_storage_type") == "deep_lane" else []
    model["row_settings"] = [{field: row.get(field, "") for field in SYNC_FIELDS} for row in model.get("rows", [])]
    _refresh_navigation(model)
    return model


def _sync_offset_rows(model: dict[str, Any], row_numbers: set[str]) -> dict[str, Any]:
    """Relayout only rows whose effective offsets changed."""
    rows = {
        _display(row.get("row_number")): row
        for row in model.get("rows", [])
    }

    for row_number, row in rows.items():
        row["top_offset_cells"] = _offset_cells(
            row.get("top_offset_cells")
        )
        row["bottom_offset_cells"] = _offset_cells(
            row.get("bottom_offset_cells")
        )
        row.setdefault("top_offset_m", 0.0)
        row.setdefault("bottom_offset_m", 0.0)
        row.setdefault(
            "cells_count",
            sum(
                _display(cell.get("row_number")) == row_number
                for cell in model.get("cells", [])
            ),
        )

    for row_number in row_numbers:
        row = rows[row_number]

        for collection in ("cells", "base_cells"):
            cells = [
                cell
                for cell in model.get(collection, [])
                if _display(cell.get("row_number")) == row_number
            ]
            _apply_row_y_layout(model, row, cells)

    if model.get("cross_aisles"):
        from warehouse_cross_aisles import relayout_cross_aisle_rows

        relayout_cross_aisle_rows(model, row_numbers)

    model["row_settings"] = [
        {field: row.get(field, "") for field in SYNC_FIELDS}
        for row in model.get("rows", [])
    ]

    _refresh_navigation(model)
    return model


def apply_row_settings_transaction(model: dict[str, Any], edited_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    original = copy.deepcopy(model)
    errors = _validate_edited_rows(model, edited_rows)
    if errors:
        return original, errors
    changed: list[str] = []
    changed_fields: dict[str, set[str]] = {}
    edited_by_number = {_display(row.get("row_number")): row for row in edited_rows}
    candidate = copy.deepcopy(model)
    rows_by_number = {_display(row.get("row_number")): row for row in candidate.get("rows", [])}
    changed_fields: dict[str, set[str]] = {}
    for row_number, edited in edited_by_number.items():
        row = rows_by_number[row_number]
        old = {field: row.get(field) for field in ["row_order", "cell_direction", "weight_zone", "row_storage_type", "deep_lane_width", "row_group", "side", "comment", "top_offset_cells", "bottom_offset_cells"]}
        storage = edited.get("row_storage_type", "normal")
        capacity = int(_float(edited.get("cell_capacity_pallets"), 1)) if storage == "deep_lane" else 1
        new = {"row_order": _float(edited.get("row_order"), row.get("row_order", 0)), "cell_direction": edited.get("cell_direction", "bottom_to_top"), "weight_zone": edited.get("weight_zone", "unassigned"), "row_storage_type": storage, "deep_lane_width": capacity, "row_group": _display(edited.get("row_group")), "side": _display(edited.get("side")), "comment": _display(edited.get("comment")), "top_offset_cells": _offset_cells(edited.get("top_offset_cells")), "bottom_offset_cells": _offset_cells(edited.get("bottom_offset_cells"))}
        effective_changes = {field for field in old if _comparison_value(field, old[field]) != _comparison_value(field, new[field])}
        diff = [f"{field}: {old[field]} → {new[field]}" for field in old if field in effective_changes]
        if diff:
            row.update(new)
            changed_fields[row_number] = effective_changes
            changed.append(f"Ряд {row_number}: " + "; ".join(diff))
    offset_fields = {"top_offset_cells", "bottom_offset_cells"}
    offsets_only = bool(changed_fields) and all(fields <= offset_fields for fields in changed_fields.values())
    if offsets_only:
        _sync_offset_rows(candidate, set(changed_fields))
    else:
        sync_row_settings_to_model(candidate)
    if candidate.get("cross_aisles") and not offsets_only:
        from warehouse_cross_aisles import apply_cross_aisles_transaction

        cross_draft = [
            {field: aisle.get(field) for field in ("row_number", "after_cell_number", "width_cells", "comment")}
            for aisle in candidate["cross_aisles"]
        ]
        candidate, cross_errors = apply_cross_aisles_transaction(candidate, cross_draft)
        if cross_errors:
            return original, cross_errors
    if not changed_fields:
    return original, ["Изменений нет."]

offsets_only = all(
    fields <= OFFSET_FIELDS
    for fields in changed_fields.values()
)

if offsets_only:
    _sync_offset_rows(candidate, set(changed_fields))
else:
    sync_row_settings_to_model(candidate)

if candidate.get("cross_aisles") and not offsets_only:
    from warehouse_cross_aisles import apply_cross_aisles_transaction

    cross_draft = [
        {
            field: aisle.get(field)
            for field in (
                "row_number",
                "after_cell_number",
                "width_cells",
                "comment",
            )
        }
        for aisle in candidate["cross_aisles"]
    ]

    candidate, cross_errors = apply_cross_aisles_transaction(
        candidate,
        cross_draft,
    )

    if cross_errors:
        return original, cross_errors