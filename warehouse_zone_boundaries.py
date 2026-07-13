from __future__ import annotations

from typing import Any

ZONE_ORDER = ["heavy", "medium", "light", "fragile"]
ZONE_LABELS = {"heavy": "Тяжёлое", "medium": "Среднее", "light": "Лёгкое", "fragile": "Хрупкое", "unassigned": "Не назначено"}
STATUS_COVERED = "Потребность покрыта"
STATUS_INSUFFICIENT = "Недостаточно вместимости"
STATUS_BLOCKED = "Граница заблокирована фактическим остатком"
STATUS_EMPTY_RECEIPT = "В приходе нет товара этой зоны"


def _display(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _number_key(value: Any) -> tuple[int, Any]:
    text = _display(value)
    try:
        return (0, int(float(text)))
    except ValueError:
        return (1, text)


def normalize_zone(value: Any) -> str:
    text = _display(value).lower().replace("ё", "е")
    text = "".join(text.split())
    if text in {"heavy", "тяжелое", "тяжелый"}:
        return "heavy"
    if text in {"medium", "среднее", "средний"}:
        return "medium"
    if text in {"light", "легкое", "легкий"}:
        return "light"
    if text in {"fragile", "хрупкое", "хрупкий"}:
        return "fragile"
    return "unassigned"


def ordered_rows(model: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(model.get("rows", []), key=lambda row: (_safe_float(row.get("row_order"), 10**9), _number_key(row.get("row_number"))))


def row_capacity(model: dict[str, Any], row_number: Any) -> float:
    row_text = _display(row_number)
    total = 0.0
    for cell in model.get("cells", []):
        if _display(cell.get("row_number")) != row_text:
            continue
        if "block" in _display(cell.get("source")).lower():
            continue
        total += _safe_float(cell.get("capacity_pallets"), 1.0)
    return round(total, 4)


def _boundaries_from_row_zones(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = ordered_rows(model)
    result: dict[str, dict[str, Any]] = {}
    for zone in ZONE_ORDER:
        zone_rows = [row for row in rows if normalize_zone(row.get("weight_zone")) == zone]
        result[zone] = _boundary_for_rows(model, zone, zone_rows)
    return result


def _boundary_for_rows(model: dict[str, Any], zone: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "zone": zone,
        "start_row": _display(rows[0].get("row_number")) if rows else "",
        "end_row": _display(rows[-1].get("row_number")) if rows else "",
        "row_count": len(rows),
        "capacity": round(sum(row_capacity(model, row.get("row_number")) for row in rows), 4),
    }


def ensure_zone_boundary_settings(model: dict[str, Any]) -> dict[str, Any]:
    settings = model.setdefault("zone_boundary_settings", {})
    current = _boundaries_from_row_zones(model)
    settings.setdefault("base_zone_boundaries", current)
    settings.setdefault("calculated_zone_boundaries", {})
    settings.setdefault("active_zone_boundaries", settings.get("base_zone_boundaries", current))
    settings.setdefault("zone_reserve_percent", 0.0)
    settings.setdefault("minimum_rows", {zone: 1 for zone in ZONE_ORDER})
    for zone in ZONE_ORDER:
        settings["minimum_rows"].setdefault(zone, 1)
    return settings


def set_base_boundaries_from_current_rows(model: dict[str, Any]) -> dict[str, Any]:
    settings = ensure_zone_boundary_settings(model)
    current = _boundaries_from_row_zones(model)
    settings["base_zone_boundaries"] = current
    settings["active_zone_boundaries"] = current
    return settings


def _rows_for_boundary(rows: list[dict[str, Any]], boundary: dict[str, Any]) -> list[dict[str, Any]]:
    start = _display(boundary.get("start_row"))
    end = _display(boundary.get("end_row"))
    if not start or not end:
        return []
    row_numbers = [_display(row.get("row_number")) for row in rows]
    try:
        start_idx = row_numbers.index(start)
        end_idx = row_numbers.index(end)
    except ValueError:
        return []
    if end_idx < start_idx:
        start_idx, end_idx = end_idx, start_idx
    return rows[start_idx : end_idx + 1]


def apply_active_boundaries_to_model(model: dict[str, Any], boundaries: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    settings = ensure_zone_boundary_settings(model)
    active = boundaries or settings.get("active_zone_boundaries") or settings.get("base_zone_boundaries") or {}
    rows = ordered_rows(model)
    zone_by_row: dict[str, str] = {}
    for zone in ZONE_ORDER:
        for row in _rows_for_boundary(rows, active.get(zone, {})):
            zone_by_row[_display(row.get("row_number"))] = zone
    for row in model.get("rows", []):
        row["weight_zone"] = zone_by_row.get(_display(row.get("row_number")), "unassigned")
    for cell in model.get("cells", []):
        cell["weight_zone"] = zone_by_row.get(_display(cell.get("row_number")), "unassigned")
    for cell in model.get("base_cells", []):
        cell["weight_zone"] = zone_by_row.get(_display(cell.get("row_number")), "unassigned")
    for row_setting in model.get("row_settings", []):
        row_setting["weight_zone"] = zone_by_row.get(_display(row_setting.get("row_number")), "unassigned")
    settings["active_zone_boundaries"] = _boundaries_from_row_zones(model)
    return model


def receipt_zone_requirements(receipts_state: dict[str, Any] | None) -> dict[str, float]:
    totals = {zone: 0.0 for zone in ZONE_ORDER}
    for receipt in (receipts_state or {}).get("receipts", []):
        zone = normalize_zone(receipt.get("weight_class") or receipt.get("warehouse_zone"))
        if zone in totals:
            totals[zone] += _safe_float(receipt.get("qty_pallets"))
    return {zone: round(value, 4) for zone, value in totals.items()}


def factual_occupied_by_zone(state: dict[str, Any] | None) -> dict[str, float]:
    totals = {zone: 0.0 for zone in ZONE_ORDER}
    for placement in (state or {}).get("placements", []):
        if placement.get("placement_mode") != "factual" and placement.get("source") != "inventory_with_cell":
            continue
        zone = normalize_zone(placement.get("weight_class") or placement.get("weight_zone"))
        if zone in totals:
            totals[zone] += _safe_float(placement.get("qty_pallets"))
    return {zone: round(value, 4) for zone, value in totals.items()}


def fixed_row_zones(state: dict[str, Any] | None) -> dict[str, str]:
    fixed: dict[str, str] = {}
    for placement in (state or {}).get("placements", []):
        if placement.get("placement_mode") != "factual" and placement.get("source") != "inventory_with_cell":
            continue
        zone = normalize_zone(placement.get("weight_class") or placement.get("weight_zone"))
        row = _display(placement.get("row_number"))
        if row and zone in ZONE_ORDER:
            fixed[row] = zone
    return fixed


def _base_zone_indices(model: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    settings = ensure_zone_boundary_settings(model)
    base = settings.get("base_zone_boundaries") or _boundaries_from_row_zones(model)
    row_numbers = [_display(row.get("row_number")) for row in rows]
    result = {}
    for zone in ZONE_ORDER:
        boundary = base.get(zone, {})
        try:
            start = row_numbers.index(_display(boundary.get("start_row")))
            end = row_numbers.index(_display(boundary.get("end_row")))
            if end < start:
                start, end = end, start
        except ValueError:
            start = end = -1
        result[zone] = (start, end)
    return result


def calculate_dynamic_zone_boundaries(model: dict[str, Any], receipts_state: dict[str, Any] | None, placement_state: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = ensure_zone_boundary_settings(model)
    rows = ordered_rows(model)
    n = len(rows)
    capacities = [row_capacity(model, row.get("row_number")) for row in rows]
    prefix = [0.0]
    for value in capacities:
        prefix.append(prefix[-1] + value)
    receipt_req = receipt_zone_requirements(receipts_state)
    factual = factual_occupied_by_zone(placement_state)
    reserve_percent = _safe_float(settings.get("zone_reserve_percent"), 0.0)
    min_rows = {zone: max(int(_safe_float(settings.get("minimum_rows", {}).get(zone), 1)), 0) for zone in ZONE_ORDER}
    if sum(min_rows.values()) > n:
        overflow = sum(min_rows.values()) - n
        for zone in reversed(ZONE_ORDER):
            take = min(overflow, min_rows[zone])
            min_rows[zone] -= take
            overflow -= take
            if overflow <= 0:
                break
    required = {zone: round((receipt_req[zone] + factual[zone]) * (1 + reserve_percent / 100.0), 4) for zone in ZONE_ORDER}
    fixed = fixed_row_zones(placement_state)
    base_indices = _base_zone_indices(model, rows)

    best: tuple | None = None
    best_counts: tuple[int, int, int, int] | None = None

    def segment_capacity(start: int, end: int) -> float:
        return round(prefix[end] - prefix[start], 4)

    hmin, mmin, lmin, fmin = [min_rows[z] for z in ZONE_ORDER]
    for h in range(hmin, n + 1):
        for m in range(mmin, n - h + 1):
            for l in range(lmin, n - h - m + 1):
                f = n - h - m - l
                if f < fmin:
                    continue
                counts = (h, m, l, f)
                starts = [0, h, h + m, h + m + l]
                ends = [h, h + m, h + m + l, n]
                blocked = False
                for zone, start, end in zip(ZONE_ORDER, starts, ends):
                    for idx in range(start, end):
                        row_num = _display(rows[idx].get("row_number"))
                        if fixed.get(row_num) and fixed[row_num] != zone:
                            blocked = True
                            break
                    if blocked:
                        break
                if blocked:
                    continue
                caps = {zone: segment_capacity(start, end) for zone, start, end in zip(ZONE_ORDER, starts, ends)}
                covered = sum(min(caps[z], required[z]) for z in ZONE_ORDER)
                full_count = sum(1 for z in ZONE_ORDER if caps[z] >= required[z])
                deficits = {z: max(required[z] - caps[z], 0.0) for z in ZONE_ORDER}
                excess = sum(max(caps[z] - required[z], 0.0) for z in ZONE_ORDER)
                shift = 0
                for zone, start, end in zip(ZONE_ORDER, starts, ends):
                    base_start, base_end = base_indices.get(zone, (-1, -1))
                    if base_start >= 0:
                        shift += abs(start - base_start) + abs((end - 1) - base_end)
                    else:
                        shift += n
                all_ok = all(value <= 0 for value in deficits.values())
                score = (0, shift, excess) if all_ok else (1, -covered, -full_count, shift, sum(deficits.values()), excess)
                if best is None or score < best:
                    best = score
                    best_counts = counts
    if best_counts is None:
        # Fallback keeps current/base rows if factual locks make every split impossible.
        calculated = settings.get("base_zone_boundaries") or _boundaries_from_row_zones(model)
        details = {zone: {"status": STATUS_BLOCKED, "deficit": required[zone], "required_capacity": required[zone], "receipt_required_pallets": receipt_req[zone], "factual_occupied_pallets": factual[zone], "reserve_percent": reserve_percent} for zone in ZONE_ORDER}
        return calculated, {"details": details, "fixed_rows": fixed, "minimum_rows": min_rows, "reserve_percent": reserve_percent}

    h, m, l, f = best_counts
    starts = [0, h, h + m, h + m + l]
    ends = [h, h + m, h + m + l, n]
    calculated: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    for zone, start, end in zip(ZONE_ORDER, starts, ends):
        zone_rows = rows[start:end]
        boundary = _boundary_for_rows(model, zone, zone_rows)
        calculated[zone] = boundary
        capacity = boundary["capacity"]
        deficit = max(required[zone] - capacity, 0.0)
        if receipt_req[zone] <= 0 and factual[zone] <= 0:
            status = STATUS_EMPTY_RECEIPT
        elif deficit > 0:
            status = STATUS_INSUFFICIENT
        else:
            status = STATUS_COVERED
        details[zone] = {
            "receipt_required_pallets": receipt_req[zone],
            "factual_occupied_pallets": factual[zone],
            "required_capacity": required[zone],
            "capacity": capacity,
            "free_capacity": max(capacity - factual[zone], 0.0),
            "reserve_percent": reserve_percent,
            "deficit": round(deficit, 4),
            "status": status,
        }
    return calculated, {"details": details, "fixed_rows": fixed, "minimum_rows": min_rows, "reserve_percent": reserve_percent}
