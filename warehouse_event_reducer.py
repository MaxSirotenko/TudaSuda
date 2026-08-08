"""Pure deterministic execution of chronological warehouse events."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from warehouse_business_identity import (
    CANONICAL_BOX_UNIT, canonical_sku_key, normalize_unit_name, normalize_warehouse,
    validate_box_quantity,
)
from warehouse_simulation_state import (
    compute_simulation_state_id, refresh_simulation_state, validate_simulation_state,
)

SAME_TIMESTAMP_POLICIES = frozenset({"event_id_order", "receipts_first", "outbound_first"})
LIMITATIONS = [
    "same_timestamp_order_is_explicit_model_policy_not_observed_business_sequence",
    "receipt_cell_is_never_selected_by_reducer",
    "multiple_outbound_locations_require_explicit_pick_plan",
    "unknown_location_stock_is_not_automatically_pickable",
    "boxes_are_not_converted_to_pallets",
    "deep_lane_depth_and_pallet_release_are_not_inferred",
    "routing_and_replenishment_are_not_modeled",
]


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _hash(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _boxes(state: Mapping[str, Any]) -> int:
    return sum(lot.get("qty_boxes", 0) for lot in state.get("stock_lots", []) or [])


def _result(event: Mapping[str, Any], state: Mapping[str, Any], *, status: str,
            reasons: list[str], receipt: int = 0, requested: int = 0,
            picked: int = 0, shortage: int = 0, after: Mapping[str, Any] | None = None,
            demand_results: list[dict[str, Any]] | None = None,
            depleted_lot_ids: list[str] | None = None) -> dict[str, Any]:
    final = after or state
    result = {"event_id": event.get("event_id"), "event_type": event.get("event_type"),
              "occurred_at": event.get("occurred_at"), "status": status,
              "before_state_id": state.get("simulation_state_id"),
              "after_state_id": final.get("simulation_state_id"), "boxes_before": _boxes(state),
              "receipt_boxes": receipt, "requested_boxes": requested, "picked_boxes": picked,
              "shortage_boxes": shortage, "boxes_after": _boxes(final), "reasons": sorted(set(reasons))}
    if demand_results is not None:
        result["demand_results"] = demand_results
    if depleted_lot_ids:
        result["depleted_stock_lot_ids"] = sorted(depleted_lot_ids)
    return result


def _parse_time(value: Any) -> dt.datetime | None:
    raw = _text(value)
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.lower().endswith("z") else raw
    try:
        return dt.datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                return dt.datetime.strptime(raw, fmt)
            except ValueError:
                pass
    return None


def _blocked(event: Mapping[str, Any], state: Mapping[str, Any], reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return copy.deepcopy(dict(state)), _result(event, state, status="blocked", reasons=[reason])


def _valid_sku(record: Mapping[str, Any]) -> str | None:
    key = _text(record.get("sku_key"))
    derived = canonical_sku_key(record)
    return key if key.startswith("sku:v2:") and (not derived or derived == key) else None


def _receipt_lot(event: Mapping[str, Any], batch: Mapping[str, Any], allocation: Mapping[str, Any] | None,
                 allocation_index: int) -> dict[str, Any]:
    cell = _text(allocation.get("cell_key")) if allocation else None
    quantity = allocation["qty_boxes"] if allocation else batch["qty_units"]
    identity = {"event_id": event["event_id"], "sku_key": batch["sku_key"],
                "allocation_index": allocation_index, "cell_key": cell,
                "receipt_line_keys": sorted(_text(x) for x in
                    (batch.get("source_receipt_line_keys") or batch.get("receipt_line_keys") or []))}
    return {"stock_lot_id": _hash(identity), "sku_key": batch["sku_key"],
            "nomenclature": _text(batch.get("nomenclature") or batch.get("sku_name")),
            "characteristic": _text(batch.get("characteristic") or batch.get("characteristic_name")),
            "qty_boxes": quantity, "unit_name": CANONICAL_BOX_UNIT,
            "location_status": "located" if allocation else "unknown", "cell_key": cell,
            "location_role": "unassigned", "production_dates": sorted({_text(x) for x in batch.get("production_dates", []) or [] if _text(x)}),
            "source": "receipt_event", "source_event_id": event["event_id"],
            "allocation_method": "explicit_receipt_allocation" if allocation else "",
            "source_placement_id": None, "location_confidence": "explicit" if allocation else "unknown",
            "pallet_count": None, "pallet_count_status": "unknown"}


def _apply_receipt(model: Mapping[str, Any], state: Mapping[str, Any], event: Mapping[str, Any],
                   allocations: list[dict[str, Any]] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    batches = event.get("receipt_batches")
    if not isinstance(batches, list) or not batches:
        return _blocked(event, state, "invalid_receipt_batches")
    quantities: Counter[str] = Counter()
    normalized_batches = []
    for batch in batches:
        if not isinstance(batch, Mapping) or not _valid_sku(batch) or normalize_unit_name(batch.get("unit_name")) != CANONICAL_BOX_UNIT:
            return _blocked(event, state, "invalid_receipt_batch")
        quantity, error = validate_box_quantity(batch.get("qty_units"), positive=True)
        if error:
            return _blocked(event, state, "invalid_receipt_batch")
        item = copy.deepcopy(dict(batch)); item["qty_units"] = quantity
        normalized_batches.append(item); quantities[item["sku_key"]] += quantity
    allocation_rows: list[dict[str, Any]] = []
    lines_by_sku = {line.get("sku_key"): line for line in event.get("receipt_lines", []) or []
                    if isinstance(line, Mapping)}
    for batch in normalized_batches:
        evidence = lines_by_sku.get(batch["sku_key"], {})
        for key in ("nomenclature", "sku_name", "item_name", "characteristic", "characteristic_name"):
            if not batch.get(key) and evidence.get(key):
                batch[key] = copy.deepcopy(evidence[key])
        if canonical_sku_key(batch) != batch["sku_key"]:
            return _blocked(event, state, "invalid_receipt_batch")
    if allocations is not None:
        if not isinstance(allocations, list):
            return _blocked(event, state, "invalid_receipt_allocation_plan")
        cells = {_text(cell.get("cell_key")) or "|".join((_text(cell.get("row_number")), _text(cell.get("cell_number")), _text(cell.get("tier") or "1")))
                 for cell in model.get("cells", []) or []}
        allocated: Counter[str] = Counter()
        for row in allocations:
            if not isinstance(row, Mapping) or _text(row.get("sku_key")) not in quantities or _text(row.get("cell_key")) not in cells:
                return _blocked(event, state, "invalid_receipt_allocation_plan")
            quantity, error = validate_box_quantity(row.get("qty_boxes"), positive=True)
            if error or (row.get("normalized_warehouse") is not None and normalize_warehouse(row.get("normalized_warehouse")) != state["target_normalized_warehouse"]):
                return _blocked(event, state, "invalid_receipt_allocation_plan")
            copied = copy.deepcopy(dict(row)); copied["qty_boxes"] = quantity
            allocation_rows.append(copied); allocated[copied["sku_key"]] += quantity
        if allocated != quantities:
            return _blocked(event, state, "invalid_receipt_allocation_plan")
    new = copy.deepcopy(dict(state)); lots = list(new.get("stock_lots", []))
    if allocations is None:
        for index, batch in enumerate(sorted(normalized_batches, key=lambda x: (x["sku_key"], json.dumps(x, sort_keys=True, ensure_ascii=False)))):
            lots.append(_receipt_lot(event, batch, None, index))
    else:
        by_sku = {batch["sku_key"]: batch for batch in normalized_batches}
        # Multiple source batches for one canonical SKU are one receipt-event provenance lot family.
        for index, allocation in enumerate(sorted(allocation_rows, key=lambda x: (x["sku_key"], x["cell_key"], json.dumps(x, sort_keys=True, ensure_ascii=False)))):
            lots.append(_receipt_lot(event, by_sku[allocation["sku_key"]], allocation, index))
    total = sum(quantities.values())
    new["stock_lots"] = lots
    new["stock_conservation"]["cumulative_receipt_boxes"] += total
    new["simulation_time"] = event["occurred_at"]
    new["applied_event_ids"] = sorted([*new.get("applied_event_ids", []), event["event_id"]])
    new = refresh_simulation_state(model, new)
    reasons = ["receipt_allocation_missing"] if allocations is None else []
    status = "partial" if reasons else "applied"
    return new, _result(event, state, status=status, reasons=reasons, receipt=total, after=new)


def _apply_outbound(model: Mapping[str, Any], state: Mapping[str, Any], event: Mapping[str, Any],
                    plan: list[dict[str, Any]] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    demands = event.get("demands")
    if not isinstance(demands, list) or not demands:
        return _blocked(event, state, "invalid_outbound_demands")
    normalized: list[dict[str, Any]] = []
    demand_by_key = {}
    for index, demand in enumerate(demands):
        if not isinstance(demand, Mapping) or not _valid_sku(demand) or normalize_unit_name(demand.get("unit_name")) != CANONICAL_BOX_UNIT:
            return _blocked(event, state, "invalid_outbound_demand")
        quantity, error = validate_box_quantity(demand.get("requested_units"), positive=True)
        key = _text(demand.get("demand_key")) or f"demand:{index}"
        if error or key in demand_by_key:
            return _blocked(event, state, "invalid_outbound_demand")
        item = copy.deepcopy(dict(demand)); item.update({"demand_key": key, "requested_units": quantity})
        normalized.append(item); demand_by_key[key] = item
    lots = copy.deepcopy(list(state.get("stock_lots", [])))
    lot_by_id = {lot["stock_lot_id"]: lot for lot in lots}
    picks: defaultdict[str, list[tuple[str, int]]] = defaultdict(list)
    if plan is not None:
        if not isinstance(plan, list):
            return _blocked(event, state, "invalid_outbound_pick_plan")
        lot_totals: Counter[str] = Counter(); demand_totals: Counter[str] = Counter()
        for row in plan:
            demand = demand_by_key.get(_text(row.get("demand_key"))) if isinstance(row, Mapping) else None
            lot = lot_by_id.get(_text(row.get("stock_lot_id"))) if isinstance(row, Mapping) else None
            quantity, error = validate_box_quantity(row.get("qty_boxes"), positive=True) if isinstance(row, Mapping) else (None, "invalid")
            if (not demand or not lot or error or lot.get("sku_key") != demand["sku_key"] or lot.get("location_status") != "located"
                    or (row.get("normalized_warehouse") is not None and normalize_warehouse(row.get("normalized_warehouse")) != state["target_normalized_warehouse"])
                    or (lot.get("normalized_warehouse") is not None and normalize_warehouse(lot.get("normalized_warehouse")) != state["target_normalized_warehouse"])):
                return _blocked(event, state, "invalid_outbound_pick_plan")
            lot_totals[lot["stock_lot_id"]] += quantity; demand_totals[demand["demand_key"]] += quantity
            picks[demand["demand_key"]].append((lot["stock_lot_id"], quantity))
        if any(value > lot_by_id[key]["qty_boxes"] for key, value in lot_totals.items()) or any(
                value > demand_by_key[key]["requested_units"] for key, value in demand_totals.items()):
            return _blocked(event, state, "invalid_outbound_pick_plan")
    reasons: list[str] = []; demand_results = []; picked_total = 0; requested_total = 0
    for demand in normalized:
        requested = demand["requested_units"]; requested_total += requested
        demand_picks = picks[demand["demand_key"]]
        if plan is None:
            eligible = [lot for lot in lots if lot["sku_key"] == demand["sku_key"] and lot["location_status"] == "located"]
            unknown = any(lot["sku_key"] == demand["sku_key"] and lot["location_status"] == "unknown" for lot in lots)
            if len(eligible) == 1:
                demand_picks = [(eligible[0]["stock_lot_id"], min(requested, eligible[0]["qty_boxes"]))]
            elif len(eligible) > 1:
                reasons.append("outbound_pick_plan_required")
            elif unknown:
                reasons.append("stock_location_unknown")
        picked = sum(quantity for _, quantity in demand_picks)
        for lot_id, quantity in demand_picks:
            lot_by_id[lot_id]["qty_boxes"] -= quantity
        shortage = requested - picked; picked_total += picked
        demand_results.append({"demand_key": demand["demand_key"], "sku_key": demand["sku_key"],
                               "requested_boxes": requested, "picked_boxes": picked, "shortage_boxes": shortage,
                               "pick_allocations": [{"stock_lot_id": lot_id, "qty_boxes": quantity} for lot_id, quantity in demand_picks]})
    depleted = [lot["stock_lot_id"] for lot in lots if lot["qty_boxes"] == 0]
    new = copy.deepcopy(dict(state)); new["stock_lots"] = [lot for lot in lots if lot["qty_boxes"] > 0]
    new["stock_conservation"]["cumulative_picked_boxes"] += picked_total
    new["simulation_time"] = event["occurred_at"]
    new["applied_event_ids"] = sorted([*new.get("applied_event_ids", []), event["event_id"]])
    new = refresh_simulation_state(model, new)
    shortage_total = requested_total - picked_total
    status = "partial" if reasons else "applied"
    return new, _result(event, state, status=status, reasons=reasons, requested=requested_total,
                        picked=picked_total, shortage=shortage_total, after=new,
                        demand_results=demand_results, depleted_lot_ids=depleted)


def apply_warehouse_event(model: dict[str, Any], state: dict[str, Any], event: dict[str, Any], *,
                          receipt_allocations: list[dict[str, Any]] | None = None,
                          outbound_pick_plan: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one event to a copied state, returning the new state and audit result."""
    if not isinstance(event, Mapping) or not _text(event.get("event_id")):
        return _blocked(event if isinstance(event, Mapping) else {}, state, "invalid_event")
    if event["event_id"] in (state.get("applied_event_ids", []) or []):
        return _blocked(event, state, "duplicate_event_already_applied")
    warehouse = normalize_warehouse(event.get("normalized_warehouse"))
    if not warehouse or warehouse != state.get("target_normalized_warehouse"):
        return _blocked(event, state, "warehouse_mismatch")
    occurred, current = _parse_time(event.get("occurred_at")), _parse_time(state.get("simulation_time"))
    try:
        before = occurred is not None and current is not None and occurred < current
    except TypeError:
        return _blocked(event, state, "incomparable_simulation_time")
    if occurred is None:
        return _blocked(event, state, "invalid_event_time")
    if before:
        return _blocked(event, state, "event_before_simulation_time")
    if event.get("event_type") == "receipt":
        return _apply_receipt(model, state, event, receipt_allocations)
    if event.get("event_type") == "outbound_order":
        return _apply_outbound(model, state, event, outbound_pick_plan)
    return _blocked(event, state, "unsupported_event_type")


def _base_report(state: Mapping[str, Any], timeline: Mapping[str, Any]) -> dict[str, Any]:
    opening = state.get("stock_conservation", {}).get("opening_boxes_input", 0)
    return {"execution_status": "blocked", "initial_state_id": state.get("simulation_state_id"),
            "final_state_id": state.get("simulation_state_id"),
            "target_normalized_warehouse": state.get("target_normalized_warehouse"),
            "events_total": len(timeline.get("events", []) or []), "events_applied": 0,
            "events_partial": 0, "events_blocked": 0, "receipt_events_applied": 0,
            "outbound_events_applied": 0, "opening_boxes": opening, "receipt_boxes": 0,
            "requested_boxes": 0, "picked_boxes": 0, "shortage_boxes": 0,
            "closing_boxes": _boxes(state), "stock_conservation_ok": False,
            "event_results": [], "limitations": list(LIMITATIONS), "diagnostics": []}


def reduce_warehouse_timeline(model: dict[str, Any], initial_state: dict[str, Any], timeline_state: dict[str, Any], *,
                              receipt_allocations_by_event_id: dict[str, list[dict[str, Any]]] | None = None,
                              outbound_pick_plans_by_event_id: dict[str, list[dict[str, Any]]] | None = None,
                              same_timestamp_policy: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute timeline time groups in their chronology, stopping on fatal findings."""
    state = copy.deepcopy(initial_state); report = _base_report(state, timeline_state)
    initial_validation = validate_simulation_state(initial_state, model)
    if not initial_validation["valid"]:
        report.update({"blocked_reason": "invalid_initial_simulation_state", "diagnostics": initial_validation["errors"]})
        return state, report
    if timeline_state.get("strict_chronology_ready") is not True:
        report.update({"blocked_reason": "invalid_timeline", "diagnostics": copy.deepcopy(timeline_state.get("diagnostics") or timeline_state.get("invalid_events") or [])})
        return state, report
    if normalize_warehouse(timeline_state.get("target_normalized_warehouse")) != state.get("target_normalized_warehouse"):
        report["blocked_reason"] = "warehouse_mismatch"; return state, report
    groups = timeline_state.get("time_groups")
    if not isinstance(groups, list):
        report["blocked_reason"] = "invalid_timeline"; return state, report
    if any((group.get("event_count", len(group.get("events", []) or [])) > 1) for group in groups):
        if same_timestamp_policy is None:
            report["blocked_reason"] = "same_timestamp_policy_required"; return state, report
        if same_timestamp_policy not in SAME_TIMESTAMP_POLICIES:
            report["blocked_reason"] = "invalid_same_timestamp_policy"; return state, report
    elif same_timestamp_policy is not None and same_timestamp_policy not in SAME_TIMESTAMP_POLICIES:
        report["blocked_reason"] = "invalid_same_timestamp_policy"; return state, report
    allocations = receipt_allocations_by_event_id or {}; plans = outbound_pick_plans_by_event_id or {}
    for group in groups:
        events = copy.deepcopy(group.get("events", []) or [])
        if len(events) > 1:
            rank = {"receipt": 0, "outbound_order": 1} if same_timestamp_policy == "receipts_first" else {"outbound_order": 0, "receipt": 1}
            events.sort(key=(lambda event: event["event_id"]) if same_timestamp_policy == "event_id_order"
                        else (lambda event: (rank.get(event.get("event_type"), 2), event["event_id"])))
        for event in events:
            state, result = apply_warehouse_event(model, state, event,
                receipt_allocations=copy.deepcopy(allocations.get(event["event_id"])) if event["event_id"] in allocations else None,
                outbound_pick_plan=copy.deepcopy(plans.get(event["event_id"])) if event["event_id"] in plans else None)
            report["event_results"].append(result)
            if result["status"] == "blocked":
                report["events_blocked"] += 1; report["blocked_event_id"] = event.get("event_id")
                report["blocked_reason"] = result["reasons"][0]; report["final_state_id"] = state.get("simulation_state_id")
                report["closing_boxes"] = _boxes(state); return state, report
            validation = validate_simulation_state(state, model)
            if not validation["valid"]:
                report["events_blocked"] += 1; report["blocked_event_id"] = event.get("event_id")
                report["blocked_reason"] = "invalid_resulting_simulation_state"; report["diagnostics"] = validation["errors"]
                report["final_state_id"] = state.get("simulation_state_id"); report["closing_boxes"] = _boxes(state); return state, report
            report["events_applied"] += 1
            report["events_partial"] += result["status"] == "partial"
            report["receipt_events_applied"] += event.get("event_type") == "receipt"
            report["outbound_events_applied"] += event.get("event_type") == "outbound_order"
            for target, source in (("receipt_boxes", "receipt_boxes"), ("requested_boxes", "requested_boxes"),
                                   ("picked_boxes", "picked_boxes"), ("shortage_boxes", "shortage_boxes")):
                report[target] += result[source]
    report["execution_status"] = "partial" if report["events_partial"] else "complete"
    report["final_state_id"] = state["simulation_state_id"]; report["closing_boxes"] = _boxes(state)
    report["stock_conservation_ok"] = state["stock_conservation"]["stock_conservation_ok"]
    return state, report
