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
from openpyxl import load_workbook

from warehouse_diagnostics import build_diagnostics
from warehouse_excel_parser import parse_warehouse_excel
from warehouse_model import WarehouseCell, WarehouseModel, WarehouseRow, WarehouseSheet
from warehouse_placement import apply_cell_addresses, apply_placements, import_cell_addresses, import_placements
from warehouse_visualization import build_virtual_warehouse_html, prepare_render_cache

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


def render_virtual_warehouse_excel() -> None:
    st.title("Симулятор скорости сборки")
    st.header("Виртуальный склад по Excel-схеме")
    st.sidebar.caption(f"Сборка приложения: {APP_BUILD_LABEL}")
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

if __name__ == "__main__":
    render_virtual_warehouse_excel()
