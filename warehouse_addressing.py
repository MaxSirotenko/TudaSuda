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
        parts = [part.strip() for part in re.split(r"[-вЂ“вЂ”]", raw_address) if part.strip()]
        if len(parts) not in {2, 3}:
            return None, [f"РќРµРІРµСЂРЅС‹Р№ С„РѕСЂРјР°С‚ Р°РґСЂРµСЃР° '{raw_address}'. РћР¶РёРґР°РµС‚СЃСЏ СЏС‡РµР№РєР°-СЂСЏРґ-СЏСЂСѓСЃ."]
        raw_cell, raw_row = parts[0], parts[1]
        raw_tier = parts[2] if len(parts) == 3 else raw_tier

    if not raw_cell or not raw_row:
        return None, ["РќРµ СѓРєР°Р·Р°РЅР° СЏС‡РµР№РєР° РёР»Рё СЂСЏРґ РґР»СЏ Р°РґСЂРµСЃР°."]

    tier_was_missing = False
    if not raw_tier:
        raw_tier = str(FIRST_TIER)
        tier_was_missing = True
        warnings.append(f"Р”Р»СЏ Р°РґСЂРµСЃР° {raw_cell}-{raw_row} РЅРµ СѓРєР°Р·Р°РЅ СЏСЂСѓСЃ; РїСЂРёРЅСЏС‚ РїРµСЂРІС‹Р№ СЏСЂСѓСЃ.")

    try:
        tier_number = int(float(raw_tier))
    except ValueError:
        return None, [f"РќРµРІРµСЂРЅС‹Р№ СЏСЂСѓСЃ '{raw_tier}' РґР»СЏ Р°РґСЂРµСЃР° {raw_cell}-{raw_row}-{raw_tier}."]

    normalized = WarehouseAddress(
        cell_number=raw_cell,
        row_number=raw_row,
        tier_number=tier_number,
        address=f"{raw_cell}-{raw_row}-{tier_number}",
        tier_was_missing=tier_was_missing,
    )
    if tier_number != FIRST_TIER:
        warnings.append(f"РђРґСЂРµСЃ {normalized.address} РѕС‚РЅРѕСЃРёС‚СЃСЏ Рє СЏСЂСѓСЃСѓ {tier_number} Рё Р±СѓРґРµС‚ РёСЃРєР»СЋС‡РµРЅ РЅР° С‚РµРєСѓС‰РµРј СЌС‚Р°РїРµ.")
    return normalized, warnings
