from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from time import perf_counter

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit.runtime.scriptrunner import get_script_run_ctx
from openpyxl import load_workbook

from warehouse_diagnostics import build_diagnostics
from warehouse_excel_parser import parse_warehouse_excel
from warehouse_model import WarehouseCell, WarehouseModel, WarehouseRow, WarehouseSheet
from warehouse_placement import apply_cell_addresses, apply_placements, import_cell_addresses, import_placements
from warehouse_visualization import build_virtual_warehouse_html, prepare_render_cache
from warehouse_geometry_model import (
    GeometrySettings,
    append_manual_change,
    apply_manual_overrides,
    build_geometry_html,
    build_geometry_model,
    cell_key,
    clear_manual_overrides,
    clear_row_settings,
    default_row_config,
    detect_column_mapping,
    empty_aisle_config,
    export_current_model_excel_bytes,
    get_excel_sheet_names as get_geometry_sheet_names,
    load_geometry_model,
    load_manual_overrides,
    manual_change_counts,
    normalize_cell_table,
    read_cell_table,
    rebuild_geometry_from_cells,
    save_geometry_model,
)

from warehouse_inventory_placement import (
    attach_placements_to_model,
    auto_place_unplaced,
    calculate_basic_weight_placement,
    clear_calculated_placements,
    clear_placement_state,
    delete_placement,
    detect_inventory_columns,
    export_placements_excel_bytes,
    get_inventory_sheet_names,
    import_inventory,
    load_placement_state,
    manual_place,
    normalize_inventory_table,
    placement_diagnostics,
    read_inventory_table,
    save_placement_state,
    update_placement_qty,
    move_placement,
)

from warehouse_receipts import (
    clear_receipts_state,
    detect_receipt_columns,
    export_receipts_excel_bytes,
    get_receipt_sheet_names,
    load_receipts_state,
    make_receipts_state,
    normalize_receipt_table,
    read_receipt_table,
    save_receipts_state,
)
st.set_page_config(page_title="Симулятор сборки", layout="wide")

APP_BUILD_LABEL = "virtual-excel-only-2026-07-04"
MODEL_VERSION = 1
LAST_IMPORT_DIR = Path("data/last_import")
MODEL_PATH = LAST_IMPORT_DIR / "warehouse_model.json"
RENDER_CACHE_PATH = LAST_IMPORT_DIR / "render_cache.json"
META_PATH = LAST_IMPORT_DIR / "import_meta.json"
RENDER_SETTINGS_PATH = LAST_IMPORT_DIR / "render_settings.json"

DEFAULT_RENDER_LABEL_SETTINGS = {
    "show_row_labels": True,
    "show_cell_labels": True,
    "show_occupancy_labels": True,
    "show_aisle_labels": True,
    "label_mode": "Авто",
    "row_label_position": "авто",
}

DEFAULT_RENDER_COLOR_SETTINGS = {
    "cell_color": "#DCEBFF",
    "deep_lane_cell_color": "#CFE8D5",
    "aisle_color": "#F2F2F2",
    "top_road_color": "#FFE8A3",
    "bottom_road_color": "#FFE8A3",
    "exit_color": "#FFCC80",
    "selected_cell_color": "#FF7043",
    "hover_cell_color": "#FFF59D",
    "occupied_cell_color": "#90CAF9",
    "deep_lane_partial_color": "#A5D6A7",
    "deep_lane_full_color": "#66BB6A",
}


@st.cache_data(show_spinner=False)
def get_git_release_info() -> dict[str, str]:
    repo_dir = Path(__file__).resolve().parent

    def git_text(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    merge_title = git_text("log", "--merges", "-1", "--pretty=%s")
    commit_title = git_text("log", "-1", "--pretty=%s")
    commit_hash = git_text("rev-parse", "--short", "HEAD")
    commit_date = git_text("log", "-1", "--date=short", "--pretty=%cd")
    return {
        "merge_title": merge_title,
        "commit_title": commit_title,
        "display_label": "Последний merge" if merge_title else "Последний commit",
        "display_title": merge_title or commit_title or "нет данных Git",
        "commit_hash": commit_hash or "—",
        "commit_date": commit_date or "—",
    }


def render_git_release_badge() -> None:
    info = get_git_release_info()
    st.sidebar.caption(f"{info['display_label']}: {info['display_title']}")
    st.sidebar.caption(f"Git commit: {info['commit_hash']} · {info['commit_date']}")


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@st.cache_data(show_spinner=False)
def get_excel_sheet_names(file_bytes: bytes, _content_hash: str) -> list[str]:
    wb = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


@st.cache_data(show_spinner=False)
def parse_warehouse_excel_cached(file_bytes: bytes, _content_hash: str, sheet_names: tuple[str, ...] | None):
    return parse_warehouse_excel(file_bytes, sheet_names=list(sheet_names) if sheet_names else None)


@st.cache_data(show_spinner=False)
def prepare_render_cache_cached(model_payload: dict) -> dict:
    return prepare_render_cache(model_from_dict(model_payload))


@st.cache_data(show_spinner=False)
def build_virtual_warehouse_html_cached(sheet_payload: dict, scale: int, summary_mode: bool):
    return build_virtual_warehouse_html(sheet_from_dict(sheet_payload), scale=scale, summary_mode=summary_mode)


@st.cache_data(show_spinner=False)
def read_cell_table_cached(file_bytes: bytes, content_hash: str, sheet_name: str, header_rows: int) -> pd.DataFrame:
    return read_cell_table(file_bytes, sheet_name, header_rows=header_rows)


@st.cache_data(show_spinner=False)
def normalize_cell_table_cached(table_payload: str, mapping_payload: str) -> tuple[pd.DataFrame, list[dict]]:
    df = pd.read_json(table_payload, orient="split")
    mapping = json.loads(mapping_payload)
    return normalize_cell_table(df, mapping)


@st.cache_data(show_spinner=False)
def build_geometry_html_cached(model_payload: str, scale: float, detailed: bool, label_settings_payload: str) -> str:
    return build_geometry_html(json.loads(model_payload), scale=scale, detailed=detailed, label_settings=json.loads(label_settings_payload))


@st.cache_data(show_spinner=False)
def read_inventory_table_cached(file_bytes: bytes, content_hash: str, sheet_name: str, header_rows: int) -> pd.DataFrame:
    return read_inventory_table(file_bytes, sheet_name, header_rows=header_rows)


@st.cache_data(show_spinner=False)
def normalize_inventory_table_cached(table_payload: str, mapping_payload: str) -> tuple[pd.DataFrame, list[dict]]:
    df = pd.read_json(table_payload, orient="split")
    mapping = json.loads(mapping_payload)
    return normalize_inventory_table(df, mapping)


@st.cache_data(show_spinner=False)
def read_receipt_table_cached(file_bytes: bytes, content_hash: str, sheet_name: str, header_rows: int) -> pd.DataFrame:
    return read_receipt_table(file_bytes, sheet_name, header_rows=header_rows)


@st.cache_data(show_spinner=False)
def normalize_receipt_table_cached(table_payload: str, mapping_payload: str) -> tuple[pd.DataFrame, dict, list[dict]]:
    df = pd.read_json(table_payload, orient="split")
    mapping = json.loads(mapping_payload)
    return normalize_receipt_table(df, mapping)


def model_to_dict(model: WarehouseModel) -> dict:
    payload = asdict(model)
    payload["model_version"] = MODEL_VERSION
    return payload


def cell_from_dict(data: dict) -> WarehouseCell:
    return WarehouseCell(**{k: v for k, v in data.items() if k in WarehouseCell.__dataclass_fields__})


def row_from_dict(data: dict) -> WarehouseRow:
    row_data = {k: v for k, v in data.items() if k in WarehouseRow.__dataclass_fields__ and k != "potential_cells"}
    row = WarehouseRow(**row_data)
    row.potential_cells = [cell_from_dict(item) for item in data.get("potential_cells", [])]
    return row


def sheet_from_dict(data: dict) -> WarehouseSheet:
    sheet_data = {k: v for k, v in data.items() if k in WarehouseSheet.__dataclass_fields__ and k != "rows"}
    sheet = WarehouseSheet(**sheet_data)
    sheet.rows = [row_from_dict(item) for item in data.get("rows", [])]
    return sheet


def model_from_dict(data: dict) -> WarehouseModel:
    version = data.get("model_version")
    if version != MODEL_VERSION:
        raise ValueError(f"Неподдерживаемая версия модели: {version}; ожидается {MODEL_VERSION}.")
    model = WarehouseModel(sheets=[sheet_from_dict(item) for item in data.get("sheets", [])])
    model.diagnostics = data.get("diagnostics", [])
    return model


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def load_render_settings() -> dict:
    settings = dict(DEFAULT_RENDER_LABEL_SETTINGS)
    settings["colors"] = dict(DEFAULT_RENDER_COLOR_SETTINGS)
    if RENDER_SETTINGS_PATH.exists():
        try:
            payload = json.loads(RENDER_SETTINGS_PATH.read_text(encoding="utf-8-sig"))
            settings.update({key: payload.get(key, value) for key, value in DEFAULT_RENDER_LABEL_SETTINGS.items()})
            colors = dict(DEFAULT_RENDER_COLOR_SETTINGS)
            colors.update(payload.get("colors", {}))
            settings["colors"] = colors
        except json.JSONDecodeError:
            pass
    return settings


def save_render_settings(settings: dict) -> None:
    payload = {}
    if RENDER_SETTINGS_PATH.exists():
        try:
            payload = json.loads(RENDER_SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            payload = {}
    payload.update({key: settings.get(key, value) for key, value in DEFAULT_RENDER_LABEL_SETTINGS.items()})
    existing_colors = payload.get("colors", {}) if isinstance(payload.get("colors"), dict) else {}
    colors = dict(existing_colors)
    colors.update({key: settings.get("colors", {}).get(key, value) for key, value in DEFAULT_RENDER_COLOR_SETTINGS.items()})
    payload["colors"] = colors
    write_json_atomic(RENDER_SETTINGS_PATH, payload)


def render_label_settings_editor(settings: dict) -> dict:
    with st.expander("Настройки подписей", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        settings["show_row_labels"] = c1.checkbox("Показывать номера рядов", value=bool(settings.get("show_row_labels", True)), key="show_row_labels")
        settings["show_cell_labels"] = c2.checkbox("Показывать номера ячеек", value=bool(settings.get("show_cell_labels", True)), key="show_cell_labels")
        settings["show_occupancy_labels"] = c3.checkbox("Показывать занятость ячеек", value=bool(settings.get("show_occupancy_labels", True)), key="show_occupancy_labels")
        settings["show_aisle_labels"] = c4.checkbox("Показывать подписи проездов", value=bool(settings.get("show_aisle_labels", True)), key="show_aisle_labels")
        c5, c6 = st.columns(2)
        label_modes = ["Авто", "Полные", "Короткие", "Только при наведении"]
        row_positions = ["авто", "сверху", "снизу", "сверху и снизу"]
        settings["label_mode"] = c5.selectbox("Режим подписей", label_modes, index=label_modes.index(settings.get("label_mode", "Авто")) if settings.get("label_mode") in label_modes else 0, key="label_mode")
        settings["row_label_position"] = c6.selectbox("Положение номера ряда", row_positions, index=row_positions.index(settings.get("row_label_position", "авто")) if settings.get("row_label_position") in row_positions else 0, key="row_label_position")
        st.caption("Если подпись не помещается, карта уменьшает шрифт до 4 px, затем скрывает текст на карте, но оставляет полную информацию в tooltip.")
    return settings


def render_color_settings_editor(settings: dict) -> dict:
    colors = dict(DEFAULT_RENDER_COLOR_SETTINGS)
    colors.update(settings.get("colors", {}))
    with st.expander("Настройки цветов карты", expanded=False):
        c1, c2, c3 = st.columns(3)
        colors["cell_color"] = c1.color_picker("Цвет обычных ячеек", colors["cell_color"], key="color_cell")
        colors["deep_lane_cell_color"] = c2.color_picker("Цвет набивных ячеек", colors["deep_lane_cell_color"], key="color_deep_lane")
        colors["aisle_color"] = c3.color_picker("Цвет проездов между рядами", colors["aisle_color"], key="color_aisle")
        c4, c5, c6 = st.columns(3)
        colors["top_road_color"] = c4.color_picker("Цвет верхнего проезда", colors["top_road_color"], key="color_top_road")
        colors["bottom_road_color"] = c5.color_picker("Цвет нижнего проезда", colors["bottom_road_color"], key="color_bottom_road")
        colors["exit_color"] = c6.color_picker("Цвет выходов", colors["exit_color"], key="color_exit")
        c7, c8, c9 = st.columns(3)
        colors["selected_cell_color"] = c7.color_picker("Цвет выбранной ячейки", colors["selected_cell_color"], key="color_selected")
        colors["hover_cell_color"] = c8.color_picker("Цвет ячейки при наведении", colors["hover_cell_color"], key="color_hover")
        colors["occupied_cell_color"] = c9.color_picker("Цвет занятой ячейки", colors["occupied_cell_color"], key="color_occupied")
        c10, c11 = st.columns(2)
        colors["deep_lane_partial_color"] = c10.color_picker("Цвет частично занятой набивной", colors["deep_lane_partial_color"], key="color_deep_partial")
        colors["deep_lane_full_color"] = c11.color_picker("Цвет полностью занятой набивной", colors["deep_lane_full_color"], key="color_deep_full")
        b1, b2 = st.columns(2)
        if b1.button("Сохранить цвета", key="save_render_colors"):
            settings["colors"] = colors
            save_render_settings(settings)
            st.success("Цвета карты сохранены.")
        if b2.button("Сбросить цвета по умолчанию", key="reset_render_colors"):
            colors = dict(DEFAULT_RENDER_COLOR_SETTINGS)
            settings["colors"] = colors
            save_render_settings(settings)
            st.success("Цвета сброшены по умолчанию.")
            st.rerun()
    settings["colors"] = colors
    return settings


def render_map_settings_editor() -> dict:
    settings = load_render_settings()
    settings = render_label_settings_editor(settings)
    settings = render_color_settings_editor(settings)
    save_render_settings(settings)
    return settings


def save_uploaded_copy(uploaded_file, target_name: str) -> str:
    if uploaded_file is None:
        return ""
    LAST_IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = LAST_IMPORT_DIR / target_name
    target.write_bytes(uploaded_file.getvalue())
    return str(target)


def save_last_import(model: WarehouseModel, render_cache: dict, meta: dict) -> float:
    started = perf_counter()
    write_json_atomic(MODEL_PATH, model_to_dict(model))
    write_json_atomic(RENDER_CACHE_PATH, render_cache)
    write_json_atomic(META_PATH, meta)
    return perf_counter() - started


def load_last_import() -> tuple[WarehouseModel | None, dict, dict, str | None]:
    if not MODEL_PATH.exists():
        return None, {}, {}, None
    try:
        model_payload = json.loads(MODEL_PATH.read_text(encoding="utf-8-sig"))
        model = model_from_dict(model_payload)
        render_cache = json.loads(RENDER_CACHE_PATH.read_text(encoding="utf-8-sig")) if RENDER_CACHE_PATH.exists() else prepare_render_cache(model)
        meta = json.loads(META_PATH.read_text(encoding="utf-8-sig")) if META_PATH.exists() else {}
        return model, render_cache, meta, None
    except Exception as exc:
        return None, {}, {}, str(exc)


def clear_last_import() -> None:
    if LAST_IMPORT_DIR.exists():
        shutil.rmtree(LAST_IMPORT_DIR)


def model_stats(model: WarehouseModel) -> dict:
    cells = model.cells
    rows = [row for sheet in model.sheets for row in sheet.rows]
    zones = {cell.fill_color or "без зоны" for cell in cells}
    tiers = {str(cell.tier_number) for cell in cells}
    disabled = [cell for cell in cells if "block" in (cell.source or "").lower() or "заблок" in " ".join(cell.warnings).lower()]
    return {
        "excel_colored_cells": sum(1 for cell in cells if cell.fill_color),
        "cells": len(cells),
        "active_cells": len(cells) - len(disabled),
        "disabled_cells": len(disabled),
        "rows": len({row.row_number for row in rows}),
        "zones": len(zones),
        "tiers": len(tiers),
    }


def ensure_loaded_model() -> None:
    if "virtual_warehouse_model" in st.session_state:
        return
    model, render_cache, meta, error = load_last_import()
    if error:
        st.session_state["last_import_error"] = error
    if model is not None:
        meta = dict(meta)
        meta["loaded_from_cache"] = True
        st.session_state["virtual_warehouse_model"] = model
        st.session_state["virtual_warehouse_render_cache"] = render_cache
        st.session_state["virtual_warehouse_meta"] = meta
        st.session_state["virtual_warehouse_source"] = "сохранённая модель"


def filter_sheet(sheet: WarehouseSheet, zone: str, row_number: str, tier: str, availability: str) -> WarehouseSheet:
    filtered = WarehouseSheet(sheet.name, sheet.max_row, sheet.max_column, sheet.values, sheet.merged_ranges, warnings=sheet.warnings)
    for row in sheet.rows:
        if row_number != "Все" and str(row.row_number) != row_number:
            continue
        new_row = WarehouseRow(row.sheet_name, row.row_number, row.min_row, row.min_col, row.max_row, row.max_col, row.direction, row.confidence, warnings=row.warnings)
        for cell in row.potential_cells:
            is_disabled = "block" in (cell.source or "").lower() or "заблок" in " ".join(cell.warnings).lower()
            if zone != "Все" and (cell.fill_color or "без зоны") != zone:
                continue
            if tier != "Все" and str(cell.tier_number) != tier:
                continue
            if availability == "Только активные" and is_disabled:
                continue
            if availability == "Только заблокированные" and not is_disabled:
                continue
            new_row.potential_cells.append(cell)
        if new_row.potential_cells:
            filtered.rows.append(new_row)
    return filtered


def render_diagnostics(model: WarehouseModel, meta: dict, diagnostics: list[dict]) -> None:
    stats = model_stats(model)
    st.subheader("Диагностика склада")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Источник", meta.get("source", st.session_state.get("virtual_warehouse_source", "новый Excel")))
    c2.metric("Excel-ячеек с цветом", stats["excel_colored_cells"])
    c3.metric("Виртуальных ячеек", stats["cells"])
    c4.metric("Заблокированных", stats["disabled_cells"])
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Активных", stats["active_cells"])
    c6.metric("Рядов", stats["rows"])
    c7.metric("Зон", stats["zones"])
    c8.metric("Ярусов", stats["tiers"])
    st.caption(f"Построено: {meta.get('imported_at', '—')} · Схема: {meta.get('scheme_file_name', '—')} · Файл ячеек: {meta.get('cells_file_name', '—')} · Модель: {MODEL_PATH}")
    warnings = [d for d in diagnostics if d.get("level") in {"warning", "error"}]
    if warnings:
        st.dataframe(pd.DataFrame(warnings), use_container_width=True)


def render_performance(meta: dict) -> None:
    perf = meta.get("performance", {})
    st.subheader("Производительность")
    p1, p2, p3, p4, p5 = st.columns(5)
    p1.metric("Чтение Excel", f"{perf.get('read_excel_seconds', 0):.2f} сек")
    p2.metric("Построение модели", f"{perf.get('build_model_seconds', 0):.2f} сек")
    p3.metric("Подготовка визуализации", f"{perf.get('prepare_render_seconds', 0):.2f} сек")
    p4.metric("Сохранение модели", f"{perf.get('save_model_seconds', 0):.2f} сек")
    p5.metric("Рендеринг", f"{perf.get('render_seconds', 0):.2f} сек")
    st.caption(f"Модель загружена из кэша: {'да' if meta.get('loaded_from_cache') else 'нет'}")



def _is_numeric_text(value: str) -> bool:
    try:
        float(str(value).strip())
        return str(value).strip() != ""
    except ValueError:
        return False


def _cell_label(cell: dict) -> str:
    code = cell.get("code") or "без кода"
    return f"ряд {cell.get('row_number')} · ячейка {cell.get('cell_number')} · ярус {cell.get('tier')} · {code}"


def _short_cell_value(cell: dict | None) -> str:
    if not cell:
        return "—"
    return f"ряд {cell.get('row_number')}, ячейка {cell.get('cell_number')}, ярус {cell.get('tier')}, код {cell.get('code') or '—'}"


def _source_label(value: str | None) -> str:
    return {
        "excel": "Excel",
        "manual_add": "добавлена вручную",
        "manual_update": "изменена вручную",
    }.get(str(value or "excel"), str(value or "Excel"))


def _validate_manual_cell(model: dict, new_cell: dict, original_key: str | None = None) -> list[str]:
    errors: list[str] = []
    if not str(new_cell.get("row_number", "")).strip():
        errors.append("Ряд не может быть пустым.")
    if not str(new_cell.get("cell_number", "")).strip():
        errors.append("Номер ячейки не может быть пустым.")
    if not str(new_cell.get("tier", "")).strip():
        errors.append("Ярус не может быть пустым.")
    if new_cell.get("row_number") and not _is_numeric_text(str(new_cell.get("row_number"))):
        errors.append("Ряд должен быть числом.")
    if new_cell.get("cell_number") and not _is_numeric_text(str(new_cell.get("cell_number"))):
        errors.append("Номер ячейки должен быть числом.")
    if new_cell.get("tier") and not _is_numeric_text(str(new_cell.get("tier"))):
        errors.append("Ярус должен быть числом.")
    new_key = cell_key(new_cell)
    for cell in model.get("cells", []):
        if cell_key(cell) == new_key and new_key != original_key:
            errors.append("Ячейка с такой комбинацией ряд + номер ячейки + ярус уже существует.")
            break
    return errors


def _save_model_after_manual_change(model: dict, overrides: dict) -> dict:
    updated = apply_manual_overrides(model, overrides)
    updated["manual_change_counts"] = manual_change_counts(overrides)
    save_geometry_model(updated)
    st.session_state["geometry_model"] = updated
    return updated


def _manual_changes_dataframe(overrides: dict | None) -> pd.DataFrame:
    rows = []
    labels = {"add": "Добавление", "update": "Изменение", "delete": "Удаление"}
    for change in (overrides or {}).get("changes", []):
        rows.append({
            "Дата/время": change.get("created_at", ""),
            "Тип изменения": labels.get(change.get("change_type"), change.get("change_type", "")),
            "Старое значение": _short_cell_value(change.get("old_value")),
            "Новое значение": _short_cell_value(change.get("new_value")),
            "Комментарий": change.get("comment", ""),
        })
    return pd.DataFrame(rows, columns=["Дата/время", "Тип изменения", "Старое значение", "Новое значение", "Комментарий"])


def render_manual_cell_editor(model: dict) -> None:
    st.subheader("Ручное редактирование ячеек")
    overrides = load_manual_overrides()
    if overrides and overrides.get("source_model_id") != model.get("model_id"):
        overrides = None
    counts = manual_change_counts(overrides)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Ручных изменений", counts["total"])
    c2.metric("Добавлено вручную", counts["add"])
    c3.metric("Изменено вручную", counts["update"])
    c4.metric("Удалено вручную", counts["delete"])
    if counts["total"] == 0:
        st.caption("Ручных изменений пока нет.")

    add_tab, edit_tab, delete_tab, log_tab = st.tabs(["Добавить ячейку", "Изменить ячейку", "Удалить ячейку", "Журнал изменений"])
    existing_rows = sorted({str(cell.get("row_number")) for cell in model.get("cells", [])}, key=lambda value: (not value.isdigit(), value))
    existing_codes = {str(cell.get("code")) for cell in model.get("cells", []) if str(cell.get("code", "")).strip()}

    with add_tab:
        st.caption("Добавление меняет только сохранённую модель проекта и не редактирует исходный Excel-файл.")
        a1, a2, a3, a4 = st.columns(4)
        code = a1.text_input("Код ячейки", key="manual_add_code")
        row_number = a2.text_input("Ряд", key="manual_add_row")
        cell_number = a3.text_input("Номер ячейки", key="manual_add_cell")
        tier = a4.text_input("Ярус", value="1", key="manual_add_tier")
        comment = st.text_input("Комментарий, необязательно", key="manual_add_comment")
        row_is_new = bool(row_number.strip() and row_number.strip() not in existing_rows)
        allow_new_row = True
        if row_is_new:
            st.warning("Такого ряда ещё нет в модели. Новый ряд будет создан только после подтверждения.")
            allow_new_row = st.checkbox("Создать новый ряд", key="manual_add_allow_new_row")
        if st.button("Добавить ячейку", key="manual_add_button", type="primary"):
            new_cell = {"code": code.strip(), "row_number": row_number.strip(), "cell_number": cell_number.strip(), "tier": tier.strip(), "source": "manual_add"}
            errors = _validate_manual_cell(model, new_cell)
            if row_is_new and not allow_new_row:
                errors.append("Подтвердите создание нового ряда.")
            if code.strip() and code.strip() in existing_codes:
                st.warning("Код ячейки уже встречается в модели. Проверьте, что это ожидаемо.")
            if errors:
                for error in errors:
                    st.error(error)
            else:
                overrides = append_manual_change(model, "add", None, new_cell, comment.strip())
                _save_model_after_manual_change(model, overrides)
                st.success("Ячейка добавлена, координаты пересчитаны, модель сохранена.")
                st.rerun()

    cell_options = {_cell_label(cell): cell for cell in model.get("cells", [])}
    labels = list(cell_options)

    with edit_tab:
        if not labels:
            st.info("В модели пока нет ячеек для изменения.")
        else:
            f1, f2, f3, f4 = st.columns(4)
            row_filter = f1.selectbox("Фильтр: ряд", ["Все"] + existing_rows, key="manual_edit_row_filter")
            cell_filter = f2.text_input("Фильтр: номер ячейки", key="manual_edit_cell_filter")
            code_filter = f3.text_input("Фильтр: код", key="manual_edit_code_filter")
            tier_filter = f4.text_input("Фильтр: ярус", key="manual_edit_tier_filter")
            filtered = []
            for label, cell in cell_options.items():
                if row_filter != "Все" and str(cell.get("row_number")) != row_filter:
                    continue
                if cell_filter and str(cell.get("cell_number")) != cell_filter.strip():
                    continue
                if code_filter and code_filter.strip().lower() not in str(cell.get("code", "")).lower():
                    continue
                if tier_filter and str(cell.get("tier")) != tier_filter.strip():
                    continue
                filtered.append(label)
            if not filtered:
                st.warning("По фильтрам ячейки не найдены.")
            else:
                selected_label = st.selectbox("Выберите ячейку", filtered, key="manual_edit_selected")
                selected = cell_options[selected_label]
                e1, e2, e3, e4 = st.columns(4)
                new_code = e1.text_input("Код ячейки", value=str(selected.get("code", "")), key="manual_edit_code")
                new_row = e2.text_input("Ряд", value=str(selected.get("row_number", "")), key="manual_edit_row")
                new_cell_number = e3.text_input("Номер ячейки", value=str(selected.get("cell_number", "")), key="manual_edit_cell")
                new_tier = e4.text_input("Ярус", value=str(selected.get("tier", "")), key="manual_edit_tier")
                edit_comment = st.text_input("Комментарий, необязательно", key="manual_edit_comment")
                b1, b2 = st.columns(2)
                if b1.button("Сохранить изменения", key="manual_edit_save", type="primary"):
                    new_value = {"code": new_code.strip(), "row_number": new_row.strip(), "cell_number": new_cell_number.strip(), "tier": new_tier.strip(), "source": "manual_update"}
                    original_key = cell_key(selected)
                    errors = _validate_manual_cell(model, new_value, original_key=original_key)
                    if errors:
                        for error in errors:
                            st.error(error)
                    else:
                        overrides = append_manual_change(model, "update", selected, new_value, edit_comment.strip())
                        _save_model_after_manual_change(model, overrides)
                        st.success("Изменения сохранены, координаты пересчитаны.")
                        st.rerun()
                if b2.button("Отменить", key="manual_edit_cancel"):
                    st.info("Изменение отменено: модель не менялась.")

    with delete_tab:
        if not labels:
            st.info("В модели пока нет ячеек для удаления.")
        else:
            selected_label = st.selectbox("Выберите ячейку для удаления", labels, key="manual_delete_selected")
            selected = cell_options[selected_label]
            st.write({
                "Код": selected.get("code", ""),
                "Ряд": selected.get("row_number", ""),
                "Ячейка": selected.get("cell_number", ""),
                "Ярус": selected.get("tier", ""),
                "Координаты": f"x={selected.get('x_center', 0):.2f}, y={selected.get('y_center', 0):.2f}",
                "Источник": _source_label(selected.get("source", "excel")),
            })
            confirm = st.checkbox("Вы точно хотите удалить ячейку?", key="manual_delete_confirm")
            delete_comment = st.text_input("Комментарий, необязательно", key="manual_delete_comment")
            if st.button("Удалить ячейку", key="manual_delete_button", type="primary"):
                if not confirm:
                    st.error("Подтвердите удаление ячейки.")
                else:
                    overrides = append_manual_change(model, "delete", selected, None, delete_comment.strip())
                    _save_model_after_manual_change(model, overrides)
                    st.success("Ячейка удалена из модели, координаты пересчитаны.")
                    st.rerun()

    with log_tab:
        st.subheader("Журнал ручных изменений")
        log_df = _manual_changes_dataframe(overrides)
        if log_df.empty:
            st.info("Ручных изменений пока нет.")
        else:
            st.dataframe(log_df, use_container_width=True)
            st.download_button("Скачать журнал изменений", log_df.to_csv(index=False).encode("utf-8-sig"), file_name="manual_overrides_log.csv", mime="text/csv")
        st.download_button("Скачать текущую модель Excel", export_current_model_excel_bytes(model), file_name="current_warehouse_model.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        clear_confirm = st.checkbox("Подтверждаю очистку ручных изменений", key="manual_clear_confirm")
        if st.button("Очистить ручные изменения", key="manual_clear_button"):
            if not clear_confirm:
                st.error("Подтвердите очистку ручных изменений.")
            else:
                clear_manual_overrides()
                base_model = rebuild_geometry_from_cells(model, model.get("base_cells", model.get("cells", [])), keep_base_cells=False)
                base_model["manual_change_counts"] = manual_change_counts(None)
                save_geometry_model(base_model)
                st.session_state["geometry_model"] = base_model
                st.success("Ручные изменения очищены. Модель восстановлена к состоянию после последней загрузки Excel.")
                st.rerun()


def render_inventory_placement(model: dict) -> dict:
    st.subheader("Размещение товара")
    state, state_warning = load_placement_state(model)
    if state_warning:
        st.warning(state_warning)
    settings = state.setdefault("settings", {"allow_mixed_sku_in_deep_lane": False})
    allow_mixed = st.checkbox(
        "Разрешить несколько SKU в одной набивной ячейке",
        value=bool(settings.get("allow_mixed_sku_in_deep_lane", False)),
        key="placement_allow_mixed",
    )
    settings["allow_mixed_sku_in_deep_lane"] = allow_mixed

    upload_tab, unplaced_tab, manual_tab, edit_tab, diag_tab = st.tabs([
        "Загрузка инвента / остатков",
        "Товар без привязки к ячейкам",
        "Разместить вручную",
        "Редактировать размещение",
        "Диагностика размещения",
    ])

    with upload_tab:
        inventory_file = st.file_uploader("Загрузить Excel с остатками", type=["xlsx"], key="inventory_upload")
        replace_current = st.checkbox("Заменить текущее размещение", value=True, key="inventory_replace_current")
        if inventory_file is not None:
            inventory_bytes = inventory_file.getvalue()
            inventory_hash = file_hash(inventory_bytes)
            sheet_names = get_inventory_sheet_names(inventory_bytes)
            sheet_name = st.selectbox("Лист инвента", sheet_names, key="inventory_sheet")
            header_rows = st.radio("Строк заголовка инвента", [1, 2], index=0, horizontal=True, key="inventory_header_rows")
            inv_df = read_inventory_table_cached(inventory_bytes, inventory_hash, sheet_name, header_rows)
            st.caption(f"Прочитано строк инвента: {len(inv_df)}; колонок: {len(inv_df.columns)}")
            with st.expander("Предпросмотр инвента", expanded=False):
                st.dataframe(inv_df.head(30), use_container_width=True)
            detected = detect_inventory_columns(inv_df)
            columns = [None] + list(inv_df.columns)
            st.caption("Проверьте автоопределение колонок или выберите вручную.")
            c1, c2, c3, c4 = st.columns(4)
            c5, c6, c7, c8 = st.columns(4)
            mapping = {
                "sku_code": c1.selectbox("Код товара / SKU", columns, index=columns.index(detected["sku_code"]) if detected["sku_code"] in columns else 0, key="inv_map_sku"),
                "sku_name": c2.selectbox("Наименование товара", columns, index=columns.index(detected["sku_name"]) if detected["sku_name"] in columns else 0, key="inv_map_name"),
                "qty_pallets": c3.selectbox("Количество паллет", columns, index=columns.index(detected["qty_pallets"]) if detected["qty_pallets"] in columns else 0, key="inv_map_pallets"),
                "qty_boxes": c4.selectbox("Количество коробов", columns, index=columns.index(detected["qty_boxes"]) if detected["qty_boxes"] in columns else 0, key="inv_map_boxes"),
                "cell_address": c5.selectbox("Адрес ячейки", columns, index=columns.index(detected["cell_address"]) if detected["cell_address"] in columns else 0, key="inv_map_address"),
                "row_number": c6.selectbox("Ряд", columns, index=columns.index(detected["row_number"]) if detected["row_number"] in columns else 0, key="inv_map_row"),
                "cell_number": c7.selectbox("Ячейка", columns, index=columns.index(detected["cell_number"]) if detected["cell_number"] in columns else 0, key="inv_map_cell"),
                "tier": c8.selectbox("Ярус", columns, index=columns.index(detected["tier"]) if detected["tier"] in columns else 0, key="inv_map_tier"),
            }
            c9 = st.columns(1)[0]
            mapping["weight_class"] = c9.selectbox("Весовая категория", columns, index=columns.index(detected["weight_class"]) if detected.get("weight_class") in columns else 0, key="inv_map_weight_class")
            for key in ["expiry_date", "batch", "characteristic", "characteristic_code", "characteristic_name", "weight", "volume"]:
                mapping[key] = detected.get(key)
            normalized_inventory, inv_diagnostics = normalize_inventory_table_cached(inv_df.to_json(orient="split", force_ascii=False), json.dumps(mapping, ensure_ascii=False))
            if inv_diagnostics:
                st.dataframe(pd.DataFrame(inv_diagnostics), use_container_width=True)
            has_cell_columns = bool(mapping.get("cell_address") or (mapping.get("row_number") and mapping.get("cell_number")))
            if not has_cell_columns:
                st.warning("В инвенте нет адресов ячеек. Система не может восстановить фактическое расположение товара. Автоматическое размещение будет модельным и используется только для расчётов.")
            if st.button("Импортировать инвент", type="primary", key="inventory_import_button"):
                if any(item.get("level") == "error" for item in inv_diagnostics):
                    st.error("Исправьте обязательные колонки перед импортом.")
                elif not replace_current and (state.get("placements") or state.get("unplaced_inventory")):
                    st.error("Подтвердите замену текущего размещения или очистите его вручную.")
                else:
                    state, placement_import_diag = import_inventory(model, normalized_inventory, allow_replace=True)
                    st.session_state["placement_state"] = state
                    st.success("Инвент импортирован. Размещение сохранено в data/last_import/placements.json.")
                    st.dataframe(pd.DataFrame(placement_import_diag), use_container_width=True)
                    st.rerun()

    with unplaced_tab:
        unplaced = state.get("unplaced_inventory", [])
        total_unplaced_pallets = sum(float(item.get("qty_pallets", 0) or 0) for item in unplaced)
        total_unplaced_boxes = sum(float(item.get("qty_boxes", 0) or 0) for item in unplaced)
        u1, u2, u3 = st.columns(3)
        u1.metric("SKU без ячейки", len(unplaced))
        u2.metric("Паллет без ячейки", f"{total_unplaced_pallets:g}")
        u3.metric("Коробов без ячейки", f"{total_unplaced_boxes:g}")
        if unplaced:
            st.warning("Это модельное размещение, а не фактическое, потому что в инвенте нет адресов ячеек.")
            st.dataframe(pd.DataFrame(unplaced), use_container_width=True)
        else:
            st.info("Товаров без привязки к ячейкам сейчас нет.")
        st.info("Размещение выполняется последовательно по маршруту внутри весовых зон. Алгоритм пока не учитывает ABC, прогноз, соседство и другие правила оптимизации.")
        if st.button("Рассчитать базовое размещение", key="basic_weight_place_inventory"):
            receipts_state, receipts_warning = load_receipts_state(model)
            if receipts_warning:
                st.warning(receipts_warning)
            state, basic_diag = calculate_basic_weight_placement(model, state, receipts_state)
            st.session_state["placement_state"] = state
            st.success("Базовое размещение по весовым зонам выполнено.")
            st.dataframe(pd.DataFrame([{"Показатель": key, "Значение": value} for key, value in basic_diag.items() if key != "Неразмещённые позиции"]), use_container_width=True)
            if basic_diag.get("Неразмещённые позиции"):
                st.dataframe(pd.DataFrame(basic_diag["Неразмещённые позиции"]), use_container_width=True)
            st.rerun()
        if st.button("Разложить автоматически по складу", disabled=not unplaced, key="auto_place_inventory"):
            state, auto_diag = auto_place_unplaced(model, state, allow_mixed_sku_in_deep_lane=allow_mixed)
            st.session_state["placement_state"] = state
            st.success("Автоматическое модельное размещение выполнено.")
            st.dataframe(pd.DataFrame(auto_diag), use_container_width=True)
            st.rerun()

    with manual_tab:
        unplaced = state.get("unplaced_inventory", [])
        if not unplaced:
            st.info("Нет товара без ячейки для ручного размещения.")
        else:
            options = {f"{idx}: {item.get('sku_code')} · {item.get('sku_name')} · {item.get('qty_pallets')} паллет": idx for idx, item in enumerate(unplaced)}
            selected_label = st.selectbox("Товар из списка Без ячейки", list(options), key="manual_place_item")
            m1, m2, m3, m4 = st.columns(4)
            target_row = m1.text_input("Ряд", key="manual_place_row")
            target_cell = m2.text_input("Ячейка", key="manual_place_cell")
            target_tier = m3.text_input("Ярус", value="1", key="manual_place_tier")
            qty = m4.number_input("Паллет к размещению", min_value=0.0, value=1.0, step=1.0, key="manual_place_qty")
            if st.button("Разместить", key="manual_place_button", type="primary"):
                state, error = manual_place(model, state, options[selected_label], target_row, target_cell, target_tier, qty, allow_mixed_sku_in_deep_lane=allow_mixed)
                if error:
                    st.error(error)
                else:
                    st.session_state["placement_state"] = state
                    st.success("Товар размещён вручную, placements.json обновлён.")
                    st.rerun()

    with edit_tab:
        placements = state.get("placements", [])
        if not placements:
            st.info("Размещений пока нет.")
        else:
            placement_options = {f"{p.get('sku_code')} · {p.get('cell_key')} · {p.get('qty_pallets')} паллет · {p.get('source')}": p for p in placements}
            selected = st.selectbox("Выберите размещение", list(placement_options), key="placement_edit_selected")
            selected_placement = placement_options[selected]
            e1, e2, e3, e4 = st.columns(4)
            new_qty = e1.number_input("Новое количество паллет", min_value=0.0, value=float(selected_placement.get("qty_pallets", 0) or 0), step=1.0, key="placement_edit_qty")
            move_row = e2.text_input("Новый ряд", value=str(selected_placement.get("row_number", "")), key="placement_move_row")
            move_cell = e3.text_input("Новая ячейка", value=str(selected_placement.get("cell_number", "")), key="placement_move_cell")
            move_tier = e4.text_input("Новый ярус", value=str(selected_placement.get("tier", "1")), key="placement_move_tier")
            a1, a2, a3 = st.columns(3)
            if a1.button("Изменить количество", key="placement_update_qty_button"):
                state, error = update_placement_qty(model, state, selected_placement["placement_id"], new_qty)
                if error:
                    st.error(error)
                else:
                    st.session_state["placement_state"] = state
                    st.success("Количество размещения изменено.")
                    st.rerun()
            if a2.button("Перенести в другую ячейку", key="placement_move_button"):
                state, error = move_placement(model, state, selected_placement["placement_id"], move_row, move_cell, move_tier, allow_mixed_sku_in_deep_lane=allow_mixed)
                if error:
                    st.error(error)
                else:
                    st.session_state["placement_state"] = state
                    st.success("Размещение перенесено.")
                    st.rerun()
            if a3.button("Удалить размещение", key="placement_delete_button"):
                state, error = delete_placement(state, selected_placement["placement_id"])
                if error:
                    st.error(error)
                else:
                    st.session_state["placement_state"] = state
                    st.success("Размещение удалено, товар возвращён в Без ячейки.")
                    st.rerun()

    with diag_tab:
        diag = placement_diagnostics(model, state)
        st.subheader("Диагностика размещения")
        st.dataframe(pd.DataFrame([{"Показатель": key, "Значение": value} for key, value in diag.items()]), use_container_width=True)
        st.download_button("Скачать размещение в Excel", export_placements_excel_bytes(model, state), file_name="placements.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        log_df = pd.DataFrame(state.get("journal", []))
        st.subheader("Журнал размещения")
        if log_df.empty:
            st.info("Журнал размещения пока пуст.")
        else:
            st.dataframe(log_df, use_container_width=True)
            st.download_button("Скачать журнал размещения", log_df.to_csv(index=False).encode("utf-8-sig"), file_name="placement_journal.csv", mime="text/csv")
        if st.button("Очистить журнал", key="placement_clear_journal"):
            state["journal"] = []
            save_placement_state(state)
            st.rerun()
        if st.button("Сбросить рассчитанное размещение", key="placement_clear_all"):
            state = clear_calculated_placements(state)
            st.session_state["placement_state"] = state
            st.success("Рассчитанное размещение очищено. Фактические остатки, приходы и настройки зон сохранены.")
            st.rerun()

    return attach_placements_to_model(model, state)


RECEIPT_STATUS_LABELS = {
    "not_placed": "Не размещено",
    "partially_placed": "Частично размещено",
    "placed": "Размещено",
    "error": "Ошибка",
}

RECEIPT_TABLE_COLUMNS = {
    "receipt_date": "Дата прихода",
    "receipt_document": "Документ прихода",
    "sku_code": "Код товара",
    "sku_name": "Наименование",
    "characteristic_name": "Характеристика",
    "qty_pallets": "Количество паллет",
    "qty_boxes": "Количество коробов",
    "expiry_date": "Срок годности",
    "placement_status": "Статус размещения",
}


def _receipt_dataframe(receipts: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(receipts)
    if df.empty:
        return df
    columns = [column for column in RECEIPT_TABLE_COLUMNS if column in df.columns]
    result = df[columns].copy()
    if "placement_status" in result.columns:
        result["placement_status"] = result["placement_status"].map(RECEIPT_STATUS_LABELS).fillna(result["placement_status"])
    return result.rename(columns=RECEIPT_TABLE_COLUMNS)


def render_receipts_section(model: dict) -> None:
    st.subheader("Приходы")
    state, state_warning = load_receipts_state(model)
    if state_warning:
        st.warning(state_warning)
    receipts = state.get("receipts", [])
    diagnostics = state.get("diagnostics", {})
    if receipts:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Файл", state.get("source_file_name", "—"))
        c2.metric("Дата загрузки", state.get("created_at", "—"))
        c3.metric("Строк", len(receipts))
        c4.metric("SKU", diagnostics.get("Всего SKU", 0))
        c5.metric("Паллет", f"{diagnostics.get('Всего паллет', 0):g}" if isinstance(diagnostics.get("Всего паллет", 0), (int, float)) else diagnostics.get("Всего паллет", 0))
        st.info("Приходы загружены. Алгоритм размещения по ячейкам пока не запущен. Все строки имеют статус ‘Не размещено’.")
    upload_tab, data_tab, diag_tab = st.tabs(["Загрузка приходов", "Приходы к размещению", "Диагностика приходов"])

    with upload_tab:
        receipt_file = st.file_uploader("Загрузить Excel с приходами", type=["xlsx"], key="receipt_upload")
        replace_current = st.checkbox("Заменить текущие загруженные приходы", value=True, key="receipt_replace_current")
        if receipt_file is not None:
            receipt_bytes = receipt_file.getvalue()
            receipt_hash = file_hash(receipt_bytes)
            sheet_names = get_receipt_sheet_names(receipt_bytes)
            sheet_name = st.selectbox("Выбрать лист", sheet_names, key="receipt_sheet")
            header_rows = st.radio("Строк заголовка приходов", [1, 2], index=0, horizontal=True, key="receipt_header_rows")
            receipt_df = read_receipt_table_cached(receipt_bytes, receipt_hash, sheet_name, header_rows)
            with st.expander("Предпросмотр", expanded=False):
                st.dataframe(receipt_df.head(30), use_container_width=True)
            detected = detect_receipt_columns(receipt_df)
            columns = [None] + list(receipt_df.columns)
            st.caption("Ручной выбор колонок: проверьте автоопределение или выберите колонки вручную.")
            c1, c2, c3, c4, c5 = st.columns(5)
            c6, c7, c8, c9, c10 = st.columns(5)
            c11, c12, c13, c14, c15 = st.columns(5)
            c16 = st.columns(1)[0]
            mapping = {
                "sku_code": c1.selectbox("Код товара", columns, index=columns.index(detected["sku_code"]) if detected["sku_code"] in columns else 0, key="receipt_map_sku"),
                "sku_name": c2.selectbox("Наименование", columns, index=columns.index(detected["sku_name"]) if detected["sku_name"] in columns else 0, key="receipt_map_name"),
                "qty_pallets": c3.selectbox("Количество паллет", columns, index=columns.index(detected["qty_pallets"]) if detected["qty_pallets"] in columns else 0, key="receipt_map_pallets"),
                "qty_boxes": c4.selectbox("Количество коробов", columns, index=columns.index(detected["qty_boxes"]) if detected["qty_boxes"] in columns else 0, key="receipt_map_boxes"),
                "qty_units": c5.selectbox("Базовое количество", columns, index=columns.index(detected["qty_units"]) if detected["qty_units"] in columns else 0, key="receipt_map_units"),
                "receipt_date": c6.selectbox("Дата прихода", columns, index=columns.index(detected["receipt_date"]) if detected["receipt_date"] in columns else 0, key="receipt_map_date"),
                "receipt_number": c7.selectbox("Номер документа", columns, index=columns.index(detected["receipt_number"]) if detected["receipt_number"] in columns else 0, key="receipt_map_number"),
                "receipt_document": c8.selectbox("Документ прихода", columns, index=columns.index(detected["receipt_document"]) if detected["receipt_document"] in columns else 0, key="receipt_map_document"),
                "warehouse": c9.selectbox("Склад", columns, index=columns.index(detected["warehouse"]) if detected["warehouse"] in columns else 0, key="receipt_map_warehouse"),
                "warehouse_zone": c10.selectbox("Зона склада", columns, index=columns.index(detected["warehouse_zone"]) if detected["warehouse_zone"] in columns else 0, key="receipt_map_zone"),
                "characteristic_code": c11.selectbox("Код характеристики", columns, index=columns.index(detected["characteristic_code"]) if detected["characteristic_code"] in columns else 0, key="receipt_map_char_code"),
                "characteristic_name": c12.selectbox("Характеристика", columns, index=columns.index(detected["characteristic_name"]) if detected["characteristic_name"] in columns else 0, key="receipt_map_char_name"),
                "batch": c13.selectbox("Партия", columns, index=columns.index(detected["batch"]) if detected["batch"] in columns else 0, key="receipt_map_batch"),
                "expiry_date": c14.selectbox("Срок годности", columns, index=columns.index(detected["expiry_date"]) if detected["expiry_date"] in columns else 0, key="receipt_map_expiry"),
                "comment": c15.selectbox("Комментарий", columns, index=columns.index(detected["comment"]) if detected["comment"] in columns else 0, key="receipt_map_comment"),
                "weight_class": c16.selectbox("Весовая категория", columns, index=columns.index(detected["weight_class"]) if detected.get("weight_class") in columns else 0, key="receipt_map_weight_class"),
            }
            normalized_receipts, receipt_diagnostics, receipt_messages = normalize_receipt_table_cached(receipt_df.to_json(orient="split", force_ascii=False), json.dumps(mapping, ensure_ascii=False))
            if receipt_messages:
                st.dataframe(pd.DataFrame(receipt_messages), use_container_width=True)
            if st.button("Загрузить приходы", type="primary", key="receipt_import_button"):
                if any(item.get("level") == "error" for item in receipt_messages):
                    st.error("Исправьте обязательные колонки или ошибки данных перед загрузкой приходов.")
                elif not replace_current and receipts:
                    st.error("Подтвердите замену текущих загруженных приходов или очистите их вручную.")
                else:
                    new_state = make_receipts_state(model, receipt_file.name, receipt_hash, normalized_receipts, receipt_diagnostics, mapping)
                    save_receipts_state(new_state)
                    st.success("Приходы загружены и сохранены. Все строки имеют статус ‘Не размещено’.")
                    st.rerun()

    with data_tab:
        if not receipts:
            st.info("Загруженных приходов пока нет.")
        else:
            st.dataframe(_receipt_dataframe(receipts), use_container_width=True)
            b1, b2, b3 = st.columns(3)
            b1.download_button("Скачать загруженные приходы", export_receipts_excel_bytes(state), file_name="receipts.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            if b2.button("Очистить загруженные приходы", key="receipt_clear_button"):
                clear_receipts_state()
                st.success("Загруженные приходы очищены.")
                st.rerun()
            if b3.button("Рассчитать размещение приходов", key="receipt_calculate_stub"):
                st.info("Алгоритм размещения приходов ещё не реализован. Сейчас можно только загрузить приходы, проверить данные и сохранить их для следующего этапа.")

    with diag_tab:
        st.subheader("Диагностика приходов")
        if diagnostics:
            st.dataframe(pd.DataFrame([{"Показатель": key, "Значение": value} for key, value in diagnostics.items() if key != "messages"]), use_container_width=True)
            messages = diagnostics.get("messages", [])
            if messages:
                st.dataframe(pd.DataFrame(messages), use_container_width=True)
        else:
            st.info("Диагностика появится после загрузки файла приходов.")

def render_excel_geometry_warehouse() -> None:
    st.title("Симулятор скорости сборки")
    st.header("Склад из Excel: ряды + ячейки + проезды")
    st.caption("Строим не копию Excel-картинки, а рабочую геометрическую модель склада в метрах: вертикальные ряды, фактические ячейки, верхний/нижний проезд и заданные межрядные проезды.")

    saved_model = load_geometry_model()
    if saved_model and "geometry_model" not in st.session_state:
        st.session_state["geometry_model"] = saved_model
    with st.sidebar:
        st.subheader("Сохранённая геометрия")
        if saved_model:
            st.success(f"Есть сохранённая модель: {saved_model.get('source_file_name', '—')}")
            if st.button("Использовать сохранённую геометрию", key="geometry_use_saved"):
                st.session_state["geometry_model"] = saved_model
                st.rerun()
            if st.button("Очистить сохранённую геометрию", key="geometry_clear_saved"):
                for path in [MODEL_PATH, META_PATH]:
                    if path.exists():
                        path.unlink()
                clear_manual_overrides()
                clear_row_settings()
                clear_placement_state()
                st.session_state.pop("geometry_model", None)
                st.session_state.pop("placement_state", None)
                st.rerun()
        else:
            st.caption("Сохранённой геометрии пока нет.")

    constructor_tab, map_tab = st.tabs(["Конструктор склада", "Карта склада"])
    with constructor_tab:
        render_geometry_constructor_tab(saved_model)
    with map_tab:
        model = st.session_state.get("geometry_model")
        if model:
            render_geometry_map_view(model)
        else:
            st.info("Сначала загрузите Excel или используйте сохранённую геометрию на вкладке «Конструктор склада».")


def render_geometry_constructor_tab(saved_model: dict | None) -> None:
    uploaded = st.file_uploader("Excel со списком фактических ячеек", type=["xlsx"], key="geometry_cells_file")
    if uploaded is None:
        if saved_model:
            st.info("Загрузите новый Excel или используйте сохранённую модель из боковой панели.")
        else:
            st.info("Загрузите Excel со списком ячеек в формате: Код | Ряд | Ячейка | Ярус.")
        model = st.session_state.get("geometry_model")
        if model:
            render_geometry_constructor_view(model)
        return

    file_bytes = uploaded.getvalue()
    content_hash = file_hash(file_bytes)
    sheet_names = get_geometry_sheet_names(file_bytes)
    sheet_name = st.selectbox("Лист со списком ячеек", sheet_names, key="geometry_sheet")
    header_rows = st.radio("Строк заголовка", [1, 2], index=1, horizontal=True, help="Если в Excel сверху 'Ряд', а ниже 'Ссылка', выберите 2 строки заголовка.")

    timings: dict[str, float] = {}
    started = perf_counter()
    df = read_cell_table_cached(file_bytes, content_hash, sheet_name, header_rows)
    timings["read_excel_seconds"] = perf_counter() - started
    st.caption(f"Прочитано строк: {len(df)}; колонок: {len(df.columns)}")
    with st.expander("Предпросмотр первых строк", expanded=False):
        st.dataframe(df.head(30), use_container_width=True)

    detected = detect_column_mapping(df)
    st.subheader("Колонки")
    columns = [None] + list(df.columns)
    c1, c2, c3, c4 = st.columns(4)
    mapping = {
        "code": c1.selectbox("Код", columns, index=columns.index(detected["code"]) if detected["code"] in columns else 0),
        "row_number": c2.selectbox("Ряд", columns, index=columns.index(detected["row_number"]) if detected["row_number"] in columns else 0),
        "cell_number": c3.selectbox("Ячейка", columns, index=columns.index(detected["cell_number"]) if detected["cell_number"] in columns else 0),
        "tier": c4.selectbox("Ярус", columns, index=columns.index(detected["tier"]) if detected["tier"] in columns else 0),
    }

    norm_started = perf_counter()
    normalized_df, column_diagnostics = normalize_cell_table_cached(df.to_json(orient="split", force_ascii=False), json.dumps(mapping, ensure_ascii=False))
    timings["normalize_columns_seconds"] = perf_counter() - norm_started
    if any(item.get("level") == "error" for item in column_diagnostics):
        st.error("Не удалось определить обязательные колонки. Выберите 'Ряд' и 'Ячейка' вручную.")
        st.dataframe(pd.DataFrame(column_diagnostics), use_container_width=True)
        return

    st.subheader("Размеры и ярусы")
    s1, s2, s3, s4, s5 = st.columns(5)
    cell_length_m = s1.number_input("Длина ячейки вдоль ряда, м", min_value=0.1, value=1.2, step=0.1)
    cell_width_m = s2.number_input("Ширина ряда, м", min_value=0.1, value=0.8, step=0.1)
    aisle_width_m = s3.number_input("Межрядный проезд, м", min_value=0.1, value=3.4, step=0.1)
    top_road_width_m = s4.number_input("Верхний проезд, м", min_value=0.1, value=3.4, step=0.1)
    bottom_road_width_m = s5.number_input("Нижний проезд, м", min_value=0.1, value=3.4, step=0.1)
    tier_values = sorted(normalized_df["tier"].dropna().astype(str).unique().tolist(), key=lambda value: (not value.isdigit(), value)) or ["1"]
    tier_mode = st.radio("Ярусы", ["selected", "all", "min_per_cell"], format_func={"selected": "только выбранный", "all": "все", "min_per_cell": "минимальный для ряд+ячейка"}.get, horizontal=True)
    selected_tier = st.selectbox("Выбранный ярус", tier_values, disabled=tier_mode != "selected")

    row_config_default = default_row_config(normalized_df)
    if st.session_state.get("geometry_row_config_hash") != content_hash:
        st.session_state["geometry_row_config_data"] = row_config_default
        st.session_state["geometry_row_config_hash"] = content_hash

    st.subheader("Настройки рядов")
    st.caption("Обычный ряд хранит одну паллету на системную ячейку. Набивной ряд хранит несколько физических паллетомест внутри одной системной ячейки.")
    row_config_source = st.session_state.get("geometry_row_config_data", row_config_default)
    row_config_display = row_config_source.copy()
    row_config_display["row_storage_type"] = row_config_display["row_storage_type"].map({"normal": "Обычный ряд", "deep_lane": "Набивной ряд"}).fillna(row_config_display["row_storage_type"])
    row_config_display["cell_direction"] = row_config_display["cell_direction"].map({"bottom_to_top": "Снизу вверх", "top_to_bottom": "Сверху вниз"}).fillna(row_config_display["cell_direction"])
    if "weight_zone" not in row_config_display.columns:
        row_config_display["weight_zone"] = "unassigned"
    row_config_display["weight_zone"] = row_config_display["weight_zone"].map({"heavy": "Тяжёлое", "medium": "Среднее", "light": "Лёгкое", "unassigned": "Не назначено"}).fillna("Не назначено")
    row_config = st.data_editor(
        row_config_display,
        num_rows="dynamic",
        use_container_width=True,
        key="geometry_row_config",
        column_config={
            "row_number": "Ряд",
            "row_order": "Порядок ряда",
            "row_storage_type": st.column_config.SelectboxColumn("Тип ряда", options=["Обычный ряд", "Набивной ряд"]),
            "deep_lane_width": st.column_config.NumberColumn("Набивных паллетомест", min_value=1, max_value=7, step=1),
            "cell_direction": st.column_config.SelectboxColumn("Направление ячеек", options=["Снизу вверх", "Сверху вниз"]),
            "weight_zone": st.column_config.SelectboxColumn("Весовая зона", options=["Тяжёлое", "Среднее", "Лёгкое", "Не назначено"]),
            "row_group": "Группа рядов",
            "side": "Сторона/зона",
            "comment": "Комментарий",
        },
    )
    row_config["row_storage_type"] = row_config["row_storage_type"].map({"Обычный ряд": "normal", "Набивной ряд": "deep_lane"}).fillna(row_config["row_storage_type"])
    row_config["cell_direction"] = row_config["cell_direction"].map({"Снизу вверх": "bottom_to_top", "Сверху вниз": "top_to_bottom"}).fillna(row_config["cell_direction"])
    if "weight_zone" not in row_config.columns:
        row_config["weight_zone"] = "Не назначено"
    row_config["weight_zone"] = row_config["weight_zone"].map({"Тяжёлое": "heavy", "Среднее": "medium", "Лёгкое": "light", "Не назначено": "unassigned"}).fillna("unassigned")
    st.session_state["geometry_row_config_data"] = row_config

    st.subheader("Набивные ряды")
    available_rows = sorted(row_config["row_number"].dropna().astype(str).tolist(), key=lambda value: (not value.isdigit(), value))
    selected_deep_rows = st.multiselect("Выберите ряды", available_rows, key="deep_lane_selected_rows")
    d1, d2, d3 = st.columns(3)
    bulk_storage_type = d1.selectbox("Тип ряда", ["Набивной ряд", "Обычный ряд"], key="deep_lane_bulk_type")
    bulk_width = d2.selectbox("Набивных паллетомест", [2, 3, 4, 5, 6, 7], index=3, key="deep_lane_bulk_width")
    bulk_direction = d3.selectbox("Направление ячеек", ["Сверху вниз", "Снизу вверх"], key="deep_lane_bulk_direction")
    deep_comment = st.text_input("Комментарий для выбранных рядов", value="", key="deep_lane_bulk_comment")
    b1, b2, b3 = st.columns(3)
    if b1.button("Применить к выбранным рядам", disabled=not selected_deep_rows, key="deep_lane_apply"):
        updated = row_config.copy()
        mask = updated["row_number"].astype(str).isin(selected_deep_rows)
        is_deep = bulk_storage_type == "Набивной ряд"
        updated.loc[mask, "row_storage_type"] = "deep_lane" if is_deep else "normal"
        updated.loc[mask, "deep_lane_width"] = bulk_width if is_deep else 1
        updated.loc[mask, "cell_direction"] = "top_to_bottom" if bulk_direction == "Сверху вниз" else "bottom_to_top"
        if deep_comment:
            updated.loc[mask, "comment"] = deep_comment
        st.session_state["geometry_row_config_data"] = updated
        st.success("Настройки выбранных рядов обновлены. Нажмите «Построить склад», чтобы пересчитать геометрию.")
        st.rerun()
    if b2.button("Добавить набивной ряд", key="deep_lane_add"):
        new_row = pd.DataFrame([{
            "row_number": "154",
            "row_order": len(row_config) + 1,
            "row_storage_type": "deep_lane",
            "deep_lane_width": 5,
            "cell_direction": "top_to_bottom",
            "row_group": "",
            "side": "",
            "comment": "ФРОВ, набивные ячейки",
        }])
        st.session_state["geometry_row_config_data"] = pd.concat([row_config, new_row], ignore_index=True).drop_duplicates("row_number", keep="last")
        st.success("Добавлена строка настройки набивного ряда. Проверьте номер ряда и нажмите «Построить склад».")
        st.rerun()
    if b3.button("Сбросить настройки набивных рядов", key="deep_lane_reset"):
        st.session_state["geometry_row_config_data"] = row_config_default
        st.success("Настройки набивных рядов сброшены для текущей выгрузки.")
        st.rerun()
    if st.button("Сохранить настройки рядов", key="deep_lane_save_hint"):
        st.info("Настройки рядов сохранятся вместе с моделью после нажатия «Построить склад».")

    st.subheader("Проезды между рядами")
    st.caption("Если пары «ряд от → ряд до» нет в таблице, ряды стоят плотно. Если есть — между ними добавляется проезд.")
    aisle_config = st.data_editor(
        empty_aisle_config(),
        num_rows="dynamic",
        use_container_width=True,
        key="geometry_aisle_config",
        column_config={
            "row_from": "Ряд от",
            "row_to": "Ряд до",
            "aisle_width_m": "Ширина проезда, м",
            "aisle_type": "Тип проезда",
            "comment": "Комментарий",
        },
    )

    build_clicked = st.button("Построить склад", type="primary", key="geometry_build")
    if build_clicked:
        settings = GeometrySettings(
            cell_length_m=cell_length_m,
            cell_width_m=cell_width_m,
            aisle_width_m=aisle_width_m,
            top_road_width_m=top_road_width_m,
            bottom_road_width_m=bottom_road_width_m,
            selected_tier=str(selected_tier),
            tier_mode=tier_mode,
        )
        build_started = perf_counter()
        model, diagnostics = build_geometry_model(normalized_df, settings, row_config, aisle_config, uploaded.name, sheet_name, source_file_hash=content_hash)
        timings["build_geometry_seconds"] = perf_counter() - build_started
        model["performance"] = timings | model.get("performance", {})
        clear_manual_overrides()
        clear_row_settings()
        clear_placement_state()
        st.session_state.pop("placement_state", None)
        st.warning("Загружен новый Excel. Старые ручные изменения, настройки набивных рядов и размещение товара сброшены для новой модели.")
        save_started = perf_counter()
        save_geometry_model(model)
        timings["save_model_seconds"] = perf_counter() - save_started
        model["performance"] = timings | model.get("performance", {})
        save_geometry_model(model)
        st.session_state["geometry_model"] = model
        st.success(f"Геометрическая модель построена и сохранена: {len(model['rows'])} рядов, {len(model['cells'])} ячеек, {len(model['aisles'])} проездов.")

    model = st.session_state.get("geometry_model")
    if model:
        render_geometry_constructor_view(model)



RUSSIAN_COLUMN_LABELS = {
    "code": "Код",
    "row_number": "Ряд",
    "cell_number": "Ячейка",
    "tier": "Ярус",
    "source": "Источник",
    "source_line": "Строка источника",
    "storage_type": "Тип хранения",
    "row_storage_type": "Тип ряда",
    "deep_lane_width": "Набивных паллетомест",
    "capacity_pallets": "Вместимость, паллет",
    "volume_m3": "Объём, м³",
    "cell_direction": "Направление ячеек",
    "physical_slots": "Физические места",
    "row_order": "Порядок",
    "row_group": "Группа",
    "side": "Сторона/зона",
    "comment": "Комментарий",
    "cells_count": "Количество ячеек",
    "row_from": "Ряд от",
    "row_to": "Ряд до",
    "aisle_width_m": "Ширина проезда, м",
    "aisle_type": "Тип проезда",
    "road_type": "Тип дороги",
    "width_m": "Ширина, м",
    "length_m": "Длина, м",
    "node_id": "Узел",
    "node_type": "Тип узла",
    "from_node": "От узла",
    "to_node": "К узлу",
    "distance_m": "Расстояние, м",
    "edge_type": "Тип связи",
    "x_min": "X от",
    "x_max": "X до",
    "y_min": "Y от",
    "y_max": "Y до",
    "x_center": "X центр",
    "y_center": "Y центр",
}


def _localized_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if "source" in df.columns:
        df["source"] = df["source"].map(_source_label)
    return df.rename(columns=RUSSIAN_COLUMN_LABELS)

def _model_aisle_config_dataframe(model: dict) -> pd.DataFrame:
    rows = []
    for aisle in model.get("aisles", []):
        rows.append({
            "row_from": aisle.get("row_from", ""),
            "row_to": aisle.get("row_to", ""),
            "aisle_width_m": aisle.get("aisle_width_m", model.get("settings", {}).get("aisle_width_m", 3.4)),
            "aisle_type": aisle.get("aisle_type", "межрядный проезд"),
            "comment": aisle.get("comment", ""),
        })
    return pd.DataFrame(rows, columns=["row_from", "row_to", "aisle_width_m", "aisle_type", "comment"])



def render_weight_zone_editor(model: dict) -> dict:
    st.subheader("Весовые зоны рядов")
    zone_to_label = {"heavy": "Тяжёлое", "medium": "Среднее", "light": "Лёгкое", "unassigned": "Не назначено"}
    label_to_zone = {value: key for key, value in zone_to_label.items()}
    rows_df = pd.DataFrame([
        {"row_number": row.get("row_number"), "weight_zone": zone_to_label.get(row.get("weight_zone", "unassigned"), "Не назначено")}
        for row in model.get("rows", [])
    ])
    selected_rows = st.multiselect("Ряды для массового назначения", rows_df["row_number"].astype(str).tolist() if not rows_df.empty else [], key="weight_zone_bulk_rows")
    z1, z2 = st.columns([1, 2])
    bulk_zone = z1.selectbox("Назначить зону", ["Тяжёлое", "Среднее", "Лёгкое", "Не назначено"], key="weight_zone_bulk_value")
    if z2.button("Применить зону к выбранным рядам", disabled=not selected_rows, key="weight_zone_bulk_apply"):
        rows_df.loc[rows_df["row_number"].astype(str).isin(selected_rows), "weight_zone"] = bulk_zone
    edited = st.data_editor(
        rows_df,
        use_container_width=True,
        hide_index=True,
        key=f"weight_zone_editor_{model.get('model_id', 'model')}",
        column_config={
            "row_number": st.column_config.TextColumn("Номер ряда", disabled=True),
            "weight_zone": st.column_config.SelectboxColumn("Текущая зона", options=["Тяжёлое", "Среднее", "Лёгкое", "Не назначено"]),
        },
    )
    if st.button("Сохранить весовые зоны рядов", key="weight_zone_save", type="primary"):
        zone_by_row = {str(row["row_number"]): label_to_zone.get(row["weight_zone"], "unassigned") for _, row in edited.iterrows()}
        for row in model.get("rows", []):
            row["weight_zone"] = zone_by_row.get(str(row.get("row_number")), "unassigned")
        for cell in model.get("cells", []):
            cell["weight_zone"] = zone_by_row.get(str(cell.get("row_number")), "unassigned")
        for cell in model.get("base_cells", []):
            cell["weight_zone"] = zone_by_row.get(str(cell.get("row_number")), "unassigned")
        for row_setting in model.get("row_settings", []):
            row_setting["weight_zone"] = zone_by_row.get(str(row_setting.get("row_number")), "unassigned")
        save_geometry_model(model)
        st.session_state["geometry_model"] = model
        st.success("Весовые зоны рядов сохранены вместе с моделью склада.")
        st.rerun()
    return model

def render_active_model_aisle_editor(model: dict) -> dict:
    st.subheader("Настройки проездов между рядами")
    st.caption("Изменение проездов перестраивает только геометрию активной модели по текущим ячейкам и не очищает ручные изменения, размещение товара или приходы.")
    settings = model.get("settings", {})
    model_key = model.get("model_id", "model")
    c1, c2, c3 = st.columns(3)
    default_aisle_width = c1.number_input(
        "Межрядный проезд по умолчанию, м",
        min_value=0.1,
        value=float(settings.get("aisle_width_m", 3.4) or 3.4),
        step=0.1,
        key=f"active_aisle_default_width_{model_key}",
    )
    top_road_width = c2.number_input(
        "Верхний проезд, м",
        min_value=0.1,
        value=float(settings.get("top_road_width_m", 3.4) or 3.4),
        step=0.1,
        key=f"active_top_road_width_{model_key}",
    )
    bottom_road_width = c3.number_input(
        "Нижний проезд, м",
        min_value=0.1,
        value=float(settings.get("bottom_road_width_m", 3.4) or 3.4),
        step=0.1,
        key=f"active_bottom_road_width_{model_key}",
    )
    st.caption("Если пары «ряд от → ряд до» нет в таблице, ряды стоят плотно. Если есть — между ними добавляется проезд.")
    aisle_config = st.data_editor(
        _model_aisle_config_dataframe(model),
        num_rows="dynamic",
        use_container_width=True,
        key=f"active_aisle_config_{model_key}",
        column_config={
            "row_from": "Ряд от",
            "row_to": "Ряд до",
            "aisle_width_m": st.column_config.NumberColumn("Ширина проезда, м", min_value=0.1, step=0.1),
            "aisle_type": "Тип проезда",
            "comment": "Комментарий",
        },
    )
    b1, b2 = st.columns(2)
    if b1.button("Сохранить настройки проездов", key="active_aisle_save", type="primary"):
        geometry_settings = GeometrySettings(
            cell_length_m=float(settings.get("cell_length_m", 1.2) or 1.2),
            cell_width_m=float(settings.get("cell_width_m", 0.8) or 0.8),
            aisle_width_m=default_aisle_width,
            top_road_width_m=top_road_width,
            bottom_road_width_m=bottom_road_width,
            pallet_height_m=float(settings.get("pallet_height_m", 2.2) or 2.2),
            selected_tier=str(settings.get("selected_tier", "1") or "1"),
            tier_mode=str(settings.get("tier_mode", "selected") or "selected"),
            row_order_mode=str(settings.get("row_order_mode", "row_order_or_number") or "row_order_or_number"),
        )
        rebuilt = rebuild_geometry_from_cells(model, model.get("cells", []), keep_base_cells=True, settings=geometry_settings, aisle_config=aisle_config)
        rebuilt["manual_change_counts"] = model.get("manual_change_counts", rebuilt.get("manual_change_counts", {}))
        save_geometry_model(rebuilt)
        st.session_state["geometry_model"] = rebuilt
        st.success("Настройки проездов сохранены, геометрия активной модели перестроена.")
        st.rerun()
    if b2.button("Сбросить таблицу проездов", key="active_aisle_reset"):
        rebuilt = rebuild_geometry_from_cells(model, model.get("cells", []), keep_base_cells=True, aisle_config=empty_aisle_config())
        save_geometry_model(rebuilt)
        st.session_state["geometry_model"] = rebuilt
        st.success("Межрядные проезды удалены из активной модели.")
        st.rerun()
    return model


def _model_summary_metrics(model: dict) -> None:
    deep_lane_rows = [row for row in model.get("rows", []) if row.get("row_storage_type") == "deep_lane"]
    total_capacity = sum(float(cell.get("capacity_pallets", 1) or 1) for cell in model.get("cells", []))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Рядов", len(model.get("rows", [])))
    c2.metric("Логических ячеек", len(model.get("cells", [])))
    c3.metric("Набивных рядов", len(deep_lane_rows))
    c4.metric("Вместимость, паллет", f"{total_capacity:g}")


def render_geometry_data_tabs(model: dict) -> None:
    tabs = st.tabs(["Ряды", "Ячейки", "Проезды", "Навигация", "JSON"])
    with tabs[0]:
        st.dataframe(_localized_dataframe(model.get("rows", [])), use_container_width=True)
    with tabs[1]:
        st.dataframe(_localized_dataframe(model.get("cells", [])).head(10000), use_container_width=True)
    with tabs[2]:
        st.dataframe(_localized_dataframe(model.get("aisles", [])), use_container_width=True)
        st.dataframe(_localized_dataframe(model.get("roads", [])), use_container_width=True)
    with tabs[3]:
        st.dataframe(_localized_dataframe(model.get("navigation_nodes", [])), use_container_width=True)
        st.dataframe(_localized_dataframe(model.get("navigation_edges", [])), use_container_width=True)
    with tabs[4]:
        st.download_button("Скачать модель JSON", json.dumps(model, ensure_ascii=False, indent=2).encode("utf-8"), file_name="warehouse_model.json", mime="application/json")


def render_geometry_constructor_view(model: dict) -> None:
    st.subheader("Активная модель")
    overrides = load_manual_overrides()
    if overrides and overrides.get("source_model_id") != model.get("model_id"):
        overrides = None
    counts = manual_change_counts(overrides)
    st.caption(f"Последний склад загружен из Excel: {model.get('source_file_name', '—')} · Дата построения: {model.get('created_at', '—')}")
    st.caption(f"Ручных изменений: {counts['total']} · Добавлено вручную: {counts['add']} · Изменено вручную: {counts['update']} · Удалено вручную: {counts['delete']}")
    _model_summary_metrics(model)
    st.subheader("Диагностика импорта")
    settings = model.get("settings", {})
    stats = [
        ("Проездов между рядами", len(model.get("aisles", []))),
        ("Верхний проезд", f"{settings.get('top_road_width_m', 0)} м"),
        ("Нижний проезд", f"{settings.get('bottom_road_width_m', 0)} м"),
    ]
    cols = st.columns(len(stats))
    for col, (label, value) in zip(cols, stats):
        col.metric(label, value)
    diagnostics = model.get("diagnostics", [])
    if diagnostics:
        st.dataframe(pd.DataFrame(diagnostics), use_container_width=True)
    render_manual_cell_editor(model)
    model = st.session_state.get("geometry_model", model)
    model = render_weight_zone_editor(model)
    model = st.session_state.get("geometry_model", model)
    render_active_model_aisle_editor(model)
    model = st.session_state.get("geometry_model", model)
    model = render_inventory_placement(model)
    render_receipts_section(model)
    render_geometry_data_tabs(model)




def _map_cell_key(cell: dict) -> str:
    return f"{cell.get('row_number')}|{cell.get('cell_number')}|{cell.get('tier') or '1'}"


def _occupied_for_cell(model: dict, key: str) -> float:
    return sum(float(p.get("occupied_capacity_pallets", p.get("qty_pallets", 0)) or 0) for p in model.get("placements", []) if p.get("cell_key") == key)


def _save_map_edit_snapshot(model: dict) -> None:
    st.session_state["map_edit_undo_model"] = copy.deepcopy(model)


def _persist_map_edit(model: dict, message: str) -> None:
    save_geometry_model(model)
    st.session_state["geometry_model"] = model
    st.success(message)


def _cell_options(model: dict) -> dict[str, str]:
    return {f"Ряд {c.get('row_number')} · ячейка {c.get('cell_number')} · ярус {c.get('tier') or '1'}": _map_cell_key(c) for c in model.get("cells", [])}


def _row_options(model: dict) -> dict[str, str]:
    return {f"Ряд {r.get('row_number')} · порядок {r.get('row_order')}": str(r.get("row_number")) for r in model.get("rows", [])}


def _find_map_cell(model: dict, key: str) -> dict | None:
    return next((cell for cell in model.get("cells", []) if _map_cell_key(cell) == key), None)


def _find_map_row(model: dict, row_number: str) -> dict | None:
    return next((row for row in model.get("rows", []) if str(row.get("row_number")) == str(row_number)), None)


def _cell_duplicate_exists(model: dict, row_number: str, cell_number: str, tier: str, original_key: str = "") -> bool:
    new_key = f"{row_number}|{cell_number}|{tier or '1'}"
    return any(_map_cell_key(cell) == new_key and _map_cell_key(cell) != original_key for cell in model.get("cells", []))


def _row_has_placements(model: dict, row_number: str) -> bool:
    row_cells = {_map_cell_key(cell) for cell in model.get("cells", []) if str(cell.get("row_number")) == str(row_number)}
    return any(p.get("cell_key") in row_cells for p in model.get("placements", []))


def _add_cell_near(model: dict, selected: dict, where: str, new_number: str) -> tuple[bool, str]:
    tier = str(selected.get("tier") or "1")
    row_number = str(selected.get("row_number"))
    if _cell_duplicate_exists(model, row_number, new_number, tier):
        return False, "Ячейка с таким адресом уже существует."
    _save_map_edit_snapshot(model)
    row_cells = [cell for cell in model.get("cells", []) if str(cell.get("row_number")) == row_number]
    selected_idx = sorted(row_cells, key=lambda c: float(c.get("y_min", 0))).index(selected) if selected in row_cells else len(row_cells) - 1
    insert_idx = selected_idx if where == "before" else selected_idx + 1
    length = float(selected.get("length_m", 1.2) or 1.2)
    y_min = insert_idx * length
    new_cell = dict(selected)
    new_cell.update({"code": "", "cell_number": str(new_number), "y_min": y_min, "y_max": y_min + length, "y_center": y_min + length / 2, "source": "manual_add"})
    if selected.get("storage_type") != "deep_lane":
        new_cell["capacity_pallets"] = 1
    model["cells"].append(new_cell)
    for idx, cell in enumerate(sorted([c for c in model["cells"] if str(c.get("row_number")) == row_number], key=lambda c: float(c.get("y_min", 0)))):
        cell["y_min"] = idx * length
        cell["y_max"] = cell["y_min"] + length
        cell["y_center"] = cell["y_min"] + length / 2
    row = _find_map_row(model, row_number)
    if row:
        row["cells_count"] = len([c for c in model["cells"] if str(c.get("row_number")) == row_number])
        row["capacity_pallets"] = sum(float(c.get("capacity_pallets", 1) or 1) for c in model["cells"] if str(c.get("row_number")) == row_number)
        row["y_max"] = row["cells_count"] * length
    return True, "Ячейка добавлена."




def _selection_stats(model: dict, row_numbers: list[str], cell_keys: list[str]) -> dict:
    selected_cells = [c for c in model.get("cells", []) if _map_cell_key(c) in set(cell_keys) or str(c.get("row_number")) in set(row_numbers)]
    unique = {_map_cell_key(c): c for c in selected_cells}.values()
    capacity = sum(float(c.get("capacity_pallets", 1) or 1) for c in unique)
    occupied = sum(_occupied_for_cell(model, _map_cell_key(c)) for c in unique)
    return {"rows": len(set(row_numbers)), "cells": len(list(unique)), "capacity": capacity, "occupied": occupied, "free": max(capacity - occupied, 0)}


def _shift_rows(model: dict, row_numbers: list[str], dx: float, dy: float, snap: bool, step: float) -> tuple[bool, str]:
    if snap and step > 0:
        dx = round(dx / step) * step
        dy = round(dy / step) * step
    moving = [row for row in model.get("rows", []) if str(row.get("row_number")) in set(row_numbers)]
    if not moving:
        return False, "Выберите ряды для сдвига."
    snapshots = {str(r.get("row_number")): dict(r) for r in moving}
    for row in moving:
        row["x_min"] = float(row.get("x_min", 0) or 0) + dx
        row["x_max"] = float(row.get("x_max", 0) or 0) + dx
        row["x_center"] = float(row.get("x_center", 0) or 0) + dx
        row["y_min"] = float(row.get("y_min", 0) or 0) + dy
        row["y_max"] = float(row.get("y_max", 0) or 0) + dy
    for cell in model.get("cells", []):
        if str(cell.get("row_number")) in set(row_numbers):
            for key in ["x_min", "x_max", "x_center"]:
                cell[key] = float(cell.get(key, 0) or 0) + dx
            for key in ["y_min", "y_max", "y_center"]:
                cell[key] = float(cell.get(key, 0) or 0) + dy
            for slot in cell.get("physical_slots", []):
                for key in ["x_min", "x_max"]:
                    slot[key] = float(slot.get(key, 0) or 0) + dx
                for key in ["y_min", "y_max"]:
                    slot[key] = float(slot.get(key, 0) or 0) + dy
    # block row intersections after move
    rows = model.get("rows", [])
    for idx, a in enumerate(rows):
        for b in rows[idx + 1:]:
            if str(a.get("row_number")) == str(b.get("row_number")):
                continue
            overlap_x = float(a.get("x_min", 0)) < float(b.get("x_max", 0)) and float(a.get("x_max", 0)) > float(b.get("x_min", 0))
            overlap_y = float(a.get("y_min", 0)) < float(b.get("y_max", 0)) and float(a.get("y_max", 0)) > float(b.get("y_min", 0))
            if overlap_x and overlap_y:
                for row in moving:
                    row.update(snapshots[str(row.get("row_number"))])
                return False, f"Нельзя сохранить: ряд {a.get('row_number')} пересекается с рядом {b.get('row_number')}."
    return True, "Ряды сдвинуты."


def render_bulk_map_actions(model: dict, snap: bool, snap_step: float) -> dict:
    row_choices = _row_options(model)
    cell_choices = _cell_options(model)
    selected_row_labels = st.multiselect("Массовое выделение рядов", list(row_choices), key="map_bulk_rows")
    selected_cell_labels = st.multiselect("Массовое выделение ячеек", list(cell_choices), key="map_bulk_cells")
    row_numbers = [row_choices[label] for label in selected_row_labels]
    cell_keys = [cell_choices[label] for label in selected_cell_labels]
    stats = _selection_stats(model, row_numbers, cell_keys)
    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Выбрано рядов", stats["rows"])
    s2.metric("Выбрано ячеек", stats["cells"])
    s3.metric("Вместимость", f"{stats['capacity']:g}")
    s4.metric("Занято", f"{stats['occupied']:g}")
    s5.metric("Свободно", f"{stats['free']:g}")
    if st.button("Снять выделение", key="map_clear_bulk_selection"):
        st.session_state["map_bulk_rows"] = []
        st.session_state["map_bulk_cells"] = []
        st.rerun()
    with st.expander("Массовые действия", expanded=False):
        b1, b2, b3, b4 = st.columns(4)
        zone = b1.selectbox("weight_zone", ["heavy", "medium", "light", "unassigned"], key="bulk_zone")
        direction = b2.selectbox("Направление", ["bottom_to_top", "top_to_bottom"], key="bulk_direction")
        storage = b3.selectbox("Тип", ["normal", "deep_lane"], key="bulk_storage")
        capacity = b4.number_input("capacity_pallets", min_value=0.0, value=1.0, step=1.0, key="bulk_capacity")
        block = st.checkbox("Заблокировать выбранные объекты", key="bulk_block")
        if st.button("Применить к выбранным", key="bulk_apply"):
            if any(_occupied_for_cell(model, key) > capacity for key in cell_keys):
                st.error("Нельзя уменьшить вместимость ниже занятого количества паллет.")
            else:
                _save_map_edit_snapshot(model)
                for row in model.get("rows", []):
                    if str(row.get("row_number")) in set(row_numbers):
                        row.update({"weight_zone": zone, "cell_direction": direction, "row_storage_type": storage})
                for cell in model.get("cells", []):
                    if _map_cell_key(cell) in set(cell_keys) or str(cell.get("row_number")) in set(row_numbers):
                        cell.update({"weight_zone": zone if str(cell.get("row_number")) in set(row_numbers) else cell.get("weight_zone"), "cell_direction": direction, "storage_type": storage, "capacity_pallets": capacity, "source": "block_manual" if block else "manual_update"})
                _persist_map_edit(model, "Массовые изменения применены.")
                st.rerun()
        dx = st.number_input("Сдвиг выбранных рядов X, м", value=0.0, step=snap_step, key="bulk_shift_x")
        dy = st.number_input("Сдвиг выбранных рядов Y, м", value=0.0, step=snap_step, key="bulk_shift_y")
        if st.button("Сдвинуть выбранные ряды", key="bulk_shift_rows"):
            _save_map_edit_snapshot(model)
            ok, msg = _shift_rows(model, row_numbers, dx, dy, snap, snap_step)
            if ok:
                _persist_map_edit(model, msg)
                st.rerun()
            else:
                st.error(msg)
        confirm = st.checkbox("Подтвердить массовое удаление пустых ячеек", key="bulk_delete_confirm")
        if st.button("Удалить выбранные пустые ячейки", disabled=not confirm, key="bulk_delete_cells"):
            occupied_keys = [key for key in cell_keys if _occupied_for_cell(model, key) > 0]
            if occupied_keys:
                st.error("Нельзя удалить занятые ячейки.")
            else:
                _save_map_edit_snapshot(model)
                model["cells"] = [c for c in model.get("cells", []) if _map_cell_key(c) not in set(cell_keys)]
                _persist_map_edit(model, "Пустые ячейки удалены.")
                st.rerun()
    return model

def render_map_edit_panel(model: dict) -> dict:
    edit_mode = st.toggle("Режим редактирования", key="map_edit_mode")
    if not edit_mode:
        st.caption("Режим редактирования выключен: zoom и pan работают без изменения склада.")
        st.session_state.pop("map_selected_cell_key", None)
        st.session_state.pop("map_selected_row_number", None)
        return model
    st.caption("В режиме редактирования клики по карте подсвечивают объект. Для применения изменений выберите объект в панели ниже.")
    tool = st.radio("Инструмент", ["Выбор", "Перемещение", "Выделение рамкой"], horizontal=True, key="map_edit_tool")
    snap = st.checkbox("Привязка к сетке", value=True, key="map_snap_enabled")
    snap_step = st.selectbox("Шаг сетки, м", [0.1, 0.2, 0.5, 1.0], key="map_snap_step")
    quick = st.columns(7)
    quick[0].button("Выбор", key="map_quick_select")
    if quick[1].button("Снять выделение", key="map_quick_clear_selection"):
        st.session_state["map_bulk_rows"] = []
        st.session_state["map_bulk_cells"] = []
        st.session_state.pop("map_selected_cell_key", None)
        st.session_state.pop("map_selected_row_number", None)
        st.rerun()
    object_type = st.radio("Объект", ["Ячейка", "Ряд"], horizontal=True, key="map_edit_object_type")
    model = render_bulk_map_actions(model, snap, float(snap_step))
    if quick[5].button("Сохранить", key="map_edit_save"):
        save_geometry_model(model)
        st.success("Текущая модель сохранена.")
    if quick[6].button("Отменить последнее изменение", key="map_edit_undo"):
        undo = st.session_state.get("map_edit_undo_model")
        if undo:
            save_geometry_model(undo)
            st.session_state["geometry_model"] = undo
            st.success("Последнее изменение отменено.")
            st.rerun()
        else:
            st.warning("Нет изменения для отмены.")

    if object_type == "Ячейка":
        options = _cell_options(model)
        if not options:
            st.info("В модели нет ячеек.")
            return model
        label = st.selectbox("Выбранная ячейка", list(options), key="map_cell_select")
        key = options[label]
        st.session_state["map_selected_cell_key"] = key
        st.session_state.pop("map_selected_row_number", None)
        cell = _find_map_cell(model, key)
        if not cell:
            return model
        occupied = _occupied_for_cell(model, key)
        capacity = float(cell.get("capacity_pallets", 1) or 1)
        st.write({"ряд": cell.get("row_number"), "ячейка": cell.get("cell_number"), "ярус": cell.get("tier"), "адрес": key, "тип": cell.get("storage_type"), "вместимость": capacity, "занято": occupied, "свободно": max(capacity - occupied, 0), "весовая зона": cell.get("weight_zone", "unassigned"), "активна": "block" not in str(cell.get("source", "")).lower()})
        c1, c2, c3, c4 = st.columns(4)
        new_number = c1.text_input("Новый номер ячейки", value=str(cell.get("cell_number", "")), key="map_cell_new_number")
        new_capacity = c2.number_input("Вместимость", min_value=0.0, value=capacity, step=1.0, key="map_cell_capacity")
        new_type = c3.selectbox("Тип", ["normal", "deep_lane"], index=1 if cell.get("storage_type") == "deep_lane" else 0, key="map_cell_type")
        blocked = c4.checkbox("Заблокирована", value="block" in str(cell.get("source", "")).lower(), key="map_cell_blocked")
        if st.button("Применить изменения ячейки", key="map_cell_apply"):
            if _cell_duplicate_exists(model, str(cell.get("row_number")), new_number, str(cell.get("tier") or "1"), key):
                st.error("Ячейка с таким адресом уже существует.")
            elif new_capacity < occupied:
                st.error("Нельзя уменьшить вместимость ниже занятого количества паллет.")
            else:
                _save_map_edit_snapshot(model)
                old_key = _map_cell_key(cell)
                cell["cell_number"] = str(new_number)
                cell["capacity_pallets"] = new_capacity
                cell["storage_type"] = new_type
                cell["source"] = "block_manual" if blocked else "manual_update"
                new_key = _map_cell_key(cell)
                for placement in model.get("placements", []):
                    if placement.get("cell_key") == old_key:
                        placement["cell_key"] = new_key
                        placement["cell_number"] = str(new_number)
                _persist_map_edit(model, "Ячейка обновлена.")
                st.rerun()
        add_number = st.text_input("Номер добавляемой ячейки", value=str(int(float(cell.get("cell_number", 0) or 0)) + 1) if str(cell.get("cell_number", "")).isdigit() else "", key="map_cell_add_number")
        a1, a2, a3 = st.columns(3)
        if a1.button("Добавить до", key="map_cell_add_before"):
            ok, msg = _add_cell_near(model, cell, "before", add_number)
            (st.success if ok else st.error)(msg)
            if ok:
                _persist_map_edit(model, msg); st.rerun()
        if a2.button("Добавить после", key="map_cell_add_after"):
            ok, msg = _add_cell_near(model, cell, "after", add_number)
            (st.success if ok else st.error)(msg)
            if ok:
                _persist_map_edit(model, msg); st.rerun()
        confirm = st.checkbox("Подтвердить удаление ячейки", key="map_cell_delete_confirm")
        if a3.button("Удалить ячейку", disabled=not confirm, key="map_cell_delete"):
            if occupied > 0:
                st.error("Ячейка занята. Сначала переместите или сбросьте размещение товара.")
            else:
                _save_map_edit_snapshot(model)
                model["cells"] = [c for c in model.get("cells", []) if _map_cell_key(c) != key]
                _persist_map_edit(model, "Ячейка удалена.")
                st.session_state.pop("map_selected_cell_key", None)
                st.rerun()
        quick[1].button("+ Ячейка", key="map_quick_add_cell", disabled=False)
        quick[2].button("− Ячейка", key="map_quick_del_cell", disabled=False)
    else:
        options = _row_options(model)
        if not options:
            st.info("В модели нет рядов.")
            return model
        label = st.selectbox("Выбранный ряд", list(options), key="map_row_select")
        row_number = options[label]
        st.session_state["map_selected_row_number"] = row_number
        st.session_state.pop("map_selected_cell_key", None)
        row = _find_map_row(model, row_number)
        row_cells = [c for c in model.get("cells", []) if str(c.get("row_number")) == row_number]
        occupied = sum(_occupied_for_cell(model, _map_cell_key(c)) for c in row_cells)
        st.write({"номер ряда": row_number, "row_order": row.get("row_order"), "ячеек": len(row_cells), "направление": row.get("cell_direction"), "тип": row.get("row_storage_type"), "весовая зона": row.get("weight_zone", "unassigned"), "вместимость": sum(float(c.get("capacity_pallets", 1) or 1) for c in row_cells), "занято": occupied})
        r1, r2, r3, r4 = st.columns(4)
        new_row_number = r1.text_input("Новый номер ряда", value=row_number, key="map_row_new_number")
        new_order = r2.number_input("row_order", value=float(row.get("row_order", 1) or 1), step=1.0, key="map_row_order")
        new_direction = r3.selectbox("Направление", ["bottom_to_top", "top_to_bottom"], index=1 if row.get("cell_direction") == "top_to_bottom" else 0, key="map_row_direction")
        new_zone = r4.selectbox("Весовая зона", ["heavy", "medium", "light", "unassigned"], index=["heavy", "medium", "light", "unassigned"].index(row.get("weight_zone", "unassigned")) if row.get("weight_zone", "unassigned") in ["heavy", "medium", "light", "unassigned"] else 3, key="map_row_zone")
        new_storage = st.selectbox("Тип хранения ряда", ["normal", "deep_lane"], index=1 if row.get("row_storage_type") == "deep_lane" else 0, key="map_row_storage")
        row_capacity = st.number_input("Массовая вместимость ячеек ряда", min_value=0.0, value=float(row_cells[0].get("capacity_pallets", 1) if row_cells else 1), step=1.0, key="map_row_capacity")
        if st.button("Применить изменения ряда", key="map_row_apply"):
            if new_row_number != row_number and any(str(r.get("row_number")) == new_row_number for r in model.get("rows", [])):
                st.error("Ряд с таким номером уже существует.")
            elif any(_occupied_for_cell(model, _map_cell_key(c)) > row_capacity for c in row_cells):
                st.error("Нельзя уменьшить вместимость ниже занятого количества паллет.")
            else:
                _save_map_edit_snapshot(model)
                for r in model.get("rows", []):
                    if str(r.get("row_number")) == row_number:
                        r.update({"row_number": new_row_number, "row_order": new_order, "cell_direction": new_direction, "weight_zone": new_zone, "row_storage_type": new_storage, "capacity_pallets": row_capacity * len(row_cells)})
                for c in row_cells:
                    old_key = _map_cell_key(c)
                    c.update({"row_number": new_row_number, "row_order": new_order, "cell_direction": new_direction, "weight_zone": new_zone, "storage_type": new_storage, "capacity_pallets": row_capacity})
                    new_key = _map_cell_key(c)
                    for p in model.get("placements", []):
                        if p.get("cell_key") == old_key:
                            p.update({"cell_key": new_key, "row_number": new_row_number, "weight_zone": new_zone})
                _persist_map_edit(model, "Ряд обновлён.")
                st.rerun()
        confirm_row = st.checkbox("Подтвердить удаление ряда", key="map_row_delete_confirm")
        if st.button("Удалить ряд", disabled=not confirm_row, key="map_row_delete"):
            if _row_has_placements(model, row_number):
                st.error("В ряду есть товар. Сначала переместите или сбросьте размещение товара.")
            else:
                _save_map_edit_snapshot(model)
                model["rows"] = [r for r in model.get("rows", []) if str(r.get("row_number")) != row_number]
                model["cells"] = [c for c in model.get("cells", []) if str(c.get("row_number")) != row_number]
                _persist_map_edit(model, "Ряд удалён.")
                st.session_state.pop("map_selected_row_number", None)
                st.rerun()
        quick[3].button("+ Ряд", key="map_quick_add_row")
        quick[4].button("− Ряд", key="map_quick_del_row", disabled=False)
    return model

def render_geometry_map_view(model: dict) -> None:
    st.subheader("Карта склада")
    _model_summary_metrics(model)
    model = render_map_edit_panel(model)
    control_left, control_right = st.columns([1, 2])
    with control_left:
        detailed = st.toggle("Детальный режим", value=len(model.get("cells", [])) <= 1500, key="map_detailed_mode")
    with control_right:
        scale = st.slider("Масштаб, px/м", min_value=4.0, max_value=60.0, value=22.0, step=1.0, key="map_scale")
    label_settings = render_map_settings_editor()
    label_settings["edit_mode"] = bool(st.session_state.get("map_edit_mode", False))
    label_settings["selected_cell_key"] = st.session_state.get("map_selected_cell_key", "")
    label_settings["selected_row_number"] = st.session_state.get("map_selected_row_number", "")
    label_settings["edit_tool"] = st.session_state.get("map_edit_tool", "Выбор")
    label_settings["snap_enabled"] = bool(st.session_state.get("map_snap_enabled", True))
    label_settings["snap_step"] = float(st.session_state.get("map_snap_step", 0.1) or 0.1)
    render_started = perf_counter()
    html = build_geometry_html_cached(json.dumps(model, ensure_ascii=False), scale, detailed, json.dumps(label_settings, ensure_ascii=False, sort_keys=True))
    components.html(html, height=980, scrolling=True)
    st.caption(f"Рендер карты: {perf_counter() - render_started:.2f} сек. Модель: data/last_import/warehouse_model.json")


def render_virtual_warehouse_excel() -> None:
    st.sidebar.caption(f"Сборка приложения: {APP_BUILD_LABEL}")
    render_git_release_badge()
    mode = st.sidebar.radio(
        "Режим",
        ["Склад из Excel: ряды + ячейки + проезды", "Виртуальный склад по Excel-схеме"],
        index=0,
    )
    if mode == "Склад из Excel: ряды + ячейки + проезды":
        render_excel_geometry_warehouse()
        return

    st.title("Симулятор скорости сборки")
    st.header("Виртуальный склад по Excel-схеме")
    st.sidebar.info("Оставлен только режим виртуального склада Excel. Старые разделы скрыты из интерфейса.")

    ensure_loaded_model()
    if st.session_state.get("last_import_error"):
        st.warning(f"Сохранённая модель не загружена: {st.session_state['last_import_error']}. Загрузите Excel заново.")

    with st.sidebar:
        st.subheader("Последний склад")
        if MODEL_PATH.exists():
            if st.button("Использовать сохранённый склад"):
                for key in ["virtual_warehouse_model", "virtual_warehouse_render_cache", "virtual_warehouse_meta"]:
                    st.session_state.pop(key, None)
                ensure_loaded_model()
                st.rerun()
            if st.button("Очистить сохранённый склад"):
                clear_last_import()
                for key in list(st.session_state.keys()):
                    if key.startswith("virtual_warehouse") or key == "last_import_error":
                        st.session_state.pop(key, None)
                st.rerun()
        else:
            st.caption("Сохранённого склада пока нет.")

    with st.expander("Загрузка нового Excel", expanded="virtual_warehouse_model" not in st.session_state):
        schema_file = st.file_uploader("Excel-схема склада", type=["xlsx"], key="virtual_warehouse_schema_upload")
        selected_sheets: list[str] | None = None
        schema_hash = ""
        schema_bytes = b""
        if schema_file is not None:
            schema_bytes = schema_file.getvalue()
            schema_hash = file_hash(schema_bytes)
            sheet_names = get_excel_sheet_names(schema_bytes, schema_hash)
            selected_sheets = st.multiselect("Листы для обработки", sheet_names, default=sheet_names[:1])
        cell_file = st.file_uploader("Файл номеров ячеек (необязательно)", type=["xlsx", "csv"], key="virtual_warehouse_cells_upload")
        placement_file = st.file_uploader("Файл размещения товаров (необязательно)", type=["xlsx", "csv"], key="virtual_warehouse_placements_upload")
        build_clicked = st.button("Построить склад", disabled=schema_file is None or not selected_sheets)

    if build_clicked:
        diagnostics: list[dict] = []
        try:
            timings = {}
            started = perf_counter()
            model = parse_warehouse_excel_cached(schema_bytes, schema_hash, tuple(selected_sheets or []))
            timings["read_excel_seconds"] = perf_counter() - started
            build_started = perf_counter()
            if cell_file is not None:
                addresses_by_row, cell_diagnostics = import_cell_addresses(cell_file)
                diagnostics.extend(cell_diagnostics)
                diagnostics.extend(apply_cell_addresses(model, addresses_by_row))
            if placement_file is not None:
                placements, placement_diagnostics = import_placements(placement_file)
                diagnostics.extend(placement_diagnostics)
                diagnostics.extend(apply_placements(model, placements))
            timings["build_model_seconds"] = perf_counter() - build_started
            render_started = perf_counter()
            render_cache = prepare_render_cache_cached(model_to_dict(model))
            timings["prepare_render_seconds"] = perf_counter() - render_started
            meta = {
                "model_version": MODEL_VERSION,
                "source": "новый Excel",
                "imported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "scheme_file_name": schema_file.name,
                "scheme_file_hash": schema_hash,
                "scheme_file_size": len(schema_bytes),
                "cells_file_name": getattr(cell_file, "name", ""),
                "cells_file_hash": file_hash(cell_file.getvalue()) if cell_file else "",
                "placement_file_name": getattr(placement_file, "name", ""),
                "placement_file_hash": file_hash(placement_file.getvalue()) if placement_file else "",
                "selected_sheets": selected_sheets,
                "model_path": str(MODEL_PATH),
                "diagnostics": diagnostics,
                "performance": timings,
                "loaded_from_cache": False,
            }
            save_uploaded_copy(schema_file, "source_scheme.xlsx")
            save_uploaded_copy(cell_file, "source_cells.xlsx")
            save_uploaded_copy(placement_file, "source_placement.xlsx")
            timings["save_model_seconds"] = save_last_import(model, render_cache, meta)
            meta["performance"] = timings
            write_json_atomic(META_PATH, meta)
            st.session_state["virtual_warehouse_model"] = model
            st.session_state["virtual_warehouse_render_cache"] = render_cache
            st.session_state["virtual_warehouse_meta"] = meta
            st.session_state["virtual_warehouse_source"] = "новый Excel"
            st.success(f"Склад построен и сохранён: {len(model.cells)} ячеек. Старая модель перезаписана только после успешного построения.")
        except Exception as exc:
            st.error(f"Не удалось построить склад. Последняя успешная сохранённая модель не удалена: {exc}")

    model = st.session_state.get("virtual_warehouse_model")
    if model is None:
        st.info("Загрузите Excel-схему и нажмите «Построить склад» или используйте ранее сохранённый склад.")
        return

    meta = st.session_state.get("virtual_warehouse_meta", {})
    diagnostics = meta.get("diagnostics", st.session_state.get("virtual_warehouse_diagnostics", []))
    render_diagnostics(model, meta, diagnostics)
    render_performance(meta)

    sheet_names = [sheet.name for sheet in model.sheets]
    selected_sheet = next(sheet for sheet in model.sheets if sheet.name == st.selectbox("Лист склада", sheet_names))
    cells = selected_sheet.potential_cells if hasattr(selected_sheet, "potential_cells") else [cell for row in selected_sheet.rows for cell in row.potential_cells]
    zones = ["Все"] + sorted({cell.fill_color or "без зоны" for cell in cells})
    rows = ["Все"] + sorted({str(row.row_number) for row in selected_sheet.rows})
    tiers = ["Все"] + sorted({str(cell.tier_number) for cell in cells})
    f1, f2, f3, f4 = st.columns(4)
    zone_filter = f1.selectbox("Зона", zones)
    row_filter = f2.selectbox("Ряд", rows)
    tier_filter = f3.selectbox("Ярус", tiers)
    availability_filter = f4.selectbox("Показывать", ["Все", "Только активные", "Только заблокированные"])

    filtered_sheet = filter_sheet(selected_sheet, zone_filter, row_filter, tier_filter, availability_filter)
    filtered_count = sum(len(row.potential_cells) for row in filtered_sheet.rows)
    summary_mode = filtered_count > 2000 and row_filter == "Все"
    if summary_mode:
        st.warning("Включён общий вид: ячеек больше 2000. Выберите ряд/фильтр для детального интерактива по ячейкам.")
    scale = st.slider("Масштаб сетки", min_value=18, max_value=60, value=34, step=2)
    render_started = perf_counter()
    html = build_virtual_warehouse_html_cached(asdict(filtered_sheet), scale, summary_mode)
    components.html(html, height=760, scrolling=True)
    render_seconds = perf_counter() - render_started
    meta.setdefault("performance", {})["render_seconds"] = render_seconds
    st.caption(f"Последний рендер: {render_seconds:.2f} сек.; показано ячеек: {filtered_count}.")

    tab_rows, tab_cells, tab_diag = st.tabs(["Ряды", "Ячейки", "Диагностика"])
    with tab_rows:
        st.dataframe(pd.DataFrame([asdict(row) | {"cells": len(row.potential_cells)} for row in filtered_sheet.rows]).drop(columns=["potential_cells"], errors="ignore"), use_container_width=True)
    with tab_cells:
        st.dataframe(pd.DataFrame([asdict(cell) for row in filtered_sheet.rows for cell in row.potential_cells]).head(5000), use_container_width=True)
    with tab_diag:
        diag_df = pd.DataFrame(build_diagnostics(model, diagnostics))
        st.dataframe(diag_df, use_container_width=True)
        st.download_button("Скачать диагностику CSV", diag_df.to_csv(index=False).encode("utf-8-sig"), file_name="virtual_warehouse_diagnostics.csv", mime="text/csv")
        st.download_button("Скачать модель JSON", json.dumps(model_to_dict(model), ensure_ascii=False, indent=2).encode("utf-8"), file_name="warehouse_model.json", mime="application/json")


_VIRTUAL_WAREHOUSE_APP_RENDERED = False


def main() -> None:
    global _VIRTUAL_WAREHOUSE_APP_RENDERED
    if get_script_run_ctx(suppress_warning=True) is None and __name__ != "__main__":
        return
    if _VIRTUAL_WAREHOUSE_APP_RENDERED:
        return
    _VIRTUAL_WAREHOUSE_APP_RENDERED = True
    render_virtual_warehouse_excel()


if __name__ == "__main__" or get_script_run_ctx(suppress_warning=True) is not None:
    main()

