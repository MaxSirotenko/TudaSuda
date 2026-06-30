from pathlib import Path
import re
import html

import pandas as pd


# ВАЖНО: теперь строим карту из очищенного файла
IN_PATH = Path("results/manual_rows/pallets_cleaned.xlsx")

OUT_DIR = Path("results/manual_rows")

PALLETS_XLSX = OUT_DIR / "pallets_numbered.xlsx"
TEMPLATE_XLSX = OUT_DIR / "manual_row_ranges.xlsx"
MAP_HTML = OUT_DIR / "pallets_map_v5_clean_zoom.html"

VERSION = "v5_clean_zoom"

PALLET_ID_START = 0


def business_zone(row_number):
    if pd.isna(row_number):
        return ""

    row_number = int(row_number)

    if 0 <= row_number <= 16:
        return "Бакалея"
    if 17 <= row_number <= 34:
        return "Дневной и напитки"
    if 186 <= row_number <= 213:
        return "Долгосрок"
    if 214 <= row_number <= 215:
        return "Жуки и буфер"
    if 216 <= row_number <= 223:
        return "ДС Вешки"

    return ""


def normalize_col_name(value):
    return str(value).lower().strip().replace(" ", "_")


def find_col(df, variants):
    cols = {normalize_col_name(c): c for c in df.columns}

    for variant in variants:
        key = normalize_col_name(variant)
        if key in cols:
            return cols[key]

    return None


def to_num(series):
    return pd.to_numeric(series, errors="coerce")


def parse_points_to_bbox(value):
    if pd.isna(value):
        return None

    text = str(value)
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)

    if len(nums) < 4:
        return None

    nums = [float(x) for x in nums]

    xs = nums[0::2]
    ys = nums[1::2]

    if not xs or not ys:
        return None

    return min(xs), max(xs), min(ys), max(ys)


def prepare_pallets(df):
    result = df.copy()

    x_min_col = find_col(result, ["x_min", "xmin", "min_x"])
    x_max_col = find_col(result, ["x_max", "xmax", "max_x"])
    y_min_col = find_col(result, ["y_min", "ymin", "min_y"])
    y_max_col = find_col(result, ["y_max", "ymax", "max_y"])

    center_x_col = find_col(result, ["center_x", "cx", "x_center", "x"])
    center_y_col = find_col(result, ["center_y", "cy", "y_center", "y"])

    width_col = find_col(result, ["width", "w", "ширина"])
    height_col = find_col(result, ["height", "h", "длина", "высота"])

    points_col = find_col(result, ["points", "vertices", "coords", "coordinates"])

    if x_min_col and x_max_col and y_min_col and y_max_col:
        result["x_min"] = to_num(result[x_min_col])
        result["x_max"] = to_num(result[x_max_col])
        result["y_min"] = to_num(result[y_min_col])
        result["y_max"] = to_num(result[y_max_col])

    elif center_x_col and center_y_col and width_col and height_col:
        result["center_x"] = to_num(result[center_x_col])
        result["center_y"] = to_num(result[center_y_col])
        result["width"] = to_num(result[width_col])
        result["height"] = to_num(result[height_col])

        result["x_min"] = result["center_x"] - result["width"] / 2
        result["x_max"] = result["center_x"] + result["width"] / 2
        result["y_min"] = result["center_y"] - result["height"] / 2
        result["y_max"] = result["center_y"] + result["height"] / 2

    elif points_col:
        bboxes = result[points_col].apply(parse_points_to_bbox)

        result["x_min"] = bboxes.apply(lambda x: x[0] if x else None)
        result["x_max"] = bboxes.apply(lambda x: x[1] if x else None)
        result["y_min"] = bboxes.apply(lambda x: x[2] if x else None)
        result["y_max"] = bboxes.apply(lambda x: x[3] if x else None)

    else:
        raise ValueError(
            "Не понял структуру файла с паллетами. "
            f"Колонки в файле: {list(result.columns)}"
        )

    result = result.dropna(subset=["x_min", "x_max", "y_min", "y_max"]).copy()

    # Нормализуем min/max
    x1 = result[["x_min", "x_max"]].min(axis=1)
    x2 = result[["x_min", "x_max"]].max(axis=1)
    y1 = result[["y_min", "y_max"]].min(axis=1)
    y2 = result[["y_min", "y_max"]].max(axis=1)

    result["x_min"] = x1
    result["x_max"] = x2
    result["y_min"] = y1
    result["y_max"] = y2

    result["center_x"] = (result["x_min"] + result["x_max"]) / 2
    result["center_y"] = (result["y_min"] + result["y_max"]) / 2
    result["width"] = result["x_max"] - result["x_min"]
    result["height"] = result["y_max"] - result["y_min"]

    # Нумерация: колонками снизу вверх.
    # Если pallet_id уже есть в cleaned-файле, всё равно пересоздаём стабильно.
    result = result.sort_values(
        ["center_x", "center_y"],
        ascending=[True, True],
    ).copy()

    if "pallet_id" in result.columns:
        result = result.drop(columns=["pallet_id"])

    result = result.reset_index(drop=True)
    result.insert(0, "pallet_id", range(PALLET_ID_START, PALLET_ID_START + len(result)))

    return result


def make_template():
    rows = []

    for row_number in range(0, 224):
        rows.append(
            {
                "row_number": row_number,
                "business_zone": business_zone(row_number),
                "from_pallet_id": "",
                "to_pallet_id": "",
                "comment": "",
            }
        )

    return pd.DataFrame(rows)


def make_html_map(pallets):
    x_min = pallets["x_min"].min()
    x_max = pallets["x_max"].max()
    y_min = pallets["y_min"].min()
    y_max = pallets["y_max"].max()

    margin = 3000

    view_x = x_min - margin
    view_y = -y_max - margin
    view_w = (x_max - x_min) + margin * 2
    view_h = (y_max - y_min) + margin * 2

    font_size = 260

    items = []

    for _, row in pallets.iterrows():
        pallet_id = int(row["pallet_id"])

        rx = row["x_min"]
        ry = -row["y_max"]
        rw = row["x_max"] - row["x_min"]
        rh = row["y_max"] - row["y_min"]

        tx = row["center_x"]
        ty = -row["center_y"] + font_size / 3

        svg_x_min = row["x_min"]
        svg_x_max = row["x_max"]
        svg_y_min = -row["y_max"]
        svg_y_max = -row["y_min"]

        tooltip = html.escape(
            f"pallet_id={pallet_id}; "
            f"x={row['center_x']:.0f}; y={row['center_y']:.0f}; "
            f"size={row['width']:.0f}x{row['height']:.0f}"
        )

        items.append(
            f"""
            <g id="pallet-{pallet_id}"
               class="pallet-group"
               data-id="{pallet_id}"
               data-x-min="{svg_x_min:.2f}"
               data-x-max="{svg_x_max:.2f}"
               data-y-min="{svg_y_min:.2f}"
               data-y-max="{svg_y_max:.2f}">

                <rect class="pallet"
                      x="{rx:.2f}" y="{ry:.2f}"
                      width="{rw:.2f}" height="{rh:.2f}">
                    <title>{tooltip}</title>
                </rect>

                <rect class="label-bg"
                      x="{tx - 360:.2f}" y="{ty - 340:.2f}"
                      width="720" height="460"></rect>

                <text class="label"
                      x="{tx:.2f}" y="{ty:.2f}"
                      font-size="{font_size}"
                      text-anchor="middle">{pallet_id}</text>
            </g>
            """
        )

    items_html = "\n".join(items)

    html_text = f"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Карта паллетомест {VERSION}</title>

<style>
    body {{
        margin: 0;
        font-family: Arial, sans-serif;
        background: #f5f5f5;
        overflow: hidden;
    }}

    .panel {{
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        z-index: 10;
        padding: 8px 12px;
        background: white;
        border-bottom: 1px solid #ddd;
        display: flex;
        gap: 10px;
        align-items: center;
        flex-wrap: wrap;
        font-size: 14px;
    }}

    input {{
        padding: 6px 8px;
        width: 220px;
    }}

    button {{
        padding: 6px 10px;
        cursor: pointer;
    }}

    #mapSvg {{
        position: fixed;
        left: 0;
        top: 58px;
        width: 100vw;
        height: calc(100vh - 58px);
        background: white;
        cursor: grab;
    }}

    #mapSvg.dragging {{
        cursor: grabbing;
    }}

    .pallet {{
        fill: #e8e8e8;
        stroke: #333;
        stroke-width: 1.2;
        vector-effect: non-scaling-stroke;
        shape-rendering: geometricPrecision;
    }}

    .label-bg {{
        fill: white;
        opacity: 0;
        pointer-events: none;
    }}

    .label {{
        fill: #111;
        pointer-events: none;
        user-select: none;
    }}

    .pallet-group.selected .pallet {{
        fill: #ffcc66 !important;
        stroke: #cc0000 !important;
        stroke-width: 4 !important;
        vector-effect: non-scaling-stroke;
    }}

    .pallet-group.selected .label-bg {{
        opacity: 0.95;
    }}

    .pallet-group.selected .label {{
        fill: #cc0000 !important;
        font-weight: bold;
        font-size: 420px;
    }}

    body.hide-labels .pallet-group:not(.selected) .label,
    body.hide-labels .pallet-group:not(.selected) .label-bg {{
        display: none;
    }}
</style>
</head>

<body>

<div class="panel">
    <b>Карта паллетомест {VERSION}</b>

    <span>Поиск ID:</span>
    <input id="searchInput" placeholder="Например: 0 или 0-15">

    <button onclick="findPallets()">Показать</button>
    <button onclick="resetView()">Сбросить</button>

    <label>
        <input type="checkbox" onchange="toggleLabels(this)" checked>
        номера
    </label>

    <span>Колесо — зум, ЛКМ — двигать карту</span>

    <span id="status"></span>
</div>

<svg id="mapSvg" viewBox="{view_x:.2f} {view_y:.2f} {view_w:.2f} {view_h:.2f}">
    {items_html}
</svg>

<script>
const svg = document.getElementById("mapSvg");
const initialViewBox = "{view_x:.2f} {view_y:.2f} {view_w:.2f} {view_h:.2f}";
const baseViewWidth = {view_w:.2f};

let isDragging = false;
let dragStart = null;
let viewStart = null;

function getViewBox() {{
    const parts = svg.getAttribute("viewBox").split(/\\s+/).map(Number);
    return {{
        x: parts[0],
        y: parts[1],
        width: parts[2],
        height: parts[3]
    }};
}}

function setViewBox(vb) {{
    svg.setAttribute(
        "viewBox",
        vb.x + " " + vb.y + " " + vb.width + " " + vb.height
    );
    updateStatusZoom();
}}

function getZoom() {{
    const vb = getViewBox();
    return baseViewWidth / vb.width;
}}

function updateStatusZoom() {{
    const status = document.getElementById("status");
    const baseText = status.dataset.text || "";
    const zoom = getZoom().toFixed(1);
    status.innerText = baseText + " | zoom x" + zoom;
}}

function toggleLabels(cb) {{
    document.body.classList.toggle("hide-labels", !cb.checked);
}}

function clearSelected() {{
    document.querySelectorAll(".pallet-group.selected").forEach(el => {{
        el.classList.remove("selected");
    }});
}}

function resetView() {{
    clearSelected();
    svg.setAttribute("viewBox", initialViewBox);
    const status = document.getElementById("status");
    status.dataset.text = "";
    updateStatusZoom();
}}

function parseInput(value) {{
    value = value.trim();
    value = value.replace("–", "-").replace("—", "-");

    if (!value) {{
        return [];
    }}

    if (value.includes("-")) {{
        const parts = value.split("-");
        const start = parseInt(parts[0].trim(), 10);
        const end = parseInt(parts[1].trim(), 10);

        if (isNaN(start) || isNaN(end)) {{
            return [];
        }}

        const ids = [];
        const a = Math.min(start, end);
        const b = Math.max(start, end);

        for (let i = a; i <= b; i++) {{
            ids.push(i);
        }}

        return ids;
    }}

    return value
        .split(/[ ,;]+/)
        .map(x => parseInt(x, 10))
        .filter(x => !isNaN(x));
}}

function bringToFront(el) {{
    if (el && el.parentNode) {{
        el.parentNode.appendChild(el);
    }}
}}

function unionBBox(elements) {{
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;

    elements.forEach(el => {{
        const xMin = parseFloat(el.dataset.xMin);
        const xMax = parseFloat(el.dataset.xMax);
        const yMin = parseFloat(el.dataset.yMin);
        const yMax = parseFloat(el.dataset.yMax);

        minX = Math.min(minX, xMin);
        minY = Math.min(minY, yMin);
        maxX = Math.max(maxX, xMax);
        maxY = Math.max(maxY, yMax);
    }});

    return {{
        x: minX,
        y: minY,
        width: maxX - minX,
        height: maxY - minY
    }};
}}

function findPallets() {{
    clearSelected();

    const value = document.getElementById("searchInput").value;
    const ids = parseInput(value);
    const found = [];
    const foundIds = [];

    ids.forEach(id => {{
        const el = document.getElementById("pallet-" + id);

        if (el) {{
            el.classList.add("selected");
            bringToFront(el);
            found.push(el);
            foundIds.push(id);
        }}
    }});

    const status = document.getElementById("status");

    if (found.length === 0) {{
        status.dataset.text = "Ничего не найдено";
        updateStatusZoom();
        return;
    }}

    const box = unionBBox(found);
    const pad = Math.max(box.width, box.height, 5000) * 0.9;

    setViewBox({{
        x: box.x - pad,
        y: box.y - pad,
        width: box.width + pad * 2,
        height: box.height + pad * 2
    }});

    status.dataset.text = "Найдено: " + found.length + "; ID: " + foundIds.join(", ");
    updateStatusZoom();
}}

svg.addEventListener("wheel", function(event) {{
    event.preventDefault();

    const vb = getViewBox();
    const rect = svg.getBoundingClientRect();

    const mouseXRatio = (event.clientX - rect.left) / rect.width;
    const mouseYRatio = (event.clientY - rect.top) / rect.height;

    const zoomIn = event.deltaY < 0;
    const scale = zoomIn ? 0.82 : 1.22;

    let newWidth = vb.width * scale;
    let newHeight = vb.height * scale;

    const minWidth = baseViewWidth / 150;
    const maxWidth = baseViewWidth * 2.5;

    if (newWidth < minWidth) {{
        newWidth = minWidth;
        newHeight = vb.height * (newWidth / vb.width);
    }}

    if (newWidth > maxWidth) {{
        newWidth = maxWidth;
        newHeight = vb.height * (newWidth / vb.width);
    }}

    const mouseSvgX = vb.x + mouseXRatio * vb.width;
    const mouseSvgY = vb.y + mouseYRatio * vb.height;

    const newX = mouseSvgX - mouseXRatio * newWidth;
    const newY = mouseSvgY - mouseYRatio * newHeight;

    setViewBox({{
        x: newX,
        y: newY,
        width: newWidth,
        height: newHeight
    }});
}}, {{ passive: false }});

svg.addEventListener("mousedown", function(event) {{
    if (event.button !== 0) {{
        return;
    }}

    isDragging = true;
    svg.classList.add("dragging");

    dragStart = {{
        x: event.clientX,
        y: event.clientY
    }};

    viewStart = getViewBox();
}});

window.addEventListener("mousemove", function(event) {{
    if (!isDragging) {{
        return;
    }}

    const rect = svg.getBoundingClientRect();
    const dxPx = event.clientX - dragStart.x;
    const dyPx = event.clientY - dragStart.y;

    const dxWorld = dxPx / rect.width * viewStart.width;
    const dyWorld = dyPx / rect.height * viewStart.height;

    setViewBox({{
        x: viewStart.x - dxWorld,
        y: viewStart.y - dyWorld,
        width: viewStart.width,
        height: viewStart.height
    }});
}});

window.addEventListener("mouseup", function() {{
    isDragging = false;
    svg.classList.remove("dragging");
}});

window.addEventListener("resize", function() {{
    updateStatusZoom();
}});

updateStatusZoom();
</script>

</body>
</html>
"""

    return html_text


def main():
    if not IN_PATH.exists():
        raise FileNotFoundError(
            f"Не найден файл: {IN_PATH}. "
            f"Сначала запусти clean_pallets_geometry.py"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Читаю: {IN_PATH}")

    df = pd.read_excel(IN_PATH)
    pallets = prepare_pallets(df)

    template = make_template()

    with pd.ExcelWriter(PALLETS_XLSX, engine="openpyxl") as writer:
        pallets.to_excel(writer, sheet_name="pallets_numbered", index=False)

    with pd.ExcelWriter(TEMPLATE_XLSX, engine="openpyxl") as writer:
        template.to_excel(writer, sheet_name="manual_row_ranges", index=False)

        info = pd.DataFrame(
            [
                {
                    "instruction": "Открой pallets_map_v5_clean_zoom.html и сверься с PDF/планом."
                },
                {
                    "instruction": "В manual_row_ranges заполни from_pallet_id и to_pallet_id для каждого ряда."
                },
                {
                    "instruction": "Если один ряд состоит из нескольких кусков, добавь вторую строку с тем же row_number."
                },
                {
                    "instruction": "Пример: row_number=1, from_pallet_id=120, to_pallet_id=184."
                },
                {
                    "instruction": "Карта строится из очищенного файла pallets_cleaned.xlsx."
                },
            ]
        )

        info.to_excel(writer, sheet_name="how_to_fill", index=False)

    MAP_HTML.write_text(make_html_map(pallets), encoding="utf-8")

    print()
    print(f"Версия карты: {VERSION}")
    print(f"Источник: {IN_PATH}")
    print(f"Паллетомест на карте: {len(pallets)}")
    print(f"Excel с номерами: {PALLETS_XLSX}")
    print(f"Шаблон для ручных рядов: {TEMPLATE_XLSX}")
    print(f"Карта: {MAP_HTML}")
    print()
    print("Открывай именно:")
    print("results/manual_rows/pallets_map_v5_clean_zoom.html")


if __name__ == "__main__":
    main()