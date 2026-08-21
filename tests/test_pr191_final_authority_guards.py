import warehouse_factual_scenario_inputs as adapter
from warehouse_monthly_fact_replay import resolve_factual_route_order


DAY = "2026-07-15"
WAREHOUSE = "Основной"


def _effective_outbound(row):
    return {"rows": [row], "conflicts": [], "duplicates": [], "authoritative": True}


def _outbound_row(**overrides):
    row = {
        "dataset_id": "dataset:o",
        "source_row": 2,
        "document_ref": "ro-1",
        "document_number": "РО-1",
        "occurred_at": DAY + "T10:00:00",
        "warehouse": WAREHOUSE,
        "line_number": 1,
        "sku_key": "sku",
        "nomenclature": "SKU",
        "characteristic": "A",
        "quantity": 10,
        "source_pick_order": None,
    }
    row.update(overrides)
    return row


def test_outbound_without_line_number_is_not_authoritative(monkeypatch):
    monkeypatch.setattr(adapter, "active_datasets", lambda registry, source_type=None: [])
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *args, **kwargs: _effective_outbound(
        _outbound_row(line_number=None)))

    result = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry={"datasets": [], "diagnostics": []})

    assert result["authoritative"] is False
    assert any(item["code"] == "factual_outbound_line_identity_missing" for item in result["blockers"])


def test_outbound_number_date_fallback_still_requires_line_number(monkeypatch):
    monkeypatch.setattr(adapter, "active_datasets", lambda registry, source_type=None: [])
    monkeypatch.setattr(adapter, "load_effective_rows", lambda *args, **kwargs: _effective_outbound(
        _outbound_row(document_ref=None, line_number="  ")))

    result = adapter.load_outbound_for_day(DAY, WAREHOUSE, registry={"datasets": [], "diagnostics": []})

    assert result["authoritative"] is False
    assert any(item["code"] == "factual_outbound_line_identity_missing" for item in result["blockers"])


def _receipt_row(document_ref, line_number, terminal_completed):
    return {
        "dataset_id": "dataset:r",
        "source_row": line_number + 1,
        "document_ref": document_ref,
        "document_number": document_ref,
        "occurred_at": DAY + "T08:00:00",
        "warehouse": WAREHOUSE,
        "line_number": line_number,
        "sku_key": f"sku-{line_number}",
        "nomenclature": f"SKU {line_number}",
        "characteristic": "A",
        "box_quantity": 10,
        "reported_pallets": 1,
        "terminal_completed": terminal_completed,
        "expected_receipt": True,
    }


def _scoped_receipts(rows):
    return {"source": "factual", "source_type": "receipts", "operational_date": DAY,
            "warehouse": WAREHOUSE, "rows": rows, "authoritative": True,
            "blockers": [], "duplicates": []}


def test_receipt_mixed_true_false_completion_blocks_whole_input(monkeypatch):
    rows = [_receipt_row("r-1", 1, True), _receipt_row("r-1", 2, False)]
    monkeypatch.setattr(adapter, "_scoped", lambda *args, **kwargs: _scoped_receipts(rows))

    result = adapter.load_receipts_for_day(DAY, WAREHOUSE)

    assert result["authoritative"] is False
    assert result["state"]["accepted_rows"] == []
    assert any(item["code"] == "factual_receipt_terminal_completion_inconsistent" for item in result["blockers"])


def test_receipt_true_unknown_completion_blocks_whole_input(monkeypatch):
    rows = [_receipt_row("r-1", 1, True), _receipt_row("r-1", 2, None)]
    monkeypatch.setattr(adapter, "_scoped", lambda *args, **kwargs: _scoped_receipts(rows))

    result = adapter.load_receipts_for_day(DAY, WAREHOUSE)

    assert result["authoritative"] is False
    assert result["state"]["accepted_rows"] == []
    assert any(item["code"] == "factual_receipt_terminal_completion_inconsistent" for item in result["blockers"])


def test_receipt_completion_can_differ_between_documents(monkeypatch):
    rows = [_receipt_row("r-1", 1, True), _receipt_row("r-2", 2, False)]
    monkeypatch.setattr(adapter, "_scoped", lambda *args, **kwargs: _scoped_receipts(rows))

    result = adapter.load_receipts_for_day(DAY, WAREHOUSE)

    assert not any(item["code"] == "factual_receipt_terminal_completion_inconsistent" for item in result["blockers"])
    assert result["authoritative"] is True
    assert len(result["state"]["accepted_rows"]) == 1
    assert len(result["state"]["pending_receipt_rows"]) == 1


def _route_candidate(order):
    return {"source_cell": "1-1-01", "resolved_geometry_cell_key": "cell-1", "cell_picking_order": order}


def test_route_authority_requires_pick_order_on_every_candidate():
    assert resolve_factual_route_order([_route_candidate(17), _route_candidate(None)])["code"] == \
        "fact_cell_picking_order_missing_or_conflicting"
    assert resolve_factual_route_order([_route_candidate(17), _route_candidate("bad")])["code"] == \
        "fact_cell_picking_order_missing_or_conflicting"
    assert resolve_factual_route_order([_route_candidate(17), _route_candidate(18)])["code"] == \
        "fact_cell_picking_order_missing_or_conflicting"


def test_route_authority_accepts_complete_consistent_pick_order():
    result = resolve_factual_route_order([_route_candidate(17), _route_candidate(17)])
    assert result["code"] is None
    assert result["cell_picking_order"] == 17
