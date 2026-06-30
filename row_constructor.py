from pathlib import Path
import re

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


OUT_DIR = Path("results/manual_rows")

CONFIG_PATH = OUT_DIR / "row_constructor_config.xlsx"
OUTPUT_PATH = OUT_DIR / "virtual_cells_from_rows.xlsx"

DEFAULT_SETTINGS = {
    "default_pallet_length_mm": 1200,
    "default_pallet_width_mm": 800,
    "default_gap_between_pallets_mm": 200,
    "default_passage_mm": 2000,
    "default_turn_mm": 2500,
}

SEGMENT_COLUMNS = [
    "zone",
    "row_number",
    "segment_no",
    "pallet_count",
    "pallet_length_mm_override",
    "pallet_width_mm_override",
    "gap_between_pallets_mm_override",
    "passage_after_segment_mm",
    "turn_after_row_mm",
    "next_row_number",
    "comment",
]

EDITOR_COLUMNS = [
    "row_number",
    "segment_no",
    "pallet_count",
    "pallet_length_mm_override",
    "pallet_width_mm_override",
    "gap_between_pallets_mm_override",
    "passage_after_segment_mm",
    "turn_after_row_mm",
    "next_row_number",
    "comment",
]

DEFAULT_SEGMENTS = pd.DataFrame(
    [
        {
            "zone": "Бакалея",
            "row_number": 0,
            "segment_no": 1,
            "pallet_count": 55,
            "pallet_length_mm_override": 0,
            "pallet_width_mm_override": 0,
            "gap_between_pallets_mm_override": 0,
            "passage_after_segment_mm": 2000,
            "turn_after_row_mm": 0,
            "next_row_number": "",
            "comment": "55 паллет, потом проезд",
        },
        {
            "zone": "Бакалея",
            "row_number": 0,
            "segment_no": 2,
            "pallet_count": 75,
            "pallet_length_mm_override": 0,
            "pallet_width_mm_override": 0,
            "gap_between_pallets_mm_override": 0,
            "passage_after_segment_mm": 0,
            "turn_after_row_mm": 2500,
            "next_row_number": "1",
            "comment": "продолжение ряда, потом поворот",
        },
        {
            "zone": "Бакалея",
            "row_number": 1,
            "segment_no": 1,
            "pallet_count": 80,
            "pallet_length_mm_override": 0,
            "pallet_width_mm_override": 0,
            "gap_between_pallets_mm_override": 0,
            "passage_after_segment_mm": 0,
            "turn_after_row_mm": 2500,
            "next_row_number": "2",
            "comment": "",
        },
    ],
    columns=SEGMENT_COLUMNS,
)


def empty_segments_df():
    return pd.DataFrame(columns=SEGMENT_COLUMNS)


def empty_editor_df():
    return pd.DataFrame(
        [
            {
                "row_number": 0,
                "segment_no": 1,
                "pallet_count": 0,
                "pallet_length_mm_override": 0,
                "pallet_width_mm_override": 0,
                "gap_between_pallets_mm_override": 0,
                "passage_after_segment_mm": 0,
                "turn_after_row_mm": 0,
                "next_row_number": "",
                "comment": "",
            }
        ],
        columns=EDITOR_COLUMNS,
    )


def empty_warehouse_row(warehouse_name):
    return pd.DataFrame(
        [
            {
                "zone": warehouse_name,
                "row_number": 0,
                "segment_no": 1,
                "pallet_count": 0,
                "pallet_length_mm_override": 0,
                "pallet_width_mm_override": 0,
                "gap_between_pallets_mm_override": 0,
                "passage_after_segment_mm": 0,
                "turn_after_row_mm": 0,
                "next_row_number": "",
                "comment": "",
            }
        ],
        columns=SEGMENT_COLUMNS,
    )


def safe_key(value):
    value = str(value)
    value = re.sub(r"[^a-zA-Zа-яА-Я0-9_]+", "_", value)
    return value.strip("_") or "warehouse"


def to_int(value, default=0):
    try:
        if pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def prepare_segments_for_editor(df):
    result = df.copy()

    for col in SEGMENT_COLUMNS:
        if col not in result.columns:
            result[col] = ""

    text_cols = ["zone", "next_row_number", "comment"]

    numeric_cols = [
        "row_number",
        "segment_no",
        "pallet_count",
        "pallet_length_mm_override",
        "pallet_width_mm_override",
        "gap_between_pallets_mm_override",
        "passage_after_segment_mm",
        "turn_after_row_mm",
    ]

    for col in text_cols:
        result[col] = result[col].fillna("").astype(str)
        result[col] = result[col].str.replace(r"\.0$", "", regex=True)
        result[col] = result[col].replace("nan", "")
        result[col] = result[col].str.strip()

    for col in numeric_cols:
        result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0).astype(int)

    return result[SEGMENT_COLUMNS].copy()


def prepare_editor_rows(df):
    result = df.copy()

    for col in EDITOR_COLUMNS:
        if col not in result.columns:
            result[col] = ""

    text_cols = ["next_row_number", "comment"]

    numeric_cols = [
        "row_number",
        "segment_no",
        "pallet_count",
        "pallet_length_mm_override",
        "pallet_width_mm_override",
        "gap_between_pallets_mm_override",
        "passage_after_segment_mm",
        "turn_after_row_mm",
    ]

    for col in text_cols:
        result[col] = result[col].fillna("").astype(str)
        result[col] = result[col].str.replace(r"\.0$", "", regex=True)
        result[col] = result[col].replace("nan", "")
        result[col] = result[col].str.strip()

    for col in numeric_cols:
        result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0).astype(int)

    return result[EDITOR_COLUMNS].copy()


def load_config():
    settings = DEFAULT_SETTINGS.copy()
    segments = DEFAULT_SEGMENTS.copy()

    if CONFIG_PATH.exists():
        try:
            loaded_settings = pd.read_excel(CONFIG_PATH, sheet_name="settings")
            loaded_segments = pd.read_excel(CONFIG_PATH, sheet_name="row_segments")

            if not loaded_settings.empty:
                for _, row in loaded_settings.iterrows():
                    param = str(row.get("param", "")).strip()
                    value = row.get("value", None)

                    if param in settings:
                        settings[param] = to_int(value, settings[param])

            segments = loaded_segments.copy()

        except Exception:
            segments = DEFAULT_SEGMENTS.copy()

    segments = prepare_segments_for_editor(segments)

    return settings, segments


def save_config(settings, segments):
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    settings_df = pd.DataFrame(
        [{"param": key, "value": value} for key, value in settings.items()]
    )

    segments_to_save = prepare_segments_for_editor(segments)

    with pd.ExcelWriter(CONFIG_PATH, engine="openpyxl") as writer:
        settings_df.to_excel(writer, sheet_name="settings", index=False)
        segments_to_save.to_excel(writer, sheet_name="row_segments", index=False)


def normalize_segments(df):
    result = prepare_segments_for_editor(df)

    result = result[result["zone"].astype(str).str.strip() != ""].copy()
    result = result[result["pallet_count"] > 0].copy()

    result = result.sort_values(
        ["zone", "row_number", "segment_no"],
        ascending=[True, True, True],
    ).reset_index(drop=True)

    return result


def resolve_value(override_value, default_value):
    override_value = to_int(override_value, 0)

    if override_value > 0:
        return override_value

    return int(default_value)


def get_warehouses(segments):
    if segments.empty:
        return []

    warehouses = (
        segments["zone"]
        .fillna("")
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .drop_duplicates()
        .tolist()
    )

    return sorted(warehouses)


def build_model(segments, settings):
    cells = []
    standard_gaps = []
    passages = []
    transitions = []
    row_summary = []

    global_cell_id = 0

    if segments.empty:
        return (
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
        )

    for (zone, row_number), group in segments.groupby(["zone", "row_number"], sort=True):
        group = group.sort_values("segment_no").copy()

        cursor_mm = 0
        cell_no_in_row = 0

        total_pallets = 0
        total_pallet_length_mm = 0
        total_standard_gaps_mm = 0
        standard_gaps_count = 0
        total_passages_mm = 0

        row_width_max_mm = 0
        last_turn_after_mm = 0
        next_row_number = ""

        need_standard_gap_before_next_cell = False

        for _, segment in group.iterrows():
            segment_no = int(segment["segment_no"])
            pallet_count = int(segment["pallet_count"])

            pallet_length_mm = resolve_value(
                segment["pallet_length_mm_override"],
                settings["default_pallet_length_mm"],
            )

            pallet_width_mm = resolve_value(
                segment["pallet_width_mm_override"],
                settings["default_pallet_width_mm"],
            )

            gap_between_pallets_mm = resolve_value(
                segment["gap_between_pallets_mm_override"],
                settings["default_gap_between_pallets_mm"],
            )

            passage_after_segment_mm = int(segment["passage_after_segment_mm"])
            turn_after_row_mm = int(segment["turn_after_row_mm"])
            next_row_value = str(segment["next_row_number"]).strip()

            row_width_max_mm = max(row_width_max_mm, pallet_width_mm)

            for i in range(pallet_count):
                if need_standard_gap_before_next_cell:
                    gap_start_mm = cursor_mm
                    gap_end_mm = cursor_mm + gap_between_pallets_mm

                    standard_gaps.append(
                        {
                            "zone": zone,
                            "row_number": row_number,
                            "before_global_cell_id": global_cell_id + 1,
                            "gap_start_mm": gap_start_mm,
                            "gap_end_mm": gap_end_mm,
                            "gap_mm": gap_between_pallets_mm,
                            "reason": "standard_gap_between_pallets",
                        }
                    )

                    cursor_mm = gap_end_mm
                    total_standard_gaps_mm += gap_between_pallets_mm
                    standard_gaps_count += 1

                global_cell_id += 1
                cell_no_in_row += 1

                cell_start_mm = cursor_mm
                cell_end_mm = cell_start_mm + pallet_length_mm
                cell_center_mm = cell_start_mm + pallet_length_mm / 2

                cells.append(
                    {
                        "global_cell_id": global_cell_id,
                        "zone": zone,
                        "row_number": row_number,
                        "cell_no_in_row": cell_no_in_row,
                        "segment_no": segment_no,
                        "cell_no_in_segment": i + 1,
                        "pallet_length_mm": pallet_length_mm,
                        "pallet_width_mm": pallet_width_mm,
                        "gap_between_pallets_mm": gap_between_pallets_mm,
                        "distance_from_row_start_mm": cell_center_mm,
                        "cell_start_mm": cell_start_mm,
                        "cell_end_mm": cell_end_mm,
                        "cell_key": f"{zone}-{row_number}-{cell_no_in_row}",
                    }
                )

                cursor_mm = cell_end_mm
                total_pallets += 1
                total_pallet_length_mm += pallet_length_mm

                need_standard_gap_before_next_cell = True

            if passage_after_segment_mm > 0:
                passage_start_mm = cursor_mm
                passage_end_mm = cursor_mm + passage_after_segment_mm

                passages.append(
                    {
                        "zone": zone,
                        "row_number": row_number,
                        "after_segment_no": segment_no,
                        "passage_start_mm": passage_start_mm,
                        "passage_end_mm": passage_end_mm,
                        "passage_width_mm": passage_after_segment_mm,
                    }
                )

                cursor_mm = passage_end_mm
                total_passages_mm += passage_after_segment_mm

                need_standard_gap_before_next_cell = False

            if turn_after_row_mm > 0:
                last_turn_after_mm = turn_after_row_mm

            if next_row_value not in ["", "nan", "None"]:
                next_row_number = next_row_value

        row_length_without_turn_mm = cursor_mm
        row_length_with_turn_mm = cursor_mm + last_turn_after_mm

        row_summary.append(
            {
                "zone": zone,
                "row_number": row_number,
                "pallets_count": total_pallets,
                "row_width_max_mm": row_width_max_mm,
                "pallets_length_mm": total_pallet_length_mm,
                "standard_gaps_count": standard_gaps_count,
                "standard_gaps_length_mm": total_standard_gaps_mm,
                "passages_length_mm": total_passages_mm,
                "row_length_without_turn_mm": row_length_without_turn_mm,
                "turn_after_row_mm": last_turn_after_mm,
                "row_length_with_turn_mm": row_length_with_turn_mm,
                "next_row_number": next_row_number,
            }
        )

        if next_row_number not in ["", "nan", "None"]:
            transitions.append(
                {
                    "zone": zone,
                    "from_row_number": row_number,
                    "to_row_number": next_row_number,
                    "turn_distance_mm": last_turn_after_mm,
                }
            )

    cells_df = pd.DataFrame(cells)
    gaps_df = pd.DataFrame(standard_gaps)
    passages_df = pd.DataFrame(passages)
    transitions_df = pd.DataFrame(transitions)
    row_summary_df = pd.DataFrame(row_summary)

    if row_summary_df.empty:
        zone_summary_df = pd.DataFrame()
    else:
        zone_summary_df = (
            row_summary_df
            .groupby("zone", as_index=False)
            .agg(
                rows_count=("row_number", "count"),
                pallets_count=("pallets_count", "sum"),
                pallets_length_mm=("pallets_length_mm", "sum"),
                standard_gaps_length_mm=("standard_gaps_length_mm", "sum"),
                passages_length_mm=("passages_length_mm", "sum"),
                turns_length_mm=("turn_after_row_mm", "sum"),
                total_route_mm=("row_length_with_turn_mm", "sum"),
            )
        )

        zone_summary_df["total_route_m"] = zone_summary_df["total_route_mm"] / 1000

        if not cells_df.empty:
            route_points = []

            for zone, group in cells_df.groupby("zone"):
                group = group.sort_values("global_cell_id").copy()

                first = group.iloc[0]
                last = group.iloc[-1]

                route_points.append(
                    {
                        "zone": zone,
                        "start_global_cell_id": int(first["global_cell_id"]),
                        "start_row_number": int(first["row_number"]),
                        "start_cell_no_in_row": int(first["cell_no_in_row"]),
                        "start_cell_key": str(first["cell_key"]),
                        "end_global_cell_id": int(last["global_cell_id"]),
                        "end_row_number": int(last["row_number"]),
                        "end_cell_no_in_row": int(last["cell_no_in_row"]),
                        "end_cell_key": str(last["cell_key"]),
                    }
                )

            route_points_df = pd.DataFrame(route_points)

            zone_summary_df = zone_summary_df.merge(
                route_points_df,
                on="zone",
                how="left",
            )

    return cells_df, gaps_df, passages_df, transitions_df, row_summary_df, zone_summary_df


def build_visual_map_html(cells, passages, row_summary, scale):
    if cells.empty:
        return """
        <html>
        <body style="font-family: Arial, sans-serif;">
            <p>Нет ячеек для отображения. Добавь строки в таблицу рядов.</p>
        </body>
        </html>
        """

    left_margin_px = 140
    top_margin_px = 80
    bottom_margin_px = 100

    cells = cells.copy()
    passages = passages.copy()
    row_summary = row_summary.copy()

    rows = (
        row_summary[["zone", "row_number"]]
        .sort_values(["zone", "row_number"])
        .reset_index(drop=True)
    )

    max_visual_row_width_px = max(42, int(cells["pallet_length_mm"].max() * scale))
    row_gap_px = max_visual_row_width_px + 55

    row_positions = {}

    for idx, row in rows.iterrows():
        key = (str(row["zone"]), int(row["row_number"]))
        row_positions[key] = left_margin_px + idx * row_gap_px

    visual_cells = []
    visual_passages = []
    row_visual_lengths = {}

    for (zone, row_number), group in cells.groupby(["zone", "row_number"], sort=True):
        group = group.sort_values(["segment_no", "cell_no_in_segment"]).copy()

        row_passages = passages[
            (passages["zone"].astype(str) == str(zone))
            & (passages["row_number"].astype(int) == int(row_number))
        ].copy()

        passage_by_segment = {}

        if not row_passages.empty:
            for _, p in row_passages.iterrows():
                passage_by_segment[int(p["after_segment_no"])] = float(p["passage_width_mm"])

        cursor_mm = 0
        need_gap_before_next_cell = False
        last_segment_no = None

        for _, c in group.iterrows():
            segment_no = int(c["segment_no"])

            if last_segment_no is not None and segment_no != last_segment_no:
                if last_segment_no in passage_by_segment:
                    passage_width_mm = passage_by_segment[last_segment_no]
                    passage_start_mm = cursor_mm
                    passage_end_mm = cursor_mm + passage_width_mm

                    visual_passages.append(
                        {
                            "zone": zone,
                            "row_number": row_number,
                            "after_segment_no": last_segment_no,
                            "visual_start_mm": passage_start_mm,
                            "visual_end_mm": passage_end_mm,
                            "passage_width_mm": passage_width_mm,
                        }
                    )

                    cursor_mm = passage_end_mm
                    need_gap_before_next_cell = False

            if need_gap_before_next_cell:
                cursor_mm += float(c["gap_between_pallets_mm"])

            visual_cell_length_mm = float(c["pallet_width_mm"])
            visual_row_width_mm = float(c["pallet_length_mm"])

            visual_start_mm = cursor_mm
            visual_end_mm = cursor_mm + visual_cell_length_mm

            row_data = c.to_dict()
            row_data["visual_start_mm"] = visual_start_mm
            row_data["visual_end_mm"] = visual_end_mm
            row_data["visual_cell_length_mm"] = visual_cell_length_mm
            row_data["visual_row_width_mm"] = visual_row_width_mm

            visual_cells.append(row_data)

            cursor_mm = visual_end_mm
            need_gap_before_next_cell = True
            last_segment_no = segment_no

        if last_segment_no is not None and last_segment_no in passage_by_segment:
            passage_width_mm = passage_by_segment[last_segment_no]
            passage_start_mm = cursor_mm
            passage_end_mm = cursor_mm + passage_width_mm

            visual_passages.append(
                {
                    "zone": zone,
                    "row_number": row_number,
                    "after_segment_no": last_segment_no,
                    "visual_start_mm": passage_start_mm,
                    "visual_end_mm": passage_end_mm,
                    "passage_width_mm": passage_width_mm,
                }
            )

            cursor_mm = passage_end_mm

        row_visual_lengths[(str(zone), int(row_number))] = cursor_mm

    visual_cells_df = pd.DataFrame(visual_cells)
    visual_passages_df = pd.DataFrame(visual_passages)

    max_visual_length_mm = max(row_visual_lengths.values()) if row_visual_lengths else 0

    svg_width = int(left_margin_px + len(rows) * row_gap_px + 300)
    svg_height = int(top_margin_px + max_visual_length_mm * scale + bottom_margin_px + 120)

    base_y = top_margin_px + max_visual_length_mm * scale

    elements = []

    for _, row in rows.iterrows():
        zone = str(row["zone"])
        row_number = int(row["row_number"])
        key = (zone, row_number)

        x = row_positions[key]
        row_length_px = row_visual_lengths.get(key, 0) * scale

        elements.append(
            f"""
            <text x="{x + max_visual_row_width_px / 2}" y="28" class="row-label" text-anchor="middle">
                {zone}
            </text>
            <text x="{x + max_visual_row_width_px / 2}" y="46" class="row-label" text-anchor="middle">
                ряд {row_number}
            </text>
            """
        )

        elements.append(
            f"""
            <line x1="{x + max_visual_row_width_px + 10}" y1="{base_y}"
                  x2="{x + max_visual_row_width_px + 10}" y2="{base_y - row_length_px}"
                  class="row-line" />
            """
        )

        elements.append(
            f"""
            <text x="{x + max_visual_row_width_px / 2}" y="{base_y + 24}" class="start-label" text-anchor="middle">
                старт
            </text>
            """
        )

    if not visual_passages_df.empty:
        for _, p in visual_passages_df.iterrows():
            zone = str(p["zone"])
            row_number = int(p["row_number"])
            key = (zone, row_number)

            if key not in row_positions:
                continue

            x = row_positions[key]
            passage_end_mm = float(p["visual_end_mm"])
            passage_width_mm = float(p["passage_width_mm"])

            y = base_y - passage_end_mm * scale
            h = max(6, passage_width_mm * scale)

            elements.append(
                f"""
                <rect class="passage"
                      x="{x:.2f}" y="{y:.2f}"
                      width="{max_visual_row_width_px}" height="{h:.2f}">
                    <title>Проезд {int(passage_width_mm)} мм</title>
                </rect>
                <text x="{x + max_visual_row_width_px + 14:.2f}" y="{y + h / 2:.2f}" class="passage-label">
                    проезд
                </text>
                """
            )

    for _, c in visual_cells_df.iterrows():
        zone = str(c["zone"])
        row_number = int(c["row_number"])
        key = (zone, row_number)

        if key not in row_positions:
            continue

        x = row_positions[key]

        visual_start_mm = float(c["visual_start_mm"])
        visual_end_mm = float(c["visual_end_mm"])
        visual_cell_length_mm = float(c["visual_cell_length_mm"])
        visual_row_width_mm = float(c["visual_row_width_mm"])

        w = max(4, visual_row_width_mm * scale)
        y = base_y - visual_end_mm * scale
        h = max(4, visual_cell_length_mm * scale)

        cell_id = int(c["global_cell_id"])
        cell_no = int(c["cell_no_in_row"])
        cell_key = str(c.get("cell_key", ""))

        elements.append(
            f"""
            <rect class="cell"
                  x="{x:.2f}" y="{y:.2f}"
                  width="{w:.2f}" height="{h:.2f}">
                <title>
                    cell_key={cell_key};
                    global_cell_id={cell_id};
                    ряд={row_number};
                    ячейка в ряду={cell_no};
                    visual_start={int(visual_start_mm)} мм;
                    visual_end={int(visual_end_mm)} мм;
                    visual_width_from_pallet_length={int(visual_row_width_mm)} мм;
                    visual_length_from_pallet_width={int(visual_cell_length_mm)} мм
                </title>
            </rect>
            <text x="{x + w / 2:.2f}" y="{y + h / 2 + 4:.2f}" class="cell-label"
                  text-anchor="middle">
                {cell_no}
            </text>
            """
        )

    elements_html = "\n".join(elements)

    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                margin: 0;
                background: #ffffff;
                font-family: Arial, sans-serif;
                overflow: hidden;
            }}

            .wrapper {{
                width: 100%;
                height: 720px;
                border: 1px solid #444;
                background: #f7f7f7;
                position: relative;
                overflow: hidden;
            }}

            .toolbar {{
                position: absolute;
                top: 8px;
                left: 8px;
                z-index: 10;
                display: flex;
                gap: 6px;
                align-items: center;
                background: rgba(255, 255, 255, 0.92);
                border: 1px solid #ccc;
                padding: 6px;
                border-radius: 6px;
                font-size: 12px;
            }}

            .toolbar button {{
                padding: 4px 8px;
                cursor: pointer;
                border: 1px solid #aaa;
                background: #f4f4f4;
                border-radius: 4px;
            }}

            .toolbar button:hover {{
                background: #e8e8e8;
            }}

            #mapSvg {{
                width: 100%;
                height: 100%;
                background: white;
                cursor: grab;
                display: block;
            }}

            #mapSvg.dragging {{
                cursor: grabbing;
            }}

            .cell {{
                fill: #e8e8e8;
                stroke: #333;
                stroke-width: 1;
                vector-effect: non-scaling-stroke;
            }}

            .cell:hover {{
                fill: #ffcc66;
                stroke: #cc0000;
                stroke-width: 2;
                vector-effect: non-scaling-stroke;
            }}

            .cell-label {{
                font-size: 9px;
                fill: #111;
                pointer-events: none;
                user-select: none;
            }}

            .passage {{
                fill: #9fd3ff;
                stroke: #0066aa;
                stroke-width: 1;
                opacity: 0.9;
                vector-effect: non-scaling-stroke;
            }}

            .passage-label {{
                font-size: 11px;
                fill: #0066aa;
                pointer-events: none;
            }}

            .row-label {{
                font-size: 12px;
                fill: #111;
                font-weight: bold;
                pointer-events: none;
            }}

            .start-label {{
                font-size: 11px;
                fill: #777;
                pointer-events: none;
            }}

            .row-line {{
                stroke: #ccc;
                stroke-width: 1;
                stroke-dasharray: 4 4;
                vector-effect: non-scaling-stroke;
            }}

            #zoomInfo {{
                min-width: 70px;
                color: #333;
            }}
        </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="toolbar">
                <button onclick="zoomBy(0.8)">+</button>
                <button onclick="zoomBy(1.25)">−</button>
                <button onclick="fitAll()">Показать всё</button>
                <span id="zoomInfo">zoom x1.0</span>
            </div>

            <svg id="mapSvg" viewBox="0 0 {svg_width} {svg_height}" preserveAspectRatio="xMidYMid meet">
                {elements_html}
            </svg>
        </div>

        <script>
            const svg = document.getElementById("mapSvg");
            const zoomInfo = document.getElementById("zoomInfo");

            const initialViewBox = {{
                x: 0,
                y: 0,
                width: {svg_width},
                height: {svg_height}
            }};

            let viewBox = {{ ...initialViewBox }};
            let isDragging = false;
            let dragStart = null;
            let viewStart = null;

            function setViewBox(vb) {{
                viewBox = vb;
                svg.setAttribute(
                    "viewBox",
                    vb.x + " " + vb.y + " " + vb.width + " " + vb.height
                );
                updateZoomInfo();
            }}

            function updateZoomInfo() {{
                const zoom = initialViewBox.width / viewBox.width;
                zoomInfo.textContent = "zoom x" + zoom.toFixed(1);
            }}

            function fitAll() {{
                setViewBox({{ ...initialViewBox }});
            }}

            function zoomBy(scale) {{
                const cx = viewBox.x + viewBox.width / 2;
                const cy = viewBox.y + viewBox.height / 2;

                const newWidth = viewBox.width * scale;
                const newHeight = viewBox.height * scale;

                setViewBox({{
                    x: cx - newWidth / 2,
                    y: cy - newHeight / 2,
                    width: newWidth,
                    height: newHeight
                }});
            }}

            function wheelZoom(event) {{
                event.preventDefault();

                const rect = svg.getBoundingClientRect();

                const mouseXRatio = (event.clientX - rect.left) / rect.width;
                const mouseYRatio = (event.clientY - rect.top) / rect.height;

                const mouseSvgX = viewBox.x + mouseXRatio * viewBox.width;
                const mouseSvgY = viewBox.y + mouseYRatio * viewBox.height;

                const scale = event.deltaY < 0 ? 0.82 : 1.22;

                let newWidth = viewBox.width * scale;
                let newHeight = viewBox.height * scale;

                const minWidth = initialViewBox.width / 80;
                const maxWidth = initialViewBox.width * 2.5;

                if (newWidth < minWidth) {{
                    newWidth = minWidth;
                    newHeight = viewBox.height * (newWidth / viewBox.width);
                }}

                if (newWidth > maxWidth) {{
                    newWidth = maxWidth;
                    newHeight = viewBox.height * (newWidth / viewBox.width);
                }}

                const newX = mouseSvgX - mouseXRatio * newWidth;
                const newY = mouseSvgY - mouseYRatio * newHeight;

                setViewBox({{
                    x: newX,
                    y: newY,
                    width: newWidth,
                    height: newHeight
                }});
            }}

            svg.addEventListener("wheel", wheelZoom, {{ passive: false }});

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

                viewStart = {{ ...viewBox }};
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

            updateZoomInfo();
        </script>
    </body>
    </html>
    """

    return html


def save_outputs(settings, segments, cells, gaps, passages, transitions, row_summary, zone_summary):
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    save_config(settings, segments)

    settings_df = pd.DataFrame(
        [{"param": key, "value": value} for key, value in settings.items()]
    )

    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        settings_df.to_excel(writer, sheet_name="settings", index=False)
        prepare_segments_for_editor(segments).to_excel(writer, sheet_name="row_segments", index=False)
        row_summary.to_excel(writer, sheet_name="row_summary", index=False)
        zone_summary.to_excel(writer, sheet_name="warehouse_summary", index=False)
        cells.to_excel(writer, sheet_name="virtual_cells", index=False)
        gaps.to_excel(writer, sheet_name="standard_gaps", index=False)
        passages.to_excel(writer, sheet_name="passages", index=False)
        transitions.to_excel(writer, sheet_name="transitions", index=False)


def main():
    st.set_page_config(
        page_title="Конструктор складов и рядов",
        layout="wide",
    )

    st.title("Конструктор складов и рядов")

    st.write(
        "Создаём склады/зоны отдельными вкладками. Внутри каждой вкладки задаём ряды, "
        "ячейки, проезды и повороты. Начало маршрута — минимальная ячейка склада, "
        "конец — максимальная."
    )

    settings, segments = load_config()
    segments = prepare_segments_for_editor(segments)

    st.subheader("Управление складами")

    create_col, button_col, clear_col, _ = st.columns([2, 1, 1, 5])

    with create_col:
        new_warehouse_name = st.text_input(
            "Название нового склада",
            placeholder="Например: Бакалея",
        )

    with button_col:
        st.write("")
        st.write("")
        if st.button("Создать склад"):
            name = str(new_warehouse_name).strip()

            if not name:
                st.warning("Сначала введи название склада")
            elif name in get_warehouses(segments):
                st.warning(f"Склад уже есть: {name}")
            else:
                segments = pd.concat(
                    [segments, empty_warehouse_row(name)],
                    ignore_index=True,
                )
                save_config(settings, segments)
                st.success(f"Склад создан: {name}")
                st.rerun()

    with clear_col:
        st.write("")
        st.write("")
        if st.button("Очистить всё"):
            save_config(settings, empty_segments_df())
            st.success("Все склады и ряды очищены")
            st.rerun()

    st.subheader("Общие настройки")

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        settings["default_pallet_length_mm"] = st.number_input(
            "Длина паллетоместа, мм",
            min_value=100,
            max_value=5000,
            step=100,
            value=int(settings["default_pallet_length_mm"]),
        )

    with col2:
        settings["default_pallet_width_mm"] = st.number_input(
            "Ширина паллетоместа, мм",
            min_value=100,
            max_value=5000,
            step=100,
            value=int(settings["default_pallet_width_mm"]),
        )

    with col3:
        settings["default_gap_between_pallets_mm"] = st.number_input(
            "Стандартный зазор, мм",
            min_value=0,
            max_value=3000,
            step=50,
            value=int(settings["default_gap_between_pallets_mm"]),
        )

    with col4:
        settings["default_passage_mm"] = st.number_input(
            "Проезд по умолчанию, мм",
            min_value=0,
            max_value=10000,
            step=100,
            value=int(settings["default_passage_mm"]),
        )

    with col5:
        settings["default_turn_mm"] = st.number_input(
            "Поворот по умолчанию, мм",
            min_value=0,
            max_value=10000,
            step=100,
            value=int(settings["default_turn_mm"]),
        )

    st.caption("Override = 0 означает использовать значение из общих настроек.")

    warehouses = get_warehouses(segments)

    updated_segments_parts = []

    if not warehouses:
        st.info("Складов пока нет. Создай склад выше, например «Бакалея».")
    else:
        tabs = st.tabs(warehouses)

        for warehouse_name, tab in zip(warehouses, tabs):
            with tab:
                st.subheader(f"Склад: {warehouse_name}")

                warehouse_segments = segments[
                    segments["zone"].astype(str).str.strip() == warehouse_name
                ].copy()

                editor_df = prepare_editor_rows(warehouse_segments[EDITOR_COLUMNS])

                if editor_df.empty:
                    editor_df = empty_editor_df()

                edited = st.data_editor(
                    editor_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key=f"segments_editor_{safe_key(warehouse_name)}",
                    column_config={
                        "row_number": st.column_config.NumberColumn("Ряд", step=1),
                        "segment_no": st.column_config.NumberColumn("Часть ряда", step=1),
                        "pallet_count": st.column_config.NumberColumn("Кол-во ячеек", step=1),
                        "pallet_length_mm_override": st.column_config.NumberColumn(
                            "Длина паллетоместа override, мм",
                            step=100,
                            help="0 = использовать общую настройку",
                        ),
                        "pallet_width_mm_override": st.column_config.NumberColumn(
                            "Ширина паллетоместа override, мм",
                            step=100,
                            help="0 = использовать общую настройку",
                        ),
                        "gap_between_pallets_mm_override": st.column_config.NumberColumn(
                            "Зазор override, мм",
                            step=50,
                            help="0 = использовать общую настройку",
                        ),
                        "passage_after_segment_mm": st.column_config.NumberColumn(
                            "Проезд после части, мм",
                            step=100,
                            help="Если проезда нет — 0.",
                        ),
                        "turn_after_row_mm": st.column_config.NumberColumn(
                            "Поворот после ряда, мм",
                            step=100,
                            help="Заполняем обычно в последней части ряда.",
                        ),
                        "next_row_number": st.column_config.TextColumn("Следующий ряд"),
                        "comment": st.column_config.TextColumn("Комментарий"),
                    },
                )

                edited = prepare_editor_rows(edited)
                edited["zone"] = warehouse_name
                edited = edited[SEGMENT_COLUMNS]

                updated_segments_parts.append(edited)

                warehouse_calc_segments = normalize_segments(edited)

                (
                    warehouse_cells,
                    warehouse_gaps,
                    warehouse_passages,
                    warehouse_transitions,
                    warehouse_row_summary,
                    warehouse_zone_summary,
                ) = build_model(warehouse_calc_segments, settings)

                metric1, metric2, metric3, metric4 = st.columns(4)

                with metric1:
                    st.metric("Рядов", len(warehouse_row_summary))

                with metric2:
                    st.metric("Ячеек", len(warehouse_cells))

                with metric3:
                    st.metric("Проездов", len(warehouse_passages))

                with metric4:
                    if not warehouse_zone_summary.empty:
                        value = round(float(warehouse_zone_summary["total_route_m"].sum()), 1)
                    else:
                        value = 0
                    st.metric("Маршрут, м", value)

                if not warehouse_zone_summary.empty:
                    st.write("Маршрут склада")
                    st.dataframe(
                        warehouse_zone_summary[
                            [
                                "zone",
                                "start_cell_key",
                                "end_cell_key",
                                "start_global_cell_id",
                                "end_global_cell_id",
                                "total_route_m",
                            ]
                        ],
                        use_container_width=True,
                    )

                st.write("Визуальная карта склада")

                map_scale = st.slider(
                    "Масштаб карты",
                    min_value=0.02,
                    max_value=0.20,
                    value=0.08,
                    step=0.01,
                    key=f"map_scale_{safe_key(warehouse_name)}",
                )

                map_html = build_visual_map_html(
                    cells=warehouse_cells,
                    passages=warehouse_passages,
                    row_summary=warehouse_row_summary,
                    scale=map_scale,
                )

                components.html(map_html, height=760, scrolling=True)

                with st.expander("Сводка по рядам"):
                    st.dataframe(warehouse_row_summary, use_container_width=True)

                with st.expander("Проезды"):
                    st.dataframe(warehouse_passages, use_container_width=True)

                with st.expander("Повороты"):
                    st.dataframe(warehouse_transitions, use_container_width=True)

                with st.expander("Первые 500 ячеек"):
                    st.dataframe(warehouse_cells.head(500), use_container_width=True)

                delete_col, _ = st.columns([1, 7])

                with delete_col:
                    if st.button(
                        f"Удалить склад",
                        key=f"delete_{safe_key(warehouse_name)}",
                    ):
                        segments_after_delete = segments[
                            segments["zone"].astype(str).str.strip() != warehouse_name
                        ].copy()

                        save_config(settings, segments_after_delete)
                        st.success(f"Склад удалён: {warehouse_name}")
                        st.rerun()

    if updated_segments_parts:
        updated_segments = pd.concat(updated_segments_parts, ignore_index=True)
    else:
        updated_segments = empty_segments_df()

    all_calc_segments = normalize_segments(updated_segments)

    (
        all_cells,
        all_gaps,
        all_passages,
        all_transitions,
        all_row_summary,
        all_zone_summary,
    ) = build_model(all_calc_segments, settings)

    st.subheader("Сохранение и общая сводка")

    save_col1, save_col2, _ = st.columns([1, 1, 6])

    with save_col1:
        if st.button("Сохранить конфиг"):
            save_config(settings, updated_segments)
            st.success(f"Конфиг сохранён: {CONFIG_PATH}")

    with save_col2:
        if st.button("Сохранить модель"):
            save_outputs(
                settings=settings,
                segments=updated_segments,
                cells=all_cells,
                gaps=all_gaps,
                passages=all_passages,
                transitions=all_transitions,
                row_summary=all_row_summary,
                zone_summary=all_zone_summary,
            )

            st.success(f"Модель сохранена: {OUTPUT_PATH}")

    st.write("Общая сводка по складам")

    if all_zone_summary.empty:
        st.info("Пока нет рассчитанных ячеек.")
    else:
        st.dataframe(all_zone_summary, use_container_width=True)

    with st.expander("Общая сводка по рядам"):
        st.dataframe(all_row_summary, use_container_width=True)

    with st.expander("Все виртуальные ячейки"):
        st.dataframe(all_cells.head(1000), use_container_width=True)

    st.caption(
        "Начало маршрута по каждому складу считается как минимальная ячейка склада. "
        "Конец маршрута — максимальная ячейка склада. Полный результат сохраняется в Excel."
    )


if __name__ == "__main__":
    main()