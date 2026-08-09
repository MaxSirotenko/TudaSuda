from warehouse_deep_lane import (
    deep_lane_access_contract_id,
    deep_lane_access_diagnostics,
    derive_deep_lane_depths,
)


def slots():
    return [
        {"slot_index": 30, "x_min": 2.0, "x_max": 3.0},
        {"slot_index": 10, "x_min": 0.0, "x_max": 1.0},
        {"slot_index": 20, "x_min": 1.0, "x_max": 2.0},
    ]


def test_access_side_is_only_depth_evidence_and_permutation_is_deterministic():
    left, error = derive_deep_lane_depths(slots(), "left", 3)
    assert error is None and left == {10: 1, 20: 2, 30: 3}
    right, error = derive_deep_lane_depths(list(reversed(slots())), "right", 3)
    assert error is None and right == {30: 1, 20: 2, 10: 3}
    unknown, error = derive_deep_lane_depths(slots(), None, 3)
    assert unknown is None and error == "deep_lane_access_unconfigured"


def test_contract_counts_and_identity_are_deterministic_and_business_relevant():
    rows = [
        {"row_number": "2", "row_storage_type": "deep_lane", "deep_lane_access_side": "left"},
        {"row_number": "1", "row_storage_type": "deep_lane"},
        {"row_number": "3", "row_storage_type": "normal", "deep_lane_access_side": "right"},
    ]
    assert deep_lane_access_diagnostics(rows) == {
        "deep_lane_rows_total": 2, "deep_lane_access_configured": 1,
        "deep_lane_access_missing": 1, "deep_lane_access_invalid": 0,
    }
    assert deep_lane_access_contract_id(rows) == deep_lane_access_contract_id(list(reversed(rows)))
    changed = [dict(row) for row in rows]
    changed[0]["deep_lane_access_side"] = "right"
    assert deep_lane_access_contract_id(rows) != deep_lane_access_contract_id(changed)
