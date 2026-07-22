import copy
import json

import pytest
import pandas as pd

from warehouse_row_settings import (
    apply_row_settings_transaction,
    build_row_settings_draft,
    changed_row_numbers,
    create_row_settings_state,
    reset_row_settings_state,
    sync_row_settings_to_model,
    update_row_settings_state,
)
from warehouse_geometry_model import GeometrySettings, build_geometry_html, build_geometry_model


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


def _direction_model():
    rows = []
    cells = []
    for idx, row_number in enumerate((152, 155, 157)):
        x_min = idx * 4.2
        x_max = x_min + 0.8
        rows.append({
            "row_number": str(row_number),
            "row_order": idx + 1,
            "cell_direction": "bottom_to_top",
            "weight_zone": "unassigned",
            "row_storage_type": "normal",
            "deep_lane_width": 1,
            "x_min": x_min,
            "x_max": x_max,
            "y_min": 0,
            "y_max": 3.6,
        })
        for cell_number in (1, 2, 3):
            cell = _cell(row_number, cell_number, x_min=x_min, x_max=x_max)
            cell["length_m"] = 1.2
            cell["y_min"] = (cell_number - 1) * 1.2
            cell["y_max"] = cell_number * 1.2
            cell["y_center"] = cell["y_min"] + 0.6
            cells.append(cell)
    return {
        "model_type": "excel_rows_cells_aisles_geometry",
        "model_id": "direction-test",
        "settings": {"cell_width_m": 0.8, "cell_length_m": 1.2},
        "rows": rows,
        "cells": cells,
        "base_cells": copy.deepcopy(cells),
        "row_settings": [],
        "aisles": [
            {"row_from": "152", "row_to": "155", "aisle_width_m": 3.4, "x_min": 0.8, "x_max": 4.2},
            {"row_from": "155", "row_to": "157", "aisle_width_m": 3.4, "x_min": 5.0, "x_max": 8.4},
        ],
        "roads": [],
        "navigation_nodes": [],
        "placements": [],
    }


def _row_cell_centers(model, row_number, group="cells"):
    row_cells = [cell for cell in model[group] if cell["row_number"] == str(row_number)]
    return {cell["cell_number"]: cell["y_center"] for cell in row_cells}


def test_cell_direction_recalculates_vertical_coordinates_for_target_rows():
    model = _direction_model()
    edited = _edited(model)
    for row in edited:
        row["cell_direction"] = "top_to_bottom"
    top, messages = apply_row_settings_transaction(model, edited)
    assert not any(message.startswith("Ошибка:") for message in messages)
    for row_number in (152, 155, 157):
        centers = _row_cell_centers(top, row_number)
        base_centers = _row_cell_centers(top, row_number, "base_cells")
        assert centers["1"] > centers["3"]
        assert base_centers["1"] > base_centers["3"]

    repeated, messages = apply_row_settings_transaction(top, build_row_settings_draft(top).to_dict(orient="records"))
    for row_number in (152, 155, 157):
        assert _row_cell_centers(repeated, row_number) == _row_cell_centers(top, row_number)

    edited_back = build_row_settings_draft(repeated).to_dict(orient="records")
    for row in edited_back:
        if row["row_number"] == "157":
            row["cell_direction"] = "bottom_to_top"
    bottom, messages = apply_row_settings_transaction(repeated, edited_back)
    centers_157 = _row_cell_centers(bottom, 157)
    assert centers_157["1"] < centers_157["3"]
    assert next(row for row in bottom["rows"] if row["row_number"] == "155")["cell_direction"] == "top_to_bottom"


def test_old_model_defaults_vertical_offsets_to_zero():
    model = _model()
    draft = build_row_settings_draft(model)
    updated = sync_row_settings_to_model(model)

    assert set(draft["top_offset_cells"]) == {0}
    assert set(draft["bottom_offset_cells"]) == {0}
    assert all(row["top_offset_m"] == 0 for row in updated["rows"])
    assert all(row["bottom_offset_m"] == 0 for row in updated["rows"])


def test_top_offset_shifts_cells_and_bottom_offset_extends_row_only():
    model = _model()
    original_numbers = [cell["cell_number"] for cell in model["cells"] if cell["row_number"] == "152"]
    original_capacity = sum(cell["capacity_pallets"] for cell in model["cells"] if cell["row_number"] == "152")
    model["placements"] = [{"cell_key": "152|1|1", "row_number": "152", "cell_number": "1", "tier": "1", "qty_pallets": 1}]
    edited = _edited(model)
    row = next(item for item in edited if item["row_number"] == "152")
    row["top_offset_cells"] = 2
    row["bottom_offset_cells"] = 3

    updated, messages = apply_row_settings_transaction(model, edited)
    cells = [cell for cell in updated["cells"] if cell["row_number"] == "152"]
    base_cells = [cell for cell in updated["base_cells"] if cell["row_number"] == "152"]
    updated_row = next(item for item in updated["rows"] if item["row_number"] == "152")

    assert not any(message.startswith("Ошибка:") for message in messages)
    assert min(cell["y_min"] for cell in cells) == 2
    assert min(cell["y_min"] for cell in base_cells) == 2
    assert updated_row["y_max"] == 8
    assert (updated_row["top_offset_m"], updated_row["bottom_offset_m"]) == (2, 3)
    assert [cell["cell_number"] for cell in cells] == original_numbers
    assert len(cells) == len(original_numbers)
    assert sum(cell["capacity_pallets"] for cell in cells) == original_capacity
    assert updated["placements"] == model["placements"]


@pytest.mark.parametrize("direction", ["bottom_to_top", "top_to_bottom"])
def test_offsets_have_same_physical_bounds_for_both_directions(direction):
    model = _model()
    edited = _edited(model)
    row = next(item for item in edited if item["row_number"] == "152")
    row.update({"cell_direction": direction, "top_offset_cells": 2, "bottom_offset_cells": 1})

    updated, _ = apply_row_settings_transaction(model, edited)
    cells = [cell for cell in updated["cells"] if cell["row_number"] == "152"]
    updated_row = next(item for item in updated["rows"] if item["row_number"] == "152")

    assert sorted(cell["y_min"] for cell in cells) == [2, 3, 4]
    assert updated_row["y_min"] == 0
    assert updated_row["y_max"] == 6


def test_different_rows_keep_individual_offsets_and_bulk_style_edit_changes_selected_only():
    model = _model()
    edited = _edited(model)
    for row in edited:
        if row["row_number"] == "152":
            row["top_offset_cells"] = 1
            row["bottom_offset_cells"] = 2

    updated, _ = apply_row_settings_transaction(model, edited)
    rows = {row["row_number"]: row for row in updated["rows"]}

    assert (rows["152"]["top_offset_cells"], rows["152"]["bottom_offset_cells"]) == (1, 2)
    assert (rows["153"]["top_offset_cells"], rows["153"]["bottom_offset_cells"]) == (0, 0)


def test_invalid_offset_rolls_back_entire_transaction():
    model = _model()
    original = copy.deepcopy(model)
    edited = _edited(model)
    edited[0]["top_offset_cells"] = -1
    edited[1]["bottom_offset_cells"] = 2

    updated, messages = apply_row_settings_transaction(model, edited)

    assert updated == original
    assert any(message.startswith("Ошибка:") for message in messages)


def test_offsets_survive_json_serialization():
    model = _model()
    edited = _edited(model)
    edited[0]["top_offset_cells"] = 4
    edited[0]["bottom_offset_cells"] = 2
    updated, _ = apply_row_settings_transaction(model, edited)

    loaded = json.loads(json.dumps(updated, ensure_ascii=False))
    row = next(item for item in loaded["rows"] if item["row_number"] == "152")
    setting = next(item for item in loaded["row_settings"] if item["row_number"] == "152")

    assert (row["top_offset_cells"], row["bottom_offset_cells"]) == (4, 2)
    assert (setting["top_offset_m"], setting["bottom_offset_m"]) == (4, 2)


def test_offsets_use_row_cell_length_and_refresh_navigation_endpoints():
    model = _direction_model()
    model["navigation_nodes"] = [
        {"node_id": "row:152:bottom", "node_type": "row_bottom_entry", "row_number": "152", "x": 0, "y": 0},
        {"node_id": "row:152:top", "node_type": "row_top_entry", "row_number": "152", "x": 0, "y": 3.6},
    ]
    model["navigation_edges"] = [{"from_node": "row:152:bottom", "to_node": "row:152:top", "edge_type": "row_walk", "distance_m": 3.6}]
    edited = _edited(model)
    row = next(item for item in edited if item["row_number"] == "152")
    row.update({"top_offset_cells": 2, "bottom_offset_cells": 1})

    updated, _ = apply_row_settings_transaction(model, edited)
    cells = [cell for cell in updated["cells"] if cell["row_number"] == "152"]
    nodes = {node["node_id"]: node for node in updated["navigation_nodes"]}

    assert sorted(round(cell["y_min"], 2) for cell in cells) == [2.4, 3.6, 4.8]
    assert nodes["row:152:bottom"]["y"] == 0
    assert nodes["row:152:top"]["y"] == pytest.approx(7.2)
    assert updated["navigation_edges"][0]["distance_m"] == pytest.approx(7.2)


def test_row_tooltip_contains_offset_cells_and_meters():
    model = _model()
    edited = _edited(model)
    edited[0].update({"top_offset_cells": 2, "bottom_offset_cells": 1})
    updated, _ = apply_row_settings_transaction(model, edited)
    updated["roads"] = []

    rendered = build_geometry_html(updated, detailed=False)

    assert "Отступ сверху: 2 яч. / 2 м" in rendered
    assert "Отступ снизу: 1 яч. / 1 м" in rendered


def test_new_geometry_build_applies_individual_offsets_without_fake_cells():
    source = pd.DataFrame([
        {"code": "A", "row_number": "1", "cell_number": "1", "tier": "1", "source_line": 2},
        {"code": "B", "row_number": "1", "cell_number": "2", "tier": "1", "source_line": 3},
        {"code": "C", "row_number": "2", "cell_number": "1", "tier": "1", "source_line": 4},
    ])
    config = pd.DataFrame([
        {"row_number": "1", "row_order": 1, "row_storage_type": "normal", "deep_lane_width": 1, "cell_direction": "bottom_to_top", "weight_zone": "light", "top_offset_cells": 2, "bottom_offset_cells": 1},
        {"row_number": "2", "row_order": 2, "row_storage_type": "normal", "deep_lane_width": 1, "cell_direction": "bottom_to_top", "weight_zone": "medium", "top_offset_cells": 0, "bottom_offset_cells": 3},
    ])

    model, _ = build_geometry_model(source, GeometrySettings(cell_length_m=1.2), config)
    rows = {row["row_number"]: row for row in model["rows"]}
    row_one_cells = [cell for cell in model["cells"] if cell["row_number"] == "1"]

    assert len(model["cells"]) == 3
    assert min(cell["y_min"] for cell in row_one_cells) == pytest.approx(2.4)
    assert rows["1"]["y_max"] == pytest.approx(6.0)
    assert rows["2"]["bottom_offset_m"] == pytest.approx(3.6)


def test_first_submitted_edit_is_kept_without_a_second_change():
    model = _model()
    state = create_row_settings_state(model)
    edited = copy.deepcopy(state["draft"])
    edited[0]["weight_zone"] = "heavy"

    state = update_row_settings_state(state, edited)
    updated, messages = apply_row_settings_transaction(model, state["draft"])

    assert next(row for row in updated["rows"] if row["row_number"] == "152")["weight_zone"] == "heavy"
    assert not any(message.startswith("Ошибка:") for message in messages)


def test_draft_isolated_from_model_and_keeps_multiple_row_changes():
    model = _model()
    original = copy.deepcopy(model)
    state = create_row_settings_state(model)
    edited = copy.deepcopy(state["draft"])
    edited[0].update({"weight_zone": "heavy", "top_offset_cells": 3, "cell_direction": "bottom_to_top"})
    edited[1]["weight_zone"] = "medium"

    state = update_row_settings_state(state, edited)

    assert model == original
    assert set(changed_row_numbers(state)) == {"152", "153"}
    updated, _ = apply_row_settings_transaction(model, state["draft"])
    rows = {row["row_number"]: row for row in updated["rows"]}
    assert (rows["152"]["weight_zone"], rows["152"]["top_offset_cells"]) == ("heavy", 3)
    assert rows["153"]["weight_zone"] == "medium"


def test_reset_draft_restores_baseline_and_state_survives_deepcopy_rerun():
    state = create_row_settings_state(_model())
    edited = copy.deepcopy(state["draft"])
    edited[0]["weight_zone"] = "heavy"
    state = update_row_settings_state(state, edited)

    simulated_session_rerun = copy.deepcopy(state)
    reset = reset_row_settings_state(simulated_session_rerun)

    assert changed_row_numbers(simulated_session_rerun) == ["152"]
    assert changed_row_numbers(reset) == []
    assert reset["draft"] == reset["baseline"]


def test_apply_transaction_runs_geometry_sync_once(monkeypatch):
    import warehouse_row_settings as row_settings

    calls = 0
    real_sync = row_settings.sync_row_settings_to_model

    def counted_sync(model):
        nonlocal calls
        calls += 1
        return real_sync(model)

    monkeypatch.setattr(row_settings, "sync_row_settings_to_model", counted_sync)
    model = _model()
    updated, messages = row_settings.apply_row_settings_transaction(model, _edited(model))

    assert updated["rows"]
    assert not any(message.startswith("Ошибка:") for message in messages)
    assert calls == 1


def test_invalid_order_rolls_back_entire_draft():
    model = _model()
    edited = _edited(model)
    edited[0]["row_order"] = edited[1]["row_order"]
    edited[1]["weight_zone"] = "medium"

    updated, messages = apply_row_settings_transaction(model, edited)

    assert updated == model
    assert any("одинаковый порядок" in message for message in messages)
