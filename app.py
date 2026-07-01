from pathlib import Path
import json
import re
from io import BytesIO

from streamlit_image_coordinates import streamlit_image_coordinates
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import fitz
from PIL import Image

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

DATA_DIR = Path("data/app_uploads")
EXCEL_DIR = DATA_DIR / "excel"
PDF_DIR = DATA_DIR / "pdf"
META_PATH = DATA_DIR / "uploads_meta.json"

REQUIRED_MAP_COLUMNS = ["x_mm", "y_mm", "width_mm", "height_mm"]
WAREHOUSE_ALIASES = ["warehouse_id", "warehouse", "склад", "зона", "камера"]
OBJECT_TYPE_ALIASES = ["object_type", "тип", "тип объекта"]


# ---------- persistence ----------


def ensure_data_dirs():
    EXCEL_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)


def safe_file_stem(value):
    value = re.sub(r"[^a-zA-Zа-яА-Я0-9_.-]+", "_", str(value)).strip("_")
    return value or "upload"


def load_meta():
    ensure_data_dirs()
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"excel_files": [], "pdf_files": []}
    return {"excel_files": [], "pdf_files": []}


def save_meta(meta):
    ensure_data_dirs()
    META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def upsert_meta_item(items, item):
    return [old for old in items if old.get("stored_name") != item["stored_name"]] + [
        item
    ]


def save_uploaded_excel(uploaded_file):
    ensure_data_dirs()
    stored_name = safe_file_stem(uploaded_file.name)
    if not stored_name.lower().endswith(".xlsx"):
        stored_name += ".xlsx"
    path = EXCEL_DIR / stored_name
    bytes_data = uploaded_file.getvalue()
    path.write_bytes(bytes_data)

    xls = pd.ExcelFile(BytesIO(bytes_data))
    meta = load_meta()
    meta["excel_files"] = upsert_meta_item(
        meta.get("excel_files", []),
        {
            "original_name": uploaded_file.name,
            "stored_name": stored_name,
            "sheets": xls.sheet_names,
        },
    )
    save_meta(meta)
    return path, xls.sheet_names


def save_uploaded_pdf(uploaded_file):
    ensure_data_dirs()
    stored_name = safe_file_stem(uploaded_file.name)
    if not stored_name.lower().endswith(".pdf"):
        stored_name += ".pdf"
    path = PDF_DIR / stored_name
    path.write_bytes(uploaded_file.getvalue())

    meta = load_meta()
    meta["pdf_files"] = upsert_meta_item(
        meta.get("pdf_files", []),
        {"original_name": uploaded_file.name, "stored_name": stored_name},
    )
    save_meta(meta)
    return path


def delete_saved_file(kind, stored_name):
    meta = load_meta()
    if kind == "excel":
        path = EXCEL_DIR / stored_name
        meta["excel_files"] = [
            i
            for i in meta.get("excel_files", [])
            if i.get("stored_name") != stored_name
        ]
    else:
        path = PDF_DIR / stored_name
        meta["pdf_files"] = [
            i for i in meta.get("pdf_files", []) if i.get("stored_name") != stored_name
        ]
    if path.exists():
        path.unlink()
    save_meta(meta)


def read_saved_excel(stored_name, sheet_name=None):
    return pd.read_excel(EXCEL_DIR / stored_name, sheet_name=sheet_name)


def load_saved_tables():
    meta = load_meta()
    tables = {}
    for item in meta.get("excel_files", []):
        path = EXCEL_DIR / item["stored_name"]
        if not path.exists():
            continue
        for sheet in item.get("sheets", []):
            try:
                tables[sheet] = pd.read_excel(path, sheet_name=sheet)
            except Exception:
                pass
    return tables


# ---------- excel helpers ----------


def make_excel_file(sheets: dict):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=sheet_name)
    output.seek(0)
    return output


def download_excel_button(label, sheets, file_name):
    st.download_button(
        label=label,
        data=make_excel_file(sheets),
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def normalize_col(value):
    return str(value).strip().lower().replace("ё", "е")


def find_column(columns, aliases):
    normalized = {normalize_col(col): col for col in columns}
    for alias in aliases:
        key = normalize_col(alias)
        if key in normalized:
            return normalized[key]
    return None


def add_warehouse_from_sheet(df, sheet_name):
    result = df.copy()
    warehouse_col = find_column(result.columns, WAREHOUSE_ALIASES)
    if warehouse_col is None:
        result["warehouse_id"] = sheet_name
    elif warehouse_col != "warehouse_id":
        result["warehouse_id"] = result[warehouse_col]
    return result


def collect_map_objects(tables):
    frames = []
    for sheet_name, df in tables.items():
        if df.empty:
            continue
        cols_norm = {normalize_col(c): c for c in df.columns}
        if all(col in cols_norm for col in REQUIRED_MAP_COLUMNS):
            item = add_warehouse_from_sheet(df, sheet_name)
            object_type_col = find_column(item.columns, OBJECT_TYPE_ALIASES)
            if object_type_col is None:
                item["object_type"] = "cell"
            elif object_type_col != "object_type":
                item["object_type"] = item[object_type_col]
            frames.append(item)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def build_objects_map_html(objects, title="Карта", scale=0.02):
    if objects.empty:
        return "<p>Нет объектов карты для отображения.</p>"
    df = objects.copy()
    for col in REQUIRED_MAP_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=REQUIRED_MAP_COLUMNS)
    if df.empty:
        return "<p>В объектах карты нет корректных координат.</p>"

    min_x = float(df["x_mm"].min())
    min_y = float(df["y_mm"].min())
    max_x = float((df["x_mm"] + df["width_mm"]).max())
    max_y = float((df["y_mm"] + df["height_mm"]).max())
    pad = 80
    width = max(600, int((max_x - min_x) * scale + pad * 2))
    height = max(420, int((max_y - min_y) * scale + pad * 2))

    colors = {
        "cell": "#d8ead2",
        "aisle": "#bde0fe",
        "obstacle": "#f4b7b7",
        "склад": "#d8ead2",
    }
    elements = []
    for _, row in df.iterrows():
        x = pad + (float(row["x_mm"]) - min_x) * scale
        y = pad + (float(row["y_mm"]) - min_y) * scale
        w = max(3, float(row["width_mm"]) * scale)
        h = max(3, float(row["height_mm"]) * scale)
        obj_type = str(row.get("object_type", "cell")).strip().lower()
        fill = colors.get(obj_type, "#eeeeee")
        label = str(row.get("object_id", row.get("cell_id", "")))
        elements.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" fill="{fill}" stroke="#333"><title>{label}</title></rect>'
        )
        if w > 20 and h > 12 and label:
            elements.append(
                f'<text x="{x+w/2:.2f}" y="{y+h/2+4:.2f}" text-anchor="middle" font-size="10">{label}</text>'
            )
    return f"""
    <div style='border:1px solid #bbb; overflow:auto; height:650px'>
      <svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' style='background:white'>
        <text x='16' y='28' font-size='18' font-weight='bold'>{title}</text>
        {''.join(elements)}
      </svg>
    </div>
    """


st.title("Симулятор скорости сборки")
st.sidebar.header("Разделы")
page = st.sidebar.radio(
    "Выберите раздел",
    [
        "Шаблоны файлов",
        "Загрузка данных",
        "Карта РЦ",
        "Карта склада",
        "Расчет маршрутов",
    ],
)

# ---------- шаблоны ----------
if page == "Шаблоны файлов":
    st.header("Шаблоны Excel-файлов")
    st.write("Скачай шаблоны, заполни их своими данными и потом загрузи в приложение.")
    warehouses_df = pd.DataFrame(
        {
            "warehouse_id": ["veshki_day", "veshki_night"],
            "warehouse_name": ["Дневной Вешки", "Ночной Вешки"],
            "width_mm": [80000, 80000],
            "height_mm": [50000, 50000],
            "comment": ["Основной дневной склад", "Пример второго склада"],
        }
    )
    map_objects_df = pd.DataFrame(
        {
            "warehouse_id": ["veshki_day"] * 5,
            "object_type": ["cell", "cell", "cell", "aisle", "obstacle"],
            "object_id": [
                "24-09-01",
                "25-09-01",
                "26-09-01",
                "aisle_001",
                "column_001",
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
                "Колонна",
            ],
        }
    )
    orders_df = pd.DataFrame(
        {
            "period": ["09.06.2026 6:12:16", "09.06.2026 6:12:16"],
            "warehouse_name": ["Дневной Вешки", "Дневной Вешки"],
            "order_id": ["Расходный ордер В20539431", "Расходный ордер В20539431"],
            "nomenclature": [
                "Пиво Бельгийское безалкогольное, 500 мл",
                "Вода родниковая газированная, 1,5 л",
            ],
            "characteristic": ["ВАРНИЦА ООО", "СВЕТЛОЯР ООО"],
            "production_date": ["14.04.2026", "17.05.2026"],
            "cell_id": ["24-09-01", "19-58-01"],
            "quantity": [12, 30],
            "cell_balance": [384, 372],
            "print_order": [53722, 53702],
        }
    )
    picker_params_df = pd.DataFrame(
        {
            "parameter": [
                "walk_speed_m_s",
                "pick_time_per_line_sec",
                "scan_time_per_line_sec",
                "start_x_mm",
                "start_y_mm",
                "finish_x_mm",
                "finish_y_mm",
            ],
            "value": [1.2, 5, 1, 0, 0, 0, 0],
            "comment": [
                "Скорость движения сборщика, м/с",
                "Время на подбор одной строки",
                "Время на сканирование одной строки",
                "X стартовой точки",
                "Y стартовой точки",
                "X точки завершения",
                "Y точки завершения",
            ],
        }
    )
    download_excel_button(
        "Скачать шаблон складов",
        {"warehouses": warehouses_df},
        "template_warehouses.xlsx",
    )
    download_excel_button(
        "Скачать шаблон карты склада",
        {"map_objects": map_objects_df},
        "template_warehouse_map.xlsx",
    )
    download_excel_button(
        "Скачать шаблон расходников", {"orders": orders_df}, "template_orders.xlsx"
    )
    download_excel_button(
        "Скачать шаблон параметров сборщика",
        {"picker_params": picker_params_df},
        "template_picker_params.xlsx",
    )
    download_excel_button(
        "Скачать все шаблоны одним файлом",
        {
            "warehouses": warehouses_df,
            "map_objects": map_objects_df,
            "orders": orders_df,
            "picker_params": picker_params_df,
        },
        "templates_all.xlsx",
    )

elif page == "Загрузка данных":
    st.header("Загрузка данных")
    st.info("Файлы сохраняются на диск и остаются доступными после закрытия проекта.")
    uploaded_file = st.file_uploader("Загрузите заполненный Excel-файл", type=["xlsx"])
    if uploaded_file:
        _, sheet_names = save_uploaded_excel(uploaded_file)
        st.success(
            f"Файл сохранён: {uploaded_file.name}. Листы: {', '.join(sheet_names)}"
        )

    meta = load_meta()
    st.subheader("Сохранённые Excel-файлы")
    if not meta.get("excel_files"):
        st.info("Сохранённых Excel-файлов пока нет.")
    for item in meta.get("excel_files", []):
        cols = st.columns([3, 4, 1])
        cols[0].write(item["original_name"])
        cols[1].write(", ".join(item.get("sheets", [])))
        if cols[2].button("Удалить", key=f"del_excel_{item['stored_name']}"):
            delete_saved_file("excel", item["stored_name"])
            st.rerun()

    if meta.get("excel_files"):
        selected_file = st.selectbox(
            "Файл для просмотра", [i["stored_name"] for i in meta["excel_files"]]
        )
        selected_item = next(
            i for i in meta["excel_files"] if i["stored_name"] == selected_file
        )
        selected_sheet = st.selectbox(
            "Лист для просмотра", selected_item.get("sheets", [])
        )
        st.dataframe(
            read_saved_excel(selected_file, selected_sheet), use_container_width=True
        )

elif page == "Карта РЦ":
    st.header("Карта распределительного центра")
    uploaded_pdf = st.file_uploader("Загрузите PDF-план РЦ", type=["pdf"])
    if uploaded_pdf:
        save_uploaded_pdf(uploaded_pdf)
        st.success(f"PDF сохранён: {uploaded_pdf.name}")
        st.rerun()

    meta = load_meta()
    pdf_files = meta.get("pdf_files", [])
    if pdf_files:
        chosen = st.selectbox(
            "Сохранённый PDF-план", [i["stored_name"] for i in pdf_files]
        )
        if st.button("Удалить выбранный PDF"):
            delete_saved_file("pdf", chosen)
            st.rerun()
        pdf_bytes = (PDF_DIR / chosen).read_bytes()
        pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_num = st.number_input(
            "Страница", min_value=1, max_value=len(pdf_doc), value=1
        )
        zoom = st.slider(
            "Масштаб PDF", min_value=0.5, max_value=8.0, value=2.0, step=0.5
        )
        pix = pdf_doc[page_num - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        clicked_point = streamlit_image_coordinates(
            img, key=f"pdf_click_{chosen}_{page_num}_{zoom}"
        )
        if clicked_point:
            st.session_state["last_click_x"] = clicked_point["x"]
            st.session_state["last_click_y"] = clicked_point["y"]
            st.metric("X", clicked_point["x"])
            st.metric("Y", clicked_point["y"])
    else:
        st.info("Загрузите PDF план РЦ.")

    st.subheader("Карта РЦ по загруженному Excel")
    map_objects = collect_map_objects(load_saved_tables())
    if map_objects.empty:
        st.info(
            "Для построения карты загрузите Excel с колонками x_mm, y_mm, width_mm, height_mm."
        )
    else:
        scale = st.slider("Масштаб объектов", 0.005, 0.2, 0.02, 0.005)
        components.html(
            build_objects_map_html(map_objects, "Общая карта РЦ", scale),
            height=680,
            scrolling=True,
        )

elif page == "Карта склада":
    st.header("Карта склада")

    st.subheader("Карта по сохранённым Excel-объектам")
    tables = load_saved_tables()
    map_objects = collect_map_objects(tables)
    if map_objects.empty:
        st.info(
            "Сначала загрузите Excel-карту в разделе 'Загрузка данных'. Листы Excel могут быть отдельными складами."
        )
    else:
        warehouses = sorted(
            map_objects["warehouse_id"]
            .fillna("")
            .astype(str)
            .replace("", "Без склада")
            .unique()
        )
        warehouse = st.selectbox("Склад", warehouses)
        view_df = map_objects[
            map_objects["warehouse_id"].fillna("").astype(str).replace("", "Без склада")
            == warehouse
        ]
        st.dataframe(view_df, use_container_width=True)
        scale = st.slider("Масштаб карты", 0.005, 0.2, 0.03, 0.005)
        components.html(
            build_objects_map_html(view_df, f"Карта склада: {warehouse}", scale),
            height=680,
            scrolling=True,
        )
        st.caption(
            "Если в листе нет колонки warehouse_id/Склад, название листа считается названием склада."
        )

    st.divider()
    st.subheader("Карта по схеме рядов")
    st.info(
        "Карту склада также можно построить прямо здесь: сначала Excel со схемой рядов, "
        "затем при необходимости Excel-выгрузку 1С с фактическими адресами ячеек."
    )

    with st.expander("Какие колонки нужны в Excel", expanded=False):
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
                st.error(f"Не удалось загрузить схему склада: {exc}")

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
            _gaps,
            passages,
            _transitions,
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
            round(float(zone_summary["total_route_m"].sum()), 1)
            if not zone_summary.empty
            else 0,
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

elif page == "Расчет маршрутов":
    st.header("Расчет маршрутов")
    tables = load_saved_tables()
    if "orders" in tables:
        st.dataframe(tables["orders"], use_container_width=True)
    else:
        st.info("Сначала загрузите файл расходников в разделе 'Загрузка данных'.")
