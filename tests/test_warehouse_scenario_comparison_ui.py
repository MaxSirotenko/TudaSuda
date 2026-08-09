from __future__ import annotations

import copy

import pandas as pd

from warehouse_business_identity import canonical_sku_key
from warehouse_actual_inventory_import import build_actual_inventory_placement_state
from warehouse_scenario_comparison_ui import (
    build_comparison_baseline,
    build_comparison_signature,
    build_sku_zone_rows,
    build_weight_zone_rule_config,
    build_distance_order_rows,
    summarize_scenario_ui_metrics,
)
from warehouse_proposed_scenario import build_proposed_scenario


def _fixture():
    cells = [
        {"cell_key": str(index), "row_number": str(index), "cell_number": "1", "tier": "1",
         "row_order": index, "physical_index": index, "weight_zone": zone,
         "storage_type": "normal", "capacity_pallets": 1}
        for index, zone in ((1, "heavy"), (2, "light"))
    ]
    model = {"model_id": "comparison-model", "source_file_hash": "source", "cells": cells}

    def sku(name):
        return canonical_sku_key({"nomenclature": name, "characteristic": "x"})

    start = {"placements": [
        {"sku_key": sku("A"), "nomenclature": "A", "characteristic": "x", "quantity": 10,
         "qty_boxes": 10, "cell_key": "1", "warehouse": "Вешки"},
        {"sku_key": sku("B"), "nomenclature": "B", "characteristic": "x", "quantity": 10,
         "qty_boxes": 10, "cell_key": "2", "warehouse": "Вешки"},
        {"sku_key": sku("FOREIGN"), "nomenclature": "FOREIGN", "characteristic": "x", "quantity": 5,
         "qty_boxes": 5, "cell_key": "1", "warehouse": "Другой"},
    ]}
    inventory = [
        {"sku_key": sku(name), "nomenclature": name, "characteristic": "x", "qty_units": 10,
         "unit_name": "короб", "warehouse": "Вешки"}
        for name in ("A", "B")
    ]
    classifications = [
        {"sku_key": sku("A"), "sku_name": "A", "characteristic_name": "x", "calculated_zone": "light"},
        {"sku_key": sku("B"), "sku_name": "B", "characteristic_name": "x", "calculated_zone": "heavy"},
    ]
    return model, start, inventory, classifications


def test_signature_is_deterministic_and_covers_rules_and_baseline():
    common = dict(model_id="m", baseline_state_id="s", operational_date="2026-08-09",
                  normalized_warehouse="Вешки", sku_zone_rows=[{"sku_key": "b", "target_zone": "light"},
                                                                 {"sku_key": "a", "target_zone": "heavy"}])
    first = build_comparison_signature(**common, rule_config=build_weight_zone_rule_config(False))
    reordered = dict(common, sku_zone_rows=list(reversed(common["sku_zone_rows"])))
    assert first == build_comparison_signature(**reordered, rule_config=build_weight_zone_rule_config(False))
    assert first != build_comparison_signature(**common, rule_config=build_weight_zone_rule_config(True))
    assert first != build_comparison_signature(**(common | {"baseline_state_id": "changed"}),
                                                rule_config=build_weight_zone_rule_config(False))


def test_classification_adapter_uses_canonical_contract_and_never_physical_zone():
    rows = build_sku_zone_rows([
        {"sku_name": "a", "calculated_zone": "Средне-лёгкое", "weight_zone": "heavy"},
        {"sku_name": "b", "calculated_zone": "show_boxes"},
        {"sku_name": "c", "calculated_zone": "unassigned"},
        {"sku_name": "d", "calculated_zone": "not-a-zone"},
    ])
    assert rows == [
        {"sku_key": canonical_sku_key({"nomenclature": "a"}), "target_zone": "medium_light", "source": "loaded_receipt_classification"},
        {"sku_key": canonical_sku_key({"nomenclature": "b"}), "target_zone": "show_boxes", "source": "loaded_receipt_classification"},
    ]


def test_off_on_off_always_uses_immutable_baseline_and_exact_warehouse_scope():
    model, start, inventory, classifications = _fixture()
    originals = copy.deepcopy((model, start, inventory))
    baseline, diagnostics = build_comparison_baseline(
        model, start, inventory, normalized_warehouse="Вешки", operational_date="2026-08-09")
    assert not diagnostics["configuration_errors"]
    assert all(lot["nomenclature"] != "FOREIGN" for lot in baseline["stock_lots"])
    zones = build_sku_zone_rows(classifications)
    first, _ = build_proposed_scenario(model, baseline, build_weight_zone_rule_config(False), sku_zone_rows=zones)
    moved, _ = build_proposed_scenario(model, baseline, build_weight_zone_rule_config(True), sku_zone_rows=zones)
    third, _ = build_proposed_scenario(model, baseline, build_weight_zone_rule_config(False), sku_zone_rows=zones)
    assert first["proposed_state_id"] == baseline["simulation_state_id"] == third["proposed_state_id"]
    assert moved["proposed_state_id"] != baseline["simulation_state_id"]
    assert moved["summary"]["units_moved"] == 2
    assert (model, start, inventory) == originals


def test_metrics_report_missing_zone_without_location_inference():
    model, start, inventory, classifications = _fixture()
    baseline, _ = build_comparison_baseline(
        model, start, inventory, normalized_warehouse="Вешки", operational_date="2026-08-09")
    one_zone = build_sku_zone_rows(classifications[:1])
    scenario, _ = build_proposed_scenario(
        model, baseline, build_weight_zone_rule_config(True), sku_zone_rows=one_zone)
    metrics = summarize_scenario_ui_metrics(scenario, baseline, one_zone)
    assert metrics["missing_zone_skus"] == 1
    assert metrics["missing_zone_placements"] == 1
    assert metrics["fixed_units"] >= 1


def test_distance_table_uses_distance_language_and_service_columns():
    rows = build_distance_order_rows({"orders": [{
        "operational_date": "2026-08-09", "outbound_order_number": "RO-1",
        "current_distance_m": 15, "proposed_distance_m": 7, "distance_saved_m": 8,
        "distance_saved_percent": 53.3, "classification": "improved", "requested_boxes": 4,
        "current_picked_boxes": 4, "proposed_picked_boxes": 4,
        "current_shortage_boxes": 0, "proposed_shortage_boxes": 0, "reasons": [],
    }]})
    assert rows[0]["Статус"] == "Улучшился"
    assert rows[0]["Экономия, м"] == 8
    assert rows[0]["Собрано CURRENT"] == rows[0]["Собрано PROPOSED"] == 4


def _factual_baseline_inputs():
    model = {"model_id": "physical", "cells": [{
        "cell_key": "1|1|1", "row_number": "1", "cell_number": "1", "tier": "1",
        "storage_type": "normal", "capacity_pallets": 1,
    }]}
    table = pd.DataFrame([{"Склад": "  WH ", "РЦ": "DC", "Паллета": "PAL-1", "Ряд": 1,
        "НомерЯчейки": 1, "Ярус": 1, "Номенклатура": "A", "Характеристика": "X",
        "ДатаПроизводства": "2026-08-09", "Количество": 40, "КоличествоПаллет": 1,
        "КоличествоВКоробке": 10, "РасчетноеКоличествоКоробов": 4,
        "КонтрольРасчета": "Расчет выполнен"}])
    start, _ = build_actual_inventory_placement_state(model, table)
    return model, start


def test_factual_start_without_inventory_control_builds_authoritative_current():
    model, start = _factual_baseline_inputs()
    baseline, diagnostics = build_comparison_baseline(
        model, start, None, normalized_warehouse="WH", operational_date="2026-08-09")
    assert baseline is not None
    assert diagnostics["opening_stock"]["inventory_totals_control_status"] == "not_supplied"
    assert diagnostics["opening_stock"].get("inventory_total_mismatch_details") is None
    assert baseline["readiness"]["opening_stock_business_ready"] is True


def test_optional_control_agreement_and_disagreement_preserve_physical_start():
    model, start = _factual_baseline_inputs()
    sku = start["placements"][0]["sku_key"]
    control = {"sku_key": sku, "nomenclature": "A", "characteristic": "X",
               "qty_units": 4, "warehouse": "WH"}
    ready, ready_diag = build_comparison_baseline(
        model, start, [control], normalized_warehouse="WH", operational_date="2026-08-09")
    blocked, blocked_diag = build_comparison_baseline(
        model, start, [control | {"qty_units": 5}], normalized_warehouse="WH", operational_date="2026-08-09")
    assert ready is not None and ready_diag["opening_stock"]["inventory_totals_control_status"] == "agrees"
    assert blocked is not None and blocked["readiness"]["opening_stock_business_ready"] is False
    assert blocked_diag["opening_stock"]["inventory_total_mismatch_details"][0]["delta_boxes"] == -1


def test_supplied_empty_control_has_dedicated_diagnostic_without_fake_mismatches():
    model, start = _factual_baseline_inputs()
    baseline, diagnostics = build_comparison_baseline(
        model, start, [], normalized_warehouse="WH", operational_date="2026-08-09",
        inventory_control_supplied=True)
    assert baseline is None
    assert diagnostics["configuration_errors"] == ["inventory_control_supplied_but_no_valid_rows"]
    assert "inventory_total_mismatch_details" not in diagnostics["opening_stock"]


def test_non_factual_start_without_inventory_control_is_not_promoted():
    model, start, _, _ = _fixture()
    baseline, diagnostics = build_comparison_baseline(
        model, start, None, normalized_warehouse="Вешки", operational_date="2026-08-09")
    assert baseline is not None
    assert baseline["readiness"]["opening_stock_business_ready"] is False
    assert diagnostics["opening_stock"]["legacy_redistribution_used"] is True
