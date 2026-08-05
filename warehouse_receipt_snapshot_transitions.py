from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from warehouse_inventory_placement import cell_key as build_cell_key

BOX_UNITS = {"короб", "короба", "коробов"}
LIMITATIONS = [
    "snapshot_comparison_is_location_evidence_not_movement_history",
    "placement_quantities_are_not_used",
    "same_sku_replenishment_in_same_cell_is_not_observable",
    "receipt_date_is_not_physical_completion_time",
    "cross_warehouse_location_matching_is_not_allowed",
    "receipt_quantity_is_not_allocated_to_candidate_cells",
]
TRANSITION_TYPES = [
    "newly_occupied_single_sku", "newly_occupied_mixed_sku", "emptied",
    "unchanged_single_sku", "replaced_single_sku", "unchanged_mixed_sku", "changed_mixed_sku",
]
EMPTY_HASH = "sha256:" + hashlib.sha256(b"{}").hexdigest()


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _filled(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _norm_wh(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).strip()).casefold().replace("ё", "е") if _filled(value) else ""


def _uniq(values: list[Any] | set[Any]) -> list[Any]:
    return sorted({v for v in values if _filled(v)}, key=lambda x: str(x))


def _empty_diags() -> dict[str, Any]:
    d = {
        "model_cells": 0, "duplicate_model_cell_keys": 0,
        "source_receipt_batches": 0, "valid_receipt_batches": 0, "invalid_receipt_batches": 0, "receipt_boxes": 0,
        "source_start_placements": 0, "valid_start_snapshot_evidence": 0, "invalid_start_snapshot_evidence": 0,
        "duplicate_start_snapshot_evidence": 0, "start_excluded_inventory": 0, "start_unmatched_inventory": 0, "start_unplaced_inventory": 0,
        "source_end_placements": 0, "valid_end_snapshot_evidence": 0, "invalid_end_snapshot_evidence": 0,
        "duplicate_end_snapshot_evidence": 0, "end_excluded_inventory": 0, "end_unmatched_inventory": 0, "end_unplaced_inventory": 0,
        "selected_normalized_warehouses": [], "cell_transitions": 0,
        "receipt_cell_candidates": 0, "strong_snapshot_delta_candidates": 0, "ambiguous_snapshot_delta_candidates": 0,
        "reused_physical_cells": 0, "virtual_slot_requirements": 0,
        "receipt_batches_no_end_snapshot_evidence": 0, "receipt_batches_persistent_same_sku_only": 0,
        "receipt_batches_new_location_evidence": 0, "receipt_batches_mixed_persistent_and_new_evidence": 0,
        "receipt_batches_with_other_warehouse_end_evidence": 0, "non_receipt_added_skus": [], "configuration_errors": [],
        "placement_quantity_is_authoritative": False, "same_sku_replenishment_in_same_cell_is_observable": False,
    }
    for t in TRANSITION_TYPES:
        d[t] = 0
    return d


def _empty_state(dataset_id: Any = None, operational_date: Any = None) -> dict[str, Any]:
    return {
        "analysis_id": EMPTY_HASH, "dataset_id": dataset_id, "operational_date": operational_date,
        "start_snapshot_id": EMPTY_HASH, "end_snapshot_id": EMPTY_HASH,
        "placement_quantity_is_authoritative": False,
        "receipt_batches": [], "cell_transitions": [], "receipt_cell_candidates": [],
        "virtual_slot_requirements": [], "receipt_batch_evidence": [],
        "invalid_receipt_batches": [], "invalid_start_snapshot_evidence": [], "invalid_end_snapshot_evidence": [],
        "limitations": list(LIMITATIONS),
    }


def _scenario_inputs_equal(state: Mapping[str, Any], dataset_id: Any) -> bool:
    si = state.get("scenario_inputs") if isinstance(state.get("scenario_inputs"), Mapping) else {}
    cur = si.get("current") if isinstance(si.get("current"), Mapping) else None
    pro = si.get("proposed") if isinstance(si.get("proposed"), Mapping) else None
    if cur is None or pro is None:
        return False
    return (cur.get("receipt_dataset_id") == pro.get("receipt_dataset_id") == dataset_id
            and cur.get("total_boxes") == pro.get("total_boxes")
            and sorted(cur.get("receipt_batch_keys") or []) == sorted(pro.get("receipt_batch_keys") or []))


def _model_cells(model: Mapping[str, Any], diags: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cells = {}
    for idx, cell in enumerate(model.get("cells") or []):
        if not isinstance(cell, Mapping):
            continue
        key = str(cell.get("cell_key") or build_cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier")))
        if key in cells:
            diags["duplicate_model_cell_keys"] += 1
            continue
        row = dict(cell)
        row.setdefault("cell_key", key)
        row["physical_order"] = cell.get("physical_index", cell.get("x", idx))
        cells[key] = row
    diags["model_cells"] = len(cells)
    return cells


def _receipt_batches(state: Mapping[str, Any], diags: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid, invalid, seen = [], [], set()
    batches = state.get("receipt_sku_batches") or []
    diags["source_receipt_batches"] = len(batches) if isinstance(batches, list) else 0
    for i, b in enumerate(batches if isinstance(batches, list) else []):
        if not isinstance(b, Mapping):
            invalid.append({"source_index": i, "reason": "receipt_batch_not_mapping"}); continue
        key = b.get("receipt_batch_key")
        reason = None
        if not _filled(key): reason = "receipt_batch_key_missing"
        elif key in seen: reason = "duplicate_receipt_batch_key"
        elif not _filled(b.get("normalized_warehouse")): reason = "receipt_batch_warehouse_missing"
        elif not _filled(b.get("sku_key")): reason = "receipt_batch_sku_missing"
        elif "qty_units" not in b: reason = "receipt_batch_qty_missing"
        elif isinstance(b.get("qty_units"), bool) or not isinstance(b.get("qty_units"), int): reason = "receipt_batch_qty_invalid"
        elif b.get("qty_units") <= 0: reason = "receipt_batch_qty_non_positive"
        elif str(b.get("unit_name", "")).strip().casefold() not in BOX_UNITS: reason = "receipt_batch_unit_not_boxes"
        if reason:
            invalid.append({"source_index": i, "receipt_batch_key": key, "reason": reason}); continue
        seen.add(key)
        valid.append({"receipt_batch_key": key, "dataset_id": state.get("dataset_id"), "operational_date": b.get("operational_date", state.get("operational_date")), "warehouse": b.get("warehouse"), "normalized_warehouse": _norm_wh(b.get("normalized_warehouse")), "sku_key": b.get("sku_key"), "qty_units": b.get("qty_units"), "unit_name": "короб"})
    valid.sort(key=lambda x: (x["normalized_warehouse"], x["sku_key"], x["receipt_batch_key"]))
    diags.update(valid_receipt_batches=len(valid), invalid_receipt_batches=len(invalid), receipt_boxes=sum(b["qty_units"] for b in valid))
    return valid, invalid


def _snapshot(state: Mapping[str, Any], cells: dict[str, dict[str, Any]], prefix: str, diags: dict[str, Any]):
    evidence, occ, invalid, dup = {}, {}, [], 0
    placements = state.get("placements") or []
    diags[f"source_{prefix}_placements"] = len(placements) if isinstance(placements, list) else 0
    for name in ("excluded_inventory", "unmatched_inventory", "unplaced_inventory"):
        diags[f"{prefix}_{name}"] = len(state.get(name) or []) if isinstance(state.get(name) or [], list) else 0
    for i, p in enumerate(placements if isinstance(placements, list) else []):
        if not isinstance(p, Mapping): invalid.append({"source_index": i, "reason": "snapshot_placement_not_mapping"}); continue
        reason = None
        if not _filled(p.get("placement_id")): reason = "placement_id_missing"
        elif not _filled(p.get("warehouse")): reason = "placement_warehouse_missing"
        elif not _filled(p.get("cell_key")): reason = "placement_cell_key_missing"
        elif not _filled(p.get("sku_key")): reason = "placement_sku_missing"
        elif p.get("cell_key") not in cells: reason = "unknown_model_cell"
        wh = _norm_wh(p.get("warehouse"))
        if not reason and not wh: reason = "placement_warehouse_missing"
        if reason:
            invalid.append({"source_index": i, "placement_id": p.get("placement_id"), "reason": reason}); continue
        key = (wh, p.get("cell_key"), p.get("sku_key"))
        if key not in evidence:
            evidence[key] = {"normalized_warehouse": wh, "warehouse": p.get("warehouse"), "cell_key": p.get("cell_key"), "sku_key": p.get("sku_key"), "production_dates": [], "placement_ids": []}
        else:
            dup += 1
        evidence[key]["production_dates"] = _uniq(evidence[key]["production_dates"] + [p.get("production_date")])
        evidence[key]["placement_ids"] = _uniq(evidence[key]["placement_ids"] + [p.get("placement_id")])
    for e in evidence.values():
        o = occ.setdefault((e["normalized_warehouse"], e["cell_key"]), {"warehouse": e["warehouse"], "sku_keys": set(), "placement_ids": [], "production_dates_by_sku": {}})
        o["sku_keys"].add(e["sku_key"]); o["placement_ids"] = _uniq(o["placement_ids"] + e["placement_ids"]); o["production_dates_by_sku"][e["sku_key"]] = e["production_dates"]
    sid = _hash(sorted(({"normalized_warehouse": e["normalized_warehouse"], "cell_key": e["cell_key"], "sku_key": e["sku_key"], "production_dates": e["production_dates"]} for e in evidence.values()), key=_canon))
    diags[f"valid_{prefix}_snapshot_evidence"] = len(evidence); diags[f"invalid_{prefix}_snapshot_evidence"] = len(invalid); diags[f"duplicate_{prefix}_snapshot_evidence"] = dup
    return evidence, occ, invalid, sid


def _tt(start: list[str], end: list[str]) -> str:
    if not start and len(end) == 1: return "newly_occupied_single_sku"
    if not start and len(end) > 1: return "newly_occupied_mixed_sku"
    if start and not end: return "emptied"
    if len(start) == len(end) == 1 and start == end: return "unchanged_single_sku"
    if len(start) == len(end) == 1 and start != end: return "replaced_single_sku"
    if len(start) > 1 and start == end: return "unchanged_mixed_sku"
    return "changed_mixed_sku"


def analyze_receipt_snapshot_transitions(model: dict[str, Any], day_receipt_state: dict[str, Any], start_placement_state: dict[str, Any], end_placement_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    diags = _empty_diags(); dataset_id = day_receipt_state.get("dataset_id"); opdate = day_receipt_state.get("operational_date")
    if not _scenario_inputs_equal(day_receipt_state, dataset_id):
        diags["configuration_errors"] = ["receipt_scenario_inputs_not_equal"]
        return _empty_state(dataset_id, opdate), diags
    cells = _model_cells(model, diags)
    batches, invalid_batches = _receipt_batches(day_receipt_state, diags)
    selected = _uniq({b["normalized_warehouse"] for b in batches}); diags["selected_normalized_warehouses"] = selected
    by_ws: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for b in batches: by_ws.setdefault((b["normalized_warehouse"], b["sku_key"]), []).append(b)
    start_ev, start_occ, invalid_start, start_id = _snapshot(start_placement_state, cells, "start", diags)
    end_ev, end_occ, invalid_end, end_id = _snapshot(end_placement_state, cells, "end", diags)
    transitions = []
    for wh, ck in sorted((set(start_occ) | set(end_occ))):
        if wh not in selected: continue
        s = _uniq(start_occ.get((wh, ck), {}).get("sku_keys", set())); e = _uniq(end_occ.get((wh, ck), {}).get("sku_keys", set()))
        added = _uniq(set(e) - set(s)); removed = _uniq(set(s) - set(e)); persist = _uniq(set(s) & set(e))
        receipt_added = _uniq([sku for sku in added if (wh, sku) in by_ws]); non_receipt = _uniq(set(added) - set(receipt_added))
        batch_keys = _uniq([b["receipt_batch_key"] for sku in receipt_added for b in by_ws[(wh, sku)]])
        c = cells[ck]; ttype = _tt(s, e); tkey = _hash([start_id, end_id, wh, ck, s, e])
        tr = {"transition_key": tkey, "normalized_warehouse": wh, "warehouse": (start_occ.get((wh, ck)) or end_occ.get((wh, ck)) or {}).get("warehouse"), "cell_key": ck,
              "row_number": c.get("row_number"), "cell_number": c.get("cell_number"), "tier": c.get("tier"), "weight_zone": c.get("weight_zone"),
              "start_sku_keys": s, "end_sku_keys": e, "added_sku_keys": added, "removed_sku_keys": removed, "persistent_sku_keys": persist,
              "transition_type": ttype, "receipt_added_sku_keys": receipt_added, "non_receipt_added_sku_keys": non_receipt, "candidate_receipt_batch_keys": batch_keys,
              "requires_virtual_slot": bool(s and receipt_added), "virtual_slot_count_hint": len(receipt_added), "ambiguous": ttype.endswith("mixed_sku"), "placement_quantity_is_authoritative": False,
              "_order": c.get("physical_order")}
        transitions.append(tr); diags[ttype] += 1
    transitions.sort(key=lambda x: (x["normalized_warehouse"], x["_order"], x.get("row_number"), x.get("cell_number"), x.get("tier"), x["cell_key"], x["transition_key"]))
    candidates, reqs = [], []
    for tr in transitions:
        for sku in tr["receipt_added_sku_keys"]:
            for b in by_ws[(tr["normalized_warehouse"], sku)]:
                strong = len(tr["end_sku_keys"]) == 1 and (not tr["start_sku_keys"] or (len(tr["start_sku_keys"]) == 1 and sku not in tr["start_sku_keys"]))
                cand_key = _hash([b["receipt_batch_key"], tr["transition_key"], tr["cell_key"]])
                cand = {"candidate_key": cand_key, "receipt_batch_key": b["receipt_batch_key"], "dataset_id": dataset_id, "normalized_warehouse": b["normalized_warehouse"], "warehouse": b["warehouse"], "sku_key": sku, "receipt_qty_units": b["qty_units"], "unit_name": "короб", "physical_cell_key": tr["cell_key"], "row_number": tr["row_number"], "cell_number": tr["cell_number"], "tier": tr["tier"], "transition_key": tr["transition_key"], "transition_type": tr["transition_type"], "candidate_type": "reused_physical_cell" if tr["start_sku_keys"] else "newly_occupied_physical_cell", "start_sku_keys": tr["start_sku_keys"], "end_sku_keys": tr["end_sku_keys"], "confidence": "strong_snapshot_delta" if strong else "ambiguous_snapshot_delta", "requires_virtual_slot": tr["requires_virtual_slot"], "placement_quantity_is_authoritative": False, "_order": tr["_order"]}
                candidates.append(cand)
                if cand["requires_virtual_slot"]:
                    rkey = _hash([dataset_id, start_id, end_id, b["receipt_batch_key"], b["normalized_warehouse"], tr["cell_key"]])
                    reqs.append({"requirement_key": rkey, "dataset_id": dataset_id, "start_snapshot_id": start_id, "end_snapshot_id": end_id, "receipt_batch_key": b["receipt_batch_key"], "normalized_warehouse": b["normalized_warehouse"], "warehouse": b["warehouse"], "sku_key": sku, "physical_cell_key": tr["cell_key"], "row_number": tr["row_number"], "cell_number": tr["cell_number"], "tier": tr["tier"], "start_sku_keys": tr["start_sku_keys"], "end_sku_keys": tr["end_sku_keys"], "reason": "physical_cell_reused_between_snapshots", "virtual_slot_count_hint": 1, "quantity_allocation_pending": True, "_order": tr["_order"]})
    candidates.sort(key=lambda x: (x["normalized_warehouse"], x["sku_key"], x["_order"], x["physical_cell_key"], x["candidate_key"]))
    reqs.sort(key=lambda x: (x["normalized_warehouse"], x["_order"], x["physical_cell_key"], x["sku_key"], x["requirement_key"]))
    evidence = []
    for b in batches:
        wh, sku = b["normalized_warehouse"], b["sku_key"]
        sc = _uniq([ck for w, ck, s in start_ev if w == wh and s == sku]); ec = _uniq([ck for w, ck, s in end_ev if w == wh and s == sku])
        pc = _uniq(set(sc) & set(ec)); nc = _uniq(set(ec) - set(sc))
        b_cands = [c for c in candidates if c["receipt_batch_key"] == b["receipt_batch_key"]]
        other = _uniq([ck for w, ck, s in end_ev if w != wh and s == sku])
        if not ec: status = "no_end_snapshot_evidence"
        elif not nc and pc: status = "persistent_same_sku_only"
        elif nc and not pc: status = "new_location_evidence"
        elif nc and pc: status = "mixed_persistent_and_new_evidence"
        else: status = "no_location_evidence"
        evidence.append({**b, "start_cell_keys": sc, "end_cell_keys": ec, "persistent_cell_keys": pc, "newly_observed_cell_keys": nc, "newly_occupied_cell_keys": _uniq([c["physical_cell_key"] for c in b_cands if c["candidate_type"] == "newly_occupied_physical_cell"]), "reused_physical_cell_keys": _uniq([c["physical_cell_key"] for c in b_cands if c["candidate_type"] == "reused_physical_cell"]), "ambiguous_candidate_cell_keys": _uniq([c["physical_cell_key"] for c in b_cands if c["confidence"] == "ambiguous_snapshot_delta"]), "other_warehouse_end_cell_keys": other, "receipt_cell_candidate_keys": _uniq([c["candidate_key"] for c in b_cands]), "virtual_slot_requirement_keys": _uniq([r["requirement_key"] for r in reqs if r["receipt_batch_key"] == b["receipt_batch_key"]]), "evidence_status": status, "placement_quantity_is_authoritative": False, "same_sku_replenishment_in_same_cell_is_observable": False})
    analysis_id = _hash([dataset_id, start_id, end_id, [b["receipt_batch_key"] for b in batches], [t["transition_key"] for t in transitions], [c["candidate_key"] for c in candidates], [r["requirement_key"] for r in reqs]])
    for lst in (transitions, candidates, reqs):
        for x in lst: x.pop("_order", None)
    state = {"analysis_id": analysis_id, "dataset_id": dataset_id, "operational_date": opdate, "start_snapshot_id": start_id, "end_snapshot_id": end_id, "placement_quantity_is_authoritative": False, "receipt_batches": batches, "cell_transitions": transitions, "receipt_cell_candidates": candidates, "virtual_slot_requirements": reqs, "receipt_batch_evidence": evidence, "invalid_receipt_batches": invalid_batches, "invalid_start_snapshot_evidence": invalid_start, "invalid_end_snapshot_evidence": invalid_end, "limitations": list(LIMITATIONS)}
    diags.update(cell_transitions=len(transitions), receipt_cell_candidates=len(candidates), strong_snapshot_delta_candidates=sum(c["confidence"] == "strong_snapshot_delta" for c in candidates), ambiguous_snapshot_delta_candidates=sum(c["confidence"] == "ambiguous_snapshot_delta" for c in candidates), reused_physical_cells=len(_uniq([c["physical_cell_key"] for c in candidates if c["candidate_type"] == "reused_physical_cell"])), virtual_slot_requirements=len(reqs), non_receipt_added_skus=_uniq([sku for t in transitions for sku in t["non_receipt_added_sku_keys"]]))
    for e in evidence:
        diags[f"receipt_batches_{e['evidence_status']}"] = diags.get(f"receipt_batches_{e['evidence_status']}", 0) + 1
        if e["other_warehouse_end_cell_keys"]: diags["receipt_batches_with_other_warehouse_end_evidence"] += 1
    return state, diags
