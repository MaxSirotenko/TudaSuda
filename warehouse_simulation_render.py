"""Read-only SimulationState projection for the existing geometry renderer."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from warehouse_geometry_render_layers import build_geometry_dynamic_payload_from_state


def _positive_number(value: Any) -> int | float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def _projection(model: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int | float]]:
    cells = {cell.get("cell_key"): cell for cell in model.get("cells", []) or [] if isinstance(cell, Mapping)}
    lots_by_cell: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    located = unknown_lots = 0
    unknown_boxes: int | float = 0
    for lot in state.get("stock_lots", []) or []:
        if not isinstance(lot, Mapping) or not _positive_number(lot.get("qty_boxes")):
            continue
        if lot.get("location_status") == "located" and lot.get("cell_key") in cells:
            lots_by_cell[lot.get("cell_key")].append(lot)
            located += 1
        elif lot.get("location_status") == "unknown":
            unknown_lots += 1
            unknown_boxes += _positive_number(lot.get("qty_boxes"))

    occupancy = {row.get("cell_key"): row for row in state.get("cell_occupancy", []) or []
                 if isinstance(row, Mapping)}
    placements: list[dict[str, Any]] = []
    exact_occupied = exact_free = unknown_cells = footprints = 0
    for cell_key, row in sorted(occupancy.items(), key=lambda item: str(item[0])):
        cell = cells.get(cell_key)
        if not cell:
            continue
        exact = row.get("exact_occupied_positions")
        unknown = exact is None
        if unknown:
            unknown_cells += 1
        elif exact > 0:
            exact_occupied += 1
            footprints += exact
        else:
            exact_free += 1
        if not unknown and exact <= 0:
            continue
        cell_lots = sorted(lots_by_cell.get(cell_key, []), key=lambda lot: str(lot.get("stock_lot_id")))
        qty = sum(_positive_number(lot.get("qty_boxes")) for lot in cell_lots)
        sku_keys = sorted({str(lot.get("sku_key")) for lot in cell_lots if lot.get("sku_key")})
        names = sorted({str(lot.get("nomenclature") or lot.get("sku_name")) for lot in cell_lots
                        if lot.get("nomenclature") or lot.get("sku_name")})
        characteristics = sorted({str(lot.get("characteristic")) for lot in cell_lots if lot.get("characteristic")})
        geometry_key = f"{cell.get('row_number')}|{cell.get('cell_number')}|{cell.get('tier') or '1'}"
        placements.append({
            "cell_key": geometry_key, "simulation_cell_key": cell_key,
            "row_number": cell.get("row_number"), "cell_number": cell.get("cell_number"), "tier": cell.get("tier"),
            "sku_key": ", ".join(sku_keys), "sku_name": ", ".join(names),
            "characteristic": ", ".join(characteristics), "characteristic_name": ", ".join(characteristics),
            "quantity": qty, "qty_boxes": qty,
            "occupied_capacity_pallets": 0 if unknown else exact,
            "occupancy_unknown": unknown,
            "calculated_zone": cell.get("weight_zone"),
            "stock_lot_count": len(cell_lots),
        })
    report = {
        "located_stock_lots": located, "unknown_location_stock_lots": unknown_lots,
        "unknown_location_boxes": unknown_boxes, "cells_exact_occupied": exact_occupied,
        "cells_exact_free": exact_free, "cells_unknown_occupancy": unknown_cells,
        "rendered_cells": len(placements), "rendered_physical_footprints": footprints,
    }
    return placements, report


def build_simulation_render_placements(
    model: dict[str, Any], simulation_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return non-authoritative render placements; inputs remain untouched."""
    return _projection(model, simulation_state)[0]


def build_simulation_render_report(
    model: dict[str, Any], simulation_state: dict[str, Any],
) -> dict[str, int | float]:
    """Return projection diagnostics, including unknown-location inventory."""
    return _projection(model, simulation_state)[1]


def build_simulation_dynamic_payload(
    model: dict[str, Any], simulation_state: dict[str, Any], *,
    label_settings: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Adapt a SimulationState to the existing sparse dynamic map payload."""
    placements = build_simulation_render_placements(model, simulation_state)
    return build_geometry_dynamic_payload_from_state(
        model, {"placements": placements}, label_settings=label_settings,
    )
