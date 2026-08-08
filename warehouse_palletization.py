"""Deterministic master-data palletization contracts for receipt events.

``reported_pallets`` is deliberately absent: it is observed receipt data, not
an authority for the reusable SKU -> boxes-per-pallet rule.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from warehouse_business_identity import canonical_sku_key, validate_box_quantity


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _valid_sku(row: Mapping[str, Any]) -> str | None:
    key = _text(row.get("sku_key"))
    if not key.startswith("sku:v2:"):
        return None
    derived = canonical_sku_key(row)
    return key if not derived or derived == key else None


def build_palletization_rule_state(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and deterministically deduplicate SKU palletization rules."""
    diagnostics: dict[str, Any] = {
        "invalid_palletization_rule": 0,
        "conflicting_palletization_rule": 0,
        "duplicate_identical_rules": 0,
        "invalid_rows": [],
        "conflicts": [],
    }
    candidates: dict[str, dict[tuple[int, str], dict[str, Any]]] = defaultdict(dict)
    raw_count = len(rows) if isinstance(rows, list) else 0
    if not isinstance(rows, list):
        rows = []
        diagnostics["invalid_palletization_rule"] += 1
        diagnostics["invalid_rows"].append({"row_index": None, "reason": "invalid_rule_rows"})
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            diagnostics["invalid_palletization_rule"] += 1
            diagnostics["invalid_rows"].append({"row_index": index, "reason": "invalid_palletization_rule"})
            continue
        sku_key = _valid_sku(raw)
        quantity, error = validate_box_quantity(raw.get("boxes_per_pallet"), positive=True)
        source = _text(raw.get("source"))
        if not sku_key or error or not source:
            diagnostics["invalid_palletization_rule"] += 1
            diagnostics["invalid_rows"].append({"row_index": index, "reason": "invalid_palletization_rule"})
            continue
        identity = {"sku_key": sku_key, "boxes_per_pallet": quantity, "source": source}
        key = (quantity, source)
        if key in candidates[sku_key]:
            diagnostics["duplicate_identical_rules"] += 1
        candidates[sku_key][key] = {"rule_id": _id(identity), **identity}

    rules: list[dict[str, Any]] = []
    conflicting: list[str] = []
    for sku_key in sorted(candidates):
        values = sorted({key[0] for key in candidates[sku_key]})
        if len(values) != 1:
            conflicting.append(sku_key)
            diagnostics["conflicting_palletization_rule"] += 1
            diagnostics["conflicts"].append({
                "reason": "conflicting_palletization_rule", "sku_key": sku_key,
                "boxes_per_pallet_values": values,
            })
            continue
        # Multiple sources agreeing on the same value are identical business
        # authority; use the lexicographically smallest canonical source.
        rules.append(sorted(candidates[sku_key].values(), key=lambda item: item["rule_id"])[0])
    rules.sort(key=lambda item: (item["sku_key"], item["rule_id"]))
    state = {
        "palletization_rule_state_id": "",
        "rules": rules,
        "conflicting_skus": conflicting,
        "summary": {
            "input_rows": raw_count,
            "valid_rules": len(rules),
            "conflicting_skus": len(conflicting),
            "invalid_rows": diagnostics["invalid_palletization_rule"],
        },
    }
    state["palletization_rule_state_id"] = _id({key: state[key] for key in ("rules", "conflicting_skus")})
    return state, diagnostics


def palletize_boxes(sku_key: str, qty_boxes: int, boxes_per_pallet: int) -> list[dict[str, Any]]:
    """Split exact boxes into full pallets and one real-size partial pallet."""
    if not _text(sku_key).startswith("sku:v2:"):
        raise ValueError("invalid_sku_key")
    quantity, quantity_error = validate_box_quantity(qty_boxes, positive=True)
    capacity, capacity_error = validate_box_quantity(boxes_per_pallet, positive=True)
    if quantity_error:
        raise ValueError("invalid_qty_boxes")
    if capacity_error:
        raise ValueError("invalid_boxes_per_pallet")
    result = []
    remaining = quantity
    sequence = 1
    while remaining:
        boxes = min(remaining, capacity)
        result.append({
            "pallet_sequence": sequence,
            "qty_boxes": boxes,
            "capacity_boxes": capacity,
            "fill_ratio": boxes / capacity,
            "is_partial": boxes < capacity,
        })
        remaining -= boxes
        sequence += 1
    return result


def palletize_receipt_event(
    event: Mapping[str, Any], palletization_rule_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Build stable pallet units independently for every receipt batch."""
    rules = {rule.get("sku_key"): rule for rule in palletization_rule_state.get("rules", []) or []}
    conflicts = set(palletization_rule_state.get("conflicting_skus", []) or [])
    units: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    batches = sorted(
        (dict(batch) for batch in event.get("receipt_batches", []) or [] if isinstance(batch, Mapping)),
        key=lambda batch: (batch.get("sku_key", ""), _json(batch)),
    )
    for batch_index, batch in enumerate(batches, 1):
        sku_key = _text(batch.get("sku_key"))
        quantity, error = validate_box_quantity(batch.get("qty_units"), positive=True)
        if error:
            unresolved.append({"sku_key": sku_key, "qty_boxes": batch.get("qty_units"), "reason": "invalid_receipt_batch"})
            continue
        reason = "palletization_rule_conflict" if sku_key in conflicts else "palletization_rule_missing"
        rule = rules.get(sku_key)
        if rule is None:
            unresolved.append({"sku_key": sku_key, "qty_boxes": quantity, "reason": reason})
            continue
        line_keys = sorted({_text(value) for value in
                            (batch.get("source_receipt_line_keys") or batch.get("receipt_line_keys") or []) if _text(value)})
        for chunk in palletize_boxes(sku_key, quantity, rule["boxes_per_pallet"]):
            identity = {
                "source_event_id": _text(event.get("event_id")), "sku_key": sku_key,
                "receipt_batch_sequence": batch_index,
                "pallet_sequence": chunk["pallet_sequence"], "source_receipt_line_keys": line_keys,
                "boxes_per_pallet": rule["boxes_per_pallet"],
            }
            units.append({
                "pallet_unit_id": _id(identity), "sku_key": sku_key,
                "source_event_id": _text(event.get("event_id")), "source_type": "receipt",
                "source_receipt_line_keys": line_keys,
                "capacity_boxes": chunk["capacity_boxes"], "initial_boxes": chunk["qty_boxes"],
                "remaining_boxes": chunk["qty_boxes"], "is_partial": chunk["is_partial"],
                "position_id": None, "cell_key": None, "location_status": "unassigned",
                "physical_status": "active", "pallet_sequence": chunk["pallet_sequence"],
            })
    units.sort(key=lambda unit: unit["pallet_unit_id"])
    unresolved.sort(key=lambda item: (item.get("sku_key", ""), item.get("reason", ""), _json(item)))
    return {
        "source_event_id": _text(event.get("event_id")), "pallet_units": units,
        "unresolved_batches": unresolved,
        "palletization_status": "unresolved" if unresolved else "palletized",
    }
