"""Pure projection of authoritative application state onto the six UX steps.

No optimizer, graph builder or replay is called here.  Staleness is determined
only by comparing IDs/signatures already stored by their owning business layer.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STEP_NAMES = ("Склад", "Данные", "Условия", "PROPOSED", "Пробег", "Аналитика")


def _yes(source: Mapping[str, Any], *keys: str) -> bool:
    return all(bool(source.get(key)) for key in keys)


def derive_workflow_ui_state(source: Mapping[str, Any]) -> dict[str, Any]:
    """Derive readiness from existing readiness flags and authoritative IDs."""
    warehouse = bool(source.get("model_exists")) and bool(source.get("geometry_valid")) and not source.get("geometry_blockers")
    data = warehouse and _yes(source, "start_ready", "warehouse_selected", "operational_date_selected", "outbound_orders_loaded", "pick_order_authoritative", "mandatory_data_checks_passed")
    rules = data and bool(source.get("ruleset_valid")) and bool(source.get("rule_dependencies_valid", True))
    proposed_exists = bool(source.get("proposed_exists"))
    proposed_stale = proposed_exists and (bool(source.get("proposed_stale")) or any(source.get(a) != source.get(b) for a, b in (
        ("proposed_geometry_signature", "geometry_signature"), ("proposed_data_signature", "data_signature"),
        ("proposed_rules_signature", "rules_signature")) if source.get(a) is not None or source.get(b) is not None))
    proposed = rules and proposed_exists and not proposed_stale
    benchmark_exists = bool(source.get("benchmark_exists"))
    benchmark_stale = benchmark_exists and (bool(source.get("benchmark_stale")) or proposed_stale or any(source.get(a) != source.get(b) for a, b in (
        ("benchmark_proposed_id", "proposed_id"), ("benchmark_demand_signature", "demand_signature"),
        ("benchmark_rules_signature", "rules_signature")) if source.get(a) is not None or source.get(b) is not None))
    benchmark_prereqs = proposed and bool(source.get("gate_valid")) and bool(source.get("benchmark_prerequisites_ready", True)) and bool(source.get("pick_order_authoritative"))
    benchmark = benchmark_prereqs and benchmark_exists and not benchmark_stale
    analytics = benchmark
    ready = (warehouse, data, rules, proposed, benchmark, analytics)
    stale = (False, False, False, proposed_stale, benchmark_stale, benchmark_stale)
    first_incomplete = next((i for i, value in enumerate(ready) if not value), len(ready) - 1)
    steps = []
    for index, name in enumerate(STEP_NAMES):
        status = "stale" if stale[index] else "completed" if ready[index] else "current" if index == first_incomplete else "available" if all(ready[:index]) else "blocked"
        steps.append({"number": index + 1, "name": name, "ready": ready[index], "status": status})
    blockers = []
    if not source.get("model_exists"): blockers.append("warehouse_model_missing")
    if warehouse and not source.get("start_ready"): blockers.append("missing_start")
    if warehouse and not source.get("gate_valid"): blockers.append("gate_missing")
    if source.get("outbound_orders_loaded") and not source.get("pick_order_authoritative"): blockers.append("factual_pick_sequence_missing_or_invalid")
    if proposed_stale: blockers.append("proposed_stale")
    if benchmark_stale: blockers.append("benchmark_stale")
    return {"steps": steps, "warehouse_ready": warehouse, "data_ready": data, "current_ready": data,
            "rules_ready": rules, "proposed_ready": proposed, "proposed_stale": proposed_stale,
            "benchmark_available": benchmark_prereqs, "benchmark_ready": benchmark,
            "benchmark_stale": benchmark_stale, "analytics_ready": analytics, "blockers": blockers}


def state_from_session(model: Mapping[str, Any] | None, session: Mapping[str, Any]) -> dict[str, Any]:
    """Small adapter; it reads session state but never writes to it."""
    current = session.get("placement_comparison_current_state") or {}
    demand = session.get("placement_comparison_outbound_demand_state") or {}
    benchmark = session.get("placement_comparison_benchmark") or {}
    proposed = benchmark.get("proposed_state") or session.get("placement_comparison_proposed_state") or {}
    comparison = session.get("placement_comparison_distance_comparison") or benchmark.get("comparison") or {}
    return derive_workflow_ui_state({
        "model_exists": bool(model), "geometry_valid": bool(model) and not model.get("configuration_errors"),
        "geometry_blockers": model.get("configuration_errors", []) if model else ["missing"],
        "start_ready": bool(current) and current.get("readiness", {}).get("opening_stock_business_ready", True),
        "warehouse_selected": bool(session.get("outbound_selected_warehouse") or current.get("warehouse_key") or model),
        "operational_date_selected": bool(session.get("outbound_selected_date") or demand.get("operational_date")),
        "outbound_orders_loaded": bool(demand.get("orders")),
        "pick_order_authoritative": demand.get("readiness", {}).get("route_sequence_authoritative") is True,
        "mandatory_data_checks_passed": bool(current) and bool(demand),
        "ruleset_valid": bool(session.get("workspace_rule_config")),
        "rule_dependencies_valid": True, "proposed_exists": bool(proposed),
        "proposed_stale": bool(session.get("workspace_proposed_stale")),
        "benchmark_exists": bool(comparison), "benchmark_stale": bool(session.get("workspace_benchmark_stale")),
        "gate_valid": bool(session.get("placement_comparison_gate_state") or model and model.get("gates")),
    })
