"""Authoritative monthly FACT adapter over factual partitions and V1 routing.

No workbook parsing, placement optimisation, or PROPOSED scenario belongs here.
Every operational day is independently anchored to its factual 00:00 snapshot.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from warehouse_factual_data import (
    DATA_ROOT, build_monthly_data_readiness, geometry_model_signature,
    load_effective_placement, load_effective_rows, load_registry, positive_outbound,
)
from warehouse_physical_graph import build_physical_warehouse_graph
from warehouse_simulation_outbound_replay import replay_factual_orders_on_graph

LEGACY_REPLAY_VERSION = "monthly-fact-v1"
REPLAY_VERSION = "monthly-fact-v2"
LIMITATIONS = [
    "intraday_receipts_are_evidence_not_stock_mutation",
    "inventory_documents_are_validation_evidence_not_state_mutation",
    "fact_source_location_ambiguity_is_not_guessed",
    "historical_source_pallet_ref_is_not_a_physical_pallet",
    "each_day_resets_to_factual_00_00_snapshot",
    "no_proposed_or_model_savings_calculated",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _atomic_json(path: Path, value: Any) -> None:
    """Publish JSON in one replace operation (also safe on Windows)."""
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
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _day_digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _day_totals(day: Mapping[str, Any]) -> dict[str, Any]:
    return {key: day.get(key) for key in ("status", "orders_total", "orders_strict", "requested_boxes",
        "picked_boxes", "shortage_boxes", "picker_distance_m", "source_location_ambiguity_count",
        "unroutable_cell_count", "missing_opening_sku_count")}


def monthly_fact_input_signature(*, active_dataset_signature: Any, cell_mapping_signature: Any,
                                 model: Mapping[str, Any], gate_state: Mapping[str, Any],
                                 period_from: str, period_to: str) -> str:
    authority = {"replay_version": REPLAY_VERSION, "active_dataset_signature": active_dataset_signature,
        "cell_mapping_signature": cell_mapping_signature, "geometry_signature": geometry_model_signature(model),
        "model_id": model.get("model_id"), "gate_state": gate_state,
        "period_from": period_from, "period_to": period_to}
    return "sha256:" + hashlib.sha256(_canonical(authority).encode()).hexdigest()


def _number(value: Any) -> float:
    if isinstance(value, bool): return 0.0
    try: result = float(value)
    except (TypeError, ValueError): return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _display(value: float) -> int | float:
    return int(value) if value.is_integer() else round(value, 6)


def _order_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("document_ref") or ""), str(row.get("document_number") or ""),
            str(row.get("occurred_at") or ""))


def _opening_and_authority(rows: list[dict[str, Any]], access: set[str]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    by_sku: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lots = []
    for ordinal, row in enumerate(rows):
        sku, cell = str(row.get("sku_key") or ""), row.get("resolved_geometry_cell_key")
        qty = _number(row.get("source_stock_quantity"))
        if sku and qty > 0: by_sku[sku].append(row)
        if sku and cell and qty and str(cell) in access:
            lots.append({"stock_lot_id": f"fact:{ordinal}:{row.get('dataset_id')}:{row.get('source_row')}",
                         "sku_key": sku, "cell_key": str(cell), "qty_boxes": qty,
                         "location_status": "located", "source_evidence": {
                             "dataset_id": row.get("dataset_id"), "source_row": row.get("source_row"),
                             "source_cell": row.get("source_cell"),
                             "source_pallet_ref": row.get("source_pallet_ref")}})
    return {"stock_lots": lots}, by_sku


def _residual_control(working: Mapping[str, Any], next_rows: list[dict[str, Any]]) -> dict[str, Any]:
    simulated: dict[str, float] = defaultdict(float); factual: dict[str, float] = defaultdict(float)
    for lot in working.get("stock_lots", []) or []:
        simulated[str(lot.get("sku_key") or "")] += _number(lot.get("qty_boxes"))
    unsupported = 0
    for row in next_rows:
        qty = row.get("source_stock_quantity")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool): unsupported += 1; continue
        factual[str(row.get("sku_key") or "")] += max(float(qty), 0)
    comparable = sorted((set(simulated) & set(factual)) - {""})
    differences = [{"sku_key": sku, "simulated_residual_boxes": _display(simulated[sku]),
                    "next_factual_quantity": _display(factual[sku]),
                    "difference": _display(simulated[sku] - factual[sku])}
                   for sku in comparable if not math.isclose(simulated[sku], factual[sku], abs_tol=1e-9)]
    return {"authority": "non_authoritative_control_only", "comparable_sku": len(comparable),
            "matched_residual_quantity": len(comparable) - len(differences),
            "difference_count": len(differences),
            "difference_amount": _display(sum(abs(float(x["difference"])) for x in differences)),
            "unsupported_comparison_count": unsupported, "differences": differences[:100]}


def resolve_factual_route_order(candidates: list[Mapping[str, Any]], *,
                                routable_cells: set[str] | None = None) -> dict[str, Any]:
    """Shared one-day/monthly authority for factual SKU source and pick order."""
    factual_cells = sorted({str(r.get("source_cell") or r.get("cell")) for r in candidates})
    resolved = sorted({str(r.get("resolved_geometry_cell_key")) for r in candidates if r.get("resolved_geometry_cell_key")})
    raw_orders = [r.get("cell_picking_order") for r in candidates]
    valid_orders = [value for value in raw_orders if isinstance(value, (int, float)) and not isinstance(value, bool)
                    and float(value).is_integer() and value >= 0]
    orders = set(valid_orders)
    code = ("fact_sku_not_in_opening_snapshot" if not candidates else
            "fact_source_location_ambiguous" if len(factual_cells) != 1 else
            "historical_cell_unresolved" if len(resolved) != 1 else
            "fact_cell_not_routable" if routable_cells is not None and resolved[0] not in routable_cells else
            "fact_cell_picking_order_missing_or_conflicting" if len(orders) != 1 or len(valid_orders) != len(raw_orders) else None)
    return {"code": code, "factual_source_cells": factual_cells, "factual_geometry_cells": resolved,
            "cell_picking_order": next(iter(orders)) if len(orders) == 1 else None}


def _replay_day(day: str, placement: dict[str, Any], outbound: dict[str, Any], next_placement: dict[str, Any],
                model: Mapping[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    access = {str(x["cell_key"]) for x in graph.get("cell_access_links", [])}
    opening, by_sku = _opening_and_authority(placement.get("rows", []), access)
    raw_lines = positive_outbound(outbound.get("rows", [])); grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in raw_lines: grouped[_order_identity(row)].append(row)
    engine_orders, preflight = [], {}
    for identity, lines in sorted(grouped.items()):
        demands, blockers, evidence = [], [], []
        for line in lines:
            sku = str(line.get("sku_key") or ""); candidates = by_sku.get(sku, [])
            authority = resolve_factual_route_order(candidates, routable_cells=access); code = authority["code"]
            item = {"sku_key": sku, "requested_boxes": line.get("quantity"), "demand_key": line.get("line_number"),
                    "source_evidence": {"dataset_id": line.get("dataset_id"), "source_row": line.get("source_row"),
                                        "document_ref": line.get("document_ref"), "document_number": line.get("document_number")},
                    "factual_source_cells": authority["factual_source_cells"],
                    "factual_geometry_cells": authority["factual_geometry_cells"]}
            evidence.append(item)
            if code: blockers.append({"code": code, "sku_key": sku, "source_row": line.get("source_row")})
            else: demands.append({**item, "cell_picking_order": authority["cell_picking_order"]})
        # Cell order is authority; source row order is deliberately irrelevant.
        demands.sort(key=lambda x: (x["cell_picking_order"], x["factual_geometry_cells"][0], str(x["demand_key"])))
        # Engine identity is deterministic and collision-safe; the factual
        # document fields below remain the user-facing identity/evidence.
        key = "fact-ro:" + hashlib.sha256(_canonical(identity).encode()).hexdigest()
        preflight[key] = {"identity": identity, "all_lines": lines, "evidence": evidence, "blockers": blockers}
        engine_orders.append({"order_key": key, "outbound_order_number": identity[1], "created_at": identity[2], "demands": demands})
    scenario, diagnostics = replay_factual_orders_on_graph(model, opening, engine_orders, graph)
    results = []
    for routed in scenario.get("orders", []):
        pf = preflight[routed["order_key"]]; blocked_requested = sum(_number(x.get("quantity")) for x in pf["all_lines"]) - float(routed["requested_boxes"])
        blockers = pf["blockers"] + ([{"code": "fact_route_unavailable"}] if routed.get("status") == "invalid_route" else [])
        strict = not blockers and not routed.get("source_location_ambiguous") and routed.get("picker_distance_m") is not None
        results.append({**routed, "operational_day": day, "order_identity": {"document_ref": pf["identity"][0],
            "document_number": pf["identity"][1], "occurred_at": pf["identity"][2]},
            "requested_boxes": _display(float(routed["requested_boxes"]) + blocked_requested),
            "shortage_boxes": _display(float(routed["shortage_boxes"]) + blocked_requested),
            "strict_comparable": strict, "factual_pick_stops": routed.get("pick_events", []),
            "factual_geometry_cells": sorted({e["cell_key"] for e in routed.get("pick_events", [])}),
            "source_evidence": pf["evidence"], "blockers": blockers, "warnings": []})
    requested = sum(float(x["requested_boxes"]) for x in results); picked = sum(float(x["picked_boxes"]) for x in results)
    strict = [x for x in results if x["strict_comparable"]]
    ambiguities = sum(any(b["code"] == "fact_source_location_ambiguous" for b in x["blockers"]) for x in results)
    unroutable = sum(any(b["code"] == "fact_cell_not_routable" for b in x["blockers"]) for x in results)
    missing = sum(any(b["code"] == "fact_sku_not_in_opening_snapshot" for b in x["blockers"]) for x in results)
    day_blockers = placement.get("conflicts", []) + outbound.get("conflicts", [])
    status = "blocked" if day_blockers or not scenario else "ready" if len(strict) == len(results) else "partial/non-strict"
    distance = round(sum(float(x["picker_distance_m"] or 0) for x in strict), 6)
    return {"operational_day": day, "status": status, "orders_total": len(results), "orders_strict": len(strict),
        "orders_non_strict": len(results)-len(strict), "outbound_lines": len(raw_lines), "requested_boxes": _display(requested),
        "picked_boxes": _display(picked), "shortage_boxes": _display(requested-picked), "picker_distance_m": distance,
        "route_order_coverage": len(strict)/len(results) if results else 1.0,
        "source_location_ambiguity_count": ambiguities, "unroutable_cell_count": unroutable,
        "missing_opening_sku_count": missing, "blockers": day_blockers, "warnings": [], "order_results": results,
        "residual_control_against_next_day": _residual_control(scenario.get("working_state", {}), next_placement.get("rows", [])),
        "replay_diagnostics": diagnostics}


def replay_monthly_fact(model: dict[str, Any], gate_state: dict[str, Any], period_from: str = "2026-07-01",
                        period_to: str = "2026-07-31", *, registry: Mapping[str, Any] | None = None,
                        root: Path = DATA_ROOT, readiness: Mapping[str, Any] | None = None,
                        progress_callback: Callable[[dict[str, Any]], None] | None = None,
                        persist: bool = True) -> dict[str, Any]:
    """Run authoritative FACT only after the Data Layer readiness gate passes."""
    registry = registry or load_registry(root)
    graph, graph_diagnostics = build_physical_warehouse_graph(model, gate_state)
    usable = [x["cell_key"] for x in graph.get("cell_access_links", [])]
    readiness = dict(readiness or build_monthly_data_readiness(registry, model, period_from, period_to,
                                                                root=root, usable_cell_keys=usable))
    signature = monthly_fact_input_signature(active_dataset_signature=readiness.get("active_dataset_signature"),
        cell_mapping_signature=readiness.get("cell_mapping_signature"), model=model, gate_state=gate_state,
        period_from=period_from, period_to=period_to)
    base = {"period_from": period_from, "period_to": period_to, "replay_version": REPLAY_VERSION,
            "input_signature": signature, "limitations": LIMITATIONS.copy(), "warnings": list(readiness.get("warnings", []))}
    blockers = list(readiness.get("hard_blockers", []))
    if len(graph.get("gate_links", [])) != 1: blockers.append({"code": "authoritative_gate_missing_or_invalid", "diagnostics": graph_diagnostics})
    if readiness.get("monthly_replay_ready") is not True or blockers:
        return {**base, "full_month_fact_valid": False, "days_total": 0, "days_ready": 0, "days_partial": 0,
            "days_blocked": 0, "orders_total": 0, "strict_orders": 0, "requested_boxes": 0, "picked_boxes": 0,
            "shortage_boxes": 0, "fact_picker_distance_m": None, "strict_fact_picker_distance_m": 0,
            "route_order_coverage": 0.0, "source_location_ambiguity_count": 0,
            "unresolved_fact_cell_count": 0, "daily_results": [], "blockers": blockers}
    start, end = date.fromisoformat(period_from), date.fromisoformat(period_to)
    total_days = (end-start).days+1
    destination = root / "monthly_fact" / signature.replace(":", "_")
    manifest_path = destination / "manifest.json"
    manifest = _read_json(manifest_path) if persist else None
    if not manifest or manifest.get("format_version") != REPLAY_VERSION or manifest.get("input_signature") != signature:
        manifest = {**base, "format_version": REPLAY_VERSION, "status": "in_progress",
            "days_total": total_days, "days": {}}
        if persist: _atomic_json(manifest_path, manifest)
    for index in range(total_days):
        day = (start+timedelta(days=index)).isoformat(); tomorrow = (start+timedelta(days=index+1)).isoformat()
        filename = f"day={day}.json"; checkpoint = destination / filename
        entry = manifest["days"].get(day) or {}
        saved = _read_json(checkpoint) if persist and entry.get("status") == "complete" else None
        if (saved is not None and saved.get("format_version") == REPLAY_VERSION and
                saved.get("input_signature") == signature and saved.get("operational_day") == day and
                entry.get("digest") == _day_digest(saved)):
            if progress_callback: progress_callback({"phase": "day_skipped", "day_index": index+1,
                "days_total": total_days, "operational_day": day, "orders_processed": saved.get("orders_total", 0)})
            continue
        if progress_callback: progress_callback({"phase": "day_started", "day_index": index+1, "days_total": total_days, "operational_day": day})
        result = _replay_day(day, load_effective_placement(day, model, registry=registry, root=root, strict=False),
            load_effective_rows("outbound", day, registry=registry, root=root, strict=False),
            load_effective_placement(tomorrow, model, registry=registry, root=root, strict=False), model, graph)
        result.update(format_version=REPLAY_VERSION, input_signature=signature)
        if persist:
            _atomic_json(checkpoint, result)
        manifest["days"][day] = {"status": "complete", "artifact": filename,
            "digest": _day_digest(result), "aggregates": _day_totals(result)}
        if persist:
            _atomic_json(manifest_path, manifest)
        if progress_callback: progress_callback({"phase": "day_completed", "day_index": index+1, "days_total": total_days,
                                                  "operational_day": day, "orders_processed": result["orders_total"]})
    aggregates = [entry["aggregates"] for entry in manifest["days"].values() if entry.get("status") == "complete"]
    strict_distance = round(sum(float(d.get("picker_distance_m") or 0) for d in aggregates), 6)
    orders_total = sum(int(d.get("orders_total") or 0) for d in aggregates)
    strict_orders = sum(int(d.get("orders_strict") or 0) for d in aggregates)
    full = len(aggregates) == total_days and all(d.get("status") == "ready" for d in aggregates)
    result = {**base, "format_version": REPLAY_VERSION, "full_month_fact_valid": full, "days_total": total_days,
        "days_completed": len(aggregates), "days_ready": sum(d.get("status") == "ready" for d in aggregates),
        "days_partial": sum(d.get("status") == "partial/non-strict" for d in aggregates),
        "days_blocked": sum(d.get("status") == "blocked" for d in aggregates), "orders_total": orders_total,
        "strict_orders": strict_orders, "requested_boxes": _display(sum(float(d.get("requested_boxes") or 0) for d in aggregates)),
        "picked_boxes": _display(sum(float(d.get("picked_boxes") or 0) for d in aggregates)),
        "shortage_boxes": _display(sum(float(d.get("shortage_boxes") or 0) for d in aggregates)),
        "fact_picker_distance_m": strict_distance if full else None, "strict_fact_picker_distance_m": strict_distance,
        "route_order_coverage": strict_orders/orders_total if orders_total else 1.0,
        "source_location_ambiguity_count": sum(int(d.get("source_location_ambiguity_count") or 0) for d in aggregates),
        "unresolved_fact_cell_count": sum(int(d.get("unroutable_cell_count") or 0) + int(d.get("missing_opening_sku_count") or 0) for d in aggregates),
        "daily_artifacts": [manifest["days"][day]["artifact"] for day in sorted(manifest["days"])], "blockers": []}
    if persist:
        _atomic_json(destination / "summary.json", result)
        manifest["status"] = "complete"; manifest["days_completed"] = len(aggregates)
        _atomic_json(manifest_path, manifest)
        result["artifact_path"] = str(destination)
    return result