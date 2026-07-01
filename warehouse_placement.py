from collections import Counter
import pandas as pd
from warehouse_addressing import FIRST_TIER, normalize_address
from row_constructor import find_column

CELL_ALIASES = ["cell", "СЏС‡РµР№РєР°", "РЅРѕРјРµСЂ СЏС‡РµР№РєРё", "РјРµСЃС‚Рѕ"]
ROW_ALIASES = ["row", "СЂСЏРґ", "РЅРѕРјРµСЂ СЂСЏРґР°", "в„– СЂСЏРґР°"]
TIER_ALIASES = ["tier", "СЏСЂСѓСЃ", "level", "СѓСЂРѕРІРµРЅСЊ"]
ADDRESS_ALIASES = ["address", "Р°РґСЂРµСЃ", "Р°РґСЂРµСЃ СЏС‡РµР№РєРё", "СЃРєР»Р°РґСЃРєР°СЏ СЏС‡РµР№РєР°"]
ITEM_ALIASES = ["item", "С‚РѕРІР°СЂ", "РЅРѕРјРµРЅРєР»Р°С‚СѓСЂР°", "product", "sku"]


def _read_table(file_obj):
    name = getattr(file_obj, "name", "")
    if str(name).lower().endswith(".csv"):
        return pd.read_csv(file_obj)
    xls = pd.ExcelFile(file_obj)
    frames = []
    for sheet in xls.sheet_names:
        df = pd.read_excel(file_obj, sheet_name=sheet).dropna(how="all")
        if not df.empty:
            df["__source_sheet"] = sheet
            frames.append(df)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def import_cell_addresses(file_obj):
    df = _read_table(file_obj)
    if df.empty:
        return {}, [{"level": "error", "message": "Р¤Р°Р№Р» Р°РґСЂРµСЃРѕРІ РїСѓСЃС‚."}]
    mapping = {
        "cell": find_column(df.columns, CELL_ALIASES),
        "row": find_column(df.columns, ROW_ALIASES),
        "tier": find_column(df.columns, TIER_ALIASES),
        "address": find_column(df.columns, ADDRESS_ALIASES),
    }
    result, diagnostics = {}, []
    for line_no, row in df.iterrows():
        addr, warnings = normalize_address(
            row.get(mapping["cell"]) if mapping["cell"] else None,
            row.get(mapping["row"]) if mapping["row"] else None,
            row.get(mapping["tier"]) if mapping["tier"] else None,
            row.get(mapping["address"]) if mapping["address"] else None,
        )
        diagnostics.extend({"level": "warning", "message": w, "line": str(line_no + 2)} for w in warnings)
        if addr and addr.tier_number == FIRST_TIER:
            result.setdefault(addr.row_number, []).append(addr)
    return result, diagnostics


def import_placements(file_obj):
    df = _read_table(file_obj)
    if df.empty:
        return [], [{"level": "error", "message": "Р¤Р°Р№Р» СЂР°Р·РјРµС‰РµРЅРёСЏ РїСѓСЃС‚."}]
    mapping = {
        "cell": find_column(df.columns, CELL_ALIASES),
        "row": find_column(df.columns, ROW_ALIASES),
        "tier": find_column(df.columns, TIER_ALIASES),
        "address": find_column(df.columns, ADDRESS_ALIASES),
        "item": find_column(df.columns, ITEM_ALIASES),
    }
    placements, diagnostics = [], []
    for line_no, row in df.iterrows():
        item = str(row.get(mapping["item"], "")).strip() if mapping["item"] else ""
        addr, warnings = normalize_address(
            row.get(mapping["cell"]) if mapping["cell"] else None,
            row.get(mapping["row"]) if mapping["row"] else None,
            row.get(mapping["tier"]) if mapping["tier"] else None,
            row.get(mapping["address"]) if mapping["address"] else None,
        )
        diagnostics.extend({"level": "warning", "message": w, "line": str(line_no + 2)} for w in warnings)
        if not item:
            diagnostics.append({"level": "warning", "message": "РђРґСЂРµСЃ РЅР°Р№РґРµРЅ, РЅРѕ С‚РѕРІР°СЂ РЅРµ СѓРєР°Р·Р°РЅ.", "line": str(line_no + 2)})
        if addr is None:
            if item:
                diagnostics.append({"level": "error", "message": f"РўРѕРІР°СЂ '{item}' СѓРєР°Р·Р°РЅ Р±РµР· РєРѕСЂСЂРµРєС‚РЅРѕРіРѕ Р°РґСЂРµСЃР°.", "line": str(line_no + 2)})
            continue
        if addr.tier_number != FIRST_TIER:
            continue
        placements.append({"address": addr.address, "item": item, "line": line_no + 2})
    counts = Counter(p["address"] for p in placements)
    for address, count in counts.items():
        if count > 1:
            diagnostics.append({"level": "warning", "message": f"Р”СѓР±Р»СЊ Р°РґСЂРµСЃР° {address}: {count} Р·Р°РїРёСЃРµР№; Р±СѓРґРµС‚ РїРѕРєР°Р·Р°РЅ СЃРїРёСЃРѕРє С‚РѕРІР°СЂРѕРІ."})
    return placements, diagnostics


def apply_cell_addresses(model, addresses_by_row):
    diagnostics = []
    for sheet in model.sheets:
        for row in sheet.rows:
            imported = addresses_by_row.get(row.row_number, [])
            if not imported:
                continue
            for cell, addr in zip(row.potential_cells, imported):
                cell.cell_number = addr.cell_number
                cell.tier_number = addr.tier_number
                cell.address = addr.address
                cell.source = "imported_cells"
            if len(imported) != len(row.potential_cells):
                diagnostics.append({"level": "warning", "message": f"Р СЏРґ {row.row_number} Р»РёСЃС‚Р° '{sheet.name}': РёРјРїРѕСЂС‚РёСЂРѕРІР°РЅРѕ {len(imported)} Р°РґСЂРµСЃРѕРІ, Р° РЅР° СЃС…РµРјРµ СЃРѕР·РґР°РЅРѕ {len(row.potential_cells)} СЏС‡РµРµРє."})
    return diagnostics


def apply_placements(model, placements):
    diagnostics = []
    index = model.cell_index()
    by_addr = {}
    for placement in placements:
        if placement["address"] not in index:
            diagnostics.append({"level": "error", "message": f"РђРґСЂРµСЃ {placement['address']} РµСЃС‚СЊ РІ СЂР°Р·РјРµС‰РµРЅРёРё, РЅРѕ РЅРµ РЅР°Р№РґРµРЅ РЅР° СЃС…РµРјРµ."})
            continue
        by_addr.setdefault(placement["address"], []).append(placement["item"])
    for address, items in by_addr.items():
        cell = index[address]
        cell.item = "; ".join(item for item in items if item)
        if len([i for i in items if i]) > 1:
            cell.warnings.append("РќРµСЃРєРѕР»СЊРєРѕ С‚РѕРІР°СЂРѕРІ РІ РѕРґРЅРѕР№ СЏС‡РµР№РєРµ.")
            diagnostics.append({"level": "warning", "message": f"Р’ СЏС‡РµР№РєРµ {address} РЅРµСЃРєРѕР»СЊРєРѕ С‚РѕРІР°СЂРѕРІ: {cell.item}."})
    for address, cell in index.items():
        if not cell.item:
            diagnostics.append({"level": "info", "message": f"РђРґСЂРµСЃ {address} РЅР°Р№РґРµРЅ РЅР° СЃС…РµРјРµ, РЅРѕ С‚РѕРІР°СЂР° РІ С„Р°Р№Р»Рµ РЅРµС‚."})
    return diagnostics
