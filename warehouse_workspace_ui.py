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

from warehouse_placement_zones import PLACEMENT_ZONE_IDS, get_placement_zone_label, normalize_placement_zone
from warehouse_scenario_comparison_ui import build_scenario_rule_config

WORKSPACE_TABS = ("Склад", "Данные", "Условия модели", "CURRENT / PROPOSED", "Аналитика")
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
LIMITATION_LABELS = {
    "intraday_receipts_not_modeled": "Приходы внутри дня не моделируются.",
    "intermediate_full_picking_pallet_return_not_modeled": "Возврат полного паллета комплектации между РО не моделируется.",
    "dynamic_passage_opening_not_modeled": "Динамическое открытие проходов не моделируется.",
    "deep_lane_internal_access_distance_not_modeled": "Внутренний пробег в набивном ряду не моделируется.",
    "replenishment_distance_loaded_one_way_only": "Пробег пополнения учитывается только в загруженном направлении.",
}


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
    )
    config["base_sku_capacity"]["parameters"]["minimum_positions_per_sku"] = max(1, int(minimum_positions_per_sku))
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
                                 comparison_renderer: Callable, analytics_renderer: Callable) -> None:
    tabs = st.tabs(list(WORKSPACE_TABS))
    for tab, renderer in zip(tabs, (warehouse_renderer, data_renderer, rules_renderer,
                                    comparison_renderer, analytics_renderer)):
        with tab:
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
    minimum = st.number_input("minimum_positions_per_sku", min_value=1, value=1, step=1,
                              disabled=not values["base_sku_capacity"])
    config = build_workspace_rule_config(values, minimum)
    # Persist configuration only; rendering never starts a scenario.
    session_state["workspace_rule_config"] = config
    with st.expander("Как работают правила"):
        st.write("Оборачиваемость использует закреплённые в warehouse_sku_velocity определения окон 28/14/7/4.")
        st.write(RULE_CARDS["adjacency"][1])
        st.caption("Весовые диапазоны здесь не дублируются: используются только подтверждённые классификации источника.")
    with st.expander("Ещё не реализовано"):
        st.write(" · ".join(UNSUPPORTED_RULES))


def render_cached_analytics(session_state: Mapping[str, Any]) -> None:
    """Render only cached authoritative benchmark output; never recalculate it."""
    st.subheader("Аналитика CURRENT / PROPOSED")
    comparison = session_state.get("placement_comparison_distance_comparison")
    if not comparison:
        st.info("Рассчитайте пробег CURRENT / PROPOSED в одноимённом разделе.")
    elif session_state.get("workspace_benchmark_stale"):
        st.warning("Результат пробега устарел — пересчитайте.")
    elif comparison.get("full_day_effect_valid") is not True:
        st.warning("Эффект полного дня не рассчитан")
        st.write(" · ".join(comparison.get("blockers") or comparison.get("limitations") or ["Сервис CURRENT и PROPOSED не эквивалентен."]))
    else:
        summary = comparison.get("authoritative_summary", {})
        keys = (("CURRENT, м", "current_picker_distance_m"), ("PROPOSED, м", "proposed_picker_distance_m"),
                ("Экономия, м", "distance_saved_m"), ("Экономия, %", "distance_saved_percent"),
                ("РО", "accepted_orders"), ("Коробов собрано", "picked_boxes"), ("Shortage", "shortage_boxes"))
        for column, (label, key) in zip(st.columns(len(keys)), keys): column.metric(label, summary.get(key, "—"))
    if comparison:
        orders = comparison.get("orders") or comparison.get("order_comparisons") or []
        if orders: st.dataframe(pd.DataFrame(orders), use_container_width=True, hide_index=True)
    st.subheader("Ограничения текущего расчёта")
    for text in LIMITATION_LABELS.values(): st.write(f"• {text}")
    with st.expander("Технические IDs и диагностика"):
        st.json({"limitations": list(LIMITATION_LABELS), "comparison": comparison or {}})
