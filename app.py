from streamlit_image_coordinates import streamlit_image_coordinates
import streamlit as st
import pandas as pd
import fitz
from PIL import Image
from io import BytesIO

import streamlit.components.v1 as components

from warehouse_diagnostics import build_diagnostics
from warehouse_excel_parser import parse_warehouse_excel
from warehouse_placement import (
    apply_cell_addresses,
    apply_placements,
    import_cell_addresses,
    import_placements,
)
from warehouse_visualization import build_virtual_warehouse_html

from row_constructor import (
    DEFAULT_SETTINGS,
    apply_1c_cell_numbers,
    build_model,
    build_visual_map_html,
    import_1c_cells_from_excel,
    import_segments_from_excel,
    normalize_segments,
)

st.set_page_config(page_title="РЎРёРјСѓР»СЏС‚РѕСЂ СЃР±РѕСЂРєРё", layout="wide")

st.title("РЎРёРјСѓР»СЏС‚РѕСЂ СЃРєРѕСЂРѕСЃС‚Рё СЃР±РѕСЂРєРё")

# ---------- С„СѓРЅРєС†РёРё ----------

def make_excel_file(sheets: dict):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=sheet_name)

    output.seek(0)
    return output


def download_excel_button(label, sheets, file_name):
    excel_file = make_excel_file(sheets)

    st.download_button(
        label=label,
        data=excel_file,
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )




def render_virtual_warehouse_excel(show_header=True):
    if show_header:
        st.header("Р’РёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ РїРѕ Excel-СЃС…РµРјРµ")
    st.caption(
        "РЈРїСЂРѕС‰С‘РЅРЅС‹Р№ СЂРµР¶РёРј С‡РёС‚Р°РµС‚ РІСЃРµ Р»РёСЃС‚С‹ .xlsx Рё СЃРЅР°С‡Р°Р»Р° СЃС‚СЂРѕРёС‚ СЏС‡РµР№РєРё РїРѕ С†РІРµС‚РЅРѕР№ "
        "Р·Р°Р»РёРІРєРµ РІ Excel. РўР°Р±Р»РёС‡РЅС‹Рµ РєРѕР»РѕРЅРєРё row_number/pallet_count РґР»СЏ СЌС‚РѕРіРѕ СЂРµР¶РёРјР° РЅРµ РЅСѓР¶РЅС‹."
    )

    with st.expander("Р¤РѕСЂРјР°С‚С‹ С„Р°Р№Р»РѕРІ", expanded=True):
        st.markdown(
            """
            **РЎС…РµРјР° СЃРєР»Р°РґР°:** Р»СЋР±РѕР№ `.xlsx` СЃ РІРёР·СѓР°Р»СЊРЅРѕР№ СЃС…РµРјРѕР№. Р•СЃР»Рё СЏС‡РµР№РєРё РЅР° СЃС…РµРјРµ Р·Р°РєСЂР°С€РµРЅС‹ С†РІРµС‚РѕРј, РєР°Р¶РґР°СЏ С†РІРµС‚РЅР°СЏ Excel-СЏС‡РµР№РєР° СЃС‚Р°РЅРµС‚ РІРёСЂС‚СѓР°Р»СЊРЅРѕР№ СЏС‡РµР№РєРѕР№ СЃРєР»Р°РґР°. РћР±СЂР°Р±Р°С‚С‹РІР°СЋС‚СЃСЏ РІСЃРµ Р»РёСЃС‚С‹.

            **Р¤Р°Р№Р» СЏС‡РµРµРє, РѕРїС†РёРѕРЅР°Р»СЊРЅРѕ:** РєРѕР»РѕРЅРєРё `cell`/`СЏС‡РµР№РєР°`, `row`/`СЂСЏРґ`, `tier`/`СЏСЂСѓСЃ`
            РёР»Рё РїРѕР»РЅР°СЏ РєРѕР»РѕРЅРєР° `address`/`Р°РґСЂРµСЃ` РІ С„РѕСЂРјР°С‚Рµ `СЏС‡РµР№РєР°-СЂСЏРґ-СЏСЂСѓСЃ`.

            **Р¤Р°Р№Р» СЂР°Р·РјРµС‰РµРЅРёСЏ, РѕРїС†РёРѕРЅР°Р»СЊРЅРѕ:** `address`/`Р°РґСЂРµСЃ` + `item`/`С‚РѕРІР°СЂ`/`РЅРѕРјРµРЅРєР»Р°С‚СѓСЂР°`
            РёР»Рё СЂР°Р·РґРµР»СЊРЅС‹Рµ РєРѕР»РѕРЅРєРё `cell`, `row`, `tier`, `item`.
            РќР° СЌС‚РѕРј СЌС‚Р°РїРµ РёСЃРїРѕР»СЊР·СѓСЋС‚СЃСЏ С‚РѕР»СЊРєРѕ Р°РґСЂРµСЃР° РїРµСЂРІРѕРіРѕ СЏСЂСѓСЃР°; РѕС‚СЃСѓС‚СЃС‚РІРёРµ СЏСЂСѓСЃР° СЃС‡РёС‚Р°РµС‚СЃСЏ РїРµСЂРІС‹Рј СЏСЂСѓСЃРѕРј Рё РїРѕРїР°РґР°РµС‚ РІ РґРёР°РіРЅРѕСЃС‚РёРєСѓ.
            """
        )

    schema_file = st.file_uploader(
        "Excel-СЃС…РµРјР° СЃРєР»Р°РґР°",
        type=["xlsx"],
        key="virtual_warehouse_schema_upload",
    )
    cell_file = st.file_uploader(
        "Р¤Р°Р№Р» РЅРѕРјРµСЂРѕРІ СЏС‡РµРµРє (РЅРµРѕР±СЏР·Р°С‚РµР»СЊРЅРѕ)",
        type=["xlsx", "csv"],
        key="virtual_warehouse_cells_upload",
    )
    placement_file = st.file_uploader(
        "Р¤Р°Р№Р» СЂР°Р·РјРµС‰РµРЅРёСЏ С‚РѕРІР°СЂРѕРІ (РЅРµРѕР±СЏР·Р°С‚РµР»СЊРЅРѕ)",
        type=["xlsx", "csv"],
        key="virtual_warehouse_placements_upload",
    )

    if st.button("РџРѕСЃС‚СЂРѕРёС‚СЊ СЃРєР»Р°Рґ РїРѕ С†РІРµС‚Р°Рј Excel", disabled=schema_file is None):
        diagnostics = []
        try:
            model = parse_warehouse_excel(schema_file)
            if cell_file is not None:
                addresses_by_row, cell_diagnostics = import_cell_addresses(cell_file)
                diagnostics.extend(cell_diagnostics)
                diagnostics.extend(apply_cell_addresses(model, addresses_by_row))
            if placement_file is not None:
                placements, placement_diagnostics = import_placements(placement_file)
                diagnostics.extend(placement_diagnostics)
                diagnostics.extend(apply_placements(model, placements))
            st.session_state["virtual_warehouse_model"] = model
            st.session_state["virtual_warehouse_diagnostics"] = diagnostics
            st.success(f"Р’РёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ РїРѕСЃС‚СЂРѕРµРЅ: {len(model.sheets)} Р»РёСЃС‚РѕРІ, {len(model.cells)} СЏС‡РµРµРє.")
        except Exception as exc:
            st.error(f"РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕСЃС‚СЂРѕРёС‚СЊ РІРёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ: {exc}")

    model = st.session_state.get("virtual_warehouse_model")
    if model is None:
        st.info("Р—Р°РіСЂСѓР·РёС‚Рµ Excel-СЃС…РµРјСѓ СЃРєР»Р°РґР° Рё РЅР°Р¶РјРёС‚Рµ РєРЅРѕРїРєСѓ РїРѕСЃС‚СЂРѕРµРЅРёСЏ.")
    else:
        diagnostics = st.session_state.get("virtual_warehouse_diagnostics", [])
        sheet_names = [sheet.name for sheet in model.sheets]
        selected_sheet_name = st.selectbox("Р›РёСЃС‚ СЃРєР»Р°РґР°", sheet_names, key="virtual_warehouse_sheet_select")
        selected_sheet = next(sheet for sheet in model.sheets if sheet.name == selected_sheet_name)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Р›РёСЃС‚РѕРІ", len(model.sheets))
        m2.metric("Р СЏРґРѕРІ РЅР° Р»РёСЃС‚Рµ", len(selected_sheet.rows))
        m3.metric("РЇС‡РµРµРє РІСЃРµРіРѕ", len(model.cells))
        m4.metric("РўРѕРІР°СЂРѕРІ СЂР°Р·РјРµС‰РµРЅРѕ", sum(1 for cell in model.cells if cell.item))

        tab_map, tab_rows, tab_cells, tab_diag = st.tabs(["Р’РёР·СѓР°Р»РёР·Р°С†РёСЏ", "Р СЏРґС‹", "РЇС‡РµР№РєРё", "Р”РёР°РіРЅРѕСЃС‚РёРєР°"])
        with tab_map:
            scale = st.slider("РњР°СЃС€С‚Р°Р± СЃРµС‚РєРё", min_value=18, max_value=60, value=34, step=2)
            components.html(build_virtual_warehouse_html(selected_sheet, scale), height=760, scrolling=True)
        with tab_rows:
            st.dataframe(
                pd.DataFrame([
                    {
                        "sheet": row.sheet_name,
                        "row_number": row.row_number,
                        "min_row": row.min_row,
                        "min_col": row.min_col,
                        "max_row": row.max_row,
                        "max_col": row.max_col,
                        "direction": row.direction,
                        "confidence": row.confidence,
                        "cells": len(row.potential_cells),
                        "warnings": "; ".join(row.warnings),
                    }
                    for row in selected_sheet.rows
                ]),
                use_container_width=True,
            )
        with tab_cells:
            st.dataframe(
                pd.DataFrame([
                    {
                        "sheet": cell.sheet_name,
                        "address": cell.address,
                        "cell_number": cell.cell_number,
                        "row_number": cell.row_number,
                        "tier_number": cell.tier_number,
                        "item": cell.item,
                        "source": cell.source,
                        "warnings": "; ".join(cell.warnings),
                    }
                    for row in selected_sheet.rows for cell in row.potential_cells
                ]),
                use_container_width=True,
            )
        with tab_diag:
            diag_df = pd.DataFrame(build_diagnostics(model, diagnostics))
            st.dataframe(diag_df, use_container_width=True)
            st.download_button(
                "РЎРєР°С‡Р°С‚СЊ РґРёР°РіРЅРѕСЃС‚РёРєСѓ CSV",
                diag_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="virtual_warehouse_diagnostics.csv",
                mime="text/csv",
            )

        if st.button("РћС‡РёСЃС‚РёС‚СЊ РІРёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ"):
            for key in ["virtual_warehouse_model", "virtual_warehouse_diagnostics"]:
                st.session_state.pop(key, None)
            st.rerun()

# ---------- РјРµРЅСЋ ----------

st.sidebar.header("Р Р°Р·РґРµР»С‹")

page = st.sidebar.radio(
    "Р’С‹Р±РµСЂРёС‚Рµ СЂР°Р·РґРµР»",
    [
    "РЁР°Р±Р»РѕРЅС‹ С„Р°Р№Р»РѕРІ",
    "Р—Р°РіСЂСѓР·РєР° РґР°РЅРЅС‹С…",
    "РљР°СЂС‚Р° Р Р¦",
    "РљР°СЂС‚Р° СЃРєР»Р°РґР°",
    "Р’РёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ Excel",
    "Р Р°СЃС‡РµС‚ РјР°СЂС€СЂСѓС‚РѕРІ"
]
)

# ---------- С€Р°Р±Р»РѕРЅС‹ ----------

if page == "РЁР°Р±Р»РѕРЅС‹ С„Р°Р№Р»РѕРІ":
    st.header("РЁР°Р±Р»РѕРЅС‹ Excel-С„Р°Р№Р»РѕРІ")

    st.write("РЎРєР°С‡Р°Р№ С€Р°Р±Р»РѕРЅС‹, Р·Р°РїРѕР»РЅРё РёС… СЃРІРѕРёРјРё РґР°РЅРЅС‹РјРё Рё РїРѕС‚РѕРј Р·Р°РіСЂСѓР·Рё РІ РїСЂРёР»РѕР¶РµРЅРёРµ.")

    # 1. РЁР°Р±Р»РѕРЅ СЃРєР»Р°РґРѕРІ
    warehouses_df = pd.DataFrame({
        "warehouse_id": ["veshki_day", "veshki_night"],
        "warehouse_name": ["Р”РЅРµРІРЅРѕР№ Р’РµС€РєРё", "РќРѕС‡РЅРѕР№ Р’РµС€РєРё"],
        "width_mm": [80000, 80000],
        "height_mm": [50000, 50000],
        "comment": ["РћСЃРЅРѕРІРЅРѕР№ РґРЅРµРІРЅРѕР№ СЃРєР»Р°Рґ", "РџСЂРёРјРµСЂ РІС‚РѕСЂРѕРіРѕ СЃРєР»Р°РґР°"]
    })

    # 2. РЁР°Р±Р»РѕРЅ РѕР±СЉРµРєС‚РѕРІ РєР°СЂС‚С‹
    map_objects_df = pd.DataFrame({
        "warehouse_id": [
            "veshki_day",
            "veshki_day",
            "veshki_day",
            "veshki_day",
            "veshki_day"
        ],
        "object_type": [
            "cell",
            "cell",
            "cell",
            "aisle",
            "obstacle"
        ],
        "object_id": [
            "24-09-01",
            "25-09-01",
            "26-09-01",
            "aisle_001",
            "column_001"
        ],
        "x_mm": [12000, 12800, 13600, 0, 20000],
        "y_mm": [4500, 4500, 4500, 3000, 10000],
        "width_mm": [800, 800, 800, 80000, 400],
        "height_mm": [1200, 1200, 1200, 2500, 400],
        "row_num": [9, 9, 9, None, None],
        "cell_num": [24, 25, 26, None, None],
        "level_num": [1, 1, 1, None, None],
        "side": ["left", "left", "left", None, None],
        "comment": [
            "РЇС‡РµР№РєР° С…СЂР°РЅРµРЅРёСЏ",
            "РЇС‡РµР№РєР° С…СЂР°РЅРµРЅРёСЏ",
            "РЇС‡РµР№РєР° С…СЂР°РЅРµРЅРёСЏ",
            "РџСЂРѕС…РѕРґ",
            "РљРѕР»РѕРЅРЅР°"
        ]
    })

    # 3. РЁР°Р±Р»РѕРЅ СЂР°СЃС…РѕРґРЅРёРєРѕРІ
    orders_df = pd.DataFrame({
        "period": ["09.06.2026 6:12:16", "09.06.2026 6:12:16"],
        "warehouse_name": ["Р”РЅРµРІРЅРѕР№ Р’РµС€РєРё", "Р”РЅРµРІРЅРѕР№ Р’РµС€РєРё"],
        "order_id": [
            "Р Р°СЃС…РѕРґРЅС‹Р№ РѕСЂРґРµСЂ Р’20539431",
            "Р Р°СЃС…РѕРґРЅС‹Р№ РѕСЂРґРµСЂ Р’20539431"
        ],
        "nomenclature": [
            "РџРёРІРѕ Р‘РµР»СЊРіРёР№СЃРєРѕРµ Р±РµР·Р°Р»РєРѕРіРѕР»СЊРЅРѕРµ, 500 РјР»",
            "Р’РѕРґР° СЂРѕРґРЅРёРєРѕРІР°СЏ РіР°Р·РёСЂРѕРІР°РЅРЅР°СЏ, 1,5 Р»"
        ],
        "characteristic": ["Р’РђР РќРР¦Рђ РћРћРћ", "РЎР’Р•РўР›РћРЇР  РћРћРћ"],
        "production_date": ["14.04.2026", "17.05.2026"],
        "cell_id": ["24-09-01", "19-58-01"],
        "quantity": [12, 30],
        "cell_balance": [384, 372],
        "print_order": [53722, 53702]
    })

    # 4. РЁР°Р±Р»РѕРЅ РїР°СЂР°РјРµС‚СЂРѕРІ СЃР±РѕСЂС‰РёРєР°
    picker_params_df = pd.DataFrame({
        "parameter": [
            "walk_speed_m_s",
            "pick_time_per_line_sec",
            "scan_time_per_line_sec",
            "start_x_mm",
            "start_y_mm",
            "finish_x_mm",
            "finish_y_mm"
        ],
        "value": [
            1.2,
            5,
            1,
            0,
            0,
            0,
            0
        ],
        "comment": [
            "РЎРєРѕСЂРѕСЃС‚СЊ РґРІРёР¶РµРЅРёСЏ СЃР±РѕСЂС‰РёРєР°, Рј/СЃ",
            "Р’СЂРµРјСЏ РЅР° РїРѕРґР±РѕСЂ РѕРґРЅРѕР№ СЃС‚СЂРѕРєРё",
            "Р’СЂРµРјСЏ РЅР° СЃРєР°РЅРёСЂРѕРІР°РЅРёРµ РѕРґРЅРѕР№ СЃС‚СЂРѕРєРё",
            "X СЃС‚Р°СЂС‚РѕРІРѕР№ С‚РѕС‡РєРё",
            "Y СЃС‚Р°СЂС‚РѕРІРѕР№ С‚РѕС‡РєРё",
            "X С‚РѕС‡РєРё Р·Р°РІРµСЂС€РµРЅРёСЏ",
            "Y С‚РѕС‡РєРё Р·Р°РІРµСЂС€РµРЅРёСЏ"
        ]
    })

    download_excel_button(
        "РЎРєР°С‡Р°С‚СЊ С€Р°Р±Р»РѕРЅ СЃРєР»Р°РґРѕРІ",
        {"warehouses": warehouses_df},
        "template_warehouses.xlsx"
    )

    download_excel_button(
        "РЎРєР°С‡Р°С‚СЊ С€Р°Р±Р»РѕРЅ РєР°СЂС‚С‹ СЃРєР»Р°РґР°",
        {"map_objects": map_objects_df},
        "template_warehouse_map.xlsx"
    )

    download_excel_button(
        "РЎРєР°С‡Р°С‚СЊ С€Р°Р±Р»РѕРЅ СЂР°СЃС…РѕРґРЅРёРєРѕРІ",
        {"orders": orders_df},
        "template_orders.xlsx"
    )

    download_excel_button(
        "РЎРєР°С‡Р°С‚СЊ С€Р°Р±Р»РѕРЅ РїР°СЂР°РјРµС‚СЂРѕРІ СЃР±РѕСЂС‰РёРєР°",
        {"picker_params": picker_params_df},
        "template_picker_params.xlsx"
    )

    download_excel_button(
        "РЎРєР°С‡Р°С‚СЊ РІСЃРµ С€Р°Р±Р»РѕРЅС‹ РѕРґРЅРёРј С„Р°Р№Р»РѕРј",
        {
            "warehouses": warehouses_df,
            "map_objects": map_objects_df,
            "orders": orders_df,
            "picker_params": picker_params_df
        },
        "templates_all.xlsx"
    )

# ---------- Р·Р°РіСЂСѓР·РєР° РґР°РЅРЅС‹С… ----------

elif page == "Р—Р°РіСЂСѓР·РєР° РґР°РЅРЅС‹С…":
    st.header("Р—Р°РіСЂСѓР·РєР° РґР°РЅРЅС‹С…")

    uploaded_file = st.file_uploader(
        "Р—Р°РіСЂСѓР·РёС‚Рµ Р·Р°РїРѕР»РЅРµРЅРЅС‹Р№ Excel-С„Р°Р№Р»",
        type=["xlsx"]
    )

    if uploaded_file:
        xls = pd.ExcelFile(uploaded_file)

        st.subheader("Р›РёСЃС‚С‹ РІ С„Р°Р№Р»Рµ")
        st.write(xls.sheet_names)

        selected_sheet = st.selectbox(
            "Р’С‹Р±РµСЂРёС‚Рµ Р»РёСЃС‚ РґР»СЏ РїСЂРѕСЃРјРѕС‚СЂР°",
            xls.sheet_names
        )

        df = pd.read_excel(uploaded_file, sheet_name=selected_sheet)

        st.subheader("Р”Р°РЅРЅС‹Рµ")
        st.dataframe(df)

        st.session_state[selected_sheet] = df

        st.success(f"Р›РёСЃС‚ '{selected_sheet}' Р·Р°РіСЂСѓР¶РµРЅ РІ РїСЂРёР»РѕР¶РµРЅРёРµ")

# ---------- РєР°СЂС‚Р° Р Р¦ ----------

elif page == "РљР°СЂС‚Р° Р Р¦":

    st.header("РљР°СЂС‚Р° СЂР°СЃРїСЂРµРґРµР»РёС‚РµР»СЊРЅРѕРіРѕ С†РµРЅС‚СЂР°")

    uploaded_pdf = st.file_uploader(
        "Р—Р°РіСЂСѓР·РёС‚Рµ PDF-РїР»Р°РЅ Р Р¦",
        type=["pdf"]
    )

    if uploaded_pdf:
        st.session_state["rc_pdf_bytes"] = uploaded_pdf.read()
        st.session_state["rc_pdf_name"] = uploaded_pdf.name

    if "rc_pdf_bytes" not in st.session_state:

        st.info("Р—Р°РіСЂСѓР·РёС‚Рµ PDF РїР»Р°РЅ СЃРєР»Р°РґР°")

    else:

        st.success(
            f"Р—Р°РіСЂСѓР¶РµРЅ С„Р°Р№Р»: {st.session_state['rc_pdf_name']}"
        )

        pdf_doc = fitz.open(
            stream=st.session_state["rc_pdf_bytes"],
            filetype="pdf"
        )

        page_num = st.number_input(
            "РЎС‚СЂР°РЅРёС†Р°",
            min_value=1,
            max_value=len(pdf_doc),
            value=1
        )

        zoom = st.slider(
            "РњР°СЃС€С‚Р°Р±",
            min_value=0.5,
            max_value=8.0,
            value=2.0,
            step=0.5
        )

        page_pdf = pdf_doc[page_num - 1]

        matrix = fitz.Matrix(zoom, zoom)

        pix = page_pdf.get_pixmap(
            matrix=matrix
        )

        img = Image.frombytes(
            "RGB",
            [pix.width, pix.height],
            pix.samples
        )

        st.write(
            "РљР»РёРєРЅРё РїРѕ РїР»Р°РЅСѓ РґР»СЏ РїРѕР»СѓС‡РµРЅРёСЏ РєРѕРѕСЂРґРёРЅР°С‚"
        )

        clicked_point = streamlit_image_coordinates(
            img,
            key=f"pdf_click_{page_num}_{zoom}"
        )

        if clicked_point:

            st.session_state["last_click_x"] = clicked_point["x"]
            st.session_state["last_click_y"] = clicked_point["y"]

            col1, col2 = st.columns(2)

            with col1:
                st.metric(
                    "X",
                    clicked_point["x"]
                )

            with col2:
                st.metric(
                    "Y",
                    clicked_point["y"]
                )

        st.divider()

        st.subheader("РњР°СЃС€С‚Р°Р± РїР»Р°РЅР°")

        col1, col2 = st.columns(2)

        with col1:
            if st.button("Р—Р°РїРѕРјРЅРёС‚СЊ С‚РѕС‡РєСѓ A"):

                if "last_click_x" in st.session_state:

                    st.session_state["point_a"] = (
                        st.session_state["last_click_x"],
                        st.session_state["last_click_y"]
                    )

        with col2:
            if st.button("Р—Р°РїРѕРјРЅРёС‚СЊ С‚РѕС‡РєСѓ B"):

                if "last_click_x" in st.session_state:

                    st.session_state["point_b"] = (
                        st.session_state["last_click_x"],
                        st.session_state["last_click_y"]
                    )

        point_a = st.session_state.get("point_a")
        point_b = st.session_state.get("point_b")

        st.write("РўРѕС‡РєР° A:", point_a)
        st.write("РўРѕС‡РєР° B:", point_b)

        real_length_mm = st.number_input(
            "Р РµР°Р»СЊРЅР°СЏ РґР»РёРЅР° РјРµР¶РґСѓ С‚РѕС‡РєР°РјРё (РјРј)",
            min_value=1,
            value=12000
        )

        if point_a and point_b:

            dx = point_b[0] - point_a[0]
            dy = point_b[1] - point_a[1]

            distance_px = (
                dx ** 2 + dy ** 2
            ) ** 0.5

            st.metric(
                "Р Р°СЃСЃС‚РѕСЏРЅРёРµ РІ РїРёРєСЃРµР»СЏС…",
                f"{distance_px:.2f}"
            )

            if distance_px == 0:
                st.warning("РўРѕС‡РєРё A Рё B СЃРѕРІРїР°РґР°СЋС‚. Р’С‹Р±РµСЂРё РґРІРµ СЂР°Р·РЅС‹Рµ С‚РѕС‡РєРё РґР»СЏ СЂР°СЃС‡РµС‚Р° РјР°СЃС€С‚Р°Р±Р°.")
            else:
                mm_per_px = (
                    real_length_mm /
                    distance_px
                )

                st.metric(
                    "РјРј РЅР° РїРёРєСЃРµР»СЊ",
                    f"{mm_per_px:.4f}"
                )

                st.session_state[
                    "mm_per_px"
                ] = mm_per_px

        if st.button("РћС‡РёСЃС‚РёС‚СЊ PDF"):

            for key in [
                "rc_pdf_bytes",
                "rc_pdf_name",
                "point_a",
                "point_b",
                "mm_per_px"
            ]:

                if key in st.session_state:
                    del st.session_state[key]

            st.rerun()
# ---------- РєР°СЂС‚Р° СЃРєР»Р°РґР° ----------

elif page == "РљР°СЂС‚Р° СЃРєР»Р°РґР°":
    st.header("РљР°СЂС‚Р° СЃРєР»Р°РґР°")

    st.warning(
        "РќРѕРІС‹Р№ РјРѕРґСѓР»СЊ Excel-СЃС…РµРјС‹ СЃРєР»Р°РґР° РґРѕСЃС‚СѓРїРµРЅ РїСЂСЏРјРѕ Р·РґРµСЃСЊ РЅРёР¶Рµ Рё РѕС‚РґРµР»СЊРЅС‹Рј РїСѓРЅРєС‚РѕРј РјРµРЅСЋ "
        "В«Р’РёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ ExcelВ». Р•СЃР»Рё РїСѓРЅРєС‚ РјРµРЅСЋ РЅРµ РїРѕСЏРІРёР»СЃСЏ, РїРµСЂРµР·Р°РїСѓСЃС‚РёС‚Рµ Streamlit/start.cmd."
    )

    st.subheader("Р’РёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ Excel вЂ” РїРѕСЃС‚СЂРѕРёС‚СЊ РїРѕ РІРёР·СѓР°Р»СЊРЅРѕР№ Excel-СЃС…РµРјРµ")
    render_virtual_warehouse_excel(show_header=False)
    st.divider()

    st.info(
        "РљР°СЂС‚Сѓ СЃРєР»Р°РґР° С‚РµРїРµСЂСЊ РјРѕР¶РЅРѕ Р·Р°РіСЂСѓР·РёС‚СЊ РїСЂСЏРјРѕ Р·РґРµСЃСЊ: СЃРЅР°С‡Р°Р»Р° Excel СЃРѕ СЃС…РµРјРѕР№ СЂСЏРґРѕРІ, "
        "Р·Р°С‚РµРј РїСЂРё РЅРµРѕР±С…РѕРґРёРјРѕСЃС‚Рё Excel-РІС‹РіСЂСѓР·РєСѓ 1РЎ СЃ С„Р°РєС‚РёС‡РµСЃРєРёРјРё Р°РґСЂРµСЃР°РјРё СЏС‡РµРµРє."
    )

    with st.expander("РљР°РєРёРµ РєРѕР»РѕРЅРєРё РЅСѓР¶РЅС‹ РІ Excel", expanded=True):
        st.markdown(
            """
            **РЎС…РµРјР° СЂСЏРґРѕРІ:** РѕР±СЏР·Р°С‚РµР»СЊРЅС‹ `Р СЏРґ` Рё `РљРѕР»-РІРѕ СЏС‡РµРµРє` / `РљРѕР»РёС‡РµСЃС‚РІРѕ СЏС‡РµРµРє`.
            Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅРѕ: `РЎРєР»Р°Рґ`, `Р§Р°СЃС‚СЊ СЂСЏРґР°`, `Р”Р»РёРЅР° СЏС‡РµР№РєРё РјРј`, `РЁРёСЂРёРЅР° СЏС‡РµР№РєРё РјРј`,
            `Р—Р°Р·РѕСЂ РјРј`, `РџСЂРѕРµР·Рґ РјРј`, `РџРѕРІРѕСЂРѕС‚ РјРј`, `РЎР»РµРґСѓСЋС‰РёР№ СЂСЏРґ`, `РљРѕРјРјРµРЅС‚Р°СЂРёР№`.

            **Р’С‹РіСЂСѓР·РєР° 1РЎ:** РѕР±СЏР·Р°С‚РµР»СЊРЅС‹ `Р СЏРґ` Рё `РЇС‡РµР№РєР°`.
            Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅРѕ: `РЎРєР»Р°Рґ`, `РђРґСЂРµСЃ СЏС‡РµР№РєРё` / `РЎРєР»Р°РґСЃРєР°СЏ СЏС‡РµР№РєР°`.
            """
        )

    upload_col, one_c_col = st.columns(2)

    with upload_col:
        default_zone = st.text_input(
            "РЎРєР»Р°Рґ/Р·РѕРЅР° РґР»СЏ СЃС‚СЂРѕРє Р±РµР· РєРѕР»РѕРЅРєРё СЃРєР»Р°РґР°",
            value="РљР°СЂС‚Р° СЃРєР»Р°РґР°",
            key="warehouse_map_default_zone",
        )
        layout_file = st.file_uploader(
            "Р—Р°РіСЂСѓР·РёС‚СЊ Excel СЃС…РµРјС‹ СЃРєР»Р°РґР°",
            type=["xlsx"],
            key="warehouse_map_layout_upload",
        )
        if st.button("РџРѕСЃС‚СЂРѕРёС‚СЊ РєР°СЂС‚Сѓ СЃРєР»Р°РґР°", disabled=layout_file is None):
            try:
                sheet_name, segments = import_segments_from_excel(
                    layout_file,
                    default_zone.strip() or "РљР°СЂС‚Р° СЃРєР»Р°РґР°",
                )
                st.session_state["warehouse_map_segments"] = segments
                st.session_state["warehouse_map_layout_sheet"] = sheet_name
                st.success(
                    f"РЎС…РµРјР° Р·Р°РіСЂСѓР¶РµРЅР°: {len(segments)} СЃС‚СЂРѕРє РёР· Р»РёСЃС‚Р° В«{sheet_name}В»."
                )
            except Exception as exc:
                try:
                    layout_file.seek(0)
                    model = parse_warehouse_excel(layout_file)
                    st.session_state["virtual_warehouse_model"] = model
                    st.session_state["virtual_warehouse_diagnostics"] = [
                        {
                            "level": "warning",
                            "message": (
                                "Р¤Р°Р№Р» РЅРµ РїРѕС…РѕР¶ РЅР° С‚Р°Р±Р»РёС‡РЅСѓСЋ СЃС…РµРјСѓ СЂСЏРґРѕРІ, РїРѕСЌС‚РѕРјСѓ РѕРЅ РѕС‚РєСЂС‹С‚ "
                                "РІ СѓРїСЂРѕС‰С‘РЅРЅРѕРј СЂРµР¶РёРјРµ РїРѕ С†РІРµС‚РЅРѕР№ Excel-СЂР°Р·РјРµС‚РєРµ."
                            ),
                        }
                    ]
                    st.warning(
                        "РўР°Р±Р»РёС‡РЅС‹Рµ РєРѕР»РѕРЅРєРё `Р СЏРґ` Рё `РљРѕР»-РІРѕ СЏС‡РµРµРє` РЅРµ РЅР°Р№РґРµРЅС‹. "
                        "РЇ РїРѕСЃС‚СЂРѕРёР» СЃРєР»Р°Рґ РїРѕ С†РІРµС‚РЅС‹Рј СЏС‡РµР№РєР°Рј Excel РІ Р±Р»РѕРєРµ РІС‹С€Рµ."
                    )
                except Exception as fallback_exc:
                    st.error(f"РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЃС…РµРјСѓ СЃРєР»Р°РґР°: {exc}. РЈРїСЂРѕС‰С‘РЅРЅС‹Р№ СЂРµР¶РёРј С‚РѕР¶Рµ РЅРµ СЃСЂР°Р±РѕС‚Р°Р»: {fallback_exc}")

    with one_c_col:
        one_c_file = st.file_uploader(
            "Р—Р°РіСЂСѓР·РёС‚СЊ Excel РІС‹РіСЂСѓР·РєРё 1РЎ",
            type=["xlsx"],
            key="warehouse_map_1c_upload",
        )
        if st.button("РџСЂРёРјРµРЅРёС‚СЊ РЅРѕРјРµСЂР° РёР· 1РЎ", disabled=one_c_file is None):
            try:
                sheet_name, one_c_cells = import_1c_cells_from_excel(one_c_file)
                st.session_state["warehouse_map_1c_cells"] = one_c_cells
                st.session_state["warehouse_map_1c_sheet"] = sheet_name
                st.success(
                    f"Р’С‹РіСЂСѓР·РєР° 1РЎ Р·Р°РіСЂСѓР¶РµРЅР°: {len(one_c_cells)} СЏС‡РµРµРє РёР· Р»РёСЃС‚Р° В«{sheet_name}В»."
                )
            except Exception as exc:
                st.error(f"РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РІС‹РіСЂСѓР·РєСѓ 1РЎ: {exc}")

    if "warehouse_map_segments" not in st.session_state:
        st.warning("Р—Р°РіСЂСѓР·РёС‚Рµ Excel СЃС…РµРјС‹ СЃРєР»Р°РґР° РІ Р±Р»РѕРєРµ РІС‹С€Рµ, С‡С‚РѕР±С‹ РїРѕСЃС‚СЂРѕРёС‚СЊ РєР°СЂС‚Сѓ.")
    else:
        segments = st.session_state["warehouse_map_segments"]
        one_c_cells = st.session_state.get("warehouse_map_1c_cells")

        st.subheader("Р—Р°РіСЂСѓР¶РµРЅРЅР°СЏ СЃС…РµРјР° СЂСЏРґРѕРІ")
        st.caption(
            f"Р›РёСЃС‚ СЃС…РµРјС‹: {st.session_state.get('warehouse_map_layout_sheet', 'РЅРµ СѓРєР°Р·Р°РЅ')}"
        )
        st.dataframe(segments, use_container_width=True)

        settings = DEFAULT_SETTINGS.copy()
        calc_segments = normalize_segments(segments)
        (
            cells,
            gaps,
            passages,
            transitions,
            row_summary,
            zone_summary,
        ) = build_model(calc_segments, settings)
        cells = apply_1c_cell_numbers(cells, one_c_cells)

        metric1, metric2, metric3, metric4 = st.columns(4)
        metric1.metric("Р СЏРґРѕРІ", len(row_summary))
        metric2.metric("РЇС‡РµРµРє", len(cells))
        metric3.metric("РџСЂРѕРµР·РґРѕРІ", len(passages))
        metric4.metric(
            "РњР°СЂС€СЂСѓС‚, Рј",
            round(float(zone_summary["total_route_m"].sum()), 1) if not zone_summary.empty else 0,
        )

        map_scale = st.slider(
            "РњР°СЃС€С‚Р°Р± РѕС‚СЂРёСЃРѕРІРєРё",
            min_value=0.02,
            max_value=0.20,
            value=0.08,
            step=0.01,
            key="warehouse_map_scale",
        )
        components.html(
            build_visual_map_html(cells, passages, row_summary, map_scale),
            height=760,
            scrolling=True,
        )

        with st.expander("РЎРІРѕРґРєР° РїРѕ СЃРєР»Р°РґСѓ"):
            st.dataframe(zone_summary, use_container_width=True)

        with st.expander("РЎРІРѕРґРєР° РїРѕ СЂСЏРґР°Рј"):
            st.dataframe(row_summary, use_container_width=True)

        with st.expander("РџРµСЂРІС‹Рµ 1000 СЏС‡РµРµРє"):
            st.dataframe(cells.head(1000), use_container_width=True)

        if st.button("РћС‡РёСЃС‚РёС‚СЊ Р·Р°РіСЂСѓР¶РµРЅРЅСѓСЋ РєР°СЂС‚Сѓ СЃРєР»Р°РґР°"):
            for key in [
                "warehouse_map_segments",
                "warehouse_map_layout_sheet",
                "warehouse_map_1c_cells",
                "warehouse_map_1c_sheet",
            ]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

# ---------- РІРёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ Excel ----------

elif page == "Р’РёСЂС‚СѓР°Р»СЊРЅС‹Р№ СЃРєР»Р°Рґ Excel":

    render_virtual_warehouse_excel()



# ---------- СЂР°СЃС‡РµС‚ РјР°СЂС€СЂСѓС‚РѕРІ ----------

elif page == "Р Р°СЃС‡РµС‚ РјР°СЂС€СЂСѓС‚РѕРІ":
    st.header("Р Р°СЃС‡РµС‚ РјР°СЂС€СЂСѓС‚РѕРІ")

    st.write("Р—РґРµСЃСЊ РґР°Р»СЊС€Рµ Р±СѓРґРµС‚ СЂР°СЃС‡РµС‚ РјР°СЂС€СЂСѓС‚РѕРІ РїРѕ Р Рћ.")

    if "orders" in st.session_state:
        st.dataframe(st.session_state["orders"])
    else:
        st.info("РЎРЅР°С‡Р°Р»Р° Р·Р°РіСЂСѓР·РёС‚Рµ С„Р°Р№Р» СЂР°СЃС…РѕРґРЅРёРєРѕРІ РІ СЂР°Р·РґРµР»Рµ 'Р—Р°РіСЂСѓР·РєР° РґР°РЅРЅС‹С…'.")
