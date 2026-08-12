"""Operational five-tab workspace presentation.

The helpers in this module are deliberately pure where possible.  Rendering a
workspace tab never runs an optimizer or replay: those operations remain behind
the explicit buttons in :mod:`warehouse_scenario_comparison_ui`.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from html import escape
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import streamlit as st

from warehouse_placement_zones import PLACEMENT_ZONE_IDS, get_placement_zone_label, is_assignable_placement_zone, normalize_placement_zone
from warehouse_scenario_comparison_ui import build_scenario_rule_config
from warehouse_ui_messages import get_ui_message, render_ui_message
from warehouse_workflow_ui_state import state_from_session
from warehouse_factual_data import (
    SOURCE_LABELS, activate_dataset_version, active_datasets, build_monthly_data_readiness,
    cross_source_coverage, date_summary, import_excel_dataset, load_effective_placement, load_registry,
    save_historical_cell_mapping,
)
from warehouse_perf_diagnostics import ENABLED as PERF_ENABLED, measure, snapshot

# The keys used by renderers and session data stay unchanged.  Only these
# customer-facing navigation labels are business terms.
WORKSPACE_TABS = ("Настройка склада", "Загрузка данных", "Правила размещения", "Сравнение вариантов", "Расчёт маршрутов", "Результаты")
LEGACY_WORKSPACE_TABS = dict(zip(
    ("Склад", "Данные", "Условия модели", "Исходное / предлагаемое", "Пробег", "Аналитика"),
    WORKSPACE_TABS,
))
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
    "deep_lane_optimization": ("Набивные ряды", "Поддерживаемый складской запас может использовать набивные ряды: в каждом канале хранится один товар."),
    "base_sku_capacity": ("Минимальная ёмкость товара", "Для активного товара резервируется минимум одно обычное место при доступной ёмкости."),
}

IMPORT_STATUS_LABELS = {
    "ready": "✅ Готово",
    "ready_with_warnings": "⚠️ Готово с ограничениями",
}

STATUS_PRESENTATION = {
    "success": ("✅", "Готово"),
    "warning": ("⚠️", "Есть ограничения"),
    "error": ("❌", "Требуется исправление"),
    "empty": ("⬜", "Не выполнено"),
}


def format_compact_number(value: Any) -> str:
    """Format a UI count consistently without changing the underlying value."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def status_card_html(title: str, value: Any, explanation: str, status: str = "empty") -> str:
    """Return one accessible, presentation-only status card."""
    tone = status if status in STATUS_PRESENTATION else "empty"
    icon, label = STATUS_PRESENTATION[tone]
    return (
        f'<div class="ui-status-card {tone}" role="status">'
        f'<div class="ui-status-heading"><span aria-hidden="true">{icon}</span>'
        f'<span>{escape(str(title))}</span></div>'
        f'<div class="ui-status-value">{escape(format_compact_number(value))}</div>'
        f'<div class="ui-status-label">{label}</div>'
        f'<div class="ui-status-explanation">{escape(str(explanation))}</div></div>'
    )


def render_status_grid(cards: list[Mapping[str, Any]]) -> None:
    """Render compact summaries from values already present in memory."""
    body = "".join(status_card_html(str(card["title"]), card.get("value"),
                                    str(card.get("explanation", "")), str(card.get("status", "empty")))
                   for card in cards)
    st.markdown(f'<div class="ui-status-grid">{body}</div>', unsafe_allow_html=True)


def import_status_label(status: Any) -> str:
    """Translate persisted import statuses without changing their data contract."""
    return IMPORT_STATUS_LABELS.get(str(status or ""), "❌ Требуется исправление")


DATA_SOURCE_CARDS = (
    ("historical_placement", "Историческое размещение", "Показывает, где фактически находился товар на каждый день."),
    ("outbound", "Расходные ордера", "Показывают фактический спрос и последовательность отбора товаров."),
    ("receipts", "Приходные ордера", "Показывают поступления товара в течение выбранного периода."),
    ("inventory", "Инвентаризации", "Позволяют сверить фактические остатки и обнаружить расхождения."),
    ("vgh", "ВГХ / паллетизация", "Задаёт габариты товара и правила укладки на паллету."),
)


def build_data_source_cards(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build the upload screen exclusively from persisted compact metadata."""
    active = active_datasets(registry)
    cards = []
    for source_type, title, purpose in DATA_SOURCE_CARDS:
        sources = [item for item in active if item.get("source_type") == source_type]
        rows = sum(int(item.get("rows", 0) or 0) for item in sources)
        sku = len({key for item in sources for key in item.get("index", {}).get("sku_keys", [])})
        dates = sorted({day for item in sources for day in item.get("index", {}).get("dates", item.get("partitions", [])) if day != "undated"})
        warnings = sum(len(item.get("warnings", [])) for item in sources)
        errors = sum(len(item.get("errors", [])) for item in sources)
        status = "❌ Требуется исправление" if errors else "⚠️ Готово с ограничениями" if warnings else "✅ Готово" if sources else "⬜ Не загружено"
        daily = [counts for item in sources for counts in item.get("index", {}).get("daily", {}).values()]
        cards.append({"source_type": source_type, "title": title, "purpose": purpose, "sources": sources,
                      "file": ", ".join(str(item.get("source_file_name") or "—") for item in sources) or "—",
                      "period": f"{dates[0]} — {dates[-1]}" if dates else "—", "dates": dates,
                      "rows": rows, "sku": sku, "documents": sum(int(x.get("documents", 0) or 0) for x in daily),
                      "cells": sum(int(x.get("cells", 0) or 0) for x in daily), "status": status})
    return cards


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


def format_monthly_readiness_check(check: Mapping[str, Any]) -> str:
    """Format one structured readiness check for the non-technical UI."""
    icon = {"pass": "✅", "fail": "❌", "warning": "⚠️", "info": "ℹ️"}.get(str(check.get("status")), "ℹ️")
    text = f"{icon} **{check.get('title', check.get('name', 'Проверка'))}**  \n{check.get('details', '')}"
    missing = check.get("missing_dates") or []
    if check.get("name") == "placement_snapshot":
        extra = check.get("extra_dates") or []
        text += (f"  \nОжидаемые даты: {check.get('expected_days', 0)}"
                 f"  \nОбнаруженные даты: {len(check.get('detected_dates', [])) or check.get('imported_days', 0)}"
                 f"  \nОтсутствующие даты: {', '.join(map(str, missing)) or 'нет'}"
                 f"  \nЛишние даты: {', '.join(map(str, extra)) or 'нет'}")
    elif missing:
        text += f"  \nНе хватает дат: {', '.join(map(str, missing))}"
    if check.get("name") == "vgh_coverage" and check.get("missing_sku_count"):
        text += f"  \nНе хватает: {check['missing_sku_count']} SKU · покрытие {check.get('percentage', 0)}%"
    return text


READINESS_BLOCKER_PRESENTATION: dict[str, tuple[str, str]] = {
    "registry_activation_review_required": (
        "Не подтверждена активная версия файлов",
        "Откройте историю загрузок и подтвердите версию источника, которая должна использоваться в расчёте.",
    ),
    "missing_placement_snapshot": (
        "Не хватает срезов размещения",
        "Загрузите срезы размещения за указанные даты и повторите проверку.",
    ),
    "multiple_active_placement_sources_for_snapshot": (
        "На одну дату найдено несколько активных срезов",
        "Оставьте одну активную версию размещения для каждой даты.",
    ),
    "conflicting_factual_business_key": (
        "В источнике найдены противоречащие записи",
        "Проверьте активные версии источника и устраните конфликтующие строки.",
    ),
    "outbound_not_available": (
        "Не найдены расходные ордера",
        "Загрузите расходные ордера с положительным спросом за проверяемый месяц.",
    ),
    "receipts_not_available": (
        "Не найдены приходы",
        "Загрузите приходы за проверяемый месяц и повторите проверку.",
    ),
    "historical_cell_unresolved": (
        "Исторические ячейки не сопоставлены с моделью склада",
        "Проверьте указанные адреса в модели: добавьте отсутствующие ячейки или устраните неоднозначность.",
    ),
}

_FACTUAL_SOURCE_LABELS = {
    "historical_placement": "размещение",
    "outbound": "расходные ордера",
    "receipts": "приходы",
    "inventory": "инвентаризация",
    "vgh": "ВГХ",
}


def monthly_readiness_blocker_details(blocker: Mapping[str, Any]) -> dict[str, str]:
    """Translate one hard blocker into an actionable, non-technical explanation."""
    code = str(blocker.get("code") or "unknown")
    title, action = READINESS_BLOCKER_PRESENTATION.get(
        code,
        ("Неизвестная блокировка готовности",
         "Откройте технические сведения и передайте код блокировки разработчику."),
    )
    facts: list[str] = []
    dates = blocker.get("dates") or []
    if dates:
        facts.append(f"Даты: {', '.join(map(str, dates))}.")
    if code == "conflicting_factual_business_key":
        source = _FACTUAL_SOURCE_LABELS.get(str(blocker.get("source_type")), str(blocker.get("source_type") or "источник"))
        facts.append(f"Источник: {source}. Конфликтов: {int(blocker.get('count') or 0)}.")
    elif code == "historical_cell_unresolved":
        unique_count = blocker.get("unique_source_cells")
        occurrences = blocker.get("demand_relevant_cells")
        if isinstance(unique_count, int):
            facts.append(f"Уникальных адресов: {unique_count}.")
        if isinstance(occurrences, int):
            facts.append(f"Повторений адресов по дням: {occurrences}.")
        preview = blocker.get("source_cell_preview") or []
        if preview:
            samples = ", ".join(f"`{str(value).replace('`', '´')}`" for value in preview)
            facts.append(f"Примеры: {samples}.")
    message = str(blocker.get("message") or "").strip()
    if message and not facts:
        facts.append(message)
    if code not in READINESS_BLOCKER_PRESENTATION:
        facts.append(f"Технический код: `{code.replace('`', '´')}`.")
    return {"code": code, "title": title, "details": " ".join(facts), "action": action}


def format_monthly_readiness_blocker(blocker: Mapping[str, Any]) -> str:
    """Format one hard blocker so it can never disappear behind the generic banner."""
    item = monthly_readiness_blocker_details(blocker)
    text = f"❌ **{item['title']}**"
    if item["details"]:
        text += f"  \n{item['details']}"
    text += f"  \nЧто сделать: {item['action']}"
    return text


MONTHLY_ROUTE_REQUIRED_SOURCES = {"historical_placement", "outbound", "receipts"}


def monthly_readiness_message(readiness: Mapping[str, Any]) -> dict[str, str]:
    """Build the readiness banner without treating optional VGH as a route input."""
    if readiness.get("monthly_replay_ready") and readiness.get("vgh_ready") is False:
        return {"severity": "warning", "title": "Данные июля готовы с ограничениями",
            "reason": "ВГХ отсутствует, неполное или содержит конфликтующие записи.",
            "impact": "Можно считать маршруты, ABC, частоту и расстояния. Нельзя считать достоверными весовые правила, тяжёлое/лёгкое и зависящие от ВГХ рекомендации.",
            "action": "Можно перейти к расчёту маршрутов; для функций, зависящих от ВГХ, загрузите полные непротиворечивые данные.",
            "target": "Расчёт маршрутов"}
    return {"severity": "success", "title": "Данные июля готовы",
        "reason": "Все обязательные проверки пройдены.",
        "impact": "Можно перейти к настройке правил размещения.",
        "action": "Перейдите к правилам размещения.", "target": "Правила размещения"}


LIMITATION_LABELS = {
    "intraday_receipts_not_modeled": "Приходы внутри дня не моделируются.",
    "intermediate_full_picking_pallet_return_not_modeled": "Возврат полного паллета комплектации между РО не моделируется.",
    "dynamic_passage_opening_not_modeled": "Динамическое открытие проходов не моделируется.",
    "deep_lane_internal_access_distance_not_modeled": "Внутренний пробег в набивном ряду не моделируется.",
    "replenishment_distance_loaded_one_way_only": "Пробег пополнения учитывается только в загруженном направлении.",
}

STEP_CONTEXT = {
    "Настройка склада": ("Настраиваем физическую схему склада.", "Модель склада, ряды, ячейки, зоны и одни ворота.", "Сохранённая схема для загрузки фактических данных."),
    "Загрузка данных": ("Загружаем фактические данные для моделирования.", "РО, историческое размещение, инвентаризация и ВГХ.", "Единый набор исходных данных для проверки."),
    "Правила размещения": ("Задаём правила предлагаемого размещения.", "Проверенные данные и выбранные правила модели.", "Конфигурация, по которой строится предлагаемый вариант."),
    "Сравнение вариантов": ("Строим и сопоставляем исходное и предлагаемое размещение.", "Готовые данные, правила размещения и настроенные ворота.", "Два сопоставимых варианта размещения."),
    "Расчёт маршрутов": ("Рассчитываем одинаковые РО для двух вариантов.", "Исходный и предлагаемый варианты, РО и ворота.", "Пробег и маршруты выбранных РО."),
    "Результаты": ("Изучаем эффект и получаем рекомендации.", "Завершённый сопоставимый расчёт маршрутов.", "Метрики, ограничения и рекомендации по размещению."),
}

WORKFLOW_DETAILS = (
    ("Склад", "Настраиваем физическую схему склада.", "Модель склада, ряды, ячейки, зоны и ворота.", "Готовая схема склада."),
    ("Загрузка данных", "Собираем факты для моделирования.", "РО, размещение, инвентаризация и ВГХ.", "Единый набор исходных данных."),
    ("Проверка качества данных", "Проверяем, хватает ли данных для расчёта.", "РО, размещение, инвентаризация и ВГХ.", "Понимание, можно ли запускать расчёт."),
    ("Настройка модели", "Выбираем правила предлагаемого размещения.", "Проверенные данные и правила размещения.", "Готовая конфигурация модели."),
    ("Расчёт маршрутов", "Считаем одинаковый спрос для двух размещений.", "Правила, РО, варианты размещения и ворота.", "Маршруты и измеренный пробег."),
    ("Сравнение результатов", "Сопоставляем исходный и предлагаемый варианты.", "Завершённый сопоставимый расчёт.", "Разница пробега без изменения исходных фактов."),
    ("Аналитика и рекомендации", "Разбираем эффект и ограничения.", "Результаты сравнения и диагностика.", "Выводы и следующие решения по складу."),
)


def deep_lane_edit_issue(row_type: str, width: Any, access_side: Any) -> dict[str, str] | None:
    """Explain an incompatible row draft instead of silently normalising it."""
    if row_type == "normal" and (float(width or 1) != 1 or str(access_side or "") not in {"", "Не настроено"}):
        return get_ui_message("deep_width_on_normal_row" if float(width or 1) != 1 else "deep_access_on_normal_row")
    return None


def render_context_block(step: str) -> None:
    doing, needed, result = STEP_CONTEXT[step]
    st.markdown(f'<div class="workflow-context"><b>Что делаем:</b><br>{doing}<br><b>Что нужно:</b><br>{needed}<br><b>Результат:</b><br>{result}</div>', unsafe_allow_html=True)


def render_workflow_stepper(model: Mapping[str, Any] | None, session_state: Mapping[str, Any]) -> None:
    state = state_from_session(model, session_state)
    symbols = {"completed": "✅", "current": "⚠️", "available": "⬜", "blocked": "⬜", "stale": "⚠️"}
    items = "".join(f'<span class="workflow-step {item["status"]}">{item["number"]}. {item["name"]} {symbols[item["status"]]}</span>' for item in state["steps"])
    current = next((item for item in state["steps"] if item["status"] in {"current", "stale"}), state["steps"][-1])
    completed = sum(item["ready"] for item in state["steps"])
    next_text = current["name"] if not current["ready"] else "Рабочий процесс завершён"
    summary = (f'<div class="workflow-summary"><b>ШАГ {current["number"]} из {len(state["steps"])}</b>'
               f'<br>✅ Выполнено этапов: {completed}<br>➡ <b>Следующее действие:</b> {next_text}</div>')
    st.markdown(f'<b>Рабочий процесс:</b>{summary}<div class="workflow-stepper">{items}</div>', unsafe_allow_html=True)
    details = WORKFLOW_DETAILS[current["number"] - 1]
    st.markdown(
        f'<div class="workflow-context"><b>Шаг: {details[0]}</b><br>'
        f'<b>Что делаем:</b><br>{details[1]}<br><b>Что нужно:</b><br>{details[2]}<br>'
        f'<b>Результат:</b><br>{details[3]}</div>', unsafe_allow_html=True,
    )


def _next_section(selected: str) -> str | None:
    index = WORKSPACE_TABS.index(selected)
    return WORKSPACE_TABS[index + 1] if index + 1 < len(WORKSPACE_TABS) else None


def render_next_action(selected: str, state: Mapping[str, Any]) -> None:
    """Always leave the user with an actionable, explained navigation choice."""
    target = _next_section(selected)
    st.markdown("### Следующее действие")
    if target is None:
        if state["analytics_ready"]:
            st.success("Рабочий процесс завершён. Изучите рекомендации и сохраните выводы.")
        else:
            st.warning("Недоступно.\n\n**Причина:** ещё нет завершённого сопоставимого расчёта.\n\n**Что сделать:** вернитесь к первому незавершённому шагу рабочего процесса.")
            target = "Загрузка данных"
    else:
        st.write(target)
    if target and st.button(f"Перейти: {target}", key=f"workspace_next_{WORKSPACE_TABS.index(selected)}"):
        st.session_state["workspace_section"] = target
        st.rerun()


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
             "Обычные места": totals[zone]["normal"], "Набивные места": totals[zone]["deep"]}
            for zone in PLACEMENT_ZONE_IDS if totals[zone]["cells"]]


def render_operational_workspace(model: dict | None, *, warehouse_renderer: Callable,
                                 data_renderer: Callable, rules_renderer: Callable,
                                 comparison_renderer: Callable, distance_renderer: Callable,
                                 analytics_renderer: Callable) -> None:
    st.markdown("""<style>
    :root{--ui-green:#287a4b;--ui-green-bg:#edf8f1;--ui-yellow:#9a6700;--ui-yellow-bg:#fff8df;--ui-red:#b42318;--ui-red-bg:#fff1f0;--ui-gray:#52606d;--ui-border:#dfe3e8}
    .stApp{background:#f6f7f9}.block-container{max-width:1280px;padding-top:1.8rem}.workflow-stepper{display:flex;gap:.45rem;flex-wrap:wrap;background:white;border:1px solid var(--ui-border);border-radius:12px;padding:.65rem .8rem;margin-bottom:.8rem}
    .workflow-step{color:#64748b;padding:.15rem .35rem}.workflow-step.completed{color:var(--ui-green)}.workflow-step.current{color:var(--ui-yellow);font-weight:650}.workflow-step.stale{color:var(--ui-yellow)}
    .workflow-summary{background:#f1f3f5;border-left:4px solid var(--ui-gray);border-radius:8px;padding:.7rem .9rem;margin-bottom:.55rem;line-height:1.5}.workflow-context{background:white;border:1px solid var(--ui-border);border-left:3px solid var(--ui-gray);border-radius:8px;padding:.7rem .9rem;line-height:1.45;margin:.25rem 0 1rem}
    .ui-page-title{margin:.2rem 0 .25rem;font-size:1.65rem;font-weight:720;color:#1f2933}.ui-page-kicker{color:#667085;margin-bottom:.75rem}
    .ui-status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.7rem;margin:.4rem 0 1rem}.ui-status-card{background:white;border:1px solid var(--ui-border);border-top:4px solid var(--ui-gray);border-radius:12px;padding:.75rem .85rem;min-height:132px}.ui-status-card.success{border-top-color:var(--ui-green);background:var(--ui-green-bg)}.ui-status-card.warning{border-top-color:var(--ui-yellow);background:var(--ui-yellow-bg)}.ui-status-card.error{border-top-color:var(--ui-red);background:var(--ui-red-bg)}.ui-status-heading{display:flex;gap:.4rem;font-weight:700}.ui-status-value{font-size:1.35rem;font-weight:750;margin:.5rem 0 .1rem}.ui-status-label{font-size:.82rem;font-weight:650;color:var(--ui-gray)}.ui-status-explanation{font-size:.85rem;color:#667085;margin-top:.35rem;line-height:1.35}
    div.stButton>button[kind="primary"]{background:var(--ui-green);border-color:var(--ui-green);font-weight:650}div.stButton>button[kind="secondary"]{border-color:#aeb6bf;color:#344054}div[data-testid="stExpander"]{background:white;border-color:var(--ui-border);border-radius:10px}
    @media(max-width:900px){.block-container{padding-left:1rem;padding-right:1rem}.ui-status-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){.ui-status-grid{grid-template-columns:1fr}}
    </style>""", unsafe_allow_html=True)
    with measure("workspace.root"):
        workflow_state = state_from_session(model, st.session_state)
        render_workflow_stepper(model, st.session_state)
        legacy = st.session_state.get("workspace_section")
        if legacy in LEGACY_WORKSPACE_TABS:
            st.session_state["workspace_section"] = LEGACY_WORKSPACE_TABS[legacy]
        selected = st.radio("Раздел", WORKSPACE_TABS, horizontal=True, key="workspace_section",
                            label_visibility="collapsed")
        st.markdown(f'<div class="ui-page-title">{escape(selected)}</div><div class="ui-page-kicker">Рабочее пространство склада</div>', unsafe_allow_html=True)
        current_step = next((item for item in workflow_state["steps"] if item["status"] in {"current", "stale"}), workflow_state["steps"][-1])
        render_status_grid([
            {"title": "Схема склада", "value": "Настроена" if model else "Нет данных", "explanation": "Основа для размещения и маршрутов.", "status": "success" if model else "empty"},
            {"title": "Рабочий процесс", "value": f'{sum(item["ready"] for item in workflow_state["steps"])} из {len(workflow_state["steps"])}', "explanation": f'Текущий этап: {current_step["name"]}.', "status": "success" if all(item["ready"] for item in workflow_state["steps"]) else "warning"},
            {"title": "Следующий шаг", "value": current_step["name"], "explanation": "Продолжите с первого незавершённого этапа.", "status": "success" if current_step["ready"] else "empty"},
        ])
        renderers = dict(zip(WORKSPACE_TABS, (warehouse_renderer, data_renderer, rules_renderer,
                                             comparison_renderer, distance_renderer, analytics_renderer)))
        render_context_block(selected)
        with measure(f"workspace.section.{selected}"):
            renderers[selected](model)
        render_next_action(selected, workflow_state)
    if PERF_ENABLED:
        perf = snapshot()
        with st.expander("Диагностика производительности", expanded=False):
            rss = f"{perf['rss_mb']} MB" if perf["rss_mb"] is not None else "недоступно"
            st.caption(f"RSS: {rss} · раздел: {selected} · последнее отображение: {perf['last_render_ms']} ms")
            st.caption(f"Чтение файлов: {perf['artifact_reads']} / {perf['artifact_bytes']} байт · кэш: {perf['cache_status']}")
            st.json(perf["top_slow_blocks"])


def render_rules_control_panel(model: Mapping[str, Any] | None, session_state: Mapping[str, Any]) -> None:
    st.subheader("Условия предлагаемого размещения")
    st.caption("Исходное остаётся неизменным; настройки применяются только после явного пересчёта предлагаемого размещения.")
    values = {}
    for rule in SUPPORTED_RULES:
        title, description = RULE_CARDS[rule]
        disabled = rule == "replenishment" and not values.get("picking_storage", False)
        values[rule] = st.checkbox(title, key=f"workspace_rule_{rule}", disabled=disabled)
        st.caption(description)
        if disabled:
            st.caption("Заблокировано: требуется правило «Комплектация / хранение».")
        else:
            st.caption("Готово" if model else "Данные склада не загружены. Загрузите схему склада, чтобы применить правило.")
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


def _render_import_result(result: Mapping[str, Any], filename: str) -> None:
    """Explain recognition without exposing the source lifecycle contract."""
    if result.get("source_type") == "unknown":
        render_ui_message({
            "severity": "error", "title": "Не удалось определить тип файла",
            "reason": "Не найден обязательный набор колонок.",
            "impact": "Файл не загружен и не будет использоваться в расчётах.",
            "action": "Проверьте выгрузку и повторите загрузку.",
            "target": "Загрузка данных", "next_action_label": "Выбрать другой файл",
        })
        found = result.get("diagnostic_found") or result.get("detected_columns") or []
        missing = result.get("diagnostic_missing") or result.get("required_missing") or []
        st.markdown(f"**Найдены:** {', '.join(map(str, found)) or 'обязательные колонки не распознаны'}")
        st.markdown(f"**Ожидаются:** {', '.join(map(str, missing)) or 'колонки одного из поддерживаемых типов файлов'}")
        return
    label = SOURCE_LABELS.get(str(result.get("source_type")), "Фактические данные")
    if result.get("reused"):
        st.info(f"ℹ️ Файл «{filename}» уже загружен. Используются сохранённые данные.")
    else:
        st.success(f"✅ Определён тип: «{label}»")


def _render_source_quality(card: Mapping[str, Any], readiness: Mapping[str, Any] | None) -> None:
    source_type = card["source_type"]
    if source_type == "historical_placement":
        st.caption(f"Срезов: {len(card['dates'])} · Период: {card['period']} · SKU: {card['sku']} · Ячеек: {card['cells']}")
        check = next((x for x in (readiness or {}).get("diagnostics", {}).get("checks", []) if x.get("name") == "placement_snapshot"), None)
        if check and check.get("missing_dates"):
            st.caption("Пропущенные даты: " + ", ".join(check["missing_dates"]))
    elif source_type == "vgh":
        coverage = (readiness or {}).get("coverage", {})
        demanded, covered = int(coverage.get("demanded_sku", 0)), int(coverage.get("vgh_covered_sku", card["sku"]))
        percent = round(100 * covered / demanded, 1) if demanded else 100.0
        st.caption(f"SKU с ВГХ: {card['sku']} · востребованные SKU без ВГХ: {max(0, demanded-covered)} · покрытие: {percent}%")
        if demanded and covered < demanded:
            st.warning(f"""⚠️ ВГХ покрывает {covered} из {demanded} востребованных SKU.

**Что это означает:** для SKU без ВГХ часть правил размещения может работать с ограничениями.

**Что делать:** при возможности загрузите более полный файл ВГХ.""")
    else:
        st.caption(f"Документов: {card['documents']} · Строк: {card['rows']} · SKU: {card['sku']} · Период: {card['period']}")


def render_factual_data_layer(model: Mapping[str, Any] | None) -> None:
    """Render a guided metadata-only upload screen; expensive checks stay explicit."""
    st.subheader("Загрузка данных")
    st.caption("Добавьте исходные данные, проверьте качество и переходите к правилам размещения.")
    registry = load_registry()
    cards = build_data_source_cards(registry)
    cached_readiness = st.session_state.get("monthly_data_readiness")

    st.markdown("### Данные июля")
    render_status_grid([{
        "title": card["title"], "value": format_compact_number(card["rows"]) + " строк" if card["sources"] else "Нет данных",
        "explanation": card["period"] if card["sources"] else "Файл ещё не загружен.",
        "status": "error" if card["status"].startswith("❌") else "warning" if card["status"].startswith("⚠️") else "success" if card["sources"] else "empty",
    } for card in cards])
    mandatory_ready = all(card["sources"] for card in cards if card["source_type"] in MONTHLY_ROUTE_REQUIRED_SOURCES)
    (st.success if mandatory_ready else st.warning)("Основные данные загружены" if mandatory_ready else "Не хватает обязательных данных")

    st.markdown("### Добавить файл")
    files = st.file_uploader("Выберите один или несколько Excel-файлов", type=["xlsx", "xls"], accept_multiple_files=True, key="factual_data_uploads")
    if files and st.button("Добавить выбранные файлы", type="primary", key="factual_data_import"):
        with st.spinner("Распознаём и сохраняем файлы…"):
            for uploaded in files:
                try:
                    result = import_excel_dataset(uploaded.getvalue(), uploaded.name)
                except (OSError, ValueError) as exc:
                    render_ui_message({"severity": "error", "title": "Файл не загружен", "reason": str(exc),
                        "impact": "Данные из этого файла не участвуют в расчётах.", "action": "Проверьте формат Excel и повторите загрузку.", "target": "Загрузка данных"})
                else:
                    _render_import_result(result, uploaded.name)

    st.markdown("### Источники данных")
    for card in cards:
        with st.container(border=True):
            st.markdown(f"#### {card['title']}")
            st.caption(card['purpose'])
            st.markdown(f"""**Основное:** {format_compact_number(card['rows'])} строк · {format_compact_number(card['sku'])} SKU · период {card['period']}

**Статус:** {card['status']}""")
            _render_source_quality(card, cached_readiness)
            with st.expander("Дополнительные сведения", expanded=False):
                st.write(f"Файл: {card['file']}")
                st.write(f"Документов: {format_compact_number(card['documents'])} · Ячеек: {format_compact_number(card['cells'])}")
            if card["sources"]:
                with st.expander("Заменить текущий файл"):
                    replacement = st.file_uploader("Новый файл", type=["xlsx", "xls"], key=f"replace_{card['source_type']}")
                    st.markdown(f"""**Текущий файл:** {card['file']}

**Новый файл:** {getattr(replacement, 'name', 'не выбран')}

**Что произойдёт:** новый файл станет использоваться в расчётах. Предыдущая версия сохранится в истории.""")
                    if replacement and st.button("Заменить файл", key=f"replace_confirm_{card['source_type']}"):
                        try:
                            result = import_excel_dataset(replacement.getvalue(), replacement.name)
                        except (OSError, ValueError) as exc:
                            render_ui_message({"severity": "error", "title": "Не удалось заменить файл", "reason": str(exc), "impact": "Текущий файл продолжает использоваться.", "action": "Исправьте новый файл и повторите замену.", "target": "Загрузка данных"})
                        else:
                            _render_import_result(result, replacement.name)
            else:
                st.caption("Действие: добавьте файл через общий загрузчик выше.")

    st.markdown("### Проверка готовности")
    st.markdown("""**Что будет проверено:**
- наличие обязательных источников;
- период;
- срезы размещения;
- конфликты;
- покрытие ВГХ.""")
    if not model:
        st.info("Для проверки сначала загрузите схему склада.")
    if st.button("Проверить готовность данных июля", key="monthly_readiness_check", disabled=not bool(model)):
        with st.spinner("Проверяем данные июля…"), measure("factual.build_monthly_data_readiness"):
            cached_readiness = build_monthly_data_readiness(registry, model or {}, "2026-07-01", "2026-07-31")
        st.session_state["monthly_data_readiness"] = cached_readiness
    if cached_readiness:
        if cached_readiness.get("monthly_replay_ready"):
            render_ui_message(monthly_readiness_message(cached_readiness))
            st.markdown("### Следующий шаг: «Перейти к правилам размещения»")
            for check in cached_readiness.get("diagnostics", {}).get("checks", []):
                if check.get("status") == "warning": st.markdown(format_monthly_readiness_check(check))
        else:
            blockers = cached_readiness.get("hard_blockers", [])
            render_ui_message({"severity": "error", "title": "Требуется исправление", "reason": f"Обнаружено проблем: {len(blockers)}.", "impact": "Переход к корректному расчёту пока недоступен.", "action": "Исправьте перечисленные проблемы и повторите проверку.", "target": "Загрузка данных"})
            st.markdown(f"### Следующий шаг: «Исправить {len(blockers)} проблем»")
            if blockers:
                st.markdown("#### Причины блокировки")
                for blocker in blockers:
                    st.markdown(format_monthly_readiness_blocker(blocker))
            for check in cached_readiness.get("diagnostics", {}).get("checks", []):
                if check.get("status") == "fail": st.markdown(format_monthly_readiness_check(check))

    datasets = registry.get("datasets", [])
    with st.expander("История загруженных файлов"):
        if not datasets:
            st.caption("История пока пуста.")
        for item in sorted(datasets, key=lambda x: x.get("imported_at", ""), reverse=True):
            state = "используется сейчас" if item.get("active", True) else "предыдущая версия"
            st.write(f"{item.get('source_file_name', '—')} · {item.get('imported_at', '—')} · {state}")
    with st.expander("Технические сведения"):
        st.json([{"dataset_id": item.get("dataset_id"), "hash": item.get("content_hash"), "parser version": item.get("parser_version"), "active": item.get("active"), "logical_source_id": item.get("logical_source_id")} for item in datasets])

def render_monthly_fact_baseline(model: Mapping[str, Any] | None, session_state: Mapping[str, Any]) -> None:
    """Run and inspect persisted July FACT partitions without предлагаемое размещение logic."""
    st.subheader("Фактический расчёт за июль")
    if not model:
        st.info("Сначала загрузите схему склада."); return
    registry = load_registry()
    readiness = session_state.get("monthly_data_readiness")
    if readiness is None:
        st.info("Перед фактическим расчётом проверьте готовность месяца в разделе «Данные»."); return
    if readiness.get("monthly_replay_ready") is not True:
        blockers = readiness.get("hard_blockers", [])
        blocker = monthly_readiness_blocker_details(blockers[0]) if blockers else {}
        failed = next((check for check in readiness.get("diagnostics", {}).get("checks", [])
                       if check.get("status") == "fail"), {})
        reason = blocker.get("title") or failed.get("details") or "Обязательная проверка данных не пройдена."
        if blocker.get("details"):
            reason += f" {blocker['details']}"
        render_ui_message({
            "severity": "error", "title": "Расчёт месяца невозможен",
            "reason": reason,
            "impact": "Нельзя достоверно построить маршруты и сравнить результат за весь месяц.",
            "action": blocker.get("action") or "Исправьте ошибки качества данных и повторите проверку месяца.",
            "target": "Данные", "next_action_label": "Исправить ошибки качества данных",
            "technical_code": blocker.get("code") or "monthly_data_not_ready",
        })
        for item in blockers:
            st.markdown(format_monthly_readiness_blocker(item))
        return
    st.success("Слой фактических данных готов")
    gate_state = session_state.get("workspace_gate_state")
    if not gate_state:
        st.error("Не настроены авторитетные ворота."); return
    if st.button("Рассчитать фактические данные за июль", key="monthly_fact_run"):
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
              ("Коробов", summary.get("picked_boxes")), ("Дефицит", summary.get("shortage_boxes")),
              ("Фактический пробег, км", round((summary.get("strict_fact_picker_distance_m") or 0)/1000, 3)),
              ("Строгое покрытие", f"{100*(summary.get('route_order_coverage') or 0):.1f}%"))
    for column, (label, value) in zip(st.columns(6), values): column.metric(label, value)
    artifact = Path(str(summary.get("artifact_path") or "")); files = sorted(artifact.glob("day=*.json")) if artifact.is_dir() else []
    if files:
        import json
        daily = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        st.dataframe(pd.DataFrame([{"Дата": d["operational_day"], "РО": d["orders_total"],
            "Запрошено коробов": d["requested_boxes"], "Собрано": d["picked_boxes"], "Дефицит": d["shortage_boxes"],
            "Фактический пробег, м": d["picker_distance_m"], "Строгий статус": d["status"],
            "Неоднозначные ячейки": d["source_location_ambiguity_count"]} for d in daily]), use_container_width=True, hide_index=True)
        selected_day = st.selectbox("День для детализации РО", [d["operational_day"] for d in daily])
        chosen = next(d for d in daily if d["operational_day"] == selected_day)
        identities = [o["order_identity"].get("document_number") or o["order_identity"].get("document_ref") for o in chosen["order_results"]]
        if identities:
            selected = st.selectbox("Расходный ордер", identities); order = chosen["order_results"][identities.index(selected)]
            st.json({"order_identity": order["order_identity"], "picker_distance_m": order.get("picker_distance_m"),
                     "shortage_boxes": order["shortage_boxes"], "strict_comparable": order["strict_comparable"],
                     "route_legs": order["route_legs"], "factual_pick_stops": order["factual_pick_stops"],
                     "source_location_ambiguity": order["source_location_ambiguous"], "blockers": order["blockers"]})
    st.caption("Приходы и инвентаризации не изменяют начальное размещение; неоднозначные ячейки не угадываются; паллета — техническая ссылка; каждый день начинается с состояния на 00:00; предлагаемое размещение и экономия не рассчитываются.")


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
    st.subheader("Исходное и предлагаемое размещение — июль")
    if not comparison:
        st.info("Сохранённое месячное сравнение ещё не сформировано.")
        return
    readiness = comparison.get("readiness", "partial")
    (st.success if readiness == "ready" else st.warning)(f"Готовность данных: {readiness}")
    metrics = (("Фактическое, м", comparison.get("fact_meters")), ("Предлагаемое", comparison.get("proposed_meters")),
               ("Экономия", comparison.get("saved_meters")), ("Экономия, %", comparison.get("saved_percent")))
    for column, (label, value) in zip(st.columns(4), metrics):
        column.metric(label, f"{float(value or 0)/1000:.3f} км" if label != "Экономия, %" else f"{float(value or 0):.2f}%")
    st.caption(f"Сопоставимо {comparison.get('comparable_orders', 0)} / {comparison.get('full_order_count', 0)} РО · "
               f"исключено {comparison.get('excluded_orders', 0)} · покрытие данных фактического / предлагаемого размещения "
               f"{100*float(comparison.get('fact_coverage', 0)):.1f}% / {100*float(comparison.get('proposed_coverage', 0)):.1f}%")
    daily = comparison.get("daily_results") or []
    if daily:
        st.dataframe(pd.DataFrame(daily).rename(columns={"date": "Дата", "ro_count": "РО", "fact_meters": "Фактическое, м",
            "proposed_meters": "Предлагаемое м", "saved_meters": "Δ м", "saved_percent": "Δ %",
            "strict_coverage": "Покрытие данных", "warnings": "Предупреждения"}), use_container_width=True, hide_index=True)
    orders = comparison.get("order_comparisons") or []
    if orders:
        labels = [str(x.get("order_identity", {}).get("document_number") or x.get("order_identity", {}).get("document_ref") or i)
                  for i, x in enumerate(orders)]
        selected = st.selectbox("Расходный ордер для детализации", labels, key="monthly_comparison_ro")
        order = orders[labels.index(selected)]
        st.json({"Фактическое": {"distance": order.get("fact_meters"), "route": order.get("fact_route"),
                          "pick_stops": order.get("fact_pick_stops")},
                 "Предлагаемое": {"distance": order.get("proposed_meters"), "route": order.get("proposed_route"),
                              "pick_stops": order.get("proposed_pick_stops")},
                 "changed_skus": order.get("changed_skus"), "warnings": order.get("warnings")})
        graph = comparison.get("route_graph") or {}
        if graph:
            from warehouse_route_ui import build_route_overlay
            st.json({"Маршрут фактического размещения": build_route_overlay({"route_legs": order.get("fact_route"),
                "pick_events": order.get("fact_pick_stops"), "picker_distance_m": order.get("fact_meters")}, graph, "current"),
                "Маршрут предлагаемого размещения": build_route_overlay({"route_legs": order.get("proposed_route"),
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
    st.subheader("Аналитика исходного и предлагаемого размещения")
    comparison = session_state.get("placement_comparison_distance_comparison")
    if not comparison:
        st.info("Рассчитайте пробег исходного и предлагаемого размещения в одноимённом разделе.")
    elif session_state.get("placement_comparison_distance_signature") != session_state.get("placement_comparison_active_distance_signature"):
        st.warning("Результат пробега устарел — пересчитайте.")
    elif comparison.get("full_day_effect_valid") is not True:
        st.warning("Эффект полного дня недоступен")
        st.write(" · ".join(comparison.get("blockers") or comparison.get("limitations") or ["Объём отбора в исходном и предлагаемом размещении различается."]))
    else:
        summary = authoritative_analytics_metrics(comparison) or {}
        keys = (("Исходное, м", "current_picker_distance_m"), ("Предлагаемое, м", "proposed_picker_distance_m"),
                ("Экономия, м", "picker_distance_saved_m"), ("Экономия, %", "picker_distance_saved_percent"),
                ("РО", "orders_total"), ("Собрано в исходном", "current_picked_boxes"),
                ("Собрано в предлагаемом", "proposed_picked_boxes"), ("Дефицит в исходном", "current_shortage_boxes"),
                ("Дефицит в предлагаемом", "proposed_shortage_boxes"), ("Сервис эквивалентен", "service_equivalent"))
        for column, (label, key) in zip(st.columns(len(keys)), keys): column.metric(label, summary.get(key, "—"))
    if comparison:
        orders = comparison.get("orders") or comparison.get("order_comparisons") or []
        if orders: st.dataframe(pd.DataFrame(orders), use_container_width=True, hide_index=True)
    st.subheader("Ограничения текущего расчёта")
    for text in LIMITATION_LABELS.values(): st.write(f"• {text}")
    with st.expander("Технические IDs и диагностика"):
        st.json({"limitations": list(LIMITATION_LABELS), "comparison": comparison or {}})
    render_monthly_fact_baseline(model, session_state)
