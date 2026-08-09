import copy
import json

import pandas as pd
import pytest

import warehouse_inventory_placement
import warehouse_outbound_orders as outbound


def _model():
    return {
        "model_id": "veshki",
        "rows": [{"row_number": "1", "row_order": 1}, {"row_number": "2", "row_order": 2}],
        "cells": [
            {"row_number": "1", "cell_number": "1", "tier": "1", "y_center": 1.0},
            {"row_number": "1", "cell_number": "2", "tier": "1", "y_center": 2.0},
            {"row_number": "2", "cell_number": "1", "tier": "1", "y_center": 1.0},
        ],
    }


def _placement(cell, units, *, name="Капуста", characteristic="Белая", unit="короб", **extra):
    row, number, tier = cell.split("|")
    result = {
        "placement_id": f"p-{cell}",
        "sku_name": name,
        "item_name": name,
        "characteristic_name": characteristic,
        "row_number": row,
        "cell_number": number,
        "tier": tier,
        "cell_key": cell,
        "qty_units": units,
        "unit_name": unit,
        "qty_pallets": 1,
        "occupied_capacity_pallets": 1,
    }
    result.update(extra)
    return result


def _state(*placements):
    return {"model_id": "veshki", "placements": list(placements), "unplaced_inventory": [], "journal": []}


def _order(number, units, *, created="2026-07-20T10:00:00", index=1, name="Капуста", characteristic="Белая", unit="короб", warehouse="Вешки", qty_raw=None):
    return {
        "outbound_order_number": number,
        "created_at": created,
        "nomenclature": name,
        "characteristic": characteristic,
        "sku_key": outbound.make_outbound_sku_key(name, characteristic),
        "qty_units": units,
        "qty_units_raw": str(units) if qty_raw is None else qty_raw,
        "quantity_validation_reason": "" if isinstance(units, int) and units >= 0 else "invalid_quantity",
        "unit_name": unit,
        "warehouse": warehouse,
        "source_index": index,
        "order_key": outbound.outbound_order_key(warehouse, number, created),
    }


def _execute(placements, rows, execution=None):
    return outbound.execute_outbound_orders(_model(), _state(*placements), rows, execution_state=execution)


def test_normalization_recognizes_columns_and_ignores_weight_for_quantity():
    table = pd.DataFrame([{
        "Номер РО": "RO-1", "Дата создания": "2026-07-20 10:00", "Номенклатура": "Капуста",
        "Характеристика": "Белая", "Количество": 3, "Единица": "короб", "Склад": "Вешки", "Вес": 999,
    }])
    mapping = outbound.detect_outbound_columns(table)
    rows, diagnostics = outbound.normalize_outbound_table(table, mapping)

    assert [item["reason"] for item in diagnostics] == ["pick_order_column_missing"]
    assert rows[0]["route_sequence_authoritative"] is False
    assert rows[0]["qty_units"] == 3
    assert "weight" not in rows[0]
    assert mapping["qty_units"] == "Количество"
    assert mapping["calculated_box_qty"] is None


def test_new_placement_record_keeps_integer_qty_units_and_never_uses_weight():
    record = warehouse_inventory_placement._placement_record(
        {"sku_name": "Капуста", "characteristic_name": "Белая", "qty_units": 7, "unit_name": "короб", "weight": 999},
        {"row_number": "1", "cell_number": "1", "tier": "1", "weight_zone": "light"},
        1,
        "receipt",
        "estimated",
        "calculated",
    )

    assert record["qty_units"] == 7
    assert record["unit_name"] == "короб"
    assert "weight" not in record


@pytest.mark.parametrize("raw,reason", [(1.5, "quantity_fractional"), (-1, "quantity_negative"), ("bad", "quantity_not_numeric")])
def test_invalid_quantity_is_preserved_and_does_not_pick(raw, reason):
    table = pd.DataFrame([{"РО": "RO-1", "Дата РО": "2026-07-20", "Номенклатура": "Капуста", "Количество": raw, "Склад": "Вешки"}])
    rows, _ = outbound.normalize_outbound_table(table, outbound.detect_outbound_columns(table))
    state, execution, log, _ = _execute([_placement("1|1|1", 5)], rows)

    assert state["placements"][0]["qty_units"] == 5
    assert execution["line_results"][0]["line_status"] == "invalid_quantity"
    assert execution["line_results"][0]["failure_reason"] == reason
    assert log == []


def test_orders_are_sorted_by_created_at_then_number_and_change_stock_sequentially():
    rows = [
        _order("RO-2", 3, created="2026-07-20T10:00:00", index=2),
        _order("RO-3", 1, created="2026-07-21T10:00:00", index=3),
        _order("RO-1", 3, created="2026-07-20T10:00:00", index=1),
    ]
    state, execution, log, _ = _execute([_placement("1|1|1", 5)], rows)

    assert [entry["outbound_order_number"] for entry in log] == ["RO-1", "RO-2"]
    assert execution["processed_orders"][rows[2]["order_key"]]["status"] == "completed"
    assert execution["processed_orders"][rows[0]["order_key"]]["status"] == "partially_completed"
    assert execution["processed_orders"][rows[1]["order_key"]]["status"] == "failed"
    assert state["placements"] == []


def test_completed_partial_and_failed_statuses_and_shortage_does_not_stop_lines_or_orders():
    rows = [
        _order("RO-1", 2, index=1),
        _order("RO-1", 10, index=2, name="Салат"),
        _order("RO-2", 1, created="2026-07-20T11:00:00", index=3, name="Морковь"),
    ]
    placements = [_placement("1|1|1", 2), _placement("2|1|1", 1, name="Морковь")]
    state, execution, log, _ = _execute(placements, rows)
    results = execution["line_results"]

    assert [result["line_status"] for result in results] == ["completed", "failed", "completed"]
    assert execution["processed_orders"][rows[0]["order_key"]]["status"] == "partially_completed"
    assert execution["processed_orders"][rows[2]["order_key"]]["status"] == "completed"
    assert len(log) == 2
    assert state["placements"] == []


def test_units_are_picked_across_cells_without_negative_or_fractional_remainders():
    placements = [_placement("1|1|1", 2), _placement("1|2|1", 4)]
    state, execution, log, summary = _execute(placements, [_order("RO-1", 5)])

    assert execution["line_results"][0]["picked_units"] == 5
    assert [(entry["units_before"], entry["picked_units"], entry["units_after"]) for entry in log] == [(2, 2, 0), (4, 3, 1)]
    assert state["placements"][0]["cell_key"] == "1|2|1"
    assert state["placements"][0]["qty_units"] == 1
    assert isinstance(state["placements"][0]["qty_units"], int)
    assert summary["Освобождено ячеек"] == 1


def test_partially_filled_cell_has_priority_when_capacity_units_is_known():
    placements = [
        _placement("1|1|1", 5, capacity_units=5),
        _placement("2|1|1", 2, capacity_units=5),
    ]
    _, _, log, _ = _execute(placements, [_order("RO-1", 1)])

    assert log[0]["cell_key"] == "2|1|1"


def test_unit_mismatch_does_not_change_stock_and_missing_unit_is_allowed_with_warning():
    mismatch_state, mismatch_execution, _, _ = _execute([_placement("1|1|1", 3, unit="мешок")], [_order("RO-1", 2, unit="короб")])
    allowed_state, allowed_execution, _, _ = _execute([_placement("1|1|1", 3, unit="")], [_order("RO-1", 2, unit="")])

    assert mismatch_state["placements"][0]["qty_units"] == 3
    assert mismatch_execution["line_results"][0]["line_status"] == "unit_mismatch"
    assert allowed_state["placements"][0]["qty_units"] == 1
    assert "Единица измерения не указана" in allowed_execution["line_results"][0]["warning"]


def test_compound_warehouse_name_is_accepted_during_execution():
    rows = [_order("RO-1", 2, warehouse="Зона комплектации тестового РЦ")]
    state, execution, log, _ = _execute([_placement("1|1|1", 3)], rows)

    assert execution["line_results"][0]["line_status"] == "completed"
    assert state["placements"][0]["qty_units"] == 1
    assert len(log) == 1


def _real_export_row(**updates):
    row = {
        "СсылкаРО": "ref-1", "НомерРО": "RO-1", "ДатаРО": "2026-07-20", "Склад": "Зона комплектации тестового РЦ",
        "РЦ": "Тестовый РЦ", "НомерСтроки": 4, "ПорядокСборки": 3,
        "КодНоменклатуры": "ITEM-1", "Номенклатура": "Товар A",
        "КодХарактеристики": "CHAR-1", "Характеристика": "Вариант A", "ДатаПроизводства": "2026-07-01",
        "ЕдиницаИзмерения": "кг", "Количество": 115.8, "КоличествоВКоробке": 19.3,
        "РасчетноеОтгруженоКоробок": 6, "КонтрольРасчета": "",
    }
    row.update(updates)
    return row


def test_real_export_exact_columns_and_control_values_are_preserved():
    table = pd.DataFrame([_real_export_row()])
    before = table.copy(deep=True)
    mapping = outbound.detect_outbound_columns(table)
    rows, diagnostics = outbound.normalize_outbound_table(table, mapping)

    assert all(mapping[field] is not None for field in (
        "outbound_order_ref", "outbound_order_number", "created_at", "warehouse", "distribution_center", "line_number",
        "nomenclature_code", "nomenclature", "characteristic_code", "characteristic", "production_date",
        "source_unit_name", "source_quantity", "quantity_per_box", "calculated_box_qty", "calculation_control",
    ))
    assert rows[0]["qty_units"] == 6
    assert rows[0]["unit_name"] == "короб"
    assert rows[0]["source_quantity"] == 115.8
    assert rows[0]["source_unit_name"] == "кг"
    assert rows[0]["quantity_per_box"] == 19.3
    assert rows[0]["line_number"] == 4 and rows[0]["source_index"] == 1
    assert diagnostics == []
    json.dumps(rows, ensure_ascii=False)
    pd.testing.assert_frame_equal(table, before)


def test_calculated_boxes_have_priority_and_are_not_recalculated_in_python():
    table = pd.DataFrame([_real_export_row(**{"Количество": 100, "КоличествоВКоробке": 10, "РасчетноеОтгруженоКоробок": 7})])
    mapping = outbound.detect_outbound_columns(table)
    rows, _ = outbound.normalize_outbound_table(table, mapping)

    assert mapping["source_quantity"] == "Количество"
    assert mapping["quantity_per_box"] == "КоличествоВКоробке"
    assert mapping["calculated_box_qty"] == "РасчетноеОтгруженоКоробок"
    assert rows[0]["qty_units"] == 7


@pytest.mark.parametrize("raw", [6, 6.0, "6", "6,0", "6.0"])
def test_calculated_boxes_accept_integer_representations(raw):
    table = pd.DataFrame([_real_export_row(**{"РасчетноеОтгруженоКоробок": raw})])
    rows, _ = outbound.normalize_outbound_table(table, outbound.detect_outbound_columns(table))
    assert rows[0]["qty_units"] == 6


@pytest.mark.parametrize("raw,reason", [
    (6.5, "quantity_fractional"), (-1, "quantity_negative"), ("bad", "quantity_not_numeric"), ("", "quantity_missing"), (True, "quantity_not_numeric"),
])
def test_invalid_calculated_boxes_have_stable_reason(raw, reason):
    table = pd.DataFrame([_real_export_row(**{"РасчетноеОтгруженоКоробок": raw})])
    rows, diagnostics = outbound.normalize_outbound_table(table, outbound.detect_outbound_columns(table))
    assert rows[0]["quantity_validation_reason"] == reason
    assert diagnostics[0]["reason"] == reason


def test_missing_quantity_per_box_control_is_nonfatal_and_unknown_control_warns():
    table = pd.DataFrame([
        _real_export_row(**{"РасчетноеОтгруженоКоробок": "", "КонтрольРасчета": "КОЛИЧЕСТВО В КОРОБКЕ НЕ НАЙДЕНО"}),
        _real_export_row(**{"НомерРО": "RO-2", "КонтрольРасчета": "Проверить исходные данные"}),
    ])
    rows, diagnostics = outbound.normalize_outbound_table(table, outbound.detect_outbound_columns(table))
    assert (rows[0]["qty_units"], rows[0]["unit_name"], rows[0]["quantity_validation_reason"]) == (0, "короб", "quantity_per_box_missing")
    assert [item["reason"] for item in diagnostics] == ["quantity_per_box_missing", "calculation_control_warning"]


def test_real_export_result_builds_box_pick_demand():
    from warehouse_pick_demands import build_outbound_pick_demands

    table = pd.DataFrame([_real_export_row(**{"РасчетноеОтгруженоКоробок": "7,0"})])
    rows, _ = outbound.normalize_outbound_table(table, outbound.detect_outbound_columns(table))
    demand = build_outbound_pick_demands(rows)["orders"][0]["demands"][0]
    assert demand["requested_units"] == 7
    assert isinstance(demand["requested_units"], int)
    assert demand["unit_name"] == "короб"


def test_processed_order_cannot_be_applied_twice():
    row = _order("RO-1", 2)
    first_state, first_execution, first_log, _ = _execute([_placement("1|1|1", 5)], [row])
    second_state, second_execution, second_log, _ = outbound.execute_outbound_orders(_model(), first_state, [row], first_execution, first_log)

    assert second_state == first_state
    assert second_execution == first_execution
    assert second_log == first_log


def test_one_order_technical_error_rolls_back_only_that_order(monkeypatch):
    rows = [_order("RO-1", 2), _order("RO-2", 1, created="2026-07-20T11:00:00")]
    original_execute = outbound._execute_order

    def fail_first(model, state, order_rows, log):
        if order_rows[0]["outbound_order_number"] == "RO-1":
            raise RuntimeError("test failure")
        return original_execute(model, state, order_rows, log)

    monkeypatch.setattr(outbound, "_execute_order", fail_first)
    state, execution, log, _ = _execute([_placement("1|1|1", 3)], rows)

    assert execution["processed_orders"][rows[0]["order_key"]]["status"] == "failed"
    assert execution["processed_orders"][rows[1]["order_key"]]["status"] == "completed"
    assert state["placements"][0]["qty_units"] == 2
    assert len(log) == 1


def test_snapshot_reset_restores_placements_keeps_orders_and_persists_after_restart(tmp_path, monkeypatch):
    paths = {
        "OUTBOUND_ORDERS_PATH": tmp_path / "outbound_orders.json",
        "OUTBOUND_EXECUTION_STATE_PATH": tmp_path / "outbound_execution_state.json",
        "OUTBOUND_EXECUTION_LOG_PATH": tmp_path / "outbound_execution_log.json",
        "PRE_OUTBOUND_SNAPSHOT_PATH": tmp_path / "pre_outbound_snapshot.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(outbound, name, path)
    monkeypatch.setattr(warehouse_inventory_placement, "PLACEMENTS_PATH", tmp_path / "placements.json")
    placement_state = _state(_placement("1|1|1", 5))
    orders_state = {"model_id": "veshki", "rows": [_order("RO-1", 2)]}
    outbound.save_outbound_orders(orders_state)
    outbound.ensure_pre_outbound_snapshot(placement_state)
    picked, execution, log, _ = outbound.execute_outbound_orders(_model(), placement_state, orders_state["rows"])
    warehouse_inventory_placement.save_placement_state(picked)
    outbound.save_outbound_execution_state(execution)
    outbound.save_outbound_execution_log(log)

    restored, result = outbound.reset_outbound_execution(_model())

    assert result["success"] is True
    assert restored["placements"] == placement_state["placements"]
    assert outbound.load_outbound_orders(_model())["rows"] == orders_state["rows"]
    assert outbound.load_outbound_execution_state(_model())["processed_orders"] == {}
    assert outbound.load_outbound_execution_log() == []
    assert json.loads((tmp_path / "placements.json").read_text(encoding="utf-8"))["placements"][0]["qty_units"] == 5


def test_old_project_without_outbound_files_loads_empty_states(tmp_path, monkeypatch):
    monkeypatch.setattr(outbound, "OUTBOUND_ORDERS_PATH", tmp_path / "missing-orders.json")
    monkeypatch.setattr(outbound, "OUTBOUND_EXECUTION_STATE_PATH", tmp_path / "missing-state.json")
    monkeypatch.setattr(outbound, "OUTBOUND_EXECUTION_LOG_PATH", tmp_path / "missing-log.json")

    assert outbound.load_outbound_orders(_model())["rows"] == []
    assert outbound.load_outbound_execution_state(_model())["processed_orders"] == {}
    assert outbound.load_outbound_execution_log() == []


def test_map_tooltip_reports_before_current_picked_and_last_order(tmp_path, monkeypatch):
    monkeypatch.setattr(outbound, "PRE_OUTBOUND_SNAPSHOT_PATH", tmp_path / "snapshot.json")
    monkeypatch.setattr(outbound, "OUTBOUND_EXECUTION_LOG_PATH", tmp_path / "log.json")
    before = _state(_placement("1|1|1", 5))
    current = _state(_placement("1|1|1", 2))
    outbound.ensure_pre_outbound_snapshot(before)
    outbound.save_outbound_execution_log([{"cell_key": "1|1|1", "outbound_order_number": "RO-9"}])

    enriched = outbound.enrich_model_with_outbound_diagnostics(_model(), current)
    tooltip = enriched["cells"][0]["placement_tooltip"]

    assert "Юнитов до моделирования: 5" in tooltip
    assert "Текущий остаток юнитов: 2" in tooltip
    assert "Списано юнитов: 3" in tooltip
    assert "Последний РО: RO-9" in tooltip
