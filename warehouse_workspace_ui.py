"""Operational five-tab workspace presentation.

The helpers in this module are deliberately pure where possible.  Rendering a
workspace tab never runs an optimizer or replay: those operations remain behind
the explicit buttons in :mod:`warehouse_scenario_comparison_ui`.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Callable

import pandas as pd
import streamlit as st

from warehouse_placement_zones import PLACEMENT_ZONE_IDS, get_placement_zone_label, is_assignable_placement_zone, normalize_placement_zone
from warehouse_scenario_comparison_ui import build_scenario_rule_config
from warehouse_ui_messages import get_ui_message
from warehouse_workflow_ui_state import state_from_session

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


def render_cached_analytics(session_state: Mapping[str, Any]) -> None:
    """Render only cached authoritative benchmark output; never recalculate it."""
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
