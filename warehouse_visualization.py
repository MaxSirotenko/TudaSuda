import html


def build_virtual_warehouse_html(sheet, scale=34):
    width = max(900, sheet.max_column * scale + 80)
    height = max(500, sheet.max_row * scale + 80)
    parts = [f"<div style='position:relative;width:{width}px;height:{height}px;background:#fafafa;border:1px solid #ddd;overflow:auto'>"]
    for row in sheet.rows:
        left = row.min_col * scale
        top = row.min_row * scale
        w = max(1, row.max_col - row.min_col + 1) * scale
        h = max(1, row.max_row - row.min_row + 1) * scale
        color = "#2563eb" if row.confidence >= 0.6 else "#f59e0b"
        parts.append(f"<div title='Ряд {html.escape(row.row_number)} confidence={row.confidence:.2f}' style='position:absolute;left:{left}px;top:{top}px;width:{w}px;height:{h}px;border:2px dashed {color};box-sizing:border-box;color:{color};font:12px Arial'>Ряд {html.escape(row.row_number)}</div>")
        for cell in row.potential_cells:
            cleft = cell.x * scale
            ctop = cell.y * scale
            bg = "#bbf7d0" if cell.item else "#e0f2fe"
            title = html.escape(f"Адрес: {cell.address}\nРяд: {cell.row_number}\nЯрус: {cell.tier_number}\nТовар: {cell.item or '-'}\nИсточник: {cell.source}\nПредупреждения: {'; '.join(cell.warnings) or '-'}")
            label = html.escape(cell.cell_number)
            parts.append(f"<div title='{title}' style='position:absolute;left:{cleft}px;top:{ctop}px;width:{scale-4}px;height:{scale-4}px;background:{bg};border:1px solid #0284c7;border-radius:4px;text-align:center;line-height:{scale-4}px;font:11px Arial;overflow:hidden'>{label}</div>")
    parts.append("</div>")
    return "".join(parts)
