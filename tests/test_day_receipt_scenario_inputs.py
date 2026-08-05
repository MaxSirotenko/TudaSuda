import ast
import copy
import datetime as dt
import json
from pathlib import Path

import warehouse_day_receipt_scenario_inputs as mod


def r(**overrides):
    base = {
        "receipt_line_key": "line-a",
        "document_key": "doc-a",
        "receipt_ref": "ref-a",
        "receipt_number": "A",
        "receipt_date": "2026-07-30 10:00:00",
        "warehouse": "Warehouse A",
        "distribution_center": "DC",
        "line_number": 1,
        "sku_key": "sku-x",
        "nomenclature_code": "NX",
        "nomenclature": "SKU X",
        "characteristic_code": "CX",
        "characteristic": "RED",
        "qty_units": 10,
        "unit_name": "короб",
        "source_box_quantity": 999,
        "reported_pallets": 7,
        "terminal_receipt_completed": True,
        "expected_receipt": True,
        "source_index": 1,
    }
    base.update(overrides)
    return base


def build(rows, **kwargs):
    state = {"accepted_rows": rows, "pending_receipt_rows": [r(receipt_line_key="p")], "zero_receipt_rows": [r(receipt_line_key="z")], "excluded_receipt_rows": [r(receipt_line_key="e")], "unassigned_rows": [r(receipt_line_key="u")]}
    return mod.build_day_receipt_scenario_inputs(state, operational_date=kwargs.pop("operational_date", "2026-07-30"), selected_warehouses=kwargs.pop("selected_warehouses", ["Warehouse A"]), **kwargs)


def integration_rows():
    return [
        r(receipt_line_key="a1", document_key="doc-a", receipt_number="A", receipt_date="30.07.2026 10:00:00", sku_key="sku-x", qty_units=386, source_index=1),
        r(receipt_line_key="b1", document_key="doc-b", receipt_number="B", receipt_date="30.07.2026 17:48:00", sku_key="sku-x", qty_units=54, source_index=2),
        r(receipt_line_key="c1", document_key="doc-c", receipt_number="C", receipt_date="30.07.2026 12:00:00", sku_key="sku-y", nomenclature="SKU Y", qty_units=20, source_index=3),
        r(receipt_line_key="d1", document_key="doc-d", receipt_number="D", receipt_date="31.07.2026", sku_key="sku-z", qty_units=100, source_index=4),
        r(receipt_line_key="e1", document_key="doc-e", receipt_number="E", receipt_date="30.07.2026", warehouse="Warehouse B", sku_key="sku-z", qty_units=50, source_index=5),
    ]


def test_empty_import_state_json_and_empty_scenarios():
    state, diag = mod.build_day_receipt_scenario_inputs({}, operational_date="2026-07-30", selected_warehouses=["Warehouse A"])
    assert state["receipt_lines"] == state["receipt_sku_batches"] == state["selected_documents"] == []
    assert state["scenario_inputs"]["current"] == state["scenario_inputs"]["proposed"]
    assert state["scenario_inputs"]["current"]["total_boxes"] == 0
    assert diag["source_accepted_rows"] == 0 and diag["aggregation_reduction_percent"] == 0.0
    json.dumps(state, ensure_ascii=False)
    json.dumps(diag, ensure_ascii=False)


def test_input_unchanged_deterministic_order_and_qty_changes_dataset():
    rows = integration_rows()
    original = copy.deepcopy({"accepted_rows": rows})
    first, _ = mod.build_day_receipt_scenario_inputs(original, operational_date=dt.date(2026, 7, 30), selected_warehouses=["Warehouse A"])
    second, _ = mod.build_day_receipt_scenario_inputs(original, operational_date=dt.datetime(2026, 7, 30, 8), selected_warehouses=["Warehouse A"])
    shuffled, _ = build(list(reversed(rows)))
    changed, _ = build([*rows[:1], r(**{**rows[1], "qty_units": 55}), *rows[2:]])
    assert original == {"accepted_rows": rows}
    assert first == second
    assert first["dataset_id"] == shuffled["dataset_id"]
    assert first["dataset_id"] != changed["dataset_id"]
    assert [x["qty_units"] for x in first["receipt_lines"]] == [386, 20, 54]


def test_operational_date_inputs_and_configuration_errors():
    for value in (dt.date(2026, 7, 30), dt.datetime(2026, 7, 30, 1), "2026-07-30", "2026-07-30T02:03:04"):
        state, diag = build([r()], operational_date=value)
        assert state["operational_date"] == "2026-07-30" and not diag["configuration_errors"]
    _, diag = build([r()], operational_date="not a date")
    assert "invalid_operational_date" in diag["configuration_errors"]
    _, diag = build([r()], selected_warehouses=[" "])
    assert "selected_warehouses_empty" in diag["configuration_errors"]


def test_warehouse_normalization_full_equality_and_no_hardcoded_names():
    state, _ = build([r(warehouse="  Склад   Ёж  ")], selected_warehouses=["склад еж"])
    assert len(state["receipt_lines"]) == 1
    state, diag = build([r(warehouse="Warehouse ABC")], selected_warehouses=["Warehouse A"])
    assert state["receipt_lines"] == [] and diag["rows_wrong_warehouse"] == 1
    text = Path("warehouse_day_receipt_scenario_inputs.py").read_text(encoding="utf-8").casefold()
    assert "овощи" not in text and "комплектация" not in text


def test_document_filter_modes_and_diagnostics():
    rows = integration_rows()
    all_state, _ = build(rows, selected_document_keys=None)
    none_state, diag = build(rows, selected_document_keys=[])
    assert {d["document_key"] for d in all_state["selected_documents"]} == {"doc-a", "doc-b", "doc-c"}
    assert none_state["receipt_lines"] == [] and diag["rows_filtered_by_document"] == 3
    _, diag = build(rows, selected_document_keys=["missing", "doc-d", "doc-e"])
    assert diag["unknown_selected_document_keys"] == ["missing"]
    assert diag["selected_document_keys_outside_scope"] == ["doc-d", "doc-e"]


def test_only_accepted_source_sections_are_counted_not_included():
    state, diag = build([r(qty_units=5)])
    assert diag["source_pending_receipt_rows"] == diag["source_zero_receipt_rows"] == diag["source_excluded_receipt_rows"] == 1
    assert diag["source_unassigned_rows"] == 1
    assert len(state["receipt_lines"]) == 1 and state["receipt_lines"][0]["receipt_line_key"] == "line-a"


def test_accepted_row_validation_reasons_and_no_fallback_sources():
    cases = [
        ("bad", "accepted_row_not_mapping"),
        (r(receipt_line_key=""), "receipt_line_key_missing"),
        (r(document_key=""), "document_key_missing"),
        (r(receipt_date="bad"), "receipt_date_invalid"),
        (r(warehouse=""), "warehouse_missing"),
        (r(sku_key=""), "sku_missing"),
        (r(qty_units=None, source_box_quantity=5), "qty_units_missing"),
        (r(qty_units=True), "qty_units_invalid"),
        (r(qty_units=1.5), "qty_units_invalid"),
        (r(qty_units=0), "qty_units_non_positive"),
        (r(qty_units=-1, reported_pallets=5), "qty_units_non_positive"),
        (r(unit_name="штука"), "unit_not_boxes"),
        (r(terminal_receipt_completed="True"), "terminal_receipt_not_completed"),
    ]
    for row, reason in cases:
        state, diag = build([row])
        assert state["receipt_lines"] == []
        assert state["invalid_accepted_rows"][0]["reason"] == reason
        assert diag["invalid_accepted_rows"] == 1
    state, _ = build([r(unit_name="короб"), r(receipt_line_key="b", document_key="b", unit_name="короба"), r(receipt_line_key="c", document_key="c", unit_name="коробов")])
    assert len(state["receipt_lines"]) == 3


def test_duplicate_and_scope_counters_do_not_double_count():
    rows = [r(qty_units=10), r(qty_units=99), r(receipt_line_key="other-date", document_key="d", receipt_date="2026-07-31"), r(receipt_line_key="other-wh", document_key="w", warehouse="Warehouse B")]
    state, diag = build(rows)
    assert len(state["receipt_lines"]) == 1 and state["scenario_inputs"]["current"]["total_boxes"] == 10
    assert diag["duplicate_receipt_line_keys"] == 1
    assert diag["rows_wrong_operational_date"] == 1 and diag["rows_wrong_warehouse"] == 1


def test_integration_386_54_20_batches_documents_and_scenarios():
    state, diag = build(integration_rows())
    assert diag["selected_receipt_rows"] == 3
    assert diag["selected_receipt_boxes"] == 460
    assert [line["qty_units"] for line in state["receipt_lines"]] == [386, 20, 54]
    assert any("17:48" in line["receipt_date"] for line in state["receipt_lines"])
    assert len(state["receipt_sku_batches"]) == 2
    batches = {b["sku_key"]: b for b in state["receipt_sku_batches"]}
    assert batches["sku-x"]["qty_units"] == 440
    assert batches["sku-y"]["qty_units"] == 20
    assert batches["sku-x"]["receipt_line_keys"] == ["a1", "b1"]
    assert batches["sku-x"]["document_keys"] == ["doc-a", "doc-b"]
    assert diag["rows_wrong_operational_date"] == 1 and diag["rows_wrong_warehouse"] == 1
    assert state["scenario_inputs"]["current"] == state["scenario_inputs"]["proposed"]
    assert state["scenario_inputs"]["current"]["receipt_dataset_id"] == state["dataset_id"]
    assert state["scenario_inputs"]["current"]["total_boxes"] == 460
    assert diag["aggregation_reduction_rows"] == 1 and diag["aggregation_reduction_percent"] == round(1 / 3 * 100, 4)


def test_same_sku_different_warehouses_not_merged_and_document_totals_from_lines():
    rows = [r(receipt_line_key="a", document_key="doc", sku_key="sku", qty_units=2), r(receipt_line_key="b", document_key="doc", sku_key="sku", qty_units=3), r(receipt_line_key="c", document_key="doc2", warehouse="Warehouse B", sku_key="sku", qty_units=4)]
    state, _ = build(rows, selected_warehouses=["Warehouse A", "Warehouse B"])
    assert len(state["receipt_lines"]) == 3
    assert len(state["receipt_sku_batches"]) == 2
    assert sorted(b["qty_units"] for b in state["receipt_sku_batches"]) == [4, 5]
    doc = next(d for d in state["selected_documents"] if d["document_key"] == "doc")
    assert doc["total_boxes"] == 5 and doc["row_count"] == 2 and doc["unique_sku_count"] == 1


def test_batch_key_scenario_lists_are_deterministic_and_not_shared():
    state, _ = build(integration_rows())
    again, _ = build(integration_rows())
    assert [b["receipt_batch_key"] for b in state["receipt_sku_batches"]] == [b["receipt_batch_key"] for b in again["receipt_sku_batches"]]
    cur = state["scenario_inputs"]["current"]
    prop = state["scenario_inputs"]["proposed"]
    assert cur["receipt_line_keys"] == prop["receipt_line_keys"]
    assert cur["receipt_batch_keys"] == prop["receipt_batch_keys"]
    assert cur["document_keys"] == prop["document_keys"]
    cur["receipt_line_keys"].append("mutated")
    assert "mutated" not in prop["receipt_line_keys"]


def test_deterministic_sorting_for_lines_batches_and_documents():
    rows = [r(receipt_line_key="l3", document_key="d3", receipt_number="C", line_number="x", source_index=9, sku_key="b"), r(receipt_line_key="l1", document_key="d1", receipt_number="A", line_number="2", source_index=2, sku_key="a"), r(receipt_line_key="l2", document_key="d2", receipt_number="A", line_number="1", source_index=1, sku_key="c")]
    state, _ = build(rows)
    assert [x["receipt_line_key"] for x in state["receipt_lines"]] == ["l2", "l1", "l3"]
    assert [x["sku_key"] for x in state["receipt_sku_batches"]] == ["a", "b", "c"]
    assert [x["document_key"] for x in state["selected_documents"]] == ["d1", "d2", "d3"]


def test_static_constraints_no_forbidden_imports_writes_or_domain_outputs():
    text = Path("warehouse_day_receipt_scenario_inputs.py").read_text(encoding="utf-8").casefold()
    tree = ast.parse(text)
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
    assert "pandas" not in imports and "streamlit" not in imports
    forbidden = ["open(", ".write_text", ".write_bytes", "placement", "working_stock", "route", "queries_1c", "16:00", ".hour"]
    assert not any(item in text for item in forbidden)
