from collections import Counter
import pandas as pd
from warehouse_addressing import FIRST_TIER, normalize_address
from row_constructor import find_column

CELL_ALIASES = ["cell", "ячейка", "номер ячейки", "место"]
ROW_ALIASES = ["row", "ряд", "номер ряда", "№ ряда"]
TIER_ALIASES = ["tier", "ярус", "level", "уровень"]
ADDRESS_ALIASES = ["address", "адрес", "адрес ячейки", "складская ячейка"]
ITEM_ALIASES = ["item", "товар", "номенклатура", "product", "sku"]


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
        return {}, [{"level": "error", "message": "Файл адресов пуст."}]
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
        return [], [{"level": "error", "message": "Файл размещения пуст."}]
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
            diagnostics.append({"level": "warning", "message": "Адрес найден, но товар не указан.", "line": str(line_no + 2)})
        if addr is None:
            if item:
                diagnostics.append({"level": "error", "message": f"Товар '{item}' указан без корректного адреса.", "line": str(line_no + 2)})
            continue
        if addr.tier_number != FIRST_TIER:
            continue
        placements.append({"address": addr.address, "item": item, "line": line_no + 2})
    counts = Counter(p["address"] for p in placements)
    for address, count in counts.items():
        if count > 1:
            diagnostics.append({"level": "warning", "message": f"Дубль адреса {address}: {count} записей; будет показан список товаров."})
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
                diagnostics.append({"level": "warning", "message": f"Ряд {row.row_number} листа '{sheet.name}': импортировано {len(imported)} адресов, а на схеме создано {len(row.potential_cells)} ячеек."})
    return diagnostics


def apply_placements(model, placements):
    diagnostics = []
    index = model.cell_index()
    by_addr = {}
    for placement in placements:
        if placement["address"] not in index:
            diagnostics.append({"level": "error", "message": f"Адрес {placement['address']} есть в размещении, но не найден на схеме."})
            continue
        by_addr.setdefault(placement["address"], []).append(placement["item"])
    for address, items in by_addr.items():
        cell = index[address]
        cell.item = "; ".join(item for item in items if item)
        if len([i for i in items if i]) > 1:
            cell.warnings.append("Несколько товаров в одной ячейке.")
            diagnostics.append({"level": "warning", "message": f"В ячейке {address} несколько товаров: {cell.item}."})
    for address, cell in index.items():
        if not cell.item:
            diagnostics.append({"level": "info", "message": f"Адрес {address} найден на схеме, но товара в файле нет."})
    return diagnostics
