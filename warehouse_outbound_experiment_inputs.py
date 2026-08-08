"""Prepare deterministic, warehouse-scoped inputs for the outbound experiment."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from warehouse_opening_stock_reconciliation import reconcile_opening_stock
from warehouse_pick_demands import build_outbound_pick_demands
from warehouse_business_identity import normalize_warehouse


DEFAULT_ZONE_ORDER = ["heavy", "medium", "light", "fragile"]
PIPELINE_INPUT_KEYS = (
    "model", "day_receipt_state", "start_placement_state", "end_placement_state",
    "opening_stock_state", "slotting_rule_state", "outbound_demand_state",
    "gate_state", "replay_rule_state",
)
LIMITATIONS = [
    "input_builder_consumes_preparsed_states_and_rows",
    "excel_ingestion_is_outside_input_builder",
    "one_normalized_warehouse_per_experiment",
    "snapshots_are_scoped_by_exact_normalized_warehouse",
    "opening_inventory_quantity_is_authoritative",
    "start_snapshot_is_location_evidence_for_opening_stock",
    "end_snapshot_is_not_used_for_opening_stock",
    "missing_slotting_rules_are_allowed_and_may_produce_unresolved_proposed_receipts",
    "slotting_zone_is_never_inferred_from_current_physical_location",
    "priority_rank_is_not_inferred",
    "one_explicit_gate_per_experiment",
    "physical_gate_validity_is_checked_downstream_by_physical_graph",
    "outbound_orders_are_not_executed_or_mutated",
    "builder_does_not_run_experiment_pipeline",
    "builder_does_not_persist_state",
    "builder_does_not_modify_inputs",
]


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _warehouse(row: Mapping[str, Any]) -> str:
    return normalize_warehouse(row.get("normalized_warehouse") or row.get("warehouse"))


def filter_actual_placement_state_by_warehouse(
    state: dict[str, Any], target_normalized_warehouse: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Deep-copy an actual-placement state and exactly scope all record lists."""
    scoped = copy.deepcopy(state)
    diagnostics = {"accepted": 0, "excluded_other_warehouse": 0, "excluded_missing_warehouse": 0}
    for field in ("placements", "excluded_inventory", "unmatched_inventory", "unplaced_inventory"):
        source = state.get(field, [])
        if not isinstance(source, list):
            continue
        kept = []
        for row in source:
            if not isinstance(row, Mapping):
                diagnostics["excluded_missing_warehouse"] += 1
                continue
            warehouse = _warehouse(row)
            if not warehouse:
                diagnostics["excluded_missing_warehouse"] += 1
            elif warehouse != target_normalized_warehouse:
                diagnostics["excluded_other_warehouse"] += 1
            else:
                kept.append(copy.deepcopy(dict(row)))
                if field == "placements":
                    diagnostics["accepted"] += 1
        scoped[field] = kept
    return scoped, diagnostics


def _identity(state: Mapping[str, Any], *fields: str) -> str:
    for field in fields:
        if state.get(field):
            return str(state[field])
    return _hash(state)


def _empty_result(model: Any, day: Any, errors: list[str], diagnostics: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    model_map = model if isinstance(model, Mapping) else {}
    day_map = day if isinstance(day, Mapping) else {}
    state = {
        "experiment_input_state_id": "", "model_id": model_map.get("model_id"),
        "source_file_hash": model_map.get("source_file_hash"),
        "receipt_dataset_id": day_map.get("dataset_id"), "operational_date": day_map.get("operational_date"),
        "target_normalized_warehouse": diagnostics.get("target_normalized_warehouse", ""),
        "pipeline_inputs_ready": False, "pipeline_inputs": {}, "identities": {}, "summary": {},
        "limitations": list(LIMITATIONS),
    }
    diagnostics["configuration_errors"] = sorted(set(errors))
    return state, diagnostics


def build_outbound_experiment_inputs(
    model: dict[str, Any],
    day_receipt_state: dict[str, Any],
    start_actual_placement_state: dict[str, Any],
    end_actual_placement_state: dict[str, Any],
    opening_inventory_rows: list[dict[str, Any]],
    outbound_order_rows: list[dict[str, Any]],
    slotting_rows: list[dict[str, Any]],
    gate_config: dict[str, Any],
    *,
    zone_order: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build all nine arguments accepted by the outbound experiment pipeline."""
    errors: list[str] = []
    diagnostics: dict[str, Any] = {
        "configuration_errors": [], "target_normalized_warehouse": "",
        "start_snapshot": {}, "end_snapshot": {}, "opening_inventory": {},
        "opening_stock": {}, "outbound_demands": {}, "slotting_rules": {},
        "gate": {}, "replay_rules": {},
    }
    if not isinstance(model, Mapping): errors.append("invalid_model")
    if not isinstance(day_receipt_state, Mapping):
        errors.extend(["receipt_dataset_id_missing", "operational_date_missing", "one_normalized_warehouse_required"])
        day = {}
    else:
        day = day_receipt_state
    if not day.get("dataset_id"): errors.append("receipt_dataset_id_missing")
    if not day.get("operational_date"): errors.append("operational_date_missing")
    batches = day.get("receipt_sku_batches")
    scenarios = day.get("scenario_inputs")
    if not isinstance(batches, list) or not isinstance(scenarios, Mapping) or not all(
            isinstance(scenarios.get(name), Mapping) for name in ("current", "proposed")):
        errors.append("one_normalized_warehouse_required")
        batches = batches if isinstance(batches, list) else []
    warehouses = set()
    selected = day.get("selected_normalized_warehouses", [])
    if isinstance(selected, list): warehouses.update(normalize_warehouse(x) for x in selected if normalize_warehouse(x))
    warehouses.update(_warehouse(x) for x in batches if isinstance(x, Mapping) and _warehouse(x))
    if not warehouses: errors.append("one_normalized_warehouse_required")
    elif len(warehouses) > 1: errors.append("multiple_normalized_warehouses")
    target = next(iter(warehouses), "") if len(warehouses) == 1 else ""
    diagnostics["target_normalized_warehouse"] = target

    for value, code in ((start_actual_placement_state, "invalid_start_placement_state"),
                        (end_actual_placement_state, "invalid_end_placement_state")):
        if not isinstance(value, Mapping) or not isinstance(value.get("placements", []), list): errors.append(code)
    for value, code in ((opening_inventory_rows, "invalid_opening_inventory_rows"),
                        (outbound_order_rows, "invalid_outbound_order_rows"),
                        (slotting_rows, "invalid_slotting_rows")):
        if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value): errors.append(code)

    zones = list(DEFAULT_ZONE_ORDER) if zone_order is None else copy.deepcopy(zone_order)
    if not isinstance(zones, list) or len(zones) != 4 or set(zones) != set(DEFAULT_ZONE_ORDER):
        errors.append("invalid_zone_order")
    if not isinstance(gate_config, Mapping):
        errors.append("invalid_gate_config"); gate = {}
    else: gate = copy.deepcopy(dict(gate_config))
    gate_key = str(gate.get("gate_key") or "").strip()
    if not gate_key: errors.append("gate_key_missing")
    if not str(gate.get("road_type") or "").strip(): errors.append("invalid_gate_config")
    coordinates = (gate.get("x"), gate.get("y"))
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in coordinates):
        errors.append("gate_coordinates_invalid")
    if errors:
        return _empty_result(model, day, errors, diagnostics)

    start, start_diag = filter_actual_placement_state_by_warehouse(dict(start_actual_placement_state), target)
    end, end_diag = filter_actual_placement_state_by_warehouse(dict(end_actual_placement_state), target)
    diagnostics["start_snapshot"], diagnostics["end_snapshot"] = start_diag, end_diag

    scoped_inventory = []
    inventory_diag = {"input_rows": len(opening_inventory_rows), "accepted": 0,
                      "excluded_other_warehouse": 0, "excluded_missing_warehouse": 0}
    for row in opening_inventory_rows:
        warehouse = _warehouse(row)
        if not warehouse: inventory_diag["excluded_missing_warehouse"] += 1
        elif warehouse != target: inventory_diag["excluded_other_warehouse"] += 1
        else: scoped_inventory.append(copy.deepcopy(dict(row))); inventory_diag["accepted"] += 1
    diagnostics["opening_inventory"] = inventory_diag
    opening_stock, opening_diag = reconcile_opening_stock(dict(model), scoped_inventory, start)
    diagnostics["opening_stock"] = opening_diag
    outbound = build_outbound_pick_demands(copy.deepcopy(outbound_order_rows))
    diagnostics["outbound_demands"] = copy.deepcopy(outbound.get("diagnostics", {}))

    receipt_skus = {str(row.get("sku_key") or "").strip() for row in batches if isinstance(row, Mapping) and str(row.get("sku_key") or "").strip()}
    candidate_rules: dict[str, list[dict[str, Any]]] = {}
    slot_diag = {"rows_input": len(slotting_rows), "valid_rules": 0, "invalid_rows": 0,
                 "duplicate_rows": 0, "conflicting_sku_rows": 0,
                 "slotting_rows_outside_receipt_scope": 0,
                 "receipt_skus_without_slotting_rule": 0, "receipt_sku_keys_without_slotting_rule": []}
    for raw in slotting_rows:
        sku = str(raw.get("sku_key") or "").strip(); zone = str(raw.get("weight_zone") or "").strip().casefold()
        rank = raw.get("priority_rank")
        if sku not in receipt_skus:
            slot_diag["slotting_rows_outside_receipt_scope"] += 1; continue
        if zone not in DEFAULT_ZONE_ORDER or (rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 0)):
            slot_diag["invalid_rows"] += 1; continue
        source = str(raw.get("source") or "experiment_input").strip() or "experiment_input"
        rule_identity = {"receipt_dataset_id": day["dataset_id"], "normalized_warehouse": target,
                         "sku_key": sku, "weight_zone": zone, "priority_rank": rank}
        rule = {"rule_key": _hash(rule_identity), "normalized_warehouse": target, "sku_key": sku,
                "weight_zone": zone, "priority_rank": rank, "source": source}
        candidate_rules.setdefault(sku, []).append(rule)

    rules = []
    for sku in sorted(candidate_rules):
        candidates = candidate_rules[sku]
        semantic_keys = {(rule["weight_zone"], rule["priority_rank"]) for rule in candidates}
        if len(semantic_keys) != 1:
            # A SKU must have one unambiguous rule.  Reject every conflicting
            # candidate rather than using input order as an implicit priority.
            slot_diag["conflicting_sku_rows"] += len(candidates)
            continue
        slot_diag["duplicate_rows"] += len(candidates) - 1
        # Source is audit metadata, not rule priority.  A stable choice keeps
        # the state invariant when otherwise-identical input rows are permuted.
        rules.append(min(candidates, key=lambda rule: (rule["source"], rule["rule_key"])))
    rules.sort(key=lambda x: (x["sku_key"], x["weight_zone"],
                              -1 if x["priority_rank"] is None else x["priority_rank"], x["rule_key"]))
    covered = {x["sku_key"] for x in rules}
    missing = sorted(receipt_skus - covered)
    slot_diag.update(valid_rules=len(rules), receipt_skus_without_slotting_rule=len(missing),
                     receipt_sku_keys_without_slotting_rule=missing)
    diagnostics["slotting_rules"] = slot_diag
    slotting = {"dataset_id": day["dataset_id"], "target_normalized_warehouse": target,
                "zone_order": copy.deepcopy(zones), "sku_rules": rules}
    slotting["slotting_rule_state_id"] = _hash(slotting)

    gate_state = {"model_id": model.get("model_id"), "gates": [gate]}
    gate_identity = _hash(gate_state)
    diagnostics["gate"] = {"gate_key": gate_key, "gates": 1, "gate_identity": gate_identity}
    replay = {"target_normalized_warehouse": target, "gate_key": gate_key, "zone_order": copy.deepcopy(zones)}
    replay["replay_rule_state_id"] = _hash(replay)
    diagnostics["replay_rules"] = {"gate_key": gate_key, "zone_order": copy.deepcopy(zones)}

    identities = {
        "receipt_dataset_id": day["dataset_id"],
        "slotting_rule_state_id": slotting["slotting_rule_state_id"],
        "replay_rule_state_id": replay["replay_rule_state_id"],
        "outbound_demand_identity": _identity(outbound, "outbound_demand_state_id", "demand_state_id"),
        "opening_stock_identity": _identity(opening_stock, "opening_stock_state_id"),
        "start_snapshot_identity": _identity(start, "actual_placement_state_id", "placement_state_id"),
        "end_snapshot_identity": _identity(end, "actual_placement_state_id", "placement_state_id"),
        "gate_identity": gate_identity,
    }
    pipeline = dict(zip(PIPELINE_INPUT_KEYS, (model, day_receipt_state, start, end, opening_stock,
                                              slotting, outbound, gate_state, replay)))
    orders = outbound.get("orders", [])
    summary = {
        "target_normalized_warehouse": target, "operational_date": day["operational_date"],
        "receipt_batches": len(batches), "receipt_qty_units": sum(x.get("qty_units", 0) for x in batches if isinstance(x, Mapping)),
        "start_snapshot_placements": len(start.get("placements", [])), "end_snapshot_placements": len(end.get("placements", [])),
        "opening_inventory_rows_input": len(opening_inventory_rows), "opening_stock_placements": len(opening_stock.get("placements", [])),
        "opening_stock_qty_units": sum(x.get("qty_units", 0) for x in opening_stock.get("placements", [])),
        "outbound_orders": len(orders), "outbound_demands": sum(len(x.get("demands", [])) for x in orders),
        "slotting_rules": len(rules), "receipt_skus_without_slotting_rule": len(missing),
        "excluded_start_other_warehouse": start_diag["excluded_other_warehouse"],
        "excluded_end_other_warehouse": end_diag["excluded_other_warehouse"],
        "excluded_opening_inventory_other_warehouse": inventory_diag["excluded_other_warehouse"],
        "gate_key": gate_key, "zone_order": copy.deepcopy(zones),
    }
    top_identity = {"model_id": model.get("model_id"), "source_file_hash": model.get("source_file_hash"),
                    "receipt_dataset_id": day["dataset_id"], "operational_date": day["operational_date"],
                    "target_normalized_warehouse": target, **identities}
    state = {"experiment_input_state_id": _hash(top_identity), "model_id": model.get("model_id"),
             "source_file_hash": model.get("source_file_hash"), "receipt_dataset_id": day["dataset_id"],
             "operational_date": day["operational_date"], "target_normalized_warehouse": target,
             "pipeline_inputs_ready": True, "pipeline_inputs": pipeline, "identities": identities,
             "summary": summary, "limitations": list(LIMITATIONS)}
    return state, diagnostics
