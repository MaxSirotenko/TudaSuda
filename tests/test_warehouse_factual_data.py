from __future__ import annotations

from io import BytesIO

import pandas as pd

from warehouse_factual_data import (
    PARSER_VERSION, cross_source_coverage, date_summary, detect_source_type,
    import_excel_dataset, load_dataset_rows, load_registry, positive_outbound,
)


def _xlsx(rows):
    stream = BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="Данные")
    return stream.getvalue()


def _placement(date="2026-07-15", pallet="P1", name="Товар", characteristic="Красный", cell="1-1", order=10):
    return {"ДатаСреза": date, "Паллета": pallet, "Номенклатура": name, "Характеристика": characteristic,
            "КоличествоОстатокТовара": 12, "Ячейка": cell, "ПорядокСборки": order,
            "КоличествоОстатокПоложения": 1}


def test_source_detection_all_families_unknown_and_ambiguous():
    families = {
        "historical_placement": _placement(),
        "inventory": {"Инвентаризация": "I", "Номер": 1, "Дата": "2026-07-15", "НомерСтроки": 1, "Склад": "A", "Номенклатура": "N", "Характеристика": "C", "КоличествоФакт": 1, "КоличествоУчет": 2},
        "receipts": {"НомерПриходногоОрдера": "R", "ДатаПриходногоОрдера": "2026-07-15", "Склад": "A", "НомерСтроки": 1, "Номенклатура": "N", "Характеристика": "C", "КоличествоКоробок": 1},
        "outbound": {"НомерРасходногоОрдера": "O", "ДатаРасходногоОрдера": "2026-07-15", "Склад": "A", "Номенклатура": "N", "Характеристика": "C", "РасчетноеКоличествоКоробов": 1},
        "vgh": {"Номенклатура": "N", "Характеристика": "C", "КоличествоКоробовВОдномСлоеНаПаллете": 2, "КоличествоСлоевНаПаллете": 3},
    }
    for expected, row in families.items():
        assert detect_source_type(row)["source_type"] == expected
    assert detect_source_type(["foo", "bar"])["status"] == "unknown"
    combined = {**families["receipts"], **families["vgh"]}
    detected = detect_source_type(combined)
    assert detected["status"] == "ambiguous"
    assert detected["source_type"] == "unknown"


def test_placement_preserves_raw_ambiguity_and_never_creates_capacity(tmp_path):
    rows = [_placement(pallet="P1", name=" A\u00a0 B ", cell="1-1", order=10),
            _placement(pallet="P1", name="Другой", cell="1-1", order=10),
            _placement(pallet="P2", name=" A B ", cell="2-1", order=20),
            _placement(date="2026-07-16", pallet="P1", cell="2-1", order=30)]
    result = import_excel_dataset(_xlsx(rows), "размещение июль.xlsx", root=tmp_path, geometry_cells={"1-1"})
    canonical = load_dataset_rows(result)
    raw = load_dataset_rows(result, raw=True)
    assert len(canonical) == len(raw) == 4
    assert raw[0]["raw"]["КоличествоОстатокПоложения"] == 1
    assert canonical[0]["source_pallet_ref"] == "P1"
    assert "capacity" not in canonical[0] and "pallet_unit" not in canonical[0]
    assert canonical[0]["sku_key"] == canonical[2]["sku_key"]
    diag = result["diagnostics"]
    assert diag["multiple_sku_per_pallet"] == 1
    assert diag["multiple_sku_per_cell"] == 1
    assert diag["sku_in_multiple_cells"] == 1
    assert diag["pallet_in_multiple_cells"] == 1
    assert diag["unknown_geometry_cells"] == 1
    assert diag["cell_picking_order_conflicts"] == 1
    assert all("sku" not in evidence for evidence in diag["cell_picking_order_evidence"])


def test_inventory_is_chronological_evidence_not_additive_stock(tmp_path):
    rows = [{"Инвентаризация": "I1", "Номер": "1", "Дата": "2026-07-15 12:00", "НомерСтроки": 1,
             "Склад": "Комплектация Овощи-фрукты Вешки", "Номенклатура": "N", "Характеристика": "C", "КоличествоФакт": 5, "КоличествоУчет": 7},
            {"Инвентаризация": "I2", "Номер": "2", "Дата": "2026-07-15 15:00", "НомерСтроки": 1,
             "Склад": "Овощи Фрукты Вешки", "Номенклатура": "N", "Характеристика": "C", "КоличествоФакт": 1, "КоличествоУчет": 2}]
    result = import_excel_dataset(_xlsx(rows), "инвенты июль 15.xlsx", root=tmp_path)
    records = load_dataset_rows(result, "2026-07-15")
    assert [r["warehouse"] for r in records] == [rows[0]["Склад"], rows[1]["Склад"]]
    assert records[0]["actual_quantity"] == 5 and records[0]["accounting_quantity"] == 7
    assert "stock" not in result and "total_quantity" not in result


def test_receipt_outbound_and_vgh_source_semantics(tmp_path):
    receipt_rows = [{"НомерПриходногоОрдера": "R", "ДатаПриходногоОрдера": "2026-07-15 17:00", "Склад": "A", "НомерСтроки": i,
                     "Номенклатура": "N", "Характеристика": "C", "КоличествоКоробок": qty, "КоличествоПаллет": 1}
                    for i, qty in enumerate((2, 0, -1, None), 1)]
    receipt = import_excel_dataset(_xlsx(receipt_rows), "ПО июль.xlsx", root=tmp_path)
    records = load_dataset_rows(receipt, "2026-07-15")
    assert [r["box_quantity"] for r in records] == [2, 0, -1, None]
    assert all(r["reported_pallets"] == 1 and "capacity" not in r for r in records)
    outbound_rows = [{"НомерРасходногоОрдера": "O", "ДатаРасходногоОрдера": "2026-07-15", "Склад": "A",
                      "Номенклатура": "N", "Характеристика": "C", "РасчетноеКоличествоКоробов": qty, "ПорядокСборки": 9}
                     for qty in (0, 4)]
    outbound = import_excel_dataset(_xlsx(outbound_rows), "РО июль.xlsx", root=tmp_path)
    out = load_dataset_rows(outbound, "2026-07-15")
    assert len(out) == 2 and [r["quantity"] for r in positive_outbound(out)] == [4]
    assert out[0]["source_pick_order"] == 9 and "sku_picking_order" not in out[0]
    vgh = import_excel_dataset(_xlsx([{"Номенклатура": "N", "Характеристика": "C", "Вес": 10, "Длина": 1,
        "Ширина": 2, "Высота": 3, "КоличествоКоробовВОдномСлоеНаПаллете": 6, "КоличествоСлоевНаПаллете": 4}]), "ВГХ паллеты.xlsx", root=tmp_path)
    vgh_row = load_dataset_rows(vgh)[0]
    assert vgh_row["source_boxes_per_pallet"] == 24
    assert vgh_row["palletization_authority"] == "source_layer_norm"


def test_registry_idempotency_changed_hash_provenance_partition_and_coverage(tmp_path):
    data = _xlsx([_placement()])
    first = import_excel_dataset(data, "same.xlsx", root=tmp_path)
    second = import_excel_dataset(data, "same.xlsx", root=tmp_path)
    changed = import_excel_dataset(_xlsx([_placement(cell="9-9")]), "same.xlsx", root=tmp_path)
    assert second["reused"] is True
    assert first["dataset_id"] != changed["dataset_id"]
    registry = load_registry(tmp_path)
    assert len(registry["datasets"]) == 2
    row = load_dataset_rows(first, "2026-07-15")[0]
    for key in ("dataset_id", "source_file_name", "content_hash", "source_type", "parser_version", "sheet", "source_row", "source_index", "imported_at"):
        assert key in row
    assert row["parser_version"] == PARSER_VERSION
    assert load_dataset_rows(first, "2026-07-16") == []
    summary = date_summary(registry, "2026-07-15")
    assert summary["placement"]["rows"] == 2
    coverage = cross_source_coverage(registry)
    assert coverage and coverage[0]["placement"] is True and coverage[0]["vgh"] is False


def test_unknown_file_is_not_persisted(tmp_path):
    result = import_excel_dataset(_xlsx([{"Количество": 1}]), "РО июль.xlsx", root=tmp_path)
    assert result["source_label"] == "Неизвестный тип файла"
    assert not (tmp_path / "registry.json").exists()
