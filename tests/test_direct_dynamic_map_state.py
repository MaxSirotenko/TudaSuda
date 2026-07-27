from __future__ import annotations

import ast
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import virtual_warehouse_app as app
import warehouse_geometry_render_layers as layers
from warehouse_geometry_render_layers import build_geometry_dynamic_payload_from_state


def _model(count=5):
    return {
        "model_id": "direct-test",
        "cells": [
            {
                "row_number": "1", "cell_number": str(index + 1), "tier": "1",
                "capacity_pallets": 2, "storage_type": "deep_lane" if index == 1 else "normal",
                "deep_lane_width": 2, "weight_zone": "heavy",
                "x_min": index, "x_max": index + 1, "y_min": 0, "y_max": 1,
            }
            for index in range(count)
        ],
        "rows": [], "aisles": [], "roads": [], "cross_aisles": [],
    }


def _placement(cell="1", qty=1, sku="SKU-A", **extra):
    return {
        "cell_key": f"1|{cell}|1", "row_number": "1", "cell_number": cell, "tier": "1",
        "occupied_capacity_pallets": qty, "sku_key": sku, "sku_code": sku,
        "sku_name": f"Товар {sku}", "characteristic_name": 'Красный "тест"\nстрока',
        "quantity": qty, "source": "receipt", "calculated_zone": "heavy",
        "receipt_numbers": ["ПР-1"], "receipt_line_ids": ["line-1"],
        "placement_reason_text": "Выбрано безопасно </script>", **extra,
    }


def test_inputs_are_immutable_and_payload_is_sparse_without_geometry():
    model = _model(1000)
    state = {"placements": [_placement("2"), _placement("10")]}
    snapshot = {"placements_before": [_placement("2", .5)]}
    settings = {"show_cell_labels": True, "colors": {"selected_cell_color": "#123456"}}
    originals = copy.deepcopy((model, state, snapshot, settings))
    payload = build_geometry_dynamic_payload_from_state(model, state, snapshot, settings)
    assert (model, state, snapshot, settings) == originals
    assert set(payload) == {"1|2|1", "1|10|1"}
    encoded = str(payload)
    assert all(field not in encoded for field in ("x_min", "x_max", "rows", "aisles", "roads", "cross_aisles", "'cells'"))


def test_empty_state_is_empty_and_interesting_outbound_cell_is_preserved():
    assert build_geometry_dynamic_payload_from_state(_model(), {}) == {}
    payload = build_geometry_dynamic_payload_from_state(
        _model(), {}, outbound_context={"statuses": {"1|3|1": "picked"}, "diagnostics": {"1|3|1": "РО"}},
    )
    assert set(payload) == {"1|3|1"}
    assert payload["1|3|1"]["outbound_status"] == "picked"
    assert payload["1|3|1"]["outbound_diagnostic"] == "РО"


@pytest.mark.parametrize("qty,status", [(1, "partial"), (2, "full"), (3, "overfilled")])
def test_occupancy_colors_and_selected_cell_match_legacy_projection(qty, status):
    state = {"placements": [_placement("2", qty)]}
    settings = {"selected_cell_key": "1|2|1"}
    direct = build_geometry_dynamic_payload_from_state(_model(), state, label_settings=settings)["1|2|1"]
    enriched_cell = dict(_model()["cells"][1], occupied_capacity_pallets=qty,
                         placements=state["placements"], placement_category="heavy")
    legacy = layers.build_geometry_dynamic_payload({"cells": [enriched_cell]}, settings)["1|2|1"]
    for field in ("occupied", "capacity", "occupancy_status", "fill_color", "border", "label", "placements"):
        assert direct.get(field) == legacy.get(field)
    assert direct["occupancy_status"] == status


def test_tooltip_preserves_order_snapshot_diagnostics_and_text_safety():
    state = {"placements": [_placement("1", 2, "SKU-B"), _placement("1", 1, "SKU-A")]}
    snapshot = {"placements_before": [_placement("1", 1, "SKU-B")]}
    tooltip = build_geometry_dynamic_payload_from_state(_model(), state, snapshot)["1|1|1"]["tooltip"]
    expected = ["Ячейка:", "Ряд:", "Тип ряда:", "Весовая зона:", "Вместимость:", "Номенклатура:",
                "Характеристика:", "sku_key:", "Количество:", "Приходные ордера:", "receipt_line_ids:",
                "Было до: 1", "Добавлено: 1", "Стало после: 2", "Способ размещения: смешанная занятость", "Причина:"]
    assert all(value in tooltip for value in expected)
    assert tooltip.index("sku_key: SKU-B") < tooltip.index("sku_key: SKU-A")
    assert "Рус" not in tooltip  # no accidental replacement/translation
    html = layers.compose_geometry_layers(layers.DYNAMIC_STATE_MARKER, {"cell": {"tooltip": tooltip}})
    assert "\\u003c/script\\u003e" in html and "Красный" in html and "\\n" in html


def test_direct_builder_uses_one_placement_aggregation_and_one_cells_iteration(monkeypatch):
    model = _model(10_000)
    state = {"placements": [_placement(str(index + 1), sku=f"SKU-{index}") for index in range(20)] +
                           [_placement("1", sku=f"EXTRA-{index}") for index in range(10)]}
    aggregate_calls = []
    real = layers.summarize_placements_by_cell
    monkeypatch.setattr(layers, "summarize_placements_by_cell", lambda placements: aggregate_calls.append(len(placements)) or real(placements))
    payload = build_geometry_dynamic_payload_from_state(model, state)
    assert aggregate_calls == [30]
    assert len(payload) == 20


def test_cached_wrapper_hits_without_snapshot_outbound_or_builder(monkeypatch):
    app.build_geometry_dynamic_layer_cached.clear()
    calls = []
    monkeypatch.setattr(app, "load_pre_placement_snapshot", lambda model: calls.append("snapshot") or (None, None))
    monkeypatch.setattr(app, "build_outbound_tooltips_by_cell", lambda model, state: calls.append("outbound") or {})
    monkeypatch.setattr(app, "build_geometry_dynamic_payload_from_state", lambda *args, **kwargs: calls.append("builder") or {})
    token = ("direct-test", 1, 1, 1, 1)
    assert app.build_geometry_dynamic_layer_cached(_model(), {"placements": [_placement()]}, token, {}, 2) == {}
    assert app.build_geometry_dynamic_layer_cached({"changed": True}, {"placements": [_placement("2")]}, token, {"changed": True}, 2) == {}
    assert calls == ["snapshot", "outbound", "builder"]


def test_active_paths_have_no_legacy_enrichment_or_deepcopy():
    tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
    functions = {node.name: ast.unparse(node) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("build_geometry_dynamic_layer_cached", "build_geometry_dynamic_layer_direct", "render_geometry_map_view"):
        source = functions[name]
        assert "copy.deepcopy" not in source
        assert "attach_placements_to_model" not in source
        assert "enrich_model_with_placement_diagnostics" not in source
        assert "enrich_model_with_outbound_diagnostics" not in source
        assert "json.dumps(model" not in source
    assert "st.cache_data.clear" not in Path(app.__file__).read_text(encoding="utf-8")
