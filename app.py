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

st.set_page_config(page_title="\u0421\u0438\u043c\u0443\u043b\u044f\u0442\u043e\u0440 \u0441\u0431\u043e\u0440\u043a\u0438", layout="wide")

st.title("\u0421\u0438\u043c\u0443\u043b\u044f\u0442\u043e\u0440 \u0441\u043a\u043e\u0440\u043e\u0441\u0442\u0438 \u0441\u0431\u043e\u0440\u043a\u0438")

APP_BUILD_LABEL = "virtual-warehouse-color-2026-07-01"

# ---------- \u0444\u0443\u043d\u043a\u0446\u0438\u0438 ----------

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
        st.header("\u0412\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434 \u043f\u043e Excel-\u0441\u0445\u0435\u043c\u0435")
    st.caption(
        "\u0423\u043f\u0440\u043e\u0449\u0451\u043d\u043d\u044b\u0439 \u0440\u0435\u0436\u0438\u043c \u0447\u0438\u0442\u0430\u0435\u0442 \u0432\u0441\u0435 \u043b\u0438\u0441\u0442\u044b .xlsx \u0438 \u0441\u043d\u0430\u0447\u0430\u043b\u0430 \u0441\u0442\u0440\u043e\u0438\u0442 \u044f\u0447\u0435\u0439\u043a\u0438 \u043f\u043e \u0446\u0432\u0435\u0442\u043d\u043e\u0439 "
        "\u0437\u0430\u043b\u0438\u0432\u043a\u0435 \u0432 Excel. \u0422\u0430\u0431\u043b\u0438\u0447\u043d\u044b\u0435 \u043a\u043e\u043b\u043e\u043d\u043a\u0438 row_number/pallet_count \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0440\u0435\u0436\u0438\u043c\u0430 \u043d\u0435 \u043d\u0443\u0436\u043d\u044b."
    )

    with st.expander("\u0424\u043e\u0440\u043c\u0430\u0442\u044b \u0444\u0430\u0439\u043b\u043e\u0432", expanded=True):
        st.markdown(
            """
            **\u0421\u0445\u0435\u043c\u0430 \u0441\u043a\u043b\u0430\u0434\u0430:** \u043b\u044e\u0431\u043e\u0439 `.xlsx` \u0441 \u0432\u0438\u0437\u0443\u0430\u043b\u044c\u043d\u043e\u0439 \u0441\u0445\u0435\u043c\u043e\u0439. \u0415\u0441\u043b\u0438 \u044f\u0447\u0435\u0439\u043a\u0438 \u043d\u0430 \u0441\u0445\u0435\u043c\u0435 \u0437\u0430\u043a\u0440\u0430\u0448\u0435\u043d\u044b \u0446\u0432\u0435\u0442\u043e\u043c, \u043a\u0430\u0436\u0434\u0430\u044f \u0446\u0432\u0435\u0442\u043d\u0430\u044f Excel-\u044f\u0447\u0435\u0439\u043a\u0430 \u0441\u0442\u0430\u043d\u0435\u0442 \u0432\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u043e\u0439 \u044f\u0447\u0435\u0439\u043a\u043e\u0439 \u0441\u043a\u043b\u0430\u0434\u0430. \u041e\u0431\u0440\u0430\u0431\u0430\u0442\u044b\u0432\u0430\u044e\u0442\u0441\u044f \u0432\u0441\u0435 \u043b\u0438\u0441\u0442\u044b.

            **\u0424\u0430\u0439\u043b \u044f\u0447\u0435\u0435\u043a, \u043e\u043f\u0446\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u043e:** \u043a\u043e\u043b\u043e\u043d\u043a\u0438 `cell`/`\u044f\u0447\u0435\u0439\u043a\u0430`, `row`/`\u0440\u044f\u0434`, `tier`/`\u044f\u0440\u0443\u0441`
            \u0438\u043b\u0438 \u043f\u043e\u043b\u043d\u0430\u044f \u043a\u043e\u043b\u043e\u043d\u043a\u0430 `address`/`\u0430\u0434\u0440\u0435\u0441` \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435 `\u044f\u0447\u0435\u0439\u043a\u0430-\u0440\u044f\u0434-\u044f\u0440\u0443\u0441`.

            **\u0424\u0430\u0439\u043b \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u0438\u044f, \u043e\u043f\u0446\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u043e:** `address`/`\u0430\u0434\u0440\u0435\u0441` + `item`/`\u0442\u043e\u0432\u0430\u0440`/`\u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u0430`
            \u0438\u043b\u0438 \u0440\u0430\u0437\u0434\u0435\u043b\u044c\u043d\u044b\u0435 \u043a\u043e\u043b\u043e\u043d\u043a\u0438 `cell`, `row`, `tier`, `item`.
            \u041d\u0430 \u044d\u0442\u043e\u043c \u044d\u0442\u0430\u043f\u0435 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u044e\u0442\u0441\u044f \u0442\u043e\u043b\u044c\u043a\u043e \u0430\u0434\u0440\u0435\u0441\u0430 \u043f\u0435\u0440\u0432\u043e\u0433\u043e \u044f\u0440\u0443\u0441\u0430; \u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0438\u0435 \u044f\u0440\u0443\u0441\u0430 \u0441\u0447\u0438\u0442\u0430\u0435\u0442\u0441\u044f \u043f\u0435\u0440\u0432\u044b\u043c \u044f\u0440\u0443\u0441\u043e\u043c \u0438 \u043f\u043e\u043f\u0430\u0434\u0430\u0435\u0442 \u0432 \u0434\u0438\u0430\u0433\u043d\u043e\u0441\u0442\u0438\u043a\u0443.
            """
        )

    schema_file = st.file_uploader(
        "Excel-\u0441\u0445\u0435\u043c\u0430 \u0441\u043a\u043b\u0430\u0434\u0430",
        type=["xlsx"],
        key="virtual_warehouse_schema_upload",
    )
    cell_file = st.file_uploader(
        "\u0424\u0430\u0439\u043b \u043d\u043e\u043c\u0435\u0440\u043e\u0432 \u044f\u0447\u0435\u0435\u043a (\u043d\u0435\u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u043e)",
        type=["xlsx", "csv"],
        key="virtual_warehouse_cells_upload",
    )
    placement_file = st.file_uploader(
        "\u0424\u0430\u0439\u043b \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u0438\u044f \u0442\u043e\u0432\u0430\u0440\u043e\u0432 (\u043d\u0435\u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u043e)",
        type=["xlsx", "csv"],
        key="virtual_warehouse_placements_upload",
    )

    if st.button("\u041f\u043e\u0441\u0442\u0440\u043e\u0438\u0442\u044c \u0441\u043a\u043b\u0430\u0434 \u043f\u043e \u0446\u0432\u0435\u0442\u0430\u043c Excel", disabled=schema_file is None):
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
            st.success(f"\u0412\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434 \u043f\u043e\u0441\u0442\u0440\u043e\u0435\u043d: {len(model.sheets)} \u043b\u0438\u0441\u0442\u043e\u0432, {len(model.cells)} \u044f\u0447\u0435\u0435\u043a.")
        except Exception as exc:
            st.error(f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u0441\u0442\u0440\u043e\u0438\u0442\u044c \u0432\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434: {exc}")

    model = st.session_state.get("virtual_warehouse_model")
    if model is None:
        st.info("\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 Excel-\u0441\u0445\u0435\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0430 \u0438 \u043d\u0430\u0436\u043c\u0438\u0442\u0435 \u043a\u043d\u043e\u043f\u043a\u0443 \u043f\u043e\u0441\u0442\u0440\u043e\u0435\u043d\u0438\u044f.")
    else:
        diagnostics = st.session_state.get("virtual_warehouse_diagnostics", [])
        sheet_names = [sheet.name for sheet in model.sheets]
        selected_sheet_name = st.selectbox("\u041b\u0438\u0441\u0442 \u0441\u043a\u043b\u0430\u0434\u0430", sheet_names, key="virtual_warehouse_sheet_select")
        selected_sheet = next(sheet for sheet in model.sheets if sheet.name == selected_sheet_name)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("\u041b\u0438\u0441\u0442\u043e\u0432", len(model.sheets))
        m2.metric("\u0420\u044f\u0434\u043e\u0432 \u043d\u0430 \u043b\u0438\u0441\u0442\u0435", len(selected_sheet.rows))
        m3.metric("\u042f\u0447\u0435\u0435\u043a \u0432\u0441\u0435\u0433\u043e", len(model.cells))
        m4.metric("\u0422\u043e\u0432\u0430\u0440\u043e\u0432 \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u043e", sum(1 for cell in model.cells if cell.item))

        tab_map, tab_rows, tab_cells, tab_diag = st.tabs(["\u0412\u0438\u0437\u0443\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u044f", "\u0420\u044f\u0434\u044b", "\u042f\u0447\u0435\u0439\u043a\u0438", "\u0414\u0438\u0430\u0433\u043d\u043e\u0441\u0442\u0438\u043a\u0430"])
        with tab_map:
            scale = st.slider("\u041c\u0430\u0441\u0448\u0442\u0430\u0431 \u0441\u0435\u0442\u043a\u0438", min_value=18, max_value=60, value=34, step=2)
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
                "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u0434\u0438\u0430\u0433\u043d\u043e\u0441\u0442\u0438\u043a\u0443 CSV",
                diag_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="virtual_warehouse_diagnostics.csv",
                mime="text/csv",
            )

        if st.button("\u041e\u0447\u0438\u0441\u0442\u0438\u0442\u044c \u0432\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434"):
            for key in ["virtual_warehouse_model", "virtual_warehouse_diagnostics"]:
                st.session_state.pop(key, None)
            st.rerun()

# ---------- \u043c\u0435\u043d\u044e ----------

st.sidebar.header("\u0420\u0430\u0437\u0434\u0435\u043b\u044b")
st.sidebar.caption(f"\u0421\u0431\u043e\u0440\u043a\u0430: {APP_BUILD_LABEL}")

page = st.sidebar.radio(
    "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0440\u0430\u0437\u0434\u0435\u043b",
    [
    "\u0428\u0430\u0431\u043b\u043e\u043d\u044b \u0444\u0430\u0439\u043b\u043e\u0432",
    "\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0434\u0430\u043d\u043d\u044b\u0445",
    "\u041a\u0430\u0440\u0442\u0430 \u0420\u0426",
    "\u041a\u0430\u0440\u0442\u0430 \u0441\u043a\u043b\u0430\u0434\u0430",
    "\u0412\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434 Excel",
    "\u0420\u0430\u0441\u0447\u0435\u0442 \u043c\u0430\u0440\u0448\u0440\u0443\u0442\u043e\u0432"
]
)

# ---------- \u0448\u0430\u0431\u043b\u043e\u043d\u044b ----------

if page == "\u0428\u0430\u0431\u043b\u043e\u043d\u044b \u0444\u0430\u0439\u043b\u043e\u0432":
    st.header("\u0428\u0430\u0431\u043b\u043e\u043d\u044b Excel-\u0444\u0430\u0439\u043b\u043e\u0432")

    st.write("\u0421\u043a\u0430\u0447\u0430\u0439 \u0448\u0430\u0431\u043b\u043e\u043d\u044b, \u0437\u0430\u043f\u043e\u043b\u043d\u0438 \u0438\u0445 \u0441\u0432\u043e\u0438\u043c\u0438 \u0434\u0430\u043d\u043d\u044b\u043c\u0438 \u0438 \u043f\u043e\u0442\u043e\u043c \u0437\u0430\u0433\u0440\u0443\u0437\u0438 \u0432 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435.")

    # 1. \u0428\u0430\u0431\u043b\u043e\u043d \u0441\u043a\u043b\u0430\u0434\u043e\u0432
    warehouses_df = pd.DataFrame({
        "warehouse_id": ["veshki_day", "veshki_night"],
        "warehouse_name": ["\u0414\u043d\u0435\u0432\u043d\u043e\u0439 \u0412\u0435\u0448\u043a\u0438", "\u041d\u043e\u0447\u043d\u043e\u0439 \u0412\u0435\u0448\u043a\u0438"],
        "width_mm": [80000, 80000],
        "height_mm": [50000, 50000],
        "comment": ["\u041e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u043e\u0439 \u0441\u043a\u043b\u0430\u0434", "\u041f\u0440\u0438\u043c\u0435\u0440 \u0432\u0442\u043e\u0440\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0430"]
    })

    # 2. \u0428\u0430\u0431\u043b\u043e\u043d \u043e\u0431\u044a\u0435\u043a\u0442\u043e\u0432 \u043a\u0430\u0440\u0442\u044b
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
            "\u042f\u0447\u0435\u0439\u043a\u0430 \u0445\u0440\u0430\u043d\u0435\u043d\u0438\u044f",
            "\u042f\u0447\u0435\u0439\u043a\u0430 \u0445\u0440\u0430\u043d\u0435\u043d\u0438\u044f",
            "\u042f\u0447\u0435\u0439\u043a\u0430 \u0445\u0440\u0430\u043d\u0435\u043d\u0438\u044f",
            "\u041f\u0440\u043e\u0445\u043e\u0434",
            "\u041a\u043e\u043b\u043e\u043d\u043d\u0430"
        ]
    })

    # 3. \u0428\u0430\u0431\u043b\u043e\u043d \u0440\u0430\u0441\u0445\u043e\u0434\u043d\u0438\u043a\u043e\u0432
    orders_df = pd.DataFrame({
        "period": ["09.06.2026 6:12:16", "09.06.2026 6:12:16"],
        "warehouse_name": ["\u0414\u043d\u0435\u0432\u043d\u043e\u0439 \u0412\u0435\u0448\u043a\u0438", "\u0414\u043d\u0435\u0432\u043d\u043e\u0439 \u0412\u0435\u0448\u043a\u0438"],
        "order_id": [
            "\u0420\u0430\u0441\u0445\u043e\u0434\u043d\u044b\u0439 \u043e\u0440\u0434\u0435\u0440 \u041220539431",
            "\u0420\u0430\u0441\u0445\u043e\u0434\u043d\u044b\u0439 \u043e\u0440\u0434\u0435\u0440 \u041220539431"
        ],
        "nomenclature": [
            "\u041f\u0438\u0432\u043e \u0411\u0435\u043b\u044c\u0433\u0438\u0439\u0441\u043a\u043e\u0435 \u0431\u0435\u0437\u0430\u043b\u043a\u043e\u0433\u043e\u043b\u044c\u043d\u043e\u0435, 500 \u043c\u043b",
            "\u0412\u043e\u0434\u0430 \u0440\u043e\u0434\u043d\u0438\u043a\u043e\u0432\u0430\u044f \u0433\u0430\u0437\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u0430\u044f, 1,5 \u043b"
        ],
        "characteristic": ["\u0412\u0410\u0420\u041d\u0418\u0426\u0410 \u041e\u041e\u041e", "\u0421\u0412\u0415\u0422\u041b\u041e\u042f\u0420 \u041e\u041e\u041e"],
        "production_date": ["14.04.2026", "17.05.2026"],
        "cell_id": ["24-09-01", "19-58-01"],
        "quantity": [12, 30],
        "cell_balance": [384, 372],
        "print_order": [53722, 53702]
    })

    # 4. \u0428\u0430\u0431\u043b\u043e\u043d \u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u043e\u0432 \u0441\u0431\u043e\u0440\u0449\u0438\u043a\u0430
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
            "\u0421\u043a\u043e\u0440\u043e\u0441\u0442\u044c \u0434\u0432\u0438\u0436\u0435\u043d\u0438\u044f \u0441\u0431\u043e\u0440\u0449\u0438\u043a\u0430, \u043c/\u0441",
            "\u0412\u0440\u0435\u043c\u044f \u043d\u0430 \u043f\u043e\u0434\u0431\u043e\u0440 \u043e\u0434\u043d\u043e\u0439 \u0441\u0442\u0440\u043e\u043a\u0438",
            "\u0412\u0440\u0435\u043c\u044f \u043d\u0430 \u0441\u043a\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u043e\u0434\u043d\u043e\u0439 \u0441\u0442\u0440\u043e\u043a\u0438",
            "X \u0441\u0442\u0430\u0440\u0442\u043e\u0432\u043e\u0439 \u0442\u043e\u0447\u043a\u0438",
            "Y \u0441\u0442\u0430\u0440\u0442\u043e\u0432\u043e\u0439 \u0442\u043e\u0447\u043a\u0438",
            "X \u0442\u043e\u0447\u043a\u0438 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f",
            "Y \u0442\u043e\u0447\u043a\u0438 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f"
        ]
    })

    download_excel_button(
        "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u0448\u0430\u0431\u043b\u043e\u043d \u0441\u043a\u043b\u0430\u0434\u043e\u0432",
        {"warehouses": warehouses_df},
        "template_warehouses.xlsx"
    )

    download_excel_button(
        "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u0448\u0430\u0431\u043b\u043e\u043d \u043a\u0430\u0440\u0442\u044b \u0441\u043a\u043b\u0430\u0434\u0430",
        {"map_objects": map_objects_df},
        "template_warehouse_map.xlsx"
    )

    download_excel_button(
        "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u0448\u0430\u0431\u043b\u043e\u043d \u0440\u0430\u0441\u0445\u043e\u0434\u043d\u0438\u043a\u043e\u0432",
        {"orders": orders_df},
        "template_orders.xlsx"
    )

    download_excel_button(
        "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u0448\u0430\u0431\u043b\u043e\u043d \u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u043e\u0432 \u0441\u0431\u043e\u0440\u0449\u0438\u043a\u0430",
        {"picker_params": picker_params_df},
        "template_picker_params.xlsx"
    )

    download_excel_button(
        "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u0432\u0441\u0435 \u0448\u0430\u0431\u043b\u043e\u043d\u044b \u043e\u0434\u043d\u0438\u043c \u0444\u0430\u0439\u043b\u043e\u043c",
        {
            "warehouses": warehouses_df,
            "map_objects": map_objects_df,
            "orders": orders_df,
            "picker_params": picker_params_df
        },
        "templates_all.xlsx"
    )

# ---------- \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0434\u0430\u043d\u043d\u044b\u0445 ----------

elif page == "\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0434\u0430\u043d\u043d\u044b\u0445":
    st.header("\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0434\u0430\u043d\u043d\u044b\u0445")

    uploaded_file = st.file_uploader(
        "\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 \u0437\u0430\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0439 Excel-\u0444\u0430\u0439\u043b",
        type=["xlsx"]
    )

    if uploaded_file:
        xls = pd.ExcelFile(uploaded_file)

        st.subheader("\u041b\u0438\u0441\u0442\u044b \u0432 \u0444\u0430\u0439\u043b\u0435")
        st.write(xls.sheet_names)

        selected_sheet = st.selectbox(
            "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043b\u0438\u0441\u0442 \u0434\u043b\u044f \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0430",
            xls.sheet_names
        )

        df = pd.read_excel(uploaded_file, sheet_name=selected_sheet)

        st.subheader("\u0414\u0430\u043d\u043d\u044b\u0435")
        st.dataframe(df)

        st.session_state[selected_sheet] = df

        st.success(f"\u041b\u0438\u0441\u0442 '{selected_sheet}' \u0437\u0430\u0433\u0440\u0443\u0436\u0435\u043d \u0432 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435")

# ---------- \u043a\u0430\u0440\u0442\u0430 \u0420\u0426 ----------

elif page == "\u041a\u0430\u0440\u0442\u0430 \u0420\u0426":

    st.header("\u041a\u0430\u0440\u0442\u0430 \u0440\u0430\u0441\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0433\u043e \u0446\u0435\u043d\u0442\u0440\u0430")

    uploaded_pdf = st.file_uploader(
        "\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 PDF-\u043f\u043b\u0430\u043d \u0420\u0426",
        type=["pdf"]
    )

    if uploaded_pdf:
        st.session_state["rc_pdf_bytes"] = uploaded_pdf.read()
        st.session_state["rc_pdf_name"] = uploaded_pdf.name

    if "rc_pdf_bytes" not in st.session_state:

        st.info("\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 PDF \u043f\u043b\u0430\u043d \u0441\u043a\u043b\u0430\u0434\u0430")

    else:

        st.success(
            f"\u0417\u0430\u0433\u0440\u0443\u0436\u0435\u043d \u0444\u0430\u0439\u043b: {st.session_state['rc_pdf_name']}"
        )

        pdf_doc = fitz.open(
            stream=st.session_state["rc_pdf_bytes"],
            filetype="pdf"
        )

        page_num = st.number_input(
            "\u0421\u0442\u0440\u0430\u043d\u0438\u0446\u0430",
            min_value=1,
            max_value=len(pdf_doc),
            value=1
        )

        zoom = st.slider(
            "\u041c\u0430\u0441\u0448\u0442\u0430\u0431",
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
            "\u041a\u043b\u0438\u043a\u043d\u0438 \u043f\u043e \u043f\u043b\u0430\u043d\u0443 \u0434\u043b\u044f \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u044f \u043a\u043e\u043e\u0440\u0434\u0438\u043d\u0430\u0442"
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

        st.subheader("\u041c\u0430\u0441\u0448\u0442\u0430\u0431 \u043f\u043b\u0430\u043d\u0430")

        col1, col2 = st.columns(2)

        with col1:
            if st.button("\u0417\u0430\u043f\u043e\u043c\u043d\u0438\u0442\u044c \u0442\u043e\u0447\u043a\u0443 A"):

                if "last_click_x" in st.session_state:

                    st.session_state["point_a"] = (
                        st.session_state["last_click_x"],
                        st.session_state["last_click_y"]
                    )

        with col2:
            if st.button("\u0417\u0430\u043f\u043e\u043c\u043d\u0438\u0442\u044c \u0442\u043e\u0447\u043a\u0443 B"):

                if "last_click_x" in st.session_state:

                    st.session_state["point_b"] = (
                        st.session_state["last_click_x"],
                        st.session_state["last_click_y"]
                    )

        point_a = st.session_state.get("point_a")
        point_b = st.session_state.get("point_b")

        st.write("\u0422\u043e\u0447\u043a\u0430 A:", point_a)
        st.write("\u0422\u043e\u0447\u043a\u0430 B:", point_b)

        real_length_mm = st.number_input(
            "\u0420\u0435\u0430\u043b\u044c\u043d\u0430\u044f \u0434\u043b\u0438\u043d\u0430 \u043c\u0435\u0436\u0434\u0443 \u0442\u043e\u0447\u043a\u0430\u043c\u0438 (\u043c\u043c)",
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
                "\u0420\u0430\u0441\u0441\u0442\u043e\u044f\u043d\u0438\u0435 \u0432 \u043f\u0438\u043a\u0441\u0435\u043b\u044f\u0445",
                f"{distance_px:.2f}"
            )

            if distance_px == 0:
                st.warning("\u0422\u043e\u0447\u043a\u0438 A \u0438 B \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u044e\u0442. \u0412\u044b\u0431\u0435\u0440\u0438 \u0434\u0432\u0435 \u0440\u0430\u0437\u043d\u044b\u0435 \u0442\u043e\u0447\u043a\u0438 \u0434\u043b\u044f \u0440\u0430\u0441\u0447\u0435\u0442\u0430 \u043c\u0430\u0441\u0448\u0442\u0430\u0431\u0430.")
            else:
                mm_per_px = (
                    real_length_mm /
                    distance_px
                )

                st.metric(
                    "\u043c\u043c \u043d\u0430 \u043f\u0438\u043a\u0441\u0435\u043b\u044c",
                    f"{mm_per_px:.4f}"
                )

                st.session_state[
                    "mm_per_px"
                ] = mm_per_px

        if st.button("\u041e\u0447\u0438\u0441\u0442\u0438\u0442\u044c PDF"):

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
# ---------- \u043a\u0430\u0440\u0442\u0430 \u0441\u043a\u043b\u0430\u0434\u0430 ----------

elif page == "\u041a\u0430\u0440\u0442\u0430 \u0441\u043a\u043b\u0430\u0434\u0430":
    st.header("\u041a\u0430\u0440\u0442\u0430 \u0441\u043a\u043b\u0430\u0434\u0430")

    st.warning(
        "\u041d\u043e\u0432\u044b\u0439 \u043c\u043e\u0434\u0443\u043b\u044c Excel-\u0441\u0445\u0435\u043c\u044b \u0441\u043a\u043b\u0430\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d \u043f\u0440\u044f\u043c\u043e \u0437\u0434\u0435\u0441\u044c \u043d\u0438\u0436\u0435 \u0438 \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u044b\u043c \u043f\u0443\u043d\u043a\u0442\u043e\u043c \u043c\u0435\u043d\u044e "
        "\u00ab\u0412\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434 Excel\u00bb. \u0415\u0441\u043b\u0438 \u043f\u0443\u043d\u043a\u0442 \u043c\u0435\u043d\u044e \u043d\u0435 \u043f\u043e\u044f\u0432\u0438\u043b\u0441\u044f, \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 Streamlit/start.cmd."
    )

    st.subheader("\u0412\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434 Excel \u2014 \u043f\u043e\u0441\u0442\u0440\u043e\u0438\u0442\u044c \u043f\u043e \u0432\u0438\u0437\u0443\u0430\u043b\u044c\u043d\u043e\u0439 Excel-\u0441\u0445\u0435\u043c\u0435")
    render_virtual_warehouse_excel(show_header=False)
    st.divider()

    st.info(
        "\u041a\u0430\u0440\u0442\u0443 \u0441\u043a\u043b\u0430\u0434\u0430 \u0442\u0435\u043f\u0435\u0440\u044c \u043c\u043e\u0436\u043d\u043e \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043f\u0440\u044f\u043c\u043e \u0437\u0434\u0435\u0441\u044c: \u0441\u043d\u0430\u0447\u0430\u043b\u0430 Excel \u0441\u043e \u0441\u0445\u0435\u043c\u043e\u0439 \u0440\u044f\u0434\u043e\u0432, "
        "\u0437\u0430\u0442\u0435\u043c \u043f\u0440\u0438 \u043d\u0435\u043e\u0431\u0445\u043e\u0434\u0438\u043c\u043e\u0441\u0442\u0438 Excel-\u0432\u044b\u0433\u0440\u0443\u0437\u043a\u0443 1\u0421 \u0441 \u0444\u0430\u043a\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u043c\u0438 \u0430\u0434\u0440\u0435\u0441\u0430\u043c\u0438 \u044f\u0447\u0435\u0435\u043a."
    )

    with st.expander("\u041a\u0430\u043a\u0438\u0435 \u043a\u043e\u043b\u043e\u043d\u043a\u0438 \u043d\u0443\u0436\u043d\u044b \u0432 Excel", expanded=True):
        st.markdown(
            """
            **\u0421\u0445\u0435\u043c\u0430 \u0440\u044f\u0434\u043e\u0432:** \u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u044b `\u0420\u044f\u0434` \u0438 `\u041a\u043e\u043b-\u0432\u043e \u044f\u0447\u0435\u0435\u043a` / `\u041a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u044f\u0447\u0435\u0435\u043a`.
            \u0414\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u043e: `\u0421\u043a\u043b\u0430\u0434`, `\u0427\u0430\u0441\u0442\u044c \u0440\u044f\u0434\u0430`, `\u0414\u043b\u0438\u043d\u0430 \u044f\u0447\u0435\u0439\u043a\u0438 \u043c\u043c`, `\u0428\u0438\u0440\u0438\u043d\u0430 \u044f\u0447\u0435\u0439\u043a\u0438 \u043c\u043c`,
            `\u0417\u0430\u0437\u043e\u0440 \u043c\u043c`, `\u041f\u0440\u043e\u0435\u0437\u0434 \u043c\u043c`, `\u041f\u043e\u0432\u043e\u0440\u043e\u0442 \u043c\u043c`, `\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0440\u044f\u0434`, `\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439`.

            **\u0412\u044b\u0433\u0440\u0443\u0437\u043a\u0430 1\u0421:** \u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u044b `\u0420\u044f\u0434` \u0438 `\u042f\u0447\u0435\u0439\u043a\u0430`.
            \u0414\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u043e: `\u0421\u043a\u043b\u0430\u0434`, `\u0410\u0434\u0440\u0435\u0441 \u044f\u0447\u0435\u0439\u043a\u0438` / `\u0421\u043a\u043b\u0430\u0434\u0441\u043a\u0430\u044f \u044f\u0447\u0435\u0439\u043a\u0430`.
            """
        )

    upload_col, one_c_col = st.columns(2)

    with upload_col:
        default_zone = st.text_input(
            "\u0421\u043a\u043b\u0430\u0434/\u0437\u043e\u043d\u0430 \u0434\u043b\u044f \u0441\u0442\u0440\u043e\u043a \u0431\u0435\u0437 \u043a\u043e\u043b\u043e\u043d\u043a\u0438 \u0441\u043a\u043b\u0430\u0434\u0430",
            value="\u041a\u0430\u0440\u0442\u0430 \u0441\u043a\u043b\u0430\u0434\u0430",
            key="warehouse_map_default_zone",
        )
        layout_file = st.file_uploader(
            "\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c Excel \u0441\u0445\u0435\u043c\u044b \u0441\u043a\u043b\u0430\u0434\u0430",
            type=["xlsx"],
            key="warehouse_map_layout_upload",
        )
        if st.button("\u041f\u043e\u0441\u0442\u0440\u043e\u0438\u0442\u044c \u043a\u0430\u0440\u0442\u0443 \u0441\u043a\u043b\u0430\u0434\u0430", disabled=layout_file is None):
            try:
                sheet_name, segments = import_segments_from_excel(
                    layout_file,
                    default_zone.strip() or "\u041a\u0430\u0440\u0442\u0430 \u0441\u043a\u043b\u0430\u0434\u0430",
                )
                st.session_state["warehouse_map_segments"] = segments
                st.session_state["warehouse_map_layout_sheet"] = sheet_name
                st.success(
                    f"\u0421\u0445\u0435\u043c\u0430 \u0437\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u0430: {len(segments)} \u0441\u0442\u0440\u043e\u043a \u0438\u0437 \u043b\u0438\u0441\u0442\u0430 \u00ab{sheet_name}\u00bb."
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
                                "\u0424\u0430\u0439\u043b \u043d\u0435 \u043f\u043e\u0445\u043e\u0436 \u043d\u0430 \u0442\u0430\u0431\u043b\u0438\u0447\u043d\u0443\u044e \u0441\u0445\u0435\u043c\u0443 \u0440\u044f\u0434\u043e\u0432, \u043f\u043e\u044d\u0442\u043e\u043c\u0443 \u043e\u043d \u043e\u0442\u043a\u0440\u044b\u0442 "
                                "\u0432 \u0443\u043f\u0440\u043e\u0449\u0451\u043d\u043d\u043e\u043c \u0440\u0435\u0436\u0438\u043c\u0435 \u043f\u043e \u0446\u0432\u0435\u0442\u043d\u043e\u0439 Excel-\u0440\u0430\u0437\u043c\u0435\u0442\u043a\u0435."
                            ),
                        }
                    ]
                    st.warning(
                        "\u0422\u0430\u0431\u043b\u0438\u0447\u043d\u044b\u0435 \u043a\u043e\u043b\u043e\u043d\u043a\u0438 `\u0420\u044f\u0434` \u0438 `\u041a\u043e\u043b-\u0432\u043e \u044f\u0447\u0435\u0435\u043a` \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b. "
                        "\u042f \u043f\u043e\u0441\u0442\u0440\u043e\u0438\u043b \u0441\u043a\u043b\u0430\u0434 \u043f\u043e \u0446\u0432\u0435\u0442\u043d\u044b\u043c \u044f\u0447\u0435\u0439\u043a\u0430\u043c Excel \u0432 \u0431\u043b\u043e\u043a\u0435 \u0432\u044b\u0448\u0435."
                    )
                except Exception as fallback_exc:
                    st.error(f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0441\u0445\u0435\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0430: {exc}. \u0423\u043f\u0440\u043e\u0449\u0451\u043d\u043d\u044b\u0439 \u0440\u0435\u0436\u0438\u043c \u0442\u043e\u0436\u0435 \u043d\u0435 \u0441\u0440\u0430\u0431\u043e\u0442\u0430\u043b: {fallback_exc}")

    with one_c_col:
        one_c_file = st.file_uploader(
            "\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c Excel \u0432\u044b\u0433\u0440\u0443\u0437\u043a\u0438 1\u0421",
            type=["xlsx"],
            key="warehouse_map_1c_upload",
        )
        if st.button("\u041f\u0440\u0438\u043c\u0435\u043d\u0438\u0442\u044c \u043d\u043e\u043c\u0435\u0440\u0430 \u0438\u0437 1\u0421", disabled=one_c_file is None):
            try:
                sheet_name, one_c_cells = import_1c_cells_from_excel(one_c_file)
                st.session_state["warehouse_map_1c_cells"] = one_c_cells
                st.session_state["warehouse_map_1c_sheet"] = sheet_name
                st.success(
                    f"\u0412\u044b\u0433\u0440\u0443\u0437\u043a\u0430 1\u0421 \u0437\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u0430: {len(one_c_cells)} \u044f\u0447\u0435\u0435\u043a \u0438\u0437 \u043b\u0438\u0441\u0442\u0430 \u00ab{sheet_name}\u00bb."
                )
            except Exception as exc:
                st.error(f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0432\u044b\u0433\u0440\u0443\u0437\u043a\u0443 1\u0421: {exc}")

    if "warehouse_map_segments" not in st.session_state:
        st.warning("\u0417\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 Excel \u0441\u0445\u0435\u043c\u044b \u0441\u043a\u043b\u0430\u0434\u0430 \u0432 \u0431\u043b\u043e\u043a\u0435 \u0432\u044b\u0448\u0435, \u0447\u0442\u043e\u0431\u044b \u043f\u043e\u0441\u0442\u0440\u043e\u0438\u0442\u044c \u043a\u0430\u0440\u0442\u0443.")
    else:
        segments = st.session_state["warehouse_map_segments"]
        one_c_cells = st.session_state.get("warehouse_map_1c_cells")

        st.subheader("\u0417\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u043d\u0430\u044f \u0441\u0445\u0435\u043c\u0430 \u0440\u044f\u0434\u043e\u0432")
        st.caption(
            f"\u041b\u0438\u0441\u0442 \u0441\u0445\u0435\u043c\u044b: {st.session_state.get('warehouse_map_layout_sheet', '\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d')}"
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
        metric1.metric("\u0420\u044f\u0434\u043e\u0432", len(row_summary))
        metric2.metric("\u042f\u0447\u0435\u0435\u043a", len(cells))
        metric3.metric("\u041f\u0440\u043e\u0435\u0437\u0434\u043e\u0432", len(passages))
        metric4.metric(
            "\u041c\u0430\u0440\u0448\u0440\u0443\u0442, \u043c",
            round(float(zone_summary["total_route_m"].sum()), 1) if not zone_summary.empty else 0,
        )

        map_scale = st.slider(
            "\u041c\u0430\u0441\u0448\u0442\u0430\u0431 \u043e\u0442\u0440\u0438\u0441\u043e\u0432\u043a\u0438",
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

        with st.expander("\u0421\u0432\u043e\u0434\u043a\u0430 \u043f\u043e \u0441\u043a\u043b\u0430\u0434\u0443"):
            st.dataframe(zone_summary, use_container_width=True)

        with st.expander("\u0421\u0432\u043e\u0434\u043a\u0430 \u043f\u043e \u0440\u044f\u0434\u0430\u043c"):
            st.dataframe(row_summary, use_container_width=True)

        with st.expander("\u041f\u0435\u0440\u0432\u044b\u0435 1000 \u044f\u0447\u0435\u0435\u043a"):
            st.dataframe(cells.head(1000), use_container_width=True)

        if st.button("\u041e\u0447\u0438\u0441\u0442\u0438\u0442\u044c \u0437\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u043d\u0443\u044e \u043a\u0430\u0440\u0442\u0443 \u0441\u043a\u043b\u0430\u0434\u0430"):
            for key in [
                "warehouse_map_segments",
                "warehouse_map_layout_sheet",
                "warehouse_map_1c_cells",
                "warehouse_map_1c_sheet",
            ]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

# ---------- \u0432\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434 Excel ----------

elif page == "\u0412\u0438\u0440\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0441\u043a\u043b\u0430\u0434 Excel":
    # \u0412\u0435\u0441\u044c UI \u044d\u0442\u043e\u0433\u043e \u0440\u0430\u0437\u0434\u0435\u043b\u0430 \u0436\u0438\u0432\u0451\u0442 \u0432 render_virtual_warehouse_excel(),
    # \u0447\u0442\u043e\u0431\u044b \u043d\u0435 \u0434\u0443\u0431\u043b\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0431\u043e\u043b\u044c\u0448\u043e\u0439 \u0431\u043b\u043e\u043a \u0438 \u043d\u0435 \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u044c merge-\u043a\u043e\u043d\u0444\u043b\u0438\u043a\u0442\u044b.
    render_virtual_warehouse_excel()

# ---------- \u0440\u0430\u0441\u0447\u0435\u0442 \u043c\u0430\u0440\u0448\u0440\u0443\u0442\u043e\u0432 ----------

elif page == "\u0420\u0430\u0441\u0447\u0435\u0442 \u043c\u0430\u0440\u0448\u0440\u0443\u0442\u043e\u0432":
    st.header("\u0420\u0430\u0441\u0447\u0435\u0442 \u043c\u0430\u0440\u0448\u0440\u0443\u0442\u043e\u0432")

    st.write("\u0417\u0434\u0435\u0441\u044c \u0434\u0430\u043b\u044c\u0448\u0435 \u0431\u0443\u0434\u0435\u0442 \u0440\u0430\u0441\u0447\u0435\u0442 \u043c\u0430\u0440\u0448\u0440\u0443\u0442\u043e\u0432 \u043f\u043e \u0420\u041e.")

    if "orders" in st.session_state:
        st.dataframe(st.session_state["orders"])
    else:
        st.info("\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 \u0444\u0430\u0439\u043b \u0440\u0430\u0441\u0445\u043e\u0434\u043d\u0438\u043a\u043e\u0432 \u0432 \u0440\u0430\u0437\u0434\u0435\u043b\u0435 '\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0434\u0430\u043d\u043d\u044b\u0445'.")
