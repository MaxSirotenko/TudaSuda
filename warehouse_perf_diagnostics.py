"""Opt-in, process-local diagnostics for Streamlit render and factual I/O.

No sampler, timer, or background worker is started.  Measurements are collected
only while an instrumented block is executing and are retained in a bounded
ring, so enabling diagnostics cannot itself create idle work or unbounded RAM.
"""
from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from functools import wraps
import os
import resource
import time
from typing import Iterator


ENABLED = os.getenv("WAREHOUSE_DEBUG_PERF", "").strip() == "1"
_events: deque[dict] = deque(maxlen=200)
_counts: Counter[str] = Counter()
_artifact_reads = 0
_artifact_bytes = 0


def rss_bytes() -> int:
    """Return current RSS with Linux/macOS ``ru_maxrss`` as safe fallback."""
    try:
        with open("/proc/self/statm", encoding="ascii") as stream:
            return int(stream.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if os.uname().sysname == "Darwin" else value * 1024)


@contextmanager
def measure(name: str, *, cache_status: str | None = None) -> Iterator[None]:
    if not ENABLED:
        yield
        return
    before, started = rss_bytes(), time.perf_counter()
    try:
        yield
    finally:
        after = rss_bytes()
        _counts[name] += 1
        _events.append({"name": name, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                        "rss_before_mb": round(before / 1048576, 2), "rss_after_mb": round(after / 1048576, 2),
                        "rss_delta_mb": round((after - before) / 1048576, 2),
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


def snapshot() -> dict:
    events = list(_events)
    cache_counts = Counter(item["cache_status"] for item in events if item["cache_status"] != "n/a")
    return {"rss_mb": round(rss_bytes() / 1048576, 2), "artifact_reads": _artifact_reads,
            "artifact_bytes": _artifact_bytes, "events": events, "cache_status": dict(cache_counts),
            "last_render_ms": next((item["elapsed_ms"] for item in reversed(events)
                                    if item["name"] == "workspace.root"), None),
            "top_slow_blocks": sorted(events, key=lambda item: item["elapsed_ms"], reverse=True)[:5]}
