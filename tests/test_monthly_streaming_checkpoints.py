import json
from pathlib import Path

import pytest
import warehouse_monthly_fact_replay as fact
from warehouse_monthly_placement_comparison import load_fact_day, load_fact_result


def _day(day):
    return {"operational_day": day, "status": "ready", "orders_total": 1, "orders_strict": 1,
        "requested_boxes": 2, "picked_boxes": 2, "shortage_boxes": 0, "picker_distance_m": 10,
        "source_location_ambiguity_count": 0, "unroutable_cell_count": 0,
        "missing_opening_sku_count": 0, "blockers": [], "order_results": [{"strict_comparable": True}]}


def _ready(signature="data"):
    return {"monthly_replay_ready": True, "hard_blockers": [], "warnings": [],
            "active_dataset_signature": signature, "cell_mapping_signature": "cells"}


def _patch(monkeypatch, calls):
    monkeypatch.setattr(fact, "build_physical_warehouse_graph", lambda *_: ({"gate_links": [{}], "cell_access_links": []}, {}))
    monkeypatch.setattr(fact, "load_effective_placement", lambda *a, **k: {})
    monkeypatch.setattr(fact, "load_effective_rows", lambda *a, **k: {})
    monkeypatch.setattr(fact, "_replay_day", lambda day, *a: calls.append(day) or _day(day))


def test_fact_stops_resumes_skips_and_repairs_corrupt_checkpoint(tmp_path, monkeypatch):
    calls = []; _patch(monkeypatch, calls)
    def stop(event):
        if event["phase"] == "day_completed" and event["day_index"] == 2: raise RuntimeError("stop")
    with pytest.raises(RuntimeError):
        fact.replay_monthly_fact({}, {}, "2026-07-01", "2026-07-03", root=tmp_path,
            registry={"datasets": []}, readiness=_ready(), progress_callback=stop)
    assert calls == ["2026-07-01", "2026-07-02"]
    result = fact.replay_monthly_fact({}, {}, "2026-07-01", "2026-07-03", root=tmp_path,
        registry={"datasets": []}, readiness=_ready())
    assert calls == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert result["days_completed"] == 3 and "daily_results" not in result
    artifact = Path(result["artifact_path"]); (artifact / "day=2026-07-02.json").write_text("broken", encoding="utf-8")
    fact.replay_monthly_fact({}, {}, "2026-07-01", "2026-07-03", root=tmp_path,
        registry={"datasets": []}, readiness=_ready())
    assert calls[-1] == "2026-07-02"
    assert not list(artifact.glob("*.tmp"))


def test_signature_change_uses_isolated_checkpoint_and_lazy_day_load(tmp_path, monkeypatch):
    calls = []; _patch(monkeypatch, calls)
    first = fact.replay_monthly_fact({}, {}, "2026-07-01", "2026-07-01", root=tmp_path,
        registry={"datasets": []}, readiness=_ready("a"))
    second = fact.replay_monthly_fact({}, {}, "2026-07-01", "2026-07-01", root=tmp_path,
        registry={"datasets": []}, readiness=_ready("b"))
    assert first["artifact_path"] != second["artifact_path"] and len(calls) == 2
    compact = load_fact_result(first["artifact_path"])
    assert "daily_results" not in compact
    assert load_fact_day(first["artifact_path"], "2026-07-01")["orders_total"] == 1
    stored = json.loads((Path(first["artifact_path"]) / "summary.json").read_text(encoding="utf-8"))
    assert stored["format_version"] == "monthly-fact-v2"


def _order(day, distance):
    return {"order_identity": {"document_ref": day, "document_number": day, "occurred_at": day},
        "operational_day": day, "strict_comparable": True, "demand_signature": "same",
        "requested_boxes": 1, "picker_distance_m": distance, "pick_events": [], "route_legs": []}


def _artifact(root, name, distance, signature):
    path = root / name; path.mkdir()
    days = []
    for day in ("2026-07-01", "2026-07-02"):
        filename = f"day={day}.json"; days.append(filename)
        (path / filename).write_text(json.dumps({"operational_day": day, "order_results": [_order(day, distance)]}), encoding="utf-8")
    (path / "summary.json").write_text(json.dumps({"period_from": "2026-07-01", "period_to": "2026-07-02",
        "format_version": "monthly-fact-v2", "input_signature": signature, "daily_artifacts": days}), encoding="utf-8")
    return load_fact_result(path)


def test_comparison_v2_is_partitioned_compact_and_resumable(tmp_path):
    from warehouse_monthly_placement_comparison import compare_monthly_placement, load_comparison_day
    factual = _artifact(tmp_path, "fact", 20, "fact")
    proposed = _artifact(tmp_path, "proposed", 10, "proposed")
    stopped = []
    def stop(event):
        if event["phase"] == "day_completed": stopped.append(event["operational_day"]); raise RuntimeError("stop")
    kwargs = dict(fact_result=factual, proposed_result=proposed, proposed_assignments=[],
        proposed_placement_signature="placement", warehouse_model_signature="geometry",
        routing_version="physical-v1", ruleset_signature="rules", root=tmp_path)
    with pytest.raises(RuntimeError): compare_monthly_placement(**kwargs, progress_callback=stop)
    result = compare_monthly_placement(**kwargs)
    assert result["comparison_version"] == "monthly-placement-only-v2"
    assert "order_comparisons" not in result and result["days_completed"] == 2
    stored = json.loads((Path(result["artifact_path"]) / "comparison.json").read_text(encoding="utf-8"))
    assert "order_comparisons" not in stored
    assert load_comparison_day(result["artifact_path"], "2026-07-01")["order_comparisons"][0]["saved_meters"] == 10
