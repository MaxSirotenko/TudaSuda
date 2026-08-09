"""Authoritative one-day opening-stock outbound CURRENT/PROPOSED benchmark.

This is the sole business-metric orchestration layer.  Receipts are deliberately
outside V1; an END snapshot, when supplied, is only independent validation.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from warehouse_proposed_scenario import build_proposed_scenario
from warehouse_simulation_distance_comparison import compare_simulation_outbound_replay
from warehouse_simulation_outbound_replay import replay_outbound_on_simulation_states
from warehouse_simulation_state import summarize_simulation_state, validate_simulation_state

BENCHMARK_VERSION = 1
LIMITATIONS = [
    "opening_stock_outbound_only_no_intraday_receipts",
    "intermediate_full_picking_pallet_return_not_modeled",
    "dynamic_passage_opening_not_modeled",
    "deep_lane_internal_access_not_modeled",
    "replenishment_distance_is_loaded_one_way_transfer_only",
]


def _end_validation(working_state: Mapping[str, Any], end_snapshot: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not end_snapshot:
        return None
    replay_boxes: dict[str, float] = defaultdict(float)
    factual_boxes: dict[str, float] = defaultdict(float)
    for lot in working_state.get("stock_lots", []) or []:
        replay_boxes[str(lot.get("sku_key"))] += float(lot.get("qty_boxes") or 0)
    for row in end_snapshot.get("placements", end_snapshot.get("stock_lots", [])) or []:
        factual_boxes[str(row.get("sku_key"))] += float(row.get("qty_boxes", row.get("qty_units", 0)) or 0)
    keys = sorted(set(replay_boxes) | set(factual_boxes))
    return {"validation_only": True, "sku_coverage_percent": 100.0 * len(set(replay_boxes) & set(factual_boxes)) / len(keys) if keys else 100.0,
            "differences_by_sku": [{"sku_key": key, "replay_boxes": replay_boxes[key],
                                    "factual_end_boxes": factual_boxes[key],
                                    "difference_boxes": replay_boxes[key] - factual_boxes[key]} for key in keys]}


def run_warehouse_day_benchmark(
    model: dict[str, Any], current_state: dict[str, Any], outbound_demand_state: dict[str, Any],
    gate_state: dict[str, Any], rule_config: dict[str, Any], *,
    sku_zone_rows: list[dict[str, Any]] | None = None,
    sku_velocity_rows: list[dict[str, Any]] | None = None,
    end_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build PROPOSED fresh from immutable CURRENT and run strict same-demand replay."""
    blockers: list[str] = []
    readiness = current_state.get("readiness", {}) or {}
    if not readiness.get("opening_stock_business_ready"):
        blockers.append("opening_stock_not_business_ready")
    if readiness.get("estimated_opening_allocation"):
        blockers.append("estimated_opening_allocation_forbidden")
    if outbound_demand_state.get("readiness", {}).get("route_sequence_authoritative") is not True:
        blockers.append("factual_pick_sequence_missing_or_invalid")
    current_validation = validate_simulation_state(current_state, model)
    if not current_validation["valid"]:
        blockers.append("invalid_current_state")
    scenario, scenario_diagnostics = build_proposed_scenario(
        model, current_state, rule_config, sku_zone_rows=sku_zone_rows or [],
        sku_velocity_rows=sku_velocity_rows, gate_state=gate_state)
    proposed = scenario.get("proposed_state")
    if scenario.get("status") != "ready" or not proposed:
        blockers.append("proposed_not_ready")
    proposed_validation = validate_simulation_state(proposed, model) if proposed else {"valid": False, "errors": []}
    if proposed and not proposed_validation["valid"]:
        blockers.append("invalid_proposed_state")
    replay: dict[str, Any] = {}
    replay_diagnostics: dict[str, Any] = {}
    comparison: dict[str, Any] = {}
    comparison_diagnostics: dict[str, Any] = {}
    if not blockers:
        replay, replay_diagnostics = replay_outbound_on_simulation_states(
            model, current_state, proposed, outbound_demand_state, gate_state,
            placement_rule_set=scenario.get("placement_rule_set"))
        if not replay:
            blockers.extend(replay_diagnostics.get("configuration_errors", []))
        else:
            comparison, comparison_diagnostics = compare_simulation_outbound_replay(replay)
            if not comparison.get("full_day_effect_valid"):
                blockers.append("full_day_service_equivalence_failed")
            for side in ("current", "proposed"):
                summary = replay.get(side, {}).get("summary", {})
                if not summary.get("conservation_valid") or not summary.get("post_replay_state_valid", True):
                    blockers.append(f"{side}_post_replay_state_invalid")
    current_summary = summarize_simulation_state(current_state)
    orders = outbound_demand_state.get("orders", []) or []
    checklist = {"opening_boxes": current_summary.get("total_boxes", 0),
                 "opening_skus": current_summary.get("sku_count", 0),
                 "exact_normal_pallets": current_summary.get("exact_positioned_pallet_units", 0),
                 "unresolved_physical_boxes": current_summary.get("unknown_location_boxes", 0),
                 "route_sequence_authoritative": outbound_demand_state.get("readiness", {}).get("route_sequence_authoritative") is True,
                 "outbound_ros": len(orders),
                 "requested_boxes": sum(float(d.get("requested_boxes", d.get("requested_units", 0)) or 0)
                                        for order in orders for d in order.get("demands", []) or []),
                 "source_location_ambiguity_count": replay.get("current", {}).get("summary", {}).get("source_location_ambiguous_orders", 0),
                 "gate_ready": not any("gate" in item for item in blockers),
                 "proposed_ready": proposed is not None and proposed_validation["valid"],
                 "enabled_rules": scenario.get("summary", {}).get("enabled_rule_ids", [])}
    result = {"warehouse_day_benchmark_version": BENCHMARK_VERSION,
              "benchmark_status": "ready" if not blockers else "blocked", "blockers": blockers,
              "current_rules": None, "proposed_placement_rule_set": scenario.get("placement_rule_set"),
              "current_state_id": current_state.get("simulation_state_id"),
              "proposed_state_id": proposed.get("simulation_state_id") if proposed else None,
              "outbound_demand_state_id": outbound_demand_state.get("outbound_demand_state_id"),
              "readiness_checklist": checklist, "replay": replay, "comparison": comparison,
              "end_snapshot_validation": _end_validation(replay.get("current", {}).get("working_state", {}), end_snapshot) if replay else None,
              "limitations": LIMITATIONS.copy()}
    diagnostics = {"current_validation": current_validation, "proposed_validation": proposed_validation,
                   "scenario": scenario_diagnostics, "replay": replay_diagnostics,
                   "comparison": comparison_diagnostics}
    return result, diagnostics
