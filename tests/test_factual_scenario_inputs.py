from pathlib import Path
from datetime import date, timedelta

import warehouse_factual_data as factual
import warehouse_factual_scenario_inputs as adapter
from warehouse_business_identity import canonical_sku_key
from warehouse_pick_demands import build_outbound_pick_demands
from warehouse_scenario_comparison_ui import build_comparison_baseline
from warehouse_sku_velocity import build_sku_velocity_profile

DAY = "2026-07-15"; WAREHOUSE = "Основной"
SKU = canonical_sku_key({"nomenclature": "SKU", "characteristic": "A"})


def _registry(*types, diagnostics=None):
    return {"diagnostics": list(diagnostics or []), "datasets": [{"dataset_id": f"dataset:{kind}",
        "source_type": kind, "active": True, "parser_version": "factual-july-v5", "artifact": f"/{kind}",
        "partitions": [DAY], "index": {"dates": [DAY],
        "warehouses": [] if kind in {"historical_placement", "vgh"} else [WAREHOUSE],
        "warehouses_by_date": {DAY: [] if kind in {"historical_placement", "vgh"} else [WAREHOUSE]},
        "daily": {DAY: {"warehouses": [] if kind in {"historical_placement", "vgh"} else [WAREHOUSE]}}}}
        for kind in types]}


def _outbound(day=DAY, quantity=10):
    return {"dataset_id": "o", "source_row": 2, "document_ref": "ro-1", "document_number": "РО-1",
        "occurred_at": day + "T10:00:00", "warehouse": WAREHOUSE, "line_number": 1,
        "nomenclature": "SKU", "characteristic": "A", "sku_key": SKU, "quantity": quantity}


def _placement(order=7, day=DAY):
    return {"dataset_id": "p", "source_row": 2, "sku_key": SKU, "nomenclature": "SKU", "characteristic": "A",
        "source_stock_quantity": 10, "source_pallet_ref": "P1", "source_cell": "A-1", "cell_picking_order": order,
        "snapshot_at": day, "resolved_geometry_cell_key": "1|1|1", "cell_resolution_status": "resolved"}


def _model():
    return {"model_id": "m", "cells": [{"cell_key": "1|1|1", "source_cell": "A-1", "row_number": "1",
        "cell_number": "1", "tier": "1", "storage_type": "normal", "capacity_pallets": 1}]}


def _view(rows, conflicts=None):
    return {"rows": rows, "duplicates": [], "conflicts": list(conflicts or []), "authoritative": not conflicts}


def _bands():
    return {"light": {"min": 0, "max": 5}, "medium_light": {"min": 5, "max": 10},
            "medium": {"min": 10, "max": 20}, "heavy": {"min": 20, "max": None}}


def test_indexed_selectors_and_normal_rerun_never_open_partitions(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: (_ for _ in ()).throw(AssertionError("partition opened")))
    registry = _registry("historical_placement", "outbound")
    assert adapter.available_warehouses(registry=registry) == [WAREHOUSE.casefold()]
    assert adapter.available_operational_dates(warehouse=WAREHOUSE, registry=registry) == [DAY]
    monkeypatch.setattr(factual, "_iter_jsonl", lambda *a, **k: (_ for _ in ()).throw(AssertionError("indexed rerun scanned")))
    assert factual.ensure_compact_scope_indexes(registry, source_types=("outbound",), root=tmp_path) is registry
    assert not (tmp_path / "registry.json").exists()


def test_factual_start_builds_ready_baseline_without_inventory_control(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, **k: _view([_placement()]))
    start = adapter.build_start_state(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
                                      registry=_registry("historical_placement"))
    baseline, diagnostics = build_comparison_baseline(_model(), start["state"], None,
        normalized_warehouse=WAREHOUSE, operational_date=DAY, inventory_control_supplied=False)
    assert start["state"]["physical_opening_readiness"]["opening_stock_business_ready"] is True
    assert baseline is not None and baseline["readiness"]["opening_stock_business_ready"] is True
    assert diagnostics["opening_stock"]["inventory_totals_control_status"] == "not_supplied"


def test_factual_inventory_actual_quantity_is_evidence_not_boxes(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([{"sku_key": SKU,
        "nomenclature": "SKU", "characteristic": "A", "warehouse": WAREHOUSE, "actual_quantity": 10}]))
    result = adapter.load_inventory_for_day(DAY, WAREHOUSE, registry=_registry("inventory"))
    assert result["rows"] == [] and result["evidence_rows"][0]["actual_quantity"] == 10
    assert result["diagnostics"]["automatic_box_control_available"] is False


def test_outbound_without_source_order_uses_historical_cell_order(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([_outbound()]))
    monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, **k: _view([_placement(order=17)]))
    result = adapter.load_routed_outbound_for_day(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
        registry=_registry("outbound", "historical_placement"))
    assert result["authoritative"] and result["rows"][0]["pick_order"] == 17
    demand = build_outbound_pick_demands(result["rows"])
    assert demand["readiness"]["route_sequence_authoritative"] is True
    assert demand["orders"][0]["demands"][0]["pick_order"] == 17


def test_missing_or_conflicting_historical_order_blocks_route(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([_outbound()]))
    for placement in ([_placement(order=None)], [_placement(order=1), {**_placement(order=2), "source_row": 3}]):
        monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, rows=placement, **k: _view(rows))
        result = adapter.load_routed_outbound_for_day(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
            registry=_registry("outbound", "historical_placement"))
        assert not result["authoritative"] and not result["rows"]
        assert result["blockers"][0]["code"] == "fact_cell_picking_order_missing_or_conflicting"


def test_invalid_factual_quantity_blocks_full_day_authority(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([_outbound(), {**_outbound(quantity=None), "source_row": 3}]))
    result = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry=_registry("outbound"))
    assert not result["authoritative"] and result["blockers"][0]["code"] == "factual_outbound_quantity_missing"
    assert any(row["quantity_validation_reason"] for row in result["rows"])


def test_scenario_opening_sku_gets_vgh_zone_without_receipt_day(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda kind, *a, **k: _view(
        [{"sku_key": SKU, "nomenclature": "SKU", "characteristic": "A", "weight": 2.5}] if kind == "vgh" else []))
    result = adapter.build_scenario_weight_classifications([_placement()], registry=_registry("vgh"), rules={"bands": _bands()})
    assert result["rows"][0]["calculated_zone"] == "light"
    assert result["diagnostics"]["SKU с подтверждённой зоной"] == 1


def test_velocity_loader_reads_exact_28_prior_days(monkeypatch):
    calls = []
    def rows(kind, day=None, **kwargs):
        calls.append(day); return _view([_outbound(day)])
    monkeypatch.setattr(adapter, "load_effective_rows", rows)
    registry = _registry("outbound"); end = date.fromisoformat(DAY)
    dates = [(end - timedelta(days=offset)).isoformat() for offset in range(28, 0, -1)]
    registry["datasets"][0]["index"]["dates"] = dates
    history = adapter.load_outbound_history(DAY, WAREHOUSE, registry=registry)
    profile, _ = build_sku_velocity_profile(history["rows"], as_of_date=DAY, target_normalized_warehouse=WAREHOUSE)
    assert len(calls) == 28 and DAY not in calls
    assert profile["summary"]["accepted_history_rows"] == 28
    assert profile["summary"]["history_span_complete"] is True


def test_registry_activation_review_blocks_scenario_without_legacy_fallback(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must block before partition")))
    registry = _registry("outbound", diagnostics=[{"code": "registry_activation_review_required", "logical_source_id": "x"}])
    result = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry=registry)
    assert not result["authoritative"] and result["blockers"][0]["code"] == "registry_activation_review_required"


def test_d_plus_one_snapshot_is_read_only_optional_end(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_placement", lambda day, *a, **k: _view([_placement(day=day)]))
    end = adapter.build_start_state("2026-07-16", WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
                                    registry=_registry("historical_placement"))
    assert end["authoritative"] and end["state"]["placements"]
    assert end["operational_date"] == "2026-07-16"


def test_manual_inventory_fallback_and_factual_main_screens_are_present():
    experiment = Path("warehouse_outbound_experiment_ui.py").read_text(encoding="utf-8")
    app = Path("virtual_warehouse_app.py").read_text(encoding="utf-8")
    assert "get_inventory_results_sheet_names" in experiment and "select_inventory_rows_for_opening_stock" in experiment
    assert '["Factual Data Layer", "Ручной fallback"]' in app
    assert "load_routed_outbound_for_day" in app
