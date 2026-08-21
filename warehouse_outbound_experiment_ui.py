"""Read-only Streamlit UI for the CURRENT versus PROPOSED experiment."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
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
from warehouse_ui_messages import get_ui_message


ZONE_TO_CODE = {get_placement_zone_label(zone): zone for zone in get_assignable_placement_zones()}
CODE_TO_ZONE = {value: key for key, value in ZONE_TO_CODE.items()}
SESSION_KEYS = (
    "outbound_experiment_input_state", "outbound_experiment_input_diagnostics",
    "outbound_experiment_state", "outbound_experiment_diagnostics",
    "outbound_experiment_ui_signature",
)


def factual_outbound_revision(registry: Mapping[str, Any]) -> str:
    """Stable cache identity for active factual outbound source versions."""
    values = sorted((str(item.get("dataset_id")), str(item.get("version") or item.get("content_hash")))
        for item in registry.get("datasets", []) if item.get("active", True) and item.get("source_type") == "outbound")
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@st.cache_data(show_spinner=False)
def _cached_factual_outbound_history(operational_date: str, warehouse: str, source_revision: str,
                                     registry_json: str) -> dict[str, Any]:
    del source_revision
    from warehouse_factual_scenario_inputs import load_outbound_history
    return load_outbound_history(operational_date, warehouse, registry=json.loads(registry_json))


def load_velocity_history_if_enabled(rule_config: Mapping[str, Any] | None, operational_date: str,
                                     warehouse: str, registry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Avoid all history I/O unless velocity is enabled; cache by source revision."""
    if not bool((rule_config or {}).get("velocity", {}).get("enabled")):
        return None
    return _cached_factual_outbound_history(operational_date, warehouse, factual_outbound_revision(registry),
        json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))


def velocity_history_gate(result: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Never substitute day-D demand when enabled history is non-authoritative."""
    if result is None: return None, []
    blockers = list(result.get("blockers", []))
    if result.get("authoritative") is not True and not blockers:
        blockers.append({"code": "velocity_history_not_authoritative"})
    return (list(result.get("rows", [])) if not blockers else []), blockers


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


def available_operational_dates(outbound_rows: Any, warehouse: Any) -> list[str]:
    """V1 dates come only from accepted outbound demand, never optional receipts."""
    target = normalize_warehouse(warehouse)
    return sorted({str(row.get("created_at"))[:10] for row in outbound_rows or []
                   if row.get("created_at") and normalize_warehouse(row.get("warehouse")) == target})


def configured_gate_state(model: Mapping[str, Any], selected_gate_key: Any = None) -> dict[str, Any] | None:
    """Select only a persistent model gate; never manufacture experiment coordinates."""
    gates = [dict(gate) for gate in model.get("gates", []) or []
             if gate.get("gate_key") and gate.get("x") is not None and gate.get("y") is not None]
    if not gates:
        return None
    selected = next((gate for gate in gates if str(gate.get("gate_key")) == str(selected_gate_key)), None)
    if selected is None and len(gates) == 1:
        selected = gates[0]
    return {"model_id": model.get("model_id"), "gates": [selected]} if selected else None


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
    """Render CURRENT/PROPOSED with factual inputs as the default authority."""
    from warehouse_factual_data import ensure_compact_scope_indexes, load_registry
    from warehouse_factual_scenario_inputs import (
        available_operational_dates as factual_dates, available_warehouses as factual_warehouses,
        build_scenario_weight_classifications, build_start_state, load_inventory_for_day,
        load_receipts_for_day, load_routed_outbound_for_day,
    )

    st.subheader("Авторитетный CURRENT vs PROPOSED")
    st.caption("V1: начальный остаток → расходные ордера; приход внутри дня не моделируется. END — только независимая валидация.")
    st.caption("Дневной приход — не используется в V1 benchmark; END snapshot — необязательно, только валидация.")
    st.caption("Инвентаризация / независимый контроль количества — необязательно.")
    with st.spinner("Однократная подготовка индекса складов factual outbound…"):
        registry = ensure_compact_scope_indexes(load_registry(), source_types=("outbound",))
    warehouses = factual_warehouses(registry=registry)
    source = st.radio("Источник сценария", ["Factual Data Layer", "Ручной fallback"], horizontal=True,
                      help="Ручной режим никогда не включается автоматически при ошибке factual-источника.")
    start_state = end_state = inventory_state = receipt_state = None
    opening_rows = None; day_state: dict[str, Any] = {}; loaded_classifications: list[dict[str, Any]] = []
    start_hash = receipt_hash = inventory_hash = outbound_hash = ""
    start_sheet = receipt_sheet = inventory_sheet = outbound_sheet = ""
    selected_warehouse = operational_date = None; outbound_rows: list[dict[str, Any]] = []
    source_blockers: list[Any] = []; velocity_rows = None; velocity_blockers: list[dict[str, Any]] = []

    if source == "Factual Data Layer":
        st.success("Источник данных: ✅ Factual Data Layer")
        if not warehouses:
            st.error("Factual outbound не содержит доступного склада. Ручной fallback можно выбрать явно выше.")
        else:
            selected_warehouse = st.selectbox("Склад", warehouses, key="experiment_factual_warehouse")
            dates = factual_dates(warehouse=selected_warehouse, registry=registry)
            operational_date = st.selectbox("Операционный день", dates, index=None,
                placeholder="Дни с historical placement и outbound", key="experiment_day") if dates else None
            if not dates:
                st.error("Нет дня, где одновременно доступны factual historical placement и outbound для выбранного склада.")
        if operational_date and selected_warehouse:
            binding_confirmed = st.checkbox(
                f"Подтверждаю: historical placement относится к складу «{selected_warehouse}»",
                key="experiment_historical_warehouse_binding")
            start = build_start_state(operational_date, selected_warehouse, model,
                                      warehouse_binding=selected_warehouse if binding_confirmed else None,
                                      registry=registry)
            outbound = load_routed_outbound_for_day(operational_date, selected_warehouse, model,
                warehouse_binding=selected_warehouse if binding_confirmed else None, registry=registry)
            receipts = load_receipts_for_day(operational_date, selected_warehouse, registry=registry)
            inventory = load_inventory_for_day(operational_date, selected_warehouse, registry=registry)
            relevant = [*start.get("rows", []), *outbound.get("rows", [])]
            rule_config = st.session_state.get("workspace_rule_config") or {}
            weight_zones_enabled = bool(rule_config.get("weight_zones", {}).get("enabled"))
            classifications = (build_scenario_weight_classifications(relevant, registry=registry)
                if weight_zones_enabled else {"rows": [], "blockers": [], "authoritative": True,
                                              "diagnostics": {"status": "weight_zones_disabled"}})
            velocity = load_velocity_history_if_enabled(
                st.session_state.get("workspace_rule_config"), operational_date, selected_warehouse, registry)
            tomorrow = (date.fromisoformat(str(operational_date)) + timedelta(days=1)).isoformat()
            end = build_start_state(tomorrow, selected_warehouse, model,
                warehouse_binding=selected_warehouse if binding_confirmed else None, registry=registry)
            velocity_rows, velocity_blockers = velocity_history_gate(velocity)
            source_blockers = [*start["blockers"], *outbound["blockers"], *classifications["blockers"],
                               *velocity_blockers]
            start_state = start["state"] if start["authoritative"] else None
            outbound_rows = outbound["rows"] if outbound["authoritative"] else []
            receipt_state = receipts.get("state") if receipts["authoritative"] else None
            opening_rows = inventory["rows"] if inventory["authoritative"] and inventory["rows"] else None
            inventory_state = inventory if opening_rows else None
            loaded_classifications = classifications["rows"] if classifications["authoritative"] else []
            end_state = end["state"] if end["authoritative"] and end["rows"] else None
            if receipt_state:
                day_state, _ = build_day_receipt_scenario_inputs(
                    receipt_state, operational_date=operational_date, selected_warehouses=[selected_warehouse])
            st.caption(f"START: historical placement · {operational_date} · {len(start.get('rows', []))} строк")
            st.caption(f"РО: factual Data Layer · parser factual-july-v5 · {len(outbound_rows)} строк")
            st.caption(f"Приходы: factual Data Layer · {len(receipts['rows'])} строк; Inventory: diagnostic evidence, не box-control")
            st.caption(f"Весовые зоны: scenario SKU + factual VGH + сохранённые правила · {len(loaded_classifications)} строк")
            st.caption(f"END: historical placement D+1 · {'доступен' if end_state else 'необязательный snapshot отсутствует'}")
            with st.expander("Технические сведения об источниках"):
                st.json({"start": {"blockers": start["blockers"], "duplicates": start["duplicates"]},
                         "outbound": {"blockers": outbound["blockers"], "duplicates": outbound["duplicates"]},
                         "receipts": {"blockers": receipts["blockers"]}, "inventory": {"blockers": inventory["blockers"]}})
    else:
        velocity_rows = None
        st.warning("Источник данных: ⚠ Ручной fallback. Он выбран пользователем явно.")
        start_state, start_diag, start_hash, start_sheet = _excel_upload(
            "START — отдельный Excel", "experiment_start", get_actual_inventory_sheet_names,
            read_actual_inventory_table, detect_actual_inventory_columns, build_actual_inventory_placement_state, model)
        warehouses = start_warehouses(start_state)
        selected_warehouse = (warehouses[0] if len(warehouses) == 1 else
            st.selectbox("Склад START", warehouses, index=None, key="experiment_start_warehouse") if warehouses else None)
        loaded_orders = load_outbound_orders_cached(model)
        legacy_rows = loaded_orders.get("rows", []) if isinstance(loaded_orders, dict) else []
        outbound_source = st.radio("Ручной источник РО", ["Legacy outbound_orders.json", "Отдельный Excel"], horizontal=True)
        outbound_rows = legacy_rows if outbound_source.startswith("Legacy") else []
        if outbound_source == "Отдельный Excel":
            uploaded = st.file_uploader("Расходные ордера", type=["xlsx", "xls"], key="experiment_outbound")
            if uploaded:
                data = uploaded.getvalue(); outbound_hash = _hash_bytes(data)
                outbound_sheet = st.selectbox("Лист — расходные ордера", get_outbound_sheet_names(data))
                table = read_outbound_table(data, outbound_sheet)
                outbound_rows, diagnostics = normalize_outbound_table(table, detect_outbound_columns(table))
                if diagnostics: st.json(diagnostics)
        dates = available_operational_dates(outbound_rows, selected_warehouse)
        operational_date = st.selectbox("Операционный день", dates, index=None, key="experiment_day") if dates else None
        loaded = load_receipts_state_cached(model)
        loaded_receipts = loaded[0] if isinstance(loaded, tuple) else loaded
        loaded_classifications = loaded_receipts.get("receipts", []) if isinstance(loaded_receipts, Mapping) else []
        inventory_state, _, inventory_hash, inventory_sheet = _excel_upload(
            "Инвентаризация / независимый контроль количества — необязательно", "experiment_inventory",
            get_inventory_results_sheet_names, read_inventory_results_table,
            detect_inventory_results_columns, build_inventory_results_import)
        if inventory_state and selected_warehouse:
            documents = [item for item in inventory_state.get("documents", [])
                         if normalize_warehouse(item.get("warehouse")) == normalize_warehouse(selected_warehouse)]
            keys = [item["document_key"] for item in documents]
            selected_keys = st.multiselect("Документ инвентаризации", keys,
                                           default=keys if len(keys) == 1 else [])
            if selected_keys:
                selection, _ = select_inventory_rows_for_opening_stock(
                    inventory_state, selected_document_keys=selected_keys,
                    included_warehouses=[selected_warehouse])
                opening_rows = selection.get("inventory_rows", [])
        end_state, _, _, _ = _excel_upload("END snapshot — необязательно", "experiment_end", get_actual_inventory_sheet_names,
            read_actual_inventory_table, detect_actual_inventory_columns, build_actual_inventory_placement_state, model)

    scope_errors = ([str(item.get("code", item)) for item in source_blockers] if source_blockers else
                    validate_outbound_scope(outbound_rows, selected_warehouse, operational_date))
    for error in scope_errors:
        issue = get_ui_message(error)
        st.error(f"{issue['title']}\n\n{issue['message']}\n\nЧто сделать: {issue['solution']}")
        with st.expander("Технический код"): st.code(issue["technical_code"])

    persistent_gate_state = st.session_state.get("workspace_gate_state") or {}
    if persistent_gate_state.get("model_id") != model.get("model_id"):
        persistent_gate_state = {"model_id": model.get("model_id"), "gates": model.get("gates", []) or []}
    gate_options = [gate for gate in persistent_gate_state.get("gates", []) or [] if gate.get("gate_key")]
    selected_gate_key = (st.selectbox("Ворота начала/возврата", [g["gate_key"] for g in gate_options], index=None)
                         if len(gate_options) > 1 else gate_options[0]["gate_key"] if gate_options else None)
    gate_state = configured_gate_state({**model, "gates": gate_options}, selected_gate_key)
    if gate_state is None: st.warning("Сначала настройте ворота в Склад → Ворота.")
    st.session_state["outbound_selected_warehouse"] = selected_warehouse
    st.session_state["outbound_selected_date"] = operational_date
    st.session_state["placement_comparison_gate_state"] = gate_state
    st.session_state["outbound_mandatory_data_checks_passed"] = not scope_errors and bool(start_state)
    st.session_state["placement_comparison_benchmark_prerequisites_ready"] = bool(gate_state and not scope_errors)
    render_scenario_comparison(model, operational_date=operational_date, selected_warehouse=selected_warehouse,
        start_state=start_state, opening_rows=opening_rows, classification_rows=loaded_classifications,
        outbound_rows=outbound_rows, gate_state=gate_state, end_snapshot=end_state,
        inventory_control_supplied=inventory_state is not None, rule_config=st.session_state.get("workspace_rule_config"),
        velocity_rows=velocity_rows, velocity_history_blockers=velocity_blockers if source == "Factual Data Layer" else None)
