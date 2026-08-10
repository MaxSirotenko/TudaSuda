"""Persisted monthly FACT versus PROPOSED placement-only measurement.

This module is intentionally not a replay or placement engine.  It joins the
immutable output of :mod:`warehouse_monthly_fact_replay` to a PROPOSED replay
produced by the existing placement and physical-graph routing pipeline.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from warehouse_factual_data import DATA_ROOT

COMPARISON_VERSION = "monthly-placement-only-v1"
PLACEMENT_ENGINE = "warehouse_proposed_scenario.build_proposed_scenario"
ROUTING_ENGINE = "warehouse_simulation_outbound_replay.replay_factual_orders_on_graph"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _signature(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def comparison_input_signature(*, fact_result_signature: str, proposed_placement_signature: str,
                               warehouse_model_signature: str, routing_version: str,
                               ruleset_signature: str, comparison_version: str = COMPARISON_VERSION) -> str:
    """Fingerprint calculation inputs; presentation state is deliberately absent."""
    return _signature({"fact_result_signature": fact_result_signature,
        "proposed_placement_signature": proposed_placement_signature,
        "warehouse_model_signature": warehouse_model_signature, "routing_version": routing_version,
        "ruleset_signature": ruleset_signature, "comparison_version": comparison_version})


def is_comparison_stale(comparison: Mapping[str, Any], **inputs: str) -> bool:
    """Return whether placement/geometry/rules/replay authority has changed."""
    return comparison.get("input_signature") != comparison_input_signature(**inputs)


def load_fact_result(artifact: str | Path) -> dict[str, Any]:
    """Load PR #165 partitions without invoking FACT replay."""
    root = Path(artifact)
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    summary["daily_results"] = [json.loads((root / name).read_text(encoding="utf-8"))
                                for name in summary.get("daily_artifacts", [])]
    summary["artifact_path"] = str(root)
    return summary


def _identity(order: Mapping[str, Any]) -> str:
    value = order.get("order_identity") or {}
    if isinstance(value, Mapping):
        return _canonical({k: value.get(k) for k in ("document_ref", "document_number", "occurred_at")})
    return str(order.get("order_key") or "")


def _demand(order: Mapping[str, Any]) -> str:
    if order.get("demand_signature"): return str(order["demand_signature"])
    rows = []
    evidence = order.get("source_evidence") or order.get("demand_results") or []
    for row in evidence:
        rows.append((str(row.get("sku_key") or ""), str(row.get("demand_key") or row.get("source_evidence", {}).get("source_row") or ""),
                     float(row.get("requested_boxes") or row.get("quantity") or 0)))
    return _signature(sorted(rows))


def _orders(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {_identity(order): order for day in result.get("daily_results", [])
            for order in day.get("order_results", [])}


def _distance(order: Mapping[str, Any]) -> float | None:
    value = order.get("picker_distance_m")
    return round(float(value), 6) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _percent(saved: float, fact: float) -> float | None:
    return None if fact == 0 else round(saved / fact * 100, 6)


def _route(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(x) for x in order.get("route_legs", [])]


def _pick_stops(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(x) for x in (order.get("factual_pick_stops") or order.get("pick_events") or [])]


def _contributions(order: Mapping[str, Any], assignments: Mapping[str, Mapping[str, Any]],
                   scenario: str) -> dict[tuple[str, str, str], float]:
    """Attribute measured graph legs to their destination pick (return to last pick)."""
    stops, legs = _pick_stops(order), _route(order); result: dict[tuple[str, str, str], float] = defaultdict(float)
    for index, leg in enumerate(legs):
        stop = stops[min(index, len(stops) - 1)] if stops else {}
        sku = str(stop.get("sku_key") or "unattributed")
        placement = assignments.get(sku, {})
        prefix = "fact_" if scenario == "fact" else ""
        result[(sku, str(placement.get(prefix + "zone") or placement.get("zone") or stop.get("zone") or "unresolved"),
                str(placement.get(prefix + "row") or placement.get("row") or "unresolved"))] += float(leg.get("distance_m") or 0)
    return result


def compare_monthly_placement(*, fact_result: Mapping[str, Any], proposed_result: Mapping[str, Any],
                              proposed_assignments: Sequence[Mapping[str, Any]],
                              proposed_placement_signature: str, warehouse_model_signature: str,
                              routing_version: str, ruleset_signature: str,
                              persist: bool = True, root: Path = DATA_ROOT) -> dict[str, Any]:
    """Join identical orders and measure graph-route difference; never mutate inputs."""
    fact_signature = str(fact_result.get("input_signature") or "")
    signature = comparison_input_signature(fact_result_signature=fact_signature,
        proposed_placement_signature=proposed_placement_signature,
        warehouse_model_signature=warehouse_model_signature, routing_version=routing_version,
        ruleset_signature=ruleset_signature)
    fact_orders, proposed_orders = _orders(fact_result), _orders(proposed_result)
    assignments = {str(x.get("sku_key") or ""): dict(x) for x in proposed_assignments}
    order_rows, warning_codes = [], []
    if proposed_result.get("routing_engine") not in (None, ROUTING_ENGINE):
        warning_codes.append("routing_engine_mismatch")
    if proposed_result.get("placement_engine") not in (None, PLACEMENT_ENGINE):
        warning_codes.append("non_authoritative_placement_engine")
    fact_contrib: dict[tuple[str, str, str], float] = defaultdict(float)
    proposed_contrib: dict[tuple[str, str, str], float] = defaultdict(float)
    all_ids = sorted(set(fact_orders) | set(proposed_orders))
    for key in all_ids:
        fact, proposed = fact_orders.get(key), proposed_orders.get(key); reasons = []
        if fact is None: reasons.append("fact_order_missing")
        if proposed is None: reasons.append("proposed_order_missing")
        if fact and not fact.get("strict_comparable", False): reasons.append("unresolved_factual_blocker")
        if fact and proposed and _demand(fact) != _demand(proposed): reasons.append("demand_mismatch")
        fd, pd = (_distance(fact or {}), _distance(proposed or {}))
        if fd is None: reasons.append("fact_route_missing")
        if pd is None: reasons.append("proposed_route_missing")
        if proposed and proposed.get("geometry_signature") not in (None, warehouse_model_signature): reasons.append("warehouse_graph_mismatch")
        blockers = list((proposed or {}).get("blockers") or [])
        reasons.extend(str(x.get("code") or x) for x in blockers)
        reasons.extend(code for code in warning_codes if code in {"routing_engine_mismatch", "non_authoritative_placement_engine"})
        comparable = not reasons
        saved = round(fd - pd, 6) if comparable else None
        identity = (fact or proposed or {}).get("order_identity") or {}
        changed = [x for x in proposed_assignments if x.get("fact_cell") != x.get("target_cell") and
                   str(x.get("sku_key")) in {str(s.get("sku_key")) for s in _pick_stops(fact or {})}]
        row = {"order_identity": identity, "operational_day": (fact or proposed or {}).get("operational_day"),
            "requested_boxes": (fact or proposed or {}).get("requested_boxes", 0), "comparable": comparable,
            "fact_meters": fd, "proposed_meters": pd, "saved_meters": saved,
            "saved_percent": _percent(saved, fd) if comparable else None, "fact_route": _route(fact or {}),
            "proposed_route": _route(proposed or {}), "fact_pick_stops": _pick_stops(fact or {}),
            "proposed_pick_stops": _pick_stops(proposed or {}), "moved_sku_count": len({x.get("sku_key") for x in changed}),
            "changed_cells_count": len(changed), "changed_skus": changed, "warnings": sorted(set(reasons))}
        order_rows.append(row); warning_codes.extend(reasons)
        if comparable:
            for group, value in _contributions(fact, assignments, "fact").items(): fact_contrib[group] += value
            for group, value in _contributions(proposed, assignments, "proposed").items(): proposed_contrib[group] += value
    comparable = [x for x in order_rows if x["comparable"]]
    dates = sorted({str(x["operational_day"]) for x in order_rows})
    daily = []
    for day in dates:
        rows, full = ([x for x in comparable if x["operational_day"] == day], [x for x in order_rows if x["operational_day"] == day])
        fact_m, proposed_m = sum(x["fact_meters"] for x in rows), sum(x["proposed_meters"] for x in rows)
        saved = round(fact_m - proposed_m, 6)
        daily.append({"date": day, "fact_meters": fact_m, "proposed_meters": proposed_m, "saved_meters": saved,
            "saved_percent": _percent(saved, fact_m), "ro_count": len(full), "comparable_orders": len(rows),
            "boxes": sum(float(x["requested_boxes"] or 0) for x in full), "strict_coverage": len(rows)/len(full) if full else 1.0,
            "blocked_orders": len(full)-len(rows), "warnings": sorted({w for x in full for w in x["warnings"]})})
    fact_total, proposed_total = sum(x["fact_meters"] for x in comparable), sum(x["proposed_meters"] for x in comparable)
    saved_total = round(fact_total-proposed_total, 6)
    contribution = []
    for group in sorted(set(fact_contrib) | set(proposed_contrib)):
        f, p = round(fact_contrib[group], 6), round(proposed_contrib[group], 6)
        contribution.append({"sku_key": group[0], "zone": group[1], "row": group[2],
                             "fact_meters": f, "proposed_meters": p, "delta_meters": round(f-p, 6)})
    sku_delta: dict[str, float] = defaultdict(float)
    for item in contribution: sku_delta[item["sku_key"]] += item["delta_meters"]
    placement_changes = []
    for raw in proposed_assignments:
        if raw.get("fact_cell") == raw.get("target_cell"): continue
        item, sku = dict(raw), str(raw.get("sku_key") or "")
        affected = [row for row in comparable if sku in {str(stop.get("sku_key") or "") for stop in row["fact_pick_stops"]}]
        item.update(distance_impact_m=round(sku_delta.get(sku, 0), 6), orders_affected=len(affected),
                    boxes_affected=sum(float(row.get("requested_boxes") or 0) for row in affected))
        placement_changes.append(item)
    result = {"period_from": fact_result.get("period_from"), "period_to": fact_result.get("period_to"),
        "fact_meters": fact_total, "proposed_meters": proposed_total, "saved_meters": saved_total,
        "saved_percent": _percent(saved_total, fact_total), "fact_orders": len(fact_orders),
        "proposed_orders": len(proposed_orders), "full_order_count": len(all_ids), "comparable_orders": len(comparable),
        "excluded_orders": len(all_ids)-len(comparable), "blocked_orders": len(all_ids)-len(comparable),
        "fact_coverage": len(comparable)/len(fact_orders) if fact_orders else 1.0,
        "proposed_coverage": len(comparable)/len(proposed_orders) if proposed_orders else 1.0,
        "warnings": sorted(set(warning_codes)), "daily_results": daily, "order_comparisons": order_rows,
        "contribution_analysis": contribution, "placement_changes": placement_changes,
        "fact_result_reference": fact_result.get("artifact_path") or fact_signature,
        "proposed_result_reference": proposed_result.get("artifact_path") or proposed_result.get("input_signature"),
        "fact_result_signature": fact_signature, "proposed_placement_signature": proposed_placement_signature,
        "geometry_signature": warehouse_model_signature, "ruleset_signature": ruleset_signature,
        "placement_engine": PLACEMENT_ENGINE, "routing_engine": ROUTING_ENGINE,
        "routing_version": routing_version, "comparison_version": COMPARISON_VERSION, "input_signature": signature,
        "route_graph": proposed_result.get("route_graph"),
        "readiness": "ready" if len(comparable) == len(all_ids) else "partial"}
    if persist:
        destination = root / "monthly_comparisons" / signature.replace(":", "_"); destination.mkdir(parents=True, exist_ok=True)
        (destination / "comparison.json").write_text(_canonical(result), encoding="utf-8")
        result["artifact_path"] = str(destination)
    return result
