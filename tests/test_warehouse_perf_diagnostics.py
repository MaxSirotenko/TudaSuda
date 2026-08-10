from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import warehouse_perf_diagnostics as diagnostics


ROOT = Path(__file__).resolve().parents[1]


def _python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)


def test_startup_chain_imports_without_unix_resource():
    process = _python(
        "import sys; "
        "sys.modules['resource'] = None; "
        "import warehouse_perf_diagnostics, warehouse_factual_data, warehouse_workspace_ui, virtual_warehouse_app; "
        "value = warehouse_perf_diagnostics.get_process_rss_bytes(); "
        "assert value is None or isinstance(value, int)"
    )

    assert process.returncode == 0, process.stderr


def test_resource_fallback_remains_available(monkeypatch):
    fake_resource = SimpleNamespace(
        RUSAGE_SELF=0,
        getrusage=lambda _who: SimpleNamespace(ru_maxrss=123),
    )
    monkeypatch.setattr(diagnostics, "resource", fake_resource)
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(diagnostics.sys, "platform", "linux")

    assert diagnostics.get_process_rss_bytes() == 123 * 1024


def test_missing_rss_provider_returns_none(monkeypatch):
    monkeypatch.setattr(diagnostics, "resource", None)
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))

    assert diagnostics.get_process_rss_bytes() is None


def test_debug_off_measure_does_not_collect_rss(monkeypatch):
    monkeypatch.setattr(diagnostics, "ENABLED", False)
    monkeypatch.setattr(
        diagnostics,
        "get_process_rss_bytes",
        lambda: (_ for _ in ()).throw(AssertionError("RSS must not be collected")),
    )

    with diagnostics.measure("disabled"):
        pass
