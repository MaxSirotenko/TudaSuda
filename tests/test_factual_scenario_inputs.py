from pathlib import Path

import warehouse_factual_scenario_inputs as adapter
import warehouse_factual_data as factual
from warehouse_business_identity import canonical_sku_key
from warehouse_pick_demands import build_outbound_pick_demands
from warehouse_scenario_comparison_ui import build_comparison_baseline

DAY = "2026-07-15"; WAREHOUSE = "Основной"
SKU = canonical_sku_key({"nomenclature": "SKU", "characteristic": "A"})


def _registry(*types):
    return {"datasets": [{"dataset_id": f"dataset:{kind}", "source_type": kind, "active": True,
        "artifact": f"/{kind}", "partitions": [DAY], "index": {"dates": [DAY],
        "warehouses": [] if kind in {"historical_placement", "vgh"} else [WAREHOUSE],
        "warehouses_by_date": {DAY: [] if kind in {"historical_placement", "vgh"} else [WAREHOUSE]},
        "daily": {DAY: {"warehouses": [] if kind in {"historical_placement", "vgh"} else [WAREHOUSE]}}}}
        for kind in types]}


def _rows():
    return {
        "outbound": [{"document_ref": "ro-1", "document_number": "РО-1", "occurred_at": DAY + "T10:00:00",
            "warehouse": WAREHOUSE, "line_number": 1, "nomenclature": "SKU", "characteristic": "A",
            "sku_key": SKU, "quantity": 10}],
        "receipts": [{"dataset_id": "r", "source_row": 2, "document_ref": "po-1", "document_number": "ПО-1",
            "occurred_at": DAY + "T09:00:00", "warehouse": WAREHOUSE, "line_number": 1,
            "nomenclature": "SKU", "characteristic": "A", "sku_key": SKU, "box_quantity": 3}],
        "inventory": [{"inventory_ref": "i", "line_number": 1, "occurred_at": DAY, "warehouse": WAREHOUSE,
            "nomenclature": "SKU", "characteristic": "A", "sku_key": SKU, "actual_quantity": 10}],
        "vgh": [{"sku_key": SKU, "nomenclature": "SKU", "characteristic": "A", "weight": 2.5}],
    }


def _patch_views(monkeypatch, *, conflict=None):
    rows = _rows()
    monkeypatch.setattr(adapter, "load_effective_rows", lambda kind, day=None, **kw:
        {"rows": [] if conflict else rows.get(kind, []), "duplicates": [], "conflicts": [conflict] if conflict else [],
         "authoritative": not conflict})
    monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, **kw: {"rows": [{
        "dataset_id": "p", "source_row": 2, "sku_key": SKU, "nomenclature": "SKU", "characteristic": "A",
        "source_stock_quantity": 10, "source_pallet_ref": "P1", "source_cell": "A-1",
        "resolved_geometry_cell_key": "1|1|1", "cell_resolution_status": "resolved"}],
        "duplicates": [], "conflicts": [], "authoritative": True})


def _model():
    return {"model_id": "m", "cells": [{"cell_key": "1|1|1", "source_cell": "A-1", "row_number": "1",
        "cell_number": "1", "tier": "1", "storage_type": "normal", "capacity_pallets": 1}]}


def _bands():
    return {"light": {"min": 0, "max": 5}, "medium_light": {"min": 5, "max": 10},
            "medium": {"min": 10, "max": 20}, "heavy": {"min": 20, "max": None}}


def test_selectors_use_only_compact_registry_metadata(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: (_ for _ in ()).throw(AssertionError("partition opened")))
    registry = _registry("historical_placement", "outbound")
    assert adapter.available_warehouses(registry=registry) == [WAREHOUSE.casefold()]
    assert adapter.available_operational_dates(warehouse=WAREHOUSE, registry=registry) == [DAY]


def test_normal_registry_upgrade_rerun_does_not_open_partitions(monkeypatch, tmp_path):
    registry = _registry("outbound")
    monkeypatch.setattr(factual, "_iter_jsonl", lambda *a, **k:
        (_ for _ in ()).throw(AssertionError("partition opened on indexed rerun")))
    assert factual.ensure_compact_scope_indexes(registry, root=tmp_path) is registry
    assert not (tmp_path / "registry.json").exists()


def test_factual_start_inventory_baseline_and_readiness(monkeypatch):
    _patch_views(monkeypatch); registry = _registry("historical_placement", "outbound", "inventory")
    start = adapter.build_start_state(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE, registry=registry)
    inventory = adapter.load_inventory_for_day(DAY, WAREHOUSE, registry=registry)
    assert start["authoritative"] and start["state"]["physical_opening_readiness"]["opening_stock_business_ready"]
    baseline, diagnostics = build_comparison_baseline(_model(), start["state"], inventory["rows"],
        normalized_warehouse=WAREHOUSE, operational_date=DAY, inventory_control_supplied=True)
    assert baseline is not None and baseline["readiness"]["opening_stock_business_ready"] is True
    assert diagnostics["opening_stock"]["inventory_totals_control_status"] == "agrees"
    assert diagnostics["opening_stock"]["inventory_total_mismatch_details"] == []


def test_historical_warehouse_requires_explicit_binding(monkeypatch):
    _patch_views(monkeypatch)
    result = adapter.build_start_state(DAY, WAREHOUSE, _model(), registry=_registry("historical_placement"))
    assert not result["authoritative"]
    assert result["blockers"][0]["code"] == "historical_placement_warehouse_binding_required"


def test_outbound_without_source_pick_order_uses_physical_route_path(monkeypatch):
    _patch_views(monkeypatch)
    outbound = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry=_registry("outbound"))
    assert outbound["rows"][0]["pick_order"] is None
    demand = build_outbound_pick_demands(outbound["rows"])
    assert demand["readiness"]["route_sequence_authoritative"] is True
    assert demand["orders"] and demand["orders"][0]["demands"]


def test_factual_receipts_vgh_and_configured_bands_build_zones(monkeypatch):
    _patch_views(monkeypatch)
    result = adapter.build_factual_weight_classifications(DAY, WAREHOUSE,
        registry=_registry("receipts", "vgh"), rules={"bands": _bands()})
    assert result["authoritative"] and result["rows"][0]["calculated_zone"] == "light"
    assert result["diagnostics"]["SKU с подтверждённой зоной"] == 1


def test_factual_outbound_wins_without_reading_conflicting_legacy(monkeypatch):
    _patch_views(monkeypatch)
    result = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry=_registry("outbound"))
    assert result["authoritative"] and result["rows"][0]["warehouse"] == WAREHOUSE


def test_conflicting_factual_never_falls_back(monkeypatch):
    conflict = {"code": "conflicting_factual_business_key"}; _patch_views(monkeypatch, conflict=conflict)
    result = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry=_registry("outbound"))
    assert result["rows"] == [] and result["blockers"] == [conflict] and result["source"] == "factual"


def test_main_receipt_and_outbound_screens_default_to_factual_with_guarded_manual_uploads():
    source = Path("virtual_warehouse_app.py").read_text(encoding="utf-8")
    receipt = source[source.index("def render_receipts_section"):source.index("def _current_warehouse_state")]
    outbound = source[source.index("def render_outbound_picking"):source.index("def render_operation_history")]
    assert '["Factual Data Layer", "Ручной fallback"]' in receipt
    assert "build_factual_weight_classifications" in receipt
    assert 'if source_mode != "Ручной fallback"' in receipt
    assert '["Factual Data Layer", "Ручной fallback"]' in outbound
    assert "load_outbound_for_day" in outbound
    assert 'if source_mode == "Ручной fallback"' in outbound
