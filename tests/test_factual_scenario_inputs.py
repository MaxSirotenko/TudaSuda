from pathlib import Path
from datetime import date, timedelta

import warehouse_factual_data as factual
import warehouse_factual_scenario_inputs as adapter
import warehouse_outbound_experiment_ui as experiment_ui
from warehouse_business_identity import canonical_sku_key
from warehouse_pick_demands import build_outbound_pick_demands
from warehouse_scenario_comparison_ui import build_comparison_baseline
from warehouse_sku_velocity import build_sku_velocity_profile
from warehouse_inventory_placement import calculate_basic_weight_placement

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


def test_invalid_optional_outbound_pick_order_diagnostic_survives_historical_resolution(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([{**_outbound(), "source_pick_order": 1.5}]))
    monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, **k: _view([_placement(order=17)]))
    result = adapter.load_routed_outbound_for_day(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
        registry=_registry("outbound", "historical_placement"))
    assert result["authoritative"] and result["rows"][0]["pick_order"] == 17
    assert result["rows"][0]["pick_order_validation_reason"] == "optional_source_pick_order_invalid"
    demand = build_outbound_pick_demands(result["rows"])
    assert demand["readiness"]["route_sequence_authoritative"] is True
    assert demand["orders"][0]["demands"][0]["pick_order"] == 17
    assert demand["diagnostics"]["source_evidence_warning_counts"] == {
        "optional_source_pick_order_invalid": 1}


def test_missing_or_conflicting_historical_order_blocks_route(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([_outbound()]))
    for placement in ([_placement(order=None)], [_placement(order=1), {**_placement(order=2), "source_row": 3}]):
        monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, rows=placement, **k: _view(rows))
        result = adapter.load_routed_outbound_for_day(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
            registry=_registry("outbound", "historical_placement"))
        assert not result["authoritative"] and not result["rows"]
        assert result["blockers"][0]["code"] == "fact_cell_picking_order_missing_or_conflicting"


def test_fractional_historical_order_and_invalid_start_quantity_block(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([_outbound()]))
    monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, **k: _view([_placement(order=1.5)]))
    routed = adapter.load_routed_outbound_for_day(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
        registry=_registry("outbound", "historical_placement"))
    assert routed["blockers"][0]["code"] == "fact_cell_picking_order_missing_or_conflicting"
    monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, **k: _view([{**_placement(), "source_stock_quantity": None}]))
    start = adapter.build_start_state(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
        registry=_registry("historical_placement"))
    assert not start["authoritative"] and start["blockers"][0]["code"] == "historical_stock_quantity_invalid"


def test_invalid_factual_quantity_blocks_full_day_authority(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([_outbound(), {**_outbound(quantity=None), "source_row": 3}]))
    result = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry=_registry("outbound"))
    assert not result["authoritative"] and result["blockers"][0]["code"] == "factual_outbound_quantity_missing"
    assert any(row["quantity_validation_reason"] for row in result["rows"])


def test_unscoped_or_identityless_outbound_rows_are_blockers(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([
        {**_outbound(), "warehouse": ""}, {**_outbound(), "document_ref": "", "document_number": "", "source_row": 3}]))
    result = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry=_registry("outbound"))
    assert not result["authoritative"]
    assert {item["code"] for item in result["blockers"]} == {
        "factual_outbound_warehouse_missing", "factual_outbound_document_identity_missing"}


def test_scenario_opening_sku_gets_vgh_zone_without_receipt_day(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_rows", lambda kind, *a, **k: _view(
        [{"sku_key": SKU, "nomenclature": "SKU", "characteristic": "A", "weight": 2.5}] if kind == "vgh" else []))
    result = adapter.build_scenario_weight_classifications([_placement()], registry=_registry("vgh"), rules={"bands": _bands()})
    assert result["rows"][0]["calculated_zone"] == "light"
    assert result["diagnostics"]["SKU с подтверждённой зоной"] == 1


def test_unrelated_vgh_conflict_does_not_block_relevant_scenario_sku(monkeypatch):
    conflict = {"code": "conflicting_factual_business_key", "business_key": ["other"]}
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view(
        [{"sku_key": SKU, "nomenclature": "SKU", "characteristic": "A", "weight": 2.5}], [conflict]))
    result = adapter.build_scenario_weight_classifications([_placement()], registry=_registry("vgh"), rules={"bands": _bands()})
    assert result["authoritative"] and result["rows"][0]["calculated_zone"] == "light"
    assert result["diagnostics"]["unrelated_vgh_conflicts"] == 1


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


def test_velocity_disabled_does_not_call_history_loader(monkeypatch):
    monkeypatch.setattr(experiment_ui, "_cached_factual_outbound_history", lambda *a, **k:
        (_ for _ in ()).throw(AssertionError("history loader called")))
    assert experiment_ui.load_velocity_history_if_enabled(
        {"velocity": {"enabled": False}}, DAY, WAREHOUSE, _registry("outbound")) is None


def test_velocity_history_cache_reuses_same_source_revision(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "load_outbound_history", lambda *a, **k: calls.append(1) or
        {"rows": [], "authoritative": True, "blockers": []})
    experiment_ui._cached_factual_outbound_history.clear()
    config = {"velocity": {"enabled": True}}; registry = _registry("outbound")
    experiment_ui.load_velocity_history_if_enabled(config, DAY, WAREHOUSE, registry)
    experiment_ui.load_velocity_history_if_enabled(config, DAY, WAREHOUSE, registry)
    assert calls == [1]


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
    assert "load_outbound_for_day" in app


def _receipt(line_id, sku, *, zone="medium_light", eligible=True):
    return {"receipt_line_id": line_id, "receipt_number": line_id, "source_row_number": 1,
        "sku_key": sku, "sku_name": sku, "characteristic_name": "", "qty_pallets": 1,
        "qty_units": 1, "calculated_zone": zone, "weight_class": zone,
        "placement_eligible": eligible, "placement_status": "not_placed"}


def _medium_light_model():
    return {"model_id": "zones", "rows": [{"row_number": "1", "row_order": 1, "weight_zone": "medium_light"}],
        "cells": [{"cell_key": "1|1|1", "row_number": "1", "cell_number": "1", "tier": "1",
                   "row_order": 1, "capacity_pallets": 4, "weight_zone": "medium_light"},
                  {"cell_key": "1|2|1", "row_number": "1", "cell_number": "2", "tier": "1",
                   "row_order": 1, "capacity_pallets": 4, "weight_zone": "medium_light"}]}


def test_medium_light_receipt_places_in_canonical_medium_light_row():
    sku = canonical_sku_key({"nomenclature": "ML"})
    state, diagnostics = calculate_basic_weight_placement(
        _medium_light_model(), {"placements": [], "unplaced_inventory": []}, {"receipts": [_receipt("D1", sku)]})
    placed = [row for row in state["placements"] if row.get("receipt_line_ids") == ["D1"]]
    assert placed and placed[0]["weight_zone"] == "medium_light"
    assert not placed[0].get("zone_mismatch") and diagnostics.get("Не размещено паллет", 0) == 0


def test_pending_factual_receipt_is_classified_but_never_mutates_placement(monkeypatch):
    row = {"dataset_id": "r", "source_row": 2, "document_ref": "p", "document_number": "P",
        "occurred_at": DAY, "warehouse": WAREHOUSE, "sku_key": SKU, "nomenclature": "SKU",
        "characteristic": "A", "box_quantity": 1, "reported_pallets": 1, "terminal_completed": False}
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([row]))
    factual = adapter.load_receipts_for_day(DAY, WAREHOUSE, registry=_registry("receipts"))
    assert factual["classification_inputs"] and not factual["state"]["accepted_rows"]
    receipt = factual["classification_inputs"][0] | {"calculated_zone": "medium_light", "qty_pallets": 1}
    state, _ = calculate_basic_weight_placement(_medium_light_model(), {"placements": []}, {"receipts": [receipt]})
    assert not state["placements"]


def test_completed_receipt_with_invalid_box_quantity_is_blocked(monkeypatch):
    row = {"dataset_id": "r", "source_row": 2, "document_ref": "p", "occurred_at": DAY,
        "warehouse": WAREHOUSE, "sku_key": SKU, "box_quantity": None, "reported_pallets": 1,
        "terminal_completed": True}
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([row]))
    result = adapter.load_receipts_for_day(DAY, WAREHOUSE, registry=_registry("receipts"))
    assert not result["authoritative"] and not result["state"]["accepted_rows"]
    assert result["blockers"][0]["code"] == "factual_receipt_box_quantity_invalid"


def test_applying_d1_then_d2_preserves_d1_and_reapplying_d2_is_idempotent():
    model = _medium_light_model(); sku1 = canonical_sku_key({"nomenclature": "A"}); sku2 = canonical_sku_key({"nomenclature": "B"})
    first, _ = calculate_basic_weight_placement(model, {"placements": []}, {"receipts": [_receipt("D1", sku1)]})
    second, _ = calculate_basic_weight_placement(model, first, {"receipts": [_receipt("D2", sku2)]})
    repeated, _ = calculate_basic_weight_placement(model, second, {"receipts": [_receipt("D2", sku2)]})
    def quantities(state):
        return {sku: sum(float(row.get("qty_pallets") or 0) for row in state["placements"] if row.get("sku_key") == sku)
                for sku in (sku1, sku2)}
    assert quantities(second) == {sku1: 1.0, sku2: 1.0}
    assert quantities(repeated) == quantities(second)


def test_broken_enabled_velocity_history_is_explicitly_blocked_without_day_fallback():
    rows, blockers = experiment_ui.velocity_history_gate(
        {"authoritative": False, "rows": [{"created_at": DAY}], "blockers": [{"code": "prior_day_conflict"}]})
    assert rows == [] and blockers == [{"code": "prior_day_conflict"}]
    assert experiment_ui.velocity_history_gate(None) == (None, [])


def test_historical_blank_sku_is_factual_source_blocker(monkeypatch):
    monkeypatch.setattr(adapter, "load_effective_placement", lambda *a, **k: _view([{**_placement(), "sku_key": ""}]))
    result = adapter.build_start_state(DAY, WAREHOUSE, _model(), warehouse_binding=WAREHOUSE,
        registry=_registry("historical_placement"))
    assert not result["authoritative"] and result["blockers"][0]["code"] == "historical_sku_identity_invalid"


def test_receipt_boolean_normalization_for_existing_partition_scalars(monkeypatch):
    template = {"dataset_id": "r", "document_ref": "p", "occurred_at": DAY, "warehouse": WAREHOUSE,
        "sku_key": SKU, "box_quantity": 1, "reported_pallets": 1}
    for value in ("Да", 1, True):
        monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, value=value, **k: _view([{**template, "terminal_completed": value}]))
        result = adapter.load_receipts_for_day(DAY, WAREHOUSE, registry=_registry("receipts"))
        assert result["state"]["accepted_rows"] and result["classification_inputs"][0]["placement_eligible"] is True
    for value in ("Нет", 0, False, "unknown", None):
        monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, value=value, **k: _view([{**template, "terminal_completed": value}]))
        result = adapter.load_receipts_for_day(DAY, WAREHOUSE, registry=_registry("receipts"))
        assert not result["state"]["accepted_rows"] and result["state"]["pending_receipt_rows"]
        assert result["classification_inputs"][0]["placement_eligible"] is False


def test_outbound_picking_workspace_uses_factual_demand_not_historical_route_contract():
    source = Path("virtual_warehouse_app.py").read_text(encoding="utf-8")
    body = source[source.index("def render_outbound_picking"):source.index("def render_operation_history")]
    assert "load_outbound_for_day" in body and "load_routed_outbound_for_day" not in body
    assert "historical_binding" not in body
    assert "текущее mutable placement" in body


def test_receipt_conflict_in_other_warehouse_does_not_block_selected_scope(tmp_path):
    from tests.test_warehouse_factual_data import _effective_registry
    base = {"document_ref": "A", "document_number": "A", "line_number": 1, "occurred_at": DAY,
            "warehouse": "A", "sku_key": SKU, "box_quantity": 1, "terminal_completed": True}
    other = {**base, "document_ref": "B", "document_number": "B", "warehouse": "B"}
    registry = _effective_registry(tmp_path, "receipts", [[base, other], [{**other, "box_quantity": 2}]])
    selected = adapter.load_receipts_for_day(DAY, "A", registry=registry, root=tmp_path)
    blocked = adapter.load_receipts_for_day(DAY, "B", registry=registry, root=tmp_path)
    assert selected["authoritative"] and len(selected["state"]["accepted_rows"]) == 1
    assert not blocked["authoritative"] and blocked["blockers"]


def test_number_only_receipts_are_blocked_and_never_double_mutable_stock(monkeypatch):
    rows = [{"dataset_id": "r", "source_row": index, "document_ref": "", "document_number": "N",
             "line_number": 1, "occurred_at": DAY, "warehouse": WAREHOUSE, "sku_key": SKU,
             "box_quantity": 2, "terminal_completed": True} for index in (2, 3)]
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view(rows))
    result = adapter.load_receipts_for_day(DAY, WAREHOUSE, registry=_registry("receipts"))
    assert not result["authoritative"]
    assert result["blockers"][0]["code"] == "factual_receipt_document_identity_missing"
    assert result["state"]["accepted_rows"] == []


def test_blank_receipt_warehouse_is_visible_source_blocker(monkeypatch):
    valid = {"dataset_id": "r", "source_row": 2, "document_ref": "A", "occurred_at": DAY,
             "warehouse": WAREHOUSE, "sku_key": SKU, "box_quantity": 1, "terminal_completed": True}
    blank = {**valid, "source_row": 3, "document_ref": "B", "warehouse": ""}
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *a, **k: _view([valid, blank]))
    result = adapter.load_receipts_for_day(DAY, WAREHOUSE, registry=_registry("receipts"))
    blocker = next(item for item in result["blockers"] if item["code"] == "factual_receipt_warehouse_missing")
    assert not result["authoritative"] and blocker["source_rows"] == [3]
    assert len(result["state"]["accepted_rows"]) == 1
