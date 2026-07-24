import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import warehouse_performance as performance


@pytest.fixture(autouse=True)
def isolated_performance(tmp_path, monkeypatch):
    directory = tmp_path / "performance"
    monkeypatch.setattr(performance, "PERFORMANCE_DIR", directory)
    monkeypatch.setattr(performance, "LATEST_RUN_PATH", directory / "latest_run.json")
    monkeypatch.setattr(performance, "HISTORY_PATH", directory / "history.jsonl")
    monkeypatch.delenv("WAREHOUSE_PERF", raising=False)
    performance._state.run = None
    performance._measured_run_count = 0
    yield directory
    if performance.tracemalloc.is_tracing():
        performance.tracemalloc.stop()


def test_disabled_measurement_is_noop_and_creates_no_files(isolated_performance):
    assert performance.start_performance_run() is None
    with performance.measure_step("disabled"):
        value = 42
    assert value == 42
    assert performance.finish_performance_run() is None
    assert not isolated_performance.exists()


def test_enabled_run_writes_reloadable_latest_with_positive_step(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_PERF", "1")
    performance.start_performance_run()
    with performance.measure_step("load_geometry_model", {"source": "test"}):
        time.sleep(0.001)
    performance.finish_performance_run(model_id="model-test")

    latest = performance.load_latest_performance_run()
    assert latest == json.loads(performance.LATEST_RUN_PATH.read_text(encoding="utf-8"))
    assert latest["model_id"] == "model-test"
    assert latest["steps"][0]["name"] == "load_geometry_model"
    assert latest["steps"][0]["duration_ms"] > 0


def test_repeated_steps_are_preserved(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_PERF", "1")
    performance.start_performance_run()
    for _ in range(2):
        with performance.measure_step("load_placements"):
            pass
    run = performance.finish_performance_run()
    assert [step["name"] for step in run["steps"]] == ["load_placements", "load_placements"]
    assert all(step["call_count"] == 1 for step in run["steps"])


def test_exception_step_and_error_run_are_recorded(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_PERF", "1")
    performance.start_performance_run()
    error = None
    with pytest.raises(RuntimeError):
        try:
            with performance.measure_step("build_map_html_svg"):
                raise RuntimeError("boom")
        except RuntimeError as exc:
            error = exc
            raise
        finally:
            performance.finish_performance_run(exception=error)

    latest = performance.load_latest_performance_run()
    assert latest["status"] == "error"
    assert latest["exception_type"] == "RuntimeError"
    assert latest["last_step"] == "build_map_html_svg"
    assert latest["steps"][0]["metadata"]["exception_type"] == "RuntimeError"


def test_history_is_limited_to_200_runs(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_PERF", "1")
    for _ in range(205):
        performance.start_performance_run()
        performance.finish_performance_run()
    history = performance.load_performance_history()
    assert len(history) == 200
    assert len(performance.HISTORY_PATH.read_text(encoding="utf-8").splitlines()) == 200


def test_write_error_never_breaks_application(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_PERF", "1")
    monkeypatch.setattr(performance, "_write_run", lambda run: (_ for _ in ()).throw(OSError("read only")))
    performance.start_performance_run()
    with performance.measure_step("work"):
        pass
    assert performance.finish_performance_run()["status"] == "success"


def test_main_application_contains_lightweight_run_boundary():
    source = Path("virtual_warehouse_app.py").read_text(encoding="utf-8")
    main_source = source[source.index("def main() -> None:"):]
    assert "start_performance_run(enabled=is_performance_enabled(st.session_state))" in main_source
    assert "finally:" in main_source
    assert "finish_performance_run(" in main_source
