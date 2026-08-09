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
    relayout_cross_aisle_rows,
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
    ("top_to_bottom", "3", [5, 4, 3, 0]),
])
def test_physical_direction_controls_gap(direction, after, expected):
    updated, errors = apply_cross_aisles_transaction(model(direction), [aisle(after)])
    assert not errors
    assert [cell["y_min"] for cell in updated["cells"]] == expected


@pytest.mark.parametrize("direction,expected", [
    ("bottom_to_top", [0, 1, 2, 3]),
    ("top_to_bottom", [3, 2, 1, 0]),
])
def test_relayout_without_cross_aisles_preserves_direction_and_footprint(direction, expected):
    source = model(direction)
    relayout_cross_aisle_rows(source, {"152"})

    assert [cell["y_min"] for cell in source["cells"]] == expected
    assert (source["rows"][0]["y_min"], source["rows"][0]["y_max"]) == (0, 4)


def test_cross_aisle_elsewhere_does_not_touch_row_152_regression_geometry():
    source = model("top_to_bottom")
    source["rows"].append({"row_number": "153", "cell_direction": "bottom_to_top", "x_min": 2, "x_max": 3, "top_offset_m": 0, "bottom_offset_m": 0})
    other_cells = []
    for number in range(1, 5):
        other_cells.append({
            "row_number": "153", "cell_number": str(number), "tier": "1",
            "cell_key": f"153|{number}|1", "x_min": 2, "x_max": 3,
            "y_min": number - 1, "y_max": number, "y_center": number - .5,
            "length_m": 1, "physical_slots": [],
        })
    source["cells"].extend(other_cells)
    source["base_cells"].extend(copy.deepcopy(other_cells))
    # This is the factual row-152 shape before applying an aisle in another row.
    for cell in source["cells"] + source["base_cells"]:
        if cell["row_number"] == "152":
            number = int(cell["cell_number"])
            cell.update({"y_min": 4 - number, "y_max": 5 - number, "y_center": 4.5 - number})
    before = copy.deepcopy([cell for cell in source["cells"] if cell["row_number"] == "152"])

    updated, errors = apply_cross_aisles_transaction(source, [aisle("2", 1, "153")])

    assert not errors
    assert [cell for cell in updated["cells"] if cell["row_number"] == "152"] == before


@pytest.mark.parametrize("direction,after,expected_gap", [
    ("bottom_to_top", "2", (2, 4)),
    ("top_to_bottom", "2", (2, 4)),
])
def test_cross_aisle_after_cell_is_on_traversal_side(direction, after, expected_gap):
    updated, errors = apply_cross_aisles_transaction(model(direction), [aisle(after)])
    assert not errors
    cells = {cell["cell_number"]: cell for cell in updated["cells"]}
    cross = updated["cross_aisles"][0]
    assert (cross["y_min"], cross["y_max"]) == expected_gap
    if direction == "bottom_to_top":
        assert cross["y_min"] == cells[after]["y_max"]
    else:
        assert cross["y_max"] == cells[after]["y_min"]


def test_direction_has_equal_span_multiple_aisles_are_ordered_and_relayout_is_idempotent():
    draft = [aisle("1", 1), aisle("3", 2)]
    bottom, errors = apply_cross_aisles_transaction(model("bottom_to_top"), draft)
    top, top_errors = apply_cross_aisles_transaction(model("top_to_bottom"), draft)
    assert not errors and not top_errors
    assert bottom["rows"][0]["y_max"] == top["rows"][0]["y_max"] == 7
    assert [cell["y_min"] for cell in top["cells"]] == [6, 4, 3, 0]
    assert [(item["after_cell_number"], item["y_min"], item["y_max"]) for item in top["cross_aisles"]] == [("1", 5, 6), ("3", 1, 3)]

    repeated, repeated_errors = apply_cross_aisles_transaction(top, draft)
    assert not repeated_errors
    assert repeated["rows"] == top["rows"]
    assert repeated["cells"] == top["cells"]
    assert repeated["cross_aisles"] == top["cross_aisles"]


def test_149_and_150_cell_rows_differ_by_exactly_one_cell_length():
    spans = []
    for direction, count in (("top_to_bottom", 149), ("bottom_to_top", 150)):
        sample = model(direction)
        sample["settings"]["cell_length_m"] = 1.2
        template = sample["cells"][0]
        sample["cells"] = [template | {"cell_number": str(number), "cell_key": f"152|{number}|1", "length_m": 1.2} for number in range(1, count + 1)]
        sample["base_cells"] = copy.deepcopy(sample["cells"])
        relayout_cross_aisle_rows(sample, {"152"})
        spans.append(sample["rows"][0]["y_max"] - sample["rows"][0]["y_min"])
        if direction == "top_to_bottom":
            assert sample["cells"][0]["y_min"] == pytest.approx(177.6)
            assert sample["cells"][-1]["y_min"] == pytest.approx(0)
    assert spans == pytest.approx([178.8, 180.0])


def test_deep_lane_has_one_full_width_aisle_and_slots_move_with_cell():
    updated, errors = apply_cross_aisles_transaction(model(deep=True), [aisle()])
    assert not errors
    assert len(updated["cross_aisles"]) == 1
    assert (updated["cross_aisles"][0]["x_min"], updated["cross_aisles"][0]["x_max"]) == (0, 2)
    assert updated["cells"][2]["physical_slots"][0]["y_min"] == 4


def test_top_to_bottom_deep_lane_slots_follow_corrected_parent_cells():
    updated, errors = apply_cross_aisles_transaction(model("top_to_bottom", deep=True), [aisle()])
    assert not errors
    for cell in updated["cells"]:
        assert all((slot["y_min"], slot["y_max"]) == (cell["y_min"], cell["y_max"]) for slot in cell["physical_slots"])


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
    source = model()
    source["navigation_nodes"] = [
        {"node_id": "row:152:bottom", "node_type": "row_bottom_entry", "row_number": "152", "x": .5, "y": 0},
        {"node_id": "row:152:top", "node_type": "row_top_entry", "row_number": "152", "x": .5, "y": 4},
    ]
    source["navigation_edges"] = [{"from_node": "row:152:bottom", "to_node": "row:152:top", "distance_m": 4, "edge_type": "row_walk"}]
    updated, errors = apply_cross_aisles_transaction(source, [aisle()])
    assert not errors
    nodes = {node["node_id"]: node for node in updated["navigation_nodes"]}
    assert {"cross:152:2:left", "cross:152:2:center", "cross:152:2:right"} < set(nodes)
    assert (nodes["row:152:bottom"]["y"], nodes["row:152:top"]["y"]) == (0, 6)
    assert next(edge for edge in updated["navigation_edges"] if edge["edge_type"] == "row_walk")["distance_m"] == 6
    assert all(edge["distance_m"] >= 0 for edge in updated["navigation_edges"])
    assert len(updated["navigation_edges"]) == 5
    html = build_geometry_html(updated)
    assert "Поперечный проезд" in html
    assert len(updated["cells"]) == 4
