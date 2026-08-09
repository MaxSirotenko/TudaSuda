"""Leakage-safe, deterministic 28/14/7/4 SKU velocity profiles."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from warehouse_business_identity import build_canonical_sku_identity, normalize_warehouse

VELOCITY_PROFILE_VERSION = 1
LIMITATIONS = (
    "rank_1_precedes_overlapping_rank_2_when_days_4_is_1",
    "velocity_priority_uses_gate_distance_not_global_route_optimization",
)


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _classification(d28: int, d14: int, d7: int, d4: int) -> tuple[int | None, str]:
    # Deliberate first-match precedence: the historical definitions overlap at d4=1.
    if 20 <= d28 <= 28 and 10 <= d14 <= 14 and 5 <= d7 <= 7 and d4 >= 1:
        return 1, "confirmed_core"
    if 20 <= d28 <= 28 and 10 <= d14 <= 14 and 5 <= d7 <= 7 and 0 <= d4 <= 1:
        return 2, "weaker_core"
    if 14 <= d28 <= 19 and 7 <= d14 <= 9 and 3 <= d7 <= 4 and d4 >= 1:
        return 3, "stable"
    if 8 <= d28 <= 13 and d7 >= 1:
        return 4, "regular"
    if 4 <= d28 <= 7:
        return 5, "periodic"
    if 1 <= d28 <= 3:
        return 6, "tail"
    return None, "no_history"


def build_sku_velocity_profile(
    outbound_rows: list[dict[str, Any]], *, as_of_date: str,
    target_normalized_warehouse: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a profile from distinct active days in ``[D-28, D)`` only."""
    end = _day(as_of_date)
    if end is None:
        empty = {"velocity_profile_version": VELOCITY_PROFILE_VERSION,
                 "velocity_profile_id": None, "rows": [], "summary": {}, "limitations": []}
        return empty, {"valid": False, "errors": [{"code": "invalid_as_of_date"}], "warnings": []}
    start = end - timedelta(days=28)
    target = normalize_warehouse(target_normalized_warehouse)
    active: dict[str, set[date]] = defaultdict(set)
    evidence: dict[str, dict[str, str]] = {}
    source_dates: set[date] = set()
    accepted = 0
    for raw in outbound_rows if isinstance(outbound_rows, list) else []:
        if not isinstance(raw, Mapping) or normalize_warehouse(raw.get("normalized_warehouse") or raw.get("warehouse")) != target:
            continue
        identity = build_canonical_sku_identity(raw)
        sku, event_day = identity["sku_key"], _day(raw.get("created_at") or raw.get("event_date"))
        if not sku:
            continue
        evidence.setdefault(sku, {"nomenclature": identity["nomenclature"], "characteristic": identity["characteristic"]})
        if event_day is not None and start <= event_day < end:
            active[sku].add(event_day)
            source_dates.add(event_day)
            accepted += 1
    rows = []
    for sku in sorted(evidence):
        dates = active[sku]
        counts = {window: sum(day >= end - timedelta(days=window) for day in dates) for window in (28, 14, 7, 4)}
        rank, classification = _classification(counts[28], counts[14], counts[7], counts[4])
        rows.append({"sku_key": sku, "days_28": counts[28], "days_14": counts[14],
                     "days_7": counts[7], "days_4": counts[4], "velocity_rank": rank,
                     "velocity_class": classification, **evidence[sku]})
    rank_counts = Counter(row["velocity_rank"] for row in rows)
    complete = len(source_dates) == 28
    limitations = list(LIMITATIONS)
    if not complete:
        limitations.append("incomplete_28_day_history")
    summary = {"source_rows_total": len(outbound_rows) if isinstance(outbound_rows, list) else 0,
               "accepted_history_rows": accepted, "unique_skus": len(rows),
               **{f"rank_{rank}_skus": rank_counts[rank] for rank in range(1, 7)},
               "no_history_skus": rank_counts[None], "history_dates_present": len(source_dates),
               "history_span_complete": complete}
    identity = {"velocity_profile_version": VELOCITY_PROFILE_VERSION, "as_of_date": end.isoformat(),
                "history_start_date": start.isoformat(), "history_end_exclusive": end.isoformat(),
                "target_normalized_warehouse": target,
                "rows": [{key: row[key] for key in ("sku_key", "days_28", "days_14", "days_7", "days_4", "velocity_rank", "velocity_class")} for row in rows]}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    profile_id = "sha256:" + hashlib.sha256(encoded).hexdigest()
    profile = {**identity, "velocity_profile_id": profile_id, "rows": rows,
               "summary": summary, "limitations": limitations}
    warnings = [{"code": "incomplete_28_day_history"}] if not complete else []
    return profile, {"valid": True, "errors": [], "warnings": warnings}
