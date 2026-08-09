"""Read-only Streamlit UI for the CURRENT versus PROPOSED experiment."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import pandas as pd
import streamlit as st

from warehouse_actual_inventory_import import (
    build_actual_inventory_placement_state, detect_actual_inventory_columns,
    get_actual_inventory_sheet_names, read_actual_inventory_table,
)
from warehouse_day_receipt_scenario_inputs import build_day_receipt_scenario_inputs
from warehouse_day_receipts_import import (
    build_day_receipts_import, detect_day_receipts_columns,
    get_day_receipts_sheet_names, read_day_receipts_table,
)
from warehouse_inventory_results_import import (
    build_inventory_results_import, detect_inventory_results_columns,
    get_inventory_results_sheet_names, read_inventory_results_table,
    select_inventory_rows_for_opening_stock,
)
from warehouse_outbound_orders import (
    detect_outbound_columns, get_outbound_sheet_names, normalize_outbound_table,
    read_outbound_table,
)
from warehouse_business_identity import normalize_warehouse
from warehouse_placement_zones import get_assignable_placement_zones, get_placement_zone_label
from warehouse_state_cache import load_outbound_orders_cached, load_receipts_state_cached
from warehouse_scenario_comparison_ui import render_scenario_comparison


ZONE_TO_CODE = {get_placement_zone_label(zone): zone for zone in get_assignable_placement_zones()}
CODE_TO_ZONE = {value: key for key, value in ZONE_TO_CODE.items()}
SESSION_KEYS = (
    "outbound_experiment_input_state", "outbound_experiment_input_diagnostics",
    "outbound_experiment_state", "outbound_experiment_diagnostics",
    "outbound_experiment_ui_signature",
)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def build_experiment_ui_signature(**inputs: Any) -> str:
    """Return a deterministic fingerprint of every user-controlled input."""
    payload = json.dumps(_canonical(inputs), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def experiment_result_is_stale(current_signature: str, saved_signature: str | None) -> bool:
    return bool(saved_signature and current_signature != saved_signature)


def format_experiment_metric(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.2f}{suffix}".replace(",", " ")
    return str(value)


def describe_distance_effect(value: Any) -> str:
    if value is None or value == 0:
        return "Без изменений"
    return "Улучшение" if value > 0 else "Ухудшение"


def build_experiment_order_rows(order_comparisons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {"improved": "Лучше", "worsened": "Хуже", "equal": "Без изменений",
              "no_route_activity": "Нет маршрута", "not_comparable": "Несопоставим"}
    return [{
        "Дата": row.get("created_at"), "РО": row.get("outbound_order_number"),
        "Статус сравнения": labels.get(row.get("classification"), row.get("classification")),
        "CURRENT, м": row.get("current_distance_m"), "PROPOSED, м": row.get("proposed_distance_m"),
        "Разница, м": row.get("distance_saved_m"), "Изменение, %": row.get("improvement_percent"),
        "Запрошено": row.get("requested_units"), "Собрано CURRENT": row.get("current_picked_units"),
        "Собрано PROPOSED": row.get("proposed_picked_units"), "Дефицит CURRENT": row.get("current_shortage_units"),
        "Дефицит PROPOSED": row.get("proposed_shortage_units"), "Сопоставим": row.get("strict_comparable"),
        "Причины": ", ".join(row.get("reasons") or []),
    } for row in order_comparisons]


def build_experiment_quality_rows(experiment_state: dict[str, Any]) -> list[dict[str, Any]]:
    comparison = experiment_state.get("states", {}).get("comparison", {})
    quality, raw = comparison.get("quality", {}), comparison.get("raw_summary", {})
    summary = experiment_state.get("summary", {})
    values = {
        "full_day_effect_valid": quality.get("full_day_effect_valid"),
        "comparison_status": quality.get("comparison_status"), "scope": quality.get("scope"),
        "current shortage": raw.get("current_shortage_units"), "proposed shortage": raw.get("proposed_shortage_units"),
        "current unresolved receipt qty": raw.get("current_receipt_unresolved_qty_units"),
        "proposed unresolved receipt qty": raw.get("proposed_receipt_unresolved_qty_units"),
        "receipt SKU without slotting rule": summary.get("receipt_skus_without_slotting_rule"),
        "graph ready": summary.get("graph_ready_for_replay"), "blocked stage": experiment_state.get("blocked_stage"),
    }
    return [{"Показатель": key, "Значение": value} for key, value in values.items()]


def experiment_inputs_ready(*, day_receipt_state: Any, start_state: Any, end_state: Any,
                            opening_inventory_rows: Any, outbound_rows: Any, gate_confirmed: bool) -> bool:
    """UI readiness only; absent slotting is deliberately not a blocker."""
    # Receipts and END are optional validation inputs in the V1 opening-stock replay.
    del day_receipt_state, end_state, opening_inventory_rows
    return bool(isinstance(start_state, Mapping) and start_state.get("placements")
                and outbound_rows and gate_confirmed)


def start_warehouses(start_state: Mapping[str, Any] | None) -> list[str]:
    """Return distinct normalized START warehouses without alias inference."""
    return sorted({warehouse for row in (start_state or {}).get("placements", [])
                   if (warehouse := normalize_warehouse(
                       row.get("normalized_warehouse") or row.get("warehouse")))})


def select_start_warehouse(warehouses: list[str], explicit: Any = None) -> tuple[str | None, str | None]:
    """Auto-select one factual scope and require a choice when several exist."""
    normalized = normalize_warehouse(explicit)
    if len(warehouses) == 1:
        return warehouses[0], None
    if len(warehouses) > 1 and normalized not in warehouses:
        return None, "multiple_start_warehouses_require_selection"
    return (normalized or None), (None if normalized else "start_warehouse_not_available")


def validate_outbound_scope(rows: Any, warehouse: Any, operational_date: Any) -> list[str]:
    """Validate exact warehouse/day scope without silently selecting another day."""
    target = normalize_warehouse(warehouse)
    if not rows:
        return ["outbound_rows_not_supplied"]
    warehouse_rows = [row for row in rows if normalize_warehouse(row.get("warehouse")) == target]
    if not warehouse_rows:
        return ["start_outbound_warehouse_scope_mismatch"]
    day = str(operational_date or "")[:10]
    if not any(str(row.get("created_at") or "")[:10] == day for row in warehouse_rows):
        return ["selected_operational_date_has_no_accepted_outbound_orders"]
    return []


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _excel_upload(label: str, key: str, sheet_fn, read_fn, detect_fn, build_fn, *build_args):
    uploaded = st.file_uploader(label, type=["xlsx", "xls"], key=key)
    if uploaded is None:
        return None, {}, "", ""
    data = uploaded.getvalue()
    sheets = sheet_fn(data)
    sheet = st.selectbox(f"Лист — {label}", sheets, key=f"{key}_sheet")
    table = read_fn(data, sheet)
    mapping = detect_fn(table)
    state, diagnostics = build_fn(*build_args, table, mapping) if build_args else build_fn(table, mapping)
    missing = diagnostics.get("missing_required_columns", [])
    (st.error if missing else st.caption)(f"Строк: {len(table)}; отсутствующие обязательные колонки: {missing or 'нет'}")
    return state, diagnostics, _hash_bytes(data), sheet


def _render_results(state: dict[str, Any], diagnostics: dict[str, Any], input_state: dict[str, Any]) -> None:
    if state.get("execution_status") == "blocked":
        st.error(f"Эксперимент заблокирован: {state.get('blocked_stage')}; {state.get('blocked_reasons')}")
        return
    comparison = state.get("states", {}).get("comparison", {})
    quality, comparable = comparison.get("quality", {}), comparison.get("comparable_summary", {})
    coverage = comparison.get("coverage", {})
    status = quality.get("comparison_status")
    route_orders = coverage.get("comparable_route_orders", 0)
    if status == "not_comparable" or not route_orders:
        st.warning("Недостаточно сопоставимых маршрутов для оценки эффекта.")
    else:
        full = quality.get("full_day_effect_valid") is True
        (st.success if full else st.warning)("Полный день сопоставим" if full else
            "Результат рассчитан только по сопоставимым РО и не является эффектом полного дня.")
        suffix = "" if full else " — сопоставимые РО"
        metrics = [(f"CURRENT, м{suffix}", comparable.get("current_comparable_distance_m")),
                   (f"PROPOSED, м{suffix}", comparable.get("proposed_comparable_distance_m")),
                   (f"Экономия, м{suffix}", comparable.get("distance_saved_m")),
                   ("Изменение, %", comparable.get("improvement_percent")),
                   ("Сопоставимых РО", route_orders), ("Покрытие РО, %", coverage.get("route_order_coverage_percent"))]
        for column, (label, value) in zip(st.columns(6), metrics):
            delta = describe_distance_effect(value) if label.startswith("Экономия") else None
            column.metric(label, format_experiment_metric(value), delta=delta, delta_color="normal")
    st.subheader("Покрытие")
    coverage_view = dict(coverage)
    coverage_view["non_comparable_orders"] = coverage.get("accepted_orders", 0) - coverage.get("strict_comparable_orders", 0)
    st.dataframe(pd.DataFrame([coverage_view]), use_container_width=True)
    st.subheader("Качество эксперимента")
    st.dataframe(pd.DataFrame(build_experiment_quality_rows(state)), use_container_width=True, hide_index=True)
    st.subheader("Сравнение расходных ордеров")
    st.dataframe(pd.DataFrame(build_experiment_order_rows(comparison.get("order_comparisons", []))), use_container_width=True)
    with st.expander("Технические RAW показатели"):
        st.warning("Разница RAW distance не является бизнес-эффектом, если сервис CURRENT и PROPOSED различается.")
        st.json(comparison.get("raw_summary", {}))
    with st.expander("Диагностика этапов"):
        st.json({"completed_stages": state.get("completed_stages"), "blocked_stage": state.get("blocked_stage"),
                 "blocked_reasons": state.get("blocked_reasons"), "stage_diagnostics": state.get("stage_diagnostics"),
                 "diagnostics": diagnostics})
    with st.expander("Исходные данные эксперимента"):
        st.json(input_state.get("summary", {}))


def render_outbound_experiment(model: dict[str, Any]) -> None:
    """Render the authoritative SimulationState CURRENT/PROPOSED benchmark."""
    st.subheader("Авторитетный CURRENT vs PROPOSED")
    st.caption("V1: начальный остаток → расходные ордера; приход внутри дня не моделируется. END — только независимая валидация.")
    st.markdown("**Минимум для первого расчёта**")
    st.caption("START / остатки по ячейкам · РО · ворота · геометрия склада, уже загруженная в проект")
    st.markdown("**Дополнительный контроль**")
    st.caption("inventory-results · END snapshot · receipts")
    start_state, start_diag, start_hash, start_sheet = _excel_upload(
        "Фактическое размещение / остаток по ячейкам — НАЧАЛО дня", "experiment_start", get_actual_inventory_sheet_names,
        read_actual_inventory_table, detect_actual_inventory_columns, build_actual_inventory_placement_state, model)
    receipt_state, receipt_diag, receipt_hash, receipt_sheet = _excel_upload(
        "Дневной приход — не используется в V1 benchmark", "experiment_receipts", get_day_receipts_sheet_names,
        read_day_receipts_table, detect_day_receipts_columns, build_day_receipts_import)
    operational_date = None
    day_state: dict[str, Any] = {}
    warehouses = start_warehouses(start_state)
    selected_warehouse = None
    if len(warehouses) == 1:
        selected_warehouse = warehouses[0]
        st.caption(f"Склад START выбран автоматически: {selected_warehouse}")
    elif len(warehouses) > 1:
        selected_warehouse = st.selectbox("Склад START", warehouses, index=None,
                                          placeholder="Выберите один склад", key="experiment_start_warehouse")
    elif start_state:
        st.error("START не содержит нормализованного склада.")
    if receipt_state:
        accepted = receipt_state.get("accepted_rows", [])
        dates = sorted({str(row.get("receipt_date"))[:10] for row in accepted if row.get("receipt_date")})
        operational_date = st.selectbox("Операционный день", dates, key="experiment_day") if dates else None
        if operational_date and selected_warehouse:
            day_state, day_diag = build_day_receipt_scenario_inputs(
                receipt_state, operational_date=operational_date, selected_warehouses=[selected_warehouse])
            if day_diag.get("configuration_errors"): st.error(day_diag["configuration_errors"])
    else:
        st.info("Дневной приход не обязателен для V1 benchmark.")
        operational_date = st.date_input("Операционный день", key="benchmark_day_without_receipts")
    end_state, end_diag, end_hash, end_sheet = _excel_upload(
        "END snapshot — необязательно, только валидация", "experiment_end", get_actual_inventory_sheet_names,
        read_actual_inventory_table, detect_actual_inventory_columns, build_actual_inventory_placement_state, model)
    for title, diag in (("START", start_diag), ("END", end_diag)):
        if diag:
            st.caption(f"{title}: исходных {diag.get('rows_total', 0)}, принято {diag.get('accepted_rows', 0)} строк / "
                       f"{diag.get('accepted_boxes', 0)} коробов; SKU {diag.get('accepted_sku_count', 0)}; "
                       f"ячеек {diag.get('accepted_cell_count', 0)}; unmatched {diag.get('unmatched_rows', 0)}; excluded {diag.get('excluded_rows', 0)}")

    inventory_state, _, inventory_hash, inventory_sheet = _excel_upload(
        "Инвентаризация / независимый контроль количества — необязательно", "experiment_inventory", get_inventory_results_sheet_names,
        read_inventory_results_table, detect_inventory_results_columns, build_inventory_results_import)
    inventory_keys: list[str] = []
    opening_rows: list[dict[str, Any]] | None = None
    if inventory_state:
        docs = [d for d in inventory_state.get("documents", []) if not selected_warehouse or d.get("warehouse") == selected_warehouse]
        options = [d["document_key"] for d in docs]
        inventory_keys = st.multiselect("Документ инвентаризации", options, default=options if len(options) == 1 else [],
                                        key="experiment_inventory_documents")
        if inventory_keys and selected_warehouse:
            selection, selection_diag = select_inventory_rows_for_opening_stock(
                inventory_state, selected_document_keys=inventory_keys, included_warehouses=[selected_warehouse])
            opening_rows = selection.get("inventory_rows", [])
            st.caption(f"Выбрано строк остатков: {selection_diag.get('selected_inventory_rows', 0)}")

    loaded_receipts_result = load_receipts_state_cached(model)
    loaded_receipts = loaded_receipts_result[0] if isinstance(loaded_receipts_result, tuple) else loaded_receipts_result
    loaded_receipts = loaded_receipts if isinstance(loaded_receipts, Mapping) else {}
    loaded_classifications = loaded_receipts.get("receipts", [])
    loaded_orders = load_outbound_orders_cached(model)
    application_rows = loaded_orders.get("rows", []) if isinstance(loaded_orders, dict) else []
    sources = ["Загруженные в приложении", "Отдельный файл"]
    source = st.radio("Источник РО", sources, index=0 if application_rows else 1, horizontal=True)
    outbound_hash, outbound_sheet = "application", ""
    outbound_rows = application_rows
    if source == "Отдельный файл":
        uploaded = st.file_uploader("Расходные ордера", type=["xlsx", "xls"], key="experiment_outbound")
        outbound_rows = []
        if uploaded:
            data = uploaded.getvalue(); outbound_hash = _hash_bytes(data)
            outbound_sheet = st.selectbox("Лист — расходные ордера", get_outbound_sheet_names(data))
            table = read_outbound_table(data, outbound_sheet)
            outbound_rows, outbound_diag = normalize_outbound_table(table, detect_outbound_columns(table))
            if outbound_diag: st.json(outbound_diag)

    scope_errors = validate_outbound_scope(outbound_rows, selected_warehouse, operational_date)
    for error in scope_errors:
        st.error(error)

    classifications = {r.get("sku_key"): r.get("calculated_zone") for r in loaded_receipts.get("receipts", [])
                       if r.get("calculated_zone") in CODE_TO_ZONE}
    editor_rows = [{"SKU": b.get("sku_key"), "Номенклатура": b.get("nomenclature"),
                    "Характеристика": b.get("characteristic"), "Коробов прихода": b.get("qty_units"),
                    "Весовая зона": CODE_TO_ZONE.get(classifications.get(b.get("sku_key")), "Не задано"),
                    "Приоритет": None} for b in day_state.get("receipt_sku_batches", [])]
    edited = st.data_editor(pd.DataFrame(editor_rows), disabled=["SKU", "Номенклатура", "Характеристика", "Коробов прихода"],
                            column_config={"Весовая зона": st.column_config.SelectboxColumn(options=[*ZONE_TO_CODE, "Не задано"])},
                            use_container_width=True, key="experiment_slotting") if editor_rows else pd.DataFrame(editor_rows)
    slotting_rows = []
    for row in edited.to_dict("records"):
        zone = ZONE_TO_CODE.get(row.get("Весовая зона"))
        if zone:
            original = classifications.get(row.get("SKU"))
            priority = row.get("Приоритет")
            slotting_rows.append({"sku_key": row.get("SKU"), "weight_zone": zone,
                                  "priority_rank": None if pd.isna(priority) else int(priority),
                                  "source": "loaded_receipt_classification" if original == zone else "experiment_ui_manual"})

    roads = model.get("roads", []) or []
    road_options = list(range(len(roads)))
    road_index = st.selectbox("Дорога/проезд для ворот", road_options,
                              format_func=lambda i: f"{roads[i].get('road_type')} · {roads[i].get('road_id', i)} · "
                              f"x=[{roads[i].get('x_min')}, {roads[i].get('x_max')}], y=[{roads[i].get('y_min')}, {roads[i].get('y_max')}]" ) if roads else None
    gate_config: dict[str, Any] = {}
    gate_confirmed = False
    if road_index is not None:
        road = roads[road_index]
        x = st.number_input("X ворот", value=float(road.get("x_min", 0) + road.get("x_max", 0)) / 2)
        y = st.number_input("Y ворот", value=float(road.get("y_min", 0) + road.get("y_max", 0)) / 2)
        gate_confirmed = st.checkbox("Использовать эти координаты ворот")
        gate_config = {"gate_key": "experiment_gate", "gate_name": "Ворота эксперимента",
                       "road_type": road.get("road_type"), "x": float(x), "y": float(y)}

    render_scenario_comparison(
        model, operational_date=operational_date, selected_warehouse=selected_warehouse,
        start_state=start_state, opening_rows=opening_rows,
        classification_rows=loaded_classifications, outbound_rows=outbound_rows,
        gate_state={"model_id": model.get("model_id"), "gates": [gate_config]} if gate_confirmed else None,
        end_snapshot=end_state,
        inventory_control_supplied=inventory_state is not None,
    )
