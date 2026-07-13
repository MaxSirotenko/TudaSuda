import copy
import json

import pytest

from warehouse_row_settings import (
    apply_row_settings_transaction,
    build_row_settings_draft,
    sync_row_settings_to_model,
)


def _cell(row, number, *, direction="bottom_to_top", capacity=1, storage="normal", x_min=0.0, x_max=0.8):
    return {
        "row_number": str(row),
        "cell_number": str(number),
        "tier": "1",
        "row_order": float(row),
        "cell_direction": direction,
        "weight_zone": "unassigned",
        "storage_type": storage,
        "deep_lane_width": capacity,
        "capacity_pallets": capacity,
        "x_min": x_min,
        "x_max": x_max,
        "x_center": (x_min + x_max) / 2,
        "y_min": float(number - 1),
        "y_max": float(number),
        "y_center": float(number) - 0.5,
    }


def _model():
    cells = [_cell(152, idx, x_min=0.0, x_max=0.8) for idx in range(1, 4)]
    cells += [_cell(153, idx, x_min=4.2, x_max=5.0) for idx in range(1, 4)]
    return {
        "model_type": "excel_rows_cells_aisles_geometry",
        "model_id": "test",
        "settings": {"cell_width_m": 0.8},
        "rows": [
            {"row_number": "152", "row_order": 1, "cell_direction": "bottom_to_top", "weight_zone": "unassigned", "row_storage_type": "normal", "deep_lane_width": 1, "x_min": 0.0, "x_max": 0.8, "y_min": 0, "y_max": 3},
            {"row_number": "153", "row_order": 2, "cell_direction": "bottom_to_top", "weight_zone": "unassigned", "row_storage_type": "normal", "deep_lane_width": 1, "x_min": 4.2, "x_max": 5.0, "y_min": 0, "y_max": 3},
        ],
        "cells": cells,
        "base_cells": copy.deepcopy(cells),
        "row_settings": [],
        "aisles": [{"row_from": "152", "row_to": "153", "aisle_width_m": 3.4, "x_min": 0.8, "x_max": 4.2}],
        "roads": [{"road_type": "top"}],
        "navigation_nodes": [{"node_id": "row:152", "row_number": "152"}, {"node_id": "row:153", "row_number": "153"}, {"node_id": "road:top"}],
        "placements": [],
    }


def _edited(model):
    return build_row_settings_draft(model).to_dict(orient="records")


def test_build_draft_prefers_rows_over_old_settings_and_cells():
    model = _model()
    model["row_settings"] = [{"row_number": "152", "cell_direction": "top_to_bottom", "weight_zone": "heavy"}]
    model["cells"][0]["cell_direction"] = "top_to_bottom"
    draft = build_row_settings_draft(model)
    row = draft[draft["row_number"] == "152"].iloc[0]
    assert row["cell_direction"] == "bottom_to_top"
    assert row["weight_zone"] == "unassigned"


def test_sync_rows_to_cells_base_cells_and_row_settings():
    model = _model()
    model["rows"][0].update({"cell_direction": "top_to_bottom", "weight_zone": "heavy", "row_storage_type": "deep_lane", "deep_lane_width": 4, "row_group": "A", "side": "Лево", "comment": "тест"})
    sync_row_settings_to_model(model)
    for group in ("cells", "base_cells"):
        row_cells = [cell for cell in model[group] if cell["row_number"] == "152"]
        assert all(cell["cell_direction"] == "top_to_bottom" for cell in row_cells)
        assert all(cell["weight_zone"] == "heavy" for cell in row_cells)
        assert all(cell["storage_type"] == "deep_lane" for cell in row_cells)
        assert all(cell["capacity_pallets"] == 4 for cell in row_cells)
        assert all(len(cell["physical_slots"]) == 4 for cell in row_cells)
    setting = next(row for row in model["row_settings"] if row["row_number"] == "152")
    assert setting["cell_direction"] == "top_to_bottom"
    assert setting["weight_zone"] == "heavy"


def test_apply_multiple_rows_transaction_and_direction_serialization():
    model = _model()
    edited = _edited(model)
    for row in edited:
        if row["row_number"] in {"152", "153"}:
            row["cell_direction"] = "top_to_bottom"
            row["weight_zone"] = "medium"
    updated, messages = apply_row_settings_transaction(model, edited)
    assert not any(message.startswith("Ошибка:") for message in messages)
    assert all(row["cell_direction"] == "top_to_bottom" for row in updated["rows"])
    loaded = json.loads(json.dumps(updated, ensure_ascii=False))
    assert all(row["cell_direction"] == "top_to_bottom" for row in loaded["rows"])
    assert all(cell["cell_direction"] == "top_to_bottom" for cell in loaded["cells"])


def test_transaction_rolls_back_on_invalid_row():
    model = _model()
    edited = _edited(model)
    edited[0]["cell_direction"] = "sideways"
    updated, messages = apply_row_settings_transaction(model, edited)
    assert any(message.startswith("Ошибка:") for message in messages)
    assert updated == model


def test_transaction_blocks_capacity_below_occupied():
    model = _model()
    model["placements"] = [{"cell_key": "152|1|1", "qty_pallets": 2}]
    edited = _edited(model)
    edited[0]["row_storage_type"] = "normal"
    edited[0]["cell_capacity_pallets"] = 1
    updated, messages = apply_row_settings_transaction(model, edited)
    assert any("занято 2" in message for message in messages)
    assert updated == model


def test_normal_to_deep_lane_deep_lane_to_normal_and_no_cumulative_width():
    model = _model()
    edited = _edited(model)
    edited[0]["row_storage_type"] = "deep_lane"
    edited[0]["cell_capacity_pallets"] = 4
    deep, messages = apply_row_settings_transaction(model, edited)
    assert not any(message.startswith("Ошибка:") for message in messages)
    row = next(row for row in deep["rows"] if row["row_number"] == "152")
    next_row = next(row for row in deep["rows"] if row["row_number"] == "153")
    aisle = deep["aisles"][0]
    assert row["x_max"] - row["x_min"] == pytest.approx(3.2)
    assert next_row["x_min"] == pytest.approx(6.6)
    assert aisle["x_min"] == pytest.approx(3.2)
    assert aisle["x_max"] == pytest.approx(6.6)
    assert aisle["aisle_width_m"] == pytest.approx(3.4)
    assert all(len(cell["physical_slots"]) == 4 for cell in deep["cells"] if cell["row_number"] == "152")

    same, messages = apply_row_settings_transaction(deep, build_row_settings_draft(deep).to_dict(orient="records"))
    same_row = next(row for row in same["rows"] if row["row_number"] == "152")
    same_next_row = next(row for row in same["rows"] if row["row_number"] == "153")
    assert same_row["x_max"] - same_row["x_min"] == pytest.approx(3.2)
    assert same_next_row["x_min"] == pytest.approx(6.6)

    edited_width = build_row_settings_draft(same).to_dict(orient="records")
    edited_width[0]["cell_capacity_pallets"] = 3
    deep_three, messages = apply_row_settings_transaction(same, edited_width)
    deep_three_row = next(row for row in deep_three["rows"] if row["row_number"] == "152")
    deep_three_next = next(row for row in deep_three["rows"] if row["row_number"] == "153")
    assert deep_three_row["x_max"] - deep_three_row["x_min"] == pytest.approx(2.4)
    assert deep_three_next["x_min"] == pytest.approx(5.8)

    edited_back = build_row_settings_draft(deep_three).to_dict(orient="records")
    edited_back[0]["row_storage_type"] = "normal"
    edited_back[0]["cell_capacity_pallets"] = 1
    normal, messages = apply_row_settings_transaction(deep_three, edited_back)
    normal_row = next(row for row in normal["rows"] if row["row_number"] == "152")
    normal_next = next(row for row in normal["rows"] if row["row_number"] == "153")
    assert normal_row["x_max"] - normal_row["x_min"] == pytest.approx(0.8)
    assert normal_next["x_min"] == pytest.approx(4.2)
    assert all(cell["capacity_pallets"] == 1 for cell in normal["cells"] if cell["row_number"] == "152")
    assert all(cell["physical_slots"] == [] for cell in normal["cells"] if cell["row_number"] == "152")


def test_deep_lane_width_five_moves_following_rows_and_preserves_directions():
    model = _model()
    model["rows"][0]["cell_direction"] = "top_to_bottom"
    model["rows"][1]["cell_direction"] = "top_to_bottom"
    edited = _edited(model)
    edited[0]["row_storage_type"] = "deep_lane"
    edited[0]["cell_capacity_pallets"] = 5
    updated, messages = apply_row_settings_transaction(model, edited)
    assert not any(message.startswith("Ошибка:") for message in messages)
    row_152 = next(row for row in updated["rows"] if row["row_number"] == "152")
    row_153 = next(row for row in updated["rows"] if row["row_number"] == "153")
    assert row_152["x_min"] == pytest.approx(0.0)
    assert row_152["x_max"] == pytest.approx(4.0)
    assert row_153["x_min"] == pytest.approx(7.4)
    assert updated["aisles"][0]["aisle_width_m"] == pytest.approx(3.4)
    assert row_152["cell_direction"] == "top_to_bottom"
    assert row_153["cell_direction"] == "top_to_bottom"
    assert all(cell["cell_direction"] == "top_to_bottom" for cell in updated["cells"] if cell["row_number"] in {"152", "153"})
