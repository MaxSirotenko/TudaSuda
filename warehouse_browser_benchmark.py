"""Aggregate-only helpers for the optional warehouse browser benchmark."""

from __future__ import annotations

import json
import math
import os
import statistics
import tempfile
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
METRIC_KEYS = (
    "benchmark_ready_ms", "dom_content_loaded_ms", "load_event_ms",
    "dom_nodes_total", "svg_elements_total", "cell_nodes_total", "text_nodes_total",
    "rect_nodes_total", "line_nodes_total", "path_nodes_total", "polygon_nodes_total",
    "circle_nodes_total", "long_task_count", "long_task_total_ms", "long_task_max_ms",
    "used_js_heap_bytes", "total_js_heap_bytes", "viewport_width", "viewport_height",
    "device_pixel_ratio", "map_width", "map_height", "final_html_bytes", "cells_count",
    "rows_count", "placements_count",
)
CDP_METRIC_KEYS = (
    "Nodes", "Documents", "Frames", "LayoutCount", "RecalcStyleCount", "ScriptDuration",
    "TaskDuration", "LayoutDuration", "RecalcStyleDuration", "JSHeapUsedSize", "JSHeapTotalSize",
)


def validate_query_params(params: dict[str, Any]) -> dict[str, int | str]:
    """Return a bounded scenario config without retaining arbitrary query input."""
    def scalar(name: str, default: str) -> str:
        value = params.get(name, default)
        if isinstance(value, list):
            value = value[0] if value else default
        return str(value)

    dataset = scalar("dataset", "current")
    if dataset not in {"current", "synthetic"}:
        raise ValueError("dataset must be current or synthetic")
    try:
        maps = int(scalar("maps", "1"))
        cells = int(scalar("cells", "16000"))
        occupied = int(scalar("occupied_cells", "500"))
        placements = int(scalar("placements", "700"))
    except ValueError as exc:
        raise ValueError("numeric query parameters must be integers") from exc
    if maps not in {1, 2}:
        raise ValueError("maps must be 1 or 2")
    if cells < 1 or occupied < 0 or occupied > cells or placements < occupied:
        raise ValueError("invalid synthetic dataset dimensions")
    if occupied == 0 and placements:
        raise ValueError("placements requires occupied cells")
    return {"dataset": dataset, "maps": maps, "cells": cells,
            "occupied_cells": occupied, "placements": placements}


def instrument_map_html(html: str, *, scenario_id: str, map_index: int,
                        cells_count: int, rows_count: int, placements_count: int) -> str:
    """Add benchmark-only timing code; the production document is never mutated in place."""
    metadata = json.dumps({"scenario_id": str(scenario_id), "map_index": int(map_index),
                           "final_html_bytes": len(html.encode("utf-8")), "cells_count": int(cells_count),
                           "rows_count": int(rows_count), "placements_count": int(placements_count)},
                          ensure_ascii=True, separators=(",", ":"))
    start = """<script>(function(){
window.__warehouseBenchmarkStart=performance.now();performance.mark('warehouse-benchmark-start');
window.__warehouseLongTasks=[];window.__warehouseLongTaskObserver=null;
if(window.PerformanceObserver){try{var o=new PerformanceObserver(function(l){l.getEntries().forEach(function(e){window.__warehouseLongTasks.push(e.duration);});});o.observe({type:'longtask',buffered:true});window.__warehouseLongTaskObserver=o;}catch(e){}}
})();</script>"""
    finish = f"""<script>(function(){{
var meta={metadata};
function raf(){{return new Promise(function(resolve){{requestAnimationFrame(resolve);}});}}
async function collect(){{
 if(document.fonts&&document.fonts.ready){{try{{await document.fonts.ready;}}catch(e){{}}}} await raf();await raf();
 var nav=performance.getEntriesByType('navigation')[0]||{{}};var tasks=window.__warehouseLongTasks||[];
 var svg=document.querySelector('svg');var box=svg?svg.getBoundingClientRect():{{width:0,height:0}};
 var memory=performance.memory||null;
 window.__WAREHOUSE_BROWSER_BENCHMARK__={{status:'ok',scenario_id:meta.scenario_id,map_index:meta.map_index,
 benchmark_ready_ms:performance.now()-window.__warehouseBenchmarkStart,
 dom_content_loaded_ms:Number.isFinite(nav.domContentLoadedEventEnd)?nav.domContentLoadedEventEnd:null,
 load_event_ms:Number.isFinite(nav.loadEventEnd)?nav.loadEventEnd:null,
 dom_nodes_total:document.querySelectorAll('*').length,svg_elements_total:document.querySelectorAll('svg *').length,
 cell_nodes_total:document.querySelectorAll('[data-cell-key]').length,text_nodes_total:document.querySelectorAll('text').length,
 rect_nodes_total:document.querySelectorAll('rect').length,line_nodes_total:document.querySelectorAll('line').length,
 path_nodes_total:document.querySelectorAll('path').length,polygon_nodes_total:document.querySelectorAll('polygon').length,
 circle_nodes_total:document.querySelectorAll('circle').length,long_task_count:tasks.length,
 long_task_total_ms:tasks.reduce(function(a,b){{return a+b;}},0),long_task_max_ms:tasks.length?Math.max.apply(null,tasks):0,
 used_js_heap_bytes:memory?memory.usedJSHeapSize:null,total_js_heap_bytes:memory?memory.totalJSHeapSize:null,
 viewport_width:innerWidth,viewport_height:innerHeight,device_pixel_ratio:devicePixelRatio,map_width:box.width,map_height:box.height,
 final_html_bytes:meta.final_html_bytes,cells_count:meta.cells_count,rows_count:meta.rows_count,placements_count:meta.placements_count}};
 if(window.__warehouseLongTaskObserver)window.__warehouseLongTaskObserver.disconnect();
 document.documentElement.dataset.warehouseBenchmarkReady='true';
}}
if(document.readyState==='complete')collect();else window.addEventListener('load',collect,{{once:true}});
}})();</script>"""
    head_at = html.lower().find("<head>")
    with_start = html[:head_at + 6] + start + html[head_at + 6:] if head_at >= 0 else start + html
    body_at = with_start.lower().rfind("</body>")
    return with_start[:body_at] + finish + with_start[body_at:] if body_at >= 0 else with_start + finish


def normalize_map_metrics(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = {"status": "ok" if source.get("status") == "ok" else "error",
              "scenario_id": str(source.get("scenario_id", "")),
              "map_index": int(source.get("map_index", 0) or 0)}
    for key in METRIC_KEYS:
        item = source.get(key)
        result[key] = item if isinstance(item, (int, float)) and math.isfinite(item) else None
    return result


def normalize_cdp_metrics(metrics: Any) -> dict[str, float | None]:
    lookup = {item.get("name"): item.get("value") for item in metrics or [] if isinstance(item, dict)}
    return {key: float(lookup[key]) if isinstance(lookup.get(key), (int, float)) else None
            for key in CDP_METRIC_KEYS}


def summarize(values: Iterable[float | int | None]) -> dict[str, float | None]:
    valid = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return {"min": min(valid), "median": statistics.median(valid), "max": max(valid)} if valid else {
        "min": None, "median": None, "max": None}


def safe_ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)) or denominator == 0:
        return None
    return float(numerator) / float(denominator)


def scenario_summary(iterations: list[dict[str, Any]]) -> dict[str, Any]:
    maps = [item for iteration in iterations for item in iteration.get("maps", [])]
    summary = {key: summarize(item.get(key) for item in maps) for key in METRIC_KEYS}
    summary["iterations"] = len(iterations)
    summary["maps"] = max((len(i.get("maps", [])) for i in iterations), default=0)
    return summary


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def write_reports(report: dict[str, Any], output_dir: Path, timestamp: str) -> tuple[Path, Path]:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    markdown = render_markdown(report)
    json_path = output_dir / f"browser_benchmark_{timestamp}.json"
    md_path = output_dir / f"browser_benchmark_{timestamp}.md"
    for path, content in ((json_path, encoded), (md_path, markdown),
                          (output_dir / "latest_browser_benchmark.json", encoded),
                          (output_dir / "latest_browser_benchmark.md", markdown)):
        atomic_write(path, content)
    return json_path, md_path


def _fmt(value: Any, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.2f}{suffix}" if isinstance(value, float) else f"{value}{suffix}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Warehouse browser benchmark", "", "| Scenario | Maps | Cells | Ready median | DOM nodes median | SVG nodes median | Long tasks | JS heap | HTML |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    summaries = report.get("scenario_summaries", {})
    for name, summary in summaries.items():
        med = lambda key: summary.get(key, {}).get("median")
        lines.append(f"| {name} | {summary.get('maps', 0)} | {_fmt(med('cells_count'))} | {_fmt(med('benchmark_ready_ms'), ' ms')} | {_fmt(med('dom_nodes_total'))} | {_fmt(med('svg_elements_total'))} | {_fmt(med('long_task_count'))} | {_fmt(med('used_js_heap_bytes'))} | {_fmt(med('final_html_bytes'))} |")
    lines += ["", "## Double / single ratios", "", "| Dataset | Ready | DOM | JS heap |", "|---|---:|---:|---:|"]
    for dataset, ratios in report.get("ratios", {}).items():
        lines.append(f"| {dataset} | {_fmt(ratios.get('ready'))} | {_fmt(ratios.get('dom'))} | {_fmt(ratios.get('js_heap'))} |")
    long_tasks = sum(int(m.get("long_task_count") or 0) for i in report.get("iterations", []) for m in i.get("maps", []))
    lines += ["", f"Long tasks observed: **{'yes' if long_tasks else 'no'}**.", "",
              "## Limitations", "", "Metrics describe these cold browser contexts and are not an architectural recommendation. Heap metrics may be unavailable; CDP values apply to the whole Streamlit page, not an individual iframe.", ""]
    return "\n".join(lines)
