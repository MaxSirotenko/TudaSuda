from __future__ import annotations

import copy

from warehouse_simulation_render import (
    build_simulation_dynamic_payload,
    build_simulation_render_placements,
    build_simulation_render_report,
)


def _inputs():
    cells = [
        {"cell_key": "normal", "row_number": "1", "cell_number": "1", "tier": "1",
         "storage_type": "normal", "capacity_pallets": 1, "weight_zone": "medium_light"},
        {"cell_key": "deep", "row_number": "2", "cell_number": "1", "tier": "1",
         "storage_type": "deep_lane", "capacity_pallets": 5, "weight_zone": "bulky"},
        {"cell_key": "unknown", "row_number": "3", "cell_number": "1", "tier": "1",
         "storage_type": "deep_lane", "capacity_pallets": 5, "weight_zone": "show_boxes"},
    ]
    model = {"model_id": "render-model", "cells": cells}
    state = {
        "stock_lots": [
            {"stock_lot_id": "lot-a", "sku_key": "A", "nomenclature": "Opaque", "characteristic": "x",
             "qty_boxes": 500, "location_status": "located", "cell_key": "normal"},
            {"stock_lot_id": "lot-b", "sku_key": "B", "nomenclature": "Pallet", "characteristic": "y",
             "qty_boxes": 20, "location_status": "located", "cell_key": "deep", "pallet_unit_id": "p1"},
            {"stock_lot_id": "lot-u", "sku_key": "U", "qty_boxes": 7, "location_status": "unknown"},
        ],
        "pallet_units": [{"pallet_unit_id": "p1", "physical_status": "active", "location_status": "located",
                          "position_id": "deep:1", "cell_key": "deep", "sku_key": "B"}],
        "cell_occupancy": [
            {"cell_key": "normal", "exact_occupied_positions": 1},
            {"cell_key": "deep", "exact_occupied_positions": 3},
            {"cell_key": "unknown", "exact_occupied_positions": None},
        ],
    }
    return model, state


def test_exact_opaque_deep_and_unknown_projection_uses_physical_occupancy():
    model, state = _inputs()
    before = copy.deepcopy((model, state))
    placements = {row["simulation_cell_key"]: row for row in build_simulation_render_placements(model, state)}
    assert placements["normal"]["qty_boxes"] == 500
    assert placements["normal"]["occupied_capacity_pallets"] == 1
    assert placements["deep"]["occupied_capacity_pallets"] == 3
    assert placements["unknown"]["occupancy_unknown"] is True
    payload = build_simulation_dynamic_payload(model, state)
    assert payload["3|1|1"]["occupancy_status"] == "unknown"
    assert payload["3|1|1"]["occupancy_unknown"] is True
    assert "Физическая занятость неизвестна" in payload["3|1|1"]["tooltip"]
    assert (model, state) == before


def test_report_and_payloads_are_independent_and_do_not_duplicate_pallet_lot():
    model, state = _inputs()
    report = build_simulation_render_report(model, state)
    assert report == {"located_stock_lots": 2, "unknown_location_stock_lots": 1,
                      "unknown_location_boxes": 7, "cells_exact_occupied": 2,
                      "cells_exact_free": 0, "cells_unknown_occupancy": 1,
                      "rendered_cells": 3, "rendered_physical_footprints": 4}
    first = build_simulation_dynamic_payload(model, state)
    second = build_simulation_dynamic_payload(model, state)
    assert first == second and first is not second
    first["1|1|1"]["label"] = "changed"
    assert second["1|1|1"].get("label") != "changed"
