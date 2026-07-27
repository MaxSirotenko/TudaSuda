"""Independent static geometry and compact dynamic state layers for the map."""

from __future__ import annotations

import json
from typing import Any

from warehouse_geometry_model import build_geometry_html

DYNAMIC_STATE_MARKER = "__WAREHOUSE_DYNAMIC_STATE__"

DEFAULT_COLORS = {
    "cell_color": "#DCEBFF",
    "deep_lane_cell_color": "#CFE8D5",
    "occupied_cell_color": "#90CAF9",
    "deep_lane_partial_color": "#A5D6A7",
    "deep_lane_full_color": "#66BB6A",
}
CATEGORY_COLORS = {
    "heavy": "#F4A6A6", "medium": "#F7D486", "light": "#BFE3B4",
    "fragile": "#D8B4FE", "unclassified": "#CBD5E1", "unassigned": "#E5E7EB",
}
_DYNAMIC_CELL_FIELDS = {
    "occupied_capacity_pallets", "occupancy_label", "placements", "placement_category",
    "placement_tooltip", "outbound_diagnostic", "outbound_status",
}


def geometry_cell_key(cell: dict[str, Any]) -> str:
    """Return the stable address shared by DOM geometry and dynamic state."""
    return f"{cell.get('row_number')}|{cell.get('cell_number')}|{cell.get('tier') or '1'}"


def build_geometry_static_layer(
    model: dict[str, Any], scale: float = 18.0, detailed: bool = True,
    label_settings: dict[str, Any] | None = None,
) -> str:
    """Build geometry once, explicitly excluding placement-derived cell fields."""
    static_model = dict(model)
    static_model["cells"] = [
        {key: value for key, value in cell.items() if key not in _DYNAMIC_CELL_FIELDS}
        for cell in model.get("cells", [])
    ]
    template = build_geometry_html(
        static_model, scale=scale, detailed=detailed, label_settings=label_settings,
        dynamic_state_marker=DYNAMIC_STATE_MARKER,
    )
    if template.count(DYNAMIC_STATE_MARKER) != 1:
        raise ValueError("Static geometry template must contain exactly one dynamic-state marker")
    return template


def _display_name(placement: dict[str, Any]) -> str:
    return str(placement.get("sku_name") or placement.get("item_name") or placement.get("sku_code") or "").strip()


def _sku_label(placements: list[dict[str, Any]]) -> str:
    unique, seen = [], set()
    for placement in placements:
        name = _display_name(placement)
        key = str(placement.get("sku_key") or placement.get("sku_code") or name).strip()
        if name and key not in seen:
            seen.add(key); unique.append(name)
    return (f"{unique[0]} +{len(unique) - 1}" if len(unique) > 1 else unique[0]) if unique else ""


def build_geometry_dynamic_payload(
    model: dict[str, Any], label_settings: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return state only for occupied or diagnostically exceptional cells."""
    settings = label_settings or {}
    colors = DEFAULT_COLORS | dict(settings.get("colors", {}))
    payload: dict[str, dict[str, Any]] = {}
    for cell in model.get("cells", []):
        key = geometry_cell_key(cell)
        occupied = float(cell.get("occupied_capacity_pallets", 0) or 0)
        special = bool(cell.get("placement_tooltip") or cell.get("outbound_diagnostic") or cell.get("outbound_status"))
        if occupied <= 0 and not special:
            continue
        capacity = float(cell.get("capacity_pallets", 1) or 1)
        placements = cell.get("placements", [])
        if occupied > capacity:
            status, fill, border = "overfilled", "#fecaca", "2px solid #DC5A5A"
        elif occupied >= capacity:
            status = "full"
            fill = CATEGORY_COLORS.get(str(cell.get("placement_category", "")), colors["deep_lane_full_color"] if cell.get("storage_type") == "deep_lane" else colors["occupied_cell_color"])
            border = "2px solid #4F8F5B"
        else:
            status = "partial"
            fill = CATEGORY_COLORS.get(str(cell.get("placement_category", "")), colors["deep_lane_partial_color"] if cell.get("storage_type") == "deep_lane" else colors["occupied_cell_color"])
            border = "2px solid #82A878"
        if key == str(settings.get("selected_cell_key", "")):
            fill, border = str(colors.get("selected_cell_color", "#FF7043")), "2px solid #E5532D"
        entry: dict[str, Any] = {
            "occupied": occupied, "capacity": capacity, "occupancy_status": status,
            "fill_color": fill, "border": border,
        }
        if cell.get("placement_tooltip"):
            entry["tooltip"] = str(cell["placement_tooltip"])
        if settings.get("show_cell_labels", True):
            label = _sku_label(placements)
            if label:
                entry["label"] = label
        compact_placements = []
        for placement in placements:
            compact = {key: placement[key] for key in ("sku_code", "sku_name", "item_name", "characteristic", "quantity", "source", "confidence") if placement.get(key) not in (None, "")}
            if compact:
                compact_placements.append(compact)
        if compact_placements:
            entry["placements"] = compact_placements
        payload[key] = entry
    return payload


def safe_json_dumps(payload: object) -> str:
    """Serialize JSON for an HTML script context, including script-end protection."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def compose_geometry_layers(static_layer: str, dynamic_payload: dict[str, Any]) -> str:
    """Inject one compact payload without traversing model geometry."""
    count = static_layer.count(DYNAMIC_STATE_MARKER)
    if count != 1:
        raise ValueError(f"Static geometry template must contain dynamic-state marker exactly once; found {count}")
    return static_layer.replace(DYNAMIC_STATE_MARKER, safe_json_dumps(dynamic_payload), 1)
