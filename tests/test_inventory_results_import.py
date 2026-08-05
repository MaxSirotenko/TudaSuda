import ast
import copy
import json
from io import BytesIO

import pandas as pd

import warehouse_inventory_results_import as inv
from warehouse_outbound_orders import make_outbound_sku_key
from warehouse_opening_stock_reconciliation import reconcile_opening_stock
from warehouse_pick_inventory import build_pickable_inventory_index

COLS = {
    "СсылкаИнвентаризации": "ref1", "НомерИнвентаризации": "N1", "ДатаИнвентаризации": "2026-07-30",
    "Склад": "WH", "РЦ": "DC", "НомерСтроки": 1, "КодНоменклатуры": "C1", "Номенклатура": "SKU A",
    "КодХарактеристики": "CH1", "Характеристика": "Red", "ЕдиницаИзмерения": "шт",
    "ФактическоеКоличество": 400, "УчетноеКоличество": 0, "Расхождение": 400,
    "КоличествоВКоробке": 10, "РасчетноеКоличествоКоробов": 40, "КонтрольРасчета": "Расчет выполнен",
}

def df(*rows):
    return pd.DataFrame([{**COLS, **r} for r in rows])

def build(*rows):
    return inv.build_inventory_results_import(df(*rows))

def test_empty_dataframe_and_missing_required_columns_are_safe():
    state, diag = inv.build_inventory_results_import(pd.DataFrame())
    assert state == {"documents": [], "document_keys": [], "accepted_rows": [], "zero_inventory_rows": [], "excluded_inventory_rows": [], "unassigned_rows": []}
    assert diag["rows_total"] == 0
    state, diag = inv.build_inventory_results_import(pd.DataFrame({"foo": [1]}))
    assert state["accepted_rows"] == []
    assert "inventory_number" in diag["missing_required_columns"]

def test_state_diagnostics_json_serializable_and_input_unchanged_and_deterministic():
    source = df({})
    before = source.copy(deep=True)
    first = inv.build_inventory_results_import(source)
    second = inv.build_inventory_results_import(source)
    pd.testing.assert_frame_equal(source, before)
    assert first == second
    json.dumps(first, ensure_ascii=False)

def test_excel_bytes_sheet_names_and_multiline_header():
    buf = BytesIO()
    with pd.ExcelWriter(buf) as writer:
        df({}).to_excel(writer, sheet_name="Data", index=False)
    assert inv.get_inventory_results_sheet_names(buf.getvalue()) == ["Data"]
    assert len(inv.read_inventory_results_table(buf.getvalue(), "Data")) == 1
    multi = BytesIO()
    columns = pd.MultiIndex.from_tuples([("Документ", "НомерИнвентаризации"), ("Документ", "Склад")])
    pd.DataFrame([["N1", "WH"]], columns=columns).to_excel(multi, sheet_name="M", index=True)
    table = inv.read_inventory_results_table(multi.getvalue(), "M", header_rows=2)
    assert any("НомерИнвентаризации" in c for c in table.columns)

def test_column_detection_exact_russian_and_ambiguous_fuzzy_not_selected():
    mapping = inv.detect_inventory_results_columns(df({}))
    assert mapping["inventory_number"] == "НомерИнвентаризации"
    ambiguous = pd.DataFrame(columns=["Мое Количество", "Другое Количество"])
    assert inv.detect_inventory_results_columns(ambiguous)["quantity_per_box"] is None

def test_document_keys_ref_fallback_single_document_and_warehouses_not_merged():
    state, _ = build({}, {"НомерСтроки": 2})
    assert len(set(r["document_key"] for r in state["accepted_rows"])) == 1
    assert state["accepted_rows"][0]["document_key"] == "ref:ref1"
    state2, _ = build({"СсылкаИнвентаризации": ""})
    assert state2["accepted_rows"][0]["document_key"].startswith("sha256:")
    assert state2 == build({"СсылкаИнвентаризации": ""})[0]
    state3, _ = build({}, {"СсылкаИнвентаризации": "ref2", "НомерИнвентаризации": "N2", "Склад": "WH2"})
    assert len(state3["documents"]) == 2

def test_sku_key_and_box_quantity_source_only_no_recalculation():
    state, _ = build({"ФактическоеКоличество": 999, "КоличествоВКоробке": 3, "РасчетноеКоличествоКоробов": "40,0"})
    row = state["accepted_rows"][0]
    assert row["sku_key"] == make_outbound_sku_key("SKU A", "Red")
    assert row["qty_units"] == 40
    assert row["actual_quantity"] == 999 and row["quantity_per_box"] == 3

def test_quantity_validation_accepts_integer_forms_and_rejects_bad_forms():
    for value in [40, 40.0, "40", "40,0", "40.0"]:
        assert build({"РасчетноеКоличествоКоробов": value})[0]["accepted_rows"][0]["qty_units"] == 40
    for value, reason in [("40.5", "box_quantity_invalid"), (-1, "box_quantity_non_positive"), (True, "box_quantity_missing"), ("", "box_quantity_missing")]:
        state, diag = build({"РасчетноеКоличествоКоробов": value})
        assert not state["accepted_rows"]
        assert diag[reason] == 1

def test_controls_zero_and_excluded_reasons():
    state, diag = build({"КонтрольРасчета": "Количество в коробке не найдено", "КоличествоВКоробке": ""})
    assert state["excluded_inventory_rows"][0]["reason"] == "quantity_per_box_missing"
    state, diag = build({"КонтрольРасчета": "Фактический остаток не положительный", "РасчетноеКоличествоКоробов": 0})
    assert not state["accepted_rows"] and state["zero_inventory_rows"][0]["reason"] == "confirmed_non_positive_inventory"
    assert diag["box_quantity_non_positive"] == 0
    assert build({"КонтрольРасчета": "???"})[0]["excluded_inventory_rows"][0]["reason"] == "calculation_not_successful"
    assert build({"КонтрольРасчета": ""})[0]["excluded_inventory_rows"][0]["reason"] == "calculation_control_missing"

def test_duplicate_rows_unique_sku_and_document_variance_warnings():
    state, diag = build({"ФактическоеКоличество": 10, "УчетноеКоличество": 10, "Расхождение": 0}, {"НомерСтроки": 2, "ФактическоеКоличество": 10, "УчетноеКоличество": 10, "Расхождение": 0})
    assert len(state["accepted_rows"]) == 2
    assert diag["unique_sku_count"] == 1 and diag["duplicate_sku_rows_within_document"] == 1
    assert "document_without_variances" in state["documents"][0]["warnings"]
    assert not build({})[0]["documents"][0]["warnings"]

def test_selection_is_explicit_full_normalized_warehouse_and_no_latest_choice():
    state, _ = build({}, {"СсылкаИнвентаризации": "ref2", "НомерИнвентаризации": "N2", "Склад": "Другой Склад", "РасчетноеКоличествоКоробов": 99})
    empty, diag = inv.select_inventory_rows_for_opening_stock(state, [], ["WH"])
    assert empty["inventory_rows"] == [] and diag["no_documents_selected"] == 1
    sel, _ = inv.select_inventory_rows_for_opening_stock(state, ["ref:ref1"], ["wh"])
    assert [r["qty_units"] for r in sel["inventory_rows"]] == [40]
    sel, _ = inv.select_inventory_rows_for_opening_stock(state, ["ref:ref1"], ["W"])
    assert sel["inventory_rows"] == [] and sel["deferred_documents"][0]["reason"] == "warehouse_outside_target_scope"
    yo_state, _ = build({"Склад": "Склад Ёлка"})
    sel, _ = inv.select_inventory_rows_for_opening_stock(yo_state, ["ref:ref1"], [" склад елка "])
    assert len(sel["selected_documents"]) == 1

def test_deferred_storage_not_counted_and_same_sku_not_merged_before_selection():
    state, _ = build({"Склад": "Storage", "РасчетноеКоличествоКоробов": 100}, {"СсылкаИнвентаризации": "pick", "НомерИнвентаризации": "P", "Склад": "Picking", "РасчетноеКоличествоКоробов": 20})
    sel, diag = inv.select_inventory_rows_for_opening_stock(state, ["ref:ref1", "ref:pick"], ["Picking"])
    assert [r["qty_units"] for r in sel["inventory_rows"]] == [20]
    assert [r["qty_units"] for r in sel["deferred_inventory_rows"]] == [100]
    assert diag["selected_inventory_boxes"] == 20 and diag["deferred_inventory_boxes"] == 100

def test_multiple_selected_documents_same_warehouse_rejected_not_summed_but_different_warehouses_ok():
    same, _ = build({}, {"СсылкаИнвентаризации": "r2", "НомерИнвентаризации": "N2", "РасчетноеКоличествоКоробов": 60})
    sel, diag = inv.select_inventory_rows_for_opening_stock(same, ["ref:ref1", "ref:r2"], ["WH"])
    assert sel["inventory_rows"] == [] and diag["multiple_selected_documents_for_warehouse"] == 2
    diff, _ = build({}, {"СсылкаИнвентаризации": "r2", "НомерИнвентаризации": "N2", "Склад": "WH2", "РасчетноеКоличествоКоробов": 60})
    sel, _ = inv.select_inventory_rows_for_opening_stock(diff, ["ref:ref1", "ref:r2"], ["WH", "WH2"])
    assert sum(r["qty_units"] for r in sel["inventory_rows"]) == 100

def test_zero_and_excluded_rows_for_selected_document_preserved_separately():
    state, _ = build({}, {"НомерСтроки": 2, "КонтрольРасчета": "Фактический остаток не положительный", "РасчетноеКоличествоКоробов": 0}, {"НомерСтроки": 3, "КонтрольРасчета": "???"})
    sel, _ = inv.select_inventory_rows_for_opening_stock(state, ["ref:ref1"], ["WH"])
    assert len(sel["zero_inventory_rows"]) == 1 and len(sel["excluded_inventory_rows"]) == 1

def test_integration_storage_100_picking_20_reconciles_and_pickable_is_20_not_120():
    storage = "Овощи Фрукты Вешки"; picking = "Комплектация Овощи-фрукты Вешки"
    state, _ = build({"Склад": storage, "РасчетноеКоличествоКоробов": 100}, {"СсылкаИнвентаризации": "pick", "НомерИнвентаризации": "P", "Склад": picking, "РасчетноеКоличествоКоробов": 20})
    selection, _ = inv.select_inventory_rows_for_opening_stock(state, ["ref:ref1", "ref:pick"], [picking])
    model = {"model_id": "m", "cells": [{"cell_key": "152|1|1", "row_number": "152", "cell_number": "1", "tier": "1"}]}
    actual = {"placements": [{"sku_key": make_outbound_sku_key("SKU A", "Red"), "cell_key": "152|1|1", "qty_units": 1}]}
    reconciled, rdiag = reconcile_opening_stock(model, selection["inventory_rows"], actual)
    index = build_pickable_inventory_index(model, reconciled)
    assert sum(r["qty_units"] for r in selection["inventory_rows"]) == 20
    assert sum(r["qty_units"] for r in selection["deferred_inventory_rows"]) == 100
    assert rdiag["inventory_boxes_total"] == 20
    assert sum(item["qty_units"] for item in index["by_sku"][make_outbound_sku_key("SKU A", "Red")]) == 20

def test_module_has_no_streamlit_writes_hardcoded_warehouses_or_row_range():
    tree = ast.parse(open("warehouse_inventory_results_import.py", encoding="utf-8").read())
    imports = [n.names[0].name for n in tree.body if isinstance(n, ast.Import)] + [n.module for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert "streamlit" not in imports
    text = open("warehouse_inventory_results_import.py", encoding="utf-8").read()
    assert ".to_excel" not in text and ".to_csv" not in text and "open(" not in text
    assert "Овощи Фрукты Вешки" not in text and "Комплектация Овощи-фрукты Вешки" not in text
    assert "152" not in text and "158" not in text
