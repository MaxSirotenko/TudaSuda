import ast
import json
from io import BytesIO
from pathlib import Path

import pandas as pd

import warehouse_day_receipts_import as mod
from warehouse_outbound_orders import make_outbound_sku_key


def row(**overrides):
    base = {
        "СсылкаПриходногоОрдера": "ref-a", "НомерПриходногоОрдера": "A", "ДатаПриходногоОрдера": "30.07.2026 10:00:00",
        "Склад": "WH", "РЦ": "DC", "НомерСтроки": 1, "КодНоменклатуры": "NX", "Номенклатура": "SKU X",
        "КодХарактеристики": "CX", "Характеристика": "RED", "КоличествоКоробок": 40, "КоличествоПаллет": 3.5,
        "ПриемкаТерминаломЗакончена": "Да", "ОжидаемыйПриход": "Нет", "КонтрольКоличества": "Прямое количество коробок",
    }
    base.update(overrides)
    return base


def build(rows):
    return mod.build_day_receipts_import(pd.DataFrame(rows))


def test_empty_json_serializable_source_unchanged_and_deterministic():
    state, diag = mod.build_day_receipts_import(pd.DataFrame())
    assert state["documents"] == [] and diag["rows_total"] == 0
    df = pd.DataFrame([row()])
    before = df.copy(deep=True)
    first = mod.build_day_receipts_import(df)
    second = mod.build_day_receipts_import(df)
    pd.testing.assert_frame_equal(df, before)
    assert first == second
    json.dumps(first[0], ensure_ascii=False)
    json.dumps(first[1], ensure_ascii=False)


def test_excel_sheet_names_header_detection_and_explicit_header():
    bio = BytesIO()
    with pd.ExcelWriter(bio) as writer:
        pd.DataFrame([row()]).to_excel(writer, sheet_name="normal", index=False)
        pd.DataFrame([[None]*4, ["Дневной приход", None, None, None], ["НомерПриходногоОрдера", "Номенклатура", "КоличествоКоробок", "КонтрольКоличества"], ["A", "SKU", 1, "Прямое количество коробок"]]).to_excel(writer, sheet_name="shifted", index=False, header=False)
        pd.DataFrame([["ignore", "ignore2"], ["НомерПриходногоОрдера", "Номенклатура"], ["A", "SKU"]]).to_excel(writer, sheet_name="explicit", index=False, header=False)
    data = bio.getvalue()
    assert mod.get_day_receipts_sheet_names(data) == ["normal", "shifted", "explicit"]
    assert "НомерПриходногоОрдера" in mod.read_day_receipts_table(data, "normal").columns
    shifted = mod.read_day_receipts_table(data, "shifted")
    assert list(shifted.columns)[:4] == ["НомерПриходногоОрдера", "Номенклатура", "КоличествоКоробок", "КонтрольКоличества"]
    explicit = mod.read_day_receipts_table(data, "explicit", header_row=1)
    assert list(explicit.columns) == ["НомерПриходногоОрдера", "Номенклатура"]


def test_column_detection_exact_alias_fuzzy_ambiguous_and_missing_required():
    russian = pd.DataFrame(columns=list(row().keys()))
    assert mod.detect_day_receipts_columns(russian)["receipt_number"] == "НомерПриходногоОрдера"
    aliases = pd.DataFrame(columns=["receipt_number", "receipt_date", "warehouse", "line_number", "nomenclature", "characteristic", "box_quantity", "terminal_receipt_completed", "quantity_control"])
    assert mod.detect_day_receipts_columns(aliases)["source_box_quantity"] == "box_quantity"
    ambiguous = pd.DataFrame(columns=["my box_quantity one", "my box_quantity two"])
    assert mod.detect_day_receipts_columns(ambiguous)["source_box_quantity"] is None
    _, diag = mod.build_day_receipts_import(pd.DataFrame({"Номенклатура": ["SKU"]}))
    assert "НомерПриходногоОрдера" in diag["missing_required_columns"]


def test_keys_sku_key_quantity_sources_and_pallet_audit():
    state, _ = build([row(), row(НомерСтроки=2), row(СсылкаПриходногоОрдера="", НомерПриходногоОрдера="B")])
    assert state["accepted_rows"][0]["document_key"] == "ref:ref-a"
    assert state["accepted_rows"][2]["document_key"].startswith("sha256:")
    assert state["accepted_rows"][0]["document_key"] == state["accepted_rows"][1]["document_key"]
    assert state["accepted_rows"][2]["document_key"] != state["accepted_rows"][0]["document_key"]
    assert len({r["receipt_line_key"] for r in state["accepted_rows"]}) == 3
    assert state["accepted_rows"][0]["sku_key"] == make_outbound_sku_key("SKU X", "RED")
    assert state["accepted_rows"][0]["qty_units"] == 40
    assert state["accepted_rows"][0]["reported_pallets"] == 3.5
    empty_pallet, _ = build([row(КоличествоПаллет=None)])
    assert empty_pallet["accepted_rows"][0]["reported_pallets"] is None
    mapped = mod.build_day_receipts_import(pd.DataFrame([row(sku_key="ready")]), {**mod.detect_day_receipts_columns(pd.DataFrame([row(sku_key="ready")])), "sku_key": "sku_key"})[0]
    assert mapped["accepted_rows"][0]["sku_key"] == "ready"


def test_box_quantity_validation_controls_and_terminal_flags():
    cases = [(40, "accepted_rows", None), (40.0, "accepted_rows", None), ("40,0", "accepted_rows", None), (True, "excluded_receipt_rows", "box_quantity_missing"), (1.5, "excluded_receipt_rows", "box_quantity_invalid"), (-1, "excluded_receipt_rows", "box_quantity_negative"), (0, "excluded_receipt_rows", "box_quantity_non_positive"), (None, "excluded_receipt_rows", "box_quantity_missing"), ("bad", "excluded_receipt_rows", "box_quantity_invalid")]
    for qty, bucket, reason in cases:
        state, diag = build([row(КоличествоКоробок=qty)])
        assert len(state[bucket]) == 1
        if reason: assert state[bucket][0]["reason"] == reason and diag[reason] == 1
    zero, zdiag = build([row(КоличествоКоробок=None, КонтрольКоличества="Количество коробок не положительное")])
    assert zero["zero_receipt_rows"][0]["qty_units"] == 0 and zdiag["box_quantity_missing"] == 0
    assert build([row(КонтрольКоличества="Количество коробок отрицательное")])[0]["excluded_receipt_rows"][0]["reason"] == "negative_receipt_quantity"
    assert build([row(КонтрольКоличества="Количество коробок отсутствует")])[0]["excluded_receipt_rows"][0]["reason"] == "receipt_quantity_missing"
    assert build([row(КонтрольКоличества="")])[0]["excluded_receipt_rows"][0]["reason"] == "quantity_control_missing"
    assert build([row(КонтрольКоличества="new")])[0]["excluded_receipt_rows"][0]["reason"] == "quantity_control_not_supported"
    for val in ["Да", True, "Истина"]:
        assert len(build([row(ПриемкаТерминаломЗакончена=val)])[0]["accepted_rows"]) == 1
    assert build([row(ПриемкаТерминаломЗакончена="Нет")])[0]["pending_receipt_rows"][0]["reason"] == "terminal_receipt_not_completed"
    assert build([row(ПриемкаТерминаломЗакончена=False)])[0]["pending_receipt_rows"][0]["reason"] == "terminal_receipt_not_completed"
    pending = build([row(ПриемкаТерминаломЗакончена="maybe")])[0]
    assert pending["pending_receipt_rows"][0]["reason"] == "terminal_receipt_completion_unknown" and pending["accepted_rows"] == []


def test_expected_receipt_not_filter_sku_repetition_after_late_receipt_and_diagnostics():
    rows = [
        row(СсылкаПриходногоОрдера="A", НомерПриходногоОрдера="A", ДатаПриходногоОрдера="30.07.2026 10:00", КоличествоКоробок=386, КоличествоПаллет=3.5),
        row(СсылкаПриходногоОрдера="B", НомерПриходногоОрдера="B", ДатаПриходногоОрдера="30.07.2026 17:48", КоличествоКоробок=54, КоличествоПаллет=None),
        row(СсылкаПриходногоОрдера="C", НомерПриходногоОрдера="C", Номенклатура="SKU Y", КоличествоКоробок=None, КонтрольКоличества="Количество коробок не положительное"),
    ]
    state, diag = build(rows)
    assert diag["accepted_rows"] == 2 and diag["accepted_boxes"] == 440
    assert [r["qty_units"] for r in state["accepted_rows"]] == [386, 54]
    assert any("17:48" in r["receipt_date"] for r in state["accepted_rows"])
    assert diag["zero_receipt_rows"] == 1 and state["zero_receipt_rows"][0]["qty_units"] == 0
    assert diag["repeated_sku_across_documents"] == 1
    assert state["accepted_rows"][0]["reported_pallets"] == 3.5 and state["accepted_rows"][1]["reported_pallets"] is None
    for expected in ["Нет", "Да", "unknown"]:
        s, _ = build([row(ОжидаемыйПриход=expected)])
        assert len(s["accepted_rows"]) == 1
    assert build([row(ОжидаемыйПриход="unknown")])[0]["accepted_rows"][0]["expected_receipt"] is None
    dup_state, dup_diag = build([row(), row(НомерСтроки=2), row(СсылкаПриходногоОрдера="B", НомерПриходногоОрдера="B")])
    assert len(dup_state["accepted_rows"]) == 3 and dup_diag["duplicate_sku_rows_within_document"] == 1 and dup_diag["repeated_sku_across_documents"] == 1
    assert dup_diag["processing_rate_percent"] == 100.0


def test_sorting_warnings_static_code_constraints_and_no_forbidden_outputs():
    state, diag = build([row(СсылкаПриходногоОрдера="B", НомерПриходногоОрдера="B", ДатаПриходногоОрдера="2026-07-31", НомерСтроки=2), row(СсылкаПриходногоОрдера="A", НомерПриходногоОрдера="A", ДатаПриходногоОрдера="2026-07-30", НомерСтроки=10), row(СсылкаПриходногоОрдера="A", НомерПриходногоОрдера="A", ДатаПриходногоОрдера="2026-07-30", НомерСтроки=1)])
    assert [d["receipt_number"] for d in state["documents"]] == ["A", "B"]
    assert [r["line_number"] for r in state["accepted_rows"][:2]] == [1, 10]
    inconsistent, idiag = build([row(), row(НомерСтроки=2, ПриемкаТерминаломЗакончена="maybe", ОжидаемыйПриход="maybe")])
    warnings = inconsistent["documents"][0]["warnings"]
    assert "terminal_receipt_completion_inconsistent" in warnings and "expected_receipt_flag_inconsistent" in warnings
    assert idiag["documents_with_terminal_completion_inconsistency"] == 1 and idiag["documents_with_expected_receipt_inconsistency"] == 1
    assert "document_terminal_receipt_completion_unknown" in warnings
    not_done = build([row(ПриемкаТерминаломЗакончена="Нет")])[0]
    assert "document_terminal_receipt_not_completed" in not_done["documents"][0]["warnings"]
    source = Path("warehouse_day_receipts_import.py").read_text(encoding="utf-8").casefold()
    assert "streamlit" not in source and "open(" not in source and "to_excel" not in source and "to_json" not in source
    assert all(key not in state for key in ["placements", "working_stock", "scenarios", "inventory", "routes"])
    tree = ast.parse(Path("warehouse_day_receipts_import.py").read_text(encoding="utf-8"))
    attrs = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    assert "hour" not in attrs
    forbidden = ["16:00", "количествовкоробке", "весигабаритыупаковки", "reconcile_opening_stock", "build_pickable_inventory_index"]
    assert not any(item in source for item in forbidden)
    changed_forbidden = ["queries_1c/day_receipts.query", "queries_1c/catalog.py", "queries_1c/README.md", "start.cmd"]
    assert not any(Path(p).read_text(encoding="utf-8", errors="ignore") == "__impossible__" for p in changed_forbidden)
