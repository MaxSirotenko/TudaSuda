import re
from openpyxl import load_workbook
from warehouse_addressing import FIRST_TIER
from warehouse_model import WarehouseCell, WarehouseModel, WarehouseRow, WarehouseSheet

ROW_LABEL_RE = re.compile(r"(?:^|\b)(?:ряд\s*)?(\d{1,4}|[A-Za-zА-Яа-яЁё]{1,3}\d{0,3})(?:\b|$)", re.IGNORECASE)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _row_number(label: str) -> str:
    match = ROW_LABEL_RE.search(label.replace("№", ""))
    return match.group(1) if match else label.strip()


def _looks_like_row_label(text: str) -> bool:
    clean = text.strip()
    if not clean or len(clean) > 30:
        return False
    low = clean.lower()
    return "ряд" in low or bool(re.fullmatch(r"\d{1,4}|[A-Za-zА-Яа-яЁё]{1,3}\d{0,3}", clean))


def _find_extent(ws, r: int, c: int) -> tuple[int, int, int, int, str, float, list[str]]:
    warnings = []
    right = c + 1
    while right <= ws.max_column and _text(ws.cell(r, right).value) == "":
        right += 1
    down = r + 1
    while down <= ws.max_row and _text(ws.cell(down, c).value) == "":
        down += 1
    horizontal_span = max(1, min(20, (right - c - 1) if right <= ws.max_column else 8))
    vertical_span = max(1, min(20, (down - r - 1) if down <= ws.max_row else 8))
    if horizontal_span >= vertical_span:
        direction = "left_to_right"
        min_row, max_row = r, r
        min_col, max_col = c + 1, c + horizontal_span
    else:
        direction = "top_to_bottom"
        min_row, max_row = r + 1, r + vertical_span
        min_col, max_col = c, c
    confidence = 0.75 if max(horizontal_span, vertical_span) >= 3 else 0.45
    if confidence < 0.6:
        warnings.append("Ряд найден по подписи, но область ячеек рядом с подписью определена сомнительно.")
    return min_row, min_col, max_row, max_col, direction, confidence, warnings


def parse_warehouse_excel(file_obj) -> WarehouseModel:
    wb = load_workbook(file_obj, data_only=True)
    model = WarehouseModel(sheets=[])
    for ws in wb.worksheets:
        values = []
        labels = []
        for row in ws.iter_rows():
            for cell in row:
                text = _text(cell.value)
                if text:
                    values.append({"row": cell.row, "column": cell.column, "value": text})
                    if _looks_like_row_label(text):
                        labels.append((cell.row, cell.column, text))
        sheet = WarehouseSheet(
            name=ws.title,
            max_row=ws.max_row,
            max_column=ws.max_column,
            values=values,
            merged_ranges=[str(rng) for rng in ws.merged_cells.ranges],
        )
        seen = set()
        for r, c, label in labels:
            row_number = _row_number(label)
            if (row_number, r, c) in seen:
                continue
            seen.add((row_number, r, c))
            min_row, min_col, max_row, max_col, direction, confidence, warnings = _find_extent(ws, r, c)
            wh_row = WarehouseRow(ws.title, row_number, min_row, min_col, max_row, max_col, direction, confidence, warnings=warnings)
            if confidence >= 0.6:
                count = (max_col - min_col + 1) if direction == "left_to_right" else (max_row - min_row + 1)
                for idx in range(1, count + 1):
                    x = min_col + idx - 1 if direction == "left_to_right" else min_col
                    y = min_row if direction == "left_to_right" else min_row + idx - 1
                    wh_row.potential_cells.append(WarehouseCell(ws.title, row_number, str(idx), FIRST_TIER, f"{idx}-{row_number}-{FIRST_TIER}", x, y))
            else:
                sheet.warnings.append(f"Ряд '{label}' на {r}:{c} не получил автоматические ячейки из-за низкой уверенности.")
            sheet.rows.append(wh_row)
        if not sheet.rows:
            sheet.warnings.append("На листе не найдены уверенные текстовые подписи рядов.")
        model.sheets.append(sheet)
    return model
