from warehouse_outbound_experiment_ui import available_operational_dates, configured_gate_state
from warehouse_workspace_ui import authoritative_analytics_metrics


def test_optional_receipts_cannot_own_operational_dates():
    rows = [
        {"warehouse": "WH", "created_at": "2026-08-02T10:00:00"},
        {"warehouse": "WH", "created_at": "2026-08-01T10:00:00"},
        {"warehouse": "OTHER", "created_at": "2026-07-31T10:00:00"},
    ]
    assert available_operational_dates(rows, "wh") == ["2026-08-01", "2026-08-02"]
    assert available_operational_dates([], "wh") == []


def test_benchmark_gate_is_selected_only_from_persistent_model_gates():
    model = {"model_id": "m", "gates": [
        {"gate_key": "g1", "x": 1, "y": 2}, {"gate_key": "g2", "x": 3, "y": 4},
    ]}
    assert configured_gate_state(model) is None
    assert configured_gate_state(model, "g2") == {
        "model_id": "m", "gates": [{"gate_key": "g2", "x": 3, "y": 4}],
    }
    assert configured_gate_state({"model_id": "m", "gates": []}) is None


def test_analytics_adapter_uses_exact_authoritative_business_keys():
    summary = {
        "current_picker_distance_m": 100, "proposed_picker_distance_m": 70,
        "picker_distance_saved_m": 30, "picker_distance_saved_percent": 30,
        "orders_total": 2, "current_picked_boxes": 8, "proposed_picked_boxes": 8,
        "current_shortage_boxes": 0, "proposed_shortage_boxes": 0,
        "service_equivalent": True,
    }
    assert authoritative_analytics_metrics({
        "full_day_effect_valid": True, "authoritative_summary": summary,
    }) == summary
    assert authoritative_analytics_metrics({
        "full_day_effect_valid": False, "authoritative_summary": summary,
    }) is None
