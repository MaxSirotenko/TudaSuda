import html


def _coord_key(x, y):
    return (int(x), int(y))


def build_virtual_warehouse_html(sheet, scale=34):
    width = max(900, sheet.max_column * scale + 80)
    height = max(500, sheet.max_row * scale + 80)
    parts = [f"<div style='position:relative;width:{width}px;height:{height}px;background:#fafafa;border:1px solid #ddd;overflow:auto'>"]
    modeled_coords = set()
    for row in sheet.rows:
        left = row.min_col * scale
        top = row.min_row * scale
        w = max(1, row.max_col - row.min_col + 1) * scale
        h = max(1, row.max_row - row.min_row + 1) * scale
        color = "#2563eb" if row.confidence >= 0.6 else "#f59e0b"
        row_number = html.escape(row.row_number)
        parts.append(f"<div title='Ряд {row_number} confidence={row.confidence:.2f}' style='position:absolute;left:{left}px;top:{top}px;width:{w}px;height:{h}px;border:2px dashed {color};box-sizing:border-box;color:{color};font:12px Arial'>Ряд {row_number}</div>")
        for cell in row.potential_cells:
            modeled_coords.add(_coord_key(cell.x, cell.y))
            cleft = cell.x * scale
            ctop = cell.y * scale
            bg = "#bbf7d0" if cell.item else (cell.fill_color or "#e0f2fe")
            title = html.escape(
                f"Адрес: {cell.address}\n"
                f"Ряд: {cell.row_number}\n"
                f"Ярус: {cell.tier_number}\n"
                f"Товар: {cell.item or '-'}\n"
                f"Источник: {cell.source}\n"
                f"Предупреждения: {'; '.join(cell.warnings) or '-'}"
            )
            label = html.escape(cell.value or cell.cell_number)
            parts.append(f"<div title='{title}' style='position:absolute;left:{cleft}px;top:{ctop}px;width:{scale-4}px;height:{scale-4}px;background:{bg};border:1px solid #0284c7;border-radius:4px;text-align:center;line-height:{scale-4}px;font:11px Arial;overflow:hidden'>{label}</div>")

    for value in sheet.values:
        coord = _coord_key(value["column"], value["row"])
        if coord in modeled_coords:
            continue
        left = value["column"] * scale
        top = value["row"] * scale
        label = html.escape(str(value["value"]))
        parts.append(f"<div title='Подпись Excel' style='position:absolute;left:{left}px;top:{top}px;min-width:{scale-4}px;height:{scale-4}px;color:#f59e0b;font:bold 12px Arial;line-height:{scale-4}px;white-space:nowrap'>{label}</div>")

    parts.append("</div>")
    return "".join(parts)
