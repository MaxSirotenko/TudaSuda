from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLACEMENT_DIAGNOSTICS_PATH = Path("data/last_import/placement_diagnostics.json")

ZONE_LABELS_RU = {
    "heavy": "Тяжёлое",
    "medium": "Среднее",
    "light": "Лёгкое",
    "fragile": "Хрупкое",
    "unclassified": "Без классификации",
    "unassigned": "Не назначено",
    "": "Нет данных",
}

PLACEMENT_CATEGORY_COLORS = {
    "heavy": "#F4A6A6",
    "medium": "#F7D486",
    "light": "#BFE3B4",
    "fragile": "#D8B4FE",
    "unclassified": "#CBD5E1",
    "unassigned": "#E5E7EB",
}

REASON_TEXTS = {
    "same_sku_partial_cell": "Добавлено в частично заполненную ячейку с тем же SKU.",
    "adjacent_to_same_sku": "Выбрана свободная ячейка рядом с уже размещённым SKU в той же зоне.",
    "matching_weight_zone": "Выбрана свободная ячейка в подходящей весовой зоне.",
    "zone_overflow": "В целевой весовой зоне закончилась свободная вместимость. Зона расширена в ближайший соседний ряд.",
    "fragile_priority": "Хрупкий товар размещён в зоне хрупкого товара.",
    "unclassified_fallback": "Товар без категории обработан резервным правилом.",
    "existing_stock": "Товар находился в ячейке до текущего расчёта.",
    "fallback": "Использовано резервное правило текущего алгоритма.",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _display(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _cell_key(row: Any, cell: Any, tier: Any = "1") -> str:
    return f"{_display(row)}|{_display(cell)}|{_display(tier) or '1'}"


def _sku_key(item: dict[str, Any]) -> str:
    return _display(item.get("sku_key")) or "|".join(
        part for part in [
            _display(item.get("sku_code")) or _display(item.get("sku_name")) or _display(item.get("item_name")),
            _display(item.get("characteristic_code")) or _display(item.get("characteristic_name")),
        ] if part
    ) or "Нет данных"


def _qty(item: dict[str, Any]) -> float:
    return _float(item.get("occupied_capacity_pallets", item.get("qty_pallets", 0)))


def _capacity(cell: dict[str, Any]) -> float:
    value = _float(cell.get("capacity_pallets"), 0.0)
    if value > 0:
        return value
    if cell.get("storage_type") == "deep_lane":
        return max(_float(cell.get("deep_lane_width"), 1.0), 1.0)
    return 1.0


def _is_receipt_placement(item: dict[str, Any]) -> bool:
    return item.get("source") == "receipt" or bool(item.get("receipt_line_ids")) or bool(item.get("receipt_numbers"))


def placement_reason_text(reason_code: str) -> str:
    return REASON_TEXTS.get(reason_code or "", "Нет данных")


def summarize_placements_by_cell(placements: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for placement in placements:
        key = _display(placement.get("cell_key")) or _cell_key(placement.get("row_number"), placement.get("cell_number"), placement.get("tier"))
        sku = _sku_key(placement)
        entry = result.setdefault(key, {"occupied_capacity_pallets": 0.0, "placements": [], "sku_keys": set(), "receipt_sku_keys": set()})
        entry["occupied_capacity_pallets"] += _qty(placement)
        entry["placements"].append(placement)
        entry["sku_keys"].add(sku)
        if _is_receipt_placement(placement):
            entry["receipt_sku_keys"].add(sku)
    for entry in result.values():
        entry["sku_keys"] = sorted(entry["sku_keys"])
        entry["receipt_sku_keys"] = sorted(entry["receipt_sku_keys"])
    return result


def save_pre_placement_snapshot(model: dict[str, Any], placement_state: dict[str, Any], receipts_state: dict[str, Any] | None = None, trigger: str = "basic_weight_placement") -> dict[str, Any]:
    snapshot = {
        "model_id": model.get("model_id"),
        "created_at": _now(),
        "trigger": trigger,
        "placements_before": copy.deepcopy(placement_state.get("placements", [])),
        "unplaced_before": copy.deepcopy(placement_state.get("unplaced_inventory", [])),
        "receipt_line_ids": [item.get("receipt_line_id") for item in (receipts_state or {}).get("receipts", []) if item.get("receipt_line_id")],
        "receipt_numbers": sorted({item.get("receipt_number") for item in (receipts_state or {}).get("receipts", []) if item.get("receipt_number")}),
        "before_by_cell": summarize_placements_by_cell(placement_state.get("placements", [])),
    }
    PLACEMENT_DIAGNOSTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLACEMENT_DIAGNOSTICS_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


def load_pre_placement_snapshot(model: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    if not PLACEMENT_DIAGNOSTICS_PATH.exists():
        return None, "Снимок состояния до размещения отсутствует. Для старых расчётов значения 'до' показаны как «Нет данных»."
    try:
        snapshot = json.loads(PLACEMENT_DIAGNOSTICS_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None, "Файл placement_diagnostics.json повреждён. Значения 'до' недоступны."
    if model and snapshot.get("model_id") not in {None, model.get("model_id")}:
        return snapshot, "Снимок 'до' относится к другой версии склада. Проверьте актуальность диагностики."
    return snapshot, None


def initialize_initial_weight_zones(model: dict[str, Any]) -> bool:
    changed = False
    row_initial: dict[str, str] = {}
    for row in model.get("rows", []):
        current = _display(row.get("weight_zone")) or "unassigned"
        if "initial_weight_zone" not in row:
            row["initial_weight_zone"] = current
            changed = True
        row_initial[_display(row.get("row_number"))] = _display(row.get("initial_weight_zone")) or current
    for group_name in ["cells", "base_cells"]:
        for cell in model.get(group_name, []):
            if "initial_weight_zone" not in cell:
                cell["initial_weight_zone"] = row_initial.get(_display(cell.get("row_number")), _display(cell.get("weight_zone")) or "unassigned")
                changed = True
    for setting in model.get("row_settings", []):
        if "initial_weight_zone" not in setting:
            setting["initial_weight_zone"] = row_initial.get(_display(setting.get("row_number")), _display(setting.get("weight_zone")) or "unassigned")
            changed = True
    return changed


def build_zone_change_rows(model: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    cells_by_row: dict[str, list[dict[str, Any]]] = {}
    for cell in model.get("cells", []):
        cells_by_row.setdefault(_display(cell.get("row_number")), []).append(cell)
    for row in model.get("rows", []):
        row_number = _display(row.get("row_number"))
        initial = row.get("initial_weight_zone")
        current = _display(row.get("weight_zone")) or "unassigned"
        row_cells = cells_by_row.get(row_number, [])
        capacity = sum(_capacity(cell) for cell in row_cells)
        if initial in {None, ""}:
            status = "Нет данных об исходной зоне"
            initial_label = "Нет данных"
        elif _display(initial) == current:
            status = "Без изменений"
            initial_label = ZONE_LABELS_RU.get(_display(initial), _display(initial))
        else:
            status = "Зона изменена"
            initial_label = ZONE_LABELS_RU.get(_display(initial), _display(initial))
        rows.append({
            "Номер ряда": row_number,
            "Исходная весовая зона": initial_label,
            "Текущая весовая зона": ZONE_LABELS_RU.get(current, current),
            "Количество логических ячеек": len(row_cells),
            "Вместимость ряда": round(capacity, 4),
            "Статус изменения": status,
        })
    return rows


def _aggregate_by_sku(items: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in items:
        result[_sku_key(item)] = result.get(_sku_key(item), 0.0) + _float(item.get("qty_pallets", item.get("occupied_capacity_pallets", 0)))
    return result


def _cell_by_key(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier")): cell for cell in model.get("cells", [])}


def _category_for_placements(placements: list[dict[str, Any]], fallback_zone: str = "unclassified") -> str:
    for placement in placements:
        zone = _display(placement.get("calculated_zone")) or _display(placement.get("weight_class"))
        if zone == "fragile":
            return "fragile"
    for placement in placements:
        zone = _display(placement.get("calculated_zone")) or _display(placement.get("weight_class"))
        if zone in {"heavy", "medium", "light", "fragile", "unclassified"}:
            return zone
    return fallback_zone or "unclassified"


def build_occupied_cell_rows(model: dict[str, Any], placement_state: dict[str, Any], receipts_state: dict[str, Any] | None, snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    before_by_cell = (snapshot or {}).get("before_by_cell") or summarize_placements_by_cell((snapshot or {}).get("placements_before", []))
    current_by_cell = summarize_placements_by_cell(placement_state.get("placements", []))
    cells = _cell_by_key(model)
    rows = []
    for key, summary in sorted(current_by_cell.items()):
        cell = cells.get(key, {})
        by_sku: dict[str, dict[str, Any]] = {}
        for placement in summary.get("placements", []):
            sku = _sku_key(placement)
            entry = by_sku.setdefault(sku, {"qty": 0.0, "placements": [], "receipt_line_ids": set(), "receipt_numbers": set()})
            entry["qty"] += _qty(placement)
            entry["placements"].append(placement)
            for value in placement.get("receipt_line_ids") or []:
                if value:
                    entry["receipt_line_ids"].add(value)
            for value in placement.get("receipt_numbers") or []:
                if value:
                    entry["receipt_numbers"].add(value)
        for sku, entry in by_sku.items():
            before_same = 0.0
            for before in before_by_cell.get(key, {}).get("placements", []):
                if _sku_key(before) == sku:
                    before_same += _qty(before)
            after = entry["qty"]
            added = max(after - before_same, 0.0)
            placements = entry["placements"]
            category = _category_for_placements(placements, _display(cell.get("weight_zone")) or "unclassified")
            reason_code = next((_display(p.get("placement_reason_code")) for p in placements if p.get("placement_reason_code")), "existing_stock" if before_same > 0 and added == 0 else "fallback")
            reason_text = next((_display(p.get("placement_reason_text")) for p in placements if p.get("placement_reason_text")), placement_reason_text(reason_code))
            source_status = "смешанная занятость" if before_same > 0 and added > 0 else ("остаток" if before_same > 0 else "новый приход")
            rows.append({
                "Ячейка": key,
                "Ряд": _display(cell.get("row_number")) or _display(placements[0].get("row_number")),
                "Весовая зона ячейки": ZONE_LABELS_RU.get(_display(cell.get("weight_zone")), _display(cell.get("weight_zone")) or "Нет данных"),
                "Категория SKU": ZONE_LABELS_RU.get(category, category),
                "Номенклатура": next((_display(p.get("sku_name") or p.get("item_name")) for p in placements if p.get("sku_name") or p.get("item_name")), "Нет данных"),
                "Характеристика": next((_display(p.get("characteristic_name")) for p in placements if p.get("characteristic_name")), ""),
                "sku_key": sku,
                "Было до": round(before_same, 4),
                "Добавлено": round(added, 4),
                "Стало после": round(after, 4),
                "Вместимость": round(_capacity(cell), 4),
                "Свободно": round(max(_capacity(cell) - sum(_qty(p) for p in summary.get("placements", [])), 0.0), 4),
                "Номера приходных ордеров": ", ".join(sorted(entry["receipt_numbers"])) or "Нет данных",
                "receipt_line_ids": ", ".join(sorted(entry["receipt_line_ids"])) or "Нет данных",
                "Код причины размещения": reason_code,
                "Причина размещения": reason_text,
                "Источник": source_status,
            })
    return rows


def build_unplaced_rows(placement_state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in placement_state.get("unplaced_inventory", []):
        required = _float(item.get("qty_pallets"))
        rows.append({
            "Номенклатура": _display(item.get("sku_name") or item.get("item_name")) or "Нет данных",
            "Характеристика": _display(item.get("characteristic_name")),
            "sku_key": _sku_key(item),
            "Требовалось разместить": required,
            "Размещено": 0.0,
            "Не размещено": required,
            "Весовая категория": ZONE_LABELS_RU.get(_display(item.get("calculated_zone") or item.get("weight_class")), _display(item.get("calculated_zone") or item.get("weight_class")) or "Нет данных"),
            "Номера приходных ордеров": ", ".join(item.get("receipt_numbers") or ([_display(item.get("receipt_number"))] if item.get("receipt_number") else [])) or "Нет данных",
            "receipt_line_ids": ", ".join(item.get("receipt_line_ids") or ([_display(item.get("receipt_line_id"))] if item.get("receipt_line_id") else [])) or "Нет данных",
            "Причина": _display(item.get("unplaced_reason") or item.get("reason")) or "Нет данных",
        })
    return rows


def build_zone_rows(model: dict[str, Any], placement_state: dict[str, Any], receipts_state: dict[str, Any] | None, snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    before_by_cell = (snapshot or {}).get("before_by_cell") or summarize_placements_by_cell((snapshot or {}).get("placements_before", []))
    after_by_cell = summarize_placements_by_cell(placement_state.get("placements", []))
    zones = {(_display(cell.get("weight_zone")) or "unassigned") for cell in model.get("cells", [])}
    zones.update(_display(p.get("weight_zone")) or "unassigned" for p in placement_state.get("placements", []))
    rows = []
    for zone in sorted(zones):
        zone_cells = [cell for cell in model.get("cells", []) if (_display(cell.get("weight_zone")) or "unassigned") == zone]
        keys = {_cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier")) for cell in zone_cells}
        total_capacity = sum(_capacity(cell) for cell in zone_cells)
        before_occ = sum(_float(before_by_cell.get(key, {}).get("occupied_capacity_pallets")) for key in keys)
        after_occ = sum(_float(after_by_cell.get(key, {}).get("occupied_capacity_pallets")) for key in keys)
        before_cells = {key for key in keys if _float(before_by_cell.get(key, {}).get("occupied_capacity_pallets")) > 0}
        after_cells = {key for key in keys if _float(after_by_cell.get(key, {}).get("occupied_capacity_pallets")) > 0}
        after_skus = {sku for key in keys for sku in after_by_cell.get(key, {}).get("sku_keys", [])}
        receipt_skus = {sku for key in keys for sku in after_by_cell.get(key, {}).get("receipt_sku_keys", [])}
        rows.append({
            "Весовая зона": ZONE_LABELS_RU.get(zone, zone),
            "Всего логических ячеек": len(zone_cells),
            "Общая вместимость": round(total_capacity, 4),
            "Занято ячеек до": len(before_cells) if snapshot else "Нет данных",
            "Занято ячеек после": len(after_cells),
            "Новых занятых ячеек": len(after_cells - before_cells) if snapshot else "Нет данных",
            "Свободно ячеек после": max(len(zone_cells) - len(after_cells), 0),
            "Занятая вместимость до": round(before_occ, 4) if snapshot else "Нет данных",
            "Занятая вместимость после": round(after_occ, 4),
            "Свободная вместимость после": round(max(total_capacity - after_occ, 0.0), 4),
            "Уникальных SKU после размещения": len(after_skus),
            "SKU текущего прихода": len(receipt_skus),
            "Процент заполненности до": round(before_occ / total_capacity * 100, 2) if snapshot and total_capacity else "Нет данных",
            "Процент заполненности после": round(after_occ / total_capacity * 100, 2) if total_capacity else 0.0,
        })
    return rows


def build_tooltip_by_cell(model: dict[str, Any], placement_state: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, str]:
    detail_rows = build_occupied_cell_rows(model, placement_state, None, snapshot)
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for row in detail_rows:
        by_cell.setdefault(row["Ячейка"], []).append(row)
    cells = _cell_by_key(model)
    result = {}
    for key, rows in by_cell.items():
        cell = cells.get(key, {})
        lines = [
            f"Ячейка: {key}",
            f"Ряд: {_display(cell.get('row_number')) or rows[0].get('Ряд')}",
            f"Тип ряда: {_display(cell.get('storage_type')) or 'Нет данных'}",
            f"Весовая зона: {ZONE_LABELS_RU.get(_display(cell.get('weight_zone')), _display(cell.get('weight_zone')) or 'Нет данных')}",
            f"Вместимость: {_capacity(cell):g}",
        ]
        for row in rows:
            lines.extend([
                "—",
                f"Номенклатура: {row['Номенклатура']}",
                f"Характеристика: {row['Характеристика'] or 'Нет данных'}",
                f"sku_key: {row['sku_key']}",
                f"Количество: {row['Стало после']:g}",
                f"Приходные ордера: {row['Номера приходных ордеров']}",
                f"receipt_line_ids: {row['receipt_line_ids']}",
                f"Было до: {row['Было до']:g}",
                f"Добавлено: {row['Добавлено']:g}",
                f"Стало после: {row['Стало после']:g}",
                f"Способ размещения: {row['Источник']}",
                f"Причина: {row['Причина размещения']}",
            ])
        return_lines = lines[:80]
        result[key] = "\n".join(return_lines)
    return result


def build_placement_diagnostics(model: dict[str, Any], placement_state: dict[str, Any], receipts_state: dict[str, Any] | None = None, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    receipts = (receipts_state or {}).get("receipts", [])
    placements = placement_state.get("placements", [])
    before_by_cell = (snapshot or {}).get("before_by_cell") or summarize_placements_by_cell((snapshot or {}).get("placements_before", []))
    after_by_cell = summarize_placements_by_cell(placements)
    cells = _cell_by_key(model)
    receipt_required = _aggregate_by_sku(receipts)
    receipt_placed: dict[str, float] = {}
    for placement in placements:
        if _is_receipt_placement(placement):
            receipt_placed[_sku_key(placement)] = receipt_placed.get(_sku_key(placement), 0.0) + _qty(placement)
    unplaced_by_sku = _aggregate_by_sku(placement_state.get("unplaced_inventory", []))
    fully = partially = not_placed = 0
    for sku, required in receipt_required.items():
        placed = receipt_placed.get(sku, 0.0)
        if placed >= required and required > 0:
            fully += 1
        elif placed > 0:
            partially += 1
        else:
            not_placed += 1
    total_capacity = sum(_capacity(cell) for cell in model.get("cells", []))
    after_occupied = sum(_float(entry.get("occupied_capacity_pallets")) for entry in after_by_cell.values())
    before_occupied = sum(_float(entry.get("occupied_capacity_pallets")) for entry in before_by_cell.values()) if snapshot else None
    receipt_total = sum(_float(item.get("qty_pallets")) for item in receipts)
    placed_total = sum(receipt_placed.values())
    unplaced_total = sum(unplaced_by_sku.values())
    used_physical = 0
    for key, entry in after_by_cell.items():
        cell = cells.get(key, {})
        used_physical += min(math.ceil(_float(entry.get("occupied_capacity_pallets"))), int(max(_capacity(cell), 1)))
    summary = {
        "Всего строк прихода": len(receipts),
        "Всего уникальных SKU в приходе": len(receipt_required),
        "Всего паллет к размещению": round(receipt_total, 4),
        "Успешно размещено": round(placed_total, 4),
        "Не размещено": round(unplaced_total, 4),
        "Процент успешного размещения": round((placed_total / receipt_total * 100) if receipt_total else 0.0, 2),
        "SKU размещены полностью": fully,
        "SKU размещены частично": partially,
        "SKU не размещены": not_placed,
        "Использованных логических ячеек": len([key for key, entry in after_by_cell.items() if _float(entry.get("occupied_capacity_pallets")) > 0]),
        "Использованных физических паллетомест": used_physical,
        "Ячеек занято до": len([key for key, entry in before_by_cell.items() if _float(entry.get("occupied_capacity_pallets")) > 0]) if snapshot else "Нет данных",
        "Ячеек занято после": len([key for key, entry in after_by_cell.items() if _float(entry.get("occupied_capacity_pallets")) > 0]),
        "Новых занятых ячеек": len(set(after_by_cell) - set(before_by_cell)) if snapshot else "Нет данных",
        "Свободных ячеек после": max(len(model.get("cells", [])) - len(after_by_cell), 0),
        "Общая вместимость склада": round(total_capacity, 4),
        "Занятая вместимость до": round(before_occupied, 4) if before_occupied is not None else "Нет данных",
        "Занятая вместимость после": round(after_occupied, 4),
        "Свободная вместимость после": round(max(total_capacity - after_occupied, 0.0), 4),
    }
    zone_changes = build_zone_change_rows(model)
    return {
        "summary": summary,
        "zone_rows": build_zone_rows(model, placement_state, receipts_state, snapshot),
        "zone_changes": zone_changes,
        "occupied_rows": build_occupied_cell_rows(model, placement_state, receipts_state, snapshot),
        "unplaced_rows": build_unplaced_rows(placement_state),
        "tooltips": build_tooltip_by_cell(model, placement_state, snapshot),
        "changed_rows_count": sum(1 for row in zone_changes if row["Статус изменения"] == "Зона изменена"),
        "changed_cells_count": sum(row["Количество логических ячеек"] for row in zone_changes if row["Статус изменения"] == "Зона изменена"),
        "snapshot_warning": None if snapshot else "Нет снимка состояния до размещения для текущего расчёта.",
    }


def enrich_model_with_placement_diagnostics(model: dict[str, Any], placement_state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    updated = copy.deepcopy(model)
    tooltips = build_tooltip_by_cell(updated, placement_state, snapshot)
    after_by_cell = summarize_placements_by_cell(placement_state.get("placements", []))
    for cell in updated.get("cells", []):
        key = _cell_key(cell.get("row_number"), cell.get("cell_number"), cell.get("tier"))
        placements = after_by_cell.get(key, {}).get("placements", [])
        occupied = _float(after_by_cell.get(key, {}).get("occupied_capacity_pallets"))
        if placements:
            cell["occupied_capacity_pallets"] = occupied
            cell["free_capacity_pallets"] = max(_capacity(cell) - occupied, 0.0)
            cell["occupancy_label"] = f"{occupied:g}/{_capacity(cell):g}"
            cell["placements"] = placements
        if key in tooltips:
            cell["placement_tooltip"] = tooltips[key]
        if placements:
            cell["placement_category"] = _category_for_placements(placements, _display(cell.get("weight_zone")) or "unclassified")
    return updated
