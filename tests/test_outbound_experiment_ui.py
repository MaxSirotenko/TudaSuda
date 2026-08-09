from pathlib import Path
from unittest.mock import patch

import warehouse_outbound_experiment_ui as ui


def test_signature_is_deterministic_and_sensitive():
    first = ui.build_experiment_ui_signature(warehouse="A", slotting_rows=[{"sku": "1", "zone": "heavy"}])
    assert first == ui.build_experiment_ui_signature(slotting_rows=[{"zone": "heavy", "sku": "1"}], warehouse="A")
    assert first != ui.build_experiment_ui_signature(warehouse="B", slotting_rows=[{"sku": "1", "zone": "heavy"}])


def test_stale_result_detection():
    assert ui.experiment_result_is_stale("new", "old")
    assert not ui.experiment_result_is_stale("same", "same")
    assert not ui.experiment_result_is_stale("new", None)


def test_active_ui_has_no_legacy_business_metric_pipeline():
    source = Path(ui.__file__).read_text(encoding="utf-8")
    assert "run_outbound_distance_experiment" not in source
    assert "Рассчитать эксперимент" not in source
    assert "render_scenario_comparison(" in source


def test_missing_slotting_does_not_block_otherwise_ready_inputs():
    assert ui.experiment_inputs_ready(day_receipt_state={"receipt_sku_batches": [{}]},
        start_state={"placements": [{}]}, end_state={"placements": [{}]},
        opening_inventory_rows=[{}], outbound_rows=[{}], gate_confirmed=True)


def test_optional_inputs_do_not_block_but_start_orders_and_gate_remain_mandatory():
    ready = dict(day_receipt_state=None, end_state=None, opening_inventory_rows=None)
    assert ui.experiment_inputs_ready(**ready, start_state={"placements": [{}]},
                                      outbound_rows=[{}], gate_confirmed=True)
    assert not ui.experiment_inputs_ready(**ready, start_state=None, outbound_rows=[{}], gate_confirmed=True)
    assert not ui.experiment_inputs_ready(**ready, start_state={"placements": [{}]},
                                          outbound_rows=[], gate_confirmed=True)
    assert not ui.experiment_inputs_ready(**ready, start_state={"placements": [{}]},
                                          outbound_rows=[{}], gate_confirmed=False)


def test_start_warehouse_selection_is_normalized_and_never_merges_scopes():
    state = {"placements": [{"warehouse": " WH  "}, {"normalized_warehouse": "WH"}]}
    assert ui.start_warehouses(state) == ["wh"]
    assert ui.select_start_warehouse(["wh"]) == ("wh", None)
    assert ui.select_start_warehouse(["a", "b"]) == (
        None, "multiple_start_warehouses_require_selection")
    assert ui.select_start_warehouse(["a", "b"], " B ") == ("b", None)


def test_outbound_scope_reports_warehouse_and_exact_date_failures():
    rows = [{"warehouse": "A", "created_at": "2026-08-09T10:00:00"}]
    assert ui.validate_outbound_scope(rows, "B", "2026-08-09") == [
        "start_outbound_warehouse_scope_mismatch"]
    assert ui.validate_outbound_scope(rows, "A", "2026-08-10") == [
        "selected_operational_date_has_no_accepted_outbound_orders"]
    assert ui.validate_outbound_scope(rows, " a ", "2026-08-09") == []


def test_ui_copy_keeps_end_and_receipts_optional_and_one_headline_path():
    source = Path(ui.__file__).read_text(encoding="utf-8")
    assert "END snapshot — необязательно, только валидация" in source
    assert "Дневной приход — не используется в V1 benchmark" in source
    assert "Инвентаризация / независимый контроль количества — необязательно" in source
    assert "run_outbound_distance_experiment" not in source


def test_effect_sign_and_none_percent_are_not_recalculated():
    assert ui.describe_distance_effect(12) == "Улучшение"
    assert ui.describe_distance_effect(-12) == "Ухудшение"
    assert ui.format_experiment_metric(None) == "—"


def test_order_rows_keep_service_fields():
    row = ui.build_experiment_order_rows([{"classification": "worsened", "current_picked_units": 8,
        "proposed_picked_units": 7, "current_shortage_units": 1, "proposed_shortage_units": 2,
        "reasons": ["shortage_units_mismatch"]}])[0]
    assert row["Статус сравнения"] == "Хуже"
    assert (row["Собрано CURRENT"], row["Собрано PROPOSED"]) == (8, 7)
    assert (row["Дефицит CURRENT"], row["Дефицит PROPOSED"]) == (1, 2)
