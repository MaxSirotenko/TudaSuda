from __future__ import annotations

import copy
from datetime import date, timedelta

from warehouse_sku_velocity import _classification, build_sku_velocity_profile


D = date(2026, 7, 15)


def row(offset: int, name: str = "SKU", warehouse: str = "Вешки", order: str = "1"):
    return {"created_at": (D - timedelta(days=offset)).isoformat(), "warehouse": warehouse,
            "outbound_order_number": order, "nomenclature": name, "characteristic": ""}


def profile(rows):
    return build_sku_velocity_profile(rows, as_of_date=D.isoformat(),
                                      target_normalized_warehouse="вешки")[0]


def test_counts_distinct_active_days_not_rows_or_orders_and_windows():
    rows = [row(1, order=str(index)) for index in range(20)] + [row(2), row(5), row(10), row(20)]
    result = profile(rows)["rows"][0]
    assert (result["days_28"], result["days_14"], result["days_7"], result["days_4"]) == (5, 4, 3, 2)


def test_operational_future_and_foreign_warehouse_are_excluded():
    result = profile([row(1), row(0), row(-1), row(2, warehouse="Другой")])
    assert result["rows"][0]["days_28"] == 1


def test_rank_contract_boundaries_and_overlap_precedence():
    fixtures = {
        "r1": list(range(1, 8)) + list(range(8, 14)) + list(range(15, 23)),
        "r3": [1, 2, 3] + list(range(8, 12)) + list(range(15, 22)),
        "r4": list(range(1, 9)), "r5": list(range(1, 5)), "r6": [1],
    }
    rows = [row(offset, name) for name, offsets in fixtures.items() for offset in offsets]
    ranks = {item["nomenclature"]: item["velocity_rank"] for item in profile(rows)["rows"]}
    assert ranks == {"r1": 1, "r3": 3, "r4": 4, "r5": 5, "r6": 6}
    assert _classification(20, 10, 5, 0) == (2, "weaker_core")
    assert _classification(20, 10, 5, 1) == (1, "confirmed_core")


def test_zero_history_determinism_permutation_no_mutation_and_incomplete_span():
    rows = [row(29), row(1, "active")]
    original = copy.deepcopy(rows)
    first = profile(rows)
    second = profile(list(reversed(rows)))
    by_name = {item["nomenclature"]: item for item in first["rows"]}
    assert by_name["sku"]["velocity_rank"] is None and by_name["sku"]["velocity_class"] == "no_history"
    assert first == second and rows == original
    assert first["summary"]["history_span_complete"] is False
    assert "incomplete_28_day_history" in first["limitations"]
