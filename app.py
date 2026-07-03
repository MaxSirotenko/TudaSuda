from streamlit_image_coordinates import streamlit_image_coordinates
import streamlit as st
import pandas as pd
import fitz
from PIL import Image
from io import BytesIO
from time import perf_counter
import json

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
from imported_warehouse_import import (
    AREA_TYPES,
    build_imported_model,
    build_imported_warehouse_html,
    read_cell_export,
    read_scheme_colors,
    save_imported_model,
)

from row_constructor import (
    DEFAULT_SETTINGS,
    apply_1c_cell_numbers,
    build_model,
    build_visual_map_html,
    import_1c_cells_from_excel,
    import_segments_from_excel,
    normalize_segments,
)

st.set_page_config(page_title="Симулятор сборки", layout="wide")

st.title("Симулятор скорости сборки")

APP_BUILD_LABEL = "virtual-warehouse-optimized-2026-07-02"

# ---------- функции ----------

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



@st.cache_data(show_spinner=False)
def parse_warehouse_excel_cached(file_bytes: bytes):
    return parse_warehouse_excel(file_bytes)


@st.cache_data(show_spinner=False)
def build_virtual_warehouse_html_cached(sheet, scale: int):
    return build_virtual_warehouse_html(sheet, scale)


def render_virtual_warehouse_excel(show_header=True):
    if show_header:
        st.header("Виртуальный склад по Excel-схеме")
    st.caption(
        "Упрощённый режим читает все листы .xlsx и сначала строит ячейки по цветной "
        "заливке в Excel. Табличные колонки row_number/pallet_count для этого режима не нужны."
    )

    with st.expander("Форматы файлов", expanded=True):
        st.markdown(
            """
            **Схема склада:** любой `.xlsx` с визуальной схемой. Если ячейки на схеме закрашены цветом, каждая цветная Excel-ячейка станет виртуальной ячейкой склада. Обрабатываются все листы.

            **Файл ячеек, опционально:** колонки `cell`/`ячейка`, `row`/`ряд`, `tier`/`ярус`
            или полная колонка `address`/`адрес` в формате `ячейка-ряд-ярус`.

            **Файл размещения, опционально:** `address`/`адрес` + `item`/`товар`/`номенклатура`
            или раздельные колонки `cell`, `row`, `tier`, `item`.
            На этом этапе используются только адреса первого яруса; отсутствие яруса считается первым ярусом и попадает в диагностику.
            """
        )

    schema_file = st.file_uploader(
        "Excel-схема склада",
        type=["xlsx"],
        key="virtual_warehouse_schema_upload",
    )
    cell_file = st.file_uploader(
        "Файл номеров ячеек (необязательно)",
        type=["xlsx", "csv"],
        key="virtual_warehouse_cells_upload",
    )
    placement_file = st.file_uploader(
        "Файл размещения товаров (необязательно)",
        type=["xlsx", "csv"],
        key="virtual_warehouse_placements_upload",
    )

    if st.button("Построить склад по цветам Excel", disabled=schema_file is None):
        diagnostics = []
        try:
            started_at = perf_counter()
            schema_bytes = schema_file.getvalue()
            model = parse_warehouse_excel_cached(schema_bytes)
            parse_seconds = perf_counter() - started_at
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
            st.session_state["virtual_warehouse_parse_seconds"] = parse_seconds
            st.success(f"Виртуальный склад построен за {parse_seconds:.2f} сек.: {len(model.sheets)} листов, {len(model.cells)} ячеек.")
        except Exception as exc:
            st.error(f"Не удалось построить виртуальный склад: {exc}")

    model = st.session_state.get("virtual_warehouse_model")
    if model is None:
        st.info("Загрузите Excel-схему склада и нажмите кнопку построения.")
    else:
        diagnostics = st.session_state.get("virtual_warehouse_diagnostics", [])
        sheet_names = [sheet.name for sheet in model.sheets]
        selected_sheet_name = st.selectbox("Лист склада", sheet_names, key="virtual_warehouse_sheet_select")
        selected_sheet = next(sheet for sheet in model.sheets if sheet.name == selected_sheet_name)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Листов", len(model.sheets))
        m2.metric("Рядов на листе", len(selected_sheet.rows))
        m3.metric("Ячеек всего", len(model.cells))
        m4.metric("Товаров размещено", sum(1 for cell in model.cells if cell.item))
        parse_seconds = st.session_state.get("virtual_warehouse_parse_seconds")
        if parse_seconds is not None:
            st.caption(f"Последний разбор Excel: {parse_seconds:.2f} сек.; повторные построения того же файла берутся из кэша.")

        tab_map, tab_rows, tab_cells, tab_diag = st.tabs(["Визуализация", "Ряды", "Ячейки", "Диагностика"])
        with tab_map:
            scale = st.slider("Масштаб сетки", min_value=18, max_value=60, value=34, step=2)
            components.html(build_virtual_warehouse_html_cached(selected_sheet, scale), height=760, scrolling=True)
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
                "Скачать диагностику CSV",
                diag_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="virtual_warehouse_diagnostics.csv",
                mime="text/csv",
            )

        if st.button("Очистить виртуальный склад"):
            for key in ["virtual_warehouse_model", "virtual_warehouse_diagnostics", "virtual_warehouse_parse_seconds"]:
                st.session_state.pop(key, None)
            st.rerun()


def render_imported_warehouse_excel():
    st.header("Импорт склада из Excel")
    st.caption(
        "Новый импорт не заменяет ручной конструктор: все ячейки берутся из выгрузки 1С, "
        "а Excel-схема используется для цветовых зон, геометрии и привязки рядов."
    )

    with st.expander("План изменений и ограничения", expanded=False):
        st.markdown(
            """
            1. Выгрузка 1С валидируется по колонкам `Ссылка`, `КоличествоПаллет`, `СкладЗоныЯчеекСсылка`, `НеИспользовать`.
            2. Адрес парсится по настраиваемым сегментам; по умолчанию `ячейка-ряд-ярус`.
            3. Цветовая Excel-схема читается через `openpyxl`, включая объединённые ячейки.
            4. Если точная геометрия ряда не найдена, включается fallback: стабильная сетка по рядам и количеству ячеек из выгрузки.
            """
        )

    c1, c2 = st.columns(2)
    with c1:
        cells_file = st.file_uploader("Excel с выгрузкой всех ячеек РЦ из 1С", type=["xlsx"], key="imported_cells_file")
    with c2:
        scheme_file = st.file_uploader("Excel со схемой / цветовой разметкой склада", type=["xlsx"], key="imported_scheme_file")

    st.subheader("Настройки парсинга адреса")
    p1, p2, p3, p4 = st.columns(4)
    with p1:
        cell_segment = st.number_input("Сегмент номера ячейки", 1, 10, 1, key="import_cell_segment")
    with p2:
        row_segment = st.number_input("Сегмент номера ряда", 1, 10, 2, key="import_row_segment")
    with p3:
        tier_segment = st.number_input("Сегмент яруса", 1, 10, 3, key="import_tier_segment")
    with p4:
        tier_mode = st.selectbox("Какие ярусы использовать", ["Только выбранный ярус", "Все ярусы", "Минимальный ярус по каждому ряду"], key="import_tier_mode")
    selected_tier = st.text_input("Выбранный ярус", value="04", disabled=tier_mode != "Только выбранный ярус")

    st.subheader("Параметры виртуальной геометрии")
    g1, g2, g3, g4, g5 = st.columns(5)
    with g1:
        cell_length_m = st.number_input("Длина одной ячейки, м", 0.1, 20.0, 1.2, step=0.1)
    with g2:
        row_width_m = st.number_input("Ширина ряда, м", 0.1, 20.0, 0.8, step=0.1)
    with g3:
        aisle_width_m = st.number_input("Ширина прохода, м", 0.1, 20.0, 2.0, step=0.1)
    with g4:
        numbering_direction = st.selectbox("Направление нумерации", ["по возрастанию", "по убыванию"])
    with g5:
        route_start = st.selectbox("Начало маршрута", ["минимальная ячейка", "максимальная ячейка", "вручную"])

    parsed = False
    if cells_file is not None:
        try:
            source_df, cells_df, diag = read_cell_export(
                cells_file,
                cell_segment=int(cell_segment),
                row_segment=int(row_segment),
                tier_segment=int(tier_segment),
                tier_mode=tier_mode,
                selected_tier=selected_tier,
                source_name=cells_file.name,
            )
            st.session_state["import_source_df"] = source_df
            st.session_state["import_cells_df"] = cells_df
            st.session_state["import_cell_diag"] = diag
            parsed = True
        except Exception as exc:
            st.error(f"Не удалось прочитать выгрузку 1С: {exc}")

    if parsed:
        diag = st.session_state["import_cell_diag"]
        if diag.errors:
            for err in diag.errors:
                st.error(err)
        st.subheader("Предпросмотр выгрузки 1С")
        st.dataframe(st.session_state["import_source_df"].head(100), use_container_width=True)
        st.subheader("Построенная таблица ячеек")
        st.dataframe(st.session_state["import_cells_df"].head(1000), use_container_width=True)

    if scheme_file is not None:
        try:
            scheme_points, color_summary, scheme_warnings = read_scheme_colors(scheme_file)
            st.session_state["import_scheme_points"] = scheme_points
            st.session_state["import_color_summary"] = color_summary
            st.session_state["import_scheme_warnings"] = scheme_warnings
        except Exception as exc:
            st.error(f"Не удалось прочитать цветовую схему: {exc}")

    color_summary = st.session_state.get("import_color_summary")
    color_types = {}
    manual_rows = {}
    if color_summary is not None and not color_summary.empty:
        st.subheader("Найденные цвета и сопоставление типов областей")
        for _, row in color_summary.iterrows():
            color = row["fill_hex"]
            cols = st.columns([1, 1, 2, 3, 2])
            cols[0].markdown(f"<span style='display:inline-block;width:32px;height:18px;background:{color};border:1px solid #333'></span> `{color}`", unsafe_allow_html=True)
            cols[1].write(int(row["excel_cells"]))
            cols[2].write(str(row["example_value"]))
            color_types[color] = cols[3].selectbox("Тип", AREA_TYPES, index=AREA_TYPES.index(str(row.get("area_type", "ряд"))), key=f"color_type_{color}")
            manual_rows[color] = cols[4].text_input("Ряд вручную", value="", key=f"manual_row_{color}")
    elif scheme_file is not None:
        st.warning("В схеме не найдены цветные области. Будет доступен fallback по рядам из выгрузки.")

    if st.button("Построить и сохранить импортированный склад", disabled=not parsed or bool(st.session_state.get("import_cell_diag") and st.session_state["import_cell_diag"].errors)):
        cells_df = st.session_state["import_cells_df"]
        scheme_points = st.session_state.get("import_scheme_points", pd.DataFrame())
        model, model_cells, warnings = build_imported_model(
            cells_df,
            scheme_points,
            color_types,
            manual_rows,
            cell_length_m=cell_length_m,
            row_width_m=row_width_m,
            aisle_width_m=aisle_width_m,
            numbering_direction=numbering_direction,
            route_start=route_start,
        )
        data_path, excel_path = save_imported_model(model, model_cells)
        st.session_state["imported_warehouse_json"] = model
        st.session_state["imported_warehouse_cells"] = model_cells
        st.session_state["warehouse_source"] = "imported_excel"
        st.success(f"Модель сохранена: {data_path}; Excel-экспорт: {excel_path}")
        for warning in warnings:
            st.warning(warning)

    model_cells = st.session_state.get("imported_warehouse_cells")
    if model_cells is not None and not model_cells.empty:
        st.subheader("Диагностика импорта")
        diag = st.session_state.get("import_cell_diag")
        metrics = dict(diag.metrics) if diag else {}
        metrics["количество ячеек без сопоставления со схемой"] = int((~model_cells["matched_to_scheme"]).sum())
        st.dataframe(pd.DataFrame([{"показатель": k, "значение": v} for k, v in metrics.items()]), use_container_width=True)
        for warning in (diag.warnings if diag else []) + st.session_state.get("imported_warehouse_json", {}).get("warnings", []):
            st.warning(warning)

        st.subheader("Фильтры и визуализация")
        f1, f2, f3, f4 = st.columns(4)
        zones = ["Все"] + sorted(model_cells["warehouse_zone"].dropna().astype(str).unique().tolist())
        rows = ["Все"] + sorted(model_cells["row_number"].dropna().astype(str).unique().tolist())
        tiers = ["Все"] + sorted(model_cells["tier"].dropna().astype(str).unique().tolist())
        zone_filter = f1.selectbox("Зона", zones)
        row_filter = f2.selectbox("Ряд", rows)
        tier_filter = f3.selectbox("Ярус", tiers)
        active_filter = f4.selectbox("Доступность", ["Все", "Только активные", "Только заблокированные"])
        filtered = model_cells.copy()
        if zone_filter != "Все":
            filtered = filtered[filtered["warehouse_zone"].astype(str) == zone_filter]
        if row_filter != "Все":
            filtered = filtered[filtered["row_number"].astype(str) == row_filter]
        if tier_filter != "Все":
            filtered = filtered[filtered["tier"].astype(str) == tier_filter]
        if active_filter == "Только активные":
            filtered = filtered[~filtered["disabled"]]
        elif active_filter == "Только заблокированные":
            filtered = filtered[filtered["disabled"]]
        components.html(build_imported_warehouse_html(filtered), height=760, scrolling=True)
        st.dataframe(filtered.head(2000), use_container_width=True)

# ---------- меню ----------

st.sidebar.header("Разделы")

page = st.sidebar.radio(
    "Выберите раздел",
    [
    "Шаблоны файлов",
    "Загрузка данных",
    "Карта РЦ",
    "Карта склада",
    "Виртуальный склад Excel",
    "Импорт склада из Excel",
    "Расчет маршрутов"
]
)

# ---------- шаблоны ----------

if page == "Шаблоны файлов":
    st.header("Шаблоны Excel-файлов")

    st.write("Скачай шаблоны, заполни их своими данными и потом загрузи в приложение.")

    # 1. Шаблон складов
    warehouses_df = pd.DataFrame({
        "warehouse_id": ["veshki_day", "veshki_night"],
        "warehouse_name": ["Дневной Вешки", "Ночной Вешки"],
        "width_mm": [80000, 80000],
        "height_mm": [50000, 50000],
        "comment": ["Основной дневной склад", "Пример второго склада"]
    })

    # 2. Шаблон объектов карты
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
            "Ячейка хранения",
            "Ячейка хранения",
            "Ячейка хранения",
            "Проход",
            "Колонна"
        ]
    })

    # 3. Шаблон расходников
    orders_df = pd.DataFrame({
        "period": ["09.06.2026 6:12:16", "09.06.2026 6:12:16"],
        "warehouse_name": ["Дневной Вешки", "Дневной Вешки"],
        "order_id": [
            "Расходный ордер В20539431",
            "Расходный ордер В20539431"
        ],
        "nomenclature": [
            "Пиво Бельгийское безалкогольное, 500 мл",
            "Вода родниковая газированная, 1,5 л"
        ],
        "characteristic": ["ВАРНИЦА ООО", "СВЕТЛОЯР ООО"],
        "production_date": ["14.04.2026", "17.05.2026"],
        "cell_id": ["24-09-01", "19-58-01"],
        "quantity": [12, 30],
        "cell_balance": [384, 372],
        "print_order": [53722, 53702]
    })

    # 4. Шаблон параметров сборщика
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
            "Скорость движения сборщика, м/с",
            "Время на подбор одной строки",
            "Время на сканирование одной строки",
            "X стартовой точки",
            "Y стартовой точки",
            "X точки завершения",
            "Y точки завершения"
        ]
    })

    download_excel_button(
        "Скачать шаблон складов",
        {"warehouses": warehouses_df},
        "template_warehouses.xlsx"
    )

    download_excel_button(
        "Скачать шаблон карты склада",
        {"map_objects": map_objects_df},
        "template_warehouse_map.xlsx"
    )

    download_excel_button(
        "Скачать шаблон расходников",
        {"orders": orders_df},
        "template_orders.xlsx"
    )

    download_excel_button(
        "Скачать шаблон параметров сборщика",
        {"picker_params": picker_params_df},
        "template_picker_params.xlsx"
    )

    download_excel_button(
        "Скачать все шаблоны одним файлом",
        {
            "warehouses": warehouses_df,
            "map_objects": map_objects_df,
            "orders": orders_df,
            "picker_params": picker_params_df
        },
        "templates_all.xlsx"
    )

# ---------- загрузка данных ----------

elif page == "Загрузка данных":
    st.header("Загрузка данных")

    uploaded_file = st.file_uploader(
        "Загрузите заполненный Excel-файл",
        type=["xlsx"]
    )

    if uploaded_file:
        xls = pd.ExcelFile(uploaded_file)

        st.subheader("Листы в файле")
        st.write(xls.sheet_names)

        selected_sheet = st.selectbox(
            "Выберите лист для просмотра",
            xls.sheet_names
        )

        df = pd.read_excel(uploaded_file, sheet_name=selected_sheet)

        st.subheader("Данные")
        st.dataframe(df)

        st.session_state[selected_sheet] = df

        st.success(f"Лист '{selected_sheet}' загружен в приложение")

# ---------- карта РЦ ----------

elif page == "Карта РЦ":

    st.header("Карта распределительного центра")

    uploaded_pdf = st.file_uploader(
        "Загрузите PDF-план РЦ",
        type=["pdf"]
    )

    if uploaded_pdf:
        st.session_state["rc_pdf_bytes"] = uploaded_pdf.read()
        st.session_state["rc_pdf_name"] = uploaded_pdf.name

    if "rc_pdf_bytes" not in st.session_state:

        st.info("Загрузите PDF план склада")

    else:

        st.success(
            f"Загружен файл: {st.session_state['rc_pdf_name']}"
        )

        pdf_doc = fitz.open(
            stream=st.session_state["rc_pdf_bytes"],
            filetype="pdf"
        )

        page_num = st.number_input(
            "Страница",
            min_value=1,
            max_value=len(pdf_doc),
            value=1
        )

        zoom = st.slider(
            "Масштаб",
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
            "Кликни по плану для получения координат"
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

        st.subheader("Масштаб плана")

        col1, col2 = st.columns(2)

        with col1:
            if st.button("Запомнить точку A"):

                if "last_click_x" in st.session_state:

                    st.session_state["point_a"] = (
                        st.session_state["last_click_x"],
                        st.session_state["last_click_y"]
                    )

        with col2:
            if st.button("Запомнить точку B"):

                if "last_click_x" in st.session_state:

                    st.session_state["point_b"] = (
                        st.session_state["last_click_x"],
                        st.session_state["last_click_y"]
                    )

        point_a = st.session_state.get("point_a")
        point_b = st.session_state.get("point_b")

        st.write("Точка A:", point_a)
        st.write("Точка B:", point_b)

        real_length_mm = st.number_input(
            "Реальная длина между точками (мм)",
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
                "Расстояние в пикселях",
                f"{distance_px:.2f}"
            )

            if distance_px == 0:
                st.warning("Точки A и B совпадают. Выбери две разные точки для расчета масштаба.")
            else:
                mm_per_px = (
                    real_length_mm /
                    distance_px
                )

                st.metric(
                    "мм на пиксель",
                    f"{mm_per_px:.4f}"
                )

                st.session_state[
                    "mm_per_px"
                ] = mm_per_px

        if st.button("Очистить PDF"):

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
# ---------- карта склада ----------

elif page == "Карта склада":
    st.header("Карта склада")

    st.warning(
        "Новый модуль Excel-схемы склада доступен прямо здесь ниже и отдельным пунктом меню "
        "«Виртуальный склад Excel». Если пункт меню не появился, перезапустите Streamlit/start.cmd."
    )

    st.subheader("Виртуальный склад Excel — построить по визуальной Excel-схеме")
    render_virtual_warehouse_excel(show_header=False)
    st.divider()

    st.info(
        "Карту склада теперь можно загрузить прямо здесь: сначала Excel со схемой рядов, "
        "затем при необходимости Excel-выгрузку 1С с фактическими адресами ячеек."
    )

    with st.expander("Какие колонки нужны в Excel", expanded=True):
        st.markdown(
            """
            **Схема рядов:** обязательны `Ряд` и `Кол-во ячеек` / `Количество ячеек`.
            Дополнительно: `Склад`, `Часть ряда`, `Длина ячейки мм`, `Ширина ячейки мм`,
            `Зазор мм`, `Проезд мм`, `Поворот мм`, `Следующий ряд`, `Комментарий`.

            **Выгрузка 1С:** обязательны `Ряд` и `Ячейка`.
            Дополнительно: `Склад`, `Адрес ячейки` / `Складская ячейка`.
            """
        )

    upload_col, one_c_col = st.columns(2)

    with upload_col:
        default_zone = st.text_input(
            "Склад/зона для строк без колонки склада",
            value="Карта склада",
            key="warehouse_map_default_zone",
        )
        layout_file = st.file_uploader(
            "Загрузить Excel схемы склада",
            type=["xlsx"],
            key="warehouse_map_layout_upload",
        )
        if st.button("Построить карту склада", disabled=layout_file is None):
            try:
                sheet_name, segments = import_segments_from_excel(
                    layout_file,
                    default_zone.strip() or "Карта склада",
                )
                st.session_state["warehouse_map_segments"] = segments
                st.session_state["warehouse_map_layout_sheet"] = sheet_name
                st.success(
                    f"Схема загружена: {len(segments)} строк из листа «{sheet_name}»."
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
                                "Файл не похож на табличную схему рядов, поэтому он открыт "
                                "в упрощённом режиме по цветной Excel-разметке."
                            ),
                        }
                    ]
                    st.warning(
                        "Табличные колонки `Ряд` и `Кол-во ячеек` не найдены. "
                        "Я построил склад по цветным ячейкам Excel в блоке выше."
                    )
                except Exception as fallback_exc:
                    st.error(f"Не удалось загрузить схему склада: {exc}. Упрощённый режим тоже не сработал: {fallback_exc}")

    with one_c_col:
        one_c_file = st.file_uploader(
            "Загрузить Excel выгрузки 1С",
            type=["xlsx"],
            key="warehouse_map_1c_upload",
        )
        if st.button("Применить номера из 1С", disabled=one_c_file is None):
            try:
                sheet_name, one_c_cells = import_1c_cells_from_excel(one_c_file)
                st.session_state["warehouse_map_1c_cells"] = one_c_cells
                st.session_state["warehouse_map_1c_sheet"] = sheet_name
                st.success(
                    f"Выгрузка 1С загружена: {len(one_c_cells)} ячеек из листа «{sheet_name}»."
                )
            except Exception as exc:
                st.error(f"Не удалось загрузить выгрузку 1С: {exc}")

    if "warehouse_map_segments" not in st.session_state:
        st.warning("Загрузите Excel схемы склада в блоке выше, чтобы построить карту.")
    else:
        segments = st.session_state["warehouse_map_segments"]
        one_c_cells = st.session_state.get("warehouse_map_1c_cells")

        st.subheader("Загруженная схема рядов")
        st.caption(
            f"Лист схемы: {st.session_state.get('warehouse_map_layout_sheet', 'не указан')}"
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
        metric1.metric("Рядов", len(row_summary))
        metric2.metric("Ячеек", len(cells))
        metric3.metric("Проездов", len(passages))
        metric4.metric(
            "Маршрут, м",
            round(float(zone_summary["total_route_m"].sum()), 1) if not zone_summary.empty else 0,
        )

        map_scale = st.slider(
            "Масштаб отрисовки",
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

        with st.expander("Сводка по складу"):
            st.dataframe(zone_summary, use_container_width=True)

        with st.expander("Сводка по рядам"):
            st.dataframe(row_summary, use_container_width=True)

        with st.expander("Первые 1000 ячеек"):
            st.dataframe(cells.head(1000), use_container_width=True)

        if st.button("Очистить загруженную карту склада"):
            for key in [
                "warehouse_map_segments",
                "warehouse_map_layout_sheet",
                "warehouse_map_1c_cells",
                "warehouse_map_1c_sheet",
            ]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

# ---------- виртуальный склад Excel ----------

elif page == "Виртуальный склад Excel":

    render_virtual_warehouse_excel()

elif page == "Импорт склада из Excel":

    render_imported_warehouse_excel()


# ---------- расчет маршрутов ----------

elif page == "Расчет маршрутов":
    st.header("Расчет маршрутов")

    source = st.radio("Источник склада", ["ручной конструктор", "импорт из Excel"], horizontal=True, key="warehouse_source_choice")
    if source == "импорт из Excel":
        imported = st.session_state.get("imported_warehouse_cells")
        if imported is None:
            try:
                from pathlib import Path
                imported_path = Path("data/imported_warehouse_model.json")
                if imported_path.exists():
                    data = json.loads(imported_path.read_text(encoding="utf-8"))
                    imported = pd.DataFrame(data.get("cells", []))
            except Exception as exc:
                st.warning(f"Не удалось загрузить импортированную модель: {exc}")
        if imported is not None and not imported.empty:
            st.success(f"Для маршрутов выбран импортированный склад: {len(imported)} ячеек.")
            st.dataframe(imported.head(200), use_container_width=True)
        else:
            st.info("Сначала построите импортированный склад во вкладке `Импорт склада из Excel`.")
    else:
        st.info("Выбран старый ручной конструктор / текущая логика склада.")

    st.write("Здесь дальше будет расчет маршрутов по РО.")

    if "orders" in st.session_state:
        st.dataframe(st.session_state["orders"])
    else:
        st.info("Сначала загрузите файл расходников в разделе 'Загрузка данных'.")
