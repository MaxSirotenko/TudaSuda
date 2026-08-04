from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import warehouse_browser_benchmark as benchmark
from warehouse_performance_benchmark import persisted_state_signatures


def load_runner():
    spec = importlib.util.spec_from_file_location("browser_runner", ROOT / "scripts/run_browser_map_benchmark.py")
    module = importlib.util.module_from_spec(spec); assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_instrumentation_is_explicit_unique_and_aggregate_only():
    production = "<html><head></head><body><svg><rect data-cell-key='SECRET'/><title>SKU tooltip</title></svg></body></html>"
    first = benchmark.instrument_map_html(production, scenario_id="current_single", map_index=0,
                                          cells_count=1, rows_count=1, placements_count=0)
    second = benchmark.instrument_map_html(production, scenario_id="current_double", map_index=1,
                                           cells_count=1, rows_count=1, placements_count=0)
    assert "__WAREHOUSE_BROWSER_BENCHMARK__" in first
    assert "warehouseBenchmarkReady" in first
    assert production == "<html><head></head><body><svg><rect data-cell-key='SECRET'/><title>SKU tooltip</title></svg></body></html>"
    assert "current_single" in first and "current_double" in second and "map_index:meta.map_index" in second
    assert "innerHTML" not in first and "textContent" not in first
    assert "PerformanceObserver" in first and ".disconnect()" in first
    assert "performance.memory||null" in first


def test_normalization_whitelists_metrics_and_supports_nullable_values():
    result = benchmark.normalize_map_metrics({"status": "ok", "scenario_id": "x", "map_index": 0,
                                              "used_js_heap_bytes": None, "cell_key": "secret",
                                              "sku": "secret", "tooltip": "secret"})
    assert result["used_js_heap_bytes"] is None
    assert not ({"cell_key", "sku", "tooltip"} & result.keys())
    assert benchmark.normalize_cdp_metrics([])["Nodes"] is None


def test_summaries_ratios_and_zero_division():
    assert benchmark.summarize([3, 1, 2]) == {"min": 1.0, "median": 2.0, "max": 3.0}
    assert benchmark.safe_ratio(4, 2) == 2
    assert benchmark.safe_ratio(4, 0) is None
    assert benchmark.safe_ratio(None, 2) is None


def test_markdown_json_and_atomic_reports(tmp_path):
    report = {"scenario_summaries": {"current_single": {"maps": 1, **{
        key: {"median": 1.0} for key in ("cells_count", "benchmark_ready_ms", "dom_nodes_total",
                                         "svg_elements_total", "long_task_count", "used_js_heap_bytes", "final_html_bytes")}}},
              "ratios": {"current": {"ready": 2.0, "dom": 2.0, "js_heap": None}}, "iterations": []}
    markdown = benchmark.render_markdown(report)
    assert "| Scenario | Maps | Cells | Ready median" in markdown and "current" in markdown
    json.dumps(report, allow_nan=False)
    paths = benchmark.write_reports(report, tmp_path, "20260101_000000")
    assert all(path.exists() for path in paths)
    assert (tmp_path / "latest_browser_benchmark.json").exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("params", [
    {"dataset": "bad"}, {"maps": "3"}, {"cells": "x"},
    {"dataset": "synthetic", "cells": "2", "occupied_cells": "3"},
])
def test_invalid_query_params_are_rejected(params):
    with pytest.raises(ValueError): benchmark.validate_query_params(params)


def test_valid_query_params_are_bounded():
    assert benchmark.validate_query_params({"dataset": "current", "maps": "2"})["maps"] == 2


def test_persisted_signatures_are_read_only(tmp_path):
    path = tmp_path / "warehouse_model.json"; path.write_text("{}", encoding="utf-8")
    before = persisted_state_signatures((path,)); after = persisted_state_signatures((path,))
    assert before == after


def test_runner_stops_subprocess_in_finally(monkeypatch, tmp_path):
    runner = load_runner(); stopped = []
    fake = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(runner, "start_streamlit", lambda port: fake)
    monkeypatch.setattr(runner, "wait_for_health", lambda *args: (_ for _ in ()).throw(RuntimeError("health")))
    monkeypatch.setattr(runner, "stop_process", lambda process: stopped.append(process))
    monkeypatch.setattr(runner, "persisted_state_signatures", lambda paths: {})
    monkeypatch.setattr(runner, "write_reports", lambda *args: None)
    args = runner.parse_args(["--output-dir", str(tmp_path)])
    with pytest.raises(RuntimeError, match="health"):
        runner.run(args)
    assert stopped == [fake]


def test_missing_playwright_message(monkeypatch):
    runner = load_runner()
    monkeypatch.setitem(sys.modules, "playwright", None)
    with pytest.raises(RuntimeError, match="setup_browser_benchmark.cmd"):
        runner.import_playwright()


def test_missing_browsers_message():
    runner = load_runner()
    class Chromium:
        def launch(self, **kwargs): raise RuntimeError("missing")
    with pytest.raises(RuntimeError, match="playwright install chromium"):
        runner.choose_browser(SimpleNamespace(chromium=Chromium()), "auto", True)


def test_production_import_does_not_load_playwright():
    process = subprocess.run([sys.executable, "-c", "import virtual_warehouse_app,sys; print('playwright' in sys.modules)"],
                             cwd=ROOT, capture_output=True, text=True, check=True)
    assert process.stdout.strip() == "False"


def test_benchmark_app_contains_no_writes_or_global_cache_clear():
    source = (ROOT / "browser_map_benchmark_app.py").read_text(encoding="utf-8")
    assert "save_" not in source
    assert "st.cache_data.clear" not in source
    assert "bump_revision" not in source
    assert "st.columns(2)" in source


def test_production_startup_is_unchanged_by_browser_benchmark_feature():
    status = subprocess.check_output(["git", "diff", "--name-only", "b4a0b8c", "--", "start.cmd"], cwd=ROOT, text=True)
    assert status == ""
