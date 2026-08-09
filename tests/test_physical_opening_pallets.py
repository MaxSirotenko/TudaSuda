from __future__ import annotations

import pandas as pd

from warehouse_actual_inventory_import import (
    build_actual_inventory_placement_state, cross_check_physical_opening_stock,
)
from warehouse_simulation_state import build_initial_simulation_state, validate_simulation_state


def model():
    return {"model_id": "m", "cells": [
        {"cell_key": "1|1|1", "row_number": "1", "cell_number": "1", "tier": "1", "storage_type": "normal", "capacity_pallets": 1},
        {"cell_key": "1|2|1", "row_number": "1", "cell_number": "2", "tier": "1", "storage_type": "normal", "capacity_pallets": 1},
        {"cell_key": "2|1|1", "row_number": "2", "cell_number": "1", "tier": "1", "storage_type": "deep_lane", "capacity_pallets": 2,
         "physical_slots": [{"slot_index": 1, "x_min": 0, "x_max": 1}, {"slot_index": 2, "x_min": 1, "x_max": 2}]},
    ]}


def row(pallet="PAL-1", cell=1, name="A", date="2026-01-01", boxes=4, warehouse="WH", row_number=1):
    return {"Склад": warehouse, "РЦ": "DC", "Паллета": pallet, "Ряд": row_number,
            "НомерЯчейки": cell, "Ярус": 1, "Номенклатура": name, "Характеристика": "X",
            "ДатаПроизводства": date, "Количество": boxes * 10, "КоличествоПаллет": 99,
            "КоличествоВКоробке": 10, "РасчетноеКоличествоКоробов": boxes,
            "КонтрольРасчета": "Расчет выполнен"}


def initial(rows, **kwargs):
    opening, diag = build_actual_inventory_placement_state(model(), pd.DataFrame(rows), **kwargs)
    state, simdiag = build_initial_simulation_state(model(), opening, target_normalized_warehouse="WH", simulation_time="2026-01-01")
    return opening, diag, state, simdiag


def test_two_pallets_and_row_permutation_have_stable_exact_units():
    rows = [row("P2", 2), row("P1", 1)]
    _, diag, state, _ = initial(rows)
    _, _, permuted, _ = initial(list(reversed(rows)))
    assert len(state["pallet_units"]) == 2
    assert state["simulation_state_id"] == permuted["simulation_state_id"]
    assert [p["pallet_unit_id"] for p in state["pallet_units"]] == [p["pallet_unit_id"] for p in permuted["pallet_units"]]
    assert diag["exact_normal_pallets"] == 2
    assert validate_simulation_state(state, model())["valid"]


def test_same_pallet_multiple_dates_is_one_aggregate_unknown_capacity_pallet():
    _, _, state, _ = initial([row(date="2026-01-01", boxes=4), row(date="2026-01-02", boxes=3)])
    assert len(state["pallet_units"]) == len(state["stock_lots"]) == 1
    assert state["stock_lots"][0]["qty_boxes"] == state["pallet_units"][0]["remaining_boxes"] == 7
    assert state["stock_lots"][0]["production_dates"] == ["2026-01-01", "2026-01-02"]
    assert state["pallet_units"][0]["capacity_boxes"] is None
    assert state["pallet_units"][0]["is_partial"] is None


def test_conflicting_or_missing_identity_never_creates_fake_pallet():
    for rows, reason in [
        ([row(name="A"), row(name="B")], "multi_sku_source_pallets"),
        ([row(cell=1), row(cell=2)], "pallet_in_multiple_cell_conflicts"),
        ([row(pallet="")], "pallets_missing_identity"),
        ([row("P1"), row("P2")], "normal_cell_overoccupancy_conflicts"),
    ]:
        _, diag, state, _ = initial(rows)
        assert diag[reason] > 0
        assert state["pallet_units"] == []


def test_deep_lane_has_factual_stock_but_no_invented_pallet_position():
    _, diag, state, _ = initial([row(cell=1, row_number=2)])
    assert diag["deep_lane_pallets_with_unknown_depth"] == 1
    assert state["pallet_units"] == []
    lot = state["stock_lots"][0]
    assert lot["cell_key"] == "2|1|1" and lot["position_id"] is None
    assert all(position["status"] == "unknown" for position in state["physical_positions"] if position["cell_key"] == "2|1|1")


def test_master_capacity_controls_status_and_mismatch_does_not_redistribute():
    opening, diag, state, _ = initial([row(boxes=4)], palletization_rule_state={"rules": [{"sku_key": "bad"}]})
    sku = opening["placements"][0]["sku_key"]
    opening, diag = build_actual_inventory_placement_state(model(), pd.DataFrame([row(boxes=4)]),
        inventory_results_rows=[{"nomenclature": "A", "characteristic": "X", "qty_units": 5}],
        palletization_rule_state={"rules": [{"sku_key": sku, "boxes_per_pallet": 10}]})
    state, _ = build_initial_simulation_state(model(), opening, target_normalized_warehouse="WH", simulation_time="2026-01-01")
    assert diag["inventory_total_mismatches"] == 1
    assert opening["placements"][0]["qty_boxes"] == 4
    assert state["pallet_units"][0]["capacity_boxes"] == 10
    assert state["pallet_units"][0]["is_partial"] is True
    assert state["readiness"]["opening_stock_business_ready"] is False


def test_absent_and_supplied_empty_inventory_controls_are_distinct():
    opening, _, _, _ = initial([row()])
    absent, absent_diag = cross_check_physical_opening_stock(opening, None)
    empty, empty_diag = cross_check_physical_opening_stock(opening, [])
    assert absent_diag["inventory_totals_control_status"] == "not_supplied"
    assert absent_diag.get("inventory_total_mismatch_details") is None
    assert absent["physical_opening_readiness"]["opening_stock_business_ready"] is True
    assert empty_diag["inventory_totals_control_status"] == "supplied_but_no_valid_rows"
    assert empty_diag["inventory_control_diagnostic"] == "inventory_control_supplied_but_no_valid_rows"
    assert empty_diag.get("inventory_total_mismatch_details") is None
    assert empty["physical_opening_readiness"]["opening_stock_business_ready"] is False
