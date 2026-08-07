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


def test_pipeline_is_not_called_when_inputs_are_not_ready():
    with patch.object(ui, "build_outbound_experiment_inputs", return_value=({"pipeline_inputs_ready": False}, {})), \
         patch.object(ui, "run_outbound_distance_experiment") as pipeline:
        result = ui.calculate_outbound_experiment({}, {}, {}, {}, [], [], [], {})
    pipeline.assert_not_called()
    assert result[2:] == (None, None)


def test_pipeline_receives_exact_builder_pipeline_inputs():
    arguments = {"model": {"model_id": "m"}, "gate_state": {"gates": []}}
    with patch.object(ui, "build_outbound_experiment_inputs",
                      return_value=({"pipeline_inputs_ready": True, "pipeline_inputs": arguments}, {"input": 1})), \
         patch.object(ui, "run_outbound_distance_experiment", return_value=({"execution_status": "completed"}, {"pipeline": 1})) as pipeline:
        result = ui.calculate_outbound_experiment({}, {}, {}, {}, [], [], [], {})
    pipeline.assert_called_once_with(**arguments)
    assert result[2]["execution_status"] == "completed"


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
