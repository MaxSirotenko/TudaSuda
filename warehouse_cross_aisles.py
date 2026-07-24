from __future__ import annotations

import copy
from typing import Any


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def ensure_cross_aisles(model: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy models without changing their cells or addresses."""
    model.setdefault("cross_aisles", [])
    return model


def build_cross_aisle_draft(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "row_number": _text(item.get("row_number")),
            "after_cell_number": _text(item.get("after_cell_number")),
            "width_cells": item.get("width_cells", 1),
            "width_m": _number(item.get("width_m")),
            "comment": _text(item.get("comment")),
        }
        for item in model.get("cross_aisles", [])
    ]


def create_cross_aisle_settings_state(model: dict[str, Any]) -> dict[str, Any]:
    records = build_cross_aisle_draft(model)
    return {
        "model_id": str(model.get("model_id") or model.get("source_file_hash") or "active"),
        "baseline": copy.deepcopy(records),
        "draft": copy.deepcopy(records),
        "editor_revision": 0,
    }


def update_cross_aisle_settings_state(state: dict[str, Any], draft: list[dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(state)
    result["draft"] = copy.deepcopy(draft)
    return result


def reset_cross_aisle_settings_state(state: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(state)
    result["draft"] = copy.deepcopy(result.get("baseline", []))
    result["editor_revision"] = int(result.get("editor_revision", 0)) + 1
    return result


def changed_cross_aisle_count(state: dict[str, Any]) -> int:
    baseline = state.get("baseline", [])
    draft = state.get("draft", [])
    return sum(1 for index in range(max(len(baseline), len(draft))) if (baseline[index] if index < len(baseline) else None) != (draft[index] if index < len(draft) else None))


def _logical_cells(model: dict[str, Any], row_number: str, collection: str = "cells") -> list[dict[str, Any]]:
    cells = [cell for cell in model.get(collection, []) if _text(cell.get("row_number")) == row_number]
    # Cell numbers express the established picking order; coordinates only break ties.
    def key(cell: dict[str, Any]) -> tuple[float, str, float]:
        raw = _text(cell.get("cell_number"))
        try:
            number = float(raw.replace(",", "."))
        except ValueError:
            number = float("inf")
        return number, raw, _number(cell.get("y_min"))
    return sorted(cells, key=key)


def _physical_cells(model: dict[str, Any], row_number: str, collection: str = "cells") -> list[dict[str, Any]]:
    """Use established coordinates and direction rather than numeric addresses."""
    cells = [cell for cell in model.get(collection, []) if _text(cell.get("row_number")) == row_number]
    row = next((item for item in model.get("rows", []) if _text(item.get("row_number")) == row_number), {})
    reverse = row.get("cell_direction", "bottom_to_top") == "top_to_bottom"
    return sorted(cells, key=lambda cell: (_number(cell.get("y_center"), _number(cell.get("y_min"))), _text(cell.get("cell_number"))), reverse=reverse)


def validate_cross_aisles(model: dict[str, Any], draft: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows = {_text(row.get("row_number")): row for row in model.get("rows", [])}
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    ids: set[str] = set()
    length = _number((model.get("settings") or {}).get("cell_length_m"), 1.0) or 1.0
    for index, source in enumerate(draft, 1):
        row_number = _text(source.get("row_number"))
        cell_number = _text(source.get("after_cell_number"))
        prefix = f"Строка {index}"
        if not row_number:
            errors.append(f"{prefix}: номер ряда не заполнен.")
        elif row_number not in rows:
            errors.append(f"{prefix}: ряд {row_number} не существует.")
        if not cell_number:
            errors.append(f"{prefix}: номер ячейки не заполнен.")
        row_cells = _logical_cells(model, row_number) if row_number in rows else []
        physical_cells = _physical_cells(model, row_number) if row_number in rows else []
        cell_numbers = [_text(cell.get("cell_number")) for cell in row_cells]
        physical_numbers = [_text(cell.get("cell_number")) for cell in physical_cells]
        if cell_number and row_number in rows and cell_number not in cell_numbers:
            errors.append(f"{prefix}: ячейка {cell_number} ряда {row_number} не существует.")
        if cell_number and cell_number in physical_numbers and physical_numbers.index(cell_number) == len(physical_numbers) - 1:
            errors.append(f"{prefix}: проезд нельзя создать после последней физической ячейки ряда {row_number}.")
        raw_width = source.get("width_cells")
        width = _number(raw_width, float("nan"))
        if not width.is_integer() or width <= 0:
            errors.append(f"{prefix}: ширина должна быть целым числом больше 0.")
        pair = (row_number, cell_number)
        if pair in seen:
            errors.append(f"{prefix}: проезд после ячейки {cell_number} ряда {row_number} дублируется.")
        seen.add(pair)
        aisle_id = f"cross:{row_number}:{cell_number}"
        if aisle_id in ids:
            errors.append(f"{prefix}: aisle_id {aisle_id} не уникален.")
        ids.add(aisle_id)
        if width.is_integer() and width > 0:
            normalized.append({"aisle_id": aisle_id, "row_number": row_number, "after_cell_number": cell_number, "width_cells": int(width), "width_m": int(width) * length, "comment": _text(source.get("comment")), "aisle_type": "cross_aisle"})
    return normalized, errors


def _layout_collection(model: dict[str, Any], row: dict[str, Any], aisles: list[dict[str, Any]], collection: str) -> None:
    row_number = _text(row.get("row_number"))
    cells = _physical_cells(model, row_number, collection)
    if not cells:
        return
    length = _number(cells[0].get("length_m")) or _number((model.get("settings") or {}).get("cell_length_m"), 1.0) or 1.0
    offset = _number(row.get("top_offset_m"), _number(row.get("top_offset_cells")) * length)
    gaps = {_text(aisle["after_cell_number"]): _number(aisle["width_m"]) for aisle in aisles}
    cursor = offset
    for cell in cells:
        cell.update({"y_min": cursor, "y_max": cursor + length, "y_center": cursor + length / 2, "length_m": length})
        cursor += length
        gap = gaps.get(_text(cell.get("cell_number")), 0.0)
        if gap:
            if collection == "cells":
                aisle = next(item for item in aisles if _text(item["after_cell_number"]) == _text(cell.get("cell_number")))
                aisle.update({"x_min": _number(row.get("x_min")), "x_max": _number(row.get("x_max")), "x_center": (_number(row.get("x_min")) + _number(row.get("x_max"))) / 2, "y_min": cursor, "y_max": cursor + gap, "y_center": cursor + gap / 2})
            cursor += gap
        if cell.get("physical_slots"):
            for slot in cell["physical_slots"]:
                slot.update({"y_min": cell["y_min"], "y_max": cell["y_max"]})
    if collection == "cells":
        row["y_min"] = 0.0
        row["y_max"] = cursor + _number(row.get("bottom_offset_m"), _number(row.get("bottom_offset_cells")) * length)


def _refresh_cross_navigation(model: dict[str, Any]) -> None:
    nodes = [node for node in model.get("navigation_nodes", []) if node.get("node_type") != "cross_aisle"]
    edges = [edge for edge in model.get("navigation_edges", []) if edge.get("edge_type") != "cross_aisle"]
    for aisle in model.get("cross_aisles", []):
        base = aisle["aisle_id"]
        xs = {"left": aisle["x_min"], "center": aisle["x_center"], "right": aisle["x_max"]}
        for side, x in xs.items():
            nodes.append({"node_id": f"{base}:{side}", "node_type": "cross_aisle", "row_number": aisle["row_number"], "x": x, "y": aisle["y_center"]})
        half = (aisle["x_max"] - aisle["x_min"]) / 2
        for source, target in (("left", "center"), ("center", "left"), ("center", "right"), ("right", "center")):
            edges.append({"from_node": f"{base}:{source}", "to_node": f"{base}:{target}", "distance_m": half, "edge_type": "cross_aisle"})
    model["navigation_nodes"], model["navigation_edges"] = nodes, edges


def relayout_cross_aisle_rows(model: dict[str, Any], row_numbers: set[str]) -> None:
    """Reapply saved cross-aisle gaps without touching unrelated rows."""
    rows = {_text(row.get("row_number")): row for row in model.get("rows", [])}
    aisles_by_row: dict[str, list[dict[str, Any]]] = {}
    for aisle in model.get("cross_aisles", []):
        aisles_by_row.setdefault(_text(aisle.get("row_number")), []).append(aisle)
    for row_number in row_numbers:
        row = rows.get(row_number)
        if row is None:
            continue
        aisles = aisles_by_row.get(row_number, [])
        _layout_collection(model, row, aisles, "cells")
        _layout_collection(model, row, aisles, "base_cells")
    _refresh_cross_navigation(model)


def apply_cross_aisles_transaction(model: dict[str, Any], draft: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Validate all records and atomically rebuild geometry exactly once."""
    normalized, errors = validate_cross_aisles(model, draft)
    if errors:
        return model, errors
    candidate = copy.deepcopy(model)
    candidate["cross_aisles"] = normalized
    by_row: dict[str, list[dict[str, Any]]] = {}
    for aisle in candidate["cross_aisles"]:
        by_row.setdefault(aisle["row_number"], []).append(aisle)
    for row in candidate.get("rows", []):
        aisles = by_row.get(_text(row.get("row_number")), [])
        _layout_collection(candidate, row, aisles, "cells")
        _layout_collection(candidate, row, aisles, "base_cells")
    max_y = max([_number(row.get("y_max")) for row in candidate.get("rows", [])] + [0.0])
    for aisle in candidate.get("aisles", []):
        aisle["y_max"] = max_y
    for road in candidate.get("roads", []):
        if road.get("road_type") == "top":
            width = _number(road.get("width_m"))
            road.update({"y_min": max_y, "y_max": max_y + width})
    _refresh_cross_navigation(candidate)
    return candidate, []
