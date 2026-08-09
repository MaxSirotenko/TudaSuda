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
