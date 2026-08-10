from warehouse_monthly_fact_replay import monthly_fact_input_signature, replay_monthly_fact


def test_monthly_readiness_gate_preserves_structured_blockers(tmp_path):
    blocker = {"code": "missing_placement_snapshot", "dates": ["2026-07-01"]}
    result = replay_monthly_fact({}, {}, registry={"datasets": []}, root=tmp_path,
        readiness={"monthly_replay_ready": False, "hard_blockers": [blocker],
                   "active_dataset_signature": "data", "cell_mapping_signature": "cells"}, persist=False)
    assert result["full_month_fact_valid"] is False
    assert blocker in result["blockers"]
    assert result["fact_picker_distance_m"] is None
    assert result["daily_results"] == []


def test_signature_tracks_authority_but_has_no_ui_filter():
    arguments = dict(active_dataset_signature="data-a", cell_mapping_signature="map-a",
        model={"model_id": "warehouse-a", "cells": [{"cell_key": "A"}]},
        gate_state={"model_id": "warehouse-a", "gates": [{"gate_key": "G", "x": 1, "y": 2}]},
        period_from="2026-07-01", period_to="2026-07-31")
    baseline = monthly_fact_input_signature(**arguments)
    assert baseline == monthly_fact_input_signature(**arguments)
    for key, value in (("active_dataset_signature", "data-b"), ("cell_mapping_signature", "map-b"),
                       ("period_to", "2026-07-30")):
        assert baseline != monthly_fact_input_signature(**{**arguments, key: value})
    assert "ui_filter" not in monthly_fact_input_signature.__code__.co_varnames


def test_monthly_fact_module_has_no_proposed_or_optimizer_dependency():
    names = set(replay_monthly_fact.__globals__)
    assert not any("optimizer" in name.casefold() or "proposed" in name.casefold() for name in names)


def test_gate_failure_is_an_authoritative_blocker(tmp_path):
    result = replay_monthly_fact({}, {}, registry={"datasets": []}, root=tmp_path,
        readiness={"monthly_replay_ready": True, "hard_blockers": [],
                   "active_dataset_signature": "data", "cell_mapping_signature": "cells"}, persist=False)
    assert {item["code"] for item in result["blockers"]} == {"authoritative_gate_missing_or_invalid"}
    assert result["days_total"] == 0
