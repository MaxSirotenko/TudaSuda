import warehouse_inventory_placement as placement_module
from warehouse_inventory_placement import calculate_basic_weight_placement, empty_placement_state


def _cell(row, cell, zone, *, capacity=1, row_order=None):
    order = row_order if row_order is not None else row - 151
    return {
        "code": f"{row}-{cell}",
        "row_number": str(row),
        "cell_number": str(cell),
        "tier": "1",
        "row_order": order,
        "cell_direction": "bottom_to_top",
        "weight_zone": zone,
        "storage_type": "deep_lane" if capacity > 1 else "normal",
        "capacity_pallets": capacity,
        "deep_lane_width": capacity,
        "x_min": float(order),
        "x_max": float(order) + 0.8 * capacity,
        "x_center": float(order) + 0.4 * capacity,
        "y_min": float(cell - 1),
        "y_max": float(cell),
        "y_center": float(cell) - 0.5,
        "physical_slots": [],
    }


def _model(cells_per_row=2, deep_light_capacity=1):
    zones = {152: "heavy", 153: "heavy", 154: "medium", 155: "medium", 156: "medium", 157: "medium", 158: "light"}
    cells = []
    rows = []
    for order, row in enumerate(range(152, 159), start=1):
        rows.append({"row_number": str(row), "row_order": order, "weight_zone": zones[row], "cells_count": cells_per_row, "x_min": float(order), "x_max": float(order) + 0.8, "y_min": 0, "y_max": cells_per_row})
        for cell in range(1, cells_per_row + 1):
            capacity = deep_light_capacity if row == 158 and cell == 1 else 1
            cells.append(_cell(row, cell, zones[row], capacity=capacity, row_order=order))
    return {"model_id": "overflow", "settings": {"cell_width_m": 0.8}, "rows": rows, "cells": cells, "aisles": [], "roads": [], "navigation_nodes": [], "navigation_edges": []}


def _receipt(sku="light-sku", qty=1, zone="light"):
    return {"sku_key": sku, "sku_code": sku, "sku_name": sku, "qty_pallets": qty, "calculated_zone": zone, "weight_class": zone, "receipt_number": "R1", "receipt_line_id": f"R1-{sku}"}


def _receipt_placements(state, sku="light-sku"):
    return [p for p in state["placements"] if p.get("source") == "receipt" and p.get("sku_key") == sku]


def test_light_overflows_compactly_from_158_to_157_then_156(tmp_path, monkeypatch):
    monkeypatch.setattr(placement_module, "PLACEMENTS_PATH", tmp_path / "placements.json")
    new_state, _ = calculate_basic_weight_placement(_model(cells_per_row=2), empty_placement_state(_model(cells_per_row=2)), {"receipts": [_receipt(qty=5)]})
    assert [(p["row_number"], p["cell_number"]) for p in _receipt_placements(new_state)] == [("158", "1"), ("158", "2"), ("157", "2"), ("157", "1"), ("156", "2")]
    overflow = [p for p in _receipt_placements(new_state) if p["row_number"] == "157"]
    assert all(p["placement_reason_code"] == "zone_overflow" for p in overflow)
    assert all(p["zone_overflow"] is True for p in overflow)
    assert all(p["target_weight_zone"] == "light" and p["actual_weight_zone"] == "medium" for p in overflow)


def test_medium_uses_own_rows_before_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(placement_module, "PLACEMENTS_PATH", tmp_path / "placements.json")
    model = _model(cells_per_row=1)
    new_state, _ = calculate_basic_weight_placement(model, empty_placement_state(model), {"receipts": [_receipt("medium-sku", 4, "medium")]})
    assert [(p["row_number"], p["cell_number"]) for p in _receipt_placements(new_state, "medium-sku")] == [("154", "1"), ("155", "1"), ("156", "1"), ("157", "1")]
    assert all(p["placement_reason_code"] in {"matching_weight_zone", "adjacent_to_same_sku"} for p in _receipt_placements(new_state, "medium-sku"))


def test_same_sku_partial_cell_keeps_priority_over_zone_order(tmp_path, monkeypatch):
    monkeypatch.setattr(placement_module, "PLACEMENTS_PATH", tmp_path / "placements.json")
    model = _model(cells_per_row=2)
    state = empty_placement_state(model)
    state["placements"] = [{"cell_key": "157|1|1", "row_number": "157", "cell_number": "1", "tier": "1", "sku_key": "light-sku", "sku_name": "light-sku", "qty_pallets": 0.5, "occupied_capacity_pallets": 0.5, "source": "inventory_with_cell", "placement_mode": "factual", "weight_class": "light"}]
    new_state, _ = calculate_basic_weight_placement(model, state, {"receipts": [_receipt(qty=0.5)]})
    receipt = _receipt_placements(new_state)[0]
    assert (receipt["row_number"], receipt["cell_number"]) == ("157", "1")
    assert receipt["placement_reason_code"] == "same_sku_partial_cell"


def test_different_sku_does_not_mix_and_deep_capacity_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(placement_module, "PLACEMENTS_PATH", tmp_path / "placements.json")
    model = _model(cells_per_row=2, deep_light_capacity=3)
    state = empty_placement_state(model)
    state["placements"] = [{"cell_key": "158|2|1", "row_number": "158", "cell_number": "2", "tier": "1", "sku_key": "other", "sku_name": "other", "qty_pallets": 1, "occupied_capacity_pallets": 1, "source": "inventory_with_cell", "placement_mode": "factual", "weight_class": "light"}]
    new_state, _ = calculate_basic_weight_placement(model, state, {"receipts": [_receipt(qty=3)]})
    receipt = _receipt_placements(new_state)[0]
    assert (receipt["row_number"], receipt["cell_number"], receipt["qty_pallets"]) == ("158", "1", 3)


def test_insufficient_capacity_only_after_all_allowed_rows_are_full(tmp_path, monkeypatch):
    monkeypatch.setattr(placement_module, "PLACEMENTS_PATH", tmp_path / "placements.json")
    model = _model(cells_per_row=1)
    state = empty_placement_state(model)
    state["placements"] = [
        {"cell_key": f"{row}|1|1", "row_number": str(row), "cell_number": "1", "tier": "1", "sku_key": f"other-{row}", "sku_name": "other", "qty_pallets": 1, "occupied_capacity_pallets": 1, "source": "inventory_with_cell", "placement_mode": "factual", "weight_class": "light"}
        for row in range(152, 159)
    ]
    new_state, _ = calculate_basic_weight_placement(model, state, {"receipts": [_receipt(qty=1)]})
    assert not _receipt_placements(new_state)
    assert new_state["unplaced_inventory"][0]["unplaced_reason"] == "insufficient_zone_capacity"
