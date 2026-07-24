import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pandas as pd
import pytest
import streamlit as streamlit

import virtual_warehouse_app
from warehouse_cross_aisles import (
    apply_cross_aisles_transaction,
    create_cross_aisle_settings_state,
    ensure_cross_aisles,
    reset_cross_aisle_settings_state,
    update_cross_aisle_settings_state,
)
from warehouse_geometry_model import build_geometry_html


def model(direction="bottom_to_top", deep=False):
    cells = []
    for number in range(1, 5):
        cells.append({
            "row_number": "152", "cell_number": str(number), "tier": "1",
            "cell_key": f"152|{number}|1", "code": f"152-{number}-1",
            "x_min": 0.0, "x_max": 2.0 if deep else 1.0, "x_center": 1.0 if deep else .5,
            "y_min": number - 1, "y_max": number, "y_center": number - .5,
            "length_m": 1.0, "capacity_pallets": 2 if deep else 1,
            "storage_type": "deep_lane" if deep else "normal", "deep_lane_width": 2 if deep else 1,
            "physical_slots": ([{"slot_index": 1, "x_min": 0, "x_max": 1, "y_min": number - 1, "y_max": number}, {"slot_index": 2, "x_min": 1, "x_max": 2, "y_min": number - 1, "y_max": number}] if deep else []),
        })
    return {
        "model_id": "test", "settings": {"cell_length_m": 1.0, "top_road_width_m": 2},
        "rows": [{"row_number": "152", "cell_direction": direction, "x_min": 0, "x_max": 2 if deep else 1, "top_offset_m": 0, "bottom_offset_m": 0}],
        "cells": cells, "base_cells": copy.deepcopy(cells), "aisles": [],
        "roads": [{"road_type": "top", "width_m": 2, "x_min": 0, "x_max": 1, "y_min": 4, "y_max": 6}], "navigation_nodes": [], "navigation_edges": [],
        "placements": [{"sku_key": "sku", "row_number": "152", "cell_number": "3", "tier": "1", "cell_key": "152|3|1", "qty_pallets": 1, "qty_units": 17}],
    }


def aisle(after="2", width=2, row="152"):
    return {"row_number": row, "after_cell_number": after, "width_cells": width, "comment": "тест"}


def test_legacy_model_gets_empty_collection():
    assert ensure_cross_aisles({})["cross_aisles"] == []


def test_one_aisle_preserves_addresses_capacity_and_placements():
    original = model()
    updated, errors = apply_cross_aisles_transaction(original, [aisle()])
    assert not errors
    assert [cell["y_min"] for cell in updated["cells"]] == [0, 1, 4, 5]
    assert [(c["cell_number"], c["cell_key"], c["capacity_pallets"]) for c in updated["cells"]] == [(c["cell_number"], c["cell_key"], c["capacity_pallets"]) for c in original["cells"]]
    assert updated["placements"] == original["placements"]
    assert updated["placements"][0]["qty_units"] == 17
    assert len(updated["cells"]) == 4
    assert all(cell.get("aisle_type") != "cross_aisle" for cell in updated["cells"])


def test_multiple_aisles_accumulate_shift():
    updated, errors = apply_cross_aisles_transaction(model(), [aisle("1", 1), aisle("3", 2)])
    assert not errors
    assert [cell["y_min"] for cell in updated["cells"]] == [0, 2, 3, 6]
    assert len(updated["cross_aisles"]) == 2


@pytest.mark.parametrize("bad,error", [
    (aisle(row="404"), "не существует"),
    (aisle(after="99"), "не существует"),
    (aisle(width=0), "целым числом"),
    (aisle(width=1.5), "целым числом"),
    (aisle(after="4"), "последней физической"),
])
def test_invalid_draft_blocks_whole_transaction(bad, error):
    original = model()
    updated, errors = apply_cross_aisles_transaction(original, [aisle("1"), bad])
    assert updated is original
    assert any(error in message for message in errors)
    assert "cross_aisles" not in original


def test_duplicate_position_is_rejected():
    _, errors = apply_cross_aisles_transaction(model(), [aisle(), aisle()])
    assert any("дублируется" in error for error in errors)


@pytest.mark.parametrize("direction,after,expected", [
    ("bottom_to_top", "2", [0, 1, 4, 5]),
    ("top_to_bottom", "3", [5, 4, 1, 0]),
])
def test_physical_direction_controls_gap(direction, after, expected):
    updated, errors = apply_cross_aisles_transaction(model(direction), [aisle(after)])
    assert not errors
    assert [cell["y_min"] for cell in updated["cells"]] == expected


def test_deep_lane_has_one_full_width_aisle_and_slots_move_with_cell():
    updated, errors = apply_cross_aisles_transaction(model(deep=True), [aisle()])
    assert not errors
    assert len(updated["cross_aisles"]) == 1
    assert (updated["cross_aisles"][0]["x_min"], updated["cross_aisles"][0]["x_max"]) == (0, 2)
    assert updated["cells"][2]["physical_slots"][0]["y_min"] == 4


def test_draft_is_isolated_and_cancel_restores_baseline():
    source = model()
    state = create_cross_aisle_settings_state(source)
    changed = update_cross_aisle_settings_state(state, [aisle()])
    assert "cross_aisles" not in source
    assert changed["draft"] == [aisle()]
    assert reset_cross_aisle_settings_state(changed)["draft"] == []


def test_error_keeps_user_draft_and_model_unchanged():
    source = model()
    state = update_cross_aisle_settings_state(create_cross_aisle_settings_state(source), [aisle(width=0)])
    updated, errors = apply_cross_aisles_transaction(source, state["draft"])
    assert errors and updated is source and state["draft"][0]["width_cells"] == 0


def test_cross_aisle_editor_uses_compatible_dataframe_types(monkeypatch):
    captured = []

    class FakeStreamlit:
        column_config = streamlit.column_config

        def __init__(self):
            self.session_state = {}

        def __getattr__(self, name):
            if name == "form":
                return lambda *args, **kwargs: nullcontext()
            if name == "columns":
                button = SimpleNamespace(form_submit_button=lambda *args, **kwargs: False)
                return lambda count: [button] * count
            if name == "data_editor":
                def data_editor(frame, **kwargs):
                    captured.append(frame.copy())
                    return frame
                return data_editor
            return lambda *args, **kwargs: None

    fake_st = FakeStreamlit()
    monkeypatch.setattr(virtual_warehouse_app, "st", fake_st)
    source = model()

    virtual_warehouse_app.render_cross_aisle_settings_editor(source)
    empty_draft = captured[-1].rename(columns={
        "Ряд": "row_number",
        "После ячейки": "after_cell_number",
        "Ширина, ячеек": "width_cells",
        "Ширина, м": "width_m",
        "Комментарий": "comment",
    })
    assert empty_draft.empty
    assert isinstance(empty_draft["row_number"].dtype, pd.StringDtype)
    assert isinstance(empty_draft["after_cell_number"].dtype, pd.StringDtype)
    assert isinstance(empty_draft["comment"].dtype, pd.StringDtype)
    assert empty_draft["width_cells"].dtype == pd.Int64Dtype()
    assert empty_draft["width_m"].dtype == pd.Float64Dtype()

    source["settings"]["cell_length_m"] = 1.2
    fake_st.session_state["cross_aisle_settings_state"] = {
        "model_id": "test",
        "editor_revision": 0,
        "baseline": [],
        "draft": [aisle(after="20", width=3) | {"comment": None}],
    }
    virtual_warehouse_app.render_cross_aisle_settings_editor(source)
    filled_draft = captured[-1]
    assert filled_draft.loc[0, "Ряд"] == "152"
    assert filled_draft.loc[0, "После ячейки"] == "20"
    assert filled_draft.loc[0, "Комментарий"] == ""
    assert filled_draft.loc[0, "Ширина, м"] == pytest.approx(3.6)


def test_navigation_nodes_edges_and_visualization_have_no_fake_address():
    updated, errors = apply_cross_aisles_transaction(model(), [aisle()])
    assert not errors
    assert {node["node_id"] for node in updated["navigation_nodes"]} == {"cross:152:2:left", "cross:152:2:center", "cross:152:2:right"}
    assert len(updated["navigation_edges"]) == 4
    html = build_geometry_html(updated)
    assert "Поперечный проезд" in html
    assert len(updated["cells"]) == 4
