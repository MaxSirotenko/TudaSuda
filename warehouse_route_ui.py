"""Read-only presentation payloads for authoritative replay routes."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import pandas as pd
import streamlit as st

ROUTE_COLORS = {"current": "#2563EB", "proposed": "#D97706", "pick": "#7C3AED", "gate": "#166534"}


def build_route_overlay(replay_order: Mapping[str, Any], graph: Mapping[str, Any], scenario: str) -> dict[str, Any]:
    """Resolve replay paths to graph coordinates without calculating a route."""
    nodes = {str(n.get("node_id")): n for n in graph.get("nodes", []) if isinstance(n, Mapping)}
    edges = {str(e.get("edge_id")): e for e in graph.get("edges", []) if isinstance(e, Mapping)}
    points: list[dict[str, Any]] = []; legs = []; unresolved = []; invalid_edges = []
    cumulative = 0.0
    raw_legs = replay_order.get("route_legs", []) or []
    for sequence, leg in enumerate(raw_legs, 1):
        leg_points = []
        for node_id in leg.get("path_node_ids", []) or []:
            node = nodes.get(str(node_id))
            if node is None:
                unresolved.append(node_id); continue
            point = {"node_id": node_id, "x": node["x"], "y": node["y"]}
            leg_points.append(point)
            if not points or points[-1]["node_id"] != node_id: points.append(point)
        for edge_id in leg.get("path_edge_ids", []) or []:
            if str(edge_id) not in edges: invalid_edges.append(edge_id)
        distance = float(leg.get("distance_m") or 0); cumulative += distance
        legs.append({"sequence": sequence, "leg_type": leg.get("leg_type"), "from_kind": leg.get("from_kind"),
                     "to_kind": leg.get("to_kind"), "distance_m": distance, "cumulative_distance_m": cumulative,
                     "path_node_ids": list(leg.get("path_node_ids", []) or []),
                     "path_edge_ids": list(leg.get("path_edge_ids", []) or []), "route_points": leg_points})
    authoritative = replay_order.get("picker_distance_m", replay_order.get("route_distance_m"))
    consistent = authoritative is not None and math.isclose(cumulative, float(authoritative), rel_tol=1e-7, abs_tol=1e-6)
    pick_events = list(replay_order.get("pick_events", replay_order.get("picks", [])) or [])
    return {"scenario": scenario, "order_key": replay_order.get("order_key"), "route_distance_m": authoritative,
            "route_points": points, "pick_stops": [{"number": i, **dict(event)} for i, event in enumerate(pick_events, 1)],
            "legs": legs, "starts_at_gate": bool(legs and legs[0].get("from_kind") == "gate"),
            "returns_to_gate": bool(replay_order.get("returned_to_gate") or legs and legs[-1].get("to_kind") == "gate"),
            "distance_consistent": consistent, "unresolved_node_ids": list(dict.fromkeys(unresolved)),
            "unresolved_edge_ids": list(dict.fromkeys(invalid_edges)),
            "visualization_ready": bool(points) and not unresolved,
            "visualization_message": "Маршрут рассчитан, но часть пути не удалось отобразить на карте." if unresolved else None,
            "style": {"route_color": ROUTE_COLORS.get(scenario, ROUTE_COLORS["current"]), "pick_color": ROUTE_COLORS["pick"], "gate_color": ROUTE_COLORS["gate"]}}


def select_replay_order(replay: Mapping[str, Any], scenario: str, order_key: str) -> Mapping[str, Any] | None:
    for order in replay.get(scenario, {}).get("orders", []) or []:
        if str(order.get("order_key")) == str(order_key): return order
    return None


def render_replay_routes(replay: Mapping[str, Any]) -> None:
    """Render CURRENT/PROPOSED from the graph and legs retained by authoritative replay."""
    graph = replay.get("route_graph") or {}
    current = replay.get("current", {}).get("orders", []) or []
    proposed = replay.get("proposed", {}).get("orders", []) or []
    keys = [str(order.get("order_key")) for order in current if order.get("order_key") is not None]
    st.markdown("### Маршрут выбранного РО")
    if not keys:
        st.info("Нет рассчитанных РО. Загрузите РО в разделе «Данные» и рассчитайте пробег.")
        return
    selected = st.selectbox("РО для просмотра", keys, key="route_ui_selected_ro")
    columns = st.columns(2)
    for column, scenario, orders in zip(columns, ("current", "proposed"), (current, proposed)):
        order = next((item for item in orders if str(item.get("order_key")) == selected), None)
        with column:
            st.markdown(f"#### {scenario.upper()}")
            if order is None:
                st.warning("Для этого сценария РО отсутствует.")
                continue
            overlay = build_route_overlay(order, graph, scenario)
            st.metric("Пробег комплектовщика, м", overlay["route_distance_m"])
            if overlay["visualization_message"]:
                st.warning("Маршрут рассчитан, но часть пути не удалось отобразить.")
            if overlay["route_points"]:
                st.dataframe(pd.DataFrame(overlay["route_points"]), hide_index=True,
                             use_container_width=True)
            st.caption(f"Точек отбора: {len(overlay['pick_stops'])}; цвет маршрута: {overlay['style']['route_color']}")
