def build_diagnostics(model, extra=None):
    rows = []
    extras = extra or []
    rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "РџСЂРѕС‡РёС‚Р°РЅРѕ Р»РёСЃС‚РѕРІ", "Р—РЅР°С‡РµРЅРёРµ": len(model.sheets), "Р”РµС‚Р°Р»Рё": ", ".join(s.name for s in model.sheets)})
    all_rows = [r for s in model.sheets for r in s.rows]
    cells = model.cells
    rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "РџРѕС‚РµРЅС†РёР°Р»СЊРЅС‹С… СЂСЏРґРѕРІ", "Р—РЅР°С‡РµРЅРёРµ": len(all_rows), "Р”РµС‚Р°Р»Рё": ""})
    rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "РЈРІРµСЂРµРЅРЅС‹С… СЂСЏРґРѕРІ", "Р—РЅР°С‡РµРЅРёРµ": sum(1 for r in all_rows if r.confidence >= 0.6), "Р”РµС‚Р°Р»Рё": ""})
    rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "РЎРѕРјРЅРёС‚РµР»СЊРЅС‹С… СЂСЏРґРѕРІ", "Р—РЅР°С‡РµРЅРёРµ": sum(1 for r in all_rows if r.confidence < 0.6), "Р”РµС‚Р°Р»Рё": ""})
    rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "РЎРѕР·РґР°РЅРѕ СЏС‡РµРµРє", "Р—РЅР°С‡РµРЅРёРµ": len(cells), "Р”РµС‚Р°Р»Рё": ""})
    rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "РЇС‡РµРµРє РёР· С„Р°Р№Р»Р°", "Р—РЅР°С‡РµРЅРёРµ": sum(1 for c in cells if c.source == "imported_cells"), "Р”РµС‚Р°Р»Рё": ""})
    rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "Р Р°Р·РјРµС‰РµРЅРѕ С‚РѕРІР°СЂРѕРІ", "Р—РЅР°С‡РµРЅРёРµ": sum(1 for c in cells if c.item), "Р”РµС‚Р°Р»Рё": ""})
    rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "РќРµСЃРѕРїРѕСЃС‚Р°РІР»РµРЅРЅС‹Рµ/РїСЂРµРґСѓРїСЂРµР¶РґРµРЅРёСЏ", "Р—РЅР°С‡РµРЅРёРµ": len([x for x in extras if x.get("level") in {"error", "warning"}]), "Р”РµС‚Р°Р»Рё": ""})
    for sheet in model.sheets:
        for warning in sheet.warnings:
            rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": "РџСЂРµРґСѓРїСЂРµР¶РґРµРЅРёРµ Р»РёСЃС‚Р°", "Р—РЅР°С‡РµРЅРёРµ": sheet.name, "Р”РµС‚Р°Р»Рё": warning})
    for item in extras:
        rows.append({"РџРѕРєР°Р·Р°С‚РµР»СЊ": item.get("level", "info"), "Р—РЅР°С‡РµРЅРёРµ": item.get("line", ""), "Р”РµС‚Р°Р»Рё": item.get("message", "")})
    return rows
