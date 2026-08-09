"""Streamlit integration for the CURRENT / PROPOSED placement preview.

The module deliberately contains orchestration and presentation only.  The
authoritative placement rules, optimizer, and materializer remain behind
``build_proposed_scenario``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from warehouse_business_identity import normalize_warehouse
from warehouse_geometry_render_layers import (
    build_geometry_static_layer,
    compose_geometry_layers,
)
from warehouse_opening_stock_reconciliation import reconcile_opening_stock
from warehouse_outbound_experiment_inputs import filter_actual_placement_state_by_warehouse
from warehouse_pick_demands import build_outbound_pick_demands
from warehouse_placement_zones import is_assignable_placement_zone, normalize_placement_zone
from warehouse_proposed_scenario import build_proposed_scenario
from warehouse_simulation_distance_comparison import compare_simulation_outbound_replay
from warehouse_simulation_outbound_replay import replay_outbound_on_simulation_states
from warehouse_simulation_render import build_simulation_dynamic_payload
from warehouse_simulation_state import build_initial_simulation_state


SESSION_PREFIX = "placement_comparison"
MAP_HEIGHT = 720
_LABEL_SETTINGS = {"show_cell_labels": True}


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def build_comparison_signature(
    *, model_id: Any, baseline_state_id: Any, operational_date: Any,
    normalized_warehouse: Any, sku_zone_rows: Sequence[Mapping[str, Any]],
    rule_config: Mapping[str, Any],
) -> str:
    """Fingerprint business inputs without depending on translated UI labels."""
    identity = {
        "model_id": model_id,
        "baseline_state_id": baseline_state_id,
        "operational_date": operational_date,
        "normalized_warehouse": normalize_warehouse(normalized_warehouse),
        "sku_zone_rows": sorted(
            (_canonical(dict(row)) for row in sku_zone_rows),
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
        ),
        "rule_config": _canonical(rule_config),
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_weight_zone_rule_config(enabled: bool) -> dict[str, dict[str, bool]]:
    """Translate the only active UI rule into the backend rule contract."""
    return {"weight_zones": {"enabled": bool(enabled)}}


def build_sku_zone_rows(classification_rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """Adapt accepted SKU classification; never infer a zone from a cell."""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in classification_rows or []:
        sku_key = str(row.get("sku_key") or "").strip()
        zone = normalize_placement_zone(row.get("calculated_zone"))
        if not sku_key or not is_assignable_placement_zone(zone):
            continue
        adapted = (sku_key, str(zone), "loaded_receipt_classification")
        if adapted not in seen:
            seen.add(adapted)
            result.append({"sku_key": adapted[0], "target_zone": adapted[1], "source": adapted[2]})
    return sorted(result, key=lambda row: (row["sku_key"], row["target_zone"], row["source"]))


def summarize_scenario_ui_metrics(
    scenario: Mapping[str, Any], baseline_state: Mapping[str, Any],
    sku_zone_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return display-only counts from backend reports and explicit coverage."""
    summary = scenario.get("summary", {}) if isinstance(scenario, Mapping) else {}
    zoned_skus = {str(row.get("sku_key")) for row in sku_zone_rows if row.get("sku_key")}
    lots = baseline_state.get("stock_lots", []) if isinstance(baseline_state, Mapping) else []
    opening_skus = {str(lot.get("sku_key")) for lot in lots if lot.get("sku_key")}
    missing_skus = opening_skus - zoned_skus
    missing_placements = sum(1 for lot in lots if str(lot.get("sku_key")) in missing_skus)
    return {
        "units_moved": summary.get("units_moved", 0),
        "units_kept": summary.get("units_kept", 0),
        "fixed_units": summary.get("fixed_units", 0),
        "unresolved_units": summary.get("unresolved_units", 0),
        "missing_zone_skus": len(missing_skus),
        "missing_zone_placements": missing_placements,
        "zone_coverage_percent": round(100 * (len(opening_skus) - len(missing_skus)) / len(opening_skus), 1)
        if opening_skus else 100.0,
        "compliance_before_percent": summary.get("weight_zone_compliance_before_percent", 100.0),
        "compliance_after_percent": summary.get("weight_zone_compliance_after_percent", 100.0),
    }


def build_comparison_baseline(
    model: dict[str, Any], start_state: dict[str, Any], opening_rows: list[dict[str, Any]],
    *, normalized_warehouse: str, operational_date: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build CURRENT through the existing warehouse-scoped reconciliation seam."""
    target = normalize_warehouse(normalized_warehouse)
    if not target or not operational_date or not model or not start_state or not opening_rows:
        return None, {"configuration_errors": ["comparison_inputs_not_ready"]}
    scoped_start, scope_diagnostics = filter_actual_placement_state_by_warehouse(start_state, target)
    scoped_inventory = [
        dict(row) for row in opening_rows
        if normalize_warehouse(row.get("normalized_warehouse") or row.get("warehouse")) == target
    ]
    opening_stock, reconciliation = reconcile_opening_stock(model, scoped_inventory, scoped_start)
    baseline, simulation = build_initial_simulation_state(
        model, opening_stock, target_normalized_warehouse=target,
        simulation_time=str(operational_date),
    )
    diagnostics = {"start_scope": scope_diagnostics, "opening_stock": reconciliation,
                   "simulation_state": simulation}
    errors = simulation.get("configuration_errors", [])
    if errors or not baseline.get("readiness", {}).get("stock_ready"):
        diagnostics["configuration_errors"] = errors or ["baseline_stock_not_ready"]
        return None, diagnostics
    diagnostics["configuration_errors"] = []
    return baseline, diagnostics


@st.cache_data(show_spinner=False)
def _cached_static_layer(model: dict[str, Any], model_id: str) -> str:
    # ``model_id`` is explicit cache identity and also documents why one shared
    # layer is safe for both dynamic states.
    del model_id
    return build_geometry_static_layer(model, scale=18.0, detailed=True,
                                       label_settings=_LABEL_SETTINGS)


def _short_id(value: Any) -> str:
    text = str(value or "—")
    return text[:19] + "…" if len(text) > 20 else text


def _hash(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _show_metrics(metrics: Mapping[str, Any]) -> None:
    labels = (
        ("Перемещено размещений", "units_moved"), ("Оставлено на месте", "units_kept"),
        ("Фиксировано / не перестраивается", "fixed_units"), ("Без зоны", "missing_zone_placements"),
    )
    for column, (label, key) in zip(st.columns(4), labels):
        column.metric(label, metrics.get(key, 0))
    st.caption(
        "Соответствие весовым зонам: "
        f"до {metrics.get('compliance_before_percent', 0)}% · "
        f"после {metrics.get('compliance_after_percent', 0)}%"
    )
    st.caption(
        f"Без весовой зоны: {metrics.get('missing_zone_skus', 0)} SKU / "
        f"{metrics.get('missing_zone_placements', 0)} размещений"
    )


def build_distance_order_rows(comparison: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt the backend distance contract to the Russian results table."""
    labels = {"improved": "Улучшился", "worsened": "Ухудшился", "equal": "Без изменений",
              "not_comparable": "Несопоставим"}
    return [{
        "Дата": row.get("operational_date"), "РО": row.get("outbound_order_number"),
        "CURRENT, м": row.get("current_distance_m"), "PROPOSED, м": row.get("proposed_distance_m"),
        "Экономия, м": row.get("distance_saved_m"), "Экономия, %": row.get("distance_saved_percent"),
        "Статус": labels.get(row.get("classification"), row.get("classification")),
        "Запрошено коробов": row.get("requested_boxes"), "Собрано CURRENT": row.get("current_picked_boxes"),
        "Собрано PROPOSED": row.get("proposed_picked_boxes"), "Дефицит CURRENT": row.get("current_shortage_boxes"),
        "Дефицит PROPOSED": row.get("proposed_shortage_boxes"),
        "Причины несопоставимости": ", ".join(row.get("reasons") or []),
    } for row in comparison.get("orders", [])]


def _show_distance_comparison(comparison: Mapping[str, Any]) -> None:
    summary, coverage = comparison.get("summary", {}), comparison.get("coverage", {})
    st.markdown("### Экономия пробега")
    primary = (("CURRENT, м", "current_total_distance_m"), ("PROPOSED, м", "proposed_total_distance_m"),
               ("Экономия, м", "distance_saved_m"), ("Экономия, %", "distance_saved_percent"))
    for column, (label, key) in zip(st.columns(4), primary):
        column.metric(label, f"{float(summary.get(key) or 0):,.2f}".replace(",", " "))
    for title, prefix in (("Средний пробег / РО", "average"), ("Медианный пробег / РО", "median")):
        st.markdown(f"**{title}**")
        values = (("CURRENT", f"{prefix}_current_distance_per_order_m"),
                  ("PROPOSED", f"{prefix}_proposed_distance_per_order_m"),
                  ("Δ", f"{prefix}_saved_per_order_m"))
        for column, (label, key) in zip(st.columns(3), values):
            value = summary.get(key)
            column.metric(label, "—" if value is None else f"{float(value):,.2f} м".replace(",", " "))
    st.write(f"Сопоставимых РО: {coverage.get('strict_comparable_orders', 0)} / {coverage.get('orders_total', 0)} · "
             f"Улучшилось: {summary.get('improved_orders', 0)} · Ухудшилось: {summary.get('worsened_orders', 0)} · "
             f"Без изменений: {summary.get('equal_orders', 0)}")
    st.caption(f"Покрытие РО: {coverage.get('order_comparability_percent', 0):.2f}% · "
               f"покрытие коробов: {coverage.get('requested_boxes_coverage_percent', 0):.2f}% · "
               f"область эффекта: {comparison.get('scope')}")
    st.dataframe(build_distance_order_rows(comparison), use_container_width=True)


def render_scenario_comparison(
    model: dict[str, Any], *, operational_date: Any, selected_warehouse: Any,
    start_state: dict[str, Any] | None, opening_rows: list[dict[str, Any]],
    classification_rows: Sequence[Mapping[str, Any]] | None,
    outbound_rows: Sequence[Mapping[str, Any]] | None = None,
    gate_state: dict[str, Any] | None = None,
) -> None:
    """Render the independent placement preview before the outbound replay UI."""
    st.divider()
    st.subheader("Сравнение размещения CURRENT / PROPOSED")
    target = normalize_warehouse(selected_warehouse)
    baseline, baseline_diagnostics = build_comparison_baseline(
        model, start_state or {}, opening_rows, normalized_warehouse=target,
        operational_date=operational_date,
    )
    sku_zone_rows = build_sku_zone_rows(classification_rows)
    opening_skus = {lot.get("sku_key") for lot in (baseline or {}).get("stock_lots", []) if lot.get("sku_key")}
    covered = {row["sku_key"] for row in sku_zone_rows} & opening_skus
    coverage = round(100 * len(covered) / len(opening_skus), 1) if opening_skus else 0.0
    readiness = {
        "START placement": bool(start_state and start_state.get("placements")),
        "Opening inventory": bool(opening_rows), "Warehouse": bool(target),
        "Baseline SimulationState": baseline is not None,
    }
    st.write(" · ".join(f"{'✓' if ready else '✗'} {name}" for name, ready in readiness.items())
             + f" · ⚠ SKU zones: {coverage}%")
    if baseline is None:
        st.error("Baseline cannot be built")
        with st.expander("Диагностика baseline"):
            st.json(baseline_diagnostics)
        return

    st.markdown("### Правила PROPOSED")
    weight_zones = st.checkbox("Весовые зоны", key=f"{SESSION_PREFIX}_weight_zones")
    st.caption("На текущем этапе доступно одно правило. Остальные будут добавляться последовательно.")
    rule_config = build_weight_zone_rule_config(weight_zones)
    signature = build_comparison_signature(
        model_id=model.get("model_id"), baseline_state_id=baseline.get("simulation_state_id"),
        operational_date=operational_date, normalized_warehouse=target,
        sku_zone_rows=sku_zone_rows, rule_config=rule_config,
    )
    if st.button("Пересчитать PROPOSED", type="primary", key=f"{SESSION_PREFIX}_calculate"):
        scenario, diagnostics = build_proposed_scenario(
            model, baseline, rule_config, sku_zone_rows=sku_zone_rows,
        )
        st.session_state[f"{SESSION_PREFIX}_baseline"] = baseline
        st.session_state[f"{SESSION_PREFIX}_scenario"] = scenario
        st.session_state[f"{SESSION_PREFIX}_diagnostics"] = diagnostics
        st.session_state[f"{SESSION_PREFIX}_signature"] = signature
        st.session_state[f"{SESSION_PREFIX}_rule_config"] = rule_config

    scenario = st.session_state.get(f"{SESSION_PREFIX}_scenario")
    saved_signature = st.session_state.get(f"{SESSION_PREFIX}_signature")
    stale = bool(saved_signature and saved_signature != signature)
    if stale:
        st.warning("Настройки или исходные данные изменены. Пересчитайте PROPOSED.")
        scenario = None

    static_layer = _cached_static_layer(model, str(model.get("model_id") or model.get("source_file_hash") or ""))
    current_payload = build_simulation_dynamic_payload(model, baseline, label_settings=_LABEL_SETTINGS)
    current_html = compose_geometry_layers(static_layer, current_payload)
    left, right = st.columns(2)
    with left:
        st.markdown("### CURRENT")
        st.caption("Фактическое размещение")
        st.caption(f"CURRENT state: {_short_id(baseline.get('simulation_state_id'))}")
        components.html(current_html, height=MAP_HEIGHT, scrolling=True)
    with right:
        st.markdown("### PROPOSED")
        st.caption("Размещение по выбранным правилам")
        if scenario is None:
            st.info("Нажмите «Пересчитать PROPOSED» для построения карты.")
        elif scenario.get("status") == "blocked":
            st.error("PROPOSED не построен")
            errors = st.session_state.get(f"{SESSION_PREFIX}_diagnostics", {}).get("errors", [])
            st.json(errors or scenario.get("limitations", []))
        else:
            proposed = scenario.get("proposed_state")
            if scenario.get("status") == "partial":
                st.warning("PROPOSED построен частично.")
            else:
                st.success("PROPOSED рассчитан")
            st.caption(f"PROPOSED state: {_short_id(scenario.get('proposed_state_id'))}")
            proposed_payload = build_simulation_dynamic_payload(model, proposed, label_settings=_LABEL_SETTINGS)
            proposed_html = compose_geometry_layers(static_layer, proposed_payload)
            components.html(proposed_html, height=MAP_HEIGHT, scrolling=True)

    if scenario and scenario.get("status") in {"ready", "partial"}:
        if not weight_zones:
            st.info("Правила оптимизации выключены — PROPOSED совпадает с CURRENT.")
        _show_metrics(summarize_scenario_ui_metrics(scenario, baseline, sku_zone_rows))
        if scenario.get("status") == "partial":
            summary = scenario.get("summary", {})
            st.warning(f"Неразрешено: {summary.get('unresolved_units', 0)} · "
                       f"фиксировано: {summary.get('fixed_units', 0)}")

        scoped_rows = [dict(row) for row in outbound_rows or []
                       if (not operational_date or str(row.get("created_at") or "")[:10] == str(operational_date)[:10])
                       and (not target or normalize_warehouse(row.get("warehouse")) == target)]
        demand = build_outbound_pick_demands(scoped_rows)
        proposed = scenario.get("proposed_state")
        distance_ready = bool(demand.get("orders") and gate_state and gate_state.get("gates") and proposed)
        st.markdown("#### Пробег CURRENT / PROPOSED")
        st.write(" · ".join(("✓ CURRENT baseline", "✓ PROPOSED scenario",
                             f"{'✓' if demand.get('orders') else '✗'} РО выбранного дня",
                             f"{'✓' if gate_state and gate_state.get('gates') else '✗'} Ворота")))
        distance_signature = _hash({"model_id": model.get("model_id"), "current": baseline.get("simulation_state_id"),
                                    "proposed": scenario.get("proposed_state_id"), "demand": demand,
                                    "gate": gate_state, "zone_order": "default"})
        if st.button("Рассчитать пробег CURRENT / PROPOSED", disabled=not distance_ready,
                     key=f"{SESSION_PREFIX}_distance_calculate"):
            replay, replay_diagnostics = replay_outbound_on_simulation_states(
                model, baseline, proposed, demand, gate_state or {})
            comparison, comparison_diagnostics = compare_simulation_outbound_replay(replay) if replay else ({}, {})
            st.session_state[f"{SESSION_PREFIX}_distance_replay"] = replay
            st.session_state[f"{SESSION_PREFIX}_distance_comparison"] = comparison
            st.session_state[f"{SESSION_PREFIX}_distance_diagnostics"] = {
                "replay": replay_diagnostics, "comparison": comparison_diagnostics}
            st.session_state[f"{SESSION_PREFIX}_distance_signature"] = distance_signature
        saved_distance_signature = st.session_state.get(f"{SESSION_PREFIX}_distance_signature")
        if saved_distance_signature and saved_distance_signature != distance_signature:
            st.warning("Результат пробега устарел. Нажмите кнопку расчёта повторно.")
        elif st.session_state.get(f"{SESSION_PREFIX}_distance_comparison"):
            _show_distance_comparison(st.session_state[f"{SESSION_PREFIX}_distance_comparison"])
