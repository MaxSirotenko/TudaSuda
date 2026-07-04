import re
from dataclasses import dataclass
from typing import Any

FIRST_TIER = 1

@dataclass(frozen=True)
class WarehouseAddress:
    cell_number: str
    row_number: str
    tier_number: int
    address: str
    tier_was_missing: bool = False


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return re.sub(r"\s+", "", text)


def normalize_address(cell: Any = None, row: Any = None, tier: Any = None, address: Any = None) -> tuple[WarehouseAddress | None, list[str]]:
    warnings: list[str] = []
    raw_address = _clean(address)
    raw_cell = _clean(cell)
    raw_row = _clean(row)
    raw_tier = _clean(tier)

    if raw_address:
        parts = [part.strip() for part in re.split(r"[-–—]", raw_address) if part.strip()]
        if len(parts) not in {2, 3}:
            return None, [f"Неверный формат адреса '{raw_address}'. Ожидается ячейка-ряд-ярус."]
        raw_cell, raw_row = parts[0], parts[1]
        raw_tier = parts[2] if len(parts) == 3 else raw_tier

    if not raw_cell or not raw_row:
        return None, ["Не указана ячейка или ряд для адреса."]

    tier_was_missing = False
    if not raw_tier:
        raw_tier = str(FIRST_TIER)
        tier_was_missing = True
        warnings.append(f"Для адреса {raw_cell}-{raw_row} не указан ярус; принят первый ярус.")

    try:
        tier_number = int(float(raw_tier))
    except ValueError:
        return None, [f"Неверный ярус '{raw_tier}' для адреса {raw_cell}-{raw_row}-{raw_tier}."]

    normalized = WarehouseAddress(
        cell_number=raw_cell,
        row_number=raw_row,
        tier_number=tier_number,
        address=f"{raw_cell}-{raw_row}-{tier_number}",
        tier_was_missing=tier_was_missing,
    )
    if tier_number != FIRST_TIER:
        warnings.append(f"Адрес {normalized.address} относится к ярусу {tier_number} и будет исключен на текущем этапе.")
    return normalized, warnings
