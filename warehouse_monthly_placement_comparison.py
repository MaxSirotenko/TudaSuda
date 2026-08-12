"""Persisted monthly FACT versus PROPOSED placement-only measurement.

This module is intentionally not a replay or placement engine.  It joins the
immutable output of :mod:`warehouse_monthly_fact_replay` to a PROPOSED replay
produced by the existing placement and physical-graph routing pipeline.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from warehouse_factual_data import DATA_ROOT

LEGACY_COMPARISON_VERSION = "monthly-placement-only-v1"
COMPARISON_VERSION = "monthly-placement-only-v2"
PLACEMENT_ENGINE = "warehouse_proposed_scenario.build_proposed_scenario"
ROUTING_ENGINE = "warehouse_simulation_outbound_replay.replay_factual_orders_on_graph"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _signature(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(_canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError): return None


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
    """Load only the compact FACT summary; day details are explicitly lazy."""
    root = Path(artifact)
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    summary["artifact_path"] = str(root)
    if summary.get("format_version") not in (None, "monthly-fact-v1", "monthly-fact-v2"):
        summary["compatibility_status"] = "unsupported_artifact_format"
    elif summary.get("format_version") in (None, "monthly-fact-v1"):
        summary["compatibility_status"] = "legacy_read_only"
    return summary


def load_fact_day(artifact: str | Path, day: str) -> dict[str, Any]:
    """Load exactly one immutable FACT partition."""
    root = Path(artifact)
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    names = summary.get("daily_artifacts", [])
    name = next((x for x in names if x == f"day={day}.json"), None)
    if name is None: raise KeyError(day)
    return json.loads((root / name).read_text(encoding="utf-8"))


def load_comparison_day(artifact: str | Path, day: str) -> dict[str, Any]:
    """Load one comparison detail partition on UI demand."""
    root = Path(artifact)
    summary = json.loads((root / "comparison.json").read_text(encoding="utf-8"))
    name = next((x for x in summary.get("daily_artifacts", []) if x == f"day={day}.json"), None)
    if name is None: raise KeyError(day)
    return json.loads((root / name).read_text(encoding="utf-8"))


def _identity(order: Mapping[str, Any]) -> str:
    value = order.get("order_identity") or {}
    if isinstance(value, Mapping):
        return _canonical({k: value.get(k) for k in ("document_ref", "document_number", "occurred_at")})
    return str(order.get("order_key") or "")


def _demand(order: Mapping[str, Any]) -> str:
    if order.get("demand_signature"): return str(order["demand_signature"])
    rows = []
    for row in order.get("source_evidence") or order.get("demand_results") or []:
        rows.append((str(row.get("sku_key") or ""), str(row.get("demand_key") or row.get("source_evidence", {}).get("source_row") or ""),
                     float(row.get("requested_boxes") or row.get("quantity") or 0)))
    return _signature(sorted(rows))


def _distance(order: Mapping[str, Any]) -> float | None:
    value = order.get("picker_distance_m")
    return round(float(value), 6) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _percent(saved: float, fact: float) -> float | None:
    return None if fact == 0 else round(saved / fact * 100, 6)


def _route(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(x) for x in order.get("route_legs", [])]


def _pick_stops(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(x) for x in (order.get("factual_pick_stops") or order.get("pick_events") or [])]


def _contributions(order: Mapping[str, Any], assignments: Mapping[str, Mapping[str, Any]], scenario: str):
    result: dict[tuple[str, str, str], float] = defaultdict(float)
    stops, legs = _pick_stops(order), _route(order)
    for index, leg in enumerate(legs):
        stop = stops[min(index, len(stops)-1)] if stops else {}; sku = str(stop.get("sku_key") or "unattributed")
        placement = assignments.get(sku, {}); prefix = "fact_" if scenario == "fact" else ""
        result[(sku, str(placement.get(prefix+"zone") or placement.get("zone") or stop.get("zone") or "unresolved"),
                str(placement.get(prefix+"row") or placement.get("row") or "unresolved"))] += float(leg.get("distance_m") or 0)
    return result


def _inline_days(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(day.get("operational_day") or next((o.get("operational_day") for o in day.get("order_results", [])), "")): day
            for day in result.get("daily_results", [])}


def _day_names(result: Mapping[str, Any]) -> list[str]:
    inline = _inline_days(result)
    if inline: return sorted(inline)
    return sorted(Path(x).stem.removeprefix("day=") for x in result.get("daily_artifacts", []))


def _load_day(result: Mapping[str, Any], day: str) -> Mapping[str, Any]:
    inline = _inline_days(result)
    if day in inline: return inline[day]
    root = result.get("artifact_path")
    if not root: return {}
    try: return json.loads((Path(str(root)) / f"day={day}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {}


def _compare_day(day: str, fact_day: Mapping[str, Any], proposed_day: Mapping[str, Any], assignments,
                 assignment_map, warehouse_model_signature: str, authority_warnings: list[str]):
    fact_orders = {_identity(x): x for x in fact_day.get("order_results", [])}
    proposed_orders = {_identity(x): x for x in proposed_day.get("order_results", [])}
    rows=[]; warnings=[]; fc=defaultdict(float); pc=defaultdict(float); affected=defaultdict(lambda: [0, 0.0])
    for key in sorted(set(fact_orders) | set(proposed_orders)):
        fact, proposed = fact_orders.get(key), proposed_orders.get(key); reasons=[]
        if fact is None: reasons.append("fact_order_missing")
        if proposed is None: reasons.append("proposed_order_missing")
        if fact and not fact.get("strict_comparable", False): reasons.append("unresolved_factual_blocker")
        if fact and proposed and _demand(fact) != _demand(proposed): reasons.append("demand_mismatch")
        fd,pd=_distance(fact or {}),_distance(proposed or {})
        if fd is None: reasons.append("fact_route_missing")
        if pd is None: reasons.append("proposed_route_missing")
        if proposed and proposed.get("geometry_signature") not in (None, warehouse_model_signature): reasons.append("warehouse_graph_mismatch")
        reasons.extend(str(x.get("code") or x) for x in (proposed or {}).get("blockers", [])); reasons.extend(authority_warnings)
        comparable=not reasons; saved=round(fd-pd,6) if comparable else None
        fact_skus={str(x.get("sku_key") or "") for x in _pick_stops(fact or {})}
        changed=[x for x in assignments if x.get("fact_cell") != x.get("target_cell") and str(x.get("sku_key")) in fact_skus]
        row={"order_identity":(fact or proposed or {}).get("order_identity") or {}, "operational_day":day,
            "requested_boxes":(fact or proposed or {}).get("requested_boxes",0), "comparable":comparable,
            "fact_meters":fd,"proposed_meters":pd,"saved_meters":saved,"saved_percent":_percent(saved,fd) if comparable else None,
            "fact_route":_route(fact or {}),"proposed_route":_route(proposed or {}),"fact_pick_stops":_pick_stops(fact or {}),
            "proposed_pick_stops":_pick_stops(proposed or {}),"moved_sku_count":len({x.get('sku_key') for x in changed}),
            "changed_cells_count":len(changed),"changed_skus":changed,"warnings":sorted(set(reasons))}
        rows.append(row); warnings.extend(reasons)
        if comparable:
            for group,value in _contributions(fact,assignment_map,"fact").items(): fc[group]+=value
            for group,value in _contributions(proposed,assignment_map,"proposed").items(): pc[group]+=value
            for sku in fact_skus: affected[sku][0]+=1; affected[sku][1]+=float(row["requested_boxes"] or 0)
    good=[x for x in rows if x["comparable"]]; fm=sum(x["fact_meters"] for x in good); pm=sum(x["proposed_meters"] for x in good)
    summary={"date":day,"fact_meters":fm,"proposed_meters":pm,"saved_meters":round(fm-pm,6),"saved_percent":_percent(fm-pm,fm),
        "ro_count":len(rows),"fact_orders":len(fact_orders),"proposed_orders":len(proposed_orders),"comparable_orders":len(good),
        "boxes":sum(float(x["requested_boxes"] or 0) for x in rows),"strict_coverage":len(good)/len(rows) if rows else 1.0,
        "blocked_orders":len(rows)-len(good),"warnings":sorted(set(warnings))}
    contrib={group:[fc[group],pc[group]] for group in set(fc)|set(pc)}
    return {"format_version":"monthly-placement-only-v2","operational_day":day,"summary":summary,"order_comparisons":rows}, contrib, affected


def compare_monthly_placement(*, fact_result: Mapping[str, Any], proposed_result: Mapping[str, Any],
                              proposed_assignments: Sequence[Mapping[str, Any]], proposed_placement_signature: str,
                              warehouse_model_signature: str, routing_version: str, ruleset_signature: str,
                              persist: bool = True, root: Path = DATA_ROOT,
                              progress_callback=None) -> dict[str, Any]:
    """Compare one operational day at a time and checkpoint every completed day."""
    fact_signature=str(fact_result.get("input_signature") or "")
    signature=comparison_input_signature(fact_result_signature=fact_signature, proposed_placement_signature=proposed_placement_signature,
        warehouse_model_signature=warehouse_model_signature,routing_version=routing_version,ruleset_signature=ruleset_signature,
        comparison_version="monthly-placement-only-v2")
    legacy_inline=bool(fact_result.get("daily_results") and proposed_result.get("daily_results"))
    destination=root/"monthly_comparisons"/signature.replace(":","_"); manifest_path=destination/"manifest.json"
    manifest=_read_json(manifest_path) if persist else None
    if not manifest or manifest.get("input_signature") != signature or manifest.get("format_version") != "monthly-placement-only-v2":
        manifest={"format_version":"monthly-placement-only-v2","input_signature":signature,"status":"in_progress","days":{}}
        if persist: _atomic_json(manifest_path,manifest)
    assignments=[dict(x) for x in proposed_assignments]; amap={str(x.get("sku_key") or ""):x for x in assignments}
    authority=[]
    if proposed_result.get("routing_engine") not in (None,ROUTING_ENGINE): authority.append("routing_engine_mismatch")
    if proposed_result.get("placement_engine") not in (None,PLACEMENT_ENGINE): authority.append("non_authoritative_placement_engine")
    all_days=sorted(set(_day_names(fact_result))|set(_day_names(proposed_result))); legacy_details=[]
    for index,day in enumerate(all_days):
        path=destination/f"day={day}.json"; entry=manifest["days"].get(day) or {}; detail=_read_json(path) if persist else None
        valid=detail and entry.get("digest")==_signature(detail) and detail.get("input_signature")==signature
        if valid:
            if progress_callback: progress_callback({"phase":"day_skipped","operational_day":day,"day_index":index+1,"days_total":len(all_days)})
            continue
        if progress_callback: progress_callback({"phase":"day_started","operational_day":day,"day_index":index+1,"days_total":len(all_days)})
        detail,contrib,affected=_compare_day(day,_load_day(fact_result,day),_load_day(proposed_result,day),assignments,amap,warehouse_model_signature,authority)
        detail.update(input_signature=signature,contributions=[[list(k),v] for k,v in contrib.items()],affected_skus=dict(affected))
        if persist: _atomic_json(path,detail)
        manifest["days"][day]={"status":"complete","artifact":path.name,"digest":_signature(detail),"summary":detail["summary"]}
        if persist: _atomic_json(manifest_path,manifest)
        if legacy_inline: legacy_details.append(detail)
        if progress_callback: progress_callback({"phase":"day_completed","operational_day":day,"day_index":index+1,"days_total":len(all_days)})
    # Aggregate compact checkpoints one at a time; never retain routes.
    daily=[]; contribution=defaultdict(lambda:[0.0,0.0]); affected=defaultdict(lambda:[0,0.0]); warning_codes=list(authority)
    for day in all_days:
        entry=manifest["days"].get(day); detail=_read_json(destination/entry["artifact"]) if persist and entry else next((x for x in legacy_details if x["operational_day"]==day),None)
        if not detail: continue
        daily.append(entry["summary"] if entry else detail["summary"]); warning_codes.extend(detail["summary"]["warnings"])
        for key,values in detail.get("contributions",[]):
            group=tuple(key); contribution[group][0]+=values[0]; contribution[group][1]+=values[1]
        for sku,values in detail.get("affected_skus",{}).items(): affected[sku][0]+=values[0]; affected[sku][1]+=values[1]
    fm=sum(x["fact_meters"] for x in daily); pm=sum(x["proposed_meters"] for x in daily); comparable=sum(x["comparable_orders"] for x in daily)
    fact_orders=sum(x["fact_orders"] for x in daily); proposed_orders=sum(x["proposed_orders"] for x in daily); full=sum(x["ro_count"] for x in daily)
    ca=[{"sku_key":k[0],"zone":k[1],"row":k[2],"fact_meters":round(v[0],6),"proposed_meters":round(v[1],6),"delta_meters":round(v[0]-v[1],6)} for k,v in sorted(contribution.items())]
    sku_delta=defaultdict(float)
    for x in ca: sku_delta[x["sku_key"]]+=x["delta_meters"]
    changes=[]
    for raw in assignments:
        if raw.get("fact_cell")==raw.get("target_cell"): continue
        item=dict(raw); sku=str(raw.get("sku_key") or ""); item.update(distance_impact_m=round(sku_delta[sku],6),orders_affected=affected[sku][0],boxes_affected=affected[sku][1]); changes.append(item)
    result={"period_from":fact_result.get("period_from"),"period_to":fact_result.get("period_to"),"fact_meters":fm,"proposed_meters":pm,
        "saved_meters":round(fm-pm,6),"saved_percent":_percent(fm-pm,fm),"fact_orders":fact_orders,"proposed_orders":proposed_orders,
        "full_order_count":full,"comparable_orders":comparable,"excluded_orders":full-comparable,"blocked_orders":full-comparable,
        "fact_coverage":comparable/fact_orders if fact_orders else 1.0,"proposed_coverage":comparable/proposed_orders if proposed_orders else 1.0,
        "warnings":sorted(set(warning_codes)),"daily_results":daily,"daily_artifacts":[f"day={x}.json" for x in all_days],
        "contribution_analysis":ca,"placement_changes":changes,"fact_result_reference":fact_result.get("artifact_path") or fact_signature,
        "proposed_result_reference":proposed_result.get("artifact_path") or proposed_result.get("input_signature"),"fact_result_signature":fact_signature,
        "proposed_placement_signature":proposed_placement_signature,"geometry_signature":warehouse_model_signature,"ruleset_signature":ruleset_signature,
        "placement_engine":PLACEMENT_ENGINE,"routing_engine":ROUTING_ENGINE,"routing_version":routing_version,
        "comparison_version":"monthly-placement-only-v2","format_version":"monthly-placement-only-v2","input_signature":signature,
        "readiness":"ready" if comparable==full else "partial","days_total":len(all_days),"days_completed":len(daily)}
    if legacy_inline:
        result["order_comparisons"]=[row for detail in legacy_details for row in detail["order_comparisons"]]
        result["comparison_version"]="monthly-placement-only-v1"
    if persist:
        _atomic_json(destination/"comparison.json",result); manifest["status"]="complete"; manifest["days_completed"]=len(daily); _atomic_json(manifest_path,manifest)
        result["artifact_path"]=str(destination)
    return result
