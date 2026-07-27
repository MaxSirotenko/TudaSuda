from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import virtual_warehouse_app as app
from warehouse_geometry_render_layers import (
    build_geometry_dynamic_payload,
    build_geometry_dynamic_payload_from_state,
    compose_geometry_layers,
    build_geometry_static_layer,
)
from warehouse_inventory_placement import attach_placements_to_model
from warehouse_placement_diagnostics import enrich_model_with_placement_diagnostics


def cell(number, capacity=2, storage_type="normal", **extra):
    return {"row_number": "1", "cell_number": str(number), "tier": "1", "capacity_pallets": capacity,
            "storage_type": storage_type, "deep_lane_width": 2, "weight_zone": "heavy",
            "x_min": number, "x_max": number + 1, **extra}


def placement(number, qty=1, sku="A", **extra):
    return {"cell_key": f"1|{number}|1", "row_number": "1", "cell_number": str(number), "tier": "1",
            "occupied_capacity_pallets": qty, "sku_key": sku, "sku_code": sku, "sku_name": f"Товар {sku}",
            "characteristic_name": 'Красный "тест"', "source": "receipt", "receipt_numbers": ["П-1"],
            "receipt_line_ids": ["line-1"], **extra}


def test_direct_builder_is_sparse_immutable_and_has_no_geometry(monkeypatch):
    model = {"model_id": "m", "cells": [cell(i) for i in range(1, 1001)], "roads": [{"x": 1}]}
    state = {"placements": [placement(i) for i in range(1, 11)]}
    snapshot = {"placements_before": [placement(1, .25)]}
    before = copy.deepcopy((model, state, snapshot))
    monkeypatch.setattr(copy, "deepcopy", lambda *_: pytest.fail("production builder used deepcopy"))
    payload = build_geometry_dynamic_payload_from_state(model, state, snapshot)
    assert len(payload) == 10
    assert (model, state, snapshot) == before
    assert "x_min" not in str(payload) and "cells" not in payload


def test_empty_and_occupancy_capacity_category_colors():
    model = {"cells": [cell(1), cell(2, 2, "deep_lane"), cell(3, 2)]}
    assert build_geometry_dynamic_payload_from_state(model, {"placements": []}) == {}
    state = {"placements": [placement(1, 1, calculated_zone="fragile"), placement(2, 2), placement(3, 3)]}
    payload = build_geometry_dynamic_payload_from_state(model, state)
    assert {key: payload["1|1|1"][key] for key in ("occupied", "capacity", "occupancy_status", "fill_color")} == {"occupied": 1.0, "capacity": 2.0, "occupancy_status": "partial", "fill_color": "#D8B4FE"}
    assert payload["1|2|1"]["occupancy_status"] == "full"
    assert payload["1|3|1"]["occupancy_status"] == "overfilled"
    assert payload["1|3|1"]["border"] == "2px solid #DC5A5A"


def test_selected_deep_lane_and_tooltip_snapshot_equivalence():
    model = {"model_id": "m", "cells": [cell(1, 2, "deep_lane")]}
    old = placement(1, .5, "A")
    state = {"placements": [placement(1, 1, "A"), placement(1, .5, "B", placement_reason_text="Причина\nстрока") ]}
    snapshot = {"before_by_cell": {"1|1|1": {"placements": [old]}}}
    payload = build_geometry_dynamic_payload_from_state(model, state, snapshot, {"selected_cell_key": "1|1|1"})
    entry = payload["1|1|1"]
    assert entry["fill_color"] == "#FF7043"
    assert [p["sku_code"] for p in entry["placements"]] == ["A", "B"]
    for text in ("Ячейка: 1|1|1", "Было до: 0.5", "Добавлено: 0.5", "смешанная занятость", "новый приход", "П-1", "line-1", "Причина\nстрока"):
        assert text in entry["tooltip"]


def test_direct_matches_legacy_enriched_projection(monkeypatch):
    model = {"model_id": "m", "cells": [cell(1), cell(2, 2, "deep_lane"), cell(3)]}
    state = {"placements": [placement(1, 1, "A"), placement(2, 2, "B"), placement(3, 3, "C")]}
    snapshot = {"placements_before": [placement(1, .25, "A")]}
    enriched = attach_placements_to_model(copy.deepcopy(model), copy.deepcopy(state))
    enriched = enrich_model_with_placement_diagnostics(enriched, state, snapshot)
    old = build_geometry_dynamic_payload(enriched)
    new = build_geometry_dynamic_payload_from_state(model, state, snapshot)
    assert new == old


def test_outbound_projection_and_script_safety():
    model = {"cells": [cell(1)]}
    state = {"placements": [placement(1)]}
    payload = build_geometry_dynamic_payload_from_state(model, state, None, None, {"1|1|1": "\nРО: </script>"})
    assert "РО: </script>" in payload["1|1|1"]["tooltip"]
    html = compose_geometry_layers("const state = __WAREHOUSE_DYNAMIC_STATE__;", payload)
    assert "\\u003c/script\\u003e" in html


def test_active_wrapper_has_no_legacy_enrichment_or_copy():
    tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
    names = {"build_geometry_dynamic_layer_cached", "_build_geometry_dynamic_layer_direct", "render_geometry_map_view"}
    source = "\n".join(ast.unparse(node) for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names)
    forbidden = {"deepcopy", "attach_placements_to_model", "enrich_model_with_placement_diagnostics", "enrich_model_with_outbound_diagnostics"}
    called = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))}
    assert not (called & forbidden)
    assert "json.dumps(model" not in source
    assert "st.cache_data.clear" not in Path(app.__file__).read_text(encoding="utf-8")
