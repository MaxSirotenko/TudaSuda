"""Repeatable, read-only baseline for the warehouse map rendering path.

The report deliberately contains aggregates only.  Product data, cell addresses,
tooltips and persisted payloads never leave the in-memory benchmark run.
"""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import streamlit

import virtual_warehouse_app as app
import warehouse_geometry_model as geometry_model
import warehouse_inventory_placement as placement
import warehouse_revisions as revisions
import warehouse_state_cache as state_cache
import warehouse_factual_data as factual_data
import warehouse_physical_graph as physical_graph
from warehouse_application_rerun import execute_warm_map_rerun
from warehouse_perf_diagnostics import capture_io_reads
from warehouse_geometry_render_layers import compose_geometry_layers, safe_json_dumps

SCHEMA_VERSION = 1
DEFAULT_OUTPUT_DIR = Path("data/performance_benchmarks")
PERSISTED_STATE_PATHS = (
    Path("data/last_import/warehouse_model.json"),
    Path("data/last_import/placements.json"),
    Path("data/last_import/receipts.json"),
    Path("data/last_import/outbound_orders.json"),
    Path("data/last_import/outbound_execution_state.json"),
    Path("data/last_import/outbound_execution_log.json"),
    Path("data/last_import/data_revisions.json"),
    Path("data/last_import/placement_diagnostics.json"),
)

# Advisory only: these are diagnostic heuristics, never performance gates.
DOM_SIZE_WARNING_BYTES = 8 * 1024 * 1024
DOM_SIZE_WARM_PYTHON_MS = 50.0
DOMINANT_COLD_SHARE = 0.45
SIGNIFICANT_COLD_SHARE = 0.30


def _timed(call: Callable[[], Any], memory: bool = False) -> tuple[Any, float, int]:
    if memory:
        tracemalloc.start()
    started = time.perf_counter_ns()
    try:
        value = call()
    finally:
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        peak = tracemalloc.get_traced_memory()[1] if memory else 0
        if memory:
            tracemalloc.stop()
    return value, elapsed, peak


def _summary(values: list[float]) -> dict[str, Any]:
    return {"status": "ok", "iterations": len(values), "min_ms": min(values),
            "median_ms": statistics.median(values), "max_ms": max(values)}


def generate_synthetic_dataset(cells_count: int = 16_000, occupied_cells: int = 500,
                               placements_count: int = 700) -> tuple[dict, dict]:
    """Create a deterministic representative geometry and placement state."""
    cells_count = int(cells_count)
    occupied_cells = int(occupied_cells)
    placements_count = int(placements_count)
    if cells_count < 1:
        raise ValueError("cells_count must be at least 1")
    if not 0 <= occupied_cells <= cells_count:
        raise ValueError("occupied_cells must be between 0 and cells_count")
    if placements_count < occupied_cells:
        raise ValueError("placements_count must be at least occupied_cells")
    if occupied_cells == 0 and placements_count:
        raise ValueError("placements_count must be 0 when occupied_cells is 0")
    row_count = min(200, cells_count)
    per_row, remainder = divmod(cells_count, row_count)
    cells, rows, offset = [], [], 0
    for row_index in range(row_count):
        count = per_row + (row_index < remainder)
        storage_type = "deep_lane" if row_index % 5 == 4 else "normal"
        y = row_index * 2.0 + 3.0
        rows.append({"row_number": str(row_index + 1), "x_min": 0.0, "x_max": float(count),
                     "y_min": y, "y_max": y + 1.0, "cells_count": count,
                     "storage_type": storage_type})
        for number in range(count):
            cells.append({"code": f"C{offset + number + 1}", "row_number": str(row_index + 1),
                          "cell_number": str(number + 1), "tier": "1", "capacity_pallets": 2,
                          "storage_type": storage_type, "deep_lane_width": 2 if storage_type == "deep_lane" else 1,
                          "x_min": float(number), "x_max": float(number + 1), "x_center": number + .5,
                          "y_min": y, "y_max": y + 1.0, "y_center": y + .5,
                          "weight_zone": ("heavy", "medium", "light")[row_index % 3],
                          "physical_slots": [], "cell_direction": "bottom_to_top", "volume_m3": 1.0})
        offset += count
    aisles = [{"x_min": float(index), "x_max": float(index) + .8, "row_from": str(index + 1),
               "row_to": str(index + 2), "aisle_width_m": .8} for index in range(0, row_count - 1, 20)]
    cross = [{"x_min": 20.0, "x_max": 22.0, "y_min": row["y_min"], "y_max": row["y_max"],
              "row_number": row["row_number"], "after_cell_number": "20", "width_cells": 2,
              "width_m": 2.0} for row in rows[::20]]
    width, bottom, top = float(max(r["cells_count"] for r in rows)), 0.0, rows[-1]["y_max"] + 2.0
    roads = [{"road_type": "bottom", "x_min": 0.0, "x_max": width, "y_min": bottom, "y_max": 2.0, "width_m": 2.0},
             {"road_type": "top", "x_min": 0.0, "x_max": width, "y_min": top, "y_max": top + 2.0, "width_m": 2.0}]
    model = {"model_type": "excel_rows_cells_aisles_geometry", "model_id": "benchmark-synthetic-v1",
             "source_file_hash": "benchmark-synthetic", "settings": {"top_road_width_m": 2},
             "cells": cells, "base_cells": copy.deepcopy(cells), "rows": rows, "aisles": aisles,
             "cross_aisles": cross, "roads": roads}
    placements = []
    for index in range(placements_count):
        cell_index = index % occupied_cells
        cell = cells[cell_index]
        ordinal = index // max(1, occupied_cells)
        quantity = 3.0 if cell_index % 31 == 0 else (2.0 if cell_index % 7 == 0 else .5)
        placements.append({"cell_key": f"{cell['row_number']}|{cell['cell_number']}|1",
                           "row_number": cell["row_number"], "cell_number": cell["cell_number"], "tier": "1",
                           "occupied_capacity_pallets": quantity, "quantity": quantity,
                           "sku_key": f"BENCH-{cell_index}-{ordinal}", "sku_code": f"BENCH-{cell_index}-{ordinal}",
                           "sku_name": "Synthetic item", "characteristic": "Synthetic characteristic",
                           "characteristic_name": "Synthetic characteristic", "source": "receipt",
                           "receipt_numbers": [f"R-{index % 11}"], "receipt_line_ids": [f"L-{index}"],
                           "placement_reason_text": "deterministic benchmark placement",
                           "outbound_status": "reserved" if index % 17 == 0 else "",
                           "outbound_order_id": f"O-{index % 5}" if index % 17 == 0 else "",
                           "calculated_zone": cell["weight_zone"]})
    state = {"model_id": model["model_id"], "source_file_hash": model["source_file_hash"],
             "placements": placements, "unplaced_inventory": [], "journal": [], "settings": {}}
    return model, state


def load_benchmark_dataset(mode: str, cells: int, occupied_cells: int, placements_count: int,
                           loader: Callable[[], dict | None] | None = None) -> tuple[dict, dict, str, list[str], float]:
    """Load the active model through its production loader, or build a fallback.

    Resolving the default loader at call time also keeps this boundary injectable
    for read-only checks without retaining a stale imported function reference.
    """
    loader = loader or geometry_model.load_geometry_model
    warnings: list[str] = []
    if mode != "synthetic":
        try:
            model, elapsed, _ = _timed(loader)
            if model and isinstance(model.get("cells"), list) and model["cells"]:
                state, warning = placement.load_placement_state(model)
                if warning:
                    warnings.append(warning)
                return model, state, "current", warnings, elapsed
            if mode == "current":
                raise RuntimeError("Applied geometry model is not available")
            warnings.append("Applied geometry model is not available; using deterministic synthetic fallback.")
        except Exception as exc:
            if mode == "current":
                raise
            warnings.append(f"Applied geometry model could not be loaded; synthetic fallback used ({type(exc).__name__}).")
    (model, state), elapsed, _ = _timed(lambda: generate_synthetic_dataset(cells, occupied_cells, placements_count))
    return model, state, "synthetic" if mode == "synthetic" else "synthetic_fallback", warnings, elapsed


def persisted_state_signatures(paths: tuple[Path, ...] = PERSISTED_STATE_PATHS) -> dict[str, dict[str, Any]]:
    result = {}
    for path in paths:
        try:
            stat = path.stat(); signature = {"exists": True, "st_mtime_ns": stat.st_mtime_ns, "st_size": stat.st_size}
        except FileNotFoundError:
            signature = {"exists": False, "st_mtime_ns": 0, "st_size": 0}
        result[path.name] = signature
    return result


@contextmanager
def _count_app_calls() -> Iterator[dict[str, int]]:
    names = ("build_geometry_static_layer", "build_geometry_dynamic_layer_direct",
             "load_pre_placement_snapshot", "build_outbound_tooltips_by_cell")
    originals, counts = {}, {name: 0 for name in names}
    for name in names:
        original = getattr(app, name); originals[name] = original
        def wrapper(*args, __name=name, __original=original, **kwargs):
            counts[__name] += 1
            return __original(*args, **kwargs)
        setattr(app, name, wrapper)
    try:
        yield counts
    finally:
        for name, original in originals.items():
            setattr(app, name, original)


@contextmanager
def _count_expensive_domain_calls() -> Iterator[dict[str, int]]:
    targets = (
        (physical_graph, "build_physical_warehouse_graph", "physical_graph_rebuilds"),
        (factual_data, "build_monthly_data_readiness", "factual_readiness_calls"),
        (factual_data, "load_effective_rows", "factual_effective_view_calls"),
    )
    originals, counts = [], {label: 0 for _, _, label in targets}
    for module, name, label in targets:
        original = getattr(module, name)
        originals.append((module, name, original))
        def wrapper(*args, __original=original, __label=label, **kwargs):
            counts[__label] += 1
            return __original(*args, **kwargs)
        setattr(module, name, wrapper)
    try:
        yield counts
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


def _import_scenario() -> dict[str, Any]:
    started = time.perf_counter_ns()
    process = subprocess.run([sys.executable, "-c", "import virtual_warehouse_app"], capture_output=True)
    return {"status": "ok" if process.returncode == 0 else "failed",
            "wall_time_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "return_code": process.returncode, "traceback_present": b"Traceback" in process.stderr,
            "stdout_bytes": len(process.stdout), "stderr_bytes": len(process.stderr)}


_IMPORTTIME_LINE = re.compile(r"^import time:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*$")
STARTUP_MODULES = ("openpyxl",)


def parse_importtime_output(stderr: str, top_count: int = 20) -> dict[str, Any]:
    """Parse aggregate-only importtime rows without retaining stderr or paths."""
    modules = []
    for line in stderr.splitlines():
        match = _IMPORTTIME_LINE.match(line)
        if not match:
            continue
        name = match.group(3).strip()
        if name == "module name":
            continue
        name = name.lstrip(".")
        if not name or "/" in name or "\\" in name:
            continue
        modules.append({"module_name": name, "self_time_us": int(match.group(1)),
                        "cumulative_time_us": int(match.group(2))})
    return {
        "modules_count": len(modules),
        "accounted_self_time_us": sum(row["self_time_us"] for row in modules),
        "accounted_cumulative_time_us": sum(row["cumulative_time_us"] for row in modules),
        "top_cumulative": sorted(modules, key=lambda row: row["cumulative_time_us"], reverse=True)[:top_count],
        "top_self": sorted(modules, key=lambda row: row["self_time_us"], reverse=True)[:top_count],
    }


def _import_breakdown_scenario() -> dict[str, Any]:
    started = time.perf_counter_ns()
    process = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import virtual_warehouse_app"],
        capture_output=True, text=True,
    )
    traceback_present = "Traceback (most recent call last)" in process.stderr
    parsed = parse_importtime_output(process.stderr)
    status = "failed" if process.returncode != 0 and traceback_present else ("ok" if parsed["modules_count"] else "warning")
    result = {"status": status, "wall_time_ms": (time.perf_counter_ns() - started) / 1_000_000,
              "return_code": process.returncode, "traceback_present": traceback_present, **parsed}
    if not parsed["modules_count"]:
        result["warning"] = "Python importtime output could not be parsed."
    elif process.returncode != 0:
        result["warning"] = "Import profiling subprocess returned a non-zero code without a traceback."
    return result


def _startup_modules_scenario() -> dict[str, Any]:
    code = "import json,sys,virtual_warehouse_app; print(json.dumps({name:name in sys.modules for name in " + repr(STARTUP_MODULES) + "}))"
    process = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    try:
        loaded = json.loads(process.stdout.strip()) if process.returncode == 0 else {}
    except json.JSONDecodeError:
        loaded = {}
    return {"status": "ok" if len(loaded) == len(STARTUP_MODULES) else "warning",
            "return_code": process.returncode, "loaded": {name: bool(loaded.get(name)) for name in STARTUP_MODULES}}


def _repeated_import_scenario(iterations: int) -> dict[str, Any]:
    measurements = [_import_scenario()["wall_time_ms"] for _ in range(max(1, iterations))]
    return _summary(measurements)


def run_benchmark(mode: str = "current-or-synthetic", cells: int = 16_000,
                  occupied_cells: int = 500, placements_count: int = 700,
                  warm_iterations: int = 3, import_iterations: int = 5) -> dict[str, Any]:
    """Run all measurements without invoking any production mutation API."""
    if mode not in {"current", "synthetic", "current-or-synthetic"}:
        raise ValueError(f"Unsupported mode: {mode}")
    before = persisted_state_signatures()
    model, placement_state, source_mode, warnings, load_ms = load_benchmark_dataset(
        mode, cells, occupied_cells, placements_count)
    scenarios: dict[str, dict[str, Any]] = {"application_import": _import_scenario()}
    scenarios["application_import_repeated"] = _repeated_import_scenario(import_iterations)
    scenarios["application_import_breakdown"] = _import_breakdown_scenario()
    scenarios["startup_modules"] = _startup_modules_scenario()
    if scenarios["application_import_breakdown"].get("warning"):
        warnings.append(scenarios["application_import_breakdown"]["warning"])
    scenarios["model_loading"] = {"status": "ok", "wall_time_ms": load_ms,
        "operation": "direct_load" if source_mode == "current" else "synthetic_generation",
        "cells_count": len(model.get("cells", [])), "rows_count": len(model.get("rows", [])),
        "aisles_count": len(model.get("aisles", [])), "cross_aisles_count": len(model.get("cross_aisles", []))}
    model_id = revisions.resolve_model_id(model)
    tokens, token_ms, _ = _timed(lambda: (
        revisions.get_revision_token(model_id, app.GEOMETRY_STATIC_DOMAINS),
        revisions.get_revision_token(model_id, app.GEOMETRY_DYNAMIC_DOMAINS),
        revisions.get_revision_token(model_id, ("placements", "inventory"))))
    static_token, dynamic_token, placement_token = tokens
    scenarios["revision_tokens"] = {"status": "ok", "wall_time_ms": token_ms,
        "static_token_parts": len(static_token), "dynamic_token_parts": len(dynamic_token),
        "placement_state_token_parts": len(placement_token)}

    if source_mode == "current":
        original = placement.load_placement_state; reads = 0
        def counted(value):
            nonlocal reads; reads += 1; return original(value)
        state_cache._load_placement_state_cached.clear()
        placement.load_placement_state = counted
        try:
            _, direct_ms, _ = _timed(lambda: counted(model))
            _, first_ms, _ = _timed(lambda: state_cache.load_placement_state_cached(model))
            _, warm_read_ms, _ = _timed(lambda: state_cache.load_placement_state_cached(model))
        finally:
            placement.load_placement_state = original
        scenarios["placement_state_read"] = {"status": "ok", "direct_ms": direct_ms,
            "cached_cold_ms": first_ms, "cached_warm_ms": warm_read_ms, "direct_loader_call_count": reads}
    else:
        scenarios["placement_state_read"] = {"status": "skipped", "reason": "Synthetic state remains in memory and is never persisted."}

    settings = {"show_cell_labels": True, "colors": {}}
    app.build_geometry_static_layer_cached.clear(); app.build_geometry_dynamic_layer_cached.clear()
    with _count_app_calls() as counts:
        static_layer, static_ms, static_peak = _timed(lambda: app.build_geometry_static_layer_cached(
            model, static_token, settings, 18.0, True, app.GEOMETRY_STATIC_CACHE_VERSION), True)
        scenarios["static_cold"] = {"status": "ok", "wall_time_ms": static_ms,
            "python_tracemalloc_peak_bytes": static_peak, "static_html_bytes": len(static_layer.encode()),
            "data_cell_key_count": static_layer.count("data-cell-key="),
            "approximate_svg_elements": sum(static_layer.count(f"<{tag}") for tag in ("rect", "line", "text", "path", "polygon", "circle")),
            "underlying_builder_call_count": counts["build_geometry_static_layer"]}
        warm = []
        for _ in range(max(1, warm_iterations)):
            _, elapsed, _ = _timed(lambda: app.build_geometry_static_layer_cached(model, static_token, settings, 18.0, True, app.GEOMETRY_STATIC_CACHE_VERSION)); warm.append(elapsed)
        scenarios["static_warm"] = _summary(warm) | {"underlying_builder_call_count": counts["build_geometry_static_layer"]}

        dynamic_payload, dynamic_ms, dynamic_peak = _timed(lambda: app.build_geometry_dynamic_layer_cached(
            model, placement_state, dynamic_token, settings, app.GEOMETRY_DYNAMIC_CACHE_VERSION), True)
        occupied_keys = {p.get("cell_key") for p in placement_state.get("placements", []) if p.get("cell_key")}
        scenarios["dynamic_cold"] = {"status": "ok", "wall_time_ms": dynamic_ms,
            "python_tracemalloc_peak_bytes": dynamic_peak, "placements_count": len(placement_state.get("placements", [])),
            "occupied_cell_keys_count": len(occupied_keys), "interesting_cell_keys_count": len(dynamic_payload),
            "dynamic_cells_count": len(dynamic_payload), "dynamic_payload_bytes": len(safe_json_dumps(dynamic_payload).encode()),
            "builder_mode": "direct_state", "full_model_enrichment": False, "full_model_deepcopy": False}
        dynamic_warm = []
        for _ in range(max(1, warm_iterations)):
            _, elapsed, _ = _timed(lambda: app.build_geometry_dynamic_layer_cached(model, placement_state, dynamic_token, settings, app.GEOMETRY_DYNAMIC_CACHE_VERSION)); dynamic_warm.append(elapsed)
        scenarios["dynamic_warm"] = _summary(dynamic_warm) | {
            "direct_builder_call_count": counts["build_geometry_dynamic_layer_direct"],
            "snapshot_loader_call_count": counts["load_pre_placement_snapshot"],
            "outbound_projection_call_count": counts["build_outbound_tooltips_by_cell"]}
        final_html, compose_ms, compose_peak = _timed(lambda: compose_geometry_layers(static_layer, dynamic_payload), True)
        static_bytes, dynamic_bytes, final_bytes = len(static_layer.encode()), len(safe_json_dumps(dynamic_payload).encode()), len(final_html.encode())
        scenarios["compose_layers"] = {"status": "ok", "wall_time_ms": compose_ms,
            "python_tracemalloc_peak_bytes": compose_peak, "static_size_bytes": static_bytes,
            "dynamic_size_bytes": dynamic_bytes, "final_html_size_bytes": final_bytes,
            "dynamic_to_static_ratio": dynamic_bytes / static_bytes if static_bytes else 0,
            "final_to_dynamic_ratio": final_bytes / dynamic_bytes if dynamic_bytes else 0}
        baseline = counts.copy()
        with capture_io_reads() as render_reads:
            no_op_html, no_op_ms, no_op_peak = _timed(lambda: compose_geometry_layers(
                app.build_geometry_static_layer_cached(model, static_token, settings, 18., True, app.GEOMETRY_STATIC_CACHE_VERSION),
                app.build_geometry_dynamic_layer_cached(model, placement_state, dynamic_token, settings, app.GEOMETRY_DYNAMIC_CACHE_VERSION)), True)
        scenarios["render_no_change"] = {"status": "ok", "wall_time_ms": no_op_ms,
            "python_tracemalloc_peak_bytes": no_op_peak, "generated_payload_bytes": len(no_op_html.encode()),
            "artifact_reads": render_reads["artifact_reads"], "file_reads": render_reads["file_reads"],
            "additional_static_builder_calls": counts["build_geometry_static_layer"] - baseline["build_geometry_static_layer"],
            "additional_dynamic_builder_calls": counts["build_geometry_dynamic_layer_direct"] - baseline["build_geometry_dynamic_layer_direct"],
            "additional_snapshot_reads": counts["load_pre_placement_snapshot"] - baseline["load_pre_placement_snapshot"],
            "additional_direct_placement_disk_reads": render_reads["reader:json"],
            "heavy_function_calls": sum(counts[name] - baseline[name] for name in baseline)}
        # Compatibility name retained for report consumers written before PR #189.
        scenarios["no_change_rerender"] = dict(scenarios["render_no_change"])
        baseline = counts.copy()
        with _count_expensive_domain_calls() as domain_calls, capture_io_reads() as application_reads:
            application_result, application_ms, application_peak = _timed(lambda: execute_warm_map_rerun(
                    model=model, placement_state=placement_state, settings=settings,
                    static_token=static_token, dynamic_token=dynamic_token,
                    static_loader=app.build_geometry_static_layer_cached,
                    dynamic_loader=app.build_geometry_dynamic_layer_cached,
                    composer=compose_geometry_layers,
                    static_version=app.GEOMETRY_STATIC_CACHE_VERSION,
                    dynamic_version=app.GEOMETRY_DYNAMIC_CACHE_VERSION,
                    state_loader=(state_cache.load_placement_state_cached if source_mode == "current" else
                                  lambda _model: (placement_state, None)),
                ), True)
        scenarios["application_warm_noop"] = {
            "status": "ok", "wall_time_ms": application_ms,
            "python_tracemalloc_peak_bytes": application_peak, **application_result,
            "file_reads": application_reads["file_reads"],
            "artifact_reads": application_reads["artifact_reads"],
            "static_rebuilds": counts["build_geometry_static_layer"] - baseline["build_geometry_static_layer"],
            "dynamic_rebuilds": counts["build_geometry_dynamic_layer_direct"] - baseline["build_geometry_dynamic_layer_direct"],
            **domain_calls,
        }
        baseline = counts.copy(); changed_dynamic = dynamic_token + ("benchmark-placement-change",)
        compose_geometry_layers(app.build_geometry_static_layer_cached(model, static_token, settings, 18., True, app.GEOMETRY_STATIC_CACHE_VERSION),
                                app.build_geometry_dynamic_layer_cached(model, placement_state, changed_dynamic, settings, app.GEOMETRY_DYNAMIC_CACHE_VERSION))
        scenarios["placement_only_change"] = {"status": "ok", "static_builder_calls": counts["build_geometry_static_layer"] - baseline["build_geometry_static_layer"], "dynamic_builder_calls": counts["build_geometry_dynamic_layer_direct"] - baseline["build_geometry_dynamic_layer_direct"], "compose_calls": 1}
        baseline = counts.copy(); changed_static = static_token + ("benchmark-geometry-change",); changed_both = dynamic_token + ("benchmark-geometry-change",)
        compose_geometry_layers(app.build_geometry_static_layer_cached(model, changed_static, settings, 18., True, app.GEOMETRY_STATIC_CACHE_VERSION), app.build_geometry_dynamic_layer_cached(model, placement_state, changed_both, settings, app.GEOMETRY_DYNAMIC_CACHE_VERSION))
        scenarios["geometry_change"] = {"status": "ok", "static_builder_calls": counts["build_geometry_static_layer"] - baseline["build_geometry_static_layer"], "dynamic_builder_calls": counts["build_geometry_dynamic_layer_direct"] - baseline["build_geometry_dynamic_layer_direct"], "compose_calls": 1}
        baseline = counts.copy(); copied_settings = copy.deepcopy(settings); copied_settings["show_cell_labels"] = False
        compose_geometry_layers(app.build_geometry_static_layer_cached(model, static_token + ("benchmark-settings-change",), copied_settings, 18., True, app.GEOMETRY_STATIC_CACHE_VERSION), app.build_geometry_dynamic_layer_cached(model, placement_state, dynamic_token + ("benchmark-settings-change",), copied_settings, app.GEOMETRY_DYNAMIC_CACHE_VERSION))
        scenarios["render_settings_change"] = {"status": "ok", "static_builder_calls": counts["build_geometry_static_layer"] - baseline["build_geometry_static_layer"], "dynamic_builder_calls": counts["build_geometry_dynamic_layer_direct"] - baseline["build_geometry_dynamic_layer_direct"], "user_settings_unchanged": settings["show_cell_labels"] is True}

    dataset = {"model_label": "active applied model" if source_mode == "current" else "deterministic synthetic model",
               "cells_count": len(model.get("cells", [])), "rows_count": len(model.get("rows", [])),
               "placements_count": len(placement_state.get("placements", [])), "occupied_cells_count": len(occupied_keys)}
    payload_sizes = {"static_html_bytes": static_bytes, "dynamic_payload_bytes": dynamic_bytes,
                     "final_html_bytes": final_bytes, "static_bytes_per_cell": static_bytes / dataset["cells_count"],
                     "dynamic_entries_per_occupied_cell": len(dynamic_payload) / len(occupied_keys) if occupied_keys else 0,
                     "data_cell_key_count": static_layer.count("data-cell-key=")}
    result = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "git": _git_info(), "environment": {"python": platform.python_version(), "streamlit": streamlit.__version__,
              "platform": platform.platform(), "source_mode": source_mode}, "dataset": dataset, "scenarios": scenarios,
              "cache_assertions": _cache_assertions(scenarios), "payload_sizes": payload_sizes,
              "bottleneck_analysis": {}, "warnings": warnings, "persisted_state_unchanged": True,
              "changed_persisted_files": []}
    result["bottleneck_analysis"] = analyze_benchmark_bottleneck(result)
    after = persisted_state_signatures()
    changed = [name for name in before if before[name] != after[name]]
    result["persisted_state_unchanged"] = not changed; result["changed_persisted_files"] = changed
    if changed:
        result["warnings"].append("Persisted state changed during benchmark: " + ", ".join(changed))
    return result


def _cache_assertions(s: dict[str, dict[str, Any]]) -> dict[str, bool]:
    return {"static_warm_hit": s["static_warm"]["underlying_builder_call_count"] == 1,
            "dynamic_warm_hit": s["dynamic_warm"]["direct_builder_call_count"] == 1,
            "dynamic_warm_snapshot_hit": s["dynamic_warm"]["snapshot_loader_call_count"] <= 1,
            "dynamic_warm_outbound_projection_hit": s["dynamic_warm"]["outbound_projection_call_count"] <= 1,
            "no_change_hit": s["no_change_rerender"]["additional_static_builder_calls"] == 0 and s["no_change_rerender"]["additional_dynamic_builder_calls"] == 0,
            "placement_change_static_hit": s["placement_only_change"]["static_builder_calls"] == 0 and s["placement_only_change"]["dynamic_builder_calls"] == 1,
            "geometry_change_misses_both": s["geometry_change"]["static_builder_calls"] == 1 and s["geometry_change"]["dynamic_builder_calls"] == 1,
            "settings_change_misses_both": s["render_settings_change"]["static_builder_calls"] == 1 and s["render_settings_change"]["dynamic_builder_calls"] == 1}


def _git_info() -> dict[str, str]:
    def command(*args: str) -> str:
        try: return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError): return "unknown"
    return {"commit": command("rev-parse", "HEAD"), "branch": command("branch", "--show-current")}


def analyze_benchmark_bottleneck(result: dict[str, Any]) -> dict[str, Any]:
    scenarios = result.get("scenarios", {})
    keys = {"startup_import": ("application_import", "wall_time_ms"), "disk_state_loading": ("placement_state_read", "direct_ms"),
            "static_svg_generation": ("static_cold", "wall_time_ms"), "dynamic_payload_generation": ("dynamic_cold", "wall_time_ms"),
            "layer_composition": ("compose_layers", "wall_time_ms")}
    measured = {name: scenarios.get(section, {}).get(field) for name, (section, field) in keys.items()}
    measured = {name: float(value) for name, value in measured.items() if isinstance(value, (int, float)) and value >= 0}
    if len(measured) < 3 or sum(measured.values()) <= 0:
        primary, share, confidence = "insufficient_data", 0, "low"
    else:
        total = sum(measured.values()); primary = max(measured, key=measured.get); share = measured[primary] / total
        warm_python = max(float(scenarios.get("static_warm", {}).get("median_ms", 0)), float(scenarios.get("dynamic_warm", {}).get("median_ms", 0)))
        final_size = result.get("payload_sizes", {}).get("final_html_bytes", 0)
        if final_size >= DOM_SIZE_WARNING_BYTES and warm_python <= DOM_SIZE_WARM_PYTHON_MS:
            primary, confidence = "final_html_dom_size", "high"
        elif share >= DOMINANT_COLD_SHARE:
            confidence = "high"
        elif share >= SIGNIFICANT_COLD_SHARE:
            confidence = "medium"
        else:
            primary, confidence = "no_dominant_python_bottleneck", "medium"
    recommendations = {
        "startup_import": "Split virtual_warehouse_app.py and evaluate lazy imports in a focused PR.",
        "disk_state_loading": "Introduce a repository boundary and separately evaluate SQLite with measured reads.",
        "static_svg_generation": "Evaluate fewer SVG/DOM elements or progressive geometry detail.",
        "dynamic_payload_generation": "Profile direct dynamic indexes and outbound projection.",
        "layer_composition": "Evaluate avoiding reconstruction of the complete final HTML on dynamic changes.",
        "final_html_dom_size": "Measure browser render/DOM cost and evaluate progressive detail.",
        "no_dominant_python_bottleneck": "Do not adopt SQLite automatically; measure browser DOM-render latency next.",
        "insufficient_data": "Collect a complete successful benchmark before choosing an architectural change.",
    }
    return {"primary_bottleneck": primary, "evidence": [f"{name}: {value:.3f} ms" for name, value in measured.items()],
            "share_of_measured_cold_path": round(share, 4), "confidence": confidence,
            "recommended_next_step": recommendations[primary],
            "rejected_next_steps": [] if primary == "disk_state_loading" else ["SQLite/repository migration without a measured disk bottleneck"]}


def render_markdown_report(result: dict[str, Any]) -> str:
    s, d, p, a = result["scenarios"], result["dataset"], result["payload_sizes"], result["bottleneck_analysis"]
    mib = lambda value: value / 1024 / 1024
    return f"""# Warehouse performance baseline

## 1. Environment
- Created: {result['created_at']}
- Python: {result['environment']['python']}; Streamlit: {result['environment']['streamlit']}
- Platform: {result['environment']['platform']}

## 2. Data source
- Mode: **{result['environment']['source_mode']}**
- Label: {d['model_label']}

## 3. Model size
- Cells: {d['cells_count']}; rows: {d['rows_count']}; occupied cells: {d['occupied_cells_count']}; placements: {d['placements_count']}

## 4. Cold operations
- App import: {s['application_import']['wall_time_ms']:.3f} ms
- Model load/generation: {s['model_loading']['wall_time_ms']:.3f} ms
- Static SVG: {s['static_cold']['wall_time_ms']:.3f} ms; tracemalloc peak {s['static_cold']['python_tracemalloc_peak_bytes']} bytes
- Dynamic payload: {s['dynamic_cold']['wall_time_ms']:.3f} ms; tracemalloc peak {s['dynamic_cold']['python_tracemalloc_peak_bytes']} bytes
- Layer composition: {s['compose_layers']['wall_time_ms']:.3f} ms; tracemalloc peak {s['compose_layers']['python_tracemalloc_peak_bytes']} bytes

## Разбивка холодного импорта
- Repeated app import min/median/max: {s['application_import_repeated']['min_ms']:.3f}/{s['application_import_repeated']['median_ms']:.3f}/{s['application_import_repeated']['max_ms']:.3f} ms ({s['application_import_repeated']['iterations']} subprocesses)

| Модуль | Self, мс | Cumulative, мс |
|---|---:|---:|
{chr(10).join(f"| {row['module_name']} | {row['self_time_us'] / 1000:.3f} | {row['cumulative_time_us'] / 1000:.3f} |" for row in s['application_import_breakdown']['top_cumulative'])}

## 5. Warm operations
- Static: min/median/max {s['static_warm']['min_ms']:.3f}/{s['static_warm']['median_ms']:.3f}/{s['static_warm']['max_ms']:.3f} ms ({s['static_warm']['iterations']} iterations)
- Dynamic: min/median/max {s['dynamic_warm']['min_ms']:.3f}/{s['dynamic_warm']['median_ms']:.3f}/{s['dynamic_warm']['max_ms']:.3f} ms ({s['dynamic_warm']['iterations']} iterations)

## 6. Cache verification
""" + "\n".join(f"- {name}: **{'PASS' if value else 'FAIL'}**" for name, value in result["cache_assertions"].items()) + f"""

## 7. Static/dynamic/final HTML size
- Static: {p['static_html_bytes']} bytes ({mib(p['static_html_bytes']):.2f} MiB)
- Dynamic: {p['dynamic_payload_bytes']} bytes ({mib(p['dynamic_payload_bytes']):.2f} MiB)
- Final: {p['final_html_bytes']} bytes ({mib(p['final_html_bytes']):.2f} MiB)

## 8. Timing distribution
- Cold-path evidence: {'; '.join(a['evidence'])}
- Dominant share: {a['share_of_measured_cold_path']:.1%}

## 9. Probable bottleneck
- **{a['primary_bottleneck']}** (confidence: {a['confidence']})

## 10. Recommended next PR
{a['recommended_next_step']}

## 11. Measurement limitations
- `tracemalloc` measures Python allocations, not browser memory, full process RSS, or DOM memory.
- Thresholds are advisory, not test gates: dominant share {DOMINANT_COLD_SHARE:.0%}, significant share {SIGNIFICANT_COLD_SHARE:.0%}, large final HTML {DOM_SIZE_WARNING_BYTES} bytes, low warm Python time {DOM_SIZE_WARM_PYTHON_MS:.0f} ms.
- This is a cold/warm baseline only; it makes no claim about improvement versus an older version.
"""


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_reports(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    timestamp = datetime.fromisoformat(result["created_at"]).strftime("%Y%m%d_%H%M%S")
    output_dir = Path(output_dir); encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"; markdown = render_markdown_report(result)
    paths = {"timestamped_json": output_dir / f"benchmark_{timestamp}.json", "timestamped_markdown": output_dir / f"benchmark_{timestamp}.md",
             "latest_json": output_dir / "latest_benchmark.json", "latest_markdown": output_dir / "latest_benchmark.md"}
    atomic_write(paths["timestamped_json"], encoded); atomic_write(paths["timestamped_markdown"], markdown)
    atomic_write(paths["latest_json"], encoded); atomic_write(paths["latest_markdown"], markdown)
    return paths
