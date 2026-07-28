"""Benchmark-only Streamlit host for one or two production warehouse maps."""

from __future__ import annotations

import time

import streamlit as st
import streamlit.components.v1 as components

import virtual_warehouse_app as production
from warehouse_browser_benchmark import instrument_map_html, validate_query_params
from warehouse_geometry_render_layers import compose_geometry_layers
from warehouse_performance_benchmark import generate_synthetic_dataset, load_benchmark_dataset


def build_map_html(config: dict) -> tuple[str, dict]:
    started = time.perf_counter()
    model, state, source, warnings, _ = load_benchmark_dataset(
        str(config["dataset"]), int(config["cells"]), int(config["occupied_cells"]), int(config["placements"]),
    )
    settings = production.load_render_settings()
    settings.update({"edit_mode": False, "selected_cell_key": "", "selected_row_number": ""})
    static_token = production.get_geometry_static_revision_token(model) if source == "current" else ("browser-synthetic",)
    dynamic_token = production.get_geometry_dynamic_revision_token(model) if source == "current" else ("browser-synthetic",)
    static = production.build_geometry_static_layer_cached(
        model, static_token, settings, 18.0, True, production.GEOMETRY_STATIC_CACHE_VERSION)
    dynamic = production.build_geometry_dynamic_layer_cached(
        model, state, dynamic_token, settings, production.GEOMETRY_DYNAMIC_CACHE_VERSION)
    html = compose_geometry_layers(static, dynamic)
    return html, {"source": source, "warnings": [type(w).__name__ if not isinstance(w, str) else "dataset warning" for w in warnings],
                  "rows": len(model.get("rows", [])), "cells": len(model.get("cells", [])),
                  "placements": len(state.get("placements", [])), "python_generation_ms": (time.perf_counter() - started) * 1000}


def main() -> None:
    st.set_page_config(page_title="Warehouse browser benchmark", layout="wide")
    st.title("Warehouse browser benchmark")
    try:
        config = validate_query_params(dict(st.query_params))
        html, metadata = build_map_html(config)
    except (ValueError, RuntimeError) as exc:
        st.error(f"Invalid benchmark scenario: {exc}")
        return
    maps = int(config["maps"]); scenario = f"{config['dataset']}_{'single' if maps == 1 else 'double'}"
    st.write({"dataset": metadata["source"], "maps": maps, "rows": metadata["rows"],
              "cells": metadata["cells"], "placements": metadata["placements"],
              "combined_python_html_bytes": len(html.encode("utf-8")) * maps,
              "python_generation_ms": round(metadata["python_generation_ms"], 3), "browser_metrics": "waiting"})
    columns = st.columns(2) if maps == 2 else [st.container()]
    for index, column in enumerate(columns):
        measured = instrument_map_html(html, scenario_id=scenario, map_index=index,
                                       cells_count=metadata["cells"], rows_count=metadata["rows"],
                                       placements_count=metadata["placements"])
        with column:
            components.html(measured, height=980, scrolling=True)


if __name__ == "__main__":
    main()
