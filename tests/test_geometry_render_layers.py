from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import virtual_warehouse_app as app
from warehouse_geometry_render_layers import (
    DYNAMIC_STATE_MARKER,
    build_geometry_dynamic_payload,
    build_geometry_static_layer,
    compose_geometry_layers,
)


def _model(count=4, occupied=0):
    cells = []
    for index in range(count):
        cell = {
            "code": f"A-{index}", "row_number": "1", "cell_number": str(index + 1), "tier": "1",
            "x_min": index, "x_max": index + 1, "y_min": 0, "y_max": 1,
            "x_center": index + .5, "y_center": .5, "capacity_pallets": 2,
            "storage_type": "deep_lane" if index == 0 else "normal", "deep_lane_width": 2,
            "physical_slots": [], "cell_direction": "bottom_to_top", "volume_m3": 1,
        }
        if index < occupied:
            cell.update(occupied_capacity_pallets=1, placement_category="heavy", placements=[{
                "sku_code": f"SKU-{index}", "sku_name": "Товар", "characteristic": "Красный", "quantity": 1,
            }], placement_tooltip=f"SKU: SKU-{index}\nХарактеристика: Красный")
        cells.append(cell)
    return {
        "model_id": "layers", "settings": {"top_road_width_m": 2},
        "rows": [{"row_number": "1", "x_min": 0, "x_max": count, "y_min": 0, "y_max": 1, "cells_count": count}],
        "cells": cells, "aisles": [{"x_min": count, "x_max": count + 1, "row_from": "1", "row_to": "2", "aisle_width_m": 1}],
        "cross_aisles": [{"x_min": 1, "x_max": 2, "y_min": 0, "y_max": 1, "row_number": "1", "after_cell_number": "1", "width_cells": 1, "width_m": 1}],
        "roads": [{"road_type": "top", "x_min": 0, "x_max": count, "y_min": 1, "y_max": 2, "width_m": 1}],
    }


def test_static_geometry_has_stable_cells_and_no_product_state():
    html = build_geometry_static_layer(_model(4, 2))
    assert "data-cell-key='1|1|1'" in html
    assert "верхний проезд" in html and "Поперечный проезд" in html
    assert "SKU-0" not in html and "Красный" not in html
    assert html.count(DYNAMIC_STATE_MARKER) == 1
    assert html == build_geometry_static_layer(_model(4, 0))


def test_dynamic_payload_is_sparse_and_has_no_geometry():
    payload = build_geometry_dynamic_payload(_model(1000, 10))
    assert len(payload) == 10
    assert payload["1|1|1"]["occupancy_status"] == "partial"
    assert payload["1|1|1"]["placements"][0]["sku_code"] == "SKU-0"
    encoded = json.dumps(payload)
    assert "x_min" not in encoded and "cross_aisles" not in encoded and '"cells"' not in encoded
    assert len(encoded) < len(build_geometry_static_layer(_model(1000, 0)))


def test_empty_dynamic_payload_is_empty_and_composes():
    static = build_geometry_static_layer(_model())
    assert build_geometry_dynamic_payload(_model()) == {}
    result = compose_geometry_layers(static, {})
    assert DYNAMIC_STATE_MARKER not in result and "const state = {};" in result


def test_compose_safely_serializes_script_end_quotes_slashes_and_unicode():
    value = '</script> "quote" \\ newline\nРусский'
    result = compose_geometry_layers(build_geometry_static_layer(_model()), {"1|1|1": {"tooltip": value}})
    assert result.lower().count("</script>") == 2  # only the two renderer-owned closing tags
    assert "\\u003c/script\\u003e" in result
    assert "Русский" in result and "\\n" in result


@pytest.mark.parametrize("template", ["no marker", DYNAMIC_STATE_MARKER * 2])
def test_compose_rejects_invalid_marker_count(template):
    with pytest.raises(ValueError, match="exactly once"):
        compose_geometry_layers(template, {})


def test_layer_cache_keys_ignore_heavy_sources_and_split_domains(monkeypatch):
    app.build_geometry_static_layer_cached.clear(); app.build_geometry_dynamic_layer_cached.clear()
    static_calls, dynamic_calls = [], []
    monkeypatch.setattr(app, "build_geometry_static_layer", lambda *a, **k: static_calls.append(1) or DYNAMIC_STATE_MARKER)
    monkeypatch.setattr(app, "build_geometry_dynamic_layer_direct", lambda *a, **k: dynamic_calls.append(1) or {})
    assert app.build_geometry_static_layer_cached(_model(), ("m", 1, 1), {}, 22., True, 1) == DYNAMIC_STATE_MARKER
    assert app.build_geometry_static_layer_cached({"changed": True}, ("m", 1, 1), {"changed": True}, 22., True, 1) == DYNAMIC_STATE_MARKER
    app.build_geometry_dynamic_layer_cached(_model(), {}, ("m", 1, 1, 1, 1), {}, 1)
    app.build_geometry_dynamic_layer_cached({"changed": True}, {"changed": True}, ("m", 1, 1, 1, 1), {"changed": True}, 1)
    assert static_calls == [1] and dynamic_calls == [1]


def test_revision_domain_contract_excludes_inventory_and_receipts():
    assert app.GEOMETRY_STATIC_DOMAINS == ("geometry", "render_settings")
    assert app.GEOMETRY_DYNAMIC_DOMAINS == ("geometry", "placements", "outbound", "render_settings")
