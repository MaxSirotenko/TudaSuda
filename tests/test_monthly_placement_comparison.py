import copy
import json

from warehouse_monthly_placement_comparison import (
    compare_monthly_placement, comparison_input_signature, is_comparison_stale,
)


def _order(distance=20, *, strict=True, demand="same", blockers=None):
    return {"order_identity": {"document_ref": "ref-1", "document_number": "RO-1",
            "occurred_at": "2026-07-01T10:00:00"}, "operational_day": "2026-07-01",
        "requested_boxes": 3, "strict_comparable": strict, "demand_signature": demand,
        "picker_distance_m": distance, "geometry_signature": "geometry",
        "route_legs": [{"distance_m": distance, "path_node_ids": ["gate", "a"], "path_edge_ids": ["e"]}],
        "pick_events": [{"sku_key": "sku:v2:item|characteristic", "cell_key": "1|1|1", "picked_boxes": 3}],
        "blockers": blockers or []}


def _month(order, signature="fact"):
    return {"period_from": "2026-07-01", "period_to": "2026-07-31", "input_signature": signature,
            "artifact_path": "/immutable/fact", "daily_results": [{"order_results": [order]}]}


def _compare(tmp_path, fact=None, proposed=None, assignment=None):
    return compare_monthly_placement(fact_result=fact or _month(_order()),
        proposed_result=proposed or _month(_order(12), "proposed"), proposed_assignments=assignment or [{
            "sku_key": "sku:v2:item|characteristic", "characteristic": "characteristic", "fact_cell": "1|1|1",
            "target_cell": "1|2|1", "zone": "light", "row": "1", "tier": "1", "picking_order": 2,
            "fact_picking_order": 1, "capacity_usage": 1, "placement_reason": "ABC rule"}],
        proposed_placement_signature="placement", warehouse_model_signature="geometry",
        routing_version="physical-v1", ruleset_signature="rules", root=tmp_path)


def test_same_demand_same_order_and_fact_immutability(tmp_path):
    fact = _month(_order()); before = copy.deepcopy(fact)
    result = _compare(tmp_path, fact=fact)
    assert fact == before
    assert result["comparable_orders"] == 1
    assert result["order_comparisons"][0]["order_identity"]["document_number"] == "RO-1"
    assert result["fact_result_reference"] == "/immutable/fact"


def test_physical_route_delta_and_negative_savings_are_not_hidden(tmp_path):
    result = _compare(tmp_path, proposed=_month(_order(30), "proposed"))
    assert result["fact_meters"] == 20
    assert result["proposed_meters"] == 30
    assert result["saved_meters"] == -10
    assert result["saved_percent"] == -50
    assert result["order_comparisons"][0]["fact_route"][0]["path_edge_ids"] == ["e"]


def test_missing_proposed_placement_is_visible_coverage_loss(tmp_path):
    proposed = _month(_order(12, blockers=[{"code": "missing_placement"}]), "proposed")
    result = _compare(tmp_path, proposed=proposed)
    assert result["comparable_orders"] == 0 and result["excluded_orders"] == 1
    assert result["proposed_coverage"] == 0
    assert "missing_placement" in result["warnings"]


def test_month_is_sum_of_daily_not_average_of_percentages(tmp_path):
    fact = _month(_order(20)); proposed = _month(_order(10), "proposed")
    second_fact, second_proposed = _order(80), _order(60)
    for order in (second_fact, second_proposed):
        order["order_identity"] = {"document_ref": "ref-2", "document_number": "RO-2", "occurred_at": "2026-07-02"}
        order["operational_day"] = "2026-07-02"
    fact["daily_results"].append({"order_results": [second_fact]})
    proposed["daily_results"].append({"order_results": [second_proposed]})
    result = _compare(tmp_path, fact=fact, proposed=proposed)
    assert result["fact_meters"] == sum(x["fact_meters"] for x in result["daily_results"]) == 100
    assert result["proposed_meters"] == sum(x["proposed_meters"] for x in result["daily_results"]) == 70
    assert result["saved_meters"] == 30 and result["saved_percent"] == 30


def test_signature_invalidates_business_inputs_not_ui_filter(tmp_path):
    kwargs = dict(fact_result_signature="fact", proposed_placement_signature="placement",
                  warehouse_model_signature="geometry", routing_version="physical-v1", ruleset_signature="rules")
    signature = comparison_input_signature(**kwargs)
    comparison = {"input_signature": signature, "ui_filter": "RO-1"}
    assert not is_comparison_stale(comparison, **kwargs)
    assert not is_comparison_stale({**comparison, "ui_filter": "RO-2"}, **kwargs)
    assert is_comparison_stale(comparison, **{**kwargs, "proposed_placement_signature": "changed"})


def test_persisted_contract_keeps_reasons_contributions_and_references(tmp_path):
    result = _compare(tmp_path)
    stored = json.loads((tmp_path / "monthly_comparisons" / result["input_signature"].replace(":", "_") /
                         "comparison.json").read_text(encoding="utf-8"))
    assert stored["placement_changes"][0]["placement_reason"] == "ABC rule"
    assert stored["contribution_analysis"]
    assert stored["comparison_version"] == "monthly-placement-only-v1"
    assert stored["ruleset_signature"] == "rules"


def test_demand_mismatch_is_excluded_not_silently_compared(tmp_path):
    result = _compare(tmp_path, proposed=_month(_order(12, demand="changed"), "proposed"))
    assert result["comparable_orders"] == 0
    assert "demand_mismatch" in result["warnings"]


def test_module_does_not_import_ai_or_optimizer():
    source = open("warehouse_monthly_placement_comparison.py", encoding="utf-8").read().casefold()
    assert "sklearn" not in source and "torch" not in source and "recommendation" not in source
    assert "proposed_placement_optimizer" not in source
