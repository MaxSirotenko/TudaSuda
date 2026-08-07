"""Build a deterministic, auditable single-day warehouse event timeline."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

BOX_UNIT = "короб"
BOX_UNITS = {"короб", "короба", "коробов"}

LIMITATIONS = [
    "timeline_uses_individual_receipt_lines_not_full_day_receipt_batches",
    "receipt_lines_are_grouped_by_receipt_document",
    "receipt_document_timestamp_is_current_receipt_event_time_proxy",
    "outbound_event_time_uses_order_created_at",
    "operational_day_currently_uses_calendar_date_boundary",
    "date_only_events_do_not_define_intra_day_chronology",
    "naive_timestamps_do_not_receive_an_assumed_timezone",
    "intra_timestamp_event_order_is_not_business_authoritative",
    "same_timestamp_business_tie_policy_is_deferred_to_simulator",
    "timeline_does_not_modify_stock",
    "timeline_does_not_place_receipts",
    "timeline_does_not_execute_outbound_orders",
    "timeline_does_not_calculate_routes",
    "timeline_does_not_model_dynamic_occupancy",
    "timeline_does_not_model_pallet_release",
    "timeline_does_not_model_storage_vs_picking",
    "timeline_does_not_forecast",
    "timeline_is_single_day_single_warehouse",
    "timeline_is_pure_and_does_not_persist_state",
]


def _text(value: Any) -> str:
    return "" if value is None else re.sub(r"\s+", " ", str(value).strip())


def _warehouse(value: Any) -> str:
    return _text(value).casefold().replace("ё", "е")


def _hash(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _timestamp(value: Any) -> dict[str, Any] | None:
    """Parse without inventing a time or timezone; preserve source text."""
    raw = _text(value)
    if not raw:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
        return {"raw": raw, "date": parsed.date().isoformat(), "precision": "datetime", "value": parsed}
    if isinstance(value, dt.date):
        return {"raw": raw, "date": value.isoformat(), "precision": "date", "value": None}
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            parsed_date = dt.datetime.strptime(raw, fmt).date()
            return {"raw": raw, "date": parsed_date.isoformat(), "precision": "date", "value": None}
        except ValueError:
            pass
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                parsed = dt.datetime.strptime(raw, fmt)
                break
            except ValueError:
                pass
    if parsed is None:
        return None
    return {"raw": raw, "date": parsed.date().isoformat(), "precision": "datetime", "value": parsed}


def _date(value: Any) -> str | None:
    parsed = _timestamp(value)
    return None if parsed is None else parsed["date"]


def _receipt_dataset_id(state: Mapping[str, Any]) -> str:
    return _text(state.get("dataset_id") or state.get("receipt_dataset_id")) or _hash({
        "receipt_lines": sorted(
            ({
                "receipt_line_key": _text(x.get("receipt_line_key")),
                "document_key": _text(x.get("document_key")),
                "receipt_date": _text(x.get("receipt_date")),
                "normalized_warehouse": _warehouse(x.get("normalized_warehouse") or x.get("warehouse")),
                "sku_key": _text(x.get("sku_key")),
                "qty_units": x.get("qty_units"),
                "unit_name": _text(x.get("unit_name")),
            } for x in state.get("receipt_lines", []) if isinstance(x, Mapping)),
            key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True),
        )
    })


def _outbound_identity(state: Mapping[str, Any]) -> str:
    for key in ("outbound_demand_state_id", "outbound_demand_identity", "state_id", "dataset_id"):
        if _text(state.get(key)):
            return _text(state[key])
    orders = []
    for order in state.get("orders", []) if isinstance(state.get("orders", []), list) else []:
        if not isinstance(order, Mapping):
            continue
        demands = sorted(({
            "demand_key": _text(d.get("demand_key")),
            "requested_units": d.get("requested_units"),
            "unit_name": _text(d.get("unit_name")),
        } for d in order.get("demands", []) if isinstance(d, Mapping)), key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
        orders.append({"order_key": _text(order.get("order_key")), "created_at": _text(order.get("created_at")), "normalized_warehouse": _warehouse(order.get("normalized_warehouse") or order.get("warehouse")), "demands": demands})
    return _hash({"orders": sorted(orders, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))})


def _invalid(source_type: str, source_key: str, reason: str, value: Any = None) -> dict[str, Any]:
    item = {"invalid_event_id": _hash({"source_type": source_type, "source_key": source_key, "reason": reason, "value": _text(value)}), "source_type": source_type, "source_key": source_key, "reason": reason}
    if value is not None:
        item["timestamp"] = _text(value)
    return item


def _event_sort_key(event: Mapping[str, Any], mixed: bool) -> tuple[Any, ...]:
    parsed = _timestamp(event["occurred_at"])
    assert parsed is not None
    if parsed["precision"] == "date":
        primary: Any = (parsed["date"], 0, "")
    elif mixed:
        primary = (parsed["date"], 1, parsed["raw"])
    else:
        value = parsed["value"]
        ordered = value.astimezone(dt.timezone.utc).replace(tzinfo=None) if value.tzinfo else value
        primary = (ordered.date().isoformat(), 1, ordered.isoformat())
    return (primary, event["event_id"])


def build_warehouse_event_timeline(
    day_receipt_state: dict[str, Any],
    outbound_demand_state: dict[str, Any],
    *,
    operational_date: str | None = None,
    target_normalized_warehouse: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return timeline state and diagnostics without mutating either input."""
    receipt_state = day_receipt_state if isinstance(day_receipt_state, Mapping) else {}
    outbound_state = outbound_demand_state if isinstance(outbound_demand_state, Mapping) else {}
    configuration_errors: list[str] = []
    op_date = _date(operational_date if operational_date is not None else receipt_state.get("operational_date"))
    if op_date is None:
        configuration_errors.append("invalid_operational_date")
        op_date = _text(operational_date or receipt_state.get("operational_date"))
    target_source = target_normalized_warehouse
    if target_source is None:
        selected = receipt_state.get("selected_normalized_warehouses", [])
        if isinstance(selected, list) and len(selected) == 1:
            target_source = selected[0]
        else:
            target_source = receipt_state.get("target_normalized_warehouse") or receipt_state.get("normalized_warehouse")
    target = _warehouse(target_source)
    if not target:
        configuration_errors.append("target_normalized_warehouse_missing")

    receipt_id = _receipt_dataset_id(receipt_state)
    outbound_id = _outbound_identity(outbound_state)
    invalid_events: list[dict[str, Any]] = []
    excluded = defaultdict(int)
    blockers = list(configuration_errors)
    receipt_lines = receipt_state.get("receipt_lines", [])
    if not isinstance(receipt_lines, list):
        receipt_lines = []
        blockers.append("receipt_lines_not_list")
    documents: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    line_key_counts: dict[str, int] = defaultdict(int)
    for line in receipt_lines:
        if isinstance(line, Mapping) and _text(line.get("receipt_line_key")):
            line_key_counts[_text(line.get("receipt_line_key"))] += 1
    rejected_duplicate_lines: set[str] = set()
    for index, line in enumerate(receipt_lines):
        if not isinstance(line, Mapping):
            blockers.append("receipt_line_not_mapping")
            invalid_events.append(_invalid("receipt", f"index:{index}", "receipt_line_not_mapping"))
            continue
        line_key = _text(line.get("receipt_line_key"))
        doc_key = _text(line.get("document_key"))
        if not line_key or not doc_key:
            reason = "receipt_line_key_missing" if not line_key else "receipt_document_key_missing"
            blockers.append(reason)
            invalid_events.append(_invalid("receipt", line_key or f"index:{index}", reason))
            continue
        if line_key_counts[line_key] > 1:
            blockers.append("duplicate_receipt_line_key")
            if line_key not in rejected_duplicate_lines:
                invalid_events.append(_invalid("receipt", line_key, "duplicate_receipt_line_key"))
                rejected_duplicate_lines.add(line_key)
            continue
        documents[doc_key].append(line)

    events: list[dict[str, Any]] = []
    receipt_authoritative = 0
    for doc_key in sorted(documents):
        lines = documents[doc_key]
        warehouses = {_warehouse(x.get("normalized_warehouse") or x.get("warehouse")) for x in lines}
        timestamps = {_text(x.get("receipt_date")) for x in lines}
        if len(warehouses) != 1:
            blockers.append("receipt_document_warehouse_conflict")
            invalid_events.append(_invalid("receipt", doc_key, "receipt_document_warehouse_conflict"))
            continue
        if len(timestamps) != 1:
            blockers.append("receipt_document_timestamp_conflict")
            invalid_events.append(_invalid("receipt", doc_key, "receipt_document_timestamp_conflict"))
            continue
        warehouse = next(iter(warehouses))
        if warehouse != target:
            excluded["excluded_receipt_other_warehouse"] += 1
            continue
        raw_timestamp = next(iter(timestamps))
        parsed = _timestamp(raw_timestamp)
        if parsed is None:
            blockers.append("invalid_receipt_timestamp")
            invalid_events.append(_invalid("receipt", doc_key, "invalid_receipt_timestamp", raw_timestamp))
            continue
        if parsed["date"] != op_date:
            excluded["excluded_receipt_other_date"] += 1
            continue
        batches: dict[tuple[str, str], dict[str, Any]] = {}
        valid = True
        for line in lines:
            qty = line.get("qty_units")
            unit = _text(line.get("unit_name")).casefold()
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0 or unit not in BOX_UNITS:
                blockers.append("invalid_receipt_quantity_or_unit")
                invalid_events.append(_invalid("receipt", doc_key, "invalid_receipt_quantity_or_unit"))
                valid = False
                break
            key = (_text(line.get("sku_key")), BOX_UNIT)
            if not key[0]:
                blockers.append("receipt_sku_key_missing")
                invalid_events.append(_invalid("receipt", doc_key, "receipt_sku_key_missing"))
                valid = False
                break
            batch = batches.setdefault(key, {"sku_key": key[0], "qty_units": 0, "unit_name": BOX_UNIT, "source_receipt_line_keys": []})
            batch["qty_units"] += qty
            batch["source_receipt_line_keys"].append(_text(line["receipt_line_key"]))
        if not valid:
            continue
        batch_list = sorted(batches.values(), key=lambda x: (x["sku_key"], x["unit_name"]))
        for batch in batch_list:
            batch["source_receipt_line_keys"].sort()
        sorted_lines = sorted((copy.deepcopy(dict(x)) for x in lines), key=lambda x: _text(x.get("receipt_line_key")))
        total = sum(x["qty_units"] for x in batch_list)
        receipt_authoritative += sum(x["qty_units"] for x in lines)
        identity = {"receipt_dataset_id": receipt_id, "operational_date": op_date, "target_normalized_warehouse": target, "document_key": doc_key, "occurred_at": raw_timestamp, "receipt_line_keys": [x["receipt_line_key"] for x in sorted_lines], "receipt_batches": batch_list}
        events.append({"event_id": _hash(identity), "event_type": "receipt", "occurred_at": raw_timestamp, "event_date": parsed["date"], "time_precision": parsed["precision"], "normalized_warehouse": target, "source_key": doc_key, "document_key": doc_key, "receipt_number": _text(sorted_lines[0].get("receipt_number")), "receipt_lines": sorted_lines, "receipt_batches": batch_list, "qty_units": total, "unit_name": BOX_UNIT})

    orders = outbound_state.get("orders", [])
    if not isinstance(orders, list):
        orders = []
        blockers.append("outbound_orders_not_list")
    order_key_counts: dict[str, int] = defaultdict(int)
    for order in orders:
        if isinstance(order, Mapping) and _text(order.get("order_key")):
            order_key_counts[_text(order.get("order_key"))] += 1
    rejected_duplicate_orders: set[str] = set()
    outbound_authoritative = 0
    for index, order in enumerate(orders):
        if not isinstance(order, Mapping):
            blockers.append("outbound_order_not_mapping")
            invalid_events.append(_invalid("outbound_order", f"index:{index}", "outbound_order_not_mapping"))
            continue
        order_key = _text(order.get("order_key"))
        if not order_key:
            blockers.append("outbound_order_key_missing")
            invalid_events.append(_invalid("outbound_order", f"index:{index}", "outbound_order_key_missing"))
            continue
        if order_key_counts[order_key] > 1:
            blockers.append("duplicate_outbound_order_key")
            if order_key not in rejected_duplicate_orders:
                invalid_events.append(_invalid("outbound_order", order_key, "duplicate_outbound_order_key"))
                rejected_duplicate_orders.add(order_key)
            continue
        warehouse_raw = order.get("normalized_warehouse") or order.get("warehouse")
        warehouse = _warehouse(warehouse_raw)
        if not warehouse:
            excluded["excluded_outbound_missing_warehouse"] += 1
            continue
        if warehouse != target:
            excluded["excluded_outbound_other_warehouse"] += 1
            continue
        parsed = _timestamp(order.get("created_at"))
        if parsed is None:
            blockers.append("invalid_outbound_timestamp")
            invalid_events.append(_invalid("outbound_order", order_key, "invalid_outbound_timestamp", order.get("created_at")))
            continue
        if parsed["date"] != op_date:
            excluded["excluded_outbound_other_date"] += 1
            continue
        demands = sorted((copy.deepcopy(dict(d)) for d in order.get("demands", []) if isinstance(d, Mapping)), key=lambda x: (_text(x.get("demand_key")), _text(x.get("sku_key")), _text(x.get("unit_name"))))
        valid = all(not isinstance(d.get("requested_units"), bool) and isinstance(d.get("requested_units"), int) and d["requested_units"] > 0 for d in demands)
        units = {_text(d.get("unit_name")).casefold() for d in demands}
        if not valid or any(unit not in BOX_UNITS for unit in units):
            blockers.append("invalid_outbound_quantity_or_unit")
            invalid_events.append(_invalid("outbound_order", order_key, "invalid_outbound_quantity_or_unit"))
            continue
        total = sum(d["requested_units"] for d in demands)
        outbound_authoritative += total
        raw_timestamp = _text(order.get("created_at"))
        identity_demands = [{"demand_key": _text(d.get("demand_key")), "requested_units": d["requested_units"], "unit_name": _text(d.get("unit_name"))} for d in demands]
        identity = {"outbound_demand_identity": outbound_id, "operational_date": op_date, "target_normalized_warehouse": target, "order_key": order_key, "occurred_at": raw_timestamp, "demands": identity_demands}
        events.append({"event_id": _hash(identity), "event_type": "outbound_order", "occurred_at": raw_timestamp, "event_date": parsed["date"], "time_precision": parsed["precision"], "normalized_warehouse": target, "source_key": order_key, "order_key": order_key, "outbound_order_number": _text(order.get("outbound_order_number")), "created_at": raw_timestamp, "warehouse": copy.deepcopy(order.get("warehouse")), "source_indexes": sorted(copy.deepcopy(order.get("source_indexes", []))), "demands": demands, "requested_units": total, "unit_name": BOX_UNIT})

    awareness = {(_timestamp(e["occurred_at"])["value"].tzinfo is not None) for e in events if e["time_precision"] == "datetime"}
    mixed = len(awareness) > 1
    timezone_mode = "mixed" if mixed else ("aware" if awareness == {True} else "naive" if awareness == {False} else "none")
    if mixed:
        blockers.append("mixed_timezone_awareness")
    events.sort(key=lambda e: _event_sort_key(e, mixed))
    if len({e["event_id"] for e in events}) != len(events):
        blockers.append("duplicate_event_identity")
    for index, event in enumerate(events, 1):
        event["deterministic_index"] = index

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        parsed = _timestamp(event["occurred_at"])
        assert parsed is not None
        if parsed["precision"] == "datetime" and parsed["value"].tzinfo is not None and not mixed:
            key = ("datetime", parsed["value"].astimezone(dt.timezone.utc).isoformat())
        else:
            key = (parsed["precision"], parsed["raw"] if parsed["precision"] == "datetime" else parsed["date"])
        grouped[key].append(event)
    time_groups = []
    for _, group_events in sorted(grouped.items(), key=lambda item: _event_sort_key(item[1][0], mixed)):
        group_events.sort(key=lambda e: e["event_id"])
        types = {e["event_type"] for e in group_events}
        tie = types == {"receipt", "outbound_order"}
        gid = _hash({"occurred_at": [e["occurred_at"] for e in group_events], "event_ids": [e["event_id"] for e in group_events]})
        time_groups.append({"time_group_id": gid, "occurred_at": group_events[0]["occurred_at"], "time_precision": group_events[0]["time_precision"], "events": copy.deepcopy(group_events), "event_count": len(group_events), "contains_receipt": "receipt" in types, "contains_outbound_order": "outbound_order" in types, "requires_tie_policy": tie})

    receipt_total = sum(e["qty_units"] for e in events if e["event_type"] == "receipt")
    outbound_total = sum(e["requested_units"] for e in events if e["event_type"] == "outbound_order")
    receipt_conservation = receipt_authoritative == receipt_total
    outbound_conservation = outbound_authoritative == outbound_total
    if not receipt_conservation:
        blockers.append("receipt_quantity_conservation_failed")
    if not outbound_conservation:
        blockers.append("outbound_quantity_conservation_failed")
    date_only = [e for e in events if e["time_precision"] == "date"]
    if date_only:
        blockers.append("events_without_time_of_day")
    cross_groups = sum(g["requires_tie_policy"] for g in time_groups)
    strict = not blockers
    requires_policy = cross_groups > 0
    invalid_events.sort(key=lambda x: x["invalid_event_id"])
    summary = {
        "receipt_events": sum(e["event_type"] == "receipt" for e in events), "outbound_events": sum(e["event_type"] == "outbound_order" for e in events), "total_events": len(events), "time_groups": len(time_groups),
        "simultaneous_groups": sum(g["event_count"] > 1 for g in time_groups), "simultaneous_cross_type_groups": cross_groups, "receipt_qty_units": receipt_total, "outbound_requested_units": outbound_total,
        "unique_receipt_documents": len({e["document_key"] for e in events if e["event_type"] == "receipt"}), "unique_outbound_orders": len({e["order_key"] for e in events if e["event_type"] == "outbound_order"}),
        "first_event_at": events[0]["occurred_at"] if events else None, "last_event_at": events[-1]["occurred_at"] if events else None, "events_with_datetime_precision": len(events) - len(date_only), "events_without_time_of_day": len(date_only),
        "receipt_events_date_only": sum(e["event_type"] == "receipt" for e in date_only), "outbound_events_date_only": sum(e["event_type"] == "outbound_order" for e in date_only), "invalid_timestamp_events": sum(x["reason"] in {"invalid_receipt_timestamp", "invalid_outbound_timestamp"} for x in invalid_events),
        **{key: excluded[key] for key in ("excluded_receipt_other_date", "excluded_receipt_other_warehouse", "excluded_outbound_other_date", "excluded_outbound_other_warehouse", "excluded_outbound_missing_warehouse")},
        "timezone_mode": timezone_mode, "strict_chronology_ready": strict, "requires_same_timestamp_policy": requires_policy, "receipt_quantity_conservation_ok": receipt_conservation, "outbound_quantity_conservation_ok": outbound_conservation,
    }
    state_core = {"receipt_dataset_id": receipt_id, "outbound_demand_identity": outbound_id, "operational_date": op_date, "target_normalized_warehouse": target, "strict_chronology_ready": strict, "requires_same_timestamp_policy": requires_policy, "events": events, "time_groups": time_groups, "invalid_events": invalid_events, "summary": summary, "limitations": list(LIMITATIONS)}
    state_id = _hash({"receipt_dataset_id": receipt_id, "outbound_demand_identity": outbound_id, "operational_date": op_date, "target_normalized_warehouse": target, "time_group_ids": [g["time_group_id"] for g in time_groups], "invalid_events": invalid_events, "strict_chronology_ready": strict, "requires_same_timestamp_policy": requires_policy})
    state = {"event_timeline_state_id": state_id, **state_core}
    diagnostics = {**summary, "blocking_reasons": sorted(set(blockers)), "configuration_errors": configuration_errors, "invalid_events": copy.deepcopy(invalid_events)}
    return state, diagnostics
