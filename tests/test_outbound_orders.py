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

    assert diagnostics == []
    assert rows[0]["qty_units"] == 3
    assert "weight" not in rows[0]
    assert set(mapping) == {"outbound_order_number", "created_at", "nomenclature", "characteristic", "qty_units", "unit_name", "warehouse"}


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


def test_wrong_warehouse_is_rejected_without_stopping_veshki_order():
    rows = [_order("RO-1", 2, warehouse="Другой"), _order("RO-2", 1, created="2026-07-20T11:00:00")]
    state, execution, log, _ = _execute([_placement("1|1|1", 3)], rows)

    assert execution["line_results"][0]["line_status"] == "wrong_warehouse"
    assert execution["line_results"][1]["line_status"] == "completed"
    assert state["placements"][0]["qty_units"] == 2
    assert len(log) == 1


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
