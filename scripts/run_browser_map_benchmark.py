#!/usr/bin/env python3
"""Run the optional warehouse map benchmark in a real local browser."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warehouse_browser_benchmark import (SCHEMA_VERSION, normalize_cdp_metrics,
    normalize_map_metrics, safe_ratio, scenario_summary, write_reports)
from warehouse_performance_benchmark import PERSISTED_STATE_PATHS, persisted_state_signatures

EXTRA_PERSISTED_PATHS = tuple(Path("data/last_import") / name for name in (
    "row_settings.json", "manual_overrides.json", "render_settings.json"))
ALL_PERSISTED_PATHS = PERSISTED_STATE_PATHS + EXTRA_PERSISTED_PATHS
EXIT_ERROR = 1
EXIT_STATE_CHANGED = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("current", "synthetic", "both"), default="current")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--port", type=int, default=8511)
    group = parser.add_mutually_exclusive_group(); group.add_argument("--headless", action="store_true", default=True); group.add_argument("--headed", action="store_false", dest="headless")
    parser.add_argument("--browser", choices=("auto", "chrome", "msedge", "chromium"), default="auto")
    parser.add_argument("--cells", type=int, default=16000)
    parser.add_argument("--occupied-cells", type=int, default=500)
    parser.add_argument("--placements", type=int, default=700)
    parser.add_argument("--output-dir", type=Path, default=Path("data/browser_performance_benchmarks"))
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.timeout_seconds < 1:
        parser.error("iterations and timeout must be positive")
    return args


def import_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run setup_browser_benchmark.cmd first.") from exc
    return sync_playwright


def choose_browser(playwright: Any, requested: str, headless: bool) -> tuple[Any, str]:
    choices = (("chrome", "chrome"), ("msedge", "msedge"), ("chromium", None)) if requested == "auto" else ((requested, None if requested == "chromium" else requested),)
    errors = []
    for name, channel in choices:
        try:
            browser = playwright.chromium.launch(channel=channel, headless=headless)
            return browser, name
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise RuntimeError("No compatible Chrome, Edge, or installed Playwright Chromium was found. "
                       "Install Chromium explicitly with: python -m playwright install chromium")


def available_port(preferred: int) -> int:
    with socket.socket() as sock:
        try: sock.bind(("127.0.0.1", preferred))
        except OSError: sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_streamlit(port: int) -> subprocess.Popen:
    command = [sys.executable, "-m", "streamlit", "run", str(ROOT / "browser_map_benchmark_app.py"),
               "--server.address", "127.0.0.1", "--server.port", str(port),
               "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    return subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_health(port: int, timeout: float, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + timeout; url = f"http://127.0.0.1:{port}/_stcore/health"
    while time.monotonic() < deadline:
        if process.poll() is not None: raise RuntimeError("Benchmark Streamlit process exited before becoming healthy")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200: return
        except (OSError, urllib.error.URLError): pass
        time.sleep(.2)
    raise TimeoutError("Timed out waiting for benchmark Streamlit health endpoint")


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None: return
    process.terminate()
    try: process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=5)


def collect_iteration(browser: Any, url: str, maps_count: int, timeout_ms: int) -> dict[str, Any]:
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    try:
        page = context.new_page(); page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        measured = []
        while time.monotonic() < deadline:
            measured = []
            for frame in page.frames:
                try:
                    result = frame.evaluate("() => document.documentElement.dataset.warehouseBenchmarkReady === 'true' ? window.__WAREHOUSE_BROWSER_BENCHMARK__ : null")
                    if result: measured.append(normalize_map_metrics(result))
                except Exception: pass
            if len(measured) == maps_count: break
            page.wait_for_timeout(100)
        if len(measured) != maps_count: raise TimeoutError(f"Expected {maps_count} ready map iframe(s), found {len(measured)}")
        measured.sort(key=lambda item: item["map_index"])
        cdp = {key: None for key in normalize_cdp_metrics([])}
        try:
            session = context.new_cdp_session(page); session.send("Performance.enable")
            cdp = normalize_cdp_metrics(session.send("Performance.getMetrics").get("metrics", [])); session.detach()
        except Exception: pass
        return {"maps": measured, "browser_cdp_metrics": cdp}
    finally:
        context.close()


def git_value(*args: str) -> str:
    try: return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError): return "unknown"


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    before = persisted_state_signatures(ALL_PERSISTED_PATHS); process = None; browser = None
    iterations: list[dict[str, Any]] = []; warnings: list[str] = []
    browser_name = browser_version = "unknown"; error: Exception | None = None
    try:
        port = available_port(args.port); process = start_streamlit(port); wait_for_health(port, args.timeout_seconds, process)
        sync_playwright = import_playwright()
        with sync_playwright() as playwright:
            browser, browser_name = choose_browser(playwright, args.browser, args.headless); browser_version = browser.version
            datasets = ("current", "synthetic") if args.mode == "both" else (args.mode,)
            for dataset in datasets:
                for maps_count in (1, 2):
                    scenario = f"{dataset}_{'single' if maps_count == 1 else 'double'}"
                    query = urllib.parse.urlencode({"dataset": dataset, "maps": maps_count, "cells": args.cells,
                                                    "occupied_cells": args.occupied_cells, "placements": args.placements})
                    for iteration in range(args.iterations):
                        result = collect_iteration(browser, f"http://127.0.0.1:{port}/?{query}", maps_count, int(args.timeout_seconds * 1000))
                        result.update({"scenario": scenario, "iteration": iteration + 1}); iterations.append(result)
            browser.close(); browser = None
    except Exception as exc:
        error = exc; warnings.append(type(exc).__name__)
    finally:
        if browser is not None:
            with contextlib.suppress(Exception): browser.close()
        stop_process(process)
    after = persisted_state_signatures(ALL_PERSISTED_PATHS)
    changed = sorted(name for name in before if before[name] != after.get(name))
    scenario_summaries = {name: scenario_summary([i for i in iterations if i["scenario"] == name])
                          for name in sorted({i["scenario"] for i in iterations})}
    ratios = {}
    for dataset in ("current", "synthetic"):
        single, double = scenario_summaries.get(dataset + "_single"), scenario_summaries.get(dataset + "_double")
        if single and double:
            med = lambda summary, key: summary[key]["median"]
            ratios[dataset] = {"ready": safe_ratio(med(double, "benchmark_ready_ms"), med(single, "benchmark_ready_ms")),
                               "dom": safe_ratio(med(double, "dom_nodes_total") * 2 if double.get("maps") == 2 else med(double, "dom_nodes_total"), med(single, "dom_nodes_total")),
                               "js_heap": safe_ratio(med(double, "used_js_heap_bytes"), med(single, "used_js_heap_bytes"))}
    try:
        import streamlit
        streamlit_version = streamlit.__version__
    except Exception: streamlit_version = "unknown"
    report = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
              "git": {"commit": git_value("rev-parse", "HEAD"), "branch": git_value("branch", "--show-current")},
              "python_version": platform.python_version(), "streamlit_version": streamlit_version,
              "browser": {"name": browser_name, "version": browser_version}, "platform": platform.platform(),
              "viewport": {"width": 1920, "height": 1080, "device_scale_factor": 1},
              "dataset_metadata": {"mode": args.mode, "synthetic_cells": args.cells,
                                   "synthetic_occupied_cells": args.occupied_cells, "synthetic_placements": args.placements},
              "iterations": iterations, "scenario_summaries": scenario_summaries, "single_map_summary": {k:v for k,v in scenario_summaries.items() if k.endswith("single")},
              "double_map_summary": {k:v for k,v in scenario_summaries.items() if k.endswith("double")}, "ratios": ratios,
              "browser_cdp_metrics": [i["browser_cdp_metrics"] for i in iterations], "warnings": warnings,
              "persisted_state_unchanged": not changed, "changed_persisted_files": changed}
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S"); write_reports(report, ROOT / args.output_dir, timestamp)
    if changed: return report, EXIT_STATE_CHANGED
    if error: raise RuntimeError(str(error)) from error
    return report, 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report, code = run(args)
        print(json.dumps({"status": "ok" if code == 0 else "persisted_state_changed",
                          "persisted_state_unchanged": report["persisted_state_unchanged"]}))
        return code
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
