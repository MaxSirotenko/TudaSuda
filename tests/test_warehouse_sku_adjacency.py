import copy
from warehouse_business_identity import canonical_sku_key
from warehouse_sku_adjacency import build_sku_adjacency_profile

def sku(name):
    return canonical_sku_key({'nomenclature': name, 'characteristic': 'x'})

def test_empty_profile_is_valid_and_stable():
    profile, diagnostics = build_sku_adjacency_profile(None)
    assert diagnostics['valid'] and profile['summary'] == {'sku_rows': 0, 'grouped_skus': 0, 'groups_total': 0, 'ungrouped_skus': 0}
    assert profile['adjacency_profile_version'] == 1 and profile['adjacency_profile_id'].startswith('sha256:')

def test_normalization_deduplication_permutation_and_immutability():
    rows = [{'sku_key': sku('A'), 'adjacency_group': 'Молоко'}, {'sku_key': sku('A'), 'adjacency_group': ' молоко  '}, {'sku_key': sku('B'), 'adjacency_group': ''}]
    original = copy.deepcopy(rows)
    first, diagnostics = build_sku_adjacency_profile(rows)
    second, _ = build_sku_adjacency_profile(list(reversed(rows)))
    assert diagnostics['valid'] and rows == original and first['adjacency_profile_id'] == second['adjacency_profile_id']
    assert first['rows'][0]['adjacency_group'] == 'молоко'
    assert first['summary'] == {'sku_rows': 2, 'grouped_skus': 1, 'groups_total': 1, 'ungrouped_skus': 1}

def test_conflicting_explicit_groups_are_invalid_without_guessing():
    profile, diagnostics = build_sku_adjacency_profile([{'sku_key': sku('A'), 'adjacency_group': 'Молоко'}, {'sku_key': sku('A'), 'adjacency_group': 'Молоко 1л'}])
    assert not diagnostics['valid']
    assert profile['validation_errors'][0]['code'] == 'conflicting_adjacency_group_assignment'
