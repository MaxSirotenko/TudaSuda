from __future__ import annotations

from datetime import datetime
from io import BytesIO
import gzip
import json
from pathlib import Path
import tracemalloc

import pandas as pd

from warehouse_factual_data import (
    AUTHORITATIVE_CONTRACTS, CONTRACT_ALIAS_STATUS, CONTRACTS, LEGACY_CONTRACT_ALIASES, PARSER_VERSION,
    build_data_contract_diagnostics, cross_source_coverage, date_summary, detect_source_type,
    import_excel_dataset, load_dataset_rows, load_registry, positive_outbound,
    normalize_source_datetime,
)
import warehouse_factual_data as factual


def test_date_summary_is_index_only(monkeypatch):
    registry = {"datasets": [{"active": True, "source_type": "outbound", "partitions": ["2026-07-15"],
        "index": {"dates": ["2026-07-15"], "sku_keys": ["sku:1"], "daily": {"2026-07-15": {
            "rows": 3, "documents": 2, "positive_quantity": 7, "positive_sku_keys": ["sku:1"]}}}}]}
    monkeypatch.setattr(factual, "load_dataset_rows", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("partition read")))
    summary = date_summary(registry, "2026-07-15")
    assert summary["outbound"] == {"documents": 2, "lines": 3, "positive_demand": 7.0}


def test_compact_conflicts_stream_business_index_with_bounded_preview(tmp_path, monkeypatch):
    artifact = tmp_path / "dataset"
    artifact.mkdir()
    path = artifact / "business_index.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for number in range(100_000):
            key = number // 2
            fingerprint = f"fp-{key}" if key >= 10 else f"conflict-{number % 2}"
            stream.write(json.dumps({"business_key": json.dumps([f"ref-{key}", 1]),
                "payload_fingerprint": fingerprint, "day": "2026-07-15",
                "dataset_id": "dataset", "source_row": number + 2}) + "\n")
    registry = {"datasets": [{"active": True, "source_type": "outbound",
                               "dataset_id": "dataset", "artifact": str(artifact)}]}
    original = factual._read_jsonl

    def reject_materialized_index(read_path):
        if Path(read_path).name == "business_index.jsonl.gz":
            raise AssertionError("business index must be streamed")
        return original(read_path)

    monkeypatch.setattr(factual, "_read_jsonl", reject_materialized_index)
    tracemalloc.start()
    analysis = factual._readiness_source_conflicts(registry, "outbound")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert analysis["conflict_count"] == 10
    assert analysis["duplicate_count"] == 49_990
    assert len(analysis["preview"]) == 10
    assert all(len(item["occurrences"]) <= 10 for item in analysis["preview"])
    assert "groups" not in analysis
    assert peak < 100 * 1024 * 1024


def test_compact_conflicts_reads_legacy_partitions_sequentially(tmp_path, monkeypatch):
    artifact = tmp_path / "legacy"
    canonical = artifact / "canonical"
    canonical.mkdir(parents=True)
    rows = [{"document_ref": "ref", "line_number": 1, "occurred_at": f"{day}T00:00:00",
             "warehouse": "A", "sku_key": "sku", "quantity": quantity,
             "dataset_id": "legacy", "source_row": index}
            for index, (day, quantity) in enumerate((("2026-07-01", 1), ("2026-07-02", 2)), 2)]
    for row in rows:
        factual._write_jsonl(canonical / f"date={row['occurred_at'][:10]}.jsonl.gz", [row])
    dataset = {"active": True, "source_type": "outbound", "dataset_id": "legacy",
               "artifact": str(artifact), "partitions": ["2026-07-01", "2026-07-02"]}
    monkeypatch.setattr(factual, "load_dataset_rows",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full dataset load")))

    analysis = factual._readiness_source_conflicts({"datasets": [dataset]}, "outbound")

    assert analysis["conflict_count"] == 1
    assert analysis["preview"][0]["business_key"] == ["ref", 1]


def _xlsx(rows):
    stream = BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="Данные")
    return stream.getvalue()


def test_factual_source_datetime_formats_are_explicit_and_day_first():
    assert normalize_source_datetime("01.07.2026") == "2026-07-01T00:00:00"
    assert normalize_source_datetime("12.07.2026") == "2026-07-12T00:00:00"
    assert normalize_source_datetime("31.07.2026") == "2026-07-31T00:00:00"
    assert normalize_source_datetime("07.08.2026") == "2026-08-07T00:00:00"
    assert normalize_source_datetime("01.07.2026 08:12:38") == "2026-07-01T08:12:38"
    assert normalize_source_datetime("2026-07-01") == "2026-07-01T00:00:00"
    native = datetime(2026, 7, 1, 10, 30)
    assert normalize_source_datetime(native) == "2026-07-01T10:30:00"
    assert normalize_source_datetime("07/08/2026") is None


def test_russian_dates_partition_every_factual_family_and_periods(tmp_path):
    families = {
        "placement.xlsx": [_placement("01.07.2026 00:00:00"), _placement("01.08.2026 00:00:00", pallet="P2")],
        "inventory.xlsx": [{"Инвентаризация": "I", "Номер": 1, "Дата": day, "НомерСтроки": i,
            "Склад": "A", "Номенклатура": "N", "Характеристика": "C", "КоличествоФакт": 1,
            "КоличествоУчет": 1} for i, day in enumerate(("01.07.2026", "12.07.2026", "31.07.2026"), 1)],
        "receipts.xlsx": [{"Ссылка": "R", "Номер": "R", "Дата": "01.07.2026 08:12:38", "Склад": "A",
            "НомерСтроки": 1, "Номенклатура": "N", "Характеристика": "C", "КоличествоКоробок": 1}],
        "outbound.xlsx": [_outbound(day="01.07.2026")],
    }
    results = {name: import_excel_dataset(_xlsx(rows), name, root=tmp_path) for name, rows in families.items()}
    assert (results["placement.xlsx"]["period_from"], results["placement.xlsx"]["period_to"]) == ("2026-07-01", "2026-08-01")
    assert (results["inventory.xlsx"]["period_from"], results["inventory.xlsx"]["period_to"]) == ("2026-07-01", "2026-07-31")
    assert results["receipts.xlsx"]["partitions"] == ["2026-07-01"]
    assert results["outbound.xlsx"]["partitions"] == ["2026-07-01"]
    assert load_dataset_rows(results["receipts.xlsx"], "2026-07-01")[0]["occurred_at"] == "2026-07-01T08:12:38"


def _placement(date="2026-07-15", pallet="P1", name="Товар", characteristic="Красный", cell="1-1", order=10):
    return {"ДатаСреза": date, "Паллета": pallet, "Номенклатура": name, "Характеристика": characteristic,
            "КоличествоОстатокТовара": 12, "Ячейка": cell, "ПорядокСборки": order,
            "КоличествоОстатокПоложения": 1}


def test_source_detection_all_families_unknown_and_ambiguous():
    families = {
        "historical_placement": _placement(),
        "inventory": {"Инвентаризация": "I", "Номер": 1, "Дата": "2026-07-15", "НомерСтроки": 1, "Склад": "A", "Номенклатура": "N", "Характеристика": "C", "КоличествоФакт": 1, "КоличествоУчет": 2},
        "receipts": {"Ссылка": "ref", "Номер": "R", "Дата": "2026-07-15", "Склад": "A", "НомерСтроки": 1, "Номенклатура": "N", "Характеристика": "C", "КоличествоКоробок": 1},
        "outbound": {"СсылкаРО": "ref", "НомерРО": "O", "ДатаРО": "2026-07-15", "Склад": "A", "НомерСтроки": 1, "Номенклатура": "N", "Характеристика": "C", "РасчетноеОтгруженоКоробок": 1},
        "vgh": {"Номенклатура": "N", "Характеристика": "C", "КоличествоКоробовВОдномСлоеНаПаллете": 2, "КоличествоСлоевНаПаллете": 3},
    }
    for expected, row in families.items():
        assert detect_source_type(row)["source_type"] == expected
    assert detect_source_type(["foo", "bar"])["status"] == "unknown_schema"
    combined = {**families["receipts"], **families["vgh"]}
    detected = detect_source_type(combined)
    assert detected["status"] == "ambiguous_schema"
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
    assert diag["unknown_geometry_cells"] is None
    assert diag["historical_cell_resolution"] == "historical_cell_not_resolved_to_geometry"
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
    receipt_rows = [{"Ссылка": "ref", "Номер": "R", "Дата": "2026-07-15 17:00", "Склад": "A", "НомерСтроки": i,
                     "Номенклатура": "N", "Характеристика": "C", "КоличествоКоробок": qty, "КоличествоПаллет": 1}
                    for i, qty in enumerate((2, 0, -1, None), 1)]
    receipt = import_excel_dataset(_xlsx(receipt_rows), "ПО июль.xlsx", root=tmp_path)
    records = load_dataset_rows(receipt, "2026-07-15")
    assert [r["box_quantity"] for r in records] == [2, 0, -1, None]
    assert all(r["reported_pallets"] == 1 and "capacity" not in r for r in records)
    outbound_rows = [{"СсылкаРО": "ref", "НомерРО": "O", "ДатаРО": "2026-07-15", "Склад": "A", "НомерСтроки": 1,
                      "Номенклатура": "N", "Характеристика": "C", "РасчетноеОтгруженоКоробок": qty, "ПорядокСборки": 9}
                     for qty in (0, 4)]
    outbound = import_excel_dataset(_xlsx(outbound_rows), "РО июль.xlsx", root=tmp_path)
    out = load_dataset_rows(outbound, "2026-07-15")
    assert len(out) == 2 and [r["quantity"] for r in positive_outbound(out)] == [4]
    assert out[0]["source_pick_order"] == 9 and "sku_picking_order" not in out[0]
    vgh = import_excel_dataset(_xlsx([{"Номенклатура": "N", "Характеристика": "C", "Вес": 10, "Длина": 1,
        "Ширина": 2, "Высота": 3, "КоличествоКоробовВОдномСлоеНаПаллете": 6, "КоличествоСлоевНаПаллете": 4, "КоличествоВКоробке": 10}]), "ВГХ паллеты.xlsx", root=tmp_path)
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
    assert summary["placement"]["rows"] == 1
    assert sum(item["active"] for item in registry["datasets"]) == 1
    coverage = cross_source_coverage(registry)
    assert coverage and coverage[0]["placement"] is True and coverage[0]["vgh"] is False


def test_unknown_file_is_not_persisted(tmp_path):
    result = import_excel_dataset(_xlsx([{"Количество": 1}]), "РО июль.xlsx", root=tmp_path)
    assert result["source_label"] == "Неизвестный тип файла"
    assert not (tmp_path / "registry.json").exists()


def _outbound(qty=1, day="2026-07-15"):
    return {"СсылкаРО": "ref", "НомерРО": "O", "ДатаРО": day, "Склад": "A", "НомерСтроки": 1,
            "Номенклатура": "N", "Характеристика": "C", "РасчетноеОтгруженоКоробок": qty}


def test_lifecycle_parser_upgrade_multifile_and_renamed_duplicate(tmp_path):
    a = import_excel_dataset(_xlsx([_outbound(1)]), "РО июль.xlsx", root=tmp_path, parser_version="factual-july-v2")
    b = import_excel_dataset(_xlsx([_outbound(2)]), "РО июль.xlsx", root=tmp_path, parser_version="factual-july-v2")
    registry = load_registry(tmp_path)
    assert not next(x for x in registry["datasets"] if x["dataset_id"] == a["dataset_id"])["active"]
    assert b["active"] and b["supersedes"] == a["dataset_id"]

    continuation_data = _xlsx([_outbound(3, "2026-07-31")])
    continuation = import_excel_dataset(continuation_data, "РО 31-1(1).xlsx", root=tmp_path)
    assert len([x for x in load_registry(tmp_path)["datasets"] if x["active"]]) == 2
    renamed = import_excel_dataset(continuation_data, "renamed.xlsx", root=tmp_path)
    assert renamed["reused"] and renamed["dataset_id"] == continuation["dataset_id"]

    upgraded = import_excel_dataset(_xlsx([_outbound(2)]), "РО июль.xlsx", root=tmp_path)
    registry = load_registry(tmp_path)
    assert upgraded["active"]
    assert upgraded["reparsed_for_parser_upgrade"] is True
    assert upgraded["parser_version"] == PARSER_VERSION
    assert len([x for x in registry["datasets"] if x["active"] and x["logical_source_id"] == upgraded["logical_source_id"]]) == 1


def test_duplicate_hash_precedes_parse_and_indexes_avoid_full_load(tmp_path, monkeypatch):
    import warehouse_factual_data as factual
    data = _xlsx([_outbound()])
    first = import_excel_dataset(data, "РО июль.xlsx", root=tmp_path)
    monkeypatch.setattr(factual, "read_excel_source", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("parsed")))
    assert import_excel_dataset(data, "РО июль.xlsx", root=tmp_path)["dataset_id"] == first["dataset_id"]
    monkeypatch.setattr(factual, "load_dataset_rows", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("full load")))
    assert cross_source_coverage(load_registry(tmp_path))[0]["outbound"]


def test_unconfirmed_outbound_names_are_not_authoritative():
    plausible = {"НомерРасходногоОрдера": "O", "ДатаРасходногоОрдера": "2026-07-15", "Склад": "A",
                 "Номенклатура": "N", "Характеристика": "C", "РасчетноеКоличествоКоробов": 1}
    assert detect_source_type(plausible)["source_type"] == "unknown"


def test_authoritative_outbound_quantity_wins_and_source_quantity_is_raw_evidence(tmp_path):
    row = {"СсылкаРО": "ref", "НомерРО": "O-1", "ДатаРО": "2026-07-01", "ДеньРО": 1,
           "Склад": "A", "НомерСтроки": 1, "Номенклатура": "N", "Характеристика": "C",
           "Количество": 24, "РасчетноеОтгруженоКоробок": 3, "ПорядокСборки": 7}
    detected = detect_source_type(row)
    assert detected["source_type"] == "outbound"
    assert detected["mapping"]["document_number"] == "НомерРО"
    assert detected["mapping"]["occurred_at"] == "ДатаРО"
    result = import_excel_dataset(_xlsx([row]), "test_random_name.xlsx", root=tmp_path)
    assert result["source_type"] == "outbound"
    assert result["source_label"] == "Расходные ордера"
    assert result["period_from"] == result["period_to"] == "2026-07-01"
    assert load_dataset_rows(result, raw=True)[0]["raw"]["НомерРО"] == "O-1"
    canonical = load_dataset_rows(result)[0]
    assert canonical["quantity"] == 3
    assert canonical["source_line_quantity_raw"] == 24


def test_broad_outbound_aliases_are_not_authoritative():
    headers = ["РасходныйОрдер", "Номер", "Дата", "Номенклатура", "Характеристика", "Количество"]
    assert detect_source_type(headers)["source_type"] == "unknown"


def test_outbound_shape_without_document_identity_returns_targeted_diagnostic():
    detected = detect_source_type(["Номенклатура", "Характеристика", "РасчетноеОтгруженоКоробок"])
    assert detected["source_type"] == "unknown"
    assert detected["diagnostic_code"] == "outbound_document_identity_missing"
    assert detected["diagnostic_missing"] == ["СсылкаРО или НомерРО + ДатаРО"]


def test_contract_alias_classification_is_explicit_and_outbound_has_no_legacy_aliases():
    assert AUTHORITATIVE_CONTRACTS["outbound"]["quantity"] == ("РасчетноеОтгруженоКоробок",)
    assert "outbound" not in LEGACY_CONTRACT_ALIASES
    assert CONTRACTS["outbound"] == AUTHORITATIVE_CONTRACTS["outbound"]
    assert set(CONTRACTS) == {"historical_placement", "outbound", "receipts", "inventory", "vgh"}
    for source, fields in CONTRACTS.items():
        assert {alias for aliases in fields.values() for alias in aliases} == set(CONTRACT_ALIAS_STATUS[source])
        assert set(CONTRACT_ALIAS_STATUS[source].values()) <= {"authoritative", "legacy_compatibility"}


def test_filename_variant_is_diagnosed_as_overlapping_active_source(tmp_path):
    first = import_excel_dataset(_xlsx([_outbound(1)]), "РО июль.xlsx", root=tmp_path)
    second = import_excel_dataset(_xlsx([_outbound(2)]), "РО июль(1).xlsx", root=tmp_path)
    diagnostics = build_data_contract_diagnostics(load_registry(tmp_path), root=tmp_path)
    rows = {row["dataset_id"]: row for row in diagnostics["datasets"]}
    assert first["logical_source_id"] != second["logical_source_id"]
    assert rows[first["dataset_id"]]["overlapping_active_sources"][0]["source_filename"] == "РО июль(1).xlsx"
    assert rows[first["dataset_id"]]["conflicting_business_keys"] == 1
    occurrences = rows[first["dataset_id"]]["conflict_preview"][0]["occurrences"]
    assert {(item["source_file_name"], item["source_row"]) for item in occurrences} == {
        ("РО июль.xlsx", 2), ("РО июль(1).xlsx", 2)}


def test_vgh_business_key_keeps_characteristics_distinct(tmp_path):
    rows = [{"Номенклатура": "N", "Характеристика": value,
             "КоличествоКоробовВОдномСлоеНаПаллете": 2, "КоличествоСлоевНаПаллете": 3}
            for value in ("Красный", "Синий")]
    imported = import_excel_dataset(_xlsx(rows), "ВГХ.xlsx", root=tmp_path)
    assert len({row["sku_key"] for row in load_dataset_rows(imported)}) == 2
