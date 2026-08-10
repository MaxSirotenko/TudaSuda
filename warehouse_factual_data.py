"""Versioned factual source registry for the authoritative Data workspace.

This module preserves source rows and their provenance.  It intentionally does
not convert source pallet references, inventory quantities, or historical pick
orders into simulator authority.  The existing one-day V1 adapters remain the
only benchmark input boundary.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import tempfile
import shutil
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from warehouse_business_identity import build_canonical_sku_identity, find_canonical_identity_collisions

PARSER_VERSION = "factual-july-v2"
DATA_ROOT = Path("data/last_import/factual")
REGISTRY_PATH = DATA_ROOT / "registry.json"
UNKNOWN_SOURCE = "unknown"
SOURCE_LABELS = {
    "historical_placement": "Историческое размещение",
    "inventory": "Инвентаризации",
    "receipts": "Приходные ордера",
    "outbound": "Расходные ордера",
    "vgh": "ВГХ / паллетизация",
    UNKNOWN_SOURCE: "Неизвестный тип файла",
}

# Contracts contain only fields confirmed in the task or existing importers.
CONTRACTS: dict[str, dict[str, tuple[str, ...]]] = {
    "historical_placement": {
        "snapshot_at": ("ДатаСреза",), "source_pallet_ref": ("Паллета",),
        "nomenclature": ("Номенклатура",), "characteristic": ("Характеристика",),
        "source_stock_quantity": ("КоличествоОстатокТовара",), "cell": ("Ячейка",),
        "cell_picking_order": ("ПорядокСборки",),
        "source_position_balance": ("КоличествоОстатокПоложения",),
    },
    "inventory": {
        "inventory_ref": ("Ссылка", "Инвентаризация", "СсылкаИнвентаризации"),
        "inventory_number": ("НомерИнвентаризации", "Номер"),
        "occurred_at": ("ДатаИнвентаризации", "Дата"), "line_number": ("НомерСтроки",),
        "warehouse": ("Склад",), "nomenclature": ("Номенклатура",),
        "characteristic": ("Характеристика",), "actual_quantity": ("КоличествоФакт", "ФактическоеКоличество"),
        "accounting_quantity": ("КоличествоУчет", "УчетноеКоличество"),
    },
    "receipts": {
        "document_ref": ("Ссылка", "СсылкаПриходногоОрдера"), "document_number": ("Номер", "НомерПриходногоОрдера"),
        "occurred_at": ("Дата", "ДатаПриходногоОрдера"), "warehouse": ("Склад",),
        "line_number": ("НомерСтроки",), "nomenclature": ("Номенклатура",),
        "characteristic": ("Характеристика",), "box_quantity": ("КоличествоКоробок",),
        "reported_pallets": ("КоличествоПаллет",),
        "terminal_completed": ("ПриемкаТерминаломЗакончена",),
        "expected_receipt": ("ОжидаемыйПриход",),
    },
    "outbound": {
        "document_ref": ("РасходныйОрдер", "СсылкаРО"),
        "document_number": ("Номер", "НомерРО"),
        "occurred_at": ("Дата", "ДатаРО"), "warehouse": ("Склад",),
        "line_number": ("НомерСтроки",), "nomenclature": ("Номенклатура",),
        "characteristic": ("Характеристика",),
        "quantity": ("Количество", "РасчетноеОтгруженоКоробок"),
        "source_pick_order": ("ПорядокСборки",),
    },
    "vgh": {
        "nomenclature": ("Номенклатура",), "characteristic": ("Характеристика",),
        "weight": ("Вес",), "length": ("Длина",), "width": ("Ширина",), "height": ("Высота",),
        "boxes_per_layer": ("КоличествоКоробовВОдномСлоеНаПаллете",),
        "layers_per_pallet": ("КоличествоСлоевНаПаллете",),
        "quantity_per_box": ("КоличествоВКоробке",),
    },
}
# Each accepted spelling is traceable to the task's confirmed exports or to a
# checked-in 1C query.  Keeping the evidence beside the executable contract
# prevents a synthetic fixture from silently becoming source authority.
CONTRACT_EVIDENCE = {
    "historical_placement": "CONFIRMED_PROJECT_SOURCE:размещение июль.xlsx",
    "inventory": "CONFIRMED_PROJECT_SOURCE;LEGACY_COMPATIBILITY:queries_1c/inventory_results.query",
    "receipts": "CONFIRMED_PROJECT_SOURCE:ПО июль.xlsx;LEGACY_COMPATIBILITY:queries_1c/day_receipts.query",
    "outbound": "EXISTING_WORKING_QUERY:queries_1c/mass_outbound_orders.query",
    "vgh": "CONFIRMED_PROJECT_SOURCE",
}
REQUIRED = {
    "historical_placement": {"snapshot_at", "source_pallet_ref", "nomenclature", "characteristic", "source_stock_quantity", "cell", "cell_picking_order", "source_position_balance"},
    "inventory": {"inventory_ref", "inventory_number", "occurred_at", "line_number", "warehouse", "nomenclature", "characteristic", "actual_quantity", "accounting_quantity"},
    "receipts": {"document_ref", "document_number", "occurred_at", "warehouse", "line_number", "nomenclature", "characteristic", "box_quantity"},
    # Outbound document identity is an OR rule and is checked explicitly by
    # detect_source_type: document_ref, or document_number + occurred_at.
    "outbound": {"nomenclature", "characteristic", "quantity"},
    "vgh": {"nomenclature", "characteristic", "boxes_per_layer", "layers_per_pallet"},
}


def _norm(value: Any) -> str:
    return "" if value is None else "".join(str(value).split()).casefold().replace("ё", "е")


def _json_value(value: Any) -> Any:
    if value is None or (not isinstance(value, (list, dict, str)) and pd.isna(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _number(value: Any) -> float | int | None:
    value = _json_value(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _timestamp(value: Any) -> str | None:
    if _json_value(value) in (None, ""):
        return None
    try:
        return pd.Timestamp(value).isoformat()
    except (TypeError, ValueError):
        return None


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dataset_identity(data_hash: str, source_type: str, sheet: str, parser_version: str = PARSER_VERSION) -> str:
    payload = "\x1f".join((data_hash, source_type, sheet, parser_version))
    return "dataset:" + hashlib.sha256(payload.encode()).hexdigest()


def detect_source_type(columns: Iterable[Any]) -> dict[str, Any]:
    """Detect by exact observed columns; filename is deliberately not authority."""
    normalized = {_norm(column): str(column) for column in columns}
    matches: dict[str, dict[str, str]] = {}
    # Priority is documented for review, but never resolves a collision: all
    # matches are collected and an ambiguous schema is returned explicitly.
    detection_order = ("historical_placement", "inventory", "vgh", "outbound", "receipts")
    for source_type in detection_order:
        fields = CONTRACTS[source_type]
        mapping = {}
        for field, aliases in fields.items():
            found = next((normalized[_norm(alias)] for alias in aliases if _norm(alias) in normalized), None)
            if found:
                mapping[field] = found
        required = REQUIRED[source_type].issubset(mapping)
        if source_type == "outbound":
            required = required and ("document_ref" in mapping or {"document_number", "occurred_at"}.issubset(mapping))
        if required:
            matches[source_type] = mapping
    status = "detected" if len(matches) == 1 else "ambiguous_schema" if len(matches) > 1 else "unknown_schema"
    source_type = next(iter(matches)) if len(matches) == 1 else UNKNOWN_SOURCE
    missing: list[str] = []
    diagnostic_code: str | None = None
    diagnostic_found: list[str] = []
    diagnostic_missing: list[str] = []
    outbound_common = {"nomenclature", "characteristic", "quantity"}
    outbound_mapping = {field: next((normalized[_norm(alias)] for alias in aliases if _norm(alias) in normalized), None)
                        for field, aliases in CONTRACTS["outbound"].items()}
    outbound_mapping = {field: value for field, value in outbound_mapping.items() if value}
    if source_type == UNKNOWN_SOURCE and not matches and outbound_common.issubset(outbound_mapping):
        diagnostic_code = "outbound_document_identity_missing"
        diagnostic_found = [outbound_mapping[field] for field in ("nomenclature", "characteristic", "quantity", "occurred_at")
                            if field in outbound_mapping]
        diagnostic_missing = ["РасходныйОрдер или Номер + Дата"]
    if source_type == UNKNOWN_SOURCE and not matches and diagnostic_code is None:
        # Family signatures are exact, confirmed source fields, not fuzzy
        # aliases.  They allow a useful mapping_required diagnostic without
        # authorising canonical rows.
        signatures = {"receipts": "box_quantity", "outbound": "quantity", "historical_placement": "source_pallet_ref",
                      "inventory": "actual_quantity", "vgh": "boxes_per_layer"}
        candidates = []
        partial = {}
        for family, signature in signatures.items():
            mapping = {field: next((normalized[_norm(alias)] for alias in aliases if _norm(alias) in normalized), None)
                       for field, aliases in CONTRACTS[family].items()}
            mapping = {field: value for field, value in mapping.items() if value}
            if signature in mapping and {"nomenclature", "characteristic"}.issubset(mapping):
                candidates.append(family); partial[family] = mapping
        if len(candidates) == 1:
            source_type = candidates[0]; status = "mapping_required"
            missing = sorted(REQUIRED[source_type] - partial[source_type].keys())
            matches = partial
    return {"source_type": source_type, "status": status, "mapping_status": "ready" if status == "detected" else status,
            "required_missing": missing, "detected_columns": list(map(str, columns)),
            "matches": sorted(matches), "mapping": matches.get(source_type, {}),
            "diagnostic_code": diagnostic_code, "diagnostic_found": diagnostic_found,
            "diagnostic_missing": diagnostic_missing}


def read_excel_source(data: bytes, sheet: str | None = None) -> tuple[str, pd.DataFrame]:
    with pd.ExcelFile(BytesIO(data)) as workbook:
        selected = sheet or workbook.sheet_names[0]
        if selected not in workbook.sheet_names:
            raise ValueError("sheet_not_found")
        table = pd.read_excel(workbook, sheet_name=selected)
    table.columns = [str(column).strip() for column in table.columns]
    return selected, table.dropna(how="all")


def _canonical_record(raw: Mapping[str, Any], mapping: Mapping[str, str], provenance: Mapping[str, Any]) -> dict[str, Any]:
    def get(field: str) -> Any:
        return raw.get(mapping[field]) if field in mapping else None
    identity = build_canonical_sku_identity({"nomenclature": get("nomenclature"), "characteristic": get("characteristic")})
    record = {**provenance, "sku_key": identity["sku_key"], "nomenclature": identity["nomenclature"],
              "characteristic": identity["characteristic"], "identity_diagnostics": identity["diagnostics"]}
    for field in mapping:
        if field in {"nomenclature", "characteristic"}:
            continue
        value = get(field)
        if field in {"snapshot_at", "occurred_at"}:
            record[field] = _timestamp(value)
        elif field in {"source_stock_quantity", "source_position_balance", "cell_picking_order", "actual_quantity",
                       "accounting_quantity", "box_quantity", "reported_pallets", "quantity", "source_pick_order",
                       "weight", "length", "width", "height", "boxes_per_layer", "layers_per_pallet", "quantity_per_box"}:
            record[field] = _number(value)
            record[field + "_raw"] = _json_value(value)
        else:
            record[field] = _json_value(value)
    if provenance["source_type"] == "vgh":
        a, b = record.get("boxes_per_layer"), record.get("layers_per_pallet")
        record["source_boxes_per_pallet"] = a * b if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        record["palletization_authority"] = "source_layer_norm" if record["source_boxes_per_pallet"] else "unresolved"
    return record


def normalize_table(table: pd.DataFrame, *, dataset_id: str, filename: str, data_hash: str,
                    source_type: str, sheet: str, imported_at: str, mapping: Mapping[str, str] | None = None,
                    parser_version: str = PARSER_VERSION,
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mapping = dict(mapping or detect_source_type(table.columns)["mapping"])
    raw_records, canonical = [], []
    for ordinal, (source_index, series) in enumerate(table.iterrows(), 2):
        provenance = {"dataset_id": dataset_id, "source_file_name": filename, "content_hash": data_hash,
                      "source_type": source_type, "parser_version": parser_version, "sheet": sheet,
                      "source_row": ordinal, "source_index": _json_value(source_index), "imported_at": imported_at}
        raw = {str(key): _json_value(value) for key, value in series.items()}
        raw_records.append({**provenance, "raw": raw})
        canonical.append(_canonical_record(raw, mapping, provenance))
    return raw_records, canonical


def _day(record: Mapping[str, Any]) -> str | None:
    value = record.get("snapshot_at") or record.get("occurred_at")
    return str(value)[:10] if value else None


def _duplicates(raw_records: list[dict[str, Any]]) -> int:
    values = [json.dumps(row["raw"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in raw_records]
    return len(values) - len(set(values))


def validate_records(source_type: str, raw: list[dict[str, Any]], rows: list[dict[str, Any]],
                     geometry_cells: set[str] | None = None) -> dict[str, Any]:
    geometry_cells = geometry_cells or set()
    days = sorted({day for row in rows if (day := _day(row))})
    sku = {row["sku_key"] for row in rows if row.get("sku_key")}
    diagnostics: dict[str, Any] = {"rows": len(raw), "unique_sku": len(sku), "missing_sku": sum(not r.get("sku_key") for r in rows),
        "duplicate_raw_rows": _duplicates(raw), "period_from": days[0] if days else None, "period_to": days[-1] if days else None,
        "snapshot_count": len(days) if source_type == "historical_placement" else None,
        "zero_quantities": 0, "negative_quantities": 0, "missing_quantities": 0,
        "identity_collisions": find_canonical_identity_collisions(rows), "warnings": [], "errors": []}
    quantity_field = {"receipts": "box_quantity", "outbound": "quantity", "inventory": "actual_quantity"}.get(source_type)
    if quantity_field:
        values = [row.get(quantity_field) for row in rows]
        diagnostics.update(zero_quantities=sum(v == 0 for v in values), negative_quantities=sum(isinstance(v, (int, float)) and v < 0 for v in values), missing_quantities=sum(v is None for v in values))
    if source_type == "historical_placement":
        def grouped(field: str, value: str, *, across_snapshots: bool = False) -> int:
            groups = defaultdict(set)
            for row in rows:
                if row.get(field) not in (None, "") and row.get(value) not in (None, ""):
                    key = str(row[field]) if across_snapshots else (row.get("snapshot_at"), str(row[field]))
                    groups[key].add(str(row[value]))
            return sum(len(items) > 1 for items in groups.values())
        cells = {str(r.get("cell")) for r in rows if r.get("cell") not in (None, "")}
        orders = defaultdict(set)
        for row in rows:
            if row.get("cell") and row.get("cell_picking_order") is not None:
                orders[str(row["cell"])].add(row["cell_picking_order"])
        diagnostics.update(unique_cells=len(cells), sku_in_multiple_cells=grouped("sku_key", "cell"),
            multiple_sku_per_cell=grouped("cell", "sku_key"), multiple_sku_per_pallet=grouped("source_pallet_ref", "sku_key"),
            pallet_in_multiple_cells=grouped("source_pallet_ref", "cell", across_snapshots=True),
            # No authoritative resolver from the historical 1C address string
            # to geometry cell_key exists in the project.  Do not compare it
            # with ad-hoc display/code fields.
            unknown_geometry_cells=None,
            historical_cell_resolution="historical_cell_not_resolved_to_geometry",
            cell_picking_order_conflicts=sum(len(values) > 1 for values in orders.values()),
            cell_picking_order_evidence=[{"cell": cell, "picking_orders": sorted(values), "conflict": len(values) > 1} for cell, values in sorted(orders.items())])
        diagnostics["warnings"].append("historical_cell_not_resolved_to_geometry")
    for key, label in (("missing_sku", "missing_sku"), ("duplicate_raw_rows", "duplicate_raw_rows"),
                       ("identity_collisions", "identity_collisions"), ("negative_quantities", "negative_quantities"),
                       ("missing_quantities", "missing_quantities")):
        if diagnostics.get(key): diagnostics["warnings"].append(label)
    return diagnostics


def _known_july_placement_check(filename: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Apply the supplied fingerprint only to the explicitly known export."""
    if _norm(Path(filename).name) != _norm("размещение июль.xlsx"):
        return None
    expected = [(date(2026, 7, 1) + timedelta(days=offset)).isoformat() for offset in range(32)]
    counts = Counter(_day(row) for row in rows)
    missing = [day for day in expected if not counts.get(day)]
    return {"expected_snapshot_count": 32, "actual_snapshot_count": len({day for day in counts if day}),
            "period_expected": [expected[0], expected[-1]], "missing_dates": missing,
            "rows_2026_07_15": counts.get("2026-07-15", 0), "rows_2026_07_15_expected": 587,
            "snapshot_count_matches": len({day for day in counts if day}) == 32,
            "july_15_fingerprint_matches": counts.get("2026-07-15", 0) == 587}


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def load_registry(root: Path = DATA_ROOT) -> dict[str, Any]:
    path = root / "registry.json"
    if not path.exists(): return {"registry_version": 2, "datasets": [], "diagnostics": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"registry_version": 1, "datasets": [], "warning": "registry_unreadable"}
    if not isinstance(state.get("datasets"), list):
        return {"registry_version": 2, "datasets": [], "warning": "registry_invalid", "diagnostics": []}
    # PR #161 registries did not distinguish immutable versions from active
    # source slots.  Migrate in memory deterministically and flag ambiguous
    # slots rather than allowing every version into business queries.
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in state["datasets"]:
        logical = item.get("logical_source_id") or logical_source_identity(
            item.get("source_type", UNKNOWN_SOURCE), item.get("source_file_name", ""), item.get("sheet", "Sheet1"))
        item.setdefault("logical_source_id", logical)
        item.setdefault("version", item.get("content_hash"))
        item.setdefault("superseded_by", None)
        item.setdefault("supersedes", None)
        groups[logical].append(item)
    diagnostics = list(state.get("diagnostics", []))
    for logical, versions in groups.items():
        if all("active" not in item for item in versions):
            ordered = sorted(versions, key=lambda item: (item.get("imported_at", ""), item.get("dataset_id", "")))
            for item in ordered: item["active"] = False
            ordered[-1]["active"] = True
            if len(ordered) > 1:
                diagnostics.append({"code": "registry_activation_review_required", "logical_source_id": logical})
    state.update(registry_version=2, diagnostics=diagnostics)
    return state


def logical_source_identity(source_type: str, filename: str, sheet: str) -> str:
    normalized_name = _norm(Path(filename).name)
    payload = "\x1f".join((source_type, normalized_name, _norm(sheet)))
    return "source:" + hashlib.sha256(payload.encode()).hexdigest()


def active_datasets(registry: Mapping[str, Any], source_type: str | None = None) -> list[dict[str, Any]]:
    """The single business-query boundary for lifecycle filtering."""
    return [item for item in registry.get("datasets", [])
            if item.get("active", True) and (source_type is None or item.get("source_type") == source_type)]


def _import_excel_dataset_buffered(data: bytes, filename: str, *, sheet: str | None = None, root: Path = DATA_ROOT,
                         geometry_cells: Iterable[str] | None = None, reimport: bool = False,
                         parser_version: str = PARSER_VERSION) -> dict[str, Any]:
    # Hash and consult content provenance before opening the workbook.  With no
    # explicit sheet, a prior default-sheet import is safe to reuse; an
    # explicitly selected sheet must match exactly.
    digest = content_hash(data)
    root.mkdir(parents=True, exist_ok=True)
    registry = load_registry(root)
    content_match = next((item for item in registry["datasets"]
        if item.get("content_hash") == digest and item.get("parser_version") == parser_version
        and (sheet is None or item.get("sheet") == sheet)), None)
    if content_match and not reimport:
        return {**content_match, "reused": True, "duplicate_filename": filename if filename != content_match.get("source_file_name") else None}
    selected, table = read_excel_source(data, sheet)
    detection = detect_source_type(table.columns)
    if detection["source_type"] == UNKNOWN_SOURCE or detection["status"] != "detected":
        return {"status": detection["status"], "source_type": UNKNOWN_SOURCE, "source_label": SOURCE_LABELS[UNKNOWN_SOURCE],
                "detected_source_family": detection["source_type"] if detection["source_type"] != UNKNOWN_SOURCE else None,
                "required_missing": detection["required_missing"], "detected_columns": detection["detected_columns"],
                "matches": detection["matches"], "diagnostic_code": detection["diagnostic_code"],
                "diagnostic_found": detection["diagnostic_found"], "diagnostic_missing": detection["diagnostic_missing"],
                "errors": [detection["status"]]}
    source_type = detection["source_type"]
    dataset_id = dataset_identity(digest, source_type, selected, parser_version)
    existing = next((item for item in registry["datasets"] if item.get("dataset_id") == dataset_id), None)
    if existing and not reimport:
        return {**existing, "reused": True}
    imported_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    logical_source_id = logical_source_identity(source_type, filename, selected)
    raw, canonical = normalize_table(table, dataset_id=dataset_id, filename=filename, data_hash=digest,
        source_type=source_type, sheet=selected, imported_at=imported_at, mapping=detection["mapping"], parser_version=parser_version)
    diagnostics = validate_records(source_type, raw, canonical, set(map(str, geometry_cells or [])))
    known_check = _known_july_placement_check(filename, canonical) if source_type == "historical_placement" else None
    if known_check:
        diagnostics["known_july_validation"] = known_check
        if known_check["missing_dates"]: diagnostics["warnings"].append("known_july_missing_dates")
        if not known_check["july_15_fingerprint_matches"]: diagnostics["warnings"].append("known_july_15_fingerprint_mismatch")
    artifact = root / dataset_id.removeprefix("dataset:")
    staging = Path(tempfile.mkdtemp(dir=root, prefix=".staging-"))
    _write_jsonl(staging / "raw.jsonl.gz", raw)
    partitions = defaultdict(list)
    for row in canonical: partitions[_day(row) or "undated"].append(row)
    daily: dict[str, Any] = {}
    sku_index = sorted({row.get("sku_key") for row in canonical if row.get("sku_key")})
    document_keys: set[str] = set()
    for day, rows in partitions.items():
        _write_jsonl(staging / "canonical" / f"date={day}.jsonl.gz", rows)
        docs = {(r.get("document_ref") or r.get("inventory_ref"), r.get("document_number") or r.get("inventory_number"), r.get("occurred_at")) for r in rows}
        document_keys.update(json.dumps(key, ensure_ascii=False, default=str) for key in docs)
        positive = [r.get("quantity") for r in rows if isinstance(r.get("quantity"), (int, float)) and r.get("quantity") > 0]
        daily[day] = {"rows": len(rows), "sku": len({r.get("sku_key") for r in rows if r.get("sku_key")}),
            "cells": len({r.get("cell") for r in rows if r.get("cell")}), "documents": len(docs),
            "positive_quantity": sum(positive), "positive_sku_keys": sorted({r.get("sku_key") for r in rows if r.get("sku_key") and isinstance(r.get("quantity"), (int, float)) and r.get("quantity") > 0})}
    index = {"sku_keys": sku_index, "dates": sorted(partitions), "daily": daily, "document_keys": sorted(document_keys)}
    metadata = {"dataset_id": dataset_id, "source_file_name": filename, "content_hash": digest, "source_type": source_type,
        "source_label": SOURCE_LABELS[source_type], "parser_version": parser_version, "sheet": selected, "rows": len(raw),
        "period_from": diagnostics["period_from"], "period_to": diagnostics["period_to"], "unique_sku": diagnostics["unique_sku"],
        "imported_at": imported_at, "status": "ready_with_warnings" if diagnostics["warnings"] else "ready", "errors": diagnostics["errors"],
        "warnings": diagnostics["warnings"], "detected_columns": detection["detected_columns"],
        "mapping": detection["mapping"], "mapping_status": "ready", "artifact": str(artifact), "partitions": sorted(partitions),
        "index": index, "diagnostics": diagnostics, "logical_source_id": logical_source_id, "version": digest,
        "active": True, "superseded_by": None, "supersedes": None}
    previous = [item for item in registry["datasets"] if item.get("active", True) and item.get("logical_source_id") == logical_source_id and item.get("dataset_id") != dataset_id]
    metadata["supersedes"] = previous[-1]["dataset_id"] if previous else None
    _atomic_json(staging / "metadata.json", metadata)
    if artifact.exists():
        # A reparse of the same immutable identity already has a complete artifact.
        import shutil; shutil.rmtree(staging)
    else:
        os.replace(staging, artifact)
    for item in previous:
        item["active"] = False
        item["superseded_by"] = dataset_id
    registry["datasets"] = [item for item in registry["datasets"] if item.get("dataset_id") != dataset_id] + [metadata]
    # Different logical files may coexist, but overlapping dates/business keys
    # are surfaced rather than silently summed without a warning.
    overlaps = []
    for item in active_datasets(registry, source_type):
        if item["dataset_id"] == dataset_id: continue
        dates = set(item.get("index", {}).get("dates", item.get("partitions", []))) & set(index["dates"])
        keys = set(item.get("index", {}).get("document_keys", [])) & set(index["document_keys"])
        if dates or keys: overlaps.append({"dataset_id": item["dataset_id"], "dates": sorted(dates), "duplicate_document_keys": len(keys)})
    if overlaps:
        metadata["diagnostics"]["overlapping_active_sources"] = overlaps
        metadata["warnings"].append("overlapping_active_sources")
    registry["datasets"].sort(key=lambda item: (item.get("imported_at", ""), item["dataset_id"]))
    registry["registry_version"] = 2
    _atomic_json(root / "registry.json", registry)
    return {**metadata, "reused": False}


BUSINESS_KEYS = {
    "outbound": ("document_ref", "line_number"),
    "receipts": ("document_ref", "line_number"),
    "inventory": ("inventory_ref", "line_number"),
    "vgh": ("sku_key",),
}
MATERIAL_FIELDS = {
    "outbound": ("occurred_at", "warehouse", "sku_key", "quantity", "source_pick_order"),
    "receipts": ("occurred_at", "warehouse", "sku_key", "box_quantity", "reported_pallets",
                 "terminal_completed", "expected_receipt"),
    "inventory": ("occurred_at", "warehouse", "sku_key", "actual_quantity", "accounting_quantity"),
    "vgh": ("boxes_per_layer", "layers_per_pallet", "quantity_per_box", "source_boxes_per_pallet"),
}


def _fingerprint(values: Any) -> str:
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _business_evidence(row: Mapping[str, Any], source_type: str) -> dict[str, Any] | None:
    fields = BUSINESS_KEYS.get(source_type)
    if not fields:
        return None
    key_values = [row.get(field) for field in fields]
    if any(value in (None, "") for value in key_values):
        return None
    payload = {field: row.get(field) for field in MATERIAL_FIELDS[source_type]}
    return {"business_key": json.dumps(key_values, ensure_ascii=False, separators=(",", ":"), default=str),
            "payload_fingerprint": _fingerprint(payload), "payload": payload,
            "day": _day(row), "dataset_id": row.get("dataset_id"),
            "source_file_name": row.get("source_file_name"), "source_row": row.get("source_row")}


def _stream_xlsx_dataset(data: bytes, filename: str, *, sheet: str | None, root: Path,
                         geometry_cells: Iterable[str] | None, parser_version: str,
                         fail_after_rows: int | None = None) -> dict[str, Any]:
    """Import XLSX with one canonical and one RAW record resident at a time.

    Memory is bounded by the uploaded XLSX bytes, openpyxl's read-only parser,
    exact SHA-256 row fingerprints, SKU/business-key indexes, and per-day
    aggregate sets.  It never creates a pandas table or complete row lists.
    """
    from openpyxl import load_workbook

    digest = content_hash(data)
    root.mkdir(parents=True, exist_ok=True)
    registry = load_registry(root)
    content_match = next((item for item in registry["datasets"]
        if item.get("content_hash") == digest and item.get("parser_version") == parser_version
        and (sheet is None or item.get("sheet") == sheet)), None)
    if content_match:
        state = "existing_active_version" if content_match.get("active", True) else "existing_superseded_version"
        return {**content_match, "reused": True, "reuse_state": state, "status": state,
                "duplicate_filename": filename if filename != content_match.get("source_file_name") else None}

    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        selected = sheet or workbook.sheetnames[0]
        if selected not in workbook.sheetnames:
            raise ValueError("sheet_not_found")
        worksheet = workbook[selected]
        iterator = worksheet.iter_rows(values_only=True)
        headers = [str(value).strip() if value is not None else "" for value in next(iterator, ())]
        detection = detect_source_type(headers)
        if detection["source_type"] == UNKNOWN_SOURCE or detection["status"] != "detected":
            return {"status": detection["status"], "source_type": UNKNOWN_SOURCE,
                    "source_label": SOURCE_LABELS[UNKNOWN_SOURCE],
                    "detected_source_family": detection["source_type"] if detection["source_type"] != UNKNOWN_SOURCE else None,
                    "required_missing": detection["required_missing"], "detected_columns": headers,
                    "matches": detection["matches"], "diagnostic_code": detection["diagnostic_code"],
                    "diagnostic_found": detection["diagnostic_found"],
                    "diagnostic_missing": detection["diagnostic_missing"], "errors": [detection["status"]]}
        source_type = detection["source_type"]
        dataset_id = dataset_identity(digest, source_type, selected, parser_version)
        imported_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        logical_source_id = logical_source_identity(source_type, filename, selected)
        staging = Path(tempfile.mkdtemp(dir=root, prefix=".staging-"))
        writers: dict[str, Any] = {}
        raw_writer = index_writer = None
        row_count = missing_sku = duplicate_raw = zero = negative = missing_qty = 0
        raw_hashes: set[str] = set(); sku_keys: set[str] = set(); days: set[str] = set()
        daily: dict[str, dict[str, Any]] = defaultdict(lambda: {"rows": 0, "sku_keys": set(), "cells": set(),
            "documents": set(), "positive_quantity": 0, "positive_sku_keys": set()})
        placement_sku_cells: dict[tuple[Any, Any], set[str]] = defaultdict(set)
        placement_cell_sku: dict[tuple[Any, Any], set[str]] = defaultdict(set)
        placement_pallet_sku: dict[tuple[Any, Any], set[str]] = defaultdict(set)
        placement_pallet_cells: dict[str, set[str]] = defaultdict(set)
        cell_orders: dict[str, set[Any]] = defaultdict(set)
        business_key_count = 0
        try:
            raw_writer = gzip.open(staging / "raw.jsonl.gz", "wt", encoding="utf-8")
            index_writer = gzip.open(staging / "business_index.jsonl.gz", "wt", encoding="utf-8")
            for source_row, values in enumerate(iterator, 2):
                if not any(value is not None for value in values):
                    continue
                if fail_after_rows is not None and row_count >= fail_after_rows:
                    raise RuntimeError("injected_streaming_import_failure")
                raw_values = {header: _json_value(value) for header, value in zip(headers, values) if header}
                provenance = {"dataset_id": dataset_id, "source_file_name": filename, "content_hash": digest,
                    "source_type": source_type, "parser_version": parser_version, "sheet": selected,
                    "source_row": source_row, "source_index": source_row - 2, "imported_at": imported_at}
                raw_record = {**provenance, "raw": raw_values}
                raw_json = json.dumps(raw_values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                raw_fp = hashlib.sha256(raw_json.encode()).hexdigest()
                duplicate_raw += raw_fp in raw_hashes; raw_hashes.add(raw_fp)
                raw_writer.write(json.dumps(raw_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                record = _canonical_record(raw_values, detection["mapping"], provenance)
                day = _day(record) or "undated"; days.add(day)
                if day not in writers:
                    target = staging / "canonical" / f"date={day}.jsonl.gz"; target.parent.mkdir(parents=True, exist_ok=True)
                    writers[day] = gzip.open(target, "wt", encoding="utf-8")
                writers[day].write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                sku = record.get("sku_key"); missing_sku += not bool(sku)
                if sku: sku_keys.add(sku); daily[day]["sku_keys"].add(sku)
                daily[day]["rows"] += 1
                if record.get("cell") not in (None, ""): daily[day]["cells"].add(str(record["cell"]))
                doc = (record.get("document_ref") or record.get("inventory_ref"),
                       record.get("document_number") or record.get("inventory_number"), record.get("occurred_at"))
                if any(doc): daily[day]["documents"].add(json.dumps(doc, ensure_ascii=False, default=str))
                qty_field = {"receipts": "box_quantity", "outbound": "quantity", "inventory": "actual_quantity"}.get(source_type)
                if qty_field:
                    qty = record.get(qty_field); zero += qty == 0; negative += isinstance(qty, (int, float)) and qty < 0; missing_qty += qty is None
                    if qty_field == "quantity" and isinstance(qty, (int, float)) and qty > 0:
                        daily[day]["positive_quantity"] += qty
                        if sku: daily[day]["positive_sku_keys"].add(sku)
                evidence = _business_evidence(record, source_type)
                if evidence:
                    index_writer.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                    business_key_count += 1
                if source_type == "historical_placement":
                    snap, cell, pallet = record.get("snapshot_at"), str(record.get("cell") or ""), str(record.get("source_pallet_ref") or "")
                    if sku and cell: placement_sku_cells[(snap, sku)].add(cell); placement_cell_sku[(snap, cell)].add(sku)
                    if pallet and sku: placement_pallet_sku[(snap, pallet)].add(sku)
                    if pallet and cell: placement_pallet_cells[pallet].add(cell)
                    if cell and record.get("cell_picking_order") is not None: cell_orders[cell].add(record["cell_picking_order"])
                row_count += 1
        except Exception:
            for stream in (raw_writer, index_writer, *writers.values()):
                if stream: stream.close()
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            for stream in (raw_writer, index_writer, *writers.values()):
                if stream and not stream.closed: stream.close()
    finally:
        workbook.close()

    real_days = sorted(day for day in days if day != "undated")
    warnings = [name for value, name in ((missing_sku, "missing_sku"), (duplicate_raw, "duplicate_raw_rows"),
        (negative, "negative_quantities"), (missing_qty, "missing_quantities")) if value]
    diagnostics: dict[str, Any] = {"rows": row_count, "unique_sku": len(sku_keys), "missing_sku": missing_sku,
        "duplicate_raw_rows": duplicate_raw, "period_from": real_days[0] if real_days else None,
        "period_to": real_days[-1] if real_days else None, "snapshot_count": len(real_days) if source_type == "historical_placement" else None,
        "zero_quantities": zero, "negative_quantities": negative, "missing_quantities": missing_qty,
        "identity_collisions": [], "warnings": warnings, "errors": [], "streaming_import": True,
        "memory_bound": "xlsx bytes + openpyxl read-only window + exact row hashes + compact unique-key indexes"}
    if source_type == "historical_placement":
        diagnostics.update(unique_cells=len({cell for _, cell in placement_cell_sku}),
            sku_in_multiple_cells=sum(len(x) > 1 for x in placement_sku_cells.values()),
            multiple_sku_per_cell=sum(len(x) > 1 for x in placement_cell_sku.values()),
            multiple_sku_per_pallet=sum(len(x) > 1 for x in placement_pallet_sku.values()),
            pallet_in_multiple_cells=sum(len(x) > 1 for x in placement_pallet_cells.values()), unknown_geometry_cells=None,
            historical_cell_resolution="historical_cell_not_resolved_to_geometry",
            cell_picking_order_conflicts=sum(len(x) > 1 for x in cell_orders.values()),
            cell_picking_order_evidence=[{"cell": c, "picking_orders": sorted(v), "conflict": len(v) > 1} for c, v in sorted(cell_orders.items())])
        diagnostics["warnings"].append("historical_cell_not_resolved_to_geometry")
    index_daily = {day: {"rows": values["rows"], "sku": len(values["sku_keys"]), "cells": len(values["cells"]),
        "documents": len(values["documents"]), "positive_quantity": values["positive_quantity"],
        "positive_sku_keys": sorted(values["positive_sku_keys"])} for day, values in daily.items()}
    index = {"sku_keys": sorted(sku_keys), "dates": sorted(days), "daily": index_daily,
             "business_key_count": business_key_count, "business_index_artifact": "business_index.jsonl.gz"}
    artifact = root / dataset_id.removeprefix("dataset:")
    previous = [item for item in registry["datasets"] if item.get("active", True) and item.get("logical_source_id") == logical_source_id]
    metadata = {"dataset_id": dataset_id, "source_file_name": filename, "content_hash": digest,
        "source_type": source_type, "source_label": SOURCE_LABELS[source_type], "parser_version": parser_version,
        "sheet": selected, "rows": row_count, "period_from": diagnostics["period_from"], "period_to": diagnostics["period_to"],
        "unique_sku": len(sku_keys), "imported_at": imported_at, "status": "ready_with_warnings" if diagnostics["warnings"] else "ready",
        "errors": [], "warnings": diagnostics["warnings"], "detected_columns": headers, "mapping": detection["mapping"],
        "mapping_status": "ready", "artifact": str(artifact), "partitions": sorted(days), "index": index,
        "diagnostics": diagnostics, "logical_source_id": logical_source_id, "version": digest, "active": True,
        "superseded_by": None, "supersedes": previous[-1]["dataset_id"] if previous else None}
    _atomic_json(staging / "metadata.json", metadata)
    if artifact.exists(): shutil.rmtree(staging)
    else: os.replace(staging, artifact)
    for item in previous: item["active"] = False; item["superseded_by"] = dataset_id
    registry["datasets"] = [item for item in registry["datasets"] if item.get("dataset_id") != dataset_id] + [metadata]
    registry["datasets"].sort(key=lambda item: (item.get("imported_at", ""), item["dataset_id"]))
    registry["registry_version"] = 3
    _atomic_json(root / "registry.json", registry)
    return {**metadata, "reused": False}


def import_excel_dataset(data: bytes, filename: str, *, sheet: str | None = None, root: Path = DATA_ROOT,
                         geometry_cells: Iterable[str] | None = None, reimport: bool = False,
                         parser_version: str = PARSER_VERSION, _fail_after_rows: int | None = None) -> dict[str, Any]:
    """Hash first, then use bounded-memory XLSX import; retain legacy XLS compatibility."""
    if Path(filename).suffix.casefold() == ".xlsx" and not reimport:
        return _stream_xlsx_dataset(data, filename, sheet=sheet, root=root, geometry_cells=geometry_cells,
                                    parser_version=parser_version, fail_after_rows=_fail_after_rows)
    result = _import_excel_dataset_buffered(data, filename, sheet=sheet, root=root, geometry_cells=geometry_cells,
                                            reimport=reimport, parser_version=parser_version)
    if Path(filename).suffix.casefold() == ".xls" and len(data) > 20_000_000:
        result.setdefault("warnings", []).append("large_xls_uses_buffered_import")
    return result


def load_dataset_rows(dataset: Mapping[str, Any], day: str | None = None, *, raw: bool = False) -> list[dict[str, Any]]:
    artifact = Path(str(dataset["artifact"]))
    if raw: return _read_jsonl(artifact / "raw.jsonl.gz")
    if day is not None: return _read_jsonl(artifact / "canonical" / f"date={day}.jsonl.gz")
    rows = []
    for partition in dataset.get("partitions", []): rows.extend(_read_jsonl(artifact / "canonical" / f"date={partition}.jsonl.gz"))
    return rows


def positive_outbound(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if isinstance(row.get("quantity"), (int, float)) and row["quantity"] > 0]


def date_summary(registry: Mapping[str, Any], day: str) -> dict[str, Any]:
    result = {"operational_day": day, "placement": {"snapshot_exists": False, "rows": 0, "sku": 0, "cells": 0},
              "inventory": {"documents": 0, "rows": 0}, "receipts": {"documents": 0, "rows": 0, "accepted_boxes": None},
              "outbound": {"documents": 0, "lines": 0, "positive_demand": 0}, "vgh": {"relevant_sku": 0, "covered_sku": 0},
              "next_day_placement_available": False}
    source_rows = defaultdict(list)
    for dataset in active_datasets(registry):
        # Only the selected partition is opened; persisted indexes handle VGH
        # and next-day existence below.
        source_rows[dataset["source_type"]].extend(load_dataset_rows(dataset, day))
    placement = source_rows["historical_placement"]
    result["placement"] = {"snapshot_exists": bool(placement), "rows": len(placement), "sku": len({r.get("sku_key") for r in placement if r.get("sku_key")}), "cells": len({r.get("cell") for r in placement if r.get("cell")})}
    for source, target in (("inventory", "inventory"), ("receipts", "receipts")):
        rows = source_rows[source]; docs = {(r.get("document_ref") or r.get("inventory_ref"), r.get("document_number") or r.get("inventory_number"), r.get("occurred_at")) for r in rows}
        result[target].update(documents=len(docs), rows=len(rows))
    outbound = source_rows["outbound"]; positive = positive_outbound(outbound)
    result["outbound"] = {"documents": len({(r.get("document_ref"), r.get("document_number"), r.get("occurred_at")) for r in outbound}), "lines": len(outbound), "positive_demand": sum(r["quantity"] for r in positive)}
    demanded = {r.get("sku_key") for r in positive if r.get("sku_key")}; vgh = set()
    for dataset in active_datasets(registry):
        if dataset.get("source_type") == "vgh": vgh.update(dataset.get("index", {}).get("sku_keys", []))
        if dataset.get("source_type") == "historical_placement":
            tomorrow = (pd.Timestamp(day).date() + timedelta(days=1)).isoformat()
            result["next_day_placement_available"] |= tomorrow in dataset.get("index", {}).get("dates", dataset.get("partitions", []))
    result["vgh"] = {"relevant_sku": len(demanded), "covered_sku": len(demanded & vgh)}
    return result


def cross_source_coverage(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    indexes: dict[str, set[str]] = defaultdict(set)
    for dataset in active_datasets(registry):
        indexes[dataset["source_type"]].update(dataset.get("index", {}).get("sku_keys", []))
    scope = indexes["outbound"] | indexes["historical_placement"]
    return [{"sku_key": sku, "outbound": sku in indexes["outbound"], "placement": sku in indexes["historical_placement"],
             "vgh": sku in indexes["vgh"], "inventory": sku in indexes["inventory"], "receipts": sku in indexes["receipts"]} for sku in sorted(scope)]


def activate_dataset_version(dataset_id: str, *, root: Path = DATA_ROOT) -> dict[str, Any]:
    """Explicitly activate an immutable version without reparsing its workbook."""
    registry = load_registry(root)
    selected = next((item for item in registry["datasets"] if item.get("dataset_id") == dataset_id), None)
    if selected is None:
        raise KeyError("dataset_not_found")
    logical = selected.get("logical_source_id")
    current = [item for item in registry["datasets"] if item.get("active", True)
               and item.get("logical_source_id") == logical and item is not selected]
    predecessor = current[-1] if current else None
    for item in current:
        item["active"] = False
        item["superseded_by"] = dataset_id
    selected["active"] = True
    selected["superseded_by"] = None
    selected["supersedes"] = predecessor.get("dataset_id") if predecessor else selected.get("supersedes")
    registry["diagnostics"] = [d for d in registry.get("diagnostics", [])
        if not (d.get("code") == "registry_activation_review_required" and d.get("logical_source_id") == logical)]
    registry["readiness_invalidated_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    _atomic_json(root / "registry.json", registry)
    return dict(selected)


def _source_conflicts(registry: Mapping[str, Any], source_type: str, day: str | None = None) -> dict[str, Any]:
    groups: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for dataset in active_datasets(registry, source_type):
        path = Path(str(dataset["artifact"])) / "business_index.jsonl.gz"
        if path.exists():
            evidence_rows = _read_jsonl(path)
        else:
            evidence_rows = [e for row in load_dataset_rows(dataset, day) if (e := _business_evidence(row, source_type))]
        for evidence in evidence_rows:
            if day is None or evidence.get("day") == day or source_type == "vgh":
                groups[evidence["business_key"]][evidence["payload_fingerprint"]].append(evidence)
    duplicates, conflicts = [], []
    for key, payloads in sorted(groups.items()):
        occurrences = [item for values in payloads.values() for item in values]
        if len(payloads) > 1:
            conflicts.append({"code": "conflicting_factual_business_key", "source_type": source_type,
                "business_key": json.loads(key), "occurrences": occurrences})
        elif len(occurrences) > 1:
            duplicates.append({"code": "duplicate_factual_evidence_collapsed", "source_type": source_type,
                "business_key": json.loads(key), "occurrences": occurrences})
    return {"duplicates": duplicates, "conflicts": conflicts, "groups": groups}


def load_effective_rows(source_type: str, day: str | None = None, *, registry: Mapping[str, Any] | None = None,
                        root: Path = DATA_ROOT, strict: bool = True) -> dict[str, Any]:
    """Authoritative replay boundary: active evidence in, deduplicated factual rows out."""
    registry = registry or load_registry(root)
    if source_type == "historical_placement":
        candidates = [dataset for dataset in active_datasets(registry, source_type)
                      if day is None or day in dataset.get("index", {}).get("dates", dataset.get("partitions", []))]
        if day is not None and len(candidates) > 1:
            conflict = {"code": "multiple_active_placement_sources_for_snapshot", "day": day,
                        "dataset_ids": [item["dataset_id"] for item in candidates]}
            if strict: raise ValueError("multiple_active_placement_sources_for_snapshot")
            return {"rows": [], "duplicates": [], "conflicts": [conflict], "authoritative": False}
        rows = [row for dataset in candidates for row in load_dataset_rows(dataset, day)]
        return {"rows": rows, "duplicates": [], "conflicts": [], "authoritative": len(candidates) <= 1}
    analysis = _source_conflicts(registry, source_type, day)
    if analysis["conflicts"] and strict:
        raise ValueError("conflicting_factual_business_key")
    representatives: dict[str, dict[str, Any]] = {}
    for dataset in sorted(active_datasets(registry, source_type), key=lambda x: x["dataset_id"]):
        for row in load_dataset_rows(dataset, day):
            evidence = _business_evidence(row, source_type)
            if evidence is None:
                representatives[f"unkeyed:{dataset['dataset_id']}:{row.get('source_row')}"] = dict(row)
                continue
            key = evidence["business_key"]
            if key in representatives: continue
            occurrences = [x for values in analysis["groups"].get(key, {}).values() for x in values]
            enriched = dict(row)
            enriched["duplicate_source_count"] = len(occurrences)
            enriched["duplicate_source_ids"] = sorted({str(x.get("dataset_id")) for x in occurrences})
            enriched["duplicate_source_filenames"] = sorted({str(x.get("source_file_name")) for x in occurrences})
            enriched["duplicate_row_evidence"] = [{"dataset_id": x.get("dataset_id"), "source_file_name": x.get("source_file_name"),
                                                     "source_row": x.get("source_row")} for x in occurrences]
            representatives[key] = enriched
    return {"rows": list(representatives.values()), "duplicates": analysis["duplicates"],
            "conflicts": analysis["conflicts"], "authoritative": not analysis["conflicts"]}


def geometry_model_signature(model: Mapping[str, Any]) -> str:
    cells = sorted(str(cell.get("cell_key") or "") for cell in model.get("cells", []) if isinstance(cell, Mapping))
    return _fingerprint({"model_id": model.get("model_id"), "cells": cells})


def load_cell_mappings(root: Path = DATA_ROOT) -> dict[str, Any]:
    path = root / "historical_cell_mappings.json"
    if not path.exists(): return {"version": 1, "mappings": []}
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {"version": 1, "mappings": [], "warning": "mapping_registry_unreadable"}


def save_historical_cell_mapping(source_cell: Any, geometry_cell_key: str, model: Mapping[str, Any], *,
                                 root: Path = DATA_ROOT, source_evidence: Any = None) -> dict[str, Any]:
    """Persist only an explicit user choice, scoped to exact geometry identity."""
    source = str(source_cell).replace("\u00a0", " ").strip()
    targets = [cell for cell in model.get("cells", []) if str(cell.get("cell_key") or "") == str(geometry_cell_key)]
    if len(targets) != 1: raise ValueError("unknown_or_ambiguous_geometry_target")
    state = load_cell_mappings(root); signature = geometry_model_signature(model)
    record = {"model_id": model.get("model_id"), "model_signature": signature, "source_cell": source,
        "geometry_cell_key": geometry_cell_key, "mapping_method": "user_confirmed",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "source_evidence": source_evidence, "validation_status": "valid"}
    state["mappings"] = [item for item in state["mappings"]
        if not (item.get("model_signature") == signature and item.get("source_cell") == source)] + [record]
    _atomic_json(root / "historical_cell_mappings.json", state)
    return record


def resolve_historical_cell(source_cell: Any, model: Mapping[str, Any], *, root: Path = DATA_ROOT,
                            mappings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve through proven source_cell identity or explicit confirmation; never guess."""
    source = str(source_cell).replace("\u00a0", " ").strip()
    signature = geometry_model_signature(model)
    cells = [cell for cell in model.get("cells", []) if isinstance(cell, Mapping)]
    valid_keys = {str(cell.get("cell_key")) for cell in cells if cell.get("cell_key") not in (None, "")}
    state = mappings or load_cell_mappings(root)
    persisted = next((item for item in state.get("mappings", []) if item.get("source_cell") == source
                      and item.get("model_signature") == signature), None)
    if persisted:
        target = str(persisted.get("geometry_cell_key") or "")
        if target in valid_keys: return {**persisted, "resolution_status": "resolved"}
        return {**persisted, "geometry_cell_key": None, "resolution_status": "stale", "diagnostic": "historical_cell_unresolved"}
    # source_cell is the only model field accepted as shared authoritative identity.
    exact = [str(cell.get("cell_key")) for cell in cells
             if str(cell.get("source_cell") or "").replace("\u00a0", " ").strip() == source]
    if len(exact) == 1:
        return {"source_cell": source, "geometry_cell_key": exact[0], "mapping_method": "exact_authoritative",
                "model_signature": signature, "resolution_status": "resolved"}
    if len(exact) > 1:
        return {"source_cell": source, "geometry_cell_key": None, "candidates": sorted(exact),
                "resolution_status": "ambiguous", "diagnostic": "historical_cell_ambiguous"}
    return {"source_cell": source, "geometry_cell_key": None, "resolution_status": "unresolved",
            "diagnostic": "historical_cell_unresolved"}


def load_effective_placement(day: str, model: Mapping[str, Any], *, registry: Mapping[str, Any] | None = None,
                             root: Path = DATA_ROOT, strict: bool = True) -> dict[str, Any]:
    view = load_effective_rows("historical_placement", day, registry=registry, root=root, strict=strict)
    resolved = []
    for row in view["rows"]:
        resolution = resolve_historical_cell(row.get("cell"), model, root=root)
        resolved.append({**row, "source_cell": row.get("cell"), "resolved_geometry_cell_key": resolution.get("geometry_cell_key"),
                         "cell_resolution_status": resolution["resolution_status"], "cell_resolution": resolution})
    return {**view, "rows": resolved}


def build_fact_route_readiness(placement_rows: Iterable[Mapping[str, Any]], demanded_sku_keys: Iterable[str],
                               usable_cell_keys: Iterable[str]) -> dict[str, Any]:
    rows = list(placement_rows); demanded = set(demanded_sku_keys); usable = set(usable_cell_keys)
    cells = {str(r.get("source_cell") or r.get("cell")) for r in rows if r.get("source_cell") or r.get("cell")}
    demand_rows = [r for r in rows if r.get("sku_key") in demanded]
    demand_cells = {str(r.get("source_cell") or r.get("cell")) for r in demand_rows}
    resolved = {str(r.get("source_cell") or r.get("cell")) for r in rows if r.get("resolved_geometry_cell_key")}
    demand_resolved = {str(r.get("source_cell") or r.get("cell")) for r in demand_rows
                       if r.get("resolved_geometry_cell_key") in usable}
    ambiguous = {str(r.get("source_cell") or r.get("cell")) for r in rows if r.get("cell_resolution_status") == "ambiguous"}
    return {"placement_rows": len(rows), "unique_factual_cells": len(cells), "resolved_cells": len(resolved),
        "unresolved_cells": len(cells - resolved), "ambiguous_cells": len(ambiguous),
        "overall_cell_coverage": {"resolved": len(resolved), "total": len(cells)},
        "demand_relevant_cell_coverage": {"resolved": len(demand_resolved), "total": len(demand_cells)},
        "resolved_row_sku_coverage": {"resolved": sum(bool(r.get("resolved_geometry_cell_key")) for r in rows), "total": len(rows)},
        "cells_without_usable_access_node": sorted({str(r.get("resolved_geometry_cell_key")) for r in demand_rows
            if r.get("resolved_geometry_cell_key") and r.get("resolved_geometry_cell_key") not in usable}),
        "fact_route_ready": demand_cells <= demand_resolved}


def build_monthly_data_readiness(registry: Mapping[str, Any], model: Mapping[str, Any], period_from: str,
                                 period_to: str, *, root: Path = DATA_ROOT,
                                 usable_cell_keys: Iterable[str] | None = None,
                                 receipts_required: bool = True) -> dict[str, Any]:
    """Pure, deterministic factual authority contract; it performs no simulation."""
    start, end = date.fromisoformat(period_from), date.fromisoformat(period_to)
    required_days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 2)]
    blockers: list[dict[str, Any]] = []; warnings: list[dict[str, Any]] = []
    registry_blockers = [d for d in registry.get("diagnostics", []) if d.get("code") == "registry_activation_review_required"]
    if registry_blockers: blockers.append({"code": "registry_activation_review_required", "message": "Требуется подтвердить активную версию источника."})
    placement_sources = {day: [d for d in active_datasets(registry, "historical_placement")
        if day in d.get("index", {}).get("dates", d.get("partitions", []))] for day in required_days}
    missing = [day for day, sources in placement_sources.items() if not sources]
    overlaps = [day for day, sources in placement_sources.items() if len(sources) > 1]
    if missing: blockers.append({"code": "missing_placement_snapshot", "dates": missing})
    if overlaps: blockers.append({"code": "multiple_active_placement_sources_for_snapshot", "dates": overlaps})
    analyses = {source: _source_conflicts(registry, source) for source in ("outbound", "receipts", "inventory", "vgh")}
    for source, analysis in analyses.items():
        if analysis["conflicts"]: blockers.append({"code": "conflicting_factual_business_key", "source_type": source,
                                                   "count": len(analysis["conflicts"]), "preview": analysis["conflicts"][:10]})
    outbound_rows = []
    for day in required_days[:-1]:
        view = load_effective_rows("outbound", day, registry=registry, root=root, strict=False)
        outbound_rows.extend(positive_outbound(view["rows"]))
    demanded = {row.get("sku_key") for row in outbound_rows if row.get("sku_key")}
    vgh_keys = {sku for dataset in active_datasets(registry, "vgh") for sku in dataset.get("index", {}).get("sku_keys", [])}
    missing_vgh = sorted(demanded - vgh_keys)
    if missing_vgh: blockers.append({"code": "missing_vgh_for_demanded_sku", "count": len(missing_vgh), "preview": missing_vgh[:10]})
    if not outbound_rows: blockers.append({"code": "outbound_not_available"})
    receipt_dates = {day for dataset in active_datasets(registry, "receipts") for day in dataset.get("index", {}).get("dates", [])}
    if receipts_required and not (set(required_days[:-1]) & receipt_dates): blockers.append({"code": "receipts_not_available"})
    route_checks = []
    usable = set(usable_cell_keys or [str(c.get("cell_key")) for c in model.get("cells", []) if c.get("cell_key")])
    if not overlaps:
        for day in required_days[:-1]:
            placement = load_effective_placement(day, model, registry=registry, root=root, strict=False)
            day_demand = {r.get("sku_key") for r in outbound_rows if _day(r) == day}
            route_checks.append(build_fact_route_readiness(placement["rows"], day_demand, usable))
    unresolved_demand = sum(x["demand_relevant_cell_coverage"]["total"] - x["demand_relevant_cell_coverage"]["resolved"] for x in route_checks)
    if unresolved_demand: blockers.append({"code": "historical_cell_unresolved", "demand_relevant_cells": unresolved_demand})
    active_signature = _fingerprint(sorted(d["dataset_id"] for d in active_datasets(registry)))
    mapping_signature = _fingerprint(load_cell_mappings(root))
    source_conflict_count = sum(len(x["conflicts"]) for x in analyses.values()) + len(overlaps)
    placement_ready = not missing and not overlaps
    outbound_ready = bool(outbound_rows) and not analyses["outbound"]["conflicts"]
    receipts_ready = (not receipts_required or bool(set(required_days[:-1]) & receipt_dates)) and not analyses["receipts"]["conflicts"]
    vgh_ready = not missing_vgh and not analyses["vgh"]["conflicts"]
    cell_ready = not unresolved_demand and bool(route_checks)
    registry_ready = not registry_blockers
    return {"monthly_replay_ready": not blockers, "period_from": period_from, "period_to": period_to,
        "control_endpoint": required_days[-1], "placement_ready": placement_ready, "outbound_ready": outbound_ready,
        "receipts_ready": receipts_ready, "vgh_ready": vgh_ready, "cell_resolution_ready": cell_ready,
        "registry_ready": registry_ready, "hard_blockers": blockers, "warnings": warnings,
        "coverage": {"placement_checkpoints": len(required_days) - len(missing), "placement_checkpoints_required": len(required_days),
                     "demanded_sku": len(demanded), "vgh_covered_sku": len(demanded & vgh_keys),
                     "demand_relevant_unresolved_cells": unresolved_demand, "source_conflicts": source_conflict_count},
        "active_dataset_signature": active_signature, "cell_mapping_signature": mapping_signature}
