import warehouse_placement_diagnostics as diag_module
import warehouse_inventory_placement as placement_module
from warehouse_placement_diagnostics import (
    build_placement_diagnostics,
    build_tooltip_by_cell,
    enrich_model_with_placement_diagnostics,
    save_pre_placement_snapshot,
)
from warehouse_inventory_placement import calculate_basic_weight_placement, empty_placement_state
from warehouse_geometry_model import build_geometry_html


def _cell(row, cell, zone="heavy", storage="normal", capacity=1):
    physical_slots = []
    if storage == "deep_lane":
        physical_slots = [{"slot_index": i, "x_min": (i - 1) * 0.8, "x_max": i * 0.8, "y_min": cell - 1, "y_max": cell, "capacity_pallets": 1} for i in range(1, capacity + 1)]
    return {
        "code": f"{row}-{cell}", "row_number": str(row), "cell_number": str(cell), "tier": "1",
        "x_min": 0.0 if row == 1 else 3.0, "x_max": float(capacity) * 0.8 if row == 1 else 3.0 + float(capacity) * 0.8,
        "y_min": float(cell - 1), "y_max": float(cell), "x_center": 0.4, "y_center": cell - 0.5,
        "storage_type": storage, "capacity_pallets": capacity, "deep_lane_width": capacity,
        "weight_zone": zone, "initial_weight_zone": zone, "physical_slots": physical_slots,
        "cell_direction": "bottom_to_top", "source": "excel", "volume_m3": 1,
    }


def _model():
    cells = [_cell(1, 1, "heavy"), _cell(1, 2, "heavy"), _cell(2, 1, "fragile", "deep_lane", 4), _cell(2, 2, "unassigned")]
    return {
        "model_id": "m1", "settings": {"cell_width_m": 0.8, "top_road_width_m": 1, "bottom_road_width_m": 1},
        "cells": cells,
        "rows": [
            {"row_number": "1", "row_order": 1, "weight_zone": "heavy", "initial_weight_zone": "medium", "cells_count": 2, "x_min": 0.0, "x_max": 0.8, "y_min": 0.0, "y_max": 2.0},
            {"row_number": "2", "row_order": 2, "weight_zone": "fragile", "initial_weight_zone": "fragile", "cells_count": 2, "x_min": 3.0, "x_max": 6.2, "y_min": 0.0, "y_max": 2.0},
        ],
        "aisles": [], "roads": [], "navigation_nodes": [], "navigation_edges": [],
    }


def _receipt(sku="sku-a", receipt="R1", line="R1-1", qty=1, zone="heavy"):
    return {"sku_key": sku, "sku_code": sku, "sku_name": "Товар", "characteristic_name": "Хар", "qty_pallets": qty, "receipt_number": receipt, "receipt_line_id": line, "calculated_zone": zone, "weight_class": zone}


def test_before_after_capacity_and_zone_analytics(tmp_path, monkeypatch):
    monkeypatch.setattr(diag_module, "PLACEMENT_DIAGNOSTICS_PATH", tmp_path / "placement_diagnostics.json")
    model = _model()
    before = {"placements": [{"cell_key": "1|1|1", "row_number": "1", "cell_number": "1", "tier": "1", "sku_key": "sku-a", "sku_name": "Товар", "qty_pallets": 0.5, "occupied_capacity_pallets": 0.5, "source": "inventory_with_cell", "placement_mode": "factual", "weight_class": "heavy"}], "unplaced_inventory": []}
    snapshot = save_pre_placement_snapshot(model, before, {"receipts": [_receipt(qty=1)]})
    after = {"placements": before["placements"] + [{"cell_key": "1|1|1", "row_number": "1", "cell_number": "1", "tier": "1", "sku_key": "sku-a", "sku_name": "Товар", "characteristic_name": "Хар", "qty_pallets": 0.5, "occupied_capacity_pallets": 0.5, "source": "receipt", "receipt_numbers": ["R1"], "receipt_line_ids": ["R1-1"], "weight_class": "heavy", "calculated_zone": "heavy", "placement_reason_code": "same_sku_partial_cell", "placement_reason_text": "Добавлено в частично заполненную ячейку с тем же SKU."}], "unplaced_inventory": []}
    diagnostics = build_placement_diagnostics(model, after, {"receipts": [_receipt(qty=1)]}, snapshot)
    assert diagnostics["summary"]["Ячеек занято до"] == 1
    assert diagnostics["summary"]["Ячеек занято после"] == 1
    assert diagnostics["summary"]["Общая вместимость склада"] == 7
    assert diagnostics["summary"]["Использованных физических паллетомест"] == 1
    assert any(row["Весовая зона"] == "Тяжёлое" for row in diagnostics["zone_rows"])
    assert diagnostics["changed_rows_count"] == 1


def test_deep_lane_physical_slots_unclassified_and_tooltip_and_color():
    model = _model()
    state = {"placements": [{"cell_key": "2|1|1", "row_number": "2", "cell_number": "1", "tier": "1", "sku_key": "fragile-sku", "sku_name": "Стекло", "qty_pallets": 3, "occupied_capacity_pallets": 3, "source": "receipt", "receipt_numbers": ["R1", "R2"], "receipt_line_ids": ["L1", "L2"], "weight_class": "fragile", "calculated_zone": "fragile", "placement_reason_code": "fragile_priority", "placement_reason_text": "Хрупкий товар размещён в зоне хрупкого товара."}], "unplaced_inventory": [{"sku_key": "unknown", "sku_name": "Без веса", "qty_pallets": 2, "weight_class": "unclassified", "unplaced_reason": "missing_calculated_zone"}]}
    diagnostics = build_placement_diagnostics(model, state, {"receipts": [_receipt("fragile-sku", "R1", "L1", 1, "fragile"), _receipt("fragile-sku", "R2", "L2", 2, "fragile")]}, None)
    assert diagnostics["summary"]["Использованных физических паллетомест"] == 3
    assert diagnostics["unplaced_rows"][0]["Весовая категория"] == "Без классификации"
    tooltip = build_tooltip_by_cell(model, state, None)["2|1|1"]
    assert "receipt_line_ids: L1, L2" in tooltip
    enriched = enrich_model_with_placement_diagnostics(model, state, None)
    cell = next(c for c in enriched["cells"] if c["row_number"] == "2" and c["cell_number"] == "1")
    assert cell["placement_category"] == "fragile"
    html = build_geometry_html(enriched, scale=10, detailed=True, label_settings={})
    assert "#D8B4FE" in html
    assert "Хрупкий товар размещён" in html


def test_placement_reasons_from_algorithm_same_adjacent_matching_and_fragile(tmp_path, monkeypatch):
    monkeypatch.setattr(placement_module, "PLACEMENTS_PATH", tmp_path / "placements.json")
    model = _model()
    state = empty_placement_state(model)
    state["placements"] = [{"cell_key": "1|1|1", "row_number": "1", "cell_number": "1", "tier": "1", "sku_key": "sku-a", "sku_name": "Товар", "qty_pallets": 0.5, "occupied_capacity_pallets": 0.5, "source": "inventory_with_cell", "placement_mode": "factual", "weight_class": "heavy"}]
    receipts = {"receipts": [_receipt("sku-a", "R1", "L1", 1.5, "heavy"), _receipt("sku-b", "R2", "L2", 1, "heavy"), _receipt("fragile-sku", "R3", "L3", 1, "fragile")]}
    new_state, _ = calculate_basic_weight_placement(model, state, receipts)
    reasons = {p.get("placement_reason_code") for p in new_state["placements"]}
    assert "same_sku_partial_cell" in reasons
    assert "adjacent_to_same_sku" in reasons
    assert "matching_weight_zone" in reasons or "fragile_priority" in reasons or "zone_overflow" in reasons
    assert "fragile_priority" in reasons or "zone_overflow" in reasons


def test_old_placements_without_diagnostic_fields_and_no_initial_zone_do_not_crash():
    model = _model()
    for row in model["rows"]:
        row.pop("initial_weight_zone", None)
    state = {"placements": [{"cell_key": "1|2|1", "row_number": "1", "cell_number": "2", "tier": "1", "sku_key": "old", "sku_name": "Старый", "qty_pallets": 1, "occupied_capacity_pallets": 1, "source": "manual"}], "unplaced_inventory": []}
    diagnostics = build_placement_diagnostics(model, state, {"receipts": []}, None)
    assert diagnostics["occupied_rows"][0]["Код причины размещения"] == "fallback"
    assert any(row["Статус изменения"] == "Нет данных об исходной зоне" for row in diagnostics["zone_changes"])


def _order_cell(row, cell, *, direction="top_to_bottom", row_order=1, capacity=1):
    return {
        "code": f"{row}-{cell}",
        "row_number": str(row),
        "cell_number": str(cell),
        "tier": "1",
        "row_order": row_order,
        "cell_direction": direction,
        "weight_zone": "heavy",
        "storage_type": "normal",
        "capacity_pallets": capacity,
        "deep_lane_width": capacity,
        "x_min": float(row_order),
        "x_max": float(row_order) + 0.8,
        "x_center": float(row_order) + 0.4,
        "y_min": float(cell - 1),
        "y_max": float(cell),
        "y_center": float(cell) - 0.5,
        "physical_slots": [],
    }


def _order_model(direction="top_to_bottom"):
    cells = [_order_cell(10, idx, direction=direction, row_order=1) for idx in (1, 2, 3)]
    return {
        "model_id": f"order-{direction}",
        "settings": {"cell_width_m": 0.8, "cell_length_m": 1.0},
        "rows": [{"row_number": "10", "row_order": 1, "cell_direction": direction, "weight_zone": "heavy", "cells_count": 3, "x_min": 0, "x_max": 0.8, "y_min": 0, "y_max": 3}],
        "cells": cells,
        "aisles": [],
        "roads": [],
        "navigation_nodes": [],
        "navigation_edges": [],
    }


def _placed_receipt_cells(state):
    return [p["cell_number"] for p in state["placements"] if p.get("source") == "receipt" and p.get("placement_mode") == "calculated"]


def test_basic_placement_orders_cells_by_number_for_both_row_directions(tmp_path, monkeypatch):
    monkeypatch.setattr(placement_module, "PLACEMENTS_PATH", tmp_path / "placements.json")
    for direction in ("top_to_bottom", "bottom_to_top"):
        model = _order_model(direction)
        state = empty_placement_state(model)
        new_state, _ = calculate_basic_weight_placement(model, state, {"receipts": [_receipt("sku-order", "R1", "L1", 3, "heavy")]})
        assert _placed_receipt_cells(new_state) == ["1", "2", "3"]


def test_cell_sort_keeps_row_order_before_cell_number():
    model = {
        "rows": [
            {"row_number": "10", "row_order": 2, "cell_direction": "top_to_bottom"},
            {"row_number": "20", "row_order": 1, "cell_direction": "bottom_to_top"},
        ],
        "cells": [_order_cell(10, 1, row_order=2), _order_cell(20, 1, direction="bottom_to_top", row_order=1)],
    }
    ordered = sorted(model["cells"], key=lambda cell: placement_module._cell_sort_key(cell, model))
    assert [cell["row_number"] for cell in ordered] == ["20", "10"]


def test_same_sku_partial_cell_still_has_priority_over_cell_order(tmp_path, monkeypatch):
    monkeypatch.setattr(placement_module, "PLACEMENTS_PATH", tmp_path / "placements.json")
    model = _order_model("top_to_bottom")
    state = empty_placement_state(model)
    state["placements"] = [{
        "cell_key": "10|3|1",
        "row_number": "10",
        "cell_number": "3",
        "tier": "1",
        "sku_key": "sku-order",
        "sku_name": "Товар",
        "qty_pallets": 0.5,
        "occupied_capacity_pallets": 0.5,
        "source": "inventory_with_cell",
        "placement_mode": "factual",
        "weight_class": "heavy",
    }]
    new_state, _ = calculate_basic_weight_placement(model, state, {"receipts": [_receipt("sku-order", "R1", "L1", 0.5, "heavy")]})
    receipt_placements = [p for p in new_state["placements"] if p.get("source") == "receipt"]
    assert receipt_placements[0]["cell_number"] == "3"
    assert receipt_placements[0]["placement_reason_code"] == "same_sku_partial_cell"


def test_deep_lane_physical_slots_do_not_override_logical_cell_tooltip():
    model = _model()
    state = {"placements": [{"cell_key": "2|1|1", "row_number": "2", "cell_number": "1", "tier": "1", "sku_key": "fragile-sku", "sku_name": "Стекло", "qty_pallets": 4, "occupied_capacity_pallets": 4, "source": "receipt", "receipt_numbers": ["R1"], "receipt_line_ids": ["L1"], "weight_class": "fragile", "calculated_zone": "fragile", "placement_reason_code": "fragile_priority", "placement_reason_text": "Хрупкий товар размещён в зоне хрупкого товара."}], "unplaced_inventory": []}
    enriched = enrich_model_with_placement_diagnostics(model, state, None)
    html = build_geometry_html(enriched, scale=10, detailed=True, label_settings={})
    assert "pointer-events:none;" in html
    assert "Хрупкий товар размещён" in html
    assert "Физическое место" not in html


def test_map_labels_show_cell_number_and_sku_name_without_slot_captions():
    model = _model()
    state = {"placements": [{"cell_key": "2|1|1", "row_number": "2", "cell_number": "1", "tier": "1", "sku_key": "fragile-sku", "sku_name": "Стекло", "qty_pallets": 3, "occupied_capacity_pallets": 3, "source": "receipt", "receipt_numbers": ["R1"], "receipt_line_ids": ["L1"], "weight_class": "fragile", "calculated_zone": "fragile", "placement_reason_code": "fragile_priority", "placement_reason_text": "Хрупкий товар размещён в зоне хрупкого товара."}], "unplaced_inventory": []}
    enriched = enrich_model_with_placement_diagnostics(model, state, None)
    html = build_geometry_html(enriched, scale=10, detailed=True, label_settings={})
    assert "Стекло" in html
    assert "pointer-events:none;" in html
    assert "Физическое место" not in html
    assert "3/4" not in html


def test_map_label_collapses_multiple_sku_names_with_suffix():
    model = _model()
    state = {"placements": [
        {"cell_key": "1|1|1", "row_number": "1", "cell_number": "1", "tier": "1", "sku_key": "berry", "sku_name": "Голубика 125 г", "qty_pallets": 0.5, "occupied_capacity_pallets": 0.5, "source": "receipt", "weight_class": "heavy"},
        {"cell_key": "1|1|1", "row_number": "1", "cell_number": "1", "tier": "1", "sku_key": "salad", "sku_name": "Салат Фриллис", "qty_pallets": 0.5, "occupied_capacity_pallets": 0.5, "source": "receipt", "weight_class": "heavy"},
    ], "unplaced_inventory": []}
    enriched = enrich_model_with_placement_diagnostics(model, state, None)
    html = build_geometry_html(enriched, scale=80, detailed=True, label_settings={})
    assert "Голубика" in html and "+1" in html


def test_deep_lane_labels_are_rendered_once_over_capacities_three_four_five():
    for capacity in (3, 4, 5):
        model = _model()
        for cell in model["cells"]:
            if cell["row_number"] == "2" and cell["cell_number"] == "1":
                cell["capacity_pallets"] = capacity
                cell["deep_lane_width"] = capacity
                cell["x_max"] = cell["x_min"] + capacity * 0.8
                cell["physical_slots"] = [{"slot_index": idx, "x_min": cell["x_min"] + (idx - 1) * 0.8, "x_max": cell["x_min"] + idx * 0.8, "y_min": cell["y_min"], "y_max": cell["y_max"], "capacity_pallets": 1} for idx in range(1, capacity + 1)]
        state = {"placements": [{"cell_key": "2|1|1", "row_number": "2", "cell_number": "1", "tier": "1", "sku_key": "deep", "sku_name": "Набивной SKU", "qty_pallets": capacity, "occupied_capacity_pallets": capacity, "source": "receipt", "weight_class": "fragile"}], "unplaced_inventory": []}
        enriched = enrich_model_with_placement_diagnostics(model, state, None)
        html = build_geometry_html(enriched, scale=10, detailed=True, label_settings={})
        assert "Набивной SKU" in html
        assert "Физическое место" not in html
