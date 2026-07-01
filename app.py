from streamlit_image_coordinates import streamlit_image_coordinates
import streamlit as st
import pandas as pd
import fitz
from PIL import Image
from io import BytesIO

st.set_page_config(page_title="Симулятор сборки", layout="wide")

st.title("Симулятор скорости сборки")

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


# ---------- меню ----------

st.sidebar.header("Разделы")

page = st.sidebar.radio(
    "Выберите раздел",
    [
    "Шаблоны файлов",
    "Загрузка данных",
    "Карта РЦ",
    "Карта склада",
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

    st.write("Здесь дальше будем рисовать склад по объектам: ячейки, проходы, колонны, препятствия.")

    if "map_objects" in st.session_state:
        st.dataframe(st.session_state["map_objects"])
    else:
        st.info("Сначала загрузите файл карты склада в разделе 'Загрузка данных'.")

# ---------- расчет маршрутов ----------

elif page == "Расчет маршрутов":
    st.header("Расчет маршрутов")

    st.write("Здесь дальше будет расчет маршрутов по РО.")

    if "orders" in st.session_state:
        st.dataframe(st.session_state["orders"])
    else:
        st.info("Сначала загрузите файл расходников в разделе 'Загрузка данных'.")
