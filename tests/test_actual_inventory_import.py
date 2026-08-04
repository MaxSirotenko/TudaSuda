from __future__ import annotations

import copy
from io import BytesIO
import json

import pandas as pd
import pytest

import warehouse_actual_inventory_import as importer
from warehouse_outbound_orders import make_outbound_sku_key
from warehouse_pick_inventory import build_pickable_inventory_index


COLUMNS = [alias[0] for alias in importer.FIELD_ALIASES.values()]


def _model():
    return {
        "model_id": "synthetic-model", "source_file_hash": "synthetic-hash",
        "cells": [
            {"row_number": "101", "cell_number": "1", "tier": "1", "weight_zone": "light"},
            {"row_number": "101", "cell_number": "2", "tier": "1", "weight_zone": "heavy"},
        ],
    }


def _row(**changes):
    row = {
        "Склад": "Тестовый склад", "РЦ": "Тестовый РЦ", "КодЯчейки": "CELL-X",
        "Ячейка": "101-1", "АдресЯчейки": "101-1", "Ряд": "101 - ТЕСТОВАЯ ЗОНА",
        "НомерЯчейки": 1, "Ярус": "Первый", "ПорядокСборки": 10,
        "КодНоменклатуры": "SKU-X", "Номенклатура": "Тестовый товар",
        "КодХарактеристики": "CHAR-X", "Характеристика": "Тестовая характеристика",
        "ДатаПроизводства": "2026-01-01", "ЕдиницаИзмерения": "шт.",
        "Количество": 100, "КоличествоПаллет": 3, "КоличествоВКоробке": 10,
        "РасчетноеКоличествоКоробов": 7, "КонтрольРасчета": "Расчет выполнен",
    }
    row.update(changes)
    return row


def _build(rows):
    return importer.build_actual_inventory_placement_state(_model(), pd.DataFrame(rows))


def test_detects_all_verified_columns_without_quantity_collision():
    mapping = importer.detect_actual_inventory_columns(pd.DataFrame(columns=COLUMNS))
    assert all(mapping.values())
    assert mapping["source_quantity"] == "Количество"
    assert len({mapping["source_quantity"], mapping["pallet_count"], mapping["quantity_per_box"], mapping["calculated_box_qty"]}) == 4
    ambiguous = importer.detect_actual_inventory_columns(
        pd.DataFrame(columns=["КоличествоПаллет", "КоличествоВКоробке", "РасчетноеКоличествоКоробов"])
    )
    assert ambiguous["source_quantity"] is None


def test_excel_helpers_support_flat_and_multiline_headers():
    output = BytesIO()
    with pd.ExcelWriter(output) as writer:
        pd.DataFrame([["Количество", "Ячейка"], [1, "101-1"], [None, None]]).to_excel(
            writer, sheet_name="Остатки", header=False, index=False
        )
    payload = output.getvalue()
    assert importer.get_actual_inventory_sheet_names(payload) == ["Остатки"]
    assert len(importer.read_actual_inventory_table(payload, "Остатки")) == 1

    output = BytesIO()
    columns = pd.MultiIndex.from_tuples([("Товар", "Номенклатура"), ("Остаток", "Количество")])
    # pandas cannot write MultiIndex columns without an index; retain the index here.
    pd.DataFrame([["X", 1]], columns=columns).to_excel(output)
    table = importer.read_actual_inventory_table(output.getvalue(), "Sheet1", header_rows=2)
    assert "Товар / Номенклатура" in table.columns


@pytest.mark.parametrize("tier,expected", [("Первый", "1"), ("Второй", "2"), ("Третий", "3"), ("Четвёртый", "4"), ("Пятый", "5")])
def test_row_cell_and_tier_normalization(tier, expected):
    row = _row(**{"Ярус": tier})
    row["НомерЯчейки"] = 1 if expected != "1" else 2
    model = _model()
    model["cells"] = [{"row_number": "101", "cell_number": str(row["НомерЯчейки"]), "tier": expected}]
    state, _ = importer.build_actual_inventory_placement_state(model, pd.DataFrame([row]))
    placement = state["placements"][0]
    assert placement["row_number"] == "101"
    assert placement["tier"] == expected


@pytest.mark.parametrize("address", ["101-2", "101 / 2"])
def test_address_fallback(address):
    state, _ = _build([_row(**{"Ряд": "", "НомерЯчейки": "", "АдресЯчейки": address})])
    assert state["placements"][0]["cell_key"] == "101|2|1"


def test_known_unknown_and_missing_sku_are_separated():
    state, diagnostics = _build([
        _row(),
        _row(**{"АдресЯчейки": "999-1", "Ряд": "999"}),
        _row(**{"Номенклатура": "", "НомерЯчейки": 2}),
    ])
    assert len(state["placements"]) == 1
    assert state["unmatched_inventory"][0]["reason"] == "unknown_cell"
    assert state["excluded_inventory"][0]["reason"] == "missing_sku"
    assert (diagnostics["unknown_cell"], diagnostics["missing_sku"]) == (1, 1)


@pytest.mark.parametrize("control", ["РАСЧЕТ   ВЫПОЛНЕН", "расчет выполнен"])
def test_success_control_is_case_and_space_insensitive(control):
    state, _ = _build([_row(**{"КонтрольРасчета": control})])
    assert len(state["placements"]) == 1


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"КонтрольРасчета": "Количество в коробке не найдено"}, "quantity_per_box_missing"),
        ({"КонтрольРасчета": ""}, "calculation_not_successful"),
        ({"КоличествоВКоробке": 0}, "quantity_per_box_missing"),
    ],
)
def test_normative_failures_are_excluded(changes, reason):
    state, diagnostics = _build([_row(**changes)])
    assert state["placements"] == []
    assert state["excluded_inventory"][0]["reason"] == reason
    assert diagnostics[reason] == 1


@pytest.mark.parametrize("boxes", [6, 6.0, "6", "6,0", "6.0"])
def test_integral_box_formats_are_int(boxes):
    state, _ = _build([_row(**{"РасчетноеКоличествоКоробов": boxes})])
    placement = state["placements"][0]
    assert placement["qty_units"] == placement["qty_boxes"] == 6
    assert isinstance(placement["qty_units"], int)
    assert placement["unit_name"] == "короб"


@pytest.mark.parametrize(
    "boxes,reason",
    [(1.5, "box_quantity_invalid"), (-1, "box_quantity_non_positive"), (None, "box_quantity_missing"), ("abc", "box_quantity_invalid")],
)
def test_invalid_boxes_are_excluded(boxes, reason):
    state, _ = _build([_row(**{"РасчетноеКоличествоКоробов": boxes})])
    assert state["excluded_inventory"][0]["reason"] == reason


def test_only_calculated_boxes_supply_units_and_pallet_occupancy_is_not_derived():
    state, diagnostics = _build([_row(**{"Количество": 100, "КоличествоПаллет": 55, "РасчетноеКоличествоКоробов": 7})])
    placement = state["placements"][0]
    assert placement["qty_units"] == 7
    assert placement["qty_pallets"] == 55
    assert placement["occupied_capacity_pallets"] == 0.0
    assert placement["occupancy_not_authoritative"] is True
    assert diagnostics["pallet_occupancy_not_derived"] == 1


@pytest.mark.parametrize("date", [None, "", "<Пустая дата>"])
def test_empty_production_date_is_retained_as_empty(date):
    state, diagnostics = _build([_row(**{"ДатаПроизводства": date})])
    assert state["placements"][0]["production_date"] == ""
    assert diagnostics["empty_production_date_rows"] == 1


def test_duplicates_are_excluded_but_distinct_lots_are_accepted():
    first = _row()
    second_lot = _row(**{"ДатаПроизводства": "2026-01-02"})
    state, diagnostics = _build([first, dict(first), second_lot])
    assert len(state["placements"]) == 2
    assert state["excluded_inventory"][0]["reason"] == "duplicate_export_row"
    assert diagnostics["accepted_boxes"] == 14


def test_missing_columns_returns_empty_state_without_error():
    state, diagnostics = importer.build_actual_inventory_placement_state(_model(), pd.DataFrame({"Номенклатура": ["X"]}))
    assert state["placements"] == []
    assert set(diagnostics["missing_required_columns"]) >= {"calculated_box_qty", "quantity_per_box", "calculation_control", "cell_location"}


def test_inputs_and_results_are_json_safe_and_excluded_units_are_separate():
    model = _model()
    table = pd.DataFrame([
        _row(**{"КонтрольРасчета": "", "ЕдиницаИзмерения": "кг", "Количество": 2}),
        _row(**{"КонтрольРасчета": "", "ЕдиницаИзмерения": "шт.", "Количество": 3, "НомерЯчейки": 2}),
    ])
    original_model, original_table = copy.deepcopy(model), table.copy(deep=True)
    state, diagnostics = importer.build_actual_inventory_placement_state(model, table)
    assert model == original_model
    pd.testing.assert_frame_equal(table, original_table)
    assert diagnostics["excluded_source_quantity_by_unit"] == {"кг": 2.0, "шт.": 3.0}
    json.dumps(state, ensure_ascii=False)
    json.dumps(diagnostics, ensure_ascii=False)


def test_pickable_inventory_integration_sums_lots_and_omits_rejected_rows():
    rows = [
        _row(**{"РасчетноеКоличествоКоробов": 4, "ДатаПроизводства": "2026-01-01"}),
        _row(**{"РасчетноеКоличествоКоробов": 3, "ДатаПроизводства": "2026-01-02"}),
        _row(**{"КонтрольРасчета": "", "НомерЯчейки": 2}),
        _row(**{"Ряд": "999", "АдресЯчейки": "999-1"}),
    ]
    state, _ = _build(rows)
    index = build_pickable_inventory_index(_model(), state)
    sku_key = make_outbound_sku_key("Тестовый товар", "Тестовая характеристика")
    assert set(index["by_sku"]) == {sku_key}
    assert set(index["by_cell"]) == {"101|1|1"}
    record = index["by_sku"][sku_key][0]
    assert record["qty_units"] == 7 and isinstance(record["qty_units"], int)
    assert record["unit_name"] == "короб"
    assert len(state["excluded_inventory"]) == 1
    assert len(state["unmatched_inventory"]) == 1
    assert state["unplaced_inventory"] == []


def test_module_has_no_streamlit_or_persistence_dependency():
    source = open(importer.__file__, encoding="utf-8").read()
    assert "import streamlit" not in source
    assert "placements.json" not in source
    assert "save_placement_state" not in source
