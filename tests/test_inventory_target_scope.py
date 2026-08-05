import ast
import copy
import json

from warehouse_outbound_orders import make_outbound_sku_key
from warehouse_inventory_target_scope import analyze_inventory_rows_target_scope, analyze_inventory_selection_target_scope

TARGET = [str(n) for n in range(152, 159)]
PICK = "Комплектация Овощи-фрукты Вешки"
STOR = "Овощи Фрукты Вешки"


def sku(name="SKU A", char="Red"):
    return make_outbound_sku_key(name, char)


def model():
    rows = [151, 152, 153, 158, 159, 160]
    return {"model_id": "m", "source_file_hash": "h", "cells": [{"cell_key": f"{r}|1|1", "row_number": str(r), "row_order": i, "cell_number": "1", "tier": "1"} for i, r in enumerate(rows)]}


def inv(warehouse=PICK, qty=20, name="SKU A", char="Red", **extra):
    return {"warehouse": warehouse, "sku_key": sku(name, char), "nomenclature": name, "characteristic": char, "qty_units": qty, "document_key": "d", "inventory_number": "n", **extra}


def place(cell="152|1|1", warehouse=PICK, name="SKU A", char="Red", qty=999, **extra):
    return {"warehouse": warehouse, "sku_key": sku(name, char), "nomenclature": name, "characteristic": char, "cell_key": cell, "qty_units": qty, **extra}


def one_status(inventory, actual, targets=TARGET):
    state, diag = analyze_inventory_rows_target_scope(model(), inventory, actual, targets)
    return state["inventory_scope_records"][0], state, diag


def test_empty_inputs_json_serializable_and_deterministic_and_no_mutation():
    m = model(); rows = []; actual = {}; selection = {"inventory_rows": [], "deferred_inventory_rows": []}
    before = copy.deepcopy((m, rows, actual, selection))
    first = analyze_inventory_selection_target_scope(m, selection, actual, [])
    second = analyze_inventory_selection_target_scope(m, selection, actual, [])
    assert first == second
    json.dumps(first, ensure_ascii=False)
    assert (m, rows, actual, selection) == before
    state, diag = analyze_inventory_rows_target_scope(m, rows, actual, [])
    json.dumps(state, ensure_ascii=False); json.dumps(diag, ensure_ascii=False)
    assert state["inventory_scope_records"] == []


def test_target_rows_normalize_and_empty_is_not_configured():
    rec, state, diag = one_status([inv()], {"placements": [place()]}, [152, 152.0, " 152 "])
    assert state["target_rows"] == ["152"]
    assert rec["status"] == "confirmed_in_target_rows"
    rec, _, diag = one_status([inv()], {"placements": [place()]}, [])
    assert rec["status"] == "target_scope_not_configured"
    assert diag["no_target_rows_configured"] == 1


def test_model_row_wins_and_boundary_rows_are_classified_from_parameter():
    rec, state, _ = one_status([inv()], {"placements": [{**place("152|1|1"), "row_number": "151"}]})
    assert rec["status"] == "confirmed_in_target_rows"
    for cell in ["152|1|1", "158|1|1"]:
        assert one_status([inv()], {"placements": [place(cell)]})[0]["status"] == "confirmed_in_target_rows"
    for cell in ["151|1|1", "159|1|1"]:
        assert one_status([inv()], {"placements": [place(cell)]})[0]["status"] == "confirmed_outside_target_rows"


def test_statuses_same_warehouse_unknown_other_and_none():
    assert one_status([inv()], {"placements": [place("152|1|1")]})[0]["status"] == "confirmed_in_target_rows"
    assert one_status([inv()], {"placements": [place("159|1|1")]})[0]["status"] == "confirmed_outside_target_rows"
    rec, _, _ = one_status([inv()], {"placements": [place("152|1|1"), place("159|1|1")]})
    assert rec["status"] == "mixed_target_and_outside_rows"
    assert rec["qty_units"] == 20
    assert one_status([inv()], {"placements": [place("999|1|1")]})[0]["status"] == "unknown_model_cell_evidence"
    assert one_status([inv(warehouse=STOR)], {"placements": [place("152|1|1", warehouse=PICK)]})[0]["status"] == "location_evidence_in_other_warehouse_only"
    assert one_status([inv()], {})[0]["status"] == "no_location_evidence"


def test_warehouse_normalization_full_match_only_and_sku_key_fallback():
    assert one_status([inv(warehouse=" Склад   Елка ")], {"placements": [place(warehouse="склад ёлка")]})[0]["status"] == "confirmed_in_target_rows"
    assert one_status([inv(warehouse="Фрукты")], {"placements": [place(warehouse="Комплектация Фрукты")]})[0]["status"] == "location_evidence_in_other_warehouse_only"
    row = {k: v for k, v in inv().items() if k != "sku_key"}
    rec, _, _ = one_status([row], {"placements": [place()]})
    assert rec["sku_key"] == sku()
    assert one_status([inv()], {"placements": [{**place(), "sku_key": "READY"}]})[0]["status"] == "no_location_evidence"


def test_inventory_grouping_quantities_and_warehouses_do_not_merge():
    rows = [inv(qty=20, source_index=2), inv(qty=5, source_index=1), inv(warehouse=STOR, qty=100)]
    state, _ = analyze_inventory_rows_target_scope(model(), rows, {"placements": [place(), place(warehouse=STOR, cell="159|1|1", qty=1)]}, TARGET)
    assert [r["qty_units"] for r in state["inventory_scope_records"]] == [25, 100]
    assert state["inventory_scope_records"][0]["reported_in_target_qty_units"] == 999
    assert all(r["reported_quantity_is_authoritative"] is False for r in state["inventory_scope_records"])


def test_evidence_sources_dedup_production_dates_unplaced_and_unusable_diagnostics():
    actual = {
        "placements": [place(production_date="2026-02-01"), place(production_date="2026-01-01")],
        "excluded_inventory": [place(qty=500, production_dates=["2026-03-01"])],
        "unmatched_inventory": [place("999|1|1")],
        "unplaced_inventory": [place("159|1|1")],
    }
    rec, state, diag = one_status([inv(qty=20)], actual)
    assert rec["status"] == "confirmed_in_target_rows"
    assert rec["qty_units"] == 20
    assert rec["has_unknown_model_cell_evidence"] is True
    qualities = rec["evidence_qualities"]
    assert "accepted_location_evidence" in qualities and "non_quantified_location_evidence" in qualities and "unknown_model_cell_evidence" in qualities
    accepted = [e for e in state["placement_evidence"]["in_target_rows"] if e["evidence_quality"] == "accepted_location_evidence"][0]
    assert accepted["production_dates"] == ["2026-01-01", "2026-02-01"]
    assert diag["deduplicated_location_records"] == 3
    bad = {"placements": [place(warehouse=""), {**place(), "sku_key": "", "nomenclature": "", "characteristic": ""}, place(cell="")]}
    _, _, bdiag = one_status([inv()], bad)
    assert bdiag["placement_warehouse_missing"] == 1
    assert bdiag["placement_sku_missing"] == 1
    assert bdiag["placement_cell_key_missing"] == 1


def test_invalid_inventory_rows_are_preserved():
    bads = [inv(qty=""), inv(qty="1.5"), inv(qty=0), inv(warehouse=""), {**inv(), "sku_key": "", "nomenclature": "", "characteristic": ""}]
    state, diag = analyze_inventory_rows_target_scope(model(), bads, {}, TARGET)
    assert [r["reason"] for r in state["invalid_inventory_rows"]] == ["inventory_quantity_missing", "inventory_quantity_invalid", "inventory_quantity_non_positive", "inventory_warehouse_missing", "inventory_sku_missing"]
    assert diag["invalid_inventory_rows"] == 5


def test_selection_and_deferred_are_separate_storage_never_moves_or_merges():
    selection = {"inventory_rows": [inv(warehouse=PICK, qty=20)], "deferred_inventory_rows": [inv(warehouse=STOR, qty=100)]}
    actual = {"placements": [place("152|1|1", warehouse=PICK), place("160|1|1", warehouse=STOR)]}
    state, diag = analyze_inventory_selection_target_scope(model(), selection, actual, TARGET)
    sel = state["selected_inventory_scope"]["inventory_scope_records"][0]
    deff = state["deferred_inventory_scope"]["inventory_scope_records"][0]
    assert (sel["status"], sel["qty_units"]) == ("confirmed_in_target_rows", 20)
    assert (deff["status"], deff["qty_units"]) == ("confirmed_outside_target_rows", 100)
    assert diag["selected_inventory_boxes_total"] == 20 and diag["deferred_inventory_boxes_total"] == 100
    assert state["selected_inventory_scope"]["inventory_scope_records"] == [sel]
    state, _ = analyze_inventory_selection_target_scope(model(), selection, {"placements": [place("152|1|1", warehouse=PICK)]}, TARGET)
    assert state["deferred_inventory_scope"]["inventory_scope_records"][0]["status"] == "location_evidence_in_other_warehouse_only"
    actual = {"placements": [place("153|1|1", warehouse=STOR), place("160|1|1", warehouse=STOR)]}
    state, _ = analyze_inventory_selection_target_scope(model(), selection, actual, TARGET)
    rec = state["deferred_inventory_scope"]["inventory_scope_records"][0]
    assert rec["status"] == "mixed_target_and_outside_rows" and rec["qty_units"] == 100


def test_module_static_constraints():
    text = open("warehouse_inventory_target_scope.py", encoding="utf-8").read()
    tree = ast.parse(text)
    imports = [n.names[0].name for n in tree.body if isinstance(n, ast.Import)] + [n.module for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert "streamlit" not in imports
    assert "pandas" not in imports
    assert "reconcile_opening_stock" not in text
    assert "select_inventory_rows_for_opening_stock" not in text
    assert "working_stock" not in text
    assert ".to_excel" not in text and ".to_csv" not in text and "open(" not in text
    assert "Овощи Фрукты Вешки" not in text and "Комплектация Овощи-фрукты Вешки" not in text
    assert '"152"' not in text and '"158"' not in text
    assert "Запрос" not in text and "ВЫБРАТЬ" not in text
    assert "placements" not in analyze_inventory_selection_target_scope.__name__
