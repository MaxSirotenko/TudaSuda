"""Single deterministic orchestration seam for rebuilding a PROPOSED scenario."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from warehouse_placement_rules import build_placement_rule_set, get_enabled_rule_ids
from warehouse_proposed_placement_optimizer import build_proposed_placement_plan
from warehouse_proposed_state import apply_proposed_placement_plan
from warehouse_sku_adjacency import build_sku_adjacency_profile


def _scenario_id(result: Mapping[str, Any]) -> str:
    identity = {key: result.get(key) for key in (
        "baseline_state_id", "placement_rule_set_id", "placement_plan_id", "proposed_state_id",
    )}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _boxes(state: Mapping[str, Any] | None) -> int | float | None:
    if state is None:
        return None
    return sum(lot.get("qty_boxes", 0) for lot in state.get("stock_lots", []) or [])


def build_proposed_scenario(
    model: dict[str, Any],
    baseline_state: dict[str, Any],
    rule_config: dict[str, Any],
    *,
    sku_zone_rows: list[dict[str, Any]] | None = None,
    sku_velocity_rows: list[dict[str, Any]] | None = None,
    gate_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run RuleSet -> Plan -> ProposedState, always from ``baseline_state``."""
    rule_set, rule_validation = build_placement_rule_set(rule_config)
    adjacency_profile, adjacency_validation = build_sku_adjacency_profile(baseline_state)
    plan, plan_validation = build_proposed_placement_plan(
        model, baseline_state, rule_set, sku_zone_rows or [],
        sku_velocity_rows=sku_velocity_rows, adjacency_profile=adjacency_profile, gate_state=gate_state,
    )
    proposed = None
    apply_report: dict[str, Any] | None = None
    if rule_validation["valid"] and plan.get("status") in {"ready", "partial"}:
        proposed, apply_report = apply_proposed_placement_plan(model, baseline_state, plan)

    if not rule_validation["valid"] or plan.get("status") == "blocked" or proposed is None:
        status = "blocked"
    else:
        status = "partial" if plan.get("status") == "partial" else "ready"

    baseline_id = baseline_state.get("simulation_state_id")
    proposed_id = proposed.get("simulation_state_id") if proposed else None
    plan_summary = plan.get("summary", {})
    baseline_boxes, proposed_boxes = _boxes(baseline_state), _boxes(proposed)
    summary = {
        "enabled_rule_ids": get_enabled_rule_ids(rule_set) if rule_validation["valid"] else [],
        **{key: plan_summary.get(key, 0) for key in (
            "placement_units_total", "units_kept", "units_moved", "fixed_units", "unresolved_units",
            "velocity_profile_skus", "velocity_ranked_units", "velocity_unranked_units", "velocity_units_moved",
            "adjacency_profile_skus", "conflicting_characteristic_groups", "adjacency_skus_involved",
            "adjacency_conflicts_before", "adjacency_conflicts_after", "adjacency_unresolved_fixed_conflicts", "multi_unit_skus",
            "same_sku_fragments_before", "same_sku_fragments_after",
            "same_sku_fragment_reduction", "adjacency_units_moved",
            "capacity_skus_total", "capacity_skus_satisfied", "capacity_skus_short",
            "capacity_positions_required", "capacity_positions_occupied", "capacity_positions_reserved",
            "capacity_shortage_positions",
            "picking_storage_participating_skus", "skus_with_picking_position", "picking_positions",
            "storage_positions", "skus_without_supported_picking_position",
        )},
        "weight_zone_compliance_before_percent": plan_summary.get("weight_zone_compliance_before_percent", 100.0),
        "weight_zone_compliance_after_percent": plan_summary.get("weight_zone_compliance_after_percent", 100.0),
        "baseline_boxes": baseline_boxes,
        "proposed_boxes": proposed_boxes,
        "box_conservation_ok": proposed_boxes is not None and baseline_boxes == proposed_boxes,
        "baseline_state_id": baseline_id,
        "proposed_state_id": proposed_id,
        "state_changed": proposed_id is not None and proposed_id != baseline_id,
    }
    limitations = list(dict.fromkeys(
        list(plan.get("limitations", [])) + list((apply_report or {}).get("limitations", []))
    ))
    result = {
        "status": status,
        "baseline_state_id": baseline_id,
        "placement_rule_set": rule_set,
        "placement_rule_set_id": rule_set.get("placement_rule_set_id"),
        "placement_plan": plan,
        "placement_plan_id": plan.get("proposed_placement_plan_id"),
        "proposed_state": proposed,
        "proposed_state_id": proposed_id,
        "summary": summary,
        "limitations": limitations,
    }
    result["proposed_scenario_id"] = _scenario_id(result)
    diagnostics = {
        "valid": status != "blocked",
        "status": status,
        "rule_set_validation": rule_validation,
        "placement_plan_validation": plan_validation,
        "adjacency_profile_validation": adjacency_validation,
        "apply_report": apply_report,
        "errors": (rule_validation.get("errors", []) + plan.get("blocked_reasons", [])
                   + ([] if apply_report is None else apply_report.get("blocked_reasons", []))),
    }
    return result, diagnostics
