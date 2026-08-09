"""Service-equivalent distance comparison for SimulationState outbound replay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from statistics import median
from typing import Any

DISTANCE_TOLERANCE_M = 0.000001
COMPARISON_VERSION = 1


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _valid_distance(order: Mapping[str, Any]) -> bool:
    value = order.get("route_distance_m")
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _service(order: Mapping[str, Any]) -> list[tuple[Any, Any, Any, Any]]:
    return [(d.get("sku_key"), d.get("requested_boxes"), d.get("picked_boxes"), d.get("shortage_boxes"))
            for d in order.get("demands", []) if isinstance(d, Mapping)]


def _operational_date(orders: list[Mapping[str, Any]]) -> str | None:
    dates = {str(order.get("created_at"))[:10] for order in orders if order.get("created_at")}
    return next(iter(dates)) if len(dates) == 1 else None


def compare_simulation_outbound_replay(
    replay_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compare only orders with identical demand-level service and valid routes."""
    current_orders = [o for o in replay_state.get("current", {}).get("orders", []) if isinstance(o, Mapping)]
    proposed_orders = [o for o in replay_state.get("proposed", {}).get("orders", []) if isinstance(o, Mapping)]
    proposed_by_key = {o.get("order_key"): o for o in proposed_orders}
    rows, current_distances, proposed_distances, savings = [], [], [], []
    comparable_requested = 0.0
    any_shortage = False
    for current in current_orders:
        proposed = proposed_by_key.get(current.get("order_key"))
        reasons = []
        if proposed is None:
            reasons.append("missing_proposed_order")
        elif _service(current) != _service(proposed):
            reasons.append("service_not_equivalent")
        if not _valid_distance(current) or proposed is not None and not _valid_distance(proposed):
            reasons.append("invalid_route_distance")
        if current.get("pick_events") and not current.get("returned_to_gate") or proposed is not None and proposed.get("pick_events") and not proposed.get("returned_to_gate"):
            reasons.append("not_returned_to_gate")
        strict = not reasons
        current_distance = current.get("route_distance_m")
        proposed_distance = proposed.get("route_distance_m") if proposed else None
        saved = float(current_distance) - float(proposed_distance) if strict else None
        classification = "not_comparable"
        if strict:
            current_distances.append(float(current_distance)); proposed_distances.append(float(proposed_distance)); savings.append(saved)
            comparable_requested += float(current.get("requested_boxes") or 0)
            classification = "improved" if saved > DISTANCE_TOLERANCE_M else "worsened" if saved < -DISTANCE_TOLERANCE_M else "equal"
        current_shortage = float(current.get("shortage_boxes") or 0)
        proposed_shortage = float(proposed.get("shortage_boxes") or 0) if proposed else 0
        any_shortage |= current_shortage > 0 or proposed_shortage > 0
        rows.append({"operational_date": str(current.get("created_at"))[:10] if current.get("created_at") else None,
                     "created_at": current.get("created_at"), "order_key": current.get("order_key"),
                     "outbound_order_number": current.get("outbound_order_number"), "strict_comparable": strict,
                     "classification": classification, "current_distance_m": current_distance,
                     "proposed_distance_m": proposed_distance, "distance_saved_m": saved,
                     "distance_saved_percent": saved / float(current_distance) * 100 if strict and current_distance else None,
                     "requested_boxes": current.get("requested_boxes"), "current_picked_boxes": current.get("picked_boxes"),
                     "proposed_picked_boxes": proposed.get("picked_boxes") if proposed else None,
                     "current_shortage_boxes": current.get("shortage_boxes"),
                     "proposed_shortage_boxes": proposed.get("shortage_boxes") if proposed else None, "reasons": reasons})
    count, total = len(savings), len(current_orders)
    current_total, proposed_total = sum(current_distances), sum(proposed_distances)
    saved_total = current_total - proposed_total
    requested_total = sum(float(o.get("requested_boxes") or 0) for o in current_orders)
    improved = sum(value > DISTANCE_TOLERANCE_M for value in savings)
    worsened = sum(value < -DISTANCE_TOLERANCE_M for value in savings)
    equal = count - improved - worsened
    full_day_valid = count == total == len(proposed_orders) and not any_shortage
    metrics = {"current_total_distance_m": current_total, "proposed_total_distance_m": proposed_total,
               "distance_saved_m": saved_total, "distance_saved_percent": saved_total / current_total * 100 if current_total else 0.0,
               "average_current_distance_per_order_m": current_total / count if count else None,
               "average_proposed_distance_per_order_m": proposed_total / count if count else None,
               "average_saved_per_order_m": sum(savings) / count if count else None,
               "median_current_distance_per_order_m": median(current_distances) if count else None,
               "median_proposed_distance_per_order_m": median(proposed_distances) if count else None,
               "median_saved_per_order_m": median(savings) if count else None,
               "improved_orders": improved, "worsened_orders": worsened, "equal_orders": equal,
               "improved_orders_percent": improved / count * 100 if count else 0.0,
               "worsened_orders_percent": worsened / count * 100 if count else 0.0}
    coverage = {"orders_total": total, "strict_comparable_orders": count, "non_comparable_orders": total - count,
                "route_orders": sum(_valid_distance(o) for o in current_orders),
                "order_comparability_percent": count / total * 100 if total else 100.0,
                "requested_boxes_total": requested_total, "comparable_requested_boxes": comparable_requested,
                "requested_boxes_coverage_percent": comparable_requested / requested_total * 100 if requested_total else 100.0}
    raw_current = sum(float(o.get("route_distance_m") or 0) for o in current_orders)
    raw_proposed = sum(float(o.get("route_distance_m") or 0) for o in proposed_orders)
    identity = {"version": COMPARISON_VERSION, "replay_id": replay_state.get("simulation_outbound_replay_id"),
                "metrics": metrics, "coverage": coverage, "orders": rows}
    comparison = {"simulation_distance_comparison_version": COMPARISON_VERSION,
                  "simulation_distance_comparison_id": _hash(identity), **identity,
                  "operational_date": _operational_date(current_orders), "summary": metrics,
                  "coverage": coverage, "orders": rows,
                  "raw_summary": {"current_total_distance_m": raw_current, "proposed_total_distance_m": raw_proposed,
                                  "raw_distance_difference_m": raw_current - raw_proposed,
                                  "raw_distance_difference_is_business_effect": count == total},
                  "full_day_effect_valid": full_day_valid,
                  "scope": "full_day" if full_day_valid else "comparable_orders_only"}
    return comparison, {"configuration_errors": [], "unmatched_proposed_orders": len(proposed_orders) - sum(o.get("order_key") in proposed_by_key for o in current_orders)}
