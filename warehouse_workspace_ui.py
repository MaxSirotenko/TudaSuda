"""Operational five-tab workspace presentation.

The helpers in this module are deliberately pure where possible.  Rendering a
workspace tab never runs an optimizer or replay: those operations remain behind
the explicit buttons in :mod:`warehouse_scenario_comparison_ui`.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import streamlit as st

from warehouse_placement_zones import PLACEMENT_ZONE_IDS, get_placement_zone_label, is_assignable_placement_zone, normalize_placement_zone
from warehouse_scenario_comparison_ui import build_scenario_rule_config
from warehouse_ui_messages import get_ui_message
from warehouse_workflow_ui_state import state_from_session
from warehouse_factual_data import (
    SOURCE_LABELS, activate_dataset_version, active_datasets, build_monthly_data_readiness,
    cross_source_coverage, date_summary, import_excel_dataset, load_effective_placement, load_registry,
    save_historical_cell_mapping,
)

WORKSPACE_TABS = ("Склад", "Данные", "Условия модели", "CURRENT / PROPOSED", "Пробег", "Аналитика")
SUPPORTED_RULES = (
    "weight_zones", "velocity", "adjacency", "picking_storage", "replenishment",
    "deep_lane_optimization", "base_sku_capacity",
)
UNSUPPORTED_RULES = ("reserve_capacity", "demand_forecast", "receipt_forecast", "demand_spikes", "sku_exceptions")
RULE_CARDS = {
    "weight_zones": ("Весовые зоны", "SKU размещается только в разрешённой весовой зоне склада."),
    "velocity": ("Оборачиваемость", "Более частые SKU получают более выгодные доступные позиции внутри допустимой зоны."),
    "adjacency": ("Товарное соседство", "Разная номенклатура с одинаковой непустой характеристикой не размещается в соседних ячейках."),
    "picking_storage": ("Комплектация / хранение", "Для SKU выделяется позиция комплектации; остальной поддерживаемый запас может использовать хранение."),
    "replenishment": ("Пополнение", "При опустошении позиции комплектации моделируется поддерживаемое пополнение из хранения."),
    "deep_lane_optimization": ("Deep lane", "Поддерживаемый складской запас может использовать набивные ряды с сохранением one-SKU-per-lane и существующего depth contract."),
    "base_sku_capacity": ("Минимальная ёмкость SKU", "Для активного SKU резервируется минимум одна normal-позиция при доступной ёмкости."),
}


def build_weight_zone_readiness(receipts: list[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Report receipt zone coverage without participating in V1 readiness."""
    zones_by_sku: dict[str, set[str]] = defaultdict(set)
    for row in receipts or []:
        sku = str(row.get("sku_key") or "").strip()
        if not sku:
            continue
        zone = normalize_placement_zone(row.get("calculated_zone"))
        if is_assignable_placement_zone(zone):
            zones_by_sku[sku].add(zone)
        else:
            zones_by_sku.setdefault(sku, set())
    total = len(zones_by_sku)
    confirmed = sum(1 for zones in zones_by_sku.values() if len(zones) == 1)
    unresolved = total - confirmed
    return {
        "status": "ready" if total and not unresolved else "partial" if confirmed else "unresolved",
        "total_sku": total,
        "confirmed_sku": confirmed,
        "unresolved_sku": unresolved,
        "coverage_percent": round(100 * confirmed / total, 1) if total else 0.0,
    }
LIMITATION_LABELS = {
    "intraday_receipts_not_modeled": "Приходы внутри дня не моделируются.",
    "intermediate_full_picking_pallet_return_not_modeled": "Возврат полного паллета комплектации между РО не моделируется.",
    "dynamic_passage_opening_not_modeled": "Динамическое открытие проходов не моделируется.",
    "deep_lane_internal_access_distance_not_modeled": "Внутренний пробег в набивном ряду не моделируется.",
    "replenishment_distance_loaded_one_way_only": "Пробег пополнения учитывается только в загруженном направлении.",
}

STEP_CONTEXT = {
    "Склад": ("Настраиваем физическую схему, ряды, зоны и Ворота.", "Геометрия определяет доступные места и физический маршрут.", "Сохранённая модель склада для следующих шагов."),
    "Данные": ("Загружаем фактический START и расходные РО выбранного дня.", "START станет неизменяемым CURRENT, а РО — одинаковым спросом для сравнения.", "Подтверждённое исходное размещение и фактический ПорядокСборки."),
    "Условия модели": ("Выбираем правила, по которым проект перестроит размещение товара.", "Правила формируют PROPOSED, не изменяя фактический CURRENT.", "Новая раскладка тех же исходных остатков."),
    "CURRENT / PROPOSED": ("Строим PROPOSED и повторяем одинаковые РО на двух размещениях.", "Так сравнивается пробег сборщика при одинаковом спросе.", "Две карты, CURRENT и PROPOSED метры и экономия."),
    "Пробег": ("Рассчитываем одинаковые расходные РО для CURRENT и PROPOSED.", "Используются выбранный день, спрос и ворота из предыдущих шагов.", "Авторитетный пробег и маршруты выбранного РО."),
    "Аналитика": ("Изучаем текущий результат сравнения.", "Метрики помогают оценить эффект без подмены фактического CURRENT.", "Сводка пробега, качества и ограничений расчёта."),
}


def deep_lane_edit_issue(row_type: str, width: Any, access_side: Any) -> dict[str, str] | None:
    """Explain an incompatible row draft instead of silently normalising it."""
    if row_type == "normal" and (float(width or 1) != 1 or str(access_side or "") not in {"", "Не настроено"}):
        return get_ui_message("deep_width_on_normal_row" if float(width or 1) != 1 else "deep_access_on_normal_row")
    return None


def render_context_block(step: str) -> None:
    doing, why, result = STEP_CONTEXT[step]
    st.markdown(f'<div class="workflow-context"><b>Что делаем</b><br>{doing}<br><b>Зачем</b><br>{why}<br><b>Что получится</b><br>{result}</div>', unsafe_allow_html=True)


def render_workflow_stepper(model: Mapping[str, Any] | None, session_state: Mapping[str, Any]) -> None:
    state = state_from_session(model, session_state)
    symbols = {"completed": "✓", "current": "●", "available": "○", "blocked": "—", "stale": "↻"}
    items = "".join(f'<span class="workflow-step {item["status"]}">{item["number"]}. {item["name"]} {symbols[item["status"]]}</span>' for item in state["steps"])
    st.markdown(f'<div class="workflow-stepper">{items}</div>', unsafe_allow_html=True)


def normalize_rule_selection(values: Mapping[str, Any]) -> dict[str, bool]:
    """Return a valid, order-independent selection; replenishment is blocked."""
    selected = {name: bool(values.get(name, False)) for name in SUPPORTED_RULES}
    selected["replenishment"] = selected["replenishment"] and selected["picking_storage"]
    return selected


def build_workspace_rule_config(values: Mapping[str, Any], minimum_positions_per_sku: int = 1) -> dict[str, Any]:
    selected = normalize_rule_selection(values)
    config = build_scenario_rule_config(
        weight_zones_enabled=selected["weight_zones"], velocity_enabled=selected["velocity"],
        adjacency_enabled=selected["adjacency"], picking_storage_enabled=selected["picking_storage"],
        replenishment_enabled=selected["replenishment"],
        deep_lane_optimization_enabled=selected["deep_lane_optimization"],
        base_sku_capacity_enabled=selected["base_sku_capacity"],
        minimum_positions_per_sku=minimum_positions_per_sku,
    )
    return config


def build_warehouse_zone_summary(model: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Summarise actual cells using only canonical warehouse placement zones."""
    totals = defaultdict(lambda: {"rows": set(), "cells": 0, "physical": 0, "normal": 0, "deep": 0})
    for cell in model.get("cells", []) or []:
        zone = normalize_placement_zone(cell.get("weight_zone"))
        zone = zone if zone in PLACEMENT_ZONE_IDS else "unassigned"
        capacity = int(cell.get("capacity_pallets") or 1)
        item = totals[zone]
        item["rows"].add(str(cell.get("row_number") or "—")); item["cells"] += 1; item["physical"] += capacity
        item["deep" if cell.get("storage_type") == "deep_lane" else "normal"] += capacity
    return [{"Зона": get_placement_zone_label(zone), "ID зоны": zone,
             "Ряды": ", ".join(sorted(totals[zone]["rows"])),
             "Количество ячеек": totals[zone]["cells"],
             "Физическая вместимость": totals[zone]["physical"],
             "Normal": totals[zone]["normal"], "Deep lane": totals[zone]["deep"]}
            for zone in PLACEMENT_ZONE_IDS if totals[zone]["cells"]]


def render_operational_workspace(model: dict | None, *, warehouse_renderer: Callable,
                                 data_renderer: Callable, rules_renderer: Callable,
                                 comparison_renderer: Callable, distance_renderer: Callable,
                                 analytics_renderer: Callable) -> None:
    st.markdown("""<style>
    .stApp {background:#f6f7f9}.workflow-stepper{display:flex;gap:.45rem;flex-wrap:wrap;background:white;border:1px solid #e5e7eb;border-radius:10px;padding:.65rem .8rem;margin-bottom:.8rem}
    .workflow-step{color:#64748b;padding:.15rem .35rem}.workflow-step.completed{color:#397354}.workflow-step.current{color:#1d4ed8;font-weight:650}.workflow-step.stale{color:#a16207}
    .workflow-context{background:white;border:1px solid #e5e7eb;border-left:3px solid #94a3b8;border-radius:8px;padding:.7rem .9rem;line-height:1.45;margin:.25rem 0 1rem}
    </style>""", unsafe_allow_html=True)
    render_workflow_stepper(model, st.session_state)
    tabs = st.tabs(list(WORKSPACE_TABS))
    for tab, name, renderer in zip(tabs, WORKSPACE_TABS, (warehouse_renderer, data_renderer, rules_renderer,
                                          comparison_renderer, distance_renderer, analytics_renderer)):
        with tab:
            render_context_block(name)
            renderer(model)


def render_rules_control_panel(model: Mapping[str, Any] | None, session_state: Mapping[str, Any]) -> None:
    st.subheader("Условия модели PROPOSED")
    st.caption("CURRENT остаётся неизменным; настройки применяются только после явного пересчёта PROPOSED.")
    values = {}
    for rule in SUPPORTED_RULES:
        title, description = RULE_CARDS[rule]
        disabled = rule == "replenishment" and not values.get("picking_storage", False)
        values[rule] = st.checkbox(title, key=f"workspace_rule_{rule}", disabled=disabled)
        st.caption(description)
        if disabled:
            st.caption("Заблокировано: требуется правило «Комплектация / хранение».")
        else:
            st.caption("Готово" if model else "Нет данных")
        if rule == "weight_zones" and values[rule]:
            receipt_state = session_state.get("receipts_state") or {}
            coverage = build_weight_zone_readiness(receipt_state.get("receipts", []))
            if coverage["status"] == "ready":
                st.success("Весовые зоны: готовы")
            else:
                st.warning("Весовые зоны: частично готовы" if coverage["confirmed_sku"] else "Весовые зоны: не готовы")
            st.caption(f'{coverage["confirmed_sku"]} из {coverage["total_sku"]} SKU имеют подтверждённую зону.')
            if coverage["unresolved_sku"]:
                st.caption(f'{coverage["unresolved_sku"]} SKU остаются без зоны.')
                st.caption('Добавьте подтверждённый вес/зону либо отключите правило "Весовые зоны".')
    minimum = st.number_input("Минимум позиций комплектации на SKU", min_value=1, value=1, step=1,
                              disabled=not values["base_sku_capacity"])
    config = build_workspace_rule_config(values, minimum)
    # Persist configuration only; rendering never starts a scenario.
    session_state["workspace_rule_config"] = config
    session_state["workspace_rule_dependencies_valid"] = not (
        config["replenishment"]["enabled"] and not config["picking_storage"]["enabled"])
    with st.expander("Как работают правила"):
        st.write("Оборачиваемость использует закреплённые в warehouse_sku_velocity определения окон 28/14/7/4.")
        st.write(RULE_CARDS["adjacency"][1])
        st.caption("Весовые диапазоны здесь не дублируются: используются только подтверждённые классификации источника.")
    with st.expander("Ещё не реализовано"):
        st.write(" · ".join(UNSUPPORTED_RULES))


def render_factual_data_layer(model: Mapping[str, Any] | None) -> None:
    """Render the sole monthly factual registry inside the existing Data tab."""
    st.subheader("Фактический Data Layer")
    st.caption("RAW + CANONICAL · реестр по хешу содержимого · исторические данные не изменяют CURRENT V1 автоматически")
    files = st.file_uploader(
        "Добавить фактические Excel", type=["xlsx", "xls"], accept_multiple_files=True,
        key="factual_data_uploads",
    )
    if files and st.button("Импортировать в Data Layer", type="primary", key="factual_data_import"):
        for uploaded in files:
            try:
                result = import_excel_dataset(uploaded.getvalue(), uploaded.name)
            except (OSError, ValueError) as exc:
                st.error(f"{uploaded.name}: файл не импортирован ({exc})")
                continue
            if result.get("source_type") == "unknown":
                family = result.get("detected_source_family")
                if result.get("diagnostic_code") == "outbound_document_identity_missing":
                    st.error(f"{uploaded.name}: Файл похож на расходные ордера, но не прошёл классификацию.")
                    st.caption("Найдены поля: " + ", ".join(result.get("diagnostic_found", [])) + ".")
                    st.caption("Не найдено: " + ", ".join(result.get("diagnostic_missing", [])) + ".")
                else:
                    st.error(f"{uploaded.name}: требуется сопоставление полей" if family else f"{uploaded.name}: Неизвестный тип файла")
                    st.caption("Обнаруженные колонки: " + " · ".join(result.get("detected_columns", [])))
                if result.get("required_missing"):
                    st.caption("Не сопоставлены обязательные поля: " + " · ".join(result["required_missing"]))
            elif result.get("reused"):
                if result.get("reuse_state") == "existing_superseded_version":
                    st.warning(f"{uploaded.name}: Эта версия уже существует, но не активна.")
                else:
                    st.info(f"{uploaded.name}: уже импортирован, использован сохранённый артефакт")
            else:
                st.success(f"{uploaded.name}: {SOURCE_LABELS[result['source_type']]}")
    registry = load_registry()
    datasets = registry.get("datasets", [])
    if not datasets:
        st.info("Фактические месячные наборы ещё не импортированы.")
        return
    active = active_datasets(registry)
    compact = [{
        "Файл": item.get("source_file_name"), "Тип": item.get("source_label"), "Статус": item.get("status"),
        "Версия": str(item.get("version") or item.get("content_hash") or "")[:10], "Активен": "да",
        "Период": " — ".join(filter(None, (item.get("period_from"), item.get("period_to")))) or "—",
        "Строк": item.get("rows", 0), "SKU": item.get("unique_sku", 0),
        "Ошибки": len(item.get("errors", [])), "Предупреждения": len(item.get("warnings", [])),
        "Импортирован": item.get("imported_at"),
    } for item in active]
    st.dataframe(pd.DataFrame(compact), use_container_width=True, hide_index=True)
    superseded = [item for item in datasets if not item.get("active", True)]
    with st.expander("Предыдущие версии / технические детали"):
        if superseded:
            st.dataframe(pd.DataFrame([{"Файл": i.get("source_file_name"), "Тип": i.get("source_label"),
                "Версия": str(i.get("version") or i.get("content_hash") or "")[:10], "Заменён": i.get("superseded_by")} for i in superseded]), hide_index=True)
            version = st.selectbox("Неактивная версия", [i["dataset_id"] for i in superseded], key="factual_inactive_version")
            if st.button("Сделать эту версию активной", key="factual_activate_version"):
                activate_dataset_version(version)
                st.success("Активная версия источника подтверждена.")
    for item in active:
        with st.expander(f"{item.get('source_file_name')} · {item.get('source_label')}"):
            st.json({key: item.get(key) for key in (
                "dataset_id", "content_hash", "parser_version", "sheet", "imported_at", "artifact",
                "detected_columns", "diagnostics",
            )})
    available_days = sorted({day for item in active for day in item.get("partitions", []) if day != "undated"})
    if available_days:
        selected = st.selectbox("Операционный день", available_days, key="factual_operational_day")
        summary = date_summary(registry, selected)
        st.subheader(f"Срез на {selected}")
        st.json(summary)
        with st.expander("Покрытие SKU между источниками"):
            coverage = cross_source_coverage(registry)
            st.dataframe(pd.DataFrame(coverage), use_container_width=True, hide_index=True)
        if model:
            placement = load_effective_placement(selected, model, registry=registry, strict=False)
            unresolved = sorted({str(row.get("source_cell")) for row in placement["rows"]
                                 if row.get("cell_resolution_status") in {"unresolved", "ambiguous", "stale"}})
            statuses = pd.Series([row.get("cell_resolution_status") for row in placement["rows"]]).value_counts()
            st.caption(f"Исторические ячейки: разрешено {int(statuses.get('resolved', 0))}; "
                       f"не разрешено {len(unresolved)}; неоднозначно {int(statuses.get('ambiguous', 0))}.")
            if unresolved:
                query = st.text_input("Поиск исторической ячейки", key="historical_cell_search")
                page = [value for value in unresolved if query.casefold() in value.casefold()][:50]
                source_cell = st.selectbox("Историческая ячейка", page, key="historical_source_cell")
                target_query = st.text_input("Поиск ячейки геометрии", key="geometry_cell_search")
                targets = sorted(str(cell.get("cell_key")) for cell in model.get("cells", []) if cell.get("cell_key"))
                targets = [value for value in targets if target_query.casefold() in value.casefold()][:100]
                if targets:
                    target = st.selectbox("Ячейка геометрии", targets, key="historical_geometry_target")
                    if st.button("Сохранить сопоставление", key="save_historical_cell_mapping"):
                        save_historical_cell_mapping(source_cell, target, model, source_evidence={"day": selected})
                        st.success("Сопоставление сохранено как подтверждённое пользователем.")
            readiness = build_monthly_data_readiness(registry, model, "2026-07-01", "2026-07-31")
            st.subheader("Готовность к месячному replay")
            c = readiness["coverage"]
            st.caption(f"Срезы START: {c['placement_checkpoints']} / {c['placement_checkpoints_required']}")
            st.caption(f"ВГХ: {c['vgh_covered_sku']} / {c['demanded_sku']} востребованных SKU")
            st.caption(f"Конфликты источников: {c['source_conflicts']}")
            if readiness["monthly_replay_ready"]:
                st.success("Готово к месячному replay")
            else:
                st.error("Месячный replay заблокирован")
                st.json(readiness["hard_blockers"])


def render_monthly_fact_baseline(model: Mapping[str, Any] | None, session_state: Mapping[str, Any]) -> None:
    """Run and inspect persisted July FACT partitions without PROPOSED logic."""
    st.subheader("FACT — июльский baseline")
    if not model:
        st.info("Сначала загрузите схему склада."); return
    registry = load_registry()
    readiness = build_monthly_data_readiness(registry, model, "2026-07-01", "2026-07-31")
    if readiness.get("monthly_replay_ready") is not True:
        st.error("Месячный FACT заблокирован"); st.json(readiness.get("hard_blockers", [])); return
    st.success("Фактический Data Layer готов")
    gate_state = session_state.get("workspace_gate_state")
    if not gate_state:
        st.error("Не настроены авторитетные ворота."); return
    if st.button("Рассчитать FACT за июль", key="monthly_fact_run"):
        from warehouse_monthly_fact_replay import replay_monthly_fact
        bar, label = st.progress(0), st.empty()
        def progress(event: dict[str, Any]) -> None:
            label.caption(f"День {event['day_index']} из {event['days_total']} · РО обработано: {event.get('orders_processed', 0)}")
            bar.progress(event["day_index"] / event["days_total"])
        result = replay_monthly_fact(dict(model), dict(gate_state), registry=registry, progress_callback=progress)
        session_state["monthly_fact_summary"] = {k: v for k, v in result.items() if k != "daily_results"}
    summary = session_state.get("monthly_fact_summary")
    if not summary: return
    values = (("Дней", summary.get("days_total")), ("РО", summary.get("orders_total")),
              ("Коробов", summary.get("picked_boxes")), ("Shortage", summary.get("shortage_boxes")),
              ("FACT, км", round((summary.get("strict_fact_picker_distance_m") or 0)/1000, 3)),
              ("Strict coverage", f"{100*(summary.get('route_order_coverage') or 0):.1f}%"))
    for column, (label, value) in zip(st.columns(6), values): column.metric(label, value)
    artifact = Path(str(summary.get("artifact_path") or "")); files = sorted(artifact.glob("day=*.json")) if artifact.is_dir() else []
    if files:
        import json
        daily = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        st.dataframe(pd.DataFrame([{"Дата": d["operational_day"], "РО": d["orders_total"],
            "Запрошено коробов": d["requested_boxes"], "Собрано": d["picked_boxes"], "Shortage": d["shortage_boxes"],
            "FACT, м": d["picker_distance_m"], "strict status": d["status"],
            "ambiguities": d["source_location_ambiguity_count"]} for d in daily]), use_container_width=True, hide_index=True)
        selected_day = st.selectbox("День для детализации РО", [d["operational_day"] for d in daily])
        chosen = next(d for d in daily if d["operational_day"] == selected_day)
        identities = [o["order_identity"].get("document_number") or o["order_identity"].get("document_ref") for o in chosen["order_results"]]
        if identities:
            selected = st.selectbox("Расходный ордер", identities); order = chosen["order_results"][identities.index(selected)]
            st.json({"order_identity": order["order_identity"], "picker_distance_m": order.get("picker_distance_m"),
                     "shortage_boxes": order["shortage_boxes"], "strict_comparable": order["strict_comparable"],
                     "route_legs": order["route_legs"], "factual_pick_stops": order["factual_pick_stops"],
                     "source_location_ambiguity": order["source_location_ambiguous"], "blockers": order["blockers"]})
    st.caption("Приходы и инвентаризации не изменяют START; неоднозначные ячейки не угадываются; Паллета — техническая ссылка; каждый день сбрасывается на D 00:00; PROPOSED и экономия не рассчитываются.")


def authoritative_analytics_metrics(comparison: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the exact benchmark headline contract, or no non-authoritative headline."""
    if comparison.get("full_day_effect_valid") is not True:
        return None
    summary = comparison.get("authoritative_summary") or {}
    keys = ("current_picker_distance_m", "proposed_picker_distance_m", "picker_distance_saved_m",
            "picker_distance_saved_percent", "orders_total", "current_picked_boxes",
            "proposed_picked_boxes", "current_shortage_boxes", "proposed_shortage_boxes",
            "service_equivalent")
    return {key: summary.get(key) for key in keys}


def render_monthly_placement_comparison(comparison: Mapping[str, Any] | None) -> None:
    """Render a persisted monthly artifact; this function never runs replay."""
    st.subheader("FACT vs PROPOSED — июль")
    if not comparison:
        st.info("Сохранённое месячное сравнение ещё не сформировано.")
        return
    readiness = comparison.get("readiness", "partial")
    (st.success if readiness == "ready" else st.warning)(f"Готовность: {readiness}")
    metrics = (("FACT", comparison.get("fact_meters")), ("PROPOSED", comparison.get("proposed_meters")),
               ("Экономия", comparison.get("saved_meters")), ("Экономия, %", comparison.get("saved_percent")))
    for column, (label, value) in zip(st.columns(4), metrics):
        column.metric(label, f"{float(value or 0)/1000:.3f} км" if label != "Экономия, %" else f"{float(value or 0):.2f}%")
    st.caption(f"Сопоставимо {comparison.get('comparable_orders', 0)} / {comparison.get('full_order_count', 0)} РО · "
               f"исключено {comparison.get('excluded_orders', 0)} · coverage FACT/PROPOSED "
               f"{100*float(comparison.get('fact_coverage', 0)):.1f}% / {100*float(comparison.get('proposed_coverage', 0)):.1f}%")
    daily = comparison.get("daily_results") or []
    if daily:
        st.dataframe(pd.DataFrame(daily).rename(columns={"date": "Дата", "ro_count": "РО", "fact_meters": "FACT м",
            "proposed_meters": "PROPOSED м", "saved_meters": "Δ м", "saved_percent": "Δ %",
            "strict_coverage": "Coverage", "warnings": "Warnings"}), use_container_width=True, hide_index=True)
    orders = comparison.get("order_comparisons") or []
    if orders:
        labels = [str(x.get("order_identity", {}).get("document_number") or x.get("order_identity", {}).get("document_ref") or i)
                  for i, x in enumerate(orders)]
        selected = st.selectbox("РО для детализации FACT / PROPOSED", labels, key="monthly_comparison_ro")
        order = orders[labels.index(selected)]
        st.json({"FACT": {"distance": order.get("fact_meters"), "route": order.get("fact_route"),
                          "pick_stops": order.get("fact_pick_stops")},
                 "PROPOSED": {"distance": order.get("proposed_meters"), "route": order.get("proposed_route"),
                              "pick_stops": order.get("proposed_pick_stops")},
                 "changed_skus": order.get("changed_skus"), "warnings": order.get("warnings")})
        graph = comparison.get("route_graph") or {}
        if graph:
            from warehouse_route_ui import build_route_overlay
            st.json({"FACT overlay": build_route_overlay({"route_legs": order.get("fact_route"),
                "pick_events": order.get("fact_pick_stops"), "picker_distance_m": order.get("fact_meters")}, graph, "current"),
                "PROPOSED overlay": build_route_overlay({"route_legs": order.get("proposed_route"),
                "pick_events": order.get("proposed_pick_stops"), "picker_distance_m": order.get("proposed_meters")}, graph, "proposed")})
    if comparison.get("contribution_analysis"):
        st.markdown("**Измеренный вклад по SKU / зоне / ряду**")
        st.dataframe(pd.DataFrame(comparison["contribution_analysis"]), use_container_width=True, hide_index=True)
    if comparison.get("placement_changes"):
        st.markdown("**Изменённые SKU**")
        st.dataframe(pd.DataFrame(comparison["placement_changes"]), use_container_width=True, hide_index=True)


def render_cached_analytics(session_state: Mapping[str, Any], model: Mapping[str, Any] | None = None) -> None:
    """Render only cached authoritative benchmark output; never recalculate it."""
    render_monthly_placement_comparison(session_state.get("monthly_placement_comparison"))
    st.subheader("Аналитика CURRENT / PROPOSED")
    comparison = session_state.get("placement_comparison_distance_comparison")
    if not comparison:
        st.info("Рассчитайте пробег CURRENT / PROPOSED в одноимённом разделе.")
    elif session_state.get("placement_comparison_distance_signature") != session_state.get("placement_comparison_active_distance_signature"):
        st.warning("Результат пробега устарел — пересчитайте.")
    elif comparison.get("full_day_effect_valid") is not True:
        st.warning("Эффект полного дня недоступен")
        st.write(" · ".join(comparison.get("blockers") or comparison.get("limitations") or ["Сервис CURRENT и PROPOSED не эквивалентен."]))
    else:
        summary = authoritative_analytics_metrics(comparison) or {}
        keys = (("CURRENT, м", "current_picker_distance_m"), ("PROPOSED, м", "proposed_picker_distance_m"),
                ("Экономия, м", "picker_distance_saved_m"), ("Экономия, %", "picker_distance_saved_percent"),
                ("РО", "orders_total"), ("Собрано CURRENT", "current_picked_boxes"),
                ("Собрано PROPOSED", "proposed_picked_boxes"), ("Shortage CURRENT", "current_shortage_boxes"),
                ("Shortage PROPOSED", "proposed_shortage_boxes"), ("Сервис эквивалентен", "service_equivalent"))
        for column, (label, key) in zip(st.columns(len(keys)), keys): column.metric(label, summary.get(key, "—"))
    if comparison:
        orders = comparison.get("orders") or comparison.get("order_comparisons") or []
        if orders: st.dataframe(pd.DataFrame(orders), use_container_width=True, hide_index=True)
    st.subheader("Ограничения текущего расчёта")
    for text in LIMITATION_LABELS.values(): st.write(f"• {text}")
    with st.expander("Технические IDs и диагностика"):
        st.json({"limitations": list(LIMITATION_LABELS), "comparison": comparison or {}})
    render_monthly_fact_baseline(model, session_state)
