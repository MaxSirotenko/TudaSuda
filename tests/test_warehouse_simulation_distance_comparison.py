from __future__ import annotations

from warehouse_simulation_distance_comparison import compare_simulation_outbound_replay


def order(key, distance, *, picked=1, shortage=0):
    return {"order_key": key, "outbound_order_number": key, "created_at": "2026-08-09T12:00:00",
            "requested_boxes": picked + shortage, "picked_boxes": picked, "shortage_boxes": shortage,
            "route_distance_m": distance, "returned_to_gate": True, "status": "fulfilled",
            "pick_events": [{}] if picked else [], "demands": [{"demand_key": key, "sku_key": "sku",
                "requested_boxes": picked + shortage, "picked_boxes": picked, "shortage_boxes": shortage}]}


def replay(current, proposed):
    return {"simulation_outbound_replay_id": "sha256:replay", "current": {"orders": current}, "proposed": {"orders": proposed}}


def test_average_median_classification_coverage_and_ids_are_exact():
    current = [order("a", 10), order("b", 20), order("c", 40)]
    proposed = [order("a", 8), order("b", 20), order("c", 50)]
    comparison, diagnostics = compare_simulation_outbound_replay(replay(current, proposed))
    summary = comparison["summary"]
    assert summary["average_current_distance_per_order_m"] == 70 / 3
    assert summary["median_current_distance_per_order_m"] == 20
    assert summary["median_proposed_distance_per_order_m"] == 20
    assert summary["median_saved_per_order_m"] == 0
    assert (summary["improved_orders"], summary["worsened_orders"], summary["equal_orders"]) == (1, 1, 1)
    assert comparison["coverage"]["order_comparability_percent"] == 100
    assert comparison["operational_date"] == "2026-08-09"
    assert comparison["full_day_effect_valid"] is True and comparison["scope"] == "full_day"
    assert comparison["simulation_distance_comparison_id"].startswith("sha256:")
    assert diagnostics["configuration_errors"] == []


def test_invalid_return_is_non_comparable_and_mixed_dates_are_not_guessed():
    current = order("a", 10); current["returned_to_gate"] = False
    proposed = order("a", 5); proposed["created_at"] = "2026-08-10"
    comparison, _ = compare_simulation_outbound_replay(replay([current], [proposed]))
    assert comparison["coverage"]["strict_comparable_orders"] == 0
    assert "not_returned_to_gate" in comparison["orders"][0]["reasons"]
    assert comparison["full_day_effect_valid"] is False
