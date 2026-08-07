"""Pure orchestration for the outbound CURRENT versus PROPOSED experiment."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from warehouse_outbound_scenario_comparison import compare_outbound_replay_scenarios
from warehouse_outbound_scenario_replay import replay_outbound_scenarios
from warehouse_physical_graph import build_physical_warehouse_graph
from warehouse_receipt_current_placements import build_current_receipt_placements
from warehouse_receipt_proposed_placements import build_proposed_receipt_placements
from warehouse_receipt_snapshot_transitions import analyze_receipt_snapshot_transitions
from warehouse_receipt_virtual_slots import build_receipt_virtual_slots


STAGES = (
    "transition_analysis",
    "virtual_slots",
    "current_receipt_placements",
    "proposed_receipt_placements",
    "physical_graph",
    "outbound_replay",
    "comparison",
)

STAGE_ID_FIELDS = {
    "transition_analysis": "analysis_id",
    "virtual_slots": "virtual_slot_state_id",
    "current_receipt_placements": "current_receipt_placement_state_id",
    "proposed_receipt_placements": "proposed_receipt_placement_state_id",
    "physical_graph": "physical_graph_state_id",
    "outbound_replay": "outbound_replay_state_id",
    "comparison": "outbound_comparison_state_id",
}

LIMITATIONS = [
    "pipeline_orchestrates_existing_stages_without_reimplementing_business_logic",
    "pipeline_starts_from_prebuilt_day_receipt_scenario_state",
    "raw_excel_ingestion_is_outside_pipeline",
    "one_normalized_warehouse_per_pipeline_run",
    "unresolved_receipts_are_valid_partial_evidence_not_pipeline_failure",
    "outbound_shortages_are_valid_replay_results_not_pipeline_failure",
    "partial_comparison_is_not_pipeline_failure",
    "physical_graph_must_be_ready_before_replay",
    "pipeline_does_not_persist_state",
    "pipeline_does_not_modify_inputs",
    "pipeline_does_not_modify_warehouse_geometry",
    "pipeline_does_not_render_ui",
    "pipeline_does_not_calculate_financial_effect",
    "comparison_metrics_are_inherited_from_comparison_stage",
    "route_algorithm_is_inherited_from_replay_stage",
]

SUMMARY_FIELDS = (
    "comparison_status", "scope", "full_day_effect_valid", "accepted_orders",
    "strict_comparable_orders", "comparable_route_orders", "non_comparable_orders",
    "current_raw_distance_m", "proposed_raw_distance_m",
    "current_comparable_distance_m", "proposed_comparable_distance_m",
    "distance_saved_m", "improvement_percent", "order_comparability_percent",
    "route_order_coverage_percent", "requested_units_coverage_percent",
    "current_shortage_units", "proposed_shortage_units",
    "current_receipt_unresolved_qty_units", "proposed_receipt_unresolved_qty_units",
)


def _normalized_warehouse(value: Any) -> str:
    return str(value or "").strip().casefold().replace("ё", "е")


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _configuration_errors(diagnostics: Any) -> list[str]:
    if not isinstance(diagnostics, Mapping):
        return ["invalid_stage_diagnostics"]
    errors = diagnostics.get("configuration_errors", [])
    if isinstance(errors, list):
        return sorted({str(error) for error in errors})
    # The physical graph contract currently reports a count.
    if isinstance(errors, int) and not isinstance(errors, bool):
        return ["physical_graph_configuration_errors"] if errors else []
    return ["invalid_configuration_errors_contract"] if errors else []


def _comparison_summary(comparison: Mapping[str, Any] | None, execution_status: str) -> dict[str, Any]:
    summary = {field: None for field in SUMMARY_FIELDS}
    summary["execution_status"] = execution_status
    if not comparison:
        return summary
    raw = comparison.get("raw_summary") if isinstance(comparison.get("raw_summary"), Mapping) else {}
    comparable = comparison.get("comparable_summary") if isinstance(comparison.get("comparable_summary"), Mapping) else {}
    coverage = comparison.get("coverage") if isinstance(comparison.get("coverage"), Mapping) else {}
    quality = comparison.get("quality") if isinstance(comparison.get("quality"), Mapping) else {}
    sources = {
        "comparison_status": quality, "scope": quality, "full_day_effect_valid": quality,
        "accepted_orders": coverage, "strict_comparable_orders": coverage,
        "comparable_route_orders": coverage, "order_comparability_percent": coverage,
        "route_order_coverage_percent": coverage, "requested_units_coverage_percent": coverage,
        "non_comparable_orders": comparable, "current_comparable_distance_m": comparable,
        "proposed_comparable_distance_m": comparable, "distance_saved_m": comparable,
        "improvement_percent": comparable, "current_shortage_units": raw,
        "proposed_shortage_units": raw, "current_receipt_unresolved_qty_units": raw,
        "proposed_receipt_unresolved_qty_units": raw,
    }
    aliases = {
        "current_raw_distance_m": "current_route_distance_m",
        "proposed_raw_distance_m": "proposed_route_distance_m",
    }
    for field in SUMMARY_FIELDS:
        source = sources.get(field, raw)
        summary[field] = source.get(aliases.get(field, field))
    return summary


def run_outbound_distance_experiment(
    model: dict[str, Any],
    day_receipt_state: dict[str, Any],
    start_placement_state: dict[str, Any],
    end_placement_state: dict[str, Any],
    opening_stock_state: dict[str, Any],
    slotting_rule_state: dict[str, Any],
    outbound_demand_state: dict[str, Any],
    gate_state: dict[str, Any],
    replay_rule_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run all existing experiment stages, stopping only on configuration failures."""
    inputs = (model, day_receipt_state, start_placement_state, end_placement_state,
              opening_stock_state, slotting_rule_state, outbound_demand_state, gate_state,
              replay_rule_state)
    states: dict[str, Any] = dict.fromkeys(STAGES)
    stage_diagnostics: dict[str, Any] = dict.fromkeys(STAGES)
    completed: list[str] = []
    blocked_stage: str | None = None
    blocked_reasons: list[str] = []

    top_errors: list[str] = []
    if not all(isinstance(value, Mapping) for value in inputs):
        top_errors.append("invalid_top_level_input_contracts")
    day = day_receipt_state if isinstance(day_receipt_state, Mapping) else {}
    slotting = slotting_rule_state if isinstance(slotting_rule_state, Mapping) else {}
    replay_rules = replay_rule_state if isinstance(replay_rule_state, Mapping) else {}
    dataset_id = day.get("dataset_id")
    operational_date = day.get("operational_date")
    if not dataset_id:
        top_errors.append("receipt_dataset_id_missing")
    warehouses = {_normalized_warehouse(value) for value in day.get("selected_normalized_warehouses", [])
                  if _normalized_warehouse(value)} if isinstance(day.get("selected_normalized_warehouses"), list) else set()
    batches = day.get("receipt_sku_batches", [])
    if isinstance(batches, list):
        warehouses.update(_normalized_warehouse(row.get("normalized_warehouse"))
                          for row in batches if isinstance(row, Mapping) and _normalized_warehouse(row.get("normalized_warehouse")))
    if len(warehouses) != 1:
        top_errors.append("one_normalized_warehouse_required" if not warehouses else "multiple_normalized_warehouses")
    target = next(iter(warehouses), "")
    slotting_target = _normalized_warehouse(slotting.get("target_normalized_warehouse"))
    replay_target = _normalized_warehouse(replay_rules.get("target_normalized_warehouse"))
    if target and slotting_target != target:
        top_errors.append("slotting_target_normalized_warehouse_mismatch")
    if target and replay_target != target:
        top_errors.append("replay_target_normalized_warehouse_mismatch")
    if top_errors:
        blocked_stage = "transition_analysis"
        blocked_reasons = sorted(set(top_errors))

    def record(stage: str, result: tuple[dict[str, Any], dict[str, Any]]) -> bool:
        nonlocal blocked_stage, blocked_reasons
        state, diagnostics = result
        states[stage], stage_diagnostics[stage] = state, diagnostics
        errors = _configuration_errors(diagnostics)
        if errors:
            blocked_stage, blocked_reasons = stage, errors
            return False
        completed.append(stage)
        return True

    if blocked_stage is None and record("transition_analysis", analyze_receipt_snapshot_transitions(
            model, day_receipt_state, start_placement_state, end_placement_state)):
        if record("virtual_slots", build_receipt_virtual_slots(model, states["transition_analysis"])):
            if record("current_receipt_placements", build_current_receipt_placements(
                    model, day_receipt_state, states["transition_analysis"], states["virtual_slots"])):
                if record("proposed_receipt_placements", build_proposed_receipt_placements(
                        model, day_receipt_state, opening_stock_state, slotting_rule_state)):
                    if record("physical_graph", build_physical_warehouse_graph(model, gate_state)):
                        graph_ready = states["physical_graph"].get("summary", {}).get("graph_ready_for_replay")
                        if graph_ready is False:
                            completed.pop()
                            blocked_stage = "physical_graph"
                            blocked_reasons = ["physical_graph_not_ready_for_replay"]
                        elif record("outbound_replay", replay_outbound_scenarios(
                                model, states["physical_graph"], outbound_demand_state, opening_stock_state,
                                states["current_receipt_placements"], states["proposed_receipt_placements"],
                                replay_rule_state)):
                            record("comparison", compare_outbound_replay_scenarios(states["outbound_replay"]))

    execution_status = "complete" if completed == list(STAGES) else "blocked"
    stage_ids = {stage: (states[stage].get(field) if isinstance(states[stage], Mapping) else None)
                 for stage, field in STAGE_ID_FIELDS.items()}
    comparison = states["comparison"] if isinstance(states["comparison"], Mapping) else None
    summary = _comparison_summary(comparison, execution_status)
    graph = states["physical_graph"] if isinstance(states["physical_graph"], Mapping) else {}
    graph_ready = graph.get("summary", {}).get("graph_ready_for_replay")
    identity = {
        "model_id": model.get("model_id") if isinstance(model, Mapping) else None,
        "source_file_hash": model.get("source_file_hash") if isinstance(model, Mapping) else None,
        "receipt_dataset_id": dataset_id, "operational_date": operational_date,
        "target_normalized_warehouse": target, "completed_stage_ids": stage_ids,
        "execution_status": execution_status, "blocked_stage": blocked_stage,
        "blocked_reasons": blocked_reasons,
    }
    experiment = {
        "experiment_state_id": _stable_hash(identity), "execution_status": execution_status,
        "blocked_stage": blocked_stage, "blocked_reasons": blocked_reasons,
        "model_id": identity["model_id"], "source_file_hash": identity["source_file_hash"],
        "receipt_dataset_id": dataset_id, "operational_date": operational_date,
        "target_normalized_warehouse": target, "completed_stages": completed,
        "stage_ids": stage_ids, "states": states, "stage_diagnostics": stage_diagnostics,
        "summary": summary, "limitations": list(LIMITATIONS),
    }
    diagnostics = {
        "configuration_errors": blocked_reasons, "execution_status": execution_status,
        "blocked_stage": blocked_stage, "blocked_reasons": blocked_reasons,
        "completed_stages": completed,
        "stage_configuration_error_counts": {
            stage: len(_configuration_errors(value)) if value is not None else 0
            for stage, value in stage_diagnostics.items()
        },
        "receipt_dataset_id": dataset_id, "operational_date": operational_date,
        "target_normalized_warehouse": target, "graph_ready_for_replay": graph_ready,
        "comparison_status": summary["comparison_status"],
        "full_day_effect_valid": summary["full_day_effect_valid"],
    }
    return experiment, diagnostics
