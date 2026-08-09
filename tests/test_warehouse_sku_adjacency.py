from __future__ import annotations

import copy

from warehouse_sku_adjacency import adjacency_conflict, build_sku_adjacency_profile


def state(*lots):
    return {"stock_lots": list(lots)}


def lot(sku, nomenclature, characteristic):
    return {"sku_key": sku, "nomenclature": nomenclature, "characteristic": characteristic}


def test_empty_baseline_profile_is_v2_valid_and_stable():
    first, diagnostics = build_sku_adjacency_profile(None)
    second, _ = build_sku_adjacency_profile({"stock_lots": []})
    assert diagnostics["valid"] and first["adjacency_profile_version"] == 2
    assert first["adjacency_profile_id"] == second["adjacency_profile_id"]


def test_profile_uses_exact_normalized_factual_evidence_and_is_permutation_stable():
    baseline = state(lot("A", "  Молоко   Ёлка ", " БЕЛЫЙ  Ёж "), lot("B", "Сливки", "белый еж"))
    original = copy.deepcopy(baseline)
    first, diagnostics = build_sku_adjacency_profile(baseline)
    second, _ = build_sku_adjacency_profile(state(*reversed(baseline["stock_lots"])))
    assert diagnostics["valid"] and baseline == original
    assert first["adjacency_profile_id"] == second["adjacency_profile_id"]
    assert first["rows"][0]["normalized_nomenclature"] == "молоко елка"
    assert first["rows"][0]["normalized_characteristic"] == "белый еж"
    assert adjacency_conflict(first["rows"][0], first["rows"][1])


def test_contract_allows_same_name_or_different_or_blank_characteristic():
    same_a = {"normalized_nomenclature": "a", "normalized_characteristic": "x"}
    same_b = {"normalized_nomenclature": "a", "normalized_characteristic": "x"}
    different = {"normalized_nomenclature": "b", "normalized_characteristic": "y"}
    blank = {"normalized_nomenclature": "b", "normalized_characteristic": ""}
    assert not adjacency_conflict(same_a, same_b)
    assert not adjacency_conflict(same_a, different)
    assert not adjacency_conflict(same_a, blank)
