"""Opt-in, process-local diagnostics for Streamlit render and factual I/O.

No sampler, timer, or background worker is started.  Measurements are collected
only while an instrumented block is executing and are retained in a bounded
ring, so enabling diagnostics cannot itself create idle work or unbounded RAM.
"""
from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os
import sys
import time
from typing import Iterator

try:
    import resource
except ImportError:  # The module is not part of the standard library on Windows.
    resource = None


ENABLED = os.getenv("WAREHOUSE_DEBUG_PERF", "").strip() == "1"
_events: deque[dict] = deque(maxlen=200)
_counts: Counter[str] = Counter()
_artifact_reads = 0
_artifact_bytes = 0
_io_probe: ContextVar[Counter[str] | None] = ContextVar("warehouse_io_probe", default=None)


@contextmanager
def capture_io_reads() -> Iterator[Counter[str]]:
    """Count explicitly instrumented production reads in the current context.

    This deliberately avoids monkeypatching global filesystem APIs.  Reader
    boundaries call :func:`record_file_read` or :func:`record_artifact_read`, so
    benchmark results reflect actual calls and tests can exercise the counter.
    """
    counts: Counter[str] = Counter()
    token = _io_probe.set(counts)
    try:
        yield counts
    finally:
        _io_probe.reset(token)


def record_file_read(kind: str = "file", byte_count: int = 0) -> None:
    probe = _io_probe.get()
    if probe is not None:
        probe["file_reads"] += 1
        probe["file_bytes"] += max(0, int(byte_count))
        probe[f"reader:{kind}"] += 1


def get_process_rss_bytes() -> int | None:
    """Return current RSS, or ``None`` when no platform provider is available."""
    try:
        with open("/proc/self/statm", encoding="ascii") as stream:
            return int(stream.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError, IndexError):
        pass

    if resource is None:
        return None
    try:
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except (AttributeError, OSError, ValueError):
        return None


# Retain the original public name used by developer tooling.
rss_bytes = get_process_rss_bytes


@contextmanager
def measure(name: str, *, cache_status: str | None = None) -> Iterator[None]:
    if not ENABLED:
        yield
        return
    before, started = get_process_rss_bytes(), time.perf_counter()
    try:
        yield
    finally:
        after = get_process_rss_bytes()
        _counts[name] += 1
        _events.append({"name": name, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                        "rss_before_mb": _to_megabytes(before), "rss_after_mb": _to_megabytes(after),
                        "rss_delta_mb": (_to_megabytes(after - before)
                                         if before is not None and after is not None else None),
                        "call_count": _counts[name], "cache_status": cache_status or "n/a"})


def profiled(name: str):
    """Decorate a synchronous hot path; when disabled overhead is one branch."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with measure(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def record_artifact_read(byte_count: int) -> None:
    global _artifact_reads, _artifact_bytes
    if ENABLED:
        _artifact_reads += 1
        _artifact_bytes += max(0, int(byte_count))
    probe = _io_probe.get()
    if probe is not None:
        probe["artifact_reads"] += 1
        probe["artifact_bytes"] += max(0, int(byte_count))
        record_file_read("artifact", byte_count)


def _to_megabytes(value: int | None) -> float | None:
    return round(value / 1048576, 2) if value is not None else None


def snapshot() -> dict:
    events = list(_events)
    cache_counts = Counter(item["cache_status"] for item in events if item["cache_status"] != "n/a")
    return {"rss_mb": _to_megabytes(get_process_rss_bytes()), "artifact_reads": _artifact_reads,
            "artifact_bytes": _artifact_bytes, "events": events, "cache_status": dict(cache_counts),
            "last_render_ms": next((item["elapsed_ms"] for item in reversed(events)
                                    if item["name"] == "workspace.root"), None),
            "top_slow_blocks": sorted(events, key=lambda item: item["elapsed_ms"], reverse=True)[:5]}
