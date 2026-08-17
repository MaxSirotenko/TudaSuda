from pathlib import Path

import warehouse_factual_scenario_inputs as adapter


def _registry(*types):
    return {"datasets": [{"dataset_id": f"dataset:{kind}", "source_type": kind, "active": True,
        "artifact": f"/{kind}", "partitions": ["2026-07-15"], "index": {"dates": ["2026-07-15"]}}
        for kind in types]}


def test_only_factual_builds_current_inputs(monkeypatch):
    registry = _registry("historical_placement", "outbound", "receipts", "inventory", "vgh")
    rows = {
        "outbound": [{"document_ref": "ro-1", "document_number": "РО-1", "occurred_at": "2026-07-15T10:00:00",
            "warehouse": "Основной", "line_number": 1, "nomenclature": "SKU", "characteristic": "A", "sku_key": "sku|a", "quantity": 7, "source_pick_order": 1}],
        "receipts": [{"document_ref": "po-1", "document_number": "ПО-1", "occurred_at": "2026-07-15T09:00:00",
            "warehouse": "Основной", "line_number": 1, "nomenclature": "SKU", "characteristic": "A", "sku_key": "sku|a", "box_quantity": 3}],
        "inventory": [{"occurred_at": "2026-07-15", "warehouse": "Основной", "sku_key": "sku|a", "actual_quantity": 10}],
        "vgh": [{"sku_key": "sku|a", "weight": 2.5}],
    }
    monkeypatch.setattr(adapter, "load_effective_rows", lambda kind, day=None, **kw:
        {"rows": rows.get(kind, []), "duplicates": [], "conflicts": [], "authoritative": True})
    monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, **kw: {"rows": [{
        "dataset_id": "p", "source_row": 2, "sku_key": "sku|a", "nomenclature": "SKU", "characteristic": "A",
        "source_stock_quantity": 10, "source_pallet_ref": "P1", "source_cell": "A-1",
        "resolved_geometry_cell_key": "cell-1", "cell_resolution_status": "resolved"}],
        "duplicates": [], "conflicts": [], "authoritative": True})
    model = {"model_id": "m", "cells": [{"cell_key": "cell-1", "source_cell": "A-1"}]}
    start = adapter.build_start_state("2026-07-15", "Основной", model, registry=registry, root=Path("/unused"))
    outbound = adapter.load_outbound_for_day("2026-07-15", "Основной", registry=registry, root=Path("/unused"))
    receipts = adapter.load_receipts_for_day("2026-07-15", "Основной", registry=registry, root=Path("/unused"))
    assert start["authoritative"] and start["state"]["placements"][0]["cell_key"] == "cell-1"
    assert outbound["rows"][0]["qty_units"] == 7 and outbound["rows"][0]["route_sequence_authoritative"]
    assert receipts["state"]["accepted_rows"][0]["qty_units"] == 3
    assert adapter.load_vgh_attributes(registry=registry, root=Path("/unused"))["rows"][0]["weight"] == 2.5


def test_factual_outbound_wins_without_reading_legacy(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **kw: {"rows": [{"document_ref": "r", "occurred_at": "2026-07-15",
        "warehouse": "Новый склад", "sku_key": "s", "quantity": 2, "source_pick_order": 1}],
        "duplicates": [], "conflicts": [], "authoritative": True})
    result = adapter.load_outbound_for_day("2026-07-15", "Новый склад", registry=_registry("outbound"))
    assert result["authoritative"] and result["rows"][0]["warehouse"] == "Новый склад"


def test_conflicting_factual_never_falls_back(monkeypatch):
    conflict = {"code": "conflicting_factual_business_key"}
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **kw:
        {"rows": [], "duplicates": [], "conflicts": [conflict], "authoritative": False})
    result = adapter.load_outbound_for_day("2026-07-15", "W", registry=_registry("outbound"))
    assert result["rows"] == [] and result["blockers"] == [conflict] and result["source"] == "factual"
