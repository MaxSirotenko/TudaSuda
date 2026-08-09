from warehouse_workflow_ui_state import STEP_NAMES, derive_workflow_ui_state


def complete(**changes):
    state = dict(model_exists=True, geometry_valid=True, geometry_blockers=[], start_ready=True,
                 warehouse_selected=True, operational_date_selected=True, outbound_orders_loaded=True,
                 pick_order_authoritative=True, mandatory_data_checks_passed=True, ruleset_valid=True,
                 rule_dependencies_valid=True, proposed_exists=True, gate_valid=True,
                 benchmark_prerequisites_ready=True, benchmark_exists=True,
                 geometry_signature="g1", proposed_geometry_signature="g1", data_signature="d1",
                 proposed_data_signature="d1", rules_signature="r1", proposed_rules_signature="r1",
                 proposed_id="p1", benchmark_proposed_id="p1", demand_signature="d1",
                 benchmark_demand_signature="d1", benchmark_rules_signature="r1")
    state.update(changes)
    return state


def test_missing_model_blocks_every_step_and_ready_warehouse_unlocks_data():
    missing = derive_workflow_ui_state({})
    assert not any(s["ready"] for s in missing["steps"])
    assert derive_workflow_ui_state(complete(start_ready=False))["steps"][1]["status"] == "current"


def test_data_and_rules_unlock_in_sequence():
    assert not derive_workflow_ui_state(complete(start_ready=False))["rules_ready"]
    assert derive_workflow_ui_state(complete(ruleset_valid=False))["data_ready"]
    assert not derive_workflow_ui_state(complete(ruleset_valid=False))["proposed_ready"]
    assert derive_workflow_ui_state(complete(benchmark_exists=False))["benchmark_available"]


def test_full_workflow_is_complete():
    result = derive_workflow_ui_state(complete())
    assert STEP_NAMES == ("Склад", "Данные", "Условия", "PROPOSED", "Пробег", "Аналитика")
    assert all(step["ready"] for step in result["steps"])
    assert result["current_ready"] and result["analytics_ready"]


def test_rules_or_geometry_change_stales_downstream_but_not_current():
    for change in ({"rules_signature": "r2"}, {"geometry_signature": "g2"}):
        result = derive_workflow_ui_state(complete(**change))
        assert result["warehouse_ready"] and result["data_ready"] and result["current_ready"]
        assert result["proposed_stale"] and result["benchmark_stale"] and not result["analytics_ready"]
        assert "proposed_stale" in result["blockers"]


def test_presentation_only_state_does_not_stale_results():
    baseline = derive_workflow_ui_state(complete())
    for change in ({"analytics_filter": "x"}, {"selected_ro": "RO-2"}, {"technical_expander_open": True}):
        assert derive_workflow_ui_state(complete(**change)) == baseline
