from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import warehouse_performance_benchmark as benchmark


def test_synthetic_generator_is_exact_deterministic_and_does_not_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = benchmark.generate_synthetic_dataset(123, 17, 25)
    second = benchmark.generate_synthetic_dataset(123, 17, 25)
    assert first == second
    assert len(first[0]["cells"]) == 123
    assert len(first[1]["placements"]) == 25
    assert len({item["cell_key"] for item in first[1]["placements"]}) == 17
    assert not (tmp_path / "data").exists()
    assert any(cell["storage_type"] == "deep_lane" for cell in first[0]["cells"])
    assert {road["road_type"] for road in first[0]["roads"]} == {"top", "bottom"}


def test_dataset_selection_uses_current_and_falls_back(monkeypatch):
    model, _ = benchmark.generate_synthetic_dataset(10, 2, 2)
    monkeypatch.setattr(benchmark.placement, "load_placement_state", lambda value: ({"placements": []}, None))
    selected = benchmark.load_benchmark_dataset("current-or-synthetic", 5, 1, 1, loader=lambda: model)
    assert selected[2] == "current"
    missing = benchmark.load_benchmark_dataset("current-or-synthetic", 5, 1, 1, loader=lambda: None)
    assert missing[2] == "synthetic_fallback" and missing[3]
    corrupt = benchmark.load_benchmark_dataset(
        "current-or-synthetic", 5, 1, 1,
        loader=lambda: (_ for _ in ()).throw(json.JSONDecodeError("bad", "x", 0)),
    )
    assert corrupt[2] == "synthetic_fallback" and "JSONDecodeError" in corrupt[3][0]


def test_small_run_is_serializable_sanitized_and_cache_scenarios_pass(monkeypatch):
    monkeypatch.setattr(benchmark, "_import_scenario", lambda: {
        "status": "ok", "wall_time_ms": 1.0, "return_code": 0, "traceback_present": False,
        "stdout_bytes": 0, "stderr_bytes": 0,
    })
    result = benchmark.run_benchmark("synthetic", 30, 5, 7, 2)
    assert result["schema_version"] == 1
    assert all("status" in scenario for scenario in result["scenarios"].values())
    assert all(result["cache_assertions"].values())
    assert result["scenarios"]["placement_only_change"]["static_builder_calls"] == 0
    assert result["scenarios"]["placement_only_change"]["dynamic_builder_calls"] == 1
    assert result["scenarios"]["geometry_change"]["static_builder_calls"] == 1
    assert result["scenarios"]["geometry_change"]["dynamic_builder_calls"] == 1
    encoded = json.dumps(result)
    assert all(secret not in encoded.lower() for secret in ("synthetic item", "bench-0|", "source_file_hash", '"placements": ['))
    assert result["persisted_state_unchanged"]


def test_report_names_markdown_and_atomic_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "_import_scenario", lambda: {
        "status": "ok", "wall_time_ms": 1.0, "return_code": 0, "traceback_present": False,
        "stdout_bytes": 0, "stderr_bytes": 0,
    })
    result = benchmark.run_benchmark("synthetic", 10, 2, 3, 1)
    replacements = []
    real_replace = benchmark.os.replace
    monkeypatch.setattr(benchmark.os, "replace", lambda source, target: replacements.append(Path(target).name) or real_replace(source, target))
    paths = benchmark.write_reports(result, tmp_path)
    assert paths["latest_json"].name == "latest_benchmark.json"
    assert paths["latest_markdown"].name == "latest_benchmark.md"
    assert paths["timestamped_json"].name.startswith("benchmark_")
    assert paths["timestamped_markdown"].suffix == ".md"
    assert len(replacements) == 4
    markdown = paths["latest_markdown"].read_text(encoding="utf-8")
    assert "## 9. Probable bottleneck" in markdown and "tracemalloc" in markdown


def _result(import_ms=1, disk_ms=1, static_ms=1, dynamic_ms=1, compose_ms=1,
            final_bytes=100, warm_ms=1):
    return {"scenarios": {
        "application_import": {"wall_time_ms": import_ms}, "placement_state_read": {"direct_ms": disk_ms},
        "static_cold": {"wall_time_ms": static_ms}, "dynamic_cold": {"wall_time_ms": dynamic_ms},
        "compose_layers": {"wall_time_ms": compose_ms}, "static_warm": {"median_ms": warm_ms},
        "dynamic_warm": {"median_ms": warm_ms}}, "payload_sizes": {"final_html_bytes": final_bytes}}


def test_bottleneck_analyzer_uses_measurements_not_a_fixed_database_answer():
    cases = [
        (_result(static_ms=100), "static_svg_generation"),
        (_result(dynamic_ms=100), "dynamic_payload_generation"),
        (_result(disk_ms=100), "disk_state_loading"),
        (_result(import_ms=100), "startup_import"),
        (_result(compose_ms=100), "layer_composition"),
        (_result(final_bytes=benchmark.DOM_SIZE_WARNING_BYTES), "final_html_dom_size"),
    ]
    for result, expected in cases:
        analysis = benchmark.analyze_benchmark_bottleneck(result)
        assert analysis["primary_bottleneck"] == expected
        if expected != "disk_state_loading":
            assert "SQLite" not in analysis["recommended_next_step"]
    assert benchmark.analyze_benchmark_bottleneck({})["primary_bottleneck"] == "insufficient_data"


def test_source_contains_only_targeted_cache_clears_and_no_mutation_calls():
    source = Path(benchmark.__file__).read_text(encoding="utf-8")
    assert "st.cache_data.clear" not in source
    assert "build_geometry_static_layer_cached.clear()" in source
    assert "build_geometry_dynamic_layer_cached.clear()" in source
    for forbidden in ("bump_revisions(", "save_geometry_model(", "save_placement_state(", "session_state"):
        assert forbidden not in source


def test_windows_launcher_contract():
    source = Path("benchmark_performance.cmd").read_text(encoding="utf-8")
    assert "%~dp0" in source and ".venv\\Scripts\\python.exe" in source
    assert "venv\\Scripts\\python.exe" in source and "%*" in source
