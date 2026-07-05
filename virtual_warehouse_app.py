from __future__ import annotations

import hashlib
import json
import shutil
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

st.set_page_config(page_title="Симулятор сборки", layout="wide")

APP_BUILD_LABEL = "virtual-excel-only-2026-07-04"
MODEL_VERSION = 1
LAST_IMPORT_DIR = Path("data/last_import")
MODEL_PATH = LAST_IMPORT_DIR / "warehouse_model.json"
RENDER_CACHE_PATH = LAST_IMPORT_DIR / "render_cache.json"
META_PATH = LAST_IMPORT_DIR / "import_meta.json"


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
def build_geometry_html_cached(model_payload: str, scale: float, detailed: bool) -> str:
    return build_geometry_html(json.loads(model_payload), scale=scale, detailed=detailed)


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
                st.session_state.pop("geometry_model", None)
                st.rerun()
        else:
            st.caption("Сохранённой геометрии пока нет.")

    uploaded = st.file_uploader("Excel со списком фактических ячеек", type=["xlsx"], key="geometry_cells_file")
    if uploaded is None:
        if saved_model:
            st.info("Загрузите новый Excel или используйте сохранённую модель из боковой панели.")
        else:
            st.info("Загрузите Excel со списком ячеек в формате: Код | Ряд | Ячейка | Ярус.")
        model = st.session_state.get("geometry_model")
        if model:
            render_geometry_model_view(model)
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
            "row_group": "Группа рядов",
            "side": "Сторона/зона",
            "comment": "Комментарий",
        },
    )
    row_config["row_storage_type"] = row_config["row_storage_type"].map({"Обычный ряд": "normal", "Набивной ряд": "deep_lane"}).fillna(row_config["row_storage_type"])
    row_config["cell_direction"] = row_config["cell_direction"].map({"Снизу вверх": "bottom_to_top", "Сверху вниз": "top_to_bottom"}).fillna(row_config["cell_direction"])
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
        st.warning("Загружен новый Excel. Старые ручные изменения и настройки набивных рядов сброшены для новой модели.")
        save_started = perf_counter()
        save_geometry_model(model)
        timings["save_model_seconds"] = perf_counter() - save_started
        model["performance"] = timings | model.get("performance", {})
        save_geometry_model(model)
        st.session_state["geometry_model"] = model
        st.success(f"Геометрическая модель построена и сохранена: {len(model['rows'])} рядов, {len(model['cells'])} ячеек, {len(model['aisles'])} проездов.")

    model = st.session_state.get("geometry_model")
    if model:
        render_geometry_model_view(model)



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

def render_geometry_model_view(model: dict) -> None:
    st.subheader("Активная модель")
    overrides = load_manual_overrides()
    if overrides and overrides.get("source_model_id") != model.get("model_id"):
        overrides = None
    counts = manual_change_counts(overrides)
    st.caption(f"Последний склад загружен из Excel: {model.get('source_file_name', '—')} · Дата построения: {model.get('created_at', '—')}")
    st.caption(f"Ручных изменений: {counts['total']} · Добавлено вручную: {counts['add']} · Изменено вручную: {counts['update']} · Удалено вручную: {counts['delete']}")
    st.subheader("Диагностика импорта")
    settings = model.get("settings", {})
    stats = [
        ("Рядов", len(model.get("rows", []))),
        ("Ячеек", len(model.get("cells", []))),
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
    st.subheader("Карта склада")
    detailed = st.toggle("Детальный режим", value=len(model.get("cells", [])) <= 1500)
    scale = st.slider("Масштаб, px/м", min_value=4.0, max_value=40.0, value=18.0, step=1.0)
    render_started = perf_counter()
    html = build_geometry_html_cached(json.dumps(model, ensure_ascii=False), scale, detailed)
    components.html(html, height=760, scrolling=True)
    st.caption(f"Рендер карты: {perf_counter() - render_started:.2f} сек. Модель: data/last_import/warehouse_model.json")
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


def render_virtual_warehouse_excel() -> None:
    st.sidebar.caption(f"Сборка приложения: {APP_BUILD_LABEL}")
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

