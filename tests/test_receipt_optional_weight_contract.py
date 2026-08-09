from __future__ import annotations

from pathlib import Path

import pandas as pd

from warehouse_receipts import (
    OPTIONAL_COLUMN_LABEL,
    calculate_receipt_zones,
    detect_receipt_columns,
    detect_zone_classification_columns,
    format_receipt_column_option,
    normalize_receipt_table,
    optional_receipt_column_index,
    optional_receipt_column_options,
)
from warehouse_workspace_ui import build_weight_zone_readiness


def _table() -> pd.DataFrame:
    return pd.DataFrame([
        {"Код": "SKU-1", "Наименование": "Товар", "Характеристика": "Красный", "Паллеты": 3},
    ])


def test_receipt_without_weight_imports_and_preserves_business_data() -> None:
    table = _table()
    mapping = detect_receipt_columns(table)
    rows, diagnostics, messages = normalize_receipt_table(table, mapping)

    assert len(rows) == 1
    assert rows.iloc[0]["sku_code"] == "SKU-1"
    assert rows.iloc[0]["sku_name"] == "Товар"
    assert rows.iloc[0]["characteristic_name"] == "Красный"
    assert rows.iloc[0]["qty_pallets"] == 3
    assert rows.iloc[0]["source_weight"] is None
    assert rows.iloc[0]["weight_parse_status"] == "not_supplied"
    assert diagnostics["Всего паллет"] == 3
    assert not [message for message in messages if message["level"] == "error"]
    assert any("Приход загружен" in message["message"] for message in messages)


def test_optional_detection_and_selector_never_fall_back_to_first_excel_column() -> None:
    table = _table()
    detected = detect_zone_classification_columns(table)
    assert detected == {"weight_column": None, "fragile_column": None, "source_zone_column": None}

    options = optional_receipt_column_options(list(table.columns))
    assert options[0] is None
    assert optional_receipt_column_index(options, detected["weight_column"]) == 0
    assert optional_receipt_column_index(options, detected["fragile_column"]) == 0
    assert optional_receipt_column_index(options, detected["source_zone_column"]) == 0
    assert format_receipt_column_option(options[0]) == OPTIONAL_COLUMN_LABEL
    assert options[optional_receipt_column_index(options, "missing")] is None


def test_explicit_zone_resolves_without_weight_and_missing_evidence_is_unassigned() -> None:
    rows, _, _ = normalize_receipt_table(_table(), detect_receipt_columns(_table()))
    receipt = rows.iloc[0].to_dict()
    explicit, _ = calculate_receipt_zones([{**receipt, "source_zone": "light"}], {})
    unresolved, diagnostics = calculate_receipt_zones([receipt], {})

    assert explicit[0]["calculated_zone"] == "light"
    assert explicit[0]["zone_calculation_reason"] == "Явная авторитетная зона источника"
    assert unresolved[0]["calculated_zone"] == "unassigned"
    assert unresolved[0]["zone_calculation_status"] == "unresolved"
    assert diagnostics["SKU без подтверждённой зоны"] == 1
    assert unresolved[0]["qty_pallets"] == 3


def test_supported_weight_resolves_without_source_zone() -> None:
    receipt = {"sku_name": "A", "characteristic_name": "x", "qty_pallets": 2,
               "source_weight": 0.5, "source_weight_raw": "0,5", "weight_parse_status": "ok"}
    rows, _ = calculate_receipt_zones([receipt], {})
    assert rows[0]["calculated_zone"] == "bulky"


def test_invalid_and_mixed_weights_retain_every_receipt_without_fabricating_zero() -> None:
    table = pd.DataFrame([
        {"Код": "A", "Наименование": "A", "Паллеты": 1, "Вес": "bad"},
        {"Код": "B", "Наименование": "B", "Паллеты": 2, "Вес": ""},
        {"Код": "C", "Наименование": "C", "Паллеты": 4, "Вес": "1,5"},
    ])
    rows, diagnostics, messages = normalize_receipt_table(table, detect_receipt_columns(table))
    assert len(rows) == 3
    assert rows["qty_pallets"].tolist() == [1, 2, 4]
    assert rows["source_weight"].tolist()[:2] == [None, None]
    assert rows.iloc[0]["weight_parse_status"] == "error"
    assert diagnostics["Всего паллет"] == 7
    assert not [message for message in messages if message["level"] == "error"]
    assert any('Вес "bad" не удалось распознать' in message["message"] for message in messages)

    classified, _ = calculate_receipt_zones(rows.to_dict("records"), {})
    assert [row["calculated_zone"] for row in classified] == ["unassigned", "unassigned", "fragile"]


def test_weight_zone_readiness_reports_partial_coverage_only() -> None:
    coverage = build_weight_zone_readiness([
        {"sku_key": "A", "calculated_zone": "light"},
        {"sku_key": "B", "calculated_zone": "unassigned"},
        {"sku_key": "C", "calculated_zone": ""},
    ])
    assert coverage == {"status": "partial", "total_sku": 3, "confirmed_sku": 1,
                        "unresolved_sku": 2, "coverage_percent": 33.3}


def test_ui_source_has_explicit_optional_copy_and_v1_isolation() -> None:
    source = Path("virtual_warehouse_app.py").read_text(encoding="utf-8")
    assert "Вес товара — необязательно" in source
    assert "Признак хрупкости — необязательно" in source
    assert "Исходная зона — необязательно" in source
    assert "format_func=format_receipt_column_option" in source
    assert 'selectbox("Колонка с весом товара"' not in source

    benchmark = Path("warehouse_day_benchmark.py").read_text(encoding="utf-8")
    assert "source_weight" not in benchmark
    assert "weight_parse_status" not in benchmark
