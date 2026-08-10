from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO

import pandas as pd
import pytest

from warehouse_factual_data import (
    activate_dataset_version, build_fact_route_readiness, build_monthly_data_readiness, import_excel_dataset,
    load_effective_rows, load_registry, resolve_historical_cell,
    save_historical_cell_mapping, normalize_operational_day,
)


def _xlsx(rows):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="Данные")
    return output.getvalue()


def _outbound(qty=1, ref="ref", line=1, day="2026-07-15"):
    return {"СсылкаРО": ref, "НомерРО": "O", "ДатаРО": day, "Склад": "A", "НомерСтроки": line,
            "Номенклатура": "N", "Характеристика": "C", "РасчетноеОтгруженоКоробок": qty}


def test_effective_duplicate_collapses_with_complete_provenance(tmp_path):
    a = import_excel_dataset(_xlsx([_outbound()]), "РО июль.xlsx", root=tmp_path)
    duplicate = _outbound(); duplicate["НомерРО"] = "alternate provenance-only number"
    b = import_excel_dataset(_xlsx([duplicate]), "РО продолжение.xlsx", root=tmp_path)
    view = load_effective_rows("outbound", "2026-07-15", registry=load_registry(tmp_path), root=tmp_path)
    assert len(view["rows"]) == 1
    assert view["rows"][0]["duplicate_source_count"] == 2
    assert set(view["rows"][0]["duplicate_source_ids"]) == {a["dataset_id"], b["dataset_id"]}
    assert view["duplicates"][0]["code"] == "duplicate_factual_evidence_collapsed"


def test_effective_conflict_refuses_authoritative_selection(tmp_path):
    import_excel_dataset(_xlsx([_outbound(1)]), "РО июль.xlsx", root=tmp_path)
    import_excel_dataset(_xlsx([_outbound(2)]), "РО продолжение.xlsx", root=tmp_path)
    with pytest.raises(ValueError, match="conflicting_factual_business_key"):
        load_effective_rows("outbound", "2026-07-15", registry=load_registry(tmp_path), root=tmp_path)
    view = load_effective_rows("outbound", "2026-07-15", registry=load_registry(tmp_path), root=tmp_path, strict=False)
    assert not view["authoritative"] and len(view["conflicts"]) == 1


def test_superseded_reupload_requires_explicit_activation_without_parse(tmp_path, monkeypatch):
    import warehouse_factual_data as factual
    old_bytes = _xlsx([_outbound(1)])
    old = import_excel_dataset(old_bytes, "РО июль.xlsx", root=tmp_path)
    current = import_excel_dataset(_xlsx([_outbound(2)]), "РО июль.xlsx", root=tmp_path)
    monkeypatch.setattr(factual, "_canonical_record", lambda *_: (_ for _ in ()).throw(AssertionError("parsed")))
    reused = import_excel_dataset(old_bytes, "РО июль.xlsx", root=tmp_path)
    assert reused["reuse_state"] == "existing_superseded_version"
    assert next(x for x in load_registry(tmp_path)["datasets"] if x["dataset_id"] == current["dataset_id"])["active"]
    activate_dataset_version(old["dataset_id"], root=tmp_path)
    registry = load_registry(tmp_path)
    assert next(x for x in registry["datasets"] if x["dataset_id"] == old["dataset_id"])["active"]
    assert not next(x for x in registry["datasets"] if x["dataset_id"] == current["dataset_id"])["active"]


def test_streaming_failure_keeps_registry_and_active_version_atomic(tmp_path):
    first = import_excel_dataset(_xlsx([_outbound()]), "РО июль.xlsx", root=tmp_path)
    before = load_registry(tmp_path)
    with pytest.raises(RuntimeError, match="injected_streaming"):
        import_excel_dataset(_xlsx([_outbound(2), _outbound(3, line=2)]), "РО июль.xlsx",
                             root=tmp_path, _fail_after_rows=1)
    assert load_registry(tmp_path) == before
    assert next(x for x in before["datasets"] if x["dataset_id"] == first["dataset_id"])["active"]
    assert not list(tmp_path.glob(".staging-*"))


def test_cell_resolution_exact_ambiguous_user_confirmed_and_stale(tmp_path):
    model = {"model_id": "A", "cells": [
        {"cell_key": "1|1|1", "source_cell": " A-01 "},
        {"cell_key": "2|1|1", "source_cell": "DUP"},
        {"cell_key": "3|1|1", "source_cell": "DUP"},
    ]}
    assert resolve_historical_cell("A-01", model, root=tmp_path)["geometry_cell_key"] == "1|1|1"
    assert resolve_historical_cell("DUP", model, root=tmp_path)["resolution_status"] == "ambiguous"
    assert resolve_historical_cell("1-1", model, root=tmp_path)["resolution_status"] == "unresolved"
    saved = save_historical_cell_mapping("1-1", "2|1|1", model, root=tmp_path)
    assert saved["mapping_method"] == "user_confirmed"
    assert resolve_historical_cell("1-1", model, root=tmp_path)["geometry_cell_key"] == "2|1|1"
    changed = {"model_id": "A", "cells": [{"cell_key": "1|1|1"}]}
    assert resolve_historical_cell("1-1", changed, root=tmp_path)["resolution_status"] == "unresolved"


def test_demand_relevant_resolution_can_be_ready_below_overall_coverage():
    rows = [{"sku_key": "wanted", "source_cell": "A", "resolved_geometry_cell_key": "A-key"},
            {"sku_key": "unused", "source_cell": "B", "resolved_geometry_cell_key": None,
             "cell_resolution_status": "unresolved"}]
    ready = build_fact_route_readiness(rows, {"wanted"}, {"A-key"})
    assert ready["overall_cell_coverage"] == {"resolved": 1, "total": 2}
    assert ready["demand_relevant_cell_coverage"] == {"resolved": 1, "total": 1}
    assert ready["fact_route_ready"]
    assert not build_fact_route_readiness(rows, {"unused"}, {"A-key"})["fact_route_ready"]


def test_complete_july_monthly_readiness_contract(tmp_path):
    placements = [{"ДатаСреза": (date(2026, 7, 1) + timedelta(days=i)).isoformat(), "Паллета": f"P{i}",
        "Номенклатура": "N", "Характеристика": "C", "КоличествоОстатокТовара": 1,
        "Ячейка": "A-01", "ПорядокСборки": 1, "КоличествоОстатокПоложения": 1} for i in range(32)]
    import_excel_dataset(_xlsx(placements), "размещение июль.xlsx", root=tmp_path)
    import_excel_dataset(_xlsx([_outbound()]), "РО июль.xlsx", root=tmp_path)
    import_excel_dataset(_xlsx([{"Ссылка": "r", "Номер": "R", "Дата": "2026-07-15", "Склад": "A",
        "НомерСтроки": 1, "Номенклатура": "N", "Характеристика": "C", "КоличествоКоробок": 1}]),
        "ПО июль.xlsx", root=tmp_path)
    import_excel_dataset(_xlsx([{"Номенклатура": "N", "Характеристика": "C",
        "КоличествоКоробовВОдномСлоеНаПаллете": 1, "КоличествоСлоевНаПаллете": 1}]),
        "ВГХ.xlsx", root=tmp_path)
    model = {"model_id": "M", "cells": [{"cell_key": "1|1|1", "source_cell": "A-01"}]}
    result = build_monthly_data_readiness(load_registry(tmp_path), model, "2026-07-01", "2026-07-31", root=tmp_path)
    assert result["monthly_replay_ready"] is True
    assert result["coverage"]["placement_checkpoints"] == 32


def test_mixed_excel_placement_dates_preserve_all_july_snapshots(tmp_path):
    days = [date(2026, 7, 1) + timedelta(days=i) for i in range(32)]
    representations = []
    for index, day in enumerate(days):
        if index < 6:
            representations.append((day - date(1899, 12, 30)).days)
        elif index < 12:
            representations.append(f"{day.isoformat()} 00:00:00")
        elif index == 31:
            representations.append("2026-08-01T00:00:00+03:00")
        else:
            representations.append(datetime.combine(day, datetime.min.time()))
    placements = [{"ДатаСреза": snapshot, "Паллета": f"P{i}", "Номенклатура": "N",
        "Характеристика": "C", "КоличествоОстатокТовара": 1, "Ячейка": "A-01",
        "ПорядокСборки": 1, "КоличествоОстатокПоложения": 1}
        for i, snapshot in enumerate(representations)]
    imported = import_excel_dataset(_xlsx(placements), "размещение июль.xlsx", root=tmp_path)
    import_excel_dataset(_xlsx([_outbound()]), "РО июль.xlsx", root=tmp_path)
    import_excel_dataset(_xlsx([{"Ссылка": "r", "Номер": "R", "Дата": "2026-07-15", "Склад": "A",
        "НомерСтроки": 1, "Номенклатура": "N", "Характеристика": "C", "КоличествоКоробок": 1}]),
        "ПО июль.xlsx", root=tmp_path)
    import_excel_dataset(_xlsx([{"Номенклатура": "N", "Характеристика": "C",
        "КоличествоКоробовВОдномСлоеНаПаллете": 1, "КоличествоСлоевНаПаллете": 1}]),
        "ВГХ.xlsx", root=tmp_path)

    expected = [day.isoformat() for day in days]
    assert imported["partitions"] == expected
    assert [normalize_operational_day(value) for value in representations] == expected
    result = build_monthly_data_readiness(load_registry(tmp_path),
        {"model_id": "M", "cells": [{"cell_key": "1|1|1", "source_cell": "A-01"}]},
        "2026-07-01", "2026-07-31", root=tmp_path)
    placement = next(check for check in result["diagnostics"]["checks"]
                     if check["name"] == "placement_snapshot")
    assert result["monthly_replay_ready"] is True
    assert result["coverage"]["placement_checkpoints"] == 32
    assert placement["available_days"] == placement["imported_days"] == 32
    assert placement["missing_dates"] == placement["extra_dates"] == []


def test_blocked_monthly_readiness_exposes_structured_reasons(tmp_path):
    result = build_monthly_data_readiness({}, {"model_id": "M", "cells": []},
                                          "2026-07-01", "2026-07-31", root=tmp_path)

    assert result["monthly_replay_ready"] is False
    assert result["diagnostics"]["ready"] is False
    checks = {check["name"]: check for check in result["diagnostics"]["checks"]}
    assert checks["placement_snapshot"]["status"] == "fail"
    assert checks["placement_snapshot"]["expected_days"] == 32
    assert len(checks["placement_snapshot"]["missing_dates"]) == 32
    assert checks["outbound"]["available"] is False
    assert checks["receipts"]["available"] is False
    assert checks["vgh_coverage"]["missing_sku_count"] == 0
    assert checks["inventory"]["available"] is False


def test_monthly_readiness_ui_formatter_is_human_readable():
    from warehouse_workspace_ui import format_monthly_readiness_check

    rendered = format_monthly_readiness_check({
        "name": "vgh_coverage", "status": "fail", "title": "ВГХ", "details": "658 / 924 SKU",
        "missing_sku_count": 266, "percentage": 71.2,
    })
    assert rendered.startswith("❌ **ВГХ**")
    assert "658 / 924 SKU" in rendered
    assert "Не хватает: 266 SKU" in rendered


def test_failed_placement_readiness_formatter_exposes_date_diagnostics():
    from warehouse_workspace_ui import format_monthly_readiness_check

    rendered = format_monthly_readiness_check({
        "name": "placement_snapshot", "status": "fail", "title": "Срезы размещения",
        "details": "31 / 32 дней", "expected_days": 32, "imported_days": 32,
        "missing_dates": ["2026-07-01"], "extra_dates": ["2026-06-30"],
    })
    assert "Ожидаемые даты: 32" in rendered
    assert "Импортированные даты размещения: 32" in rendered
    assert "Отсутствующие даты: 2026-07-01" in rendered
    assert "Лишние даты: 2026-06-30" in rendered
