"""Single source of truth for deep-lane access and front-to-back depth."""
from __future__ import annotations

import math
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

VALID_DEEP_LANE_ACCESS_SIDES = frozenset({"left", "right"})


def normalize_deep_lane_access_side(value: Any) -> str | None:
    """Return a canonical configured side; blanks deliberately stay unknown."""
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    return normalized if normalized in VALID_DEEP_LANE_ACCESS_SIDES else None


def derive_deep_lane_depths(
    physical_slots: Sequence[Mapping[str, Any]], access_side: Any, capacity: int
) -> tuple[dict[int, int] | None, str | None]:
    """Map stable slot ids to semantic depths using X coordinates only."""
    side = normalize_deep_lane_access_side(access_side)
    if side is None:
        reason = "deep_lane_access_unconfigured" if access_side is None or not str(access_side).strip() else "deep_lane_access_invalid"
        return None, reason
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1 or len(physical_slots) != capacity:
        return None, "deep_lane_geometry_invalid"
    evidence: list[tuple[float, int]] = []
    for slot in physical_slots:
        slot_index = slot.get("slot_index")
        try:
            x_min, x_max = float(slot.get("x_min")), float(slot.get("x_max"))
        except (TypeError, ValueError):
            return None, "deep_lane_geometry_invalid"
        if (isinstance(slot_index, bool) or not isinstance(slot_index, int) or slot_index < 1
                or not math.isfinite(x_min) or not math.isfinite(x_max) or x_max <= x_min):
            return None, "deep_lane_geometry_invalid"
        evidence.append(((x_min + x_max) / 2, slot_index))
    if len({slot_id for _, slot_id in evidence}) != capacity or len({x for x, _ in evidence}) != capacity:
        return None, "deep_lane_geometry_invalid"
    ordered = sorted(evidence, key=lambda item: item[0], reverse=side == "right")
    depths = {slot_id: depth for depth, (_, slot_id) in enumerate(ordered, 1)}
    if set(depths.values()) != set(range(1, capacity + 1)):
        return None, "deep_lane_geometry_invalid"
    return depths, None


def deep_lane_access_diagnostics(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result = {"deep_lane_rows_total": 0, "deep_lane_access_configured": 0,
              "deep_lane_access_missing": 0, "deep_lane_access_invalid": 0}
    for row in rows:
        if row.get("row_storage_type") != "deep_lane":
            continue
        result["deep_lane_rows_total"] += 1
        raw = row.get("deep_lane_access_side")
        if raw is None or not str(raw).strip():
            result["deep_lane_access_missing"] += 1
        elif normalize_deep_lane_access_side(raw):
            result["deep_lane_access_configured"] += 1
        else:
            result["deep_lane_access_invalid"] += 1
    return result


def deep_lane_access_contract_id(rows: Sequence[Mapping[str, Any]]) -> str:
    """Deterministic business identity for the row-level access contract."""
    contract = sorted(
        ({"row_number": str(row.get("row_number") or "").strip(),
          "access_side": str(row.get("deep_lane_access_side") or "").strip()}
         for row in rows if row.get("row_storage_type") == "deep_lane"),
        key=lambda item: (item["row_number"], item["access_side"]),
    )
    payload = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
