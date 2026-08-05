import copy
import importlib
import json
from pathlib import Path

import pytest

from warehouse_receipt_snapshot_transitions import analyze_receipt_snapshot_transitions


def model(*keys):
    return {"cells": [{"cell_key": k, "row_number": i + 1, "cell_number": i + 1, "tier": 1, "weight_zone": "heavy", "physical_index": i} for i, k in enumerate(keys)]}


def receipt(*items, dataset_id="D1"):
    keys = [i[0] for i in items]
    return {"dataset_id": dataset_id, "operational_date": "2026-08-05", "receipt_sku_batches": [
        {"receipt_batch_key": k, "operational_date": "2026-08-05", "warehouse": wh, "normalized_warehouse": wh, "sku_key": sku, "qty_units": qty, "unit_name": unit}
        for k, wh, sku, qty, unit in items], "scenario_inputs": {"current": {"receipt_dataset_id": dataset_id, "total_boxes": sum(i[3] for i in items if isinstance(i[3], int) and not isinstance(i[3], bool)), "receipt_batch_keys": keys}, "proposed": {"receipt_dataset_id": dataset_id, "total_boxes": sum(i[3] for i in items if isinstance(i[3], int) and not isinstance(i[3], bool)), "receipt_batch_keys": list(reversed(keys))}}}


def p(pid, wh, cell, sku, prod="2026-01-01", **extra):
    d = {"placement_id": pid, "warehouse": wh, "cell_key": cell, "sku_key": sku, "production_date": prod}
    d.update(extra)
    return d


def snap(*placements, excluded=None, unmatched=None, unplaced=None):
    return {"placements": list(placements), "excluded_inventory": excluded or [], "unmatched_inventory": unmatched or [], "unplaced_inventory": unplaced or []}


def run(m=None, r=None, s=None, e=None):
    return analyze_receipt_snapshot_transitions(m or model("C1"), r or receipt(), s or snap(), e or snap())


def by_cell(state, cell):
    return next(t for t in state["cell_transitions"] if t["cell_key"] == cell)


def test_empty_json_serializable_inputs_unchanged_and_deterministic():
    m, r, s, e = model("C1"), receipt(), snap(), snap()
    before = copy.deepcopy((m, r, s, e))
    state1, diag1 = run(m, r, s, e)
    state2, diag2 = run(m, r, s, e)
    json.dumps(state1, ensure_ascii=False); json.dumps(diag1, ensure_ascii=False)
    assert (m, r, s, e) == before
    assert state1 == state2 and diag1 == diag2
    assert state1["receipt_batches"] == []


def test_permutations_and_snapshot_change_rules():
    m = model("C1", "C2")
    r1 = receipt(("B2", "Warehouse A", "SKU2", 1, "короб"), ("B1", "Warehouse A", "SKU1", 1, "короб"))
    r2 = copy.deepcopy(r1); r2["receipt_sku_batches"].reverse()
    s1 = snap(p("1", "Warehouse A", "C1", "SKU0"), p("2", "Warehouse A", "C2", "SKU2", qty_units=999))
    s2 = snap(*reversed(s1["placements"]))
    e1 = snap(p("3", "Warehouse A", "C1", "SKU1"), p("4", "Warehouse A", "C2", "SKU2"))
    e2 = snap(*reversed(e1["placements"]))
    a, _ = run(m, r1, s1, e1); b, _ = run(m, r2, s2, e2)
    assert a["start_snapshot_id"] == b["start_snapshot_id"]
    assert a["end_snapshot_id"] == b["end_snapshot_id"]
    assert a["analysis_id"] == b["analysis_id"]
    changed_cell, _ = run(model("C9", "C2"), r1, snap(p("1", "Warehouse A", "C9", "SKU0"), p("2", "Warehouse A", "C2", "SKU2")), e1)
    changed_sku, _ = run(m, r1, snap(p("1", "Warehouse A", "C1", "OTHER"), p("2", "Warehouse A", "C2", "SKU2")), e1)
    assert a["start_snapshot_id"] != changed_cell["start_snapshot_id"]
    assert a["start_snapshot_id"] != changed_sku["start_snapshot_id"]


def test_configuration_error_returns_empty_analysis():
    r = receipt(("B1", "W", "S", 1, "короб")); r["scenario_inputs"]["proposed"]["total_boxes"] = 2
    state, diag = run(model("C1"), r, snap(), snap())
    assert state["cell_transitions"] == []
    assert diag["configuration_errors"] == ["receipt_scenario_inputs_not_equal"]


@pytest.mark.parametrize("batch,reason", [
    ("bad", "receipt_batch_not_mapping"),
    ({"receipt_batch_key": "", "normalized_warehouse": "W", "sku_key": "S", "qty_units": 1, "unit_name": "короб"}, "receipt_batch_key_missing"),
    ({"receipt_batch_key": "B", "normalized_warehouse": "", "sku_key": "S", "qty_units": 1, "unit_name": "короб"}, "receipt_batch_warehouse_missing"),
    ({"receipt_batch_key": "B", "normalized_warehouse": "W", "sku_key": "", "qty_units": 1, "unit_name": "короб"}, "receipt_batch_sku_missing"),
    ({"receipt_batch_key": "B", "normalized_warehouse": "W", "sku_key": "S", "unit_name": "короб"}, "receipt_batch_qty_missing"),
    ({"receipt_batch_key": "B", "normalized_warehouse": "W", "sku_key": "S", "qty_units": True, "unit_name": "короб"}, "receipt_batch_qty_invalid"),
    ({"receipt_batch_key": "B", "normalized_warehouse": "W", "sku_key": "S", "qty_units": 1.5, "unit_name": "короб"}, "receipt_batch_qty_invalid"),
    ({"receipt_batch_key": "B", "normalized_warehouse": "W", "sku_key": "S", "qty_units": 0, "unit_name": "короб"}, "receipt_batch_qty_non_positive"),
    ({"receipt_batch_key": "B", "normalized_warehouse": "W", "sku_key": "S", "qty_units": 1, "unit_name": "шт"}, "receipt_batch_unit_not_boxes"),
])
def test_invalid_receipt_batches(batch, reason):
    r = receipt(); r["receipt_sku_batches"] = [batch]; r["scenario_inputs"]["current"]["receipt_batch_keys"] = []; r["scenario_inputs"]["proposed"]["receipt_batch_keys"] = []
    state, diag = run(model("C1"), r, snap(), snap())
    assert state["invalid_receipt_batches"][0]["reason"] == reason
    assert diag["invalid_receipt_batches"] == 1


def test_units_and_duplicate_batch_do_not_double_count():
    r = receipt(("B1", "W", "S1", 2, "короб"), ("B2", "W", "S2", 3, "короба"), ("B3", "W", "S3", 4, "коробов"))
    r["receipt_sku_batches"].append({**r["receipt_sku_batches"][0], "qty_units": 100})
    state, diag = run(model("C1"), r, snap(), snap())
    assert [b["unit_name"] for b in state["receipt_batches"]] == ["короб", "короб", "короб"]
    assert diag["receipt_boxes"] == 9
    assert state["invalid_receipt_batches"][-1]["reason"] == "duplicate_receipt_batch_key"


def test_model_cells_source_unknown_duplicate_and_placement_validation():
    m = {"cells": [{"cell_key": "C1"}, {"cell_key": "C1"}]}
    s = snap("x", p("", "W", "C1", "S"), p("2", "", "C1", "S"), p("3", "W", "", "S"), p("4", "W", "C1", ""), p("5", "W", "BAD", "S"))
    state, diag = run(m, receipt(("B", "W", "S", 1, "короб")), s, snap())
    assert diag["duplicate_model_cell_keys"] == 1
    assert diag["valid_start_snapshot_evidence"] == 0
    assert [x["reason"] for x in state["invalid_start_snapshot_evidence"]] == ["snapshot_placement_not_mapping", "placement_id_missing", "placement_warehouse_missing", "placement_cell_key_missing", "placement_sku_missing", "unknown_model_cell"]


def test_excluded_unmatched_unplaced_not_occupancy_and_duplicate_evidence_dates():
    m = model("C1", "C2")
    r = receipt(("B", "W", "S", 1, "короб"))
    s = snap(p("1", "W", "C1", "S", "2026-01-01", qty_units=1), p("2", "W", "C1", "S", "2026-01-02", qty_units=999), excluded=[p("x", "W", "C2", "S")], unmatched=[p("y", "W", "C2", "S")], unplaced=[p("z", "W", "C2", "S")])
    state, diag = run(m, r, s, snap())
    assert diag["duplicate_start_snapshot_evidence"] == 1
    assert len(state["cell_transitions"]) == 1 and state["cell_transitions"][0]["cell_key"] == "C1"
    assert diag["start_excluded_inventory"] == diag["start_unmatched_inventory"] == diag["start_unplaced_inventory"] == 1


def classify(start, end):
    skus = sorted(set(start + end + ["R"]))
    r = receipt(*[(f"B{i}", "W", sku, 1, "короб") for i, sku in enumerate(skus)])
    m = model("C1")
    return by_cell(run(m, r, snap(*[p(f"s{i}", "W", "C1", sku) for i, sku in enumerate(start)]), snap(*[p(f"e{i}", "W", "C1", sku) for i, sku in enumerate(end)]))[0], "C1")


@pytest.mark.parametrize("start,end,ttype", [
    ([], ["A"], "newly_occupied_single_sku"), ([], ["A", "B"], "newly_occupied_mixed_sku"),
    (["A"], [], "emptied"), (["A"], ["A"], "unchanged_single_sku"), (["A"], ["B"], "replaced_single_sku"),
    (["A", "B"], ["A", "B"], "unchanged_mixed_sku"), (["A", "B"], ["B", "C"], "changed_mixed_sku"),
])
def test_transition_classifications(start, end, ttype):
    assert classify(start, end)["transition_type"] == ttype


def test_candidates_requirements_confidence_and_statuses():
    m = model("C1", "C2", "C3", "C4", "C5")
    r = receipt(("BR", "W", "R", 10, "короб"), ("BP", "W", "P", 5, "короб"), ("BN", "W", "N", 1, "короб"), ("BM", "W", "M", 1, "короб"))
    s = snap(p("s1", "W", "C1", "A"), p("s2", "W", "C2", "P"), p("s3", "W", "C5", "M"))
    e = snap(p("e1", "W", "C1", "R"), p("e2", "W", "C2", "P"), p("e3", "W", "C3", "N"), p("e6", "W", "C4", "M"), p("e4", "W", "C5", "M"), p("e5", "W", "C5", "R"))
    state, diag = run(m, r, s, e)
    c1 = next(c for c in state["receipt_cell_candidates"] if c["physical_cell_key"] == "C1")
    c3 = next(c for c in state["receipt_cell_candidates"] if c["physical_cell_key"] == "C3")
    c5 = next(c for c in state["receipt_cell_candidates"] if c["physical_cell_key"] == "C5")
    assert c1["candidate_type"] == "reused_physical_cell" and c1["requires_virtual_slot"] and c1["confidence"] == "strong_snapshot_delta"
    assert c3["candidate_type"] == "newly_occupied_physical_cell" and not c3["requires_virtual_slot"]
    assert c5["confidence"] == "ambiguous_snapshot_delta"
    assert len(state["virtual_slot_requirements"]) == 2  # C1/R and C5/R
    statuses = {e["sku_key"]: e["evidence_status"] for e in state["receipt_batch_evidence"]}
    assert statuses["P"] == "persistent_same_sku_only"
    assert statuses["N"] == "new_location_evidence"
    assert statuses["M"] == "mixed_persistent_and_new_evidence"
    assert diag["reused_physical_cells"] == 2


def test_non_receipt_and_warehouse_exact_matching_other_warehouse():
    m = model("C1", "C2")
    r = receipt(("B", "Main WH", "S", 1, "короб"))
    s = snap()
    e = snap(p("1", "Main WH extension", "C1", "S"), p("2", "Other", "C2", "S"))
    state, diag = run(m, r, s, e)
    ev = state["receipt_batch_evidence"][0]
    assert state["receipt_cell_candidates"] == []
    assert ev["end_cell_keys"] == [] and ev["other_warehouse_end_cell_keys"] == ["C1", "C2"]
    assert diag["receipt_batches_with_other_warehouse_end_evidence"] == 1


def test_two_receipt_skus_in_one_reused_cell_make_two_requirements_and_no_forbidden_outputs():
    m = model("C1")
    r = receipt(("B1", "W", "B", 100, "короб"), ("B2", "W", "C", 50, "короб"))
    state, _ = run(m, r, snap(p("s", "W", "C1", "A")), snap(p("e1", "W", "C1", "B"), p("e2", "W", "C1", "C")))
    assert len(state["virtual_slot_requirements"]) == 2
    assert not {"virtual_cells", "placements", "working_stock", "routes"} & set(state)
    for item in state["receipt_cell_candidates"] + state["virtual_slot_requirements"]:
        assert "allocated_qty_units" not in item
    assert "same_sku_replenishment_in_same_cell_is_not_observable" in state["limitations"]


def test_integrated_c1_c4_case():
    m = model("C1", "C2", "C3", "C4")
    r = receipt(("BB", "Warehouse A", "SKU B", 100, "короб"), ("BC", "Warehouse A", "SKU C", 50, "короб"), ("BD", "Warehouse A", "SKU D", 20, "короб"))
    s = snap(p("s1", "Warehouse A", "C1", "SKU A"), p("s3", "Warehouse A", "C3", "SKU C"), p("s4", "Warehouse A", "C4", "SKU X", qty_pallets=999))
    e = snap(p("e1", "Warehouse A", "C1", "SKU B", qty_units=1), p("e2", "Warehouse A", "C2", "SKU D"), p("e3", "Warehouse A", "C3", "SKU C"))
    state, diag = run(m, r, s, e)
    assert by_cell(state, "C1")["transition_type"] == "replaced_single_sku"
    assert by_cell(state, "C1")["receipt_added_sku_keys"] == ["SKU B"] and by_cell(state, "C1")["requires_virtual_slot"]
    assert by_cell(state, "C2")["transition_type"] == "newly_occupied_single_sku" and not by_cell(state, "C2")["requires_virtual_slot"]
    assert by_cell(state, "C3")["transition_type"] == "unchanged_single_sku"
    assert by_cell(state, "C4")["transition_type"] == "emptied"
    assert len(state["receipt_cell_candidates"]) == 2
    assert len(state["virtual_slot_requirements"]) == 1
    assert diag["reused_physical_cells"] == 1
    assert {b["sku_key"]: b["qty_units"] for b in state["receipt_batches"]} == {"SKU B": 100, "SKU C": 50, "SKU D": 20}
    assert next(ev for ev in state["receipt_batch_evidence"] if ev["sku_key"] == "SKU C")["evidence_status"] == "persistent_same_sku_only"
    assert state["start_snapshot_id"].startswith("sha256:") and state["end_snapshot_id"].startswith("sha256:")


def test_module_contract_no_forbidden_imports_or_writes_and_files_unchanged_targets():
    mod = importlib.import_module("warehouse_receipt_snapshot_transitions")
    assert mod.__name__
    text = Path("warehouse_receipt_snapshot_transitions.py").read_text(encoding="utf-8").casefold()
    assert "pandas" not in text and "streamlit" not in text
    assert ".write_text" not in text and ".open(" not in text
    changed = set(__import__("subprocess").check_output(["git", "diff", "--name-only"], text=True).splitlines())
    assert "queries_1c" not in "\n".join(changed)
    assert "start.cmd" not in changed
