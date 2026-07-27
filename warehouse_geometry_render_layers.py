"""Independent static geometry and compact dynamic state layers for the map."""

from __future__ import annotations

import json
from typing import Any

from warehouse_geometry_model import build_geometry_html
from warehouse_performance import measure_step
from warehouse_placement_diagnostics import (
    build_placement_tooltip_for_cell,
    placement_category_for_placements,
    summarize_placements_by_cell,
)

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


def build_dynamic_cell_payload(
    cell: dict[str, Any], label_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one enriched-or-direct cell onto the state consumed by JavaScript."""
    settings = label_settings or {}
    colors = DEFAULT_COLORS | dict(settings.get("colors", {}))
    key = geometry_cell_key(cell)
    occupied = float(cell.get("occupied_capacity_pallets", 0) or 0)
    capacity = float(cell.get("capacity_pallets", 1) or 1)
    placements = cell.get("placements", [])
    if occupied > capacity:
        status, fill, border = "overfilled", "#fecaca", "2px solid #DC5A5A"
    elif occupied >= capacity:
        status, border = "full", "2px solid #4F8F5B"
        fill = CATEGORY_COLORS.get(str(cell.get("placement_category", "")), colors["deep_lane_full_color"] if cell.get("storage_type") == "deep_lane" else colors["occupied_cell_color"])
    else:
        status, border = "partial", "2px solid #82A878"
        fill = CATEGORY_COLORS.get(str(cell.get("placement_category", "")), colors["deep_lane_partial_color"] if cell.get("storage_type") == "deep_lane" else colors["occupied_cell_color"])
    if key == str(settings.get("selected_cell_key", "")):
        fill, border = str(colors.get("selected_cell_color", "#FF7043")), "2px solid #E5532D"
    entry: dict[str, Any] = {"occupied": occupied, "capacity": capacity, "occupancy_status": status, "fill_color": fill, "border": border}
    if cell.get("placement_tooltip"):
        entry["tooltip"] = str(cell["placement_tooltip"])
    if settings.get("show_cell_labels", True) and (label := _sku_label(placements)):
        entry["label"] = label
    compact_placements = []
    for placement in placements:
        compact = {field: placement[field] for field in ("sku_code", "sku_name", "item_name", "characteristic", "quantity", "source", "confidence") if placement.get(field) not in (None, "")}
        if compact:
            compact_placements.append(compact)
    if compact_placements:
        entry["placements"] = compact_placements
    for field in ("outbound_status", "outbound_diagnostic"):
        if cell.get(field) not in (None, ""):
            entry[field] = cell[field]
    return entry


def build_geometry_dynamic_payload(
    model: dict[str, Any], label_settings: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return state only for occupied or diagnostically exceptional cells."""
    payload: dict[str, dict[str, Any]] = {}
    for cell in model.get("cells", []):
        occupied = float(cell.get("occupied_capacity_pallets", 0) or 0)
        special = bool(cell.get("placement_tooltip") or cell.get("outbound_diagnostic") or cell.get("outbound_status"))
        if occupied <= 0 and not special:
            continue
        payload[geometry_cell_key(cell)] = build_dynamic_cell_payload(cell, label_settings)
    return payload


def build_geometry_dynamic_payload_from_state(
    model: dict[str, Any], placement_state: dict[str, Any], snapshot: dict[str, Any] | None = None,
    label_settings: dict[str, Any] | None = None, outbound_context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build sparse map state directly, without copying or enriching warehouse cells."""
    placements = placement_state.get("placements", []) if isinstance(placement_state, dict) else []
    aggregate_metadata = {"placements_count": len(placements), "occupied_cell_keys_count": 0}
    with measure_step("aggregate_dynamic_placements", aggregate_metadata):
        summaries = summarize_placements_by_cell(placements)
        aggregate_metadata["occupied_cell_keys_count"] = len(summaries)
    before_by_cell = (snapshot or {}).get("before_by_cell") if isinstance(snapshot, dict) else None
    if not before_by_cell and isinstance(snapshot, dict):
        before_by_cell = summarize_placements_by_cell(snapshot.get("placements_before", []))
    outbound = outbound_context or {}
    outbound_tooltips = outbound.get("tooltips", {})
    outbound_statuses = outbound.get("statuses", {})
    outbound_diagnostics = outbound.get("diagnostics", {})
    interesting = set(summaries) | set(outbound_tooltips) | set(outbound_statuses) | set(outbound_diagnostics)
    resolve_metadata = {"model_cells_count": len(model.get("cells", [])), "interesting_cell_keys_count": len(interesting), "resolved_cell_keys_count": 0}
    resolved: dict[str, dict[str, Any]] = {}
    with measure_step("resolve_dynamic_cell_metadata", resolve_metadata):
        for cell in model.get("cells", []):
            key = geometry_cell_key(cell)
            if key in interesting:
                resolved[key] = cell
        resolve_metadata["resolved_cell_keys_count"] = len(resolved)
    payload: dict[str, dict[str, Any]] = {}
    with measure_step("build_dynamic_tooltips", {"interesting_cell_keys_count": len(interesting)}):
        tooltips = {
            key: build_placement_tooltip_for_cell(cell, key, summaries[key], (before_by_cell or {}).get(key))
            for key, cell in resolved.items() if key in summaries
        }
    build_metadata = {"dynamic_cells_count": 0, "payload_size_bytes": 0, "builder_mode": "direct_state"}
    with measure_step("build_dynamic_payload_direct", build_metadata):
        for key, cell in resolved.items():
            summary = summaries.get(key, {})
            dynamic = {
                "row_number": cell.get("row_number"), "cell_number": cell.get("cell_number"), "tier": cell.get("tier"),
                "capacity_pallets": cell.get("capacity_pallets"), "storage_type": cell.get("storage_type"),
                "occupied_capacity_pallets": summary.get("occupied_capacity_pallets", 0),
                "placements": summary.get("placements", []),
                "placement_category": placement_category_for_placements(summary.get("placements", []), str(cell.get("weight_zone") or "unclassified")),
            }
            tooltip = tooltips.get(key, "") + str(outbound_tooltips.get(key, ""))
            if tooltip:
                dynamic["placement_tooltip"] = tooltip
            if key in outbound_statuses:
                dynamic["outbound_status"] = outbound_statuses[key]
            if key in outbound_diagnostics:
                dynamic["outbound_diagnostic"] = outbound_diagnostics[key]
            payload[key] = build_dynamic_cell_payload(dynamic, label_settings)
        build_metadata["dynamic_cells_count"] = len(payload)
        build_metadata["payload_size_bytes"] = len(safe_json_dumps(payload).encode("utf-8"))
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
