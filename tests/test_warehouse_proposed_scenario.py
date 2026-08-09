from __future__ import annotations

import copy

from warehouse_business_identity import canonical_sku_key
from warehouse_proposed_scenario import build_proposed_scenario
from warehouse_simulation_state import build_initial_simulation_state


def _fixture():
    cells = [
        {"cell_key": str(index), "row_number": str(index), "cell_number": "1", "tier": "1",
         "row_order": index, "physical_index": index, "weight_zone": zone,
         "storage_type": "normal", "capacity_pallets": 1}
        for index, zone in ((1, "heavy"), (2, "light"), (3, "heavy"))
    ]
    model = {"model_id": "scenario-model", "source_file_hash": "source", "cells": cells}
    def opening(name, cell, boxes):
        return {"sku_key": canonical_sku_key({"nomenclature": name, "characteristic": "x"}),
                "nomenclature": name, "characteristic": "x", "qty_units": boxes,
                "unit_name": "короб", "cell_key": cell, "production_dates": [],
                "placement_id": "opening:" + name, "allocation_method": "exact_single_cell"}
    state, _ = build_initial_simulation_state(
        model, {"placements": [opening("A", "1", 50), opening("B", "2", 30)],
                "unknown_location_inventory": []},
        target_normalized_warehouse="вешки", simulation_time="2026-08-09T00:00:00",
    )
    rows = [{"sku_key": opening("A", "1", 1)["sku_key"], "target_zone": "light"},
            {"sku_key": opening("B", "2", 1)["sku_key"], "target_zone": "heavy"}]
    return model, state, rows


def test_all_off_is_identity_deterministic_and_rebuilds_from_baseline():
    model, baseline, rows = _fixture()
    before = copy.deepcopy((model, baseline, rows))
    first, diagnostics = build_proposed_scenario(model, baseline, {}, sku_zone_rows=rows)
    moved, _ = build_proposed_scenario(model, baseline, {"weight_zones": {"enabled": True}}, sku_zone_rows=rows)
    third, _ = build_proposed_scenario(model, baseline, {}, sku_zone_rows=rows)
    assert diagnostics["valid"] and first["status"] == "ready"
    assert first["proposed_state_id"] == baseline["simulation_state_id"] == third["proposed_state_id"]
    assert first["proposed_scenario_id"] == third["proposed_scenario_id"]
    assert moved["status"] == "ready" and moved["summary"]["units_moved"] == 2
    assert moved["proposed_state_id"] != baseline["simulation_state_id"]
    assert (model, baseline, rows) == before


def test_adjacency_rule_is_supported_and_invalid_rule_set_is_blocked():
    model, baseline, rows = _fixture()
    blocked, diagnostics = build_proposed_scenario(
        model, baseline, {"adjacency": {"enabled": True}}, sku_zone_rows=rows,
    )
    assert blocked["status"] == "ready" and blocked["proposed_state"] is not None
    assert diagnostics["valid"]

    invalid, invalid_diagnostics = build_proposed_scenario(
        model, baseline, {"unknown_rule": True}, sku_zone_rows=rows,
    )
    assert invalid["status"] == "blocked" and not invalid_diagnostics["rule_set_validation"]["valid"]
