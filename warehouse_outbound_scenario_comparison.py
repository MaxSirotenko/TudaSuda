"""Deterministic audit and service-equivalent comparison of outbound replays."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from typing import Any


DISTANCE_PRECISION = 6
DISTANCE_TOLERANCE_M = 0.000001
_CONTROLS = (
    "same_outbound_demand_set", "same_opening_stock", "same_physical_graph",
    "same_gate", "same_zone_order", "receipt_dataset_ids_match",
    "only_receipt_placement_differs",
)
LIMITATIONS = [
    "raw_total_distance_difference_is_not_a_business_effect_without_service_equivalence",
    "primary_distance_effect_uses_only_strictly_comparable_orders",
    "equal_requested_picked_and_shortage_units_are_required_per_demand",
    "different_pick_counts_and_splits_are_allowed_when_service_is_equivalent",
    "non_comparable_orders_do_not_contribute_to_distance_savings",
    "zero_route_activity_orders_do_not_contribute_to_improved_order_share",
    "unresolved_receipts_reduce_full_day_evidence_quality",
    "shortages_prevent_full_day_effect_claim",
    "invalid_routes_prevent_full_day_effect_claim",
    "greedy_replay_quality_is_inherited_from_source_replay",
    "comparison_does_not_replay_orders", "comparison_does_not_modify_working_stock",
    "comparison_does_not_modify_placements", "comparison_does_not_calculate_financial_effect",
    "comparison_does_not_extrapolate_missing_orders", "partial_comparison_must_report_coverage",
]


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _unit(value: Any) -> str:
    normalized = _text(value).casefold().replace("ё", "е")
    return "короб" if normalized in {"короб", "короба", "коробов"} else normalized


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _distance(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else round(number, DISTANCE_PRECISION)


def _pct(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else round(numerator / denominator * 100, DISTANCE_PRECISION)


def _duplicates(items: list[Any]) -> list[Any]:
    counts = Counter(item for item in items if item is not None)
    return sorted((key for key, count in counts.items() if count > 1), key=str)


def _summary_value(summary: Mapping[str, Any], name: str) -> Any:
    value = summary.get(name, 0)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def compare_outbound_replay_scenarios(
    outbound_replay_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compare prepared CURRENT and PROPOSED replay results without replaying them."""
    source = outbound_replay_state if isinstance(outbound_replay_state, Mapping) else {}
    errors: list[str] = []
    if not isinstance(outbound_replay_state, Mapping):
        errors.append("invalid_replay_state")
    for field in ("outbound_replay_state_id", "outbound_demand_set_id", "physical_graph_state_id",
                  "opening_stock_identity", "receipt_dataset_id"):
        if not source.get(field): errors.append(f"{field}_missing")
    current = source.get("current") if isinstance(source.get("current"), Mapping) else {}
    proposed = source.get("proposed") if isinstance(source.get("proposed"), Mapping) else {}
    if not isinstance(source.get("current"), Mapping): errors.append("invalid_current_scenario")
    if not isinstance(source.get("proposed"), Mapping): errors.append("invalid_proposed_scenario")
    if current.get("scenario") != "current": errors.append("current_scenario_mismatch")
    if proposed.get("scenario") != "proposed": errors.append("proposed_scenario_mismatch")
    accepted = source.get("accepted_order_keys")
    if not isinstance(accepted, list): errors.append("invalid_accepted_order_keys"); accepted = []
    duplicate_accepted = _duplicates(accepted)
    if duplicate_accepted: errors.append("duplicate_accepted_order_keys")
    controls = source.get("shared_controls") if isinstance(source.get("shared_controls"), Mapping) else {}
    for control in _CONTROLS:
        if controls.get(control) is not True: errors.append(f"shared_control_{control}_not_true")
    cs = current.get("summary") if isinstance(current.get("summary"), Mapping) else {}
    ps = proposed.get("summary") if isinstance(proposed.get("summary"), Mapping) else {}
    for label, summary in (("current", cs), ("proposed", ps)):
        for conservation in ("stock_conservation_ok", "demand_conservation_ok"):
            if summary.get(conservation) is not True: errors.append(f"{label}_{conservation}_not_true")
    current_orders = current.get("orders") if isinstance(current.get("orders"), list) else []
    proposed_orders = proposed.get("orders") if isinstance(proposed.get("orders"), list) else []
    ckeys = [o.get("order_key") for o in current_orders if isinstance(o, Mapping)]
    pkeys = [o.get("order_key") for o in proposed_orders if isinstance(o, Mapping)]
    duplicate_current = _duplicates(ckeys); duplicate_proposed = _duplicates(pkeys)
    if duplicate_current: errors.append("duplicate_current_order_keys")
    if duplicate_proposed: errors.append("duplicate_proposed_order_keys")
    unmatched = sorted(set(ckeys) ^ set(pkeys), key=str)
    if unmatched: errors.append("current_proposed_order_keys_mismatch")
    if set(ckeys) != set(accepted): errors.append("accepted_order_keys_mismatch")
    errors = sorted(set(errors))

    comparisons: list[dict[str, Any]] = []
    if not errors:
        cmap = {o["order_key"]: o for o in current_orders}
        pmap = {o["order_key"]: o for o in proposed_orders}
        for key in accepted:
            co, po = cmap[key], pmap[key]
            reasons: set[str] = set()
            cd, pd = _distance(co.get("route_distance_m")), _distance(po.get("route_distance_m"))
            if cd is None: reasons.add("invalid_current_distance")
            if pd is None: reasons.add("invalid_proposed_distance")
            if co.get("status") == "invalid_route": reasons.add("current_invalid_route")
            if po.get("status") == "invalid_route": reasons.add("proposed_invalid_route")
            if (co.get("picks") or []) and co.get("returned_to_gate") is not True: reasons.add("current_not_returned_to_gate")
            if (po.get("picks") or []) and po.get("returned_to_gate") is not True: reasons.add("proposed_not_returned_to_gate")
            if co.get("requested_units") != po.get("requested_units"): reasons.add("requested_units_mismatch")
            cdemands = co.get("demands") if isinstance(co.get("demands"), list) else []
            pdemands = po.get("demands") if isinstance(po.get("demands"), list) else []
            cdkeys = [d.get("demand_key") for d in cdemands if isinstance(d, Mapping)]
            pdkeys = [d.get("demand_key") for d in pdemands if isinstance(d, Mapping)]
            if _duplicates(cdkeys) or _duplicates(pdkeys) or set(cdkeys) != set(pdkeys):
                reasons.add("demand_set_mismatch")
            else:
                dm = {d["demand_key"]: d for d in pdemands}
                for demand in cdemands:
                    other = dm[demand["demand_key"]]
                    if demand.get("sku_key") != other.get("sku_key"): reasons.add("sku_mismatch")
                    if _unit(demand.get("unit_name")) != _unit(other.get("unit_name")): reasons.add("unit_mismatch")
                    for field in ("requested_units", "picked_units", "shortage_units"):
                        if demand.get(field) != other.get(field): reasons.add(f"{field}_mismatch")
            service_reasons = {"demand_set_mismatch", "requested_units_mismatch", "picked_units_mismatch", "shortage_units_mismatch"}
            service_equivalent = not bool(reasons & service_reasons)
            strict = not reasons
            activity = bool(strict and ((cd or 0) > DISTANCE_TOLERANCE_M or (pd or 0) > DISTANCE_TOLERANCE_M))
            saved = change = improvement = None
            if strict:
                saved = round(cd - pd, DISTANCE_PRECISION); change = round(pd - cd, DISTANCE_PRECISION)
                improvement = _pct(saved, cd) if cd > DISTANCE_TOLERANCE_M else None
                if not activity: classification = "no_route_activity"
                elif saved > DISTANCE_TOLERANCE_M: classification = "improved"
                elif saved < -DISTANCE_TOLERANCE_M: classification = "worsened"
                else: classification = "equal"
            else: classification = "not_comparable"
            item = {"order_comparison_id": "", "order_key": key,
                "outbound_order_number": co.get("outbound_order_number"), "created_at": co.get("created_at"),
                "warehouse": co.get("warehouse"), "strict_comparable": strict,
                "service_equivalent": service_equivalent, "has_route_activity": activity,
                "current_status": co.get("status"), "proposed_status": po.get("status"),
                "requested_units": co.get("requested_units"), "current_picked_units": co.get("picked_units"),
                "proposed_picked_units": po.get("picked_units"), "current_shortage_units": co.get("shortage_units"),
                "proposed_shortage_units": po.get("shortage_units"), "current_distance_m": cd,
                "proposed_distance_m": pd, "distance_saved_m": saved, "distance_change_m": change,
                "improvement_percent": improvement, "current_pick_events": len(co.get("picks") or []),
                "proposed_pick_events": len(po.get("picks") or []),
                "current_split_demands": sum(d.get("split") is True for d in cdemands),
                "proposed_split_demands": sum(d.get("split") is True for d in pdemands),
                "classification": classification, "reasons": sorted(reasons)}
            identity_fields = ("order_key", "strict_comparable", "service_equivalent", "current_distance_m",
                "proposed_distance_m", "current_picked_units", "proposed_picked_units", "current_shortage_units",
                "proposed_shortage_units", "classification", "reasons")
            item["order_comparison_id"] = _hash({"outbound_replay_state_id": source.get("outbound_replay_state_id"),
                **{field: item[field] for field in identity_fields}})
            comparisons.append(item)
        comparisons.sort(key=lambda x: (_text(x["created_at"]), _text(x["outbound_order_number"]), _text(x["order_key"])))

    def raw_distance(orders: list[Any]) -> float:
        return round(sum(value for order in orders if isinstance(order, Mapping)
                         for value in [_distance(order.get("route_distance_m"))] if value is not None), DISTANCE_PRECISION)
    cr, pr = raw_distance(current_orders), raw_distance(proposed_orders)
    raw = {"orders_total": len(accepted), "current_route_distance_m": cr, "proposed_route_distance_m": pr,
        "current_requested_units": _summary_value(cs, "requested_units"), "proposed_requested_units": _summary_value(ps, "requested_units"),
        "current_picked_units": _summary_value(cs, "picked_units"), "proposed_picked_units": _summary_value(ps, "picked_units"),
        "current_shortage_units": _summary_value(cs, "shortage_units"), "proposed_shortage_units": _summary_value(ps, "shortage_units"),
        "current_orders_with_shortage": _summary_value(cs, "orders_with_shortage"), "proposed_orders_with_shortage": _summary_value(ps, "orders_with_shortage"),
        "current_receipt_unresolved_batches": _summary_value(cs, "receipt_unresolved_batches"), "proposed_receipt_unresolved_batches": _summary_value(ps, "receipt_unresolved_batches"),
        "current_receipt_unresolved_qty_units": _summary_value(cs, "receipt_unresolved_qty_units"), "proposed_receipt_unresolved_qty_units": _summary_value(ps, "receipt_unresolved_qty_units"),
        "raw_distance_difference_m": round(cr-pr, DISTANCE_PRECISION), "raw_distance_difference_is_business_effect": False}
    strict_items = [x for x in comparisons if x["strict_comparable"]]
    route_items = [x for x in strict_items if x["has_route_activity"]]
    cdist = round(sum(x["current_distance_m"] for x in route_items), DISTANCE_PRECISION)
    pdist = round(sum(x["proposed_distance_m"] for x in route_items), DISTANCE_PRECISION)
    saved = round(cdist-pdist, DISTANCE_PRECISION)
    classes = Counter(x["classification"] for x in comparisons)
    requested = sum(x["requested_units"] or 0 for x in strict_items)
    picked = sum(x["current_picked_units"] or 0 for x in strict_items)
    shortage = sum(x["current_shortage_units"] or 0 for x in strict_items)
    comparable = {"comparable_orders": len(strict_items), "comparable_route_orders": len(route_items),
        "non_comparable_orders": len(comparisons)-len(strict_items), "no_route_activity_orders": classes["no_route_activity"],
        "current_comparable_distance_m": cdist, "proposed_comparable_distance_m": pdist,
        "distance_saved_m": saved, "distance_change_m": round(-saved, DISTANCE_PRECISION),
        "improvement_percent": _pct(saved, cdist) if cdist > DISTANCE_TOLERANCE_M else None,
        "improved_orders": classes["improved"], "worsened_orders": classes["worsened"], "equal_orders": classes["equal"],
        "share_improved_orders_percent": _pct(classes["improved"], len(route_items)),
        "share_worsened_orders_percent": _pct(classes["worsened"], len(route_items)),
        "comparable_requested_units": requested, "comparable_picked_units": picked, "comparable_shortage_units": shortage}
    accepted_requested = _summary_value(cs, "requested_units")
    coverage = {"accepted_orders": len(accepted), "strict_comparable_orders": len(strict_items),
        "comparable_route_orders": len(route_items), "order_comparability_percent": _pct(len(strict_items), len(accepted)),
        "route_order_coverage_percent": _pct(len(route_items), len(accepted)), "accepted_requested_units": accepted_requested,
        "comparable_requested_units": requested, "requested_units_coverage_percent": _pct(requested, accepted_requested)}
    all_returned = all(not (o.get("picks") or []) or o.get("returned_to_gate") is True
                       for o in current_orders + proposed_orders if isinstance(o, Mapping))
    full = (not errors and len(strict_items) == len(accepted) and
            _summary_value(cs, "shortage_units") == _summary_value(ps, "shortage_units") == 0 and
            _summary_value(cs, "receipt_unresolved_qty_units") == _summary_value(ps, "receipt_unresolved_qty_units") == 0 and
            all_returned)
    status = "invalid_configuration" if errors else ("full_day_valid" if full else ("partial" if route_items else "not_comparable"))
    quality = {"comparison_status": status, "scope": "full_day" if full else "comparable_orders_only",
        "full_day_effect_valid": full, **{name: controls.get(name) is True for name in _CONTROLS if name != "only_receipt_placement_differs"},
        "all_orders_service_equivalent": len(comparisons) == len(accepted) and all(x["service_equivalent"] for x in comparisons),
        "all_orders_returned_to_gate": all_returned,
        "current_receipt_complete": _summary_value(cs, "receipt_unresolved_qty_units") == 0,
        "proposed_receipt_complete": _summary_value(ps, "receipt_unresolved_qty_units") == 0,
        "current_no_shortage": _summary_value(cs, "shortage_units") == 0, "proposed_no_shortage": _summary_value(ps, "shortage_units") == 0,
        "current_stock_conservation_ok": cs.get("stock_conservation_ok") is True, "proposed_stock_conservation_ok": ps.get("stock_conservation_ok") is True,
        "current_demand_conservation_ok": cs.get("demand_conservation_ok") is True, "proposed_demand_conservation_ok": ps.get("demand_conservation_ok") is True}
    state = {"outbound_comparison_state_id": "", "source_outbound_replay_state_id": source.get("outbound_replay_state_id", ""),
        "outbound_demand_set_id": source.get("outbound_demand_set_id", ""), "physical_graph_state_id": source.get("physical_graph_state_id", ""),
        "receipt_dataset_id": source.get("receipt_dataset_id", ""), "target_normalized_warehouse": source.get("target_normalized_warehouse", ""),
        "gate_key": source.get("gate_key", ""), "zone_order": list(source.get("zone_order", [])) if isinstance(source.get("zone_order"), list) else [],
        "order_comparisons": comparisons, "raw_summary": raw, "comparable_summary": comparable,
        "coverage": coverage, "quality": quality, "limitations": LIMITATIONS.copy()}
    state["outbound_comparison_state_id"] = _hash({"source_outbound_replay_state_id": state["source_outbound_replay_state_id"],
        "outbound_demand_set_id": state["outbound_demand_set_id"], "physical_graph_state_id": state["physical_graph_state_id"],
        "receipt_dataset_id": state["receipt_dataset_id"], "target_normalized_warehouse": state["target_normalized_warehouse"],
        "gate_key": state["gate_key"], "zone_order": state["zone_order"],
        "order_comparison_ids": sorted(x["order_comparison_id"] for x in comparisons), "raw_summary": raw,
        "comparable_summary": comparable, "coverage": coverage, "quality": quality, "configuration_errors": errors})
    reason_map = {"demand_set_mismatch": "orders_with_demand_set_mismatch", "sku_mismatch": "orders_with_sku_mismatch",
        "unit_mismatch": "orders_with_unit_mismatch", "requested_units_mismatch": "orders_with_requested_units_mismatch",
        "picked_units_mismatch": "orders_with_picked_units_mismatch", "shortage_units_mismatch": "orders_with_shortage_units_mismatch"}
    diagnostics = {"configuration_errors": errors, "accepted_orders": len(accepted), "current_orders": len(current_orders),
        "proposed_orders": len(proposed_orders), "duplicate_current_order_keys": duplicate_current,
        "duplicate_proposed_order_keys": duplicate_proposed, "unmatched_order_keys": unmatched,
        **{output: [x["order_key"] for x in comparisons if reason in x["reasons"]] for reason, output in reason_map.items()},
        "orders_with_invalid_route": [x["order_key"] for x in comparisons if "invalid_route" in " ".join(x["reasons"])],
        "orders_not_returned_to_gate": [x["order_key"] for x in comparisons if "not_returned_to_gate" in " ".join(x["reasons"])],
        "invalid_distance_orders": [x["order_key"] for x in comparisons if "invalid_" in " ".join(x["reasons"]) and "distance" in " ".join(x["reasons"])],
        "strict_comparable_orders": len(strict_items), "comparable_route_orders": len(route_items),
        "non_comparable_orders": len(comparisons)-len(strict_items), "improved_orders": classes["improved"],
        "worsened_orders": classes["worsened"], "equal_orders": classes["equal"], "no_route_activity_orders": classes["no_route_activity"],
        "current_raw_distance_m": cr, "proposed_raw_distance_m": pr, "current_comparable_distance_m": cdist,
        "proposed_comparable_distance_m": pdist, "comparable_distance_saved_m": saved,
        "current_receipt_unresolved_qty_units": raw["current_receipt_unresolved_qty_units"],
        "proposed_receipt_unresolved_qty_units": raw["proposed_receipt_unresolved_qty_units"],
        "current_shortage_units": raw["current_shortage_units"], "proposed_shortage_units": raw["proposed_shortage_units"],
        "full_day_effect_valid": full}
    return state, diagnostics
