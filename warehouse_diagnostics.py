def build_diagnostics(model, extra=None):
    rows = []
    extras = extra or []
    rows.append({"Показатель": "Прочитано листов", "Значение": len(model.sheets), "Детали": ", ".join(s.name for s in model.sheets)})
    all_rows = [r for s in model.sheets for r in s.rows]
    cells = model.cells
    rows.append({"Показатель": "Потенциальных рядов", "Значение": len(all_rows), "Детали": ""})
    rows.append({"Показатель": "Уверенных рядов", "Значение": sum(1 for r in all_rows if r.confidence >= 0.6), "Детали": ""})
    rows.append({"Показатель": "Сомнительных рядов", "Значение": sum(1 for r in all_rows if r.confidence < 0.6), "Детали": ""})
    rows.append({"Показатель": "Создано ячеек", "Значение": len(cells), "Детали": ""})
    rows.append({"Показатель": "Ячеек из файла", "Значение": sum(1 for c in cells if c.source == "imported_cells"), "Детали": ""})
    rows.append({"Показатель": "Размещено товаров", "Значение": sum(1 for c in cells if c.item), "Детали": ""})
    rows.append({"Показатель": "Несопоставленные/предупреждения", "Значение": len([x for x in extras if x.get("level") in {"error", "warning"}]), "Детали": ""})
    for sheet in model.sheets:
        for warning in sheet.warnings:
            rows.append({"Показатель": "Предупреждение листа", "Значение": sheet.name, "Детали": warning})
    for item in extras:
        rows.append({"Показатель": item.get("level", "info"), "Значение": item.get("line", ""), "Детали": item.get("message", "")})
    return rows
