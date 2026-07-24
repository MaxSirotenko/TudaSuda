"""Lightweight, best-effort performance tracing for warehouse application runs."""

from __future__ import annotations

import json
import os
import threading
import time
import tracemalloc
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


PERFORMANCE_DIR = Path("data/performance")
LATEST_RUN_PATH = PERFORMANCE_DIR / "latest_run.json"
HISTORY_PATH = PERFORMANCE_DIR / "history.jsonl"
HISTORY_LIMIT = 200

_state = threading.local()
_measured_run_count = 0
_counter_lock = threading.Lock()


def is_performance_enabled(session_state: Mapping[str, Any] | None = None) -> bool:
    """Return whether tracing is requested by the environment or UI state."""
    environment_enabled = os.getenv("WAREHOUSE_PERF", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        session_enabled = bool(session_state and session_state.get("performance_enabled", False))
    except Exception:
        session_enabled = False
    return environment_enabled or session_enabled


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def start_performance_run(*, enabled: bool | None = None, model_id: str | None = None) -> dict | None:
    """Start a process-local run, starting tracemalloc only when enabled."""
    global _measured_run_count
    if enabled is None:
        enabled = is_performance_enabled()
    if not enabled:
        _state.run = None
        return None
    try:
        with _counter_lock:
            is_cold_start = _measured_run_count == 0
            _measured_run_count += 1
        owns_tracemalloc = not tracemalloc.is_tracing()
        if owns_tracemalloc:
            tracemalloc.start()
        run = {
            "run_id": uuid.uuid4().hex,
            "started_at": _now(),
            "finished_at": None,
            "status": "running",
            "is_cold_start": is_cold_start,
            "total_duration_ms": None,
            "peak_traced_memory_mb": None,
            "model_id": model_id,
            "process_id": os.getpid(),
            "steps": [],
            "_started_ns": time.perf_counter_ns(),
            "_owns_tracemalloc": owns_tracemalloc,
            "_last_step": None,
        }
        _state.run = run
        return run
    except Exception:
        _state.run = None
        return None


@contextmanager
def measure_step(name: str, metadata: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Measure a named step, or act as a near-zero-cost context manager."""
    run = getattr(_state, "run", None)
    if run is None:
        yield
        return
    started_ns = time.perf_counter_ns()
    run["_last_step"] = name
    error_type = None
    try:
        yield
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        step_metadata = dict(metadata or {})
        if error_type:
            step_metadata["exception_type"] = error_type
        run["steps"].append(
            {
                "name": name,
                "duration_ms": max((time.perf_counter_ns() - started_ns) / 1_000_000, 0.000001),
                "call_count": 1,
                "metadata": step_metadata,
            }
        )


def _public_run(run: dict) -> dict:
    return {key: value for key, value in run.items() if not key.startswith("_")}


def _write_run(run: dict) -> None:
    PERFORMANCE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(run, ensure_ascii=False, indent=2)
    temporary = LATEST_RUN_PATH.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(LATEST_RUN_PATH)
    history = load_performance_history()
    history.append(run)
    history = history[-HISTORY_LIMIT:]
    history_temporary = HISTORY_PATH.with_suffix(".jsonl.tmp")
    history_temporary.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in history), encoding="utf-8"
    )
    history_temporary.replace(HISTORY_PATH)


def finish_performance_run(
    *,
    status: str = "success",
    exception: BaseException | None = None,
    model_id: str | None = None,
) -> dict | None:
    """Finish and best-effort persist the current run without masking app errors."""
    run = getattr(_state, "run", None)
    if run is None:
        return None
    try:
        run["finished_at"] = _now()
        run["status"] = "error" if exception is not None else status
        run["total_duration_ms"] = max((time.perf_counter_ns() - run["_started_ns"]) / 1_000_000, 0.000001)
        if model_id is not None:
            run["model_id"] = model_id
        if exception is not None:
            run["exception_type"] = type(exception).__name__
            run["last_step"] = run.get("_last_step")
        if tracemalloc.is_tracing():
            run["peak_traced_memory_mb"] = tracemalloc.get_traced_memory()[1] / (1024 * 1024)
        result = _public_run(run)
        try:
            _write_run(result)
        except Exception:
            pass
        return result
    finally:
        if run.get("_owns_tracemalloc") and tracemalloc.is_tracing():
            tracemalloc.stop()
        _state.run = None


def load_latest_performance_run() -> dict | None:
    try:
        return json.loads(LATEST_RUN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def load_performance_history() -> list[dict]:
    try:
        return [json.loads(line) for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()][-HISTORY_LIMIT:]
    except (OSError, ValueError, TypeError):
        return []


def clear_performance_history() -> None:
    """Best-effort removal of persisted diagnostics."""
    for path in (LATEST_RUN_PATH, HISTORY_PATH):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
